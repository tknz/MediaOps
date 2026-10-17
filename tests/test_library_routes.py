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

if __name__=='__main__':unittest.main()
