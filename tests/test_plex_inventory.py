import unittest
from unittest.mock import patch,AsyncMock
from datetime import datetime,timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db import Base
from app.services import library_summary as inv,library_audit as audit

CFG=dict(plex_server_url='http://synthetic',plex_server_token='synthetic')
def video(guid,path,size='100',kind='movie',**attrs):
    extra=' '.join(f'{k}="{v}"' for k,v in attrs.items())
    return f'<Video type="{kind}" guid="{guid}" ratingKey="{guid}" {extra}><Media><Part file="{path}"'+(f' size="{size}"' if size is not None else '')+'/></Media></Video>'
def page(nodes,total=None):return '<MediaContainer'+(f' totalSize="{total}"' if total is not None else '')+'>'+''.join(nodes)+'</MediaContainer>'

class PlexInventoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://');Base.metadata.create_all(self.engine);self.db=Session(self.engine)
    def tearDown(self):self.db.close();self.engine.dispose()
    async def test_plex_primary_deduplicates_shared_paths_and_title_collisions(self):
        libs=[dict(key='1',title='All',type='movie'),dict(key='2',title='Kids',type='movie'),dict(key='3',title='TV',type='show'),dict(key='4',title='Kids TV',type='show'),dict(key='5',title='Music',type='artist'),dict(key='6',title='Clips',type='movie',agent='tv.plex.agents.none')]
        def read(path,**kw):
            key=path.split('/')[3]
            if key=='1':return page([video('movie-a','/one.mkv'),video('remake-a','/two.mkv',title='Same'),video('remake-b','/three.mkv',title='Same')],3)
            if key=='2':return page([video('movie-a','/one.mkv'),video('movie-a','/one-4k.mkv','200')],2)
            if key in ('3','4'):
                if kw['type']==2:return '<MediaContainer totalSize="1"><Directory ratingKey="show-'+key+'" guid="plex://show/unique"/></MediaContainer>'
                return page([video('episode-a','/show/e1.mkv','300',kind='episode',grandparentRatingKey='show-'+key)],1)
            if key=='5':return page([video('track-a','/music/song.flac','50',kind='track')],1)
            return page([video('clip-a','/clips/one.mp4','20')],1)
        client=AsyncMock();client.libraries.return_value=libs;client.get.side_effect=read
        with patch.object(inv,'PlexClient',return_value=client),patch.object(inv,'arr_summary',AsyncMock()) as arr:
            report=await inv.summary(self.db,CFG)
            self.assertEqual((report['provider'],report['total_bytes'],report['file_count'],report['movies'],report['shows'],report['episodes'],report['tracks'],report['videos']),('Plex',870,7,3,1,1,1,1))
            self.assertTrue(report['complete']);arr.assert_not_awaited()
            await inv.summary(self.db,CFG);self.assertEqual(client.libraries.await_count,1)
    async def test_all_pages_beyond_250_and_unknown_total(self):
        client=AsyncMock();calls=[]
        def read(path,**kw):
            start=kw['X-Plex-Container-Start'];calls.append(start)
            return page([video(str(i),f'/movie/{i}.mkv','1') for i in range(start,min(start+500,1101))],1101)
        client.libraries.return_value=[dict(key='1',title='Movies',type='movie')];client.get.side_effect=read
        with patch.object(inv,'PlexClient',return_value=client):
            result=await inv.plex_summary(CFG)
            self.assertEqual(result['movies'],1101);self.assertEqual(result['total_bytes'],1101);self.assertEqual(calls,[0,500,1000])
        client.get.side_effect=[page([video(str(i),f'/{i}','1') for i in range(500)]),page([video('last','/last','1')])]
        nodes=[n async for n in inv.plex_items(client,dict(key='1'),1)];self.assertEqual(len(nodes),501)
    async def test_failed_refresh_keeps_last_complete_plex_totals(self):
        cached=dict(schema_version=2,provider='Plex',complete=True,checked_at=(datetime.utcnow()-timedelta(hours=1)).isoformat(),sources=[dict(source='All')],errors=[],total_bytes=1000)
        audit.save(self.db,'inventory-summary',cached)
        with patch.object(inv,'plex_summary',AsyncMock(side_effect=ValueError('failure'))),patch.object(inv,'arr_summary',AsyncMock()) as arr:
            stale=await inv.summary(self.db,CFG,refresh=True)
            self.assertEqual(stale['total_bytes'],1000);self.assertTrue(stale['refresh_failed']);self.assertEqual(stale['provider'],'Plex');arr.assert_not_awaited()
        with patch.object(inv,'plex_summary',AsyncMock(return_value=dict(provider='Plex',complete=False,errors=[dict(source='Music',error='failed')]))):
            stale=await inv.summary(self.db,CFG,refresh=True);self.assertEqual(stale['total_bytes'],1000)
    async def test_first_failure_uses_labeled_arr_fallback_and_old_cache_refreshes(self):
        arr=dict(provider='Arr',complete=True,checked_at=datetime.utcnow().isoformat(),sources=[dict(source='Arr')],errors=[],total_bytes=100)
        with patch.object(inv,'plex_summary',AsyncMock(side_effect=ValueError('failure'))),patch.object(inv,'arr_summary',AsyncMock(return_value=arr)):
            report=await inv.summary(self.db,CFG)
            self.assertEqual(report['provider'],'Arr');self.assertEqual(report['fallback_reason'],'Plex unavailable')
        audit.save(self.db,'inventory-summary',dict(arr,schema_version=1))
        with patch.object(inv,'plex_summary',AsyncMock()) as scan:
            report=await inv.summary(self.db,CFG);self.assertTrue(report['refreshing']);scan.assert_not_awaited()
    async def test_missing_sizes_are_partial_and_no_silent_truncated_page(self):
        client=AsyncMock();client.libraries.return_value=[dict(key='1',title='Movies',type='movie')]
        client.get.return_value=page([video('a','/one',None)],1)
        with patch.object(inv,'PlexClient',return_value=client):
            report=await inv.plex_summary(CFG);self.assertFalse(report['complete']);self.assertEqual(report['total_bytes'],0)
        client.get.side_effect=[page([video('a','/one')],2),page([],2)]
        with patch.object(inv,'PlexClient',return_value=client):
            report=await inv.plex_summary(CFG);self.assertFalse(report['complete']);self.assertTrue(report['errors'])
