"""Shared compact-media policy and pure Radarr configuration transforms."""
from copy import deepcopy
import re

HEVC = r'(?i)\b(?:x265|h[ ._-]?265|hevc)\b'
HDR = r'(?i)\b(?:hdr(?:10(?:plus)?)?|hdr10\+|hlg|pq|dovi|dv)\b|dolby[ ._-]?vision'
STEREO = r'(?i)\bstereo\b|(?<!\d)2[ .]0(?!\d)'
SURROUND = r'(?i)(?<!\d)5[ .]1(?!\d)'
LOSSLESS = r'(?i)true[ ._-]?hd|dts[ ._-]?hd|dts[ ._-]?x|(?<!\d)7[ .]1(?!\d)'
REJECT_SCORE = -10000


def spec(implementation, value, *, negate=False, name=None):
    fields = [dict(name=k,value=v) for k,v in value.items()] if isinstance(value,dict) else [dict(name='value',value=value)]
    return dict(name=name or implementation,implementation=implementation,negate=negate,required=False,fields=fields)


def formats():
    return [
        (dict(name='Compact - Reject HDR',specifications=[spec('ReleaseTitleSpecification',HDR)]),REJECT_SCORE),
        (dict(name='Compact - Reject remux and disc',specifications=[spec('QualityModifierSpecification',5,name='Remux'),spec('QualityModifierSpecification',4,name='Disc')]),REJECT_SCORE),
        (dict(name='Compact - Require HEVC for 4K',specifications=[spec('ResolutionSpecification',2160),spec('ReleaseTitleSpecification',HEVC,negate=True)]),REJECT_SCORE),
        (dict(name='Compact - Prefer stereo',specifications=[spec('ReleaseTitleSpecification',STEREO)]),20),
        (dict(name='Compact - Allow 5.1',specifications=[spec('ReleaseTitleSpecification',SURROUND)]),10),
        (dict(name='Compact - Avoid lossless and 7.1',specifications=[spec('ReleaseTitleSpecification',LOSSLESS)]),-20),
        (dict(name='Compact - Under 15 GiB',specifications=[spec('SizeSpecification',dict(min=0,max=15))]),50),
        (dict(name='Compact - Over 25 GiB',specifications=[spec('SizeSpecification',dict(min=25,max=100000))]),-50),
    ]


def leaves(items):
    return [leaf for item in items for leaf in (leaves(item['items']) if item.get('items') else [item])]


def profile(original, scores):
    result=deepcopy(original); flat=leaves(result['items']); old_cutoff=result['cutoff']
    by_resolution={}
    for item in flat:
        q=item['quality']; res=q.get('resolution',0)
        if q.get('modifier') in ('remux','brdisk','rawhd') or q.get('source') in ('workprint','cam','telesync','telecine') or q['name'] in ('REGIONAL','DVDSCR'):
            item['allowed']=False
        if res in (720,1080,2160):by_resolution.setdefault(res,[]).append(item)
    group_ids={720:1001,1080:1002,2160:1003}
    grouped=[]; placed=set()
    for item in result['items']:
        members=leaves([item]); rest=[]
        for leaf in members:
            res=leaf['quality'].get('resolution',0)
            if res not in group_ids:rest.append(leaf);continue
            if res in placed:continue
            placed.add(res); rows=by_resolution[res]
            grouped.append(dict(id=group_ids[res],name=f'{res}p Compact',items=rows,allowed=any(r['allowed'] for r in rows)))
        grouped.extend(rest)
    result['items']=grouped
    for res,rows in by_resolution.items():
        if old_cutoff in [row['quality']['id'] for row in rows] or old_cutoff==group_ids[res]:
            result['cutoff']=group_ids[res]
    result.update(upgradeAllowed=True,minFormatScore=0,cutoffFormatScore=170,minUpgradeFormatScore=1)
    for item in result.get('formatItems',[]):
        if item['format'] in scores:item['score']=scores[item['format']]
    return result


def size_definition(original):
    result=deepcopy(original);q=result['quality']
    if q.get('modifier')=='none' and q.get('resolution') in (1080,2160):
        minimum,preferred,maximum=(30,80,140) if q['resolution']==2160 else (10,30,55)
        result.update(minSize=minimum,preferredSize=preferred,maxSize=maximum)
    return result


def ceiling_bytes(runtime_minutes, resolution):
    if not runtime_minutes:return None
    return int(float(runtime_minutes)*(140 if resolution>=2160 else 55 if resolution>=1080 else 35)*1024*1024)


def audio_label(title):
    if re.search(STEREO,title):return 'Stereo'
    if re.search(SURROUND,title):return '5.1'
    if re.search(LOSSLESS,title):return 'Lossless / 7.1'
    return 'Not stated'
