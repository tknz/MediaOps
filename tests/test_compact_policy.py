import unittest
from app.services import compact_policy as p
class CompactPolicyTests(unittest.TestCase):
    def test_profiles_preserve_resolution_permissions_and_disable_remux(self):
        rows=[dict(quality=dict(id=i,name=name,resolution=res,modifier=mod,source=source),items=[],allowed=allowed) for i,name,res,mod,source,allowed in [(3,'WEBDL-1080p',1080,'none','webdl',True),(7,'Bluray-1080p',1080,'none','bluray',True),(30,'Remux-1080p',1080,'remux','bluray',True),(18,'WEBDL-2160p',2160,'none','webdl',False),(31,'Remux-2160p',2160,'remux','bluray',False)]]
        original=dict(items=rows,cutoff=30,formatItems=[dict(format=10,score=0)])
        changed=p.profile(original,{10:-10000})
        self.assertEqual(changed['cutoff'],1002);self.assertTrue(changed['upgradeAllowed']);self.assertEqual(changed['formatItems'][0]['score'],-10000)
        after={r['quality']['id']:r for r in p.leaves(changed['items'])}
        self.assertFalse(after[30]['allowed']);self.assertFalse(after[18]['allowed']);self.assertTrue(after[3]['allowed'])
        self.assertEqual([r['quality']['id'] for r in changed['items'][0]['items']],[3,7,30])
        self.assertTrue(original['items'][2]['allowed'])
    def test_runtime_size_rules_and_audio(self):
        row=dict(quality=dict(resolution=2160,modifier='none'),maxSize=None)
        changed=p.size_definition(row);self.assertEqual(changed['maxSize'],140);self.assertEqual(changed['preferredSize'],80)
        self.assertEqual(p.ceiling_bytes(120,2160),120*140*1024*1024)
        self.assertEqual(p.audio_label('Film.DDP5.1.x265'),'5.1');self.assertEqual(p.audio_label('Film.AAC2.0.x265'),'Stereo')
        self.assertEqual(p.audio_label('Film.DTS-HD.MA.7.1'),'Lossless / 7.1')
    def test_hdr_and_native_format_rules(self):
        import re
        for title in ['Film.HDR10+.x265','Film.DV.HEVC','Film.HLG.x265']:
            self.assertTrue(re.search(p.HDR,title))
        self.assertFalse(re.search(p.HDR,'Film.2160p.SDR.x265'))
        formats=p.formats();self.assertEqual(len(formats),8)
        hdr=next(score for value,score in formats if value['name']=='Compact - Prefer SDR');self.assertEqual(hdr,-25)
        self.assertEqual(next(score for value,score in formats if 'remux' in value['name']),p.REJECT_SCORE)
        reject=next(v for v,score in formats if 'remux' in v['name'])
        self.assertEqual([s['fields'][0]['value'] for s in reject['specifications']],[5,4])
