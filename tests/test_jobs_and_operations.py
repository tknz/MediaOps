import asyncio
import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from app import main, operations
from app.api_auth import AuthContext
from app.db import Base
from app.services import job_engine as jobs, library_audit as audit
from app.services.job_registry import JOBS, BY_ID, SCHEDULE_DEFAULTS


class JobsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db=Session(self.engine); self.factory=sessionmaker(bind=self.engine)
        self.cfg=dict(SCHEDULE_DEFAULTS)
        self.scheduler=Mock(); self.registered={}
        self.scheduler.get_job.side_effect=lambda key:self.registered.get(key)
        def add(function, trigger, **kwargs):
            if kwargs['id'] in self.registered and not kwargs.get('replace_existing'): raise ValueError('Duplicate job')
            self.registered[kwargs['id']]=SimpleNamespace(next_run_time=kwargs.get('next_run_time'),function=function,kwargs=kwargs)
        self.scheduler.add_job.side_effect=add
        self.patches=[patch.object(jobs,'SessionLocal',self.factory),patch.object(jobs,'all_settings',lambda:self.cfg),
                      patch.object(jobs,'set_settings',lambda values:self.cfg.update(values)),
                      patch.object(jobs,'scheduler',None),patch.object(jobs,'handlers',{}),patch.object(jobs,'locks',{})]
        for p in self.patches:p.start()
        self.namespace={spec.handler:AsyncMock() for spec in JOBS}
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.db.close();self.engine.dispose()

    async def test_registry_registers_every_recurring_job_and_exposes_old_reports(self):
        audit.save(self.db,'storage',dict(checked_at=datetime.utcnow().isoformat(),complete=True,files=[{}],duplicates=[{}],errors=[]))
        jobs.install(self.scheduler,self.namespace)
        self.assertEqual(set(self.registered),{j.id for j in JOBS if j.setting})
        listing=jobs.listing(self.db)
        self.assertEqual(len(listing),len(JOBS))
        storage=next(j for j in listing if j['id']=='library-storage')
        self.assertEqual(storage['last_report']['duplicate_groups'],1)
        self.assertEqual(storage['value'],168)

    async def test_manual_and_scheduled_share_tracking_and_prevent_overlapping_runs(self):
        gate=asyncio.Event();started=asyncio.Event()
        @jobs.tracked('library-storage')
        async def scan():
            started.set(); await gate.wait()
            with self.factory() as db:audit.save(db,'storage',dict(complete=False,errors=[{}],files=[],duplicates=[]))
        self.namespace[BY_ID['library-storage'].handler]=scan
        jobs.install(self.scheduler,self.namespace)
        queued=jobs.enqueue(self.db,'library-storage',actor='owner')
        with self.assertRaisesRegex(ValueError,'queued'):jobs.enqueue(self.db,'library-storage')
        task=asyncio.create_task(scan()); await started.wait()
        with self.assertRaisesRegex(ValueError,'running'):jobs.enqueue(self.db,'library-storage')
        self.db.expire_all();self.assertEqual(jobs.detail(self.db,'library-storage')['status'],'running')
        gate.set();await task;self.db.expire_all()
        last=jobs.detail(self.db,'library-storage')['last_run']
        self.assertEqual(last['id'],queued['run_id']);self.assertEqual(last['trigger'],'manual')
        self.assertEqual(last['status'],'completed_with_issues');self.assertIn('duration_seconds',last)

    async def test_failures_are_sanitized_and_restart_marks_interrupted(self):
        @jobs.tracked('library-health')
        async def fail():raise RuntimeError('https://private/?apikey=do-not-return')
        with self.assertRaises(RuntimeError):await fail()
        self.db.expire_all();last=jobs.detail(self.db,'library-health')['last_run']
        self.assertEqual(last['status'],'failed');self.assertNotIn('apikey',json.dumps(last))
        jobs.record(self.db,'library-storage',dict(id='old',status='running',started_at='2026-01-01T00:00:00'))
        jobs.install(self.scheduler,self.namespace);self.db.expire_all()
        self.assertEqual(jobs.detail(self.db,'library-storage')['status'],'interrupted')

    async def test_schedule_controls_and_settings_have_one_interval_and_persist_pause(self):
        jobs.install(self.scheduler,self.namespace)
        jobs.schedule(self.db,'library-health',value=45,enabled=False)
        self.assertEqual(self.cfg['job_library_health_minutes'],'45')
        self.scheduler.pause_job.assert_called_with('library-health')
        jobs.install(self.scheduler,self.namespace)
        self.assertIsNone(self.registered['library-health'].next_run_time)
        self.assertEqual(jobs.detail(self.db,'library-health')['value'],45)
        self.cfg['job_library_health_minutes']='60' # Settings form save
        jobs.install(self.scheduler,self.namespace)
        self.assertEqual(jobs.detail(self.db,'library-health')['value'],60)
        with self.assertRaises(ValueError):jobs.schedule(self.db,'library-health',value=0)
        with self.assertRaises(ValueError):jobs.enqueue(self.db,'compact-replacement-approval')

    async def test_history_is_bounded_and_reconfigure_does_not_interrupt_active_runs(self):
        jobs.install(self.scheduler,self.namespace)
        for i in range(40): jobs.record(self.db,'library-health',dict(id=str(i),status='completed'))
        self.assertEqual(len(jobs.detail(self.db,'library-health')['runs']),jobs.HISTORY_LIMIT)
        jobs.record(self.db,'library-health',dict(id='active',status='running'))
        jobs.install(self.scheduler,self.namespace)
        self.assertEqual(jobs.detail(self.db,'library-health')['status'],'running')

    async def test_environment_owned_interval_cannot_report_an_unsaved_change(self):
        jobs.install(self.scheduler,self.namespace)
        with patch.object(jobs,'env_owned',return_value={'job_library_health_minutes'}):
            with self.assertRaisesRegex(ValueError,'environment variable'):
                jobs.schedule(self.db,'library-health',value=45)
            self.scheduler.reschedule_job.assert_not_called()
            jobs.schedule(self.db,'library-health',enabled=False)
            self.scheduler.pause_job.assert_called_with('library-health')

    async def test_actual_apscheduler_executes_the_registered_manual_callback(self):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        scheduler=AsyncIOScheduler()
        ran=asyncio.Event()
        @jobs.tracked('library-storage')
        async def scan():ran.set()
        self.namespace[BY_ID['library-storage'].handler]=scan
        jobs.install(scheduler,self.namespace)
        scheduler.start(paused=True)
        try:
            queued=jobs.enqueue(self.db,'library-storage',actor='test')
            scheduler.resume()
            await asyncio.wait_for(ran.wait(),2)
            await asyncio.sleep(.03)
            self.db.expire_all()
            run=jobs.detail(self.db,'library-storage')['last_run']
            self.assertEqual(run['id'],queued['run_id']);self.assertEqual(run['status'],'completed')
        finally:scheduler.shutdown(wait=False)


class OperationRoutesTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
        Base.metadata.create_all(self.engine);self.db=Session(self.engine)
        main.app.dependency_overrides[main.get_db]=lambda:self.db
        main.app.dependency_overrides[operations.get_db]=lambda:self.db
        self.context=AuthContext('owner',True)
        main.app.dependency_overrides[main.require_auth]=lambda:self.context
        self.user=SimpleNamespace(is_admin=True,username='owner')
        self.client=TestClient(main.app,headers={'Origin':'http://testserver'})
    def tearDown(self):
        main.app.dependency_overrides.clear();self.db.close();self.engine.dispose()
    def rpc(self,name,args=None):
        return self.client.post('/api/mcp',json=dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(name='mediaops.'+name,arguments=args or {})))
    def result(self,response):
        self.assertEqual(response.status_code,200,response.text)
        return json.loads(response.json()['result']['content'][0]['text'])

    def test_jobs_screen_rest_and_mcp_use_one_catalogue(self):
        with patch.object(main,'current_user',return_value=self.user):
            response=self.client.get('/jobs');self.assertEqual(response.status_code,200,response.text)
            self.assertIn('Duplicates &amp; storage efficiency',response.text)
            self.assertIn('Streaming availability',response.text)
            self.assertIn('/jobs/library-storage/run',response.text)
        rest=self.client.get('/api/jobs').json()
        mcp=self.result(self.rpc('jobs_list'))
        self.assertEqual(rest,mcp)
        self.assertEqual({j['id'] for j in rest['jobs']},set(BY_ID))
        self.assertEqual(self.client.get('/api/jobs/library-storage').status_code,200)
        self.assertEqual(self.client.get('/api/jobs/unknown').status_code,404)

    def test_mcp_discovery_permissions_and_schemas(self):
        names={t['name'] for t in self.client.post('/api/mcp',json=dict(id=1,method='tools/list')).json()['result']['tools']}
        self.assertTrue({'mediaops.duplicates_delete','mediaops.job_schedule','mediaops.download_control','mediaops.library_delete','mediaops.replacements_approve'} <= names)
        self.context=AuthContext('reader',False,frozenset({'mcp.read','jobs.read','libraries.read'}))
        response=self.client.post('/api/mcp',json=dict(id=1,method='tools/list')).json()
        tools=response['result']['tools'];names={t['name'] for t in tools}
        self.assertIn('mediaops.library_browse',names);self.assertNotIn('mediaops.duplicates_delete',names)
        self.assertEqual(self.client.get('/api/jobs').status_code,200)
        self.assertEqual(self.rpc('job_run',dict(job_id='library-storage')).status_code,403)
        self.assertEqual(self.rpc('duplicates_delete',dict(confirmation='token',confirm='delete')).status_code,403)
        self.context=AuthContext('plain-reader',False)
        self.assertEqual(self.client.get('/api/jobs').status_code,403)
        self.context=AuthContext('job-reader',False,frozenset({'jobs.read'}))
        self.assertEqual(self.rpc('overview').status_code,403)
        self.assertEqual(self.client.get('/api/integrations/homeassistant/status').status_code,403)

    def test_rest_and_mcp_queue_same_scan_and_reject_invalid_options(self):
        with patch.object(jobs,'enqueue',return_value=dict(status='queued',job_id='library-storage')) as enqueue:
            self.assertEqual(self.client.post('/api/jobs/run',json=dict(job_id='library-storage')).status_code,200)
            self.result(self.rpc('job_run',dict(job_id='library-storage')))
            self.assertEqual(enqueue.call_count,2)
            self.assertEqual(enqueue.call_args.kwargs,dict(actor='owner',job_id='library-storage',options={}))
            self.assertEqual(self.rpc('job_run',dict(job_id='library-storage',options=dict(delete=True))).status_code,422)
            self.assertEqual(enqueue.call_count,2)
        with patch.object(jobs,'enqueue',return_value=dict(status='queued')) as enqueue:
            with patch.object(main,'current_user',return_value=self.user):
                response=self.client.post('/libraries/audit/run',data=dict(audit='storage',view='duplicates'),follow_redirects=False)
            self.assertEqual(response.status_code,303)
            enqueue.assert_called_once_with(self.db,'library-storage',actor='owner')

    def test_media_actions_require_review_confirmation_and_share_services(self):
        with patch.object(operations.duplicate_actions,'remove_many',AsyncMock(return_value=2)) as remove,patch.object(main,'refresh_inventory_after_delete'):
            self.assertEqual(self.client.post('/api/libraries/duplicates/delete',json=dict(confirmation='token')).status_code,422)
            self.assertEqual(self.rpc('duplicates_delete',dict(confirmation='token')).status_code,422)
            remove.assert_not_awaited()
            result=self.result(self.rpc('duplicates_delete',dict(confirmation='reviewed-token',confirm='delete')))
            self.assertEqual(result['removed'],2)
            remove.assert_awaited_once_with(self.db,'reviewed-token',actor='owner')
        with patch.object(operations,'queue_replacement_approval',return_value=dict(status='queued')) as approve:
            self.assertEqual(self.client.post('/api/libraries/replacements/approve',json=dict(confirmation='token')).status_code,422)
            self.result(self.rpc('replacements_approve',dict(confirmation='token',confirm='replace')))
            approve.assert_called_once_with(self.db,'token','owner')
        self.context=AuthContext('scoped',False,frozenset({'libraries.delete','replacements.write'}))
        self.assertEqual(self.rpc('library_delete',dict(confirmation='token',confirm='delete',mode='files_future')).status_code,403)

    def test_filter_arguments_inventory_and_download_controls_are_available(self):
        args=dict(q='Example',kind='series',provider='8',source='Kids',page=2,page_size=10,viewer='')
        with patch.object(main,'browse_library_request',AsyncMock(return_value=dict(total_count=500,total_bytes=1000))) as browse:
            data=self.result(self.rpc('library_browse',args));self.assertEqual(data['total_count'],500)
            self.assertEqual(browse.call_args.kwargs['provider'],'8');self.assertEqual(browse.call_args.kwargs['page'],2)
        with patch.object(operations.library_summary,'summary',AsyncMock(return_value=dict(total_bytes=123,episodes=456))):
            self.assertEqual(self.client.get('/api/libraries/inventory').json()['total_bytes'],123)
            self.assertEqual(self.result(self.rpc('library_inventory'))['episodes'],456)
        with patch.object(operations.download_controls,'control',AsyncMock(return_value=dict(status=True))) as control:
            data=dict(download_id='SABnzbd_nzo_test',action='pause')
            self.result(self.rpc('download_control',data));control.assert_awaited_once_with('SABnzbd_nzo_test','pause','0')
        schema=self.client.get('/openapi.json').json()
        self.assertIn('JobRun',schema['components']['schemas'])
        self.assertIn('DeleteConfirmation',schema['components']['schemas'])

    def test_preferences_update_is_validated_scoped_and_does_not_touch_arr(self):
        with patch.object(operations,'set_settings') as save,patch.object(operations,'all_settings',return_value={}):
            self.result(self.rpc('library_preferences_update',dict(replacement_max_gb=20,streaming_region='nz')))
            save.assert_called_once_with(dict(replacement_max_gb='20.0',streaming_region='NZ'))
            self.assertEqual(self.rpc('library_preferences_update',dict(replacement_max_gb=-1)).status_code,422)
            self.assertEqual(save.call_count,1)
        self.context=AuthContext('writer',False,frozenset({'libraries.write'}))
        self.assertEqual(self.rpc('library_preferences_update',dict(streaming_region='NZ')).status_code,403)


if __name__=='__main__':unittest.main()
