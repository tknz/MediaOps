import copy
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from app.db import Base
from app import main
from app.api_auth import AuthContext
from app.services import library_actions as actions, streaming_availability as streaming, libraries as lib
from app.services.library_audit import save, state


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://');Base.metadata.create_all(self.engine);self.db=Session(self.engine)
    def tearDown(self):
        self.db.close();self.engine.dispose()
    def test_subscription_country_stale_and_unknown(self):
        details=dict(watchProviders=[dict(iso_3166_1='NZ',flatrate=[dict(id=8,name='Netflix')],buy=[dict(id=2,name='Apple Store')]),dict(iso_3166_1='CA',flatrate=[dict(id=9,name='Other')])])
        parsed=streaming.parse(details,'NZ');self.assertEqual(parsed['providers'],[dict(id=8,name='Netflix')])
        save(self.db,streaming.KEY+'-NZ',dict(items={'movie:1':parsed,'movie:2':dict(parsed,checked_at=(datetime.utcnow()-timedelta(days=2)).isoformat())}))
        inv=[dict(kind='movie',tmdb_id=i) for i in (1,2,3)]
        streaming.attach(self.db,inv,{'streaming_region':'NZ'})
        self.assertEqual([i['streaming']['status'] for i in inv],['known','stale','unknown'])
        self.assertEqual([streaming.matches(i,'8') for i in inv],[True,False,False])
        streaming.attach(self.db,inv,{'streaming_region':'CA'})
        self.assertTrue(all(i['streaming']['status']=='unknown' for i in inv))
        self.assertFalse(streaming.matches(inv[0],'none'))
    def test_stable_tv_identity_and_conflicting_plex_ids(self):
        item=dict(kind='series',tvdb_id=9)
        self.assertEqual(streaming.identity(item,[dict(kind='show',tvdb_id=9,tmdb_id=20)]),'tv:20')
        self.assertIsNone(streaming.identity(item,[dict(kind='show',tvdb_id=9,tmdb_id=20),dict(kind='show',tvdb_id=9,tmdb_id=21)]))
    async def test_refresh_deduplicates_titles_and_preserves_success_on_error(self):
        cfg=dict(streaming_region='NZ',seerr_url='http://seerr',seerr_api_key='test')
        inv=[dict(kind='movie',tmdb_id=1),dict(kind='movie',tmdb_id=1)]
        c=SimpleNamespace(get=AsyncMock(return_value=dict(id=1,watchProviders=[])))
        with patch.object(streaming,'SeerrClient',return_value=c): await streaming.refresh(self.db,inv,cfg)
        c.get.assert_awaited_once_with('/api/v1/movie/1')
        self.assertTrue(streaming.matches(inv[0],'none'))
        c.get=AsyncMock(side_effect=RuntimeError('private details'))
        with patch.object(streaming,'SeerrClient',return_value=c): await streaming.refresh(self.db,inv,cfg,force=True)
        cached=state(self.db,streaming.KEY+'-NZ')['items']['movie:1']
        self.assertIn('checked_at',cached);self.assertEqual(cached['error'],'RuntimeError');self.assertNotIn('private',str(cached))
    def test_platform_filter_precedes_pagination_and_full_totals(self):
        inv=[dict(id=i,title=f'Show {i:03}',kind='series',source='Kids',size=i+1,streaming=dict(status='known',providers=[dict(id=8,name='Netflix')])) for i in range(301)]
        inv.append(dict(id=400,title='A nonmatch',kind='series',source='Kids',size=9999,streaming=dict(status='known',providers=[])))
        page=lib.browse_page(inv,source='Kids',provider='8',sort='size',direction='desc',page=2,page_size=10)
        self.assertEqual(page['total_count'],301);self.assertEqual(page['total_bytes'],sum(range(1,302)))
        self.assertEqual(page['items'][0]['size'],291);self.assertIn('provider=8',page['next_url'])


class ActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://');Base.metadata.create_all(self.engine);self.db=Session(self.engine)
        self.series=dict(id=7,title='Example',path='/tv/Example',tvdbId=44,tmdbId=55,monitored=False,monitorNewItems='none',seasons=[dict(seasonNumber=1,monitored=False)],statistics=dict(episodeFileCount=1,sizeOnDisk=100))
        self.files=[dict(id=21,seriesId=7,path='/tv/Example/old.mkv',size=100)]
        self.episodes=[dict(id=1,seasonNumber=1,airDateUtc='2020-01-01T00:00:00Z',hasFile=True,episodeFileId=21,monitored=True),dict(id=2,seasonNumber=1,airDateUtc='2099-01-01T00:00:00Z',hasFile=False,monitored=False),dict(id=3,seasonNumber=1,airDateUtc='2020-02-01T00:00:00Z',hasFile=False,monitored=True)]
        self.c=SimpleNamespace(cfg=SimpleNamespace(url='http://sonarr-kids'),get=AsyncMock(side_effect=self.get),put_json=AsyncMock(side_effect=self.put),delete_json=AsyncMock(side_effect=self.delete_files),delete_series=AsyncMock())
        self.patcher=patch.object(lib,'_client_for_action',return_value=dict(client=self.c,name='Sonarr Kids',index=1));self.patcher.start()
    def tearDown(self):
        self.patcher.stop();self.db.close();self.engine.dispose()
    async def get(self,path,**params):
        if path=='/api/v3/series/7': return copy.deepcopy(self.series)
        if path=='/api/v3/episodefile': return copy.deepcopy(self.files)
        if path=='/api/v3/episode': return copy.deepcopy(self.episodes)
        if path=='/api/v3/queue': return dict(records=[],totalRecords=0)
        raise AssertionError(path)
    async def put(self,path,payload):
        if path=='/api/v3/episode/monitor':
            for e in self.episodes:
                if e['id'] in payload['episodeIds']:e['monitored']=payload['monitored']
        elif path=='/api/v3/series/editor':self.series.update(monitored=payload['monitored'],monitorNewItems=payload['monitorNewItems'])
        elif path=='/api/v3/series/7': self.series=copy.deepcopy(payload)
        return payload
    async def delete_files(self,path,payload):
        self.assertEqual(path,'/api/v3/episodefile/bulk');self.assertEqual(payload,dict(episodeFileIds=[21]))
        self.files=[]
    async def review_token(self): return actions.token(await actions.inspect(self.db,'series',1,7,{}))
    async def test_files_only_keeps_show_and_prevents_backfill_and_replay(self):
        token=await self.review_token()
        result=await actions.execute(self.db,token,'files_future','admin',{})
        self.assertTrue(result['ok']);self.c.delete_series.assert_not_awaited()
        self.assertTrue(self.series['monitored']);self.assertEqual(self.series['monitorNewItems'],'all')
        self.assertTrue(self.series['seasons'][0]['monitored']);self.assertEqual([e['monitored'] for e in self.episodes],[False,True,False])
        self.c.delete_json.assert_awaited_once()
        with self.assertRaisesRegex(ValueError,'already submitted'):await actions.execute(self.db,token,'files_future','admin',{})
        self.c.delete_json.assert_awaited_once()
    async def test_changed_files_block_all_writes(self):
        token=await self.review_token();self.files[0]['size']=999
        with self.assertRaisesRegex(ValueError,'changed'):await actions.execute(self.db,token,'files_future','admin',{})
        self.c.put_json.assert_not_awaited();self.c.delete_json.assert_not_awaited()
    async def test_active_playback_and_download_block_writes(self):
        token=await self.review_token()
        cfg=dict(plex_server_url='http://plex',plex_server_token='test')
        with patch.object(actions,'PlexClient',return_value=SimpleNamespace(get=AsyncMock(return_value='<MediaContainer><Video grandparentTitle="Example"/></MediaContainer>'))):
            with self.assertRaisesRegex(ValueError,'playing'):await actions.execute(self.db,token,'remove','admin',cfg)
        with patch('app.services.library_audit.full_queue',AsyncMock(return_value=[dict(seriesId=7)])):
            with self.assertRaisesRegex(ValueError,'download activity'):await actions.execute(self.db,token,'remove','admin',{})
        self.c.put_json.assert_not_awaited();self.c.delete_series.assert_not_awaited()
    async def test_full_remove_uses_native_series_delete(self):
        token=await self.review_token();await actions.execute(self.db,token,'remove','admin',{})
        self.c.delete_series.assert_awaited_once_with(7,delete_files=True);self.c.delete_json.assert_not_awaited()
    async def test_unconfirmed_monitoring_never_deletes(self):
        token=await self.review_token();self.c.put_json=AsyncMock()
        with self.assertRaisesRegex(ValueError,'Monitoring was not confirmed'):await actions.execute(self.db,token,'files_future','admin',{})
        self.c.delete_json.assert_not_awaited()
    async def test_partial_delete_is_durable_and_not_replayed(self):
        token=await self.review_token();self.c.delete_json=AsyncMock(side_effect=TimeoutError())
        with self.assertRaises(TimeoutError):await actions.execute(self.db,token,'files_future','admin',{})
        self.assertEqual(state(self.db,'library-delete-'+actions.digest(token))['status'],'needs_review')
        with self.assertRaisesRegex(ValueError,'already submitted'):await actions.execute(self.db,token,'files_future','admin',{})
        self.c.delete_json.assert_awaited_once()
