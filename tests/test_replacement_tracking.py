import copy
import unittest
from unittest.mock import AsyncMock,patch
from sqlalchemy import create_engine,select
from sqlalchemy.orm import Session
from app.db import Base
from app.models import LibraryAuditState
from app.services import replacements as r, replacement_tracking as t,library_audit as a
from test_replacements import movie,release,GB

def task():
    m=movie();inst=dict(index=0,name='Main');row=r.row(m,inst);row.update(candidate=r.choose(m,[release()]),status='Ready')
    return dict(id=t.operation_id(row),key=row['key'],fingerprint=row['fingerprint'],index=0,source='Main',movie_id=1,title='Hellboy',candidate=row['candidate'],original_bytes=60*GB,status='Awaiting Radarr',attempted=True,approved_at='2026-10-17T12:00:00',download_id=None)

def grab(**changes):
    value=dict(id=1,date='2026-10-17T12:00:01Z',eventType='grabbed',sourceTitle=release()['title'],downloadId='SABnzbd_nzo_test',movieId=1,data={});value.update(changes);return value

def imported(**changes):
    value=dict(id=2,date='2026-10-17T12:01:00Z',eventType='downloadFolderImported',sourceTitle='renamed.mkv',downloadId='SABnzbd_nzo_test',movieId=1,data=dict(fileId=2,importedPath='/movies/Hellboy (2019)/new.mkv',size=str(10*GB)));value.update(changes);return value

class SelectionTests(unittest.TestCase):
    def test_above_target_fallback_only_for_size_rejection(self):
        big=release(size=29*GB,approved=False,rejections=['27.3 GB is larger than maximum allowed 13.8 GB (for Hellboy)'])
        chosen=r.choose(movie(),[big]);self.assertTrue(chosen['above_target']);self.assertEqual(chosen['bytes'],29*GB)
        self.assertFalse(r.eligible(movie(),big));self.assertTrue(r.eligible(movie(),big,above_target=True))
        for changes in [dict(rejections=big['rejections']+['English is wanted, but found French']),dict(rejections=['Existing file on disk is of equal or higher preference']),dict(quality=dict(quality=dict(resolution=2160,modifier='remux'))),dict(customFormatScore=-50),dict(size=60*GB),dict(edition='Extended')]:
            with self.subTest(changes=changes):self.assertFalse(r.eligible(movie(),dict(big,**changes),above_target=True))
        self.assertFalse(r.choose(movie(),[big,release()])['above_target'])
        marginal=release(size=59*GB,approved=False,rejections=big['rejections'])
        self.assertTrue(r.choose(movie(),[marginal])['fallback']);self.assertEqual(r.choose(movie(),[big,marginal])['bytes'],29*GB)
    def test_all_database_operation_ids_fit_postgres_constraint(self):
        limit=LibraryAuditState.__table__.c.key.type.length
        for key in [t.operation_id(dict(key='0:1',fingerprint='x'*64,candidate=dict(key='y'*64))),r.operation_key('compact-release-',{'long':'x'*1000}),r.operation_key('compact-grab-','token'*200)]:self.assertLessEqual(len(key),limit)
    def test_import_requires_exact_download_and_present_imported_file(self):
        m=movie();m['movieFile'].update(id=2,size=10*GB,path='/movies/Hellboy (2019)/new.mkv')
        result=t.reconcile(task(),m,[],[grab(),imported()]);self.assertEqual(result['status'],'Imported');self.assertEqual(result['actual_saving_bytes'],50*GB)
        m['movieFile']['id']=3
        self.assertEqual(t.reconcile(task(),m,[],[grab(),imported()])['status'],'Needs review')
        self.assertNotEqual(t.reconcile(task(),m,[],[grab(downloadId='other'),imported()])['status'],'Imported')
        self.assertNotEqual(t.reconcile(task(),m,[],[grab(date='2020-01-01T00:00:00Z'),imported()])['status'],'Imported')
    def test_download_finished_is_not_imported_and_failures_visible(self):
        q=dict(movieId=1,downloadId='SABnzbd_nzo_test',status='completed',trackedDownloadState='importPending',size=10*GB,sizeleft=0)
        result=t.reconcile(task(),movie(),[q],[grab()]);self.assertEqual(result['status'],'Importing');self.assertEqual(result['progress'],100)
        q.update(trackedDownloadState='importBlocked',statusMessages=[dict(title='Blocked')]);self.assertEqual(t.reconcile(task(),movie(),[q],[grab()])['status'],'Import blocked')
        failed=grab(eventType='downloadFailed');self.assertEqual(t.reconcile(task(),movie(),[],[grab(),failed])['status'],'Failed')
    def test_title_collision_does_not_attach_other_download(self):
        result=t.reconcile(task(),movie(),[],[grab(sourceTitle='Hellboy.2004.2160p.HEVC')]);self.assertIsNone(result['download_id']);self.assertNotEqual(result['status'],'Imported')

    def test_native_retry_follows_failed_download_to_verified_import(self):
        entry=task();entry.update(status='Failed',download_id='SABnzbd_nzo_test')
        failure=grab(eventType='downloadFailed',date='2026-10-17T12:02:00Z')
        retry=grab(downloadId='SABnzbd_nzo_retry',sourceTitle='Hellboy.2019.2160p.HEVC.5.1',date='2026-10-17T12:02:13Z',data=dict(size=str(12*GB)))
        m=movie();m['movieFile'].update(id=2,size=11*GB)
        event=imported(downloadId='SABnzbd_nzo_retry',date='2026-10-17T12:05:00Z')
        result=t.reconcile(entry,m,[],[event,retry,failure,grab()])
        self.assertEqual(result['status'],'Imported');self.assertEqual(result['download_id'],'SABnzbd_nzo_retry')
        self.assertEqual(result['actual_bytes'],11*GB);self.assertEqual(result['retry_count'],1)
        self.assertEqual(result['requested_candidate'],entry['candidate']);self.assertEqual(result['attempts'][0]['download_id'],entry['download_id'])
        self.assertEqual(t.reconcile(result,m,[],[event,retry,failure,grab()])['retry_count'],1)
    def test_retry_ignores_other_movies_unrelated_later_grabs_and_missing_evidence(self):
        entry=task();entry.update(status='Failed',download_id='SABnzbd_nzo_test')
        failure=grab(eventType='downloadFailed',date='2026-10-17T12:02:00Z')
        retry=grab(downloadId='other',sourceTitle='Hellboy.2019.2160p.HEVC',date='2026-10-17T12:02:13Z',data=dict(size=12*GB))
        for change in [dict(movieId=9),dict(date='2026-10-17T12:05:00Z'),dict(data={}),dict(downloadId=None)]:
            result=t.reconcile(entry,movie(),[],[failure,dict(retry,**change),grab()])
            self.assertEqual(result['status'],'Failed');self.assertEqual(result['download_id'],entry['download_id'])
        result=t.reconcile(entry,movie(),[dict(movieId=1,downloadId='other',status='completed')],[failure,retry,grab()])
        self.assertEqual(result['status'],'Importing');self.assertNotEqual(result['status'],'Imported')

class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://');Base.metadata.create_all(self.engine);self.db=Session(self.engine)
    def tearDown(self):self.db.close();self.engine.dispose()
    def ready(self):
        row=r.row(movie(),dict(index=0,name='Main'));row.update(status='Ready',candidate=r.choose(movie(),[release()]));a.save(self.db,r.KEY,dict(rows=[row],status='complete',done=1));return row
    async def test_reservation_blocks_rerun_across_report_refresh_and_import(self):
        row=self.ready();entry=t.reserve(self.db,[row],'admin')[0]
        with self.assertRaises(ValueError):t.reserve(self.db,[row],'admin')
        changed=copy.deepcopy(row);changed['candidate']['key']='new-release';self.assertIsNotNone(t.for_row(self.db,changed))
        t.save(self.db,entry,status='Imported');self.assertEqual(r.payload(self.db)['rows'],[]);self.assertEqual(t.summary(self.db)['imported'],1)
        changed['fingerprint']='new-file';self.assertIsNone(t.for_row(self.db,changed))
    async def test_legacy_submission_adopted_without_sending(self):
        row=self.ready();a.save(self.db,r.operation_key('compact-release-',[row['key'],row['fingerprint'],row['candidate']['key']]),dict(at='2026-10-17T12:00:00',actor='admin'))
        t.legacy_import(self.db);self.assertTrue(t.tasks(self.db)[0]['attempted']);self.assertEqual(len(t.tasks(self.db)),1);t.legacy_import(self.db);self.assertEqual(len(t.tasks(self.db)),1)
    async def test_resume_skips_uncertain_submission_and_never_resends(self):
        row=self.ready();entry=t.reserve(self.db,[row],'admin')[0];t.save(self.db,entry,status='Needs review',attempted=True)
        a.save(self.db,r.KEY+'-approval',dict(active=True,plan=dict(rows=[dict(key=row['key'],fingerprint=row['fingerprint'],candidate=row['candidate']['key'])]),job_id=r.operation_key('compact-grab-','token'),actor='admin',approved_at=r.stamp()))
        c=AsyncMock()
        with patch.object(r.libraries,'arr_clients',return_value=[dict(index=0,name='Main',client=c)]):self.assertEqual(await r.approve(self.db,None,'admin'),0)
        c.post_json.assert_not_awaited();c.put_json.assert_not_awaited()
    async def test_native_outage_retains_status_and_does_not_resend(self):
        row=self.ready();entry=t.reserve(self.db,[row],'admin')[0];t.save(self.db,entry,status='Downloading',attempted=True,download_id='SABnzbd_nzo_test');c=AsyncMock();c.movies.side_effect=TimeoutError()
        with patch.object(t.libraries,'arr_clients',return_value=[dict(index=0,name='Main',client=c)]):await t.refresh(self.db)
        result=t.tasks(self.db)[0];self.assertEqual(result['status'],'Downloading');self.assertIn('unavailable',result['tracking_error']);c.post_json.assert_not_awaited()

    async def test_refresh_revisits_failed_jobs_and_records_native_retry_without_submitting(self):
        entry=task();entry.update(status='Failed',download_id='SABnzbd_nzo_test');a.save(self.db,entry['id'],entry)
        m=movie();m['movieFile'].update(id=2,size=11*GB)
        events=[grab(eventType='downloadFailed',date='2026-10-17T12:02:00Z'),
            grab(downloadId='retry',sourceTitle='Hellboy.2019.2160p.HEVC.5.1',date='2026-10-17T12:02:13Z',data=dict(size=12*GB)),
            imported(downloadId='retry',date='2026-10-17T12:05:00Z')]
        c=AsyncMock();c.movies.return_value=[m];c.get.side_effect=lambda path,**kw:dict(records=[],totalRecords=0) if path.endswith('/queue') else events
        with patch.object(t,'now',return_value='2026-10-17T12:06:00'),patch.object(t.libraries,'arr_clients',return_value=[dict(index=0,name='Main',client=c)]):
            self.assertTrue(await t.refresh(self.db))
        result=t.tasks(self.db)[0];self.assertEqual(result['status'],'Imported');self.assertEqual(result['retry_count'],1)
        c.post_json.assert_not_awaited();c.put_json.assert_not_awaited();c.delete.assert_not_awaited()

class CurrentInventoryTests(unittest.IsolatedAsyncioTestCase):
    setUp=PersistenceTests.setUp
    tearDown=PersistenceTests.tearDown
    ready=PersistenceTests.ready
    def plex_file(self,m,**changes):
        f=dict(kind='movie',title=m['title'],year=m['year'],tmdb_id=m['tmdbId'],path='/plex/Movies/Hellboy (2019)/film.mkv',bytes=m['movieFile']['size'],sources=['Movies'],resolution='4k',codec='h264')
        f.update(changes);return f
    async def test_plex_inventory_and_filters_include_unmanaged_without_actions(self):
        m=movie();m['path']='/movies/Hellboy (2019)';c=AsyncMock();c.movies.return_value=[m];c.get.return_value=[]
        inst=dict(index=0,name='Main',client=c)
        files=[self.plex_file(m),self.plex_file(m,title='Outside',tmdb_id=99,path='/plex/Movies/Outside/film.mkv',bytes=80*GB)]
        a.save(self.db,'plex-file-catalogue',dict(files=files,complete=True))
        with patch.object(r.libraries,'arr_clients',return_value=[inst]):
            values=await r.current_rows(self.db,min_gb=25,source='Movies')
            self.assertEqual([v[0]['title'] for v in values],['Outside','Hellboy'])
            self.assertIsNone(values[0][1]);self.assertEqual(values[0][0]['status'],'Outside Radarr')
            report=await r.current_payload(self.db,q='Hellboy',source='Movies',min_gb=25)
            self.assertEqual(len(report['rows']),1);self.assertEqual(report['filters']['source'],'Movies')
        c.post_json.assert_not_awaited();c.delete.assert_not_awaited()
    async def test_import_hidden_after_file_change_and_uncached_active_job_visible(self):
        old=self.ready();entry=t.reserve(self.db,[old],'admin')[0]
        m=movie();m['path']='/movies/Hellboy (2019)';c=AsyncMock();c.movies.return_value=[m];c.get.return_value=[]
        a.save(self.db,'plex-file-catalogue',dict(files=[self.plex_file(m)],complete=True))
        # Irishman case: durable task exists but no cached search row.
        a.save(self.db,r.KEY,dict(rows=[],status='complete',done=0))
        t.save(self.db,entry,status='Downloading',attempted=True)
        with patch.object(r.libraries,'arr_clients',return_value=[dict(index=0,name='Main',client=c)]):
            report=await r.current_payload(self.db)
            self.assertEqual(report['rows'][0]['status'],'Downloading');self.assertEqual(report['rows'][0]['operation']['id'],entry['id'])
            self.assertEqual(report['ready'],0)
            # Completed replacement has a new file identity and current size.
            m['movieFile'].update(id=5,size=10*GB)
            t.save(self.db,entry,status='Imported',file_id=5,actual_bytes=10*GB)
            a.save(self.db,'plex-file-catalogue',dict(files=[self.plex_file(m)],complete=True))
            report=await r.current_payload(self.db)
            self.assertEqual(report['rows'],[]);self.assertEqual(report['operations']['imported'],1)
        c.post_json.assert_not_awaited()
    async def test_same_title_different_path_and_size_mismatch_cannot_offer(self):
        m=movie();m['path']='/movies/Hellboy (2019)';c=AsyncMock();c.movies.return_value=[m];c.get.return_value=[];c.releases.return_value=[release()]
        for f in [self.plex_file(m,path='/plex/Other/Hellboy (2019)/other.mkv'),self.plex_file(m,bytes=70*GB)]:
            a.save(self.db,'plex-file-catalogue',dict(files=[f],complete=True))
            with patch.object(r.libraries,'arr_clients',return_value=[dict(index=0,name='Main',client=c)]),patch.object(r.asyncio,'sleep',AsyncMock()):
                report=await r.scan(self.db)
                self.assertNotEqual(report['rows'][0]['status'],'Ready');self.assertIsNone(report['rows'][0]['candidate'])
        c.releases.assert_not_awaited();c.post_json.assert_not_awaited()

    async def test_duplicate_plex_copies_cannot_share_an_arr_action(self):
        m=movie();m['path']='/movies/Hellboy (2019)';c=AsyncMock();c.movies.return_value=[m];c.get.return_value=[]
        files=[self.plex_file(m),self.plex_file(m,path='/another/library/Hellboy (2019)/film.mkv')]
        a.save(self.db,'plex-file-catalogue',dict(files=files,complete=True))
        with patch.object(r.libraries,'arr_clients',return_value=[dict(index=0,name='Main',client=c)]):
            rows=await r.current_rows(self.db)
        self.assertEqual(len(rows),2);self.assertEqual(len({row['key'] for row,_ in rows}),2)
        self.assertTrue(all(owner is None and row['status']=='File match needs review' for row,owner in rows))
        c.post_json.assert_not_awaited()
