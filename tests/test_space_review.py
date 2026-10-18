import unittest
from datetime import datetime
from unittest.mock import patch,AsyncMock
from sqlalchemy import create_engine,insert
from sqlalchemy.orm import Session
from app.db import Base
from app.models import PlexSession
from app.services import libraries as lib,library_audit as audit,space_review as space,compact_policy,replacements
from test_library_cleanup import item,session,NOW
from test_replacements import movie,release,GB

class HistoryBridgeTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://');Base.metadata.create_all(self.engine);self.db=Session(self.engine)
    def tearDown(self):self.db.close();self.engine.dispose()
    def test_renamed_year_suffix_and_other_viewer_recent_history(self):
        inv=[item('Foundation (2021)',year=2021),item('The Grand Tour (2016)',year=2016,ident=2)]
        rows=[session('Foundation (2021) - Old',datetime(2021,11,23)),session('Foundation - New',datetime(2025,9,27),'other'),session('The Grand Tour - New',datetime(2026,9,14),'other')]
        self.db.execute(insert(PlexSession),rows);self.db.commit();lib.enrich_inventory_usage(self.db,inv)
        self.assertEqual(inv[0]['last_watched_at'],datetime(2025,9,27));self.assertEqual(inv[0]['last_watched_by'],'other')
        self.assertEqual(lib.browse_page(inv,preset='tv-cleanup',now=NOW)['total_count'],0)
        lib.enrich_inventory_usage(self.db,inv,'owner');self.assertEqual(inv[0]['last_watched_at'],datetime(2021,11,23))
    def test_live_external_identity_bridges_alias_and_recency_without_guessing_paths(self):
        inv=[item("Tom Clancy's Jack Ryan",year=2018,ident=99)]
        audit.save(self.db,'plex-file-catalogue',dict(owners=[dict(kind='show',title='Jack Ryan',year=2018,tvdb_id=99,rating_keys=['123'],last_viewed_at=0)]))
        self.db.execute(insert(PlexSession),[session('Jack Ryan - Pilot',datetime(2026,6,10))]);self.db.commit()
        lib.enrich_inventory_usage(self.db,inv);self.assertEqual(inv[0]['last_watched_at'],datetime(2026,6,10))
        self.assertEqual(inv[0]['history_status'],'recorded')
    def test_remakes_stay_ambiguous_but_structured_keys_disambiguate(self):
        inv=[item('Same (2000)',year=2000,ident=1),item('Same (2020)',year=2020,ident=2)]
        self.db.execute(insert(PlexSession),[session('Same - Pilot',datetime(2026,1,1))]);self.db.commit()
        lib.enrich_inventory_usage(self.db,inv);self.assertTrue(all(r['history_status']=='ambiguous' for r in inv))
        self.db.query(PlexSession).delete();self.db.commit()
        audit.save(self.db,'plex-file-catalogue',dict(owners=[dict(kind='show',title='Same',year=2020,tvdb_id=2,rating_keys=['2020'],last_viewed_at=0)]))
        self.db.execute(insert(PlexSession),[session('Same - Pilot',datetime(2026,1,1),grandparent_rating_key='2020')]);self.db.commit()
        lib.enrich_inventory_usage(self.db,inv);self.assertEqual(inv[0]['history_status'],'unknown');self.assertEqual(inv[1]['history_status'],'recorded')
    def test_plex_recency_excludes_inactivity_without_attributing_it_to_selected_viewer(self):
        inv=[item('Show',ident=1)]
        audit.save(self.db,'plex-file-catalogue',dict(owners=[dict(kind='show',title='Show',tvdb_id=1,year=2020,rating_keys=['1'],last_viewed_at=1760000000)]))
        lib.enrich_inventory_usage(self.db,inv);self.assertEqual(inv[0]['last_watched_by'],'Plex (viewer unavailable)')
        self.assertEqual(lib.browse_page(inv,preset='tv-cleanup',now=NOW)['total_count'],0)
        lib.enrich_inventory_usage(self.db,inv,'owner');self.assertEqual(inv[0]['history_status'],'unknown')

    def test_conflicting_external_ids_cannot_be_overridden_by_equal_title_and_year(self):
        inv=[item('Same',year=2020,ident=2)]
        audit.save(self.db,'plex-file-catalogue',dict(owners=[dict(kind='show',title='Same',year=2020,tvdb_id=99,rating_keys=['bad'],last_viewed_at=1760000000)]))
        self.db.execute(insert(PlexSession),[session('Same - Pilot',datetime(2021,1,1))]);self.db.commit()
        lib.enrich_inventory_usage(self.db,inv);self.assertEqual(inv[0]['history_status'],'ambiguous');self.assertIsNone(inv[0]['last_watched_at'])

class SpaceTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://');Base.metadata.create_all(self.engine);self.db=Session(self.engine)
    def tearDown(self):self.db.close();self.engine.dispose()
    def test_full_plex_inventory_strict_threshold_unmanaged_and_urls(self):
        rows=[dict(path=f'/arbitrary/{i}.mkv',title=f'Movie {i}',year=2020,kind='movie',tmdb_id=i,sources=['Custom library'],bytes=(26+i)*GB) for i in range(301)]
        rows.append(dict(rows[0],path='/boundary.mkv',bytes=25*GB))
        audit.save(self.db,'plex-file-catalogue',dict(files=rows,complete=True))
        p=space.page(self.db,[],page=2,page_size=10,source='Custom library',managed='unmanaged')
        self.assertEqual(p['total_count'],301);self.assertEqual(p['items'][0]['title'],'Movie 290');self.assertEqual(p['unmanaged_count'],301)
        self.assertIn('managed=unmanaged',p['next_url']);self.assertIn('source=Custom+library',p['next_url']);self.assertIn('min_gb=25',p['next_url'])
        self.assertEqual(p['total_bytes'],sum((26+i)*GB for i in range(301)))
        with self.assertRaises(ValueError):space.page(self.db,[],min_gb=float('nan'))
    def test_external_identity_matches_renamed_arr_title_without_root_assumptions(self):
        audit.save(self.db,'plex-file-catalogue',dict(files=[dict(path='/unrelated/Plex/Movie.mkv',title='Plex title',kind='movie',tmdb_id=8,sources=['Cinema'],bytes=40*GB)]))
        p=space.page(self.db,[dict(kind='movie',tmdb_id=8,title='Arr alias',source='My server',id=9,monitored=False)])
        self.assertTrue(p['items'][0]['managed']);self.assertFalse(p['items'][0]['owners'][0]['monitored']);self.assertIn('Arr+alias',p['items'][0]['manage_url'])

class GenericPolicyTests(unittest.TestCase):
    def test_hdr_is_preference_and_native_rejections_still_block(self):
        m=movie();hdr=release(title='Hellboy.2019.2160p.HEVC.HDR10.AAC2.0',size=8*GB);sdr=release(size=10*GB)
        self.assertTrue(replacements.eligible(m,hdr));self.assertEqual(replacements.choose(m,[hdr,sdr])['bytes'],10*GB)
        self.assertFalse(replacements.eligible(m,dict(hdr,approved=False,rejections=['Custom format score is below minimum'])))
        p=compact_policy.preferences(dict(replacement_hdr='exclude'));self.assertFalse(replacements.eligible(m,hdr,preferences=p))
    def test_configurable_codec_language_and_native_quality_size_budget(self):
        m=movie();candidate=release(title='Hellboy.2019.2160p.H264.SDR.2.0',languages=[dict(id=2)],quality=dict(quality=dict(id=18,resolution=2160,name='WEB',modifier='none')))
        p=compact_policy.preferences(dict(replacement_language_id='0',replacement_4k_codec='any'))
        self.assertTrue(replacements.eligible(m,candidate,preferences=p))
        replacements.native_constraints(m,[dict(quality=dict(id=18),maxSize=20)],[dict(id=5,minFormatScore=-100)])
        self.assertFalse(replacements.eligible(m,candidate,preferences=p))
        self.assertTrue(replacements.eligible(m,dict(candidate,approved=False,rejections=['10 GB is larger than maximum allowed 3 GB (for 120min)'],customFormatScore=-50),above_target=True,preferences=p))
    def test_invalid_preferences_have_safe_finite_defaults(self):
        p=compact_policy.preferences(dict(replacement_max_gb='nan',replacement_scan_limit='99999',replacement_hdr='invalid'))
        self.assertEqual(p['replacement_max_gb'],25);self.assertEqual(p['replacement_scan_limit'],500);self.assertEqual(p['replacement_hdr'],'prefer_sdr')
