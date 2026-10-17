import unittest
from unittest.mock import AsyncMock,patch
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db import Base
from app.services import duplicate_actions as d,library_audit as audit

class DuplicateBatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://');Base.metadata.create_all(self.engine);self.db=Session(self.engine)
        self.groups=[dict(kind='movie',identity='plex://movie/'+str(i),title='Movie '+str(i),copies=[dict(path=f'/film{i}-{j}.mkv',source='Movies',size=100+j,codec='h264',resolution='1080',references=[dict(rating_key=str(10+i),media_id=str(100+i*10+j))]) for j in range(3)]) for i in range(2)]
        audit.save(self.db,'storage',dict(duplicates=self.groups));self.deleted=set();self.missing=set();self.playing='<MediaContainer/>';self.plex=AsyncMock();self.plex.get.side_effect=self.read
        async def delete(path):self.deleted.add(path.rsplit('/',1)[-1])
        self.plex.delete.side_effect=delete
    def tearDown(self):self.db.close();self.engine.dispose()
    def selection(self,g,c):return (d.key(('movie',self.groups[g]['identity'])),d.key(self.groups[g]['copies'][c]['path']))
    def read(self,path,**kw):
        if path=='/status/sessions':return self.playing
        i=int(path.rsplit('/',1)[-1])-10;group=self.groups[i]
        media=[]
        for c in group['copies']:
            ref=c['references'][0]
            if ref['media_id'] in self.deleted or c['path'] in self.missing:continue
            media.append(f'<Media id="{ref["media_id"]}" videoResolution="1080" videoCodec="h264"><Part file="{c["path"]}" size="{c["size"]}" exists="1"/></Media>')
        return '<MediaContainer><Video guid="'+group['identity']+'">'+''.join(media)+'</Video></MediaContainer>'
    async def test_batch_deletes_multiple_copies_and_preserves_selected_keeps(self):
        selections=[self.selection(0,0),self.selection(0,1),self.selection(1,0)]
        with patch.object(d,'client',return_value=self.plex):
            reviews=await d.review_many(self.db,selections)
            self.assertEqual(len(reviews[0]['keep']),1);self.plex.delete.assert_not_awaited()
            count=await d.remove_many(self.db,d.batch_token(reviews),actor='admin')
            self.assertEqual(count,3);self.assertEqual(self.deleted,{'100','101','110'})
            self.assertNotIn('102',self.deleted);self.assertNotIn('111',self.deleted)
            remaining=audit.state(self.db,'storage')['duplicates'];self.assertEqual(len(remaining),1);self.assertEqual(len(remaining[0]['copies']),2)
    async def test_no_writes_if_any_keep_disappears_or_any_selected_file_is_playing(self):
        selections=[self.selection(0,0),self.selection(1,0),self.selection(1,1)]
        with patch.object(d,'client',return_value=self.plex):
            reviews=await d.review_many(self.db,selections);confirmation=d.batch_token(reviews)
            self.missing.add('/film1-2.mkv')
            with self.assertRaises(ValueError):await d.remove_many(self.db,confirmation)
            self.plex.delete.assert_not_awaited();self.missing.clear()
            self.playing='<MediaContainer><Video><Media><Part file="/film1-0.mkv"/></Media></Video></MediaContainer>'
            with self.assertRaises(ValueError):await d.remove_many(self.db,confirmation)
            self.plex.delete.assert_not_awaited()
    async def test_all_copies_selected_is_rejected_and_partial_failure_stops_batch(self):
        with patch.object(d,'client',return_value=self.plex):
            with self.assertRaisesRegex(ValueError,'Keep at least one'):await d.review_many(self.db,[self.selection(0,c) for c in range(3)])
            reviews=await d.review_many(self.db,[self.selection(0,0),self.selection(1,0),self.selection(1,1)])
            self.plex.delete.side_effect=[None,TimeoutError(),None]
            confirmation=d.batch_token(reviews)
            with self.assertRaises(d.BatchStopped) as exc:await d.remove_many(self.db,confirmation)
            self.assertEqual(exc.exception.completed,1);self.assertEqual(self.plex.delete.await_count,2)
            with self.assertRaises(ValueError):await d.remove_many(self.db,confirmation)
            self.assertEqual(self.plex.delete.await_count,2)
    async def test_empty_tampered_or_expired_batch_never_deletes(self):
        with patch.object(d,'client',return_value=self.plex):
            with self.assertRaises(ValueError):await d.review_many(self.db,[])
            reviews=await d.review_many(self.db,[self.selection(0,0)]);confirmation=d.batch_token(reviews)
            with self.assertRaises(Exception):await d.remove_many(self.db,confirmation+'tampered')
            with patch('itsdangerous.timed.time.time',return_value=0):old=d.batch_token(reviews)
            with self.assertRaises(Exception):await d.remove_many(self.db,old)
            self.plex.delete.assert_not_awaited()
