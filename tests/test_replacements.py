import copy
import unittest
from unittest.mock import AsyncMock,patch
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db import Base
from app.services import replacements as r, library_audit as audit

GB=1_000_000_000

def movie(i=1,size=60*GB,monitored=False):
    return dict(id=i,tmdbId=i,title='Hellboy',year=2019,runtime=120,qualityProfileId=5,monitored=monitored,movieFile=dict(id=i,path='/movies/Hellboy (2019)/film.mkv',relativePath='film.mkv',size=size,edition='',quality=dict(quality=dict(resolution=2160,name='Remux-2160p',modifier='remux'))))

def release(**changes):
    value=dict(guid='private-guid',indexerId=1,title='Hellboy.2019.2160p.WEB.HEVC.SDR.DD2.0',movieTitles=['Hellboy'],quality=dict(quality=dict(resolution=2160,name='WEBDL-2160p',modifier='none')),size=10*GB,approved=True,downloadAllowed=True,rejections=[],languages=[dict(id=1,name='English')],edition='')
    value.update(changes);return value

class CandidateTests(unittest.TestCase):
    def test_resolution_codec_hdr_identity_language_size_and_native_rejection(self):
        m=movie();self.assertTrue(r.eligible(m,release()))
        for changes in [dict(approved=False),dict(rejections=['Blocked']),dict(title='Hellboy.2019.2160p.H264'),dict(title='Hellboy.2019.2160p.HEVC.HDR10'),dict(title='Hellboy.2019.2160p.HEVC.TrueHD.7.1'),dict(title='Hellboy.2004.2160p.HEVC'),dict(movieTitles=['A Different Film']),dict(languages=[dict(id=2)]),dict(edition='Extended'),dict(size=19*GB),dict(quality=dict(quality=dict(resolution=1080,modifier='none')))]:
            with self.subTest(changes=changes):self.assertFalse(r.eligible(m,release(**changes)))
    def test_audio_then_smallest_and_unknown_is_honest(self):
        stereo=release(size=11*GB);surround=release(title='Hellboy.2019.2160p.HEVC.5.1',size=8*GB)
        chosen=r.choose(movie(),[surround,stereo,release(size=10*GB)])
        self.assertEqual(chosen['bytes'],10*GB);self.assertEqual(chosen['audio'],'Stereo');self.assertEqual(chosen['range'],'SDR label')
        chosen=r.choose(movie(),[release(title='Hellboy.2019.2160p.HEVC')])
        self.assertEqual(chosen['audio'],'Not stated');self.assertEqual(chosen['range'],'No HDR label');self.assertNotIn('guid',chosen)

class JobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://');Base.metadata.create_all(self.engine);self.db=Session(self.engine)
    def tearDown(self):self.db.close();self.engine.dispose()
    async def test_full_inventory_sort_before_fifty_includes_unmonitored_both_sources(self):
        c1=AsyncMock();c2=AsyncMock();c1.movies.return_value=[movie(i,(i+1)*GB) for i in range(300)];c2.movies.return_value=[movie(999,500*GB)]
        c1.get.return_value=c2.get.return_value={'totalRecords':0};c1.releases.return_value=c2.releases.return_value=[release()]
        instances=[dict(index=0,name='Main',client=c1),dict(index=1,name='Kids',client=c2)]
        with patch.object(r.libraries,'arr_clients',return_value=instances),patch.object(r.asyncio,'sleep',AsyncMock()):
            report=await r.scan(self.db)
        self.assertEqual(len(report['rows']),50);self.assertEqual(report['rows'][0]['source'],'Kids');self.assertEqual(report['rows'][1]['movie_id'],299)
        self.assertEqual(report['done'],50);self.assertTrue(all(not x['monitored'] for x in report['rows']));c1.post_json.assert_not_awaited();c1.put_json.assert_not_awaited()
        self.assertNotIn('private-guid',str(report))
        report['rows'][0].update(status='No suitable release',note='Profile excludes some resolutions.')
        audit.save(self.db,r.KEY,report)
        self.assertEqual(r.payload(self.db)['rows'][0]['note'],'No approved release meets the replacement rules.')
    def setup_ready(self):
        m=movie();c=AsyncMock();c.get.side_effect=lambda path,**kw: {'totalRecords':0} if path.endswith('queue') else copy.deepcopy(m);c.releases.return_value=[release()]
        instance=dict(index=0,name='Main',client=c);row=r.row(m,instance);row.update(candidate=r.choose(m,[release()]),status='Ready');audit.save(self.db,r.KEY,dict(rows=[row],status='complete',done=1))
        return m,c,instance,row
    async def test_batch_preflight_fresh_native_approval_and_playback_no_mutation(self):
        m,c,inst,row=self.setup_ready();plex=AsyncMock();plex.sessions.return_value=[dict(file='/mnt/Plex/Movies/Hellboy (2019)/film.mkv')]
        with patch.object(r.libraries,'arr_clients',return_value=[inst]),patch.object(r.duplicate_actions,'client',return_value=plex):
            with self.assertRaisesRegex(ValueError,'playing now'):await r.approve(self.db,r.token([row]),'admin')
        c.post_json.assert_not_awaited();c.put_json.assert_not_awaited()
        plex.sessions.return_value=[];c.releases.return_value=[release(approved=False)]
        with patch.object(r.libraries,'arr_clients',return_value=[inst]),patch.object(r.duplicate_actions,'client',return_value=plex):
            with self.assertRaisesRegex(ValueError,'no longer approved'):await r.approve(self.db,r.token([row]),'admin')
        c.post_json.assert_not_awaited()
    async def test_approved_grab_leaves_monitoring_and_existing_file_intact_and_replay_blocked(self):
        m,c,inst,row=self.setup_ready();plex=AsyncMock();plex.sessions.return_value=[];token=r.token([row])
        with patch.object(r.libraries,'arr_clients',return_value=[inst]),patch.object(r.duplicate_actions,'client',return_value=plex):
            self.assertEqual(await r.approve(self.db,token,'admin'),1)
            with self.assertRaises(ValueError):await r.approve(self.db,token,'admin')
        c.post_json.assert_awaited_once_with('/api/v3/release',dict(guid='private-guid',indexerId=1,movieId=1));c.put_json.assert_not_awaited();c.delete.assert_not_awaited()
    async def test_ambiguous_grab_is_recorded_and_cannot_resend(self):
        m,c,inst,row=self.setup_ready();plex=AsyncMock();plex.sessions.return_value=[];c.post_json.side_effect=TimeoutError()
        with patch.object(r.libraries,'arr_clients',return_value=[inst]),patch.object(r.duplicate_actions,'client',return_value=plex):
            with self.assertRaisesRegex(ValueError,'Batch stopped'):await r.approve(self.db,r.token([row]),'admin')
            with self.assertRaises(ValueError):await r.approve(self.db,r.token([row]),'admin')
        self.assertEqual(c.post_json.await_count,1)
