import unittest
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock
from datetime import datetime
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session
from app.db import Base
from app.api_auth import AuthContext
from app import main
from app.services import libraries as lib
from app.services.library_audit import save,efficiency_report,file_record

class RouteTests(unittest.TestCase):
    def setUp(self):
        self.engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
        Base.metadata.create_all(self.engine);self.db=Session(self.engine)
        main.app.dependency_overrides[main.get_db]=lambda:self.db
        main.app.dependency_overrides[main.require_auth]=lambda:AuthContext('admin',True)
        self.user=SimpleNamespace(is_admin=True,username='admin')
        self.client=TestClient(main.app)
    def tearDown(self):
        main.app.dependency_overrides.clear();self.db.close();self.engine.dispose()

    def test_large_plex_files_html_api_pagination_and_authorization(self):
        files=[dict(path=f'/custom/{i}.mkv',title=f'Large {i}',year=2020,kind='movie',tmdb_id=i,sources=['Custom'],bytes=(26+i)*1_000_000_000,codec='hevc',resolution='4k',audio_codec='aac',audio_channels='2',bitrate_mbps=10,edition='',episode=None) for i in range(301)]
        save(self.db,'plex-file-catalogue',dict(files=files,complete=True,checked_at=datetime.utcnow().isoformat()))
        with patch.object(main,'current_user',return_value=self.user),patch.object(lib,'library_inventory',AsyncMock(return_value=([],[]))):
            api=self.client.get('/api/libraries/space?source=Custom&managed=unmanaged&page=2&page_size=10')
            self.assertEqual(api.status_code,200,api.text);self.assertEqual(api.json()['total_count'],301)
            html=self.client.get('/libraries/space?source=Custom&managed=unmanaged&page=2&page_size=10')
            self.assertEqual(html.status_code,200,html.text);self.assertIn('Large 290',html.text);self.assertIn('Outside Arr',html.text)
            self.assertIn('page=3',html.text);self.assertEqual(len(api.json()['items']),10)
            self.assertEqual(self.client.get('/api/libraries/space?min_gb=-1').status_code,422)
        main.app.dependency_overrides[main.require_auth]=lambda:AuthContext('reader',False)
        self.assertEqual(self.client.get('/api/libraries/space').status_code,403)
        with patch.object(main,'current_user',return_value=SimpleNamespace(is_admin=False)):
            self.assertEqual(self.client.get('/libraries/space').status_code,403)
    def test_replacement_scope_roundtrip_and_completion_history(self):
        from app.services import replacements as r,replacement_tracking as t
        from test_replacement_tracking import task
        saved=task();saved.update(status='Imported',actual_bytes=10_000_000_000,actual_saving_bytes=50_000_000_000)
        save(self.db,saved['id'],saved)
        with patch.object(main,'current_user',return_value=self.user),patch.object(r,'current_rows',AsyncMock(return_value=[])) as inventory,patch.object(main.job_engine,'enqueue') as scheduled:
            params='q=Irishman&source=Custom+Cinema&min_gb=25&managed=managed'
            html=self.client.get('/libraries/replacements?'+params)
            self.assertEqual(html.status_code,200);self.assertIn('source=Custom+Cinema',html.text)
            inventory.assert_awaited_with(self.db,q='Irishman',source='Custom Cinema',min_gb=25,managed='managed')
            api=self.client.get('/api/libraries/replacements?'+params).json()
            self.assertEqual(api['filters']['source'],'Custom Cinema')
            self.assertEqual(self.client.get('/api/libraries/replacements?min_gb=-1').status_code,422)
            response=self.client.post('/libraries/replacements/scan?'+params,headers={'Origin':'http://testserver'},follow_redirects=False)
            self.assertEqual(response.status_code,303);self.assertIn('source=Custom+Cinema',response.headers['location'])
            self.assertEqual(scheduled.call_args.kwargs['options']['q'],'Irishman')
            self.assertEqual(scheduled.call_args.args[1],'compact-replacements')
            pending=self.client.get('/libraries/replacements/jobs')
            self.assertNotIn('id="'+saved['id']+'"',pending.text)
            history=self.client.get('/libraries/replacements/jobs?view=complete')
            self.assertIn('id="'+saved['id']+'"',history.text)

    def test_replacements_html_api_batch_review_and_auth(self):
        from app.services import replacements as r
        candidate=dict(key='candidate',title='Movie.2020.2160p.HEVC.SDR.AAC2.0',bytes=10_000_000_000,saving_bytes=40_000_000_000,audio='Stereo',format='WEBDL-2160p',codec='HEVC',range='SDR label')
        row=dict(key='0:1',index=0,movie_id=1,fingerprint='file',source='Radarr',title='Movie',year=2020,monitored=False,bytes=50_000_000_000,format='Remux-2160p',codec='HEVC',hdr='HDR10',status='Ready',candidate=candidate)
        save(self.db,r.KEY,dict(rows=[row],status='complete',done=1,checked_at=datetime.utcnow().isoformat()))
        with patch.object(main,'current_user',return_value=self.user),patch.object(r,'current_rows',AsyncMock(return_value=[(dict(row,managed=True),None)])):
            html=self.client.get('/libraries/replacements');self.assertEqual(html.status_code,200,html.text)
            self.assertIn('Unmonitored',html.text);self.assertIn('Largest movies',html.text);self.assertIn('Stereo',html.text)
            api=self.client.get('/api/libraries/replacements').json();self.assertEqual(api['ready'],1);self.assertEqual(api['saving_bytes'],40_000_000_000)
            with patch.object(r,'approve',AsyncMock()) as approve:
                review=self.client.post('/libraries/replacements/review',data={'selection':'0:1'},headers={'Origin':'http://testserver'})
                self.assertEqual(review.status_code,200,review.text);self.assertIn('Download 1 replacements',review.text);approve.assert_not_awaited()
                response=self.client.post('/libraries/replacements/approve',data={'confirmation':r.token([row])},headers={'Origin':'http://testserver'})
                self.assertEqual(response.status_code,422);approve.assert_not_awaited()
                with patch.object(main.scheduler,'add_job') as schedule:
                    response=self.client.post('/libraries/replacements/approve',data={'confirmation':r.token([row]),'confirm':'replace'},headers={'Origin':'http://testserver'},follow_redirects=False)
                    self.assertEqual(response.status_code,303);schedule.assert_called_once();approve.assert_not_awaited()
                    jobs=self.client.get('/libraries/replacements/jobs');self.assertEqual(jobs.status_code,200,jobs.text);self.assertIn('Approved',jobs.text)
                    retry=self.client.post('/libraries/replacements/approve',data={'confirmation':r.token([row]),'confirm':'replace'},headers={'Origin':'http://testserver'},follow_redirects=False)
                    self.assertEqual(retry.status_code,303);schedule.assert_called_once();self.assertIn('error=',retry.headers['location'])
        main.app.dependency_overrides[main.require_auth]=lambda:AuthContext('reader',False)
        self.assertEqual(self.client.get('/api/libraries/replacements').status_code,403)
        with patch.object(main,'current_user',return_value=SimpleNamespace(is_admin=False)):
            self.assertEqual(self.client.post('/libraries/replacements/approve',data={'confirmation':'x','confirm':'replace'},headers={'Origin':'http://testserver'}).status_code,403)

    def test_api_and_fallback_consistent(self):
        inv=[lib.normalise_series(dict(id=i,title=f'Show{i}',statistics=dict(sizeOnDisk=i+1)), 'Kids',0) for i in range(80)]
        lib.enrich_inventory_usage(self.db,inv)
        with patch.object(main,'current_user',return_value=self.user),patch.object(lib,'library_inventory',AsyncMock(return_value=(inv,[]))),patch.object(lib,'plex_libraries',AsyncMock(return_value=[])):
            api=self.client.get('/api/libraries?preset=tv-cleanup&history=unknown&source=Kids&page=2&page_size=10')
            self.assertEqual(api.status_code,200,api.text);data=api.json();self.assertEqual(data['total_count'],80);self.assertEqual(len(data['items']),10)
            html=self.client.get('/libraries?preset=tv-cleanup&history=unknown&source=Kids&page=2&page_size=10')
            self.assertEqual(html.status_code,200,html.text);self.assertIn('80 results',html.text);self.assertIn('Show69',html.text)
            self.assertIn('ALL viewers',html.text);self.assertIn('Playback',html.text)
            self.assertEqual(self.client.get('/api/libraries?before=invalid').status_code,422)
    def test_audit_render_and_admin(self):
        f=file_record(dict(size=30_000_000_000,mediaInfo=dict(runTime='2:00:00',resolution='1920x1080',videoCodec='AV1')),dict(id=1,title='Large',tmdbId=1),'Kids','movie')
        save(self.db,'storage',dict(files=[f],checked_at=datetime.utcnow().isoformat(),duplicates=[],efficiency=efficiency_report([f]),errors=[],complete=True,limitations='Metadata only'))
        with patch.object(main,'current_user',return_value=self.user):
            r=self.client.get('/libraries/audit/report?view=storage');self.assertEqual(r.status_code,200,r.text);self.assertIn('AV1',r.text)
        main.app.dependency_overrides[main.require_auth]=lambda:AuthContext('reader',False)
        self.assertEqual(self.client.get('/api/libraries/audit').status_code,403)
        with patch.object(main,'current_user',return_value=SimpleNamespace(is_admin=False)):
            self.assertEqual(self.client.post('/libraries/audit/run',data={'audit':'storage'},headers={'Origin':'http://testserver'}).status_code,403)

    def test_overview_uses_same_plex_inventory_in_html_and_api(self):
        inventory=dict(schema_version=4,provider='Plex',complete=True,checked_at=datetime.utcnow().isoformat(),errors=[],total_bytes=28_000_000_000_000,movies=1382,shows=185,episodes=8300,tracks=23909,videos=231,photos=0,sources=[dict(source='Music',kind='artist',tracks=23909,bytes=800_000_000_000)])
        save(self.db,'inventory-summary',inventory)
        with patch.object(main,'current_user',return_value=self.user):
            html=self.client.get('/admin?period=30d')
            self.assertEqual(html.status_code,200,html.text)
            self.assertIn('23,909 music files',html.text);self.assertIn('231 other videos',html.text);self.assertIn('Plex · 1 libraries',html.text)
            self.assertNotIn('movies without files',html.text)
            api=self.client.get('/api/admin/overview?period=30d')
            self.assertEqual(api.status_code,200,api.text);self.assertEqual(api.json()['inventory'],inventory)
            self.assertEqual(api.json()['period']['key'],'30d')

    def test_duplicate_size_tie_selection_in_api_and_html(self):
        group=dict(kind='movie',identity='plex://movie/same-resolution',title='Same resolution',copies=[dict(source='All Movies',path='/small.mkv',size=100,codec='hevc',resolution='1080'),dict(source='All Movies',path='/large.mkv',size=200,codec='h264',resolution='1080')])
        save(self.db,'storage',dict(files=[],duplicates=[group],checked_at=datetime.utcnow().isoformat(),errors=[],complete=True))
        with patch.object(main,'current_user',return_value=self.user):
            data=self.client.get('/api/libraries/audit').json()
            copies=data['storage']['duplicates'][0]['copies']
            self.assertEqual([c['auto_delete'] for c in copies],[False,True])
            html=self.client.get('/libraries/audit/report?view=duplicates')
            self.assertEqual(html.status_code,200,html.text)
            self.assertEqual(html.text.count('data-auto-delete="true"'),1)
            self.assertIn('Larger at same resolution',html.text)
            self.assertIn('keep the smallest file',html.text)

    def test_bulk_duplicates_render_review_and_require_admin_confirmation(self):
        from app.services import duplicate_actions as d
        group=dict(kind='movie',identity='plex://movie/test',title='Movie',copies=[dict(source='Movies',path='/low.mkv',size=100,codec='h264',resolution='1080'),dict(source='Movies',path='/high.mkv',size=400,codec='hevc',resolution='4k')])
        save(self.db,'storage',dict(files=[],duplicates=[group],checked_at=datetime.utcnow().isoformat(),errors=[],complete=True))
        with patch.object(main,'current_user',return_value=self.user):
            html=self.client.get('/libraries/audit/report?view=duplicates')
            self.assertEqual(html.status_code,200,html.text);self.assertIn('duplicate-table',html.text);self.assertEqual(html.text.count('data-delete-copy'),2)
            self.assertIn('Select extras',html.text)
            selected=[(d.key(('movie',group['identity'])),d.key('/low.mkv'))]
            review=dict(title='Movie',group_key=selected[0][0],copy_key=selected[0][1],target=dict(path='/low.mkv',size=100,rating_key='1',media_id='10',resolution='1080',codec='h264'),keep=[dict(path='/high.mkv',size=400,rating_key='1',media_id='11',resolution='4k',codec='hevc')])
            with patch.object(d,'review_many',AsyncMock(return_value=[review])) as inspect,patch.object(d,'remove_many',AsyncMock()) as remove:
                r=self.client.post('/libraries/audit/duplicates/review',data={'copy:'+':'.join(selected[0]):'delete'},headers={'Origin':'http://testserver'})
                self.assertEqual(r.status_code,200,r.text);self.assertIn('Delete 1 copies?',r.text);inspect.assert_awaited_once_with(self.db,selected);remove.assert_not_awaited()
            with patch.object(d,'remove_many',AsyncMock()) as remove:
                r=self.client.post('/libraries/audit/duplicates/remove-batch',data={'confirmation':'x'},headers={'Origin':'http://testserver'})
                self.assertEqual(r.status_code,422);remove.assert_not_awaited()
        with patch.object(main,'current_user',return_value=SimpleNamespace(is_admin=False)),patch.object(d,'remove_many',AsyncMock()) as remove:
            r=self.client.post('/libraries/audit/duplicates/remove-batch',data={'confirmation':'x','confirm':'delete'},headers={'Origin':'http://testserver'})
            self.assertEqual(r.status_code,403);remove.assert_not_awaited()

    def test_streaming_filter_html_api_and_delete_review_authorization(self):
        from app.services import streaming_availability as streaming, library_actions as actions
        now=datetime.utcnow().isoformat()
        save(self.db,streaming.KEY+'-NZ',dict(items={'tv:10':dict(checked_at=now,providers=[dict(id=8,name='Netflix')]),'tv:11':dict(checked_at=now,providers=[])}))
        inv=[lib.normalise_series(dict(id=i,title='Series '+str(i),tmdbId=10+i,statistics=dict(sizeOnDisk=100+i)),'Kids',1) for i in range(2)]
        lib.enrich_inventory_usage(self.db,inv)
        with patch.object(main,'current_user',return_value=self.user),patch.object(lib,'library_inventory',AsyncMock(return_value=(inv,[]))),patch.object(lib,'plex_libraries',AsyncMock(return_value=[])):
            data=self.client.get('/api/libraries?provider=8&source=Kids&page_size=1').json()
            self.assertEqual(data['total_count'],1);self.assertEqual(data['total_bytes'],100);self.assertEqual(data['filters']['provider'],'8')
            html=self.client.get('/libraries?provider=8&source=Kids&page_size=1')
            self.assertEqual(html.status_code,200,html.text);self.assertIn('Netflix',html.text);self.assertIn('NZ',html.text)
        item=dict(inv[0],streaming=dict(region='NZ',status='known',providers=[dict(id=8,name='Netflix')],checked_at=now,link=None))
        review=dict(item=item,fingerprint='fingerprint',file_count=3,bytes=100)
        with patch.object(main,'current_user',return_value=self.user),patch.object(actions,'inspect',AsyncMock(return_value=review)),patch.object(streaming,'refresh',AsyncMock()),patch.object(actions,'execute',AsyncMock(return_value=dict(title='Series',ok=True))) as execute,patch.object(main.scheduler,'add_job'):
            html=self.client.get('/libraries/manage/delete-review?kind=series&source_index=1&item_id=0')
            self.assertEqual(html.status_code,200,html.text);self.assertIn('Delete files, monitor future episodes',html.text);self.assertIn('Remove series completely',html.text);self.assertIn('Netflix',html.text)
            api=self.client.get('/api/libraries/manage/delete-review?kind=series&source_index=1&item_id=0').json()
            token=api['confirmation']
            self.assertEqual(self.client.post('/api/libraries/manage/delete',json=dict(confirmation=token,mode='files_future'),headers={'Origin':'http://testserver'}).status_code,422)
            execute.assert_not_awaited()
            response=self.client.post('/api/libraries/manage/delete',json=dict(confirmation=token,mode='files_future',confirm='delete'),headers={'Origin':'http://testserver'})
            self.assertEqual(response.status_code,200,response.text);execute.assert_awaited_once_with(self.db,token,'files_future','admin')
        main.app.dependency_overrides[main.require_auth]=lambda:AuthContext('reader',False,scopes=frozenset({'libraries.delete'}))
        self.assertEqual(self.client.get('/api/libraries/manage/delete-review?kind=series&source_index=1&item_id=0').status_code,403)
        self.assertEqual(self.client.post('/api/libraries/manage/delete',json={},headers={'Origin':'http://testserver'}).status_code,403)
        with patch.object(main,'current_user',return_value=SimpleNamespace(is_admin=False)):
            self.assertEqual(self.client.get('/libraries/manage/delete-review?kind=series&source_index=1&item_id=0').status_code,403)

    def test_legacy_download_delete_opens_review_without_deletion(self):
        item=dict(kind='series',title='Example',source_index=1,id=7)
        with patch.object(main,'current_user',return_value=self.user),patch.object(lib,'library_inventory',AsyncMock(return_value=([item],[]))),patch.object(lib,'delete_library_item',AsyncMock()) as delete:
            result=self.client.post('/downloads/media/delete-files',data=dict(title='Example',request_type='tv'),headers={'Origin':'http://testserver'},follow_redirects=False)
            self.assertEqual(result.status_code,303);self.assertIn('source_index=1',result.headers['location']);self.assertIn('delete-review',result.headers['location']);delete.assert_not_awaited()
        with patch.object(main,'current_user',return_value=self.user),patch.object(lib,'library_inventory',AsyncMock(return_value=([item,dict(item,source_index=0)],[]))):
            result=self.client.post('/downloads/media/delete-files',data=dict(title='Example',request_type='tv'),headers={'Origin':'http://testserver'},follow_redirects=False)
            self.assertEqual(result.status_code,303);self.assertNotIn('delete-review',result.headers['location'])

if __name__=='__main__':unittest.main()
