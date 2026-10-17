import unittest
from datetime import datetime,timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from app.db import Base
from app import main
from app.services import library_summary as inv, library_audit as audit, download_controls as dl

class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_inventory_totals_unique_titles_and_copies(self):
        engine=create_engine('sqlite://');Base.metadata.create_all(engine)
        instances={'radarr':[dict(name='Adult',client=AsyncMock()),dict(name='Kids',client=AsyncMock())], 'sonarr':[dict(name='TV',client=AsyncMock())]}
        movie=dict(id=1,tmdbId=10,hasFile=True,sizeOnDisk=100,monitored=True)
        instances['radarr'][0]['client'].get.return_value=[movie,dict(id=2,tmdbId=20,hasFile=False)]
        instances['radarr'][1]['client'].get.return_value=[movie]
        instances['sonarr'][0]['client'].get.return_value=[dict(id=3,tvdbId=30,statistics=dict(episodeFileCount=8,sizeOnDisk=400))]
        with Session(engine) as db,patch.object(inv,'arr_clients',side_effect=lambda kind,values:instances[kind]):
            report=await inv.summary(db)
            self.assertEqual((report['total_bytes'],report['movies'],report['movie_files'],report['shows'],report['episodes'],report['missing_movies']),(600,1,2,1,8,1))
            await inv.summary(db)
            instances['sonarr'][0]['client'].get.assert_awaited_once()
            report['checked_at']=(datetime.utcnow()-timedelta(hours=1)).isoformat();audit.save(db,'inventory-summary',report)
            instances['radarr'][0]['client'].get.side_effect=ValueError('unavailable')
            failed=await inv.summary(db,refresh=True);self.assertFalse(failed['complete']);self.assertEqual(failed['total_bytes'],600);self.assertTrue(failed['refresh_failed'])
        engine.dispose()
    async def test_download_controls_verify_exact_job_and_allowlist(self):
        job=dict(nzo_id='SABnzbd_nzo_abc',status='Downloading')
        with patch.object(dl,'queue',AsyncMock(return_value=({},[job]))),patch.object(dl,'sab_request',AsyncMock(return_value=dict(status=True,nzo_ids=[job['nzo_id']]))) as write:
            await dl.control(job['nzo_id'],'pause')
            write.assert_awaited_once_with(None,mutation=True,mode='queue',name='pause',value=job['nzo_id'])
            with self.assertRaises(ValueError):await dl.control(job['nzo_id'],'delete')
            with self.assertRaises(ValueError):await dl.control('all','pause')
            with self.assertRaises(ValueError):await dl.control('SABnzbd_nzo_other','pause')
            self.assertEqual(write.await_count,1)
    def test_download_telemetry_and_stale_controls(self):
        now=datetime.utcnow();slot=dict(nzo_id='SABnzbd_nzo_a',status='Downloading',filename='Film',mb='100',mbleft='40',percentage='60')
        previous=dl.snapshot({},[dict(slot,mbleft='70')],now=now-timedelta(seconds=30))
        current=dl.snapshot(dict(kbpersec='1024'),[slot],previous,now)
        item=current['items'][slot['nzo_id']];self.assertEqual(item['speed_bps'],1024*1024)
        rows=dl.attach([dict(download_id=slot['nzo_id'],source='Kids')],current)
        self.assertEqual(len(rows),1);self.assertTrue(rows[0]['controls'])
        current['checked_at']=(now-timedelta(minutes=3)).isoformat()
        self.assertFalse(dl.attach([dict(download_id=slot['nzo_id'])],current)[0]['controls'])
    def test_codec_buckets_and_full_totals_before_page(self):
        files=[audit.file_record(dict(id=i,size=30_000_000_000,mediaInfo=dict(runTime='2:00:00',resolution='1920x800',videoCodec=['x265','HEVC','h265'][i%3])),dict(id=i,title='Film',tmdbId=i),'Adult','movie') for i in range(60)]
        report=dict(files=files,duplicates=[],errors=[],complete=True,checked_at=datetime.utcnow().isoformat())
        with patch.object(audit,'state',side_effect=lambda db,key:report if key=='storage' else None):
            p=main.library_audit_payload(None,page=2,target_mbps=8)
            self.assertEqual(len(p['storage']['efficiency']['candidates']),10)
            self.assertEqual(p['storage']['efficiency']['candidate_bytes'],60*30_000_000_000)
            self.assertEqual(p['storage']['efficiency']['codecs'],[dict(codec='HEVC',bytes=60*30_000_000_000,count=60)])
    def test_download_control_auth_and_origin(self):
        client=TestClient(main.app)
        with patch.object(main,'current_user',return_value=SimpleNamespace(is_admin=False)),patch.object(dl,'control',AsyncMock()) as control:
            response=client.post('/live/downloads/control',data=dict(download_id='SABnzbd_nzo_a',action='pause'),headers={'Origin':'http://testserver'})
            self.assertEqual(response.status_code,403);control.assert_not_awaited()
        with patch.object(dl,'control',AsyncMock()) as control:
            response=client.post('/live/downloads/control',data=dict(download_id='SABnzbd_nzo_a',action='pause'),headers={'Origin':'https://untrusted.example'})
            self.assertEqual(response.status_code,403);control.assert_not_awaited()

    def test_download_control_success_refreshes_and_redirects(self):
        client=TestClient(main.app)
        with patch.object(main,'current_user',return_value=SimpleNamespace(is_admin=True)),patch.object(dl,'control',AsyncMock()) as control,patch.object(dl,'queue',AsyncMock(return_value=({},[]))),patch.object(audit,'state',return_value=None),patch.object(audit,'save') as saved:
            r=client.post('/live/downloads/control',data=dict(download_id='SABnzbd_nzo_a',action='pause'),headers={'Origin':'http://testserver'},follow_redirects=False)
            self.assertEqual(r.status_code,303);self.assertEqual(r.headers['location'],'/live?download_updated=1')
            control.assert_awaited_once_with('SABnzbd_nzo_a','pause','0');saved.assert_called_once()
