import unittest
from datetime import datetime,timedelta
from unittest.mock import AsyncMock,patch
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db import Base
from app.services.library_audit import *

class AuditTests(unittest.IsolatedAsyncioTestCase):
    def test_codec_neutral_and_estimates(self):
        files=[file_record(dict(id=1,size=30_000_000_000,mediaInfo=dict(runTime='2:00:00',resolution='1920x1080',videoCodec=c)),dict(id=1,title=c,tmdbId=12),'Adult','movie') for c in ['AV1','x265','MPEG2','x264']]
        r=efficiency_report(files,8)
        self.assertEqual(len(r['candidates']),4)
        self.assertEqual(r['candidates'][0]['scenario_saving_bytes'],22_800_000_000)
        self.assertIsNone(duration_seconds('unknown'));self.assertEqual(duration_seconds('45:30'),2730)
        with self.assertRaises(ValueError):efficiency_report(files,0)
    def test_duplicates_require_external_identity(self):
        rows=[dict(kind='movie',identity=12,title='One',source='main'),dict(kind='movie',identity=12,title='One',source='kids'),dict(kind='movie',identity=13,title='One',source='main'),dict(kind='movie',identity=None,title='One',source='kids')]
        r=duplicate_groups(rows);self.assertEqual(len(r),1);self.assertIsNone(r[0]['reclaimable_bytes']);self.assertEqual(len(r[0]['copies']),2)
    def test_stagnant_progress_and_recovery(self):
        now=datetime(2026,10,17);rows=[dict(id=1,downloadId='d1',sizeleft=100,status='downloading')]
        findings,obs=queue_findings(rows,{},now);self.assertEqual(findings,[])
        findings,obs=queue_findings(rows,obs,now+timedelta(hours=3));self.assertEqual(findings[0]['category'],'possible stall')
        rows[0]['sizeleft']=50;findings,obs=queue_findings(rows,obs,now+timedelta(hours=4));self.assertEqual(findings,[])
    async def test_queue_paginates_and_never_mutates(self):
        client=AsyncMock();client.get.side_effect=[dict(records=[{'id':i} for i in range(200)],totalRecords=201),dict(records=[{'id':201}],totalRecords=201)]
        result=await full_queue(client);self.assertEqual(len(result),201);client.post.assert_not_called();client.delete.assert_not_called()
    async def test_health_changed_only_on_meaningful_changes(self):
        engine=create_engine('sqlite://');Base.metadata.create_all(engine)
        client=AsyncMock();client.get.side_effect=lambda path,**kw: [] if path.endswith('health') else dict(records=[],totalRecords=0)
        with Session(engine) as db,patch('app.services.library_audit.arr_clients',return_value=[dict(index=0,name='Test',client=client)]):
            a=await health_audit(db);b=await health_audit(db)
            self.assertTrue(a['changed']);self.assertFalse(b['changed']);self.assertEqual(b['issue_count'],0)
            client.post.assert_not_called();client.put_json.assert_not_called();client.delete.assert_not_called()
        engine.dispose()

if __name__=='__main__':unittest.main()

class ScheduleTests(unittest.TestCase):
    def test_persisted_due_time_survives_restart(self):
        engine=create_engine('sqlite://');Base.metadata.create_all(engine)
        now=datetime(2026,10,17)
        with Session(engine) as db:
            self.assertEqual(next_run(db,'storage',timedelta(weeks=1),now),now+timedelta(minutes=5))
            save(db,'storage',dict(checked_at=now.isoformat()))
            self.assertEqual(next_run(db,'storage',timedelta(weeks=1),now+timedelta(days=1)),now+timedelta(weeks=1))
        engine.dispose()

class PlexDuplicateTests(unittest.IsolatedAsyncioTestCase):
    async def test_combined_library_is_not_duplicate_but_second_file_is(self):
        from app.services.clients import PlexClient
        cfg=dict(plex_server_url='http://synthetic',plex_server_token='synthetic-token')
        def xml(path):return f'<MediaContainer totalSize="1"><Video type="movie" guid="plex://movie/one" title="One"><Media videoCodec="hevc"><Part file="{path}" size="100"/></Media></Video></MediaContainer>'
        libs=[dict(key='1',title='All',type='movie'),dict(key='4',title='Kids',type='movie')]
        with patch.object(PlexClient,'libraries',AsyncMock(return_value=libs)),patch.object(PlexClient,'get',AsyncMock(side_effect=[xml('/kids/one.mkv'),xml('/kids/one.mkv')])):
            groups,errors,status=await plex_duplicate_groups(cfg);self.assertEqual(groups,[]);self.assertEqual(errors,[])
        with patch.object(PlexClient,'libraries',AsyncMock(return_value=libs)),patch.object(PlexClient,'get',AsyncMock(side_effect=[xml('/main/one.mkv'),xml('/kids/one.mkv')])):
            groups,errors,status=await plex_duplicate_groups(cfg);self.assertEqual(len(groups),1);self.assertEqual(len(groups[0]['copies']),2);self.assertIsNone(groups[0]['reclaimable_bytes'])
