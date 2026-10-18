import unittest
from app.services import duplicate_actions as d


def group(*versions,title='Movie',**changes):
    return dict(kind='movie',identity='plex://movie/1',title=title,copies=[dict(path=f'/Movies/{title}/{title}-{i}.mkv',resolution=res,size=size) for i,(res,size) in enumerate(versions)],**changes)

class SelectionTests(unittest.TestCase):
    def test_resolution_first_then_delete_larger_same_resolution(self):
        g=group(('720',1),('1080',3),('4k',8),('2160p',6))
        d.annotate(g)
        self.assertEqual([c['auto_delete'] for c in g['copies']],[True,True,True,False])
        self.assertEqual([c['selection_reason'] for c in g['copies']],['Lower resolution','Lower resolution','Larger at same resolution',None])
    def test_equal_sizes_keep_both_smallest_and_unknown_sizes_stay_manual(self):
        for versions,expected in [([('1080',5),('1080p',3)],[True,False]),([('1080',3),('1080',3),('1080',5)],[False,False,True]),([('1080',0),('1080',5)],[False,False]),([('unknown',3),('1080',5)],[False,False])]:
            with self.subTest(versions=versions):
                g=group(*versions);d.annotate(g);self.assertEqual([c['auto_delete'] for c in g['copies']],expected)
    def test_title_parts_are_not_file_parts_but_actual_suffixes_are(self):
        for number in (1,2):
            title=f'Harry Potter and the Deathly Hallows: Part {number}'
            g=group(('4k',20),('1080',10),title=title)
            for i,c in enumerate(g['copies']):c['path']=f'/Movies/{title}/{title.replace(":", "")} (2010) {2160 if i==0 else 1080}p.mkv'
            self.assertFalse(d.is_split_group(g));d.annotate(g);self.assertTrue(g['copies'][1]['auto_delete'])
            g['copies'][0]['path']=g['copies'][0]['path'].replace('.mkv',' cd1.mkv');self.assertTrue(d.is_split_group(g));d.annotate(g);self.assertFalse(any(c['auto_delete'] for c in g['copies']))
    def test_real_split_media_and_different_editions_not_automatically_selected(self):
        for alteration in ('parts','references','suffix','editions'):
            g=group(('4k',20),('1080',10))
            if alteration=='parts':g['copies'][0]['part_count']=2
            if alteration=='references':
                for c in g['copies']:c['references']=[dict(rating_key='1',media_id='2')]
            if alteration=='suffix':
                for i,c in enumerate(g['copies']):c['path']=f'/Movies/Movie/Movie cd{i+1}.mkv'
            if alteration=='editions':g['copies'][1]['edition']='Extended'
            d.annotate(g);self.assertFalse(any(c['auto_delete'] for c in g['copies']))
    def test_annotation_recomputed_without_stale_flags(self):
        g=group(('1080',10),('1080',5));d.annotate(g);self.assertTrue(g['copies'][0]['auto_delete'])
        g['copies'][0]['size']=5;d.annotate(g);self.assertFalse(any(c['auto_delete'] for c in g['copies']))
