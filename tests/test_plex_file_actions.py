import base64
import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import unquote
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app import main, operations
from app.db import Base
from app.api_auth import AuthContext
from app.models import PlexSession
from app.services import plex_file_actions as action, library_audit as audit, space_review as space
from test_library_cleanup import session

def watched_movie():
    return dict(session('Oppenheimer',datetime(2026,1,2),'other',rating_key='10'),media_type='movie',watched_seconds=3600)


class PlexFileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
        Base.metadata.create_all(self.engine);self.db=Session(self.engine)
        self.path='/Plex/Movies/Oppenheimer (2023)/movie.mkv'
        self.file=dict(path=self.path,title='Oppenheimer',year=2023,kind='movie',tmdb_id=872585,owner_guid='plex://movie/oppenheimer',rating_key='10',sources=['Movies'],bytes=39_000_000_000,codec='hevc',resolution='4k',audio_codec='dts',audio_channels=6)
        audit.save(self.db,'plex-file-catalogue',dict(complete=True,files=[self.file],owners=[dict(kind='movie',title='Oppenheimer',year=2023,tmdb_id=872585,rating_keys=['10'],last_viewed_at=0)]))
        self.exists=True;self.folder=True;self.sidecars=False;self.retain_empty=False;self.multipart=False;self.playing=False;self.deletion=True;self.size=self.file['bytes'];self.browse_error=False
        self.plex=SimpleNamespace(cfg=SimpleNamespace(url='http://plex'),get=AsyncMock(side_effect=self.read),delete=AsyncMock(side_effect=self.delete))
        self.inventory=patch.object(action.libraries,'library_inventory',AsyncMock(return_value=([],[])));self.inventory.start()
    def tearDown(self):
        self.inventory.stop();self.db.close();self.engine.dispose()
    def read(self,path,**params):
        if path=='/:/prefs':return '<MediaContainer><Setting id="allowMediaDeletion" value="'+('1' if self.deletion else '0')+'" /></MediaContainer>'
        if path=='/status/sessions':return '<MediaContainer>'+('<Video ratingKey="10"><Media><Part file="'+self.path+'"/></Media></Video>' if self.playing else '')+'</MediaContainer>'
        if path=='/library/metadata/10':
            if not self.exists:return '<MediaContainer/>'
            parts=f'<Part id="30" file="{self.path}" size="{self.size}" exists="1" accessible="1"/>'
            if self.multipart:parts+='<Part file="/unreviewed-part2.mkv" size="100"/>'
            return '<MediaContainer><Video ratingKey="10" guid="plex://movie/oppenheimer" type="movie"><Media id="20">'+parts+'</Media></Video></MediaContainer>'
        if path.startswith('/services/browse/'):
            if self.browse_error:raise PermissionError('not mounted here')
            folder=base64.b64decode(unquote(path.rsplit('/',1)[-1])).decode()
            entries=''
            if folder=='/Plex/Movies' and self.folder:entries='<Path path="/Plex/Movies/Oppenheimer (2023)"/>'
            elif folder=='/Plex/Movies/Oppenheimer (2023)':
                if self.exists:entries='<File path="'+self.path+'"/>'
                if self.sidecars:entries+='<File path="/Plex/Movies/Oppenheimer (2023)/poster.jpg"/>'
            return '<MediaContainer>'+entries+'</MediaContainer>'
        raise AssertionError(path)
    def delete(self,path):
        self.assertEqual(path,'/library/metadata/10/media/20')
        self.exists=False;self.folder=self.sidecars or self.retain_empty
    async def test_unmanaged_last_copy_can_be_deleted_via_plex_and_folder_is_verified(self):
        self.db.execute(insert(PlexSession),[watched_movie()]);self.db.commit()
        review=await action.inspect(self.db,space.file_key(self.path),plex=self.plex)
        self.assertFalse(review['item']['managed']);self.assertEqual(review['item']['plays'],1)
        self.assertEqual(review['item']['last_watched_by'],'other')
        self.assertEqual(review['target']['other_versions'],0)
        with patch.object(action,'client',return_value=self.plex):result=await action.remove(self.db,action.token(review))
        self.assertTrue(result['ok']);self.assertEqual(result['file'],'removed');self.assertEqual(result['folder'],'removed')
        self.plex.delete.assert_awaited_once_with('/library/metadata/10/media/20')
        self.assertEqual(audit.state(self.db,'plex-file-catalogue')['files'],[])
        self.assertEqual(self.db.query(PlexSession).count(),1)
        self.assertEqual((await action.status(self.db,result['operation_id']))['status'],'completed')
    async def test_sidecars_are_not_deleted_and_remaining_movie_folder_is_reported(self):
        self.sidecars=True;review=await action.inspect(self.db,space.file_key(self.path),plex=self.plex)
        result=await action.remove(self.db,action.token(review),plex=self.plex)
        self.assertFalse(result['ok']);self.assertEqual(result['status'],'folder_cleanup_required')
        self.assertEqual(result['file'],'removed');self.assertEqual(result['folder'],'retained_with_contents')
        self.assertEqual(result['remaining_entries'],1);self.plex.delete.assert_awaited_once()
    async def test_empty_movie_folder_retained_by_plex_is_reported_as_incomplete(self):
        self.retain_empty=True
        review=await action.inspect(self.db,space.file_key(self.path),plex=self.plex)
        result=await action.remove(self.db,action.token(review),plex=self.plex)
        self.assertEqual(result['file'],'removed');self.assertEqual(result['folder'],'retained_empty')
        self.assertFalse(result['ok']);self.assertEqual(result['status'],'folder_cleanup_required')
        self.plex.delete.assert_awaited_once()
    async def test_unsupported_directory_check_does_not_claim_deleted_folder(self):
        self.browse_error=True;review=await action.inspect(self.db,space.file_key(self.path),plex=self.plex)
        result=await action.remove(self.db,action.token(review),plex=self.plex)
        self.assertEqual(result['status'],'needs_verification');self.assertEqual(result['folder'],'unverified')
        self.assertEqual(len(audit.state(self.db,'plex-file-catalogue')['files']),1)
    async def test_live_playback_multipart_permission_and_changed_size_block_writes(self):
        for attr in ('playing','multipart'):
            setattr(self,attr,True)
            with self.assertRaises(ValueError):await action.inspect(self.db,space.file_key(self.path),plex=self.plex)
            setattr(self,attr,False)
        self.deletion=False
        with self.assertRaisesRegex(ValueError,'disabled'):await action.inspect(self.db,space.file_key(self.path),plex=self.plex)
        self.deletion=True
        review=await action.inspect(self.db,space.file_key(self.path),plex=self.plex)
        self.size+=1
        with self.assertRaisesRegex(ValueError,'size changed'):await action.remove(self.db,action.token(review),plex=self.plex)
        self.plex.delete.assert_not_awaited()
    async def test_uncertain_write_is_never_retried_even_with_a_fresh_token(self):
        review=await action.inspect(self.db,space.file_key(self.path),plex=self.plex)
        self.plex.delete.side_effect=TimeoutError('uncertain')
        result=await action.remove(self.db,action.token(review),plex=self.plex)
        self.assertEqual(result['status'],'needs_verification')
        with self.assertRaisesRegex(ValueError,'already submitted'):await action.remove(self.db,action.token(review),plex=self.plex)
        self.plex.delete.assert_awaited_once()
    async def test_lost_response_and_later_folder_cleanup_are_reconciled_without_another_delete(self):
        self.retain_empty=True
        def lost_response(path):
            self.delete(path);raise TimeoutError('response lost')
        self.plex.delete.side_effect=lost_response
        review=await action.inspect(self.db,space.file_key(self.path),plex=self.plex)
        result=await action.remove(self.db,action.token(review),plex=self.plex)
        self.assertEqual(result['file'],'removed');self.assertEqual(result['status'],'folder_cleanup_required')
        self.folder=False
        current=await action.status(self.db,result['operation_id'],plex=self.plex)
        self.assertEqual(current['status'],'completed');self.assertEqual(current['folder'],'removed')
        self.plex.delete.assert_awaited_once()
    def test_all_files_include_unmanaged_playback_and_keep_remakes_distinct(self):
        second=dict(self.file,path='/Plex/Movies/Remake/movie.mkv',year=2000,tmdb_id=999,owner_guid='plex://movie/remake',rating_key='11')
        catalogue=audit.state(self.db,'plex-file-catalogue');catalogue['files'].append(second);catalogue['owners'].append(dict(kind='movie',title='Oppenheimer',year=2000,tmdb_id=999,rating_keys=['11'],last_viewed_at=0));audit.save(self.db,'plex-file-catalogue',catalogue)
        self.db.execute(insert(PlexSession),[watched_movie()]);self.db.commit()
        rows=space.page(self.db,[],q='Oppenheimer',min_gb=0)['items']
        by_year={r['year']:r for r in rows}
        self.assertEqual(by_year[2023]['plays'],1);self.assertEqual(by_year[2000]['plays'],0)
        self.assertEqual(by_year[2023]['last_watched_by'],'other')
        self.assertIn('/delete',by_year[2023]['delete_url'])


class FileRoutesTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
        Base.metadata.create_all(self.engine);self.db=Session(self.engine)
        main.app.dependency_overrides[main.get_db]=lambda:self.db
        main.app.dependency_overrides[operations.get_db]=lambda:self.db
        self.context=AuthContext('owner',True)
        main.app.dependency_overrides[main.require_auth]=lambda:self.context
        self.user=SimpleNamespace(is_admin=True,username='owner')
        self.client=TestClient(main.app,headers={'Origin':'http://testserver'})
    def tearDown(self):main.app.dependency_overrides.clear();self.db.close();self.engine.dispose()
    def test_api_mcp_confirmation_and_admin_protections(self):
        with patch.object(action,'remove',AsyncMock(return_value=dict(ok=True,operation_id='op'))) as remove,patch.object(main,'refresh_inventory_after_delete'):
            self.assertEqual(self.client.post('/api/libraries/files/delete',json=dict(confirmation='token')).status_code,422)
            remove.assert_not_awaited()
            response=self.client.post('/api/mcp',json=dict(id=1,method='tools/call',params=dict(name='mediaops.plex_file_delete',arguments=dict(confirmation='token',confirm='delete'))))
            self.assertEqual(response.status_code,200,response.text);remove.assert_awaited_once_with(self.db,'token','owner')
            self.context=AuthContext('reader',False,frozenset({'libraries.delete'}))
            self.assertEqual(self.client.post('/api/libraries/files/delete',json=dict(confirmation='token',confirm='delete')).status_code,403)
        with patch.object(main,'current_user',return_value=self.user),patch.object(action,'remove',AsyncMock()) as remove:
            self.assertEqual(self.client.post('/libraries/space/files/delete',data=dict(confirmation='token')).status_code,422);remove.assert_not_awaited()
    def test_result_page_does_not_describe_retained_folder_as_fully_done(self):
        result=dict(title='Oppenheimer',status='folder_cleanup_required',file='removed',bytes=39_000_000_000,folder='retained_empty',folder_path='/Plex/Movies/Oppenheimer')
        op='plex-file-delete-'+'a'*40;audit.save(self.db,op,result)
        with patch.object(main,'current_user',return_value=self.user):
            response=self.client.get('/libraries/space/files/operations/'+op)
            self.assertEqual(response.status_code,200,response.text)
            self.assertIn('Plex did not remove it',response.text);self.assertIn('Needs review',response.text)


if __name__=='__main__':unittest.main()
