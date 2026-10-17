import unittest
from datetime import datetime, timedelta
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import Session
from app.db import Base
from app.models import PlexSession, ActivePlexSession, PlexLibraryItem
from app.services.libraries import enrich_inventory_usage, browse_page, calendar_cutoff

NOW = datetime(2026, 10, 17, 12)

def item(title, size=100, year=2020, ident=1, source='Adult'):
    return dict(title=title, size=size, kind='series', id=ident, source=source, year=year, tvdb_id=ident, tmdb_id=None, path='/tv/'+title)

def session(title, when, user='owner', **kw):
    return dict(source='test', source_id=str(title)+str(when)+user, plex_user_id=user, username=user, title=title,
                media_type='episode', started_at=when, stopped_at=when, watched_seconds=5, **kw)

class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://'); Base.metadata.create_all(self.engine); self.db=Session(self.engine)
    def tearDown(self): self.db.close(); self.engine.dispose()
    def add(self, *rows): self.db.execute(insert(PlexSession), list(rows)); self.db.commit()
    def test_all_viewers_boundary_unknown_and_size(self):
        inv=[item('A recent'),item('Z big',900),item('B small',20),item('Boundary'),item('Unknown')]
        cutoff=calendar_cutoff(24,NOW)
        self.add(session('A recent - Old',cutoff-timedelta(days=1)), session('A recent - New',NOW-timedelta(days=1),'other'),
                 session('Z big - Pilot',cutoff-timedelta(seconds=1)),session('B small - Pilot',cutoff-timedelta(days=10)),session('Boundary - Pilot',cutoff))
        enrich_inventory_usage(self.db,inv)
        p=browse_page(inv,preset='tv-cleanup',now=NOW)
        self.assertEqual([i['title'] for i in p['items']],['Z big','B small']); self.assertEqual(p['total_bytes'],920)
        self.assertEqual(inv[0]['last_watched_by'],'other'); self.assertEqual(inv[-1]['history_status'],'unknown')
        enrich_inventory_usage(self.db,inv,'owner')
        self.assertIn('A recent',[i['title'] for i in browse_page(inv,preset='tv-cleanup',now=NOW)['items']])
        self.assertEqual(browse_page(inv,history='unknown')['total_count'],1)
    def test_no_session_or_item_cutoff_and_source_page(self):
        inv=[item('ZZ oldest',999,ident=1,source='Kids')]+[item(f'A{i:04}',10,ident=i+2) for i in range(300)]
        old=NOW-timedelta(days=900)
        self.add(session('ZZ oldest - Pilot',old))
        rows=[session('Unrelated',NOW, str(i)) for i in range(60001)]
        self.db.execute(insert(PlexSession),rows);self.db.commit()
        enrich_inventory_usage(self.db,inv)
        self.assertEqual(inv[0]['plays'],1)
        p=browse_page(inv,sort='size',direction='desc',page_size=25)
        self.assertEqual(p['items'][0]['title'],'ZZ oldest');self.assertEqual(p['total_count'],301);self.assertEqual(p['pages'],13)
        p=browse_page(inv,source='Kids',preset='tv-cleanup',now=NOW)
        self.assertEqual(p['total_count'],1);self.assertEqual(p['filters']['source'],'Kids')
    def test_collision_and_structured_identity(self):
        inv=[item('Remake',year=2000,ident=1),item('Remake',year=2020,ident=2)]
        self.add(session('Remake - Pilot',NOW-timedelta(days=900)))
        enrich_inventory_usage(self.db,inv)
        self.assertTrue(all(i['history_status']=='ambiguous' for i in inv))
        self.assertEqual(browse_page(inv,preset='tv-cleanup',now=NOW)['total_count'],0)
        self.db.add(PlexLibraryItem(key='show2',rating_key='show2',title='Remake',year=2020,media_type='show'));self.db.commit()
        self.add(session(None,NOW,grandparent_title='Remake',grandparent_rating_key='show2'))
        enrich_inventory_usage(self.db,inv)
        self.assertEqual(inv[1]['plays'],1); self.assertEqual(inv[0]['plays'],0)
    def test_live_exclusion_and_explicit_sort(self):
        inv=[item('Z',999),item('A',10)]
        self.add(session('Z - Episode',NOW-timedelta(days=900)),session('A - Episode',NOW-timedelta(days=900)))
        self.db.add(ActivePlexSession(session_key='live',username='other',title='Z - Episode',media_type='episode',started_at=datetime.utcnow(),last_seen_at=datetime.utcnow()))
        self.db.commit(); enrich_inventory_usage(self.db,inv)
        p=browse_page(inv,preset='tv-cleanup',sort='title',direction='asc',now=NOW)
        self.assertEqual([i['title'] for i in p['items']],['A'])
    def test_calendar_and_url_roundtrip(self):
        from urllib.parse import parse_qs,urlsplit
        self.assertEqual(calendar_cutoff(12,datetime(2024,2,29)),datetime(2023,2,28))
        inv=[item(str(i),ident=i) for i in range(5)]
        for i in inv:i.update(history_status='unknown',last_watched_at=None)
        p=browse_page(inv,source='Adult',viewer='A B',history='unknown',sort='size',direction='desc',page_size=2)
        q={k:v[0] for k,v in parse_qs(urlsplit(p['next_url']).query,keep_blank_values=True).items()}
        for k in ['months','page','page_size']:q[k]=int(q[k])
        p2=browse_page(inv,**q)
        self.assertEqual(p2['page'],2); self.assertEqual(p2['total_count'],5);self.assertEqual(p2['filters']['viewer'],'A B')

if __name__=='__main__':unittest.main()

class ImportIdentityTests(unittest.TestCase):
    setUp = CleanupTests.setUp
    tearDown = CleanupTests.tearDown
    def test_import_preserves_series_keys_without_deleting_history(self):
        from app.services.tautulli_import import _upsert_session
        row=dict(row_id='legacy',user='viewer',started=1_700_000_000,stopped=1_700_000_005,media_type='episode',title='Episode',grandparent_title='Show',grandparent_rating_key='series-key')
        self.assertEqual(_upsert_session(self.db,row),(True,False));self.db.commit()
        self.assertEqual(self.db.query(PlexSession).one().grandparent_rating_key,'series-key')
        row['grandparent_rating_key']='new-key';self.assertEqual(_upsert_session(self.db,row),(False,True));self.db.commit()
        self.assertEqual(self.db.query(PlexSession).count(),1)
