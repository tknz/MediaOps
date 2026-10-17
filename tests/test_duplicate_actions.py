import unittest
from unittest.mock import AsyncMock,patch
from types import SimpleNamespace
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from app.db import Base
from app import main
from app.services import duplicate_actions as d,library_audit as a

class DuplicateActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://');Base.metadata.create_all(self.engine);self.db=Session(self.engine)
        self.group=dict(kind='movie',identity='plex://movie/test',title='Test',copies=[dict(path='/films/test-1080.mkv',source='All',size=100,resolution='1080',codec='h264',references=[dict(rating_key='10',media_id='20')]),dict(path='/films/test-4k.mkv',source='All',size=400,resolution='4k',codec='hevc',references=[dict(rating_key='10',media_id='30')])])
        a.save(self.db,'storage',dict(duplicates=[self.group]));self.group_key=d.key(('movie',self.group['identity']));self.copy_key=d.key('/films/test-1080.mkv')
        self.plex=AsyncMock();self.playing='<MediaContainer />';self.low_size=100;self.high=True;self.multipart=False
        self.plex.get.side_effect=self.read
    def read(self,path,**params):
        if path=='/status/sessions':return self.playing
        extra='<Part file="/films/part2.mkv" size="50" exists="1"/>' if self.multipart else ''
        high='<Media id="30" videoResolution="4k" videoCodec="hevc"><Part file="/films/test-4k.mkv" size="400" exists="1"/></Media>' if self.high else ''
        return f'<MediaContainer><Video ratingKey="10" guid="plex://movie/test" title="Test"><Media id="20" videoResolution="1080" videoCodec="h264"><Part file="/films/test-1080.mkv" size="{self.low_size}" exists="1"/>{extra}</Media>{high}</Video></MediaContainer>'
    def tearDown(self):self.db.close();self.engine.dispose()
    async def test_review_then_delete_only_selected_version_and_keep_history(self):
        with patch.object(d,'client',return_value=self.plex):
            review=await d.inspect(self.db,self.group_key,self.copy_key)
            self.assertEqual(review['keep'][0]['media_id'],'30');self.plex.delete.assert_not_awaited()
            confirmation=d.token(review)
            await d.remove(self.db,confirmation,actor='test-admin')
            self.plex.delete.assert_awaited_once_with('/library/metadata/10/media/20')
            self.assertEqual(a.state(self.db,'storage')['duplicates'],[])
            ledger=a.state(self.db,'duplicate-delete-'+d.key(confirmation));self.assertEqual(ledger['status'],'completed');self.assertEqual(ledger['actor'],'test-admin')
    async def test_playing_or_missing_keep_or_multipart_never_deletes(self):
        with patch.object(d,'client',return_value=self.plex):
            self.playing='<MediaContainer><Video ratingKey="10"><Media><Part file="/films/test-1080.mkv"/></Media></Video></MediaContainer>'
            with self.assertRaisesRegex(ValueError,'playing'):await d.inspect(self.db,self.group_key,self.copy_key)
            self.playing='<MediaContainer/>';self.high=False
            with self.assertRaises(ValueError):await d.inspect(self.db,self.group_key,self.copy_key)
            self.high=True;self.multipart=True
            with self.assertRaises(ValueError):await d.inspect(self.db,self.group_key,self.copy_key)
            self.plex.delete.assert_not_awaited()
    async def test_changed_files_or_tampered_confirmation_never_deletes(self):
        with patch.object(d,'client',return_value=self.plex):
            review=await d.inspect(self.db,self.group_key,self.copy_key);confirmation=d.token(review);self.low_size=101
            with self.assertRaisesRegex(ValueError,'changed'):await d.remove(self.db,confirmation)
            with self.assertRaises(Exception):await d.remove(self.db,confirmation+'tampered')
            self.plex.delete.assert_not_awaited()
    async def test_ambiguous_delete_is_not_retried(self):
        with patch.object(d,'client',return_value=self.plex):
            review=await d.inspect(self.db,self.group_key,self.copy_key);confirmation=d.token(review)
            self.plex.delete.side_effect=TimeoutError()
            with self.assertRaises(TimeoutError):await d.remove(self.db,confirmation)
            with self.assertRaisesRegex(ValueError,'already submitted'):await d.remove(self.db,confirmation)
            self.plex.delete.assert_awaited_once()
    def test_resolution_suggestion_not_codec_size_or_editions(self):
        d.annotate(self.group);self.assertTrue(self.group['copies'][0]['lower_resolution'])
        self.group['copies'][0].pop('lower_resolution');self.group['copies'][1]['edition']='Directors Cut'
        d.annotate(self.group);self.assertNotIn('lower_resolution',self.group['copies'][0])
    def test_delete_requires_admin_origin_and_explicit_confirmation(self):
        c=TestClient(main.app)
        with patch.object(main,'current_user',return_value=SimpleNamespace(is_admin=False)),patch.object(d,'remove',AsyncMock()) as remove:
            r=c.post('/libraries/audit/duplicates/remove',data=dict(confirmation='test',confirm='delete'),headers={'Origin':'http://testserver'})
            self.assertEqual(r.status_code,403);remove.assert_not_awaited()
        with patch.object(main,'current_user',return_value=SimpleNamespace(is_admin=True)),patch.object(d,'remove',AsyncMock()) as remove:
            r=c.post('/libraries/audit/duplicates/remove',data=dict(confirmation='test'),headers={'Origin':'http://testserver'})
            self.assertEqual(r.status_code,422);remove.assert_not_awaited()
            r=c.post('/libraries/audit/duplicates/remove',data=dict(confirmation='test',confirm='delete'),headers={'Origin':'https://untrusted.example'})
            self.assertEqual(r.status_code,403);remove.assert_not_awaited()
