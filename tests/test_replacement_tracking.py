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
        for changes in [dict(rejections=big['rejections']+['English is wanted, but found French']),dict(rejections=['Existing file on disk is of equal or higher preference']),dict(title='Hellboy.2019.2160p.HDR.HEVC'),dict(quality=dict(quality=dict(resolution=2160,modifier='remux'))),dict(customFormatScore=-50),dict(size=60*GB),dict(edition='Extended')]:
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
        t.save(self.db,entry,status='Imported');self.assertEqual(r.payload(self.db)['rows'][0]['status'],'Imported')
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
