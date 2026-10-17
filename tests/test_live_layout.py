import unittest
from types import SimpleNamespace
from unittest.mock import patch,AsyncMock
from fastapi.testclient import TestClient
from app import main

class LiveLayoutTests(unittest.TestCase):
    def test_release_names_are_readable_without_changing_original(self):
        for raw,expected in [('Example.Film.2026.1080p.WEB-DL.H.265-GROUP','Example Film (2026)'),('Blade.Runner.2049.2017.2160p.WEB-DL','Blade Runner 2049 (2017)'),('A Real Title','A Real Title'),('Example.Show.S02E03.1080p.HDTV-GROUP','Example Show · S02E03')]:
            self.assertEqual(main.download_display_title(raw),expected)
    def test_server_rendered_queue_uses_table_and_retains_release_details(self):
        raw='Example.Film.2026.1080p.WEB-DL.H.265-GROUP'
        downloads=[dict(item_key='one',source='Radarr',title=raw,display_title=main.download_display_title(raw),status='downloading',quality='WEBDL-1080p',progress=25,size_bytes=8_000_000_000,size_gb=8,size_left_bytes=6_000_000_000,timeleft='00:10:00',indexer=None,message=None)]
        with patch.object(main,'current_user',return_value=SimpleNamespace(is_admin=True,username='preview')),patch.object(main,'enriched_active_payloads',AsyncMock(return_value=[])),patch.object(main,'active_download_payloads',return_value=downloads),patch.object(main,'download_queue_stats',return_value={}):
            response=TestClient(main.app).get('/live')
            self.assertEqual(response.status_code,200)
            self.assertIn('<table class="download-table">',response.text)
            self.assertIn('Example Film (2026)',response.text);self.assertIn(raw,response.text)
            self.assertIn('Playing now',response.text);self.assertIn('Speed / ETA',response.text)
            self.assertNotIn('class="ops-card"',response.text)
