"""Read-only discovery of compact replacements, followed by explicit batch approval."""
import asyncio
import hashlib
import json
import re
import unicodedata
from datetime import datetime
from itsdangerous import URLSafeTimedSerializer
from ..config import settings
from . import library_audit as audit, libraries, compact_policy as policy, duplicate_actions

scan_lock=asyncio.Lock()
action_lock=asyncio.Lock()
KEY='compact-replacements'

def stamp():return datetime.utcnow().isoformat()
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
def normal(value):return re.sub(r'[^a-z0-9]','',unicodedata.normalize('NFKD',str(value)).encode('ascii','ignore').decode().lower())
def release_key(release):return digest([release.get('indexerId'),release.get('guid')])
def file_stamp(movie):
    f=movie.get('movieFile') or {}
    return digest([movie.get('id'),movie.get('tmdbId'),movie.get('qualityProfileId'),movie.get('monitored'),f.get('id'),f.get('path'),f.get('size'),f.get('quality'),f.get('edition')])

def eligible(movie, release):
    """Native approval is mandatory; never force an import or disguise a rejected release."""
    if not release.get('approved') or not release.get('downloadAllowed') or release.get('rejections'):return False
    title=release.get('title') or ''; q=(release.get('quality') or {}).get('quality') or {}
    current=(movie['movieFile'].get('quality') or {}).get('quality') or {}
    if q.get('resolution')!=current.get('resolution') or q.get('resolution',0)<720:return False
    if q.get('modifier')!='none' or re.search(policy.HDR,title) or re.search(r'(?i)\b(?:remux|bdremux|bdmv|complete|3d)\b',title):return False
    if q['resolution']>=2160 and not re.search(policy.HEVC,title):return False
    if re.search(policy.LOSSLESS,title):return False
    # Require an explicit English match and matching parsed title/year. TMDB alone is insufficient.
    if not any(x.get('id')==1 for x in release.get('languages',[])):return False
    names={normal(movie['title']),normal(movie.get('originalTitle') or movie['title'])}
    names.update(normal(t.get('title')) for t in movie.get('alternateTitles',[]) if t.get('title'))
    if not names.intersection(normal(t) for t in release.get('movieTitles',[])):return False
    years=re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)',title)
    if str(movie.get('year')) not in years:return False
    if normal(release.get('edition') or '')!=normal(movie['movieFile'].get('edition') or ''):return False
    size=int(release.get('size') or 0); old=int(movie['movieFile'].get('size') or 0)
    ceiling=policy.ceiling_bytes(movie.get('runtime'),q['resolution'])
    return bool(size and ceiling and size<=ceiling and size<=old*.8 and old-size>=2_000_000_000)

def choose(movie, releases):
    candidates=[r for r in releases if eligible(movie,r)]
    # Prefer stereo, then 5.1; use smallest acceptable file within that tier.
    candidates.sort(key=lambda r:({'Stereo':0,'5.1':1,'Not stated':2}.get(policy.audio_label(r['title']),3),r['size'],release_key(r)))
    if not candidates:return None
    r=candidates[0]
    return dict(key=release_key(r),title=r['title'],bytes=r['size'],saving_bytes=movie['movieFile']['size']-r['size'],audio=policy.audio_label(r['title']),format=r['quality']['quality']['name'],codec='HEVC' if re.search(policy.HEVC,r['title']) else 'Not stated',range='SDR label' if re.search(r'(?i)\bSDR\b',r['title']) else 'No HDR label',score=r.get('customFormatScore',0))

def row(movie, inst):
    f=movie['movieFile']; q=(f.get('quality') or {}).get('quality') or {}; info=f.get('mediaInfo') or {}
    return dict(key=f"{inst['index']}:{movie['id']}",source=inst['name'],index=inst['index'],movie_id=movie['id'],title=movie['title'],year=movie.get('year'),monitored=movie.get('monitored',False),bytes=f.get('size',0),format=q.get('name','Unknown'),codec=info.get('videoCodec','Unknown'),hdr=info.get('videoDynamicRangeType') or info.get('videoDynamicRange') or 'Unknown',fingerprint=file_stamp(movie),candidate=None,status='Waiting')

async def inventory(instances):
    values=[]
    for inst in instances:
        for movie in await inst['client'].movies():
            if movie.get('movieFile',{}).get('size'):values.append((inst,movie))
    return sorted(values,key=lambda item:(-item[1]['movieFile']['size'],item[0]['index'],item[1]['id']))[:50]

async def queued(client,movie_id):
    # Narrow native queue query returns full matching total, including unmonitored movies.
    q=await client.get('/api/v3/queue',movieIds=movie_id,pageSize=1)
    return bool(q.get('totalRecords'))

async def scan(db):
    if scan_lock.locked() or action_lock.locked():return
    async with scan_lock:
        report=dict(started_at=stamp(),checked_at=stamp(),status='running',done=0,rows=[],errors=[])
        audit.save(db,KEY,report)
        try:
            entries=await inventory(libraries.arr_clients('radarr'))
            report['rows']=[row(m,i) for i,m in entries];audit.save(db,KEY,report)
            for number,(inst,movie) in enumerate(entries):
                result=report['rows'][number]
                try:
                    if await queued(inst['client'],movie['id']):result['status']='Already downloading'
                    else:
                        releases=await inst['client'].releases(movie['id'])
                        result['candidate']=choose(movie,releases)
                        result['status']='Ready' if result['candidate'] else 'No suitable release'
                        if not result['candidate']:
                            result['note']='No approved release meets the replacement rules.'
                except Exception:result['status']='Search failed';result['note']='Indexer search unavailable. Run the check again.'
                report['done']=number+1;report['checked_at']=stamp();audit.save(db,KEY,report)
                await asyncio.sleep(.25)
            report['status']='complete'
        except Exception:
            report['status']='failed';report['errors']=['Could not load both Radarr inventories. Run the check again.']
        report['checked_at']=stamp();audit.save(db,KEY,report)
        return report

def payload(db):
    report=audit.state(db,KEY) or dict(rows=[],status='not_run',done=0)
    for row in report['rows']:
        if row['status']=='No suitable release':row['note']='No approved release meets the replacement rules.'
    report['approval']=audit.state(db,KEY+'-approval') or {}
    report['ready']=sum(r['status']=='Ready' for r in report['rows'])
    report['saving_bytes']=sum(r['candidate']['saving_bytes'] for r in report['rows'] if r['status']=='Ready')
    return report

def selections(db, keys):
    keys=list(dict.fromkeys(keys)); report=payload(db)
    if not keys or len(keys)>50:raise ValueError('Select between 1 and 50 replacements.')
    rows=[r for r in report['rows'] if r['key'] in keys]
    if len(rows)!=len(keys) or any(r['status']!='Ready' or not r.get('candidate') for r in rows):raise ValueError('Results changed. Review the table again.')
    return rows

def serializer():return URLSafeTimedSerializer(settings.secret_key,salt='compact-replacement-approval')
def token(rows):return serializer().dumps(dict(rows=[dict(key=r['key'],fingerprint=r['fingerprint'],candidate=r['candidate']['key']) for r in rows]))

async def inspect_one(db, selection, instances):
    r=selections(db,[selection['key']])[0]
    if r['fingerprint']!=selection['fingerprint'] or r['candidate']['key']!=selection['candidate']:raise ValueError('Results changed. Review again.')
    inst=next((i for i in instances if i['index']==r['index'] and i['name']==r['source']),None)
    if not inst:raise ValueError('Source changed.')
    c=inst['client'];movie=await c.get('/api/v3/movie/'+str(r['movie_id']))
    if file_stamp(movie)!=r['fingerprint']:raise ValueError(r['title']+': file, monitoring or profile changed. Run the check again.')
    if await queued(c,movie['id']):raise ValueError(r['title']+' is already downloading.')
    # Plex and Radarr have different mount prefixes; compare the movie-relative path.
    sessions=await duplicate_actions.client().sessions()
    relative=str(movie['movieFile'].get('relativePath') or '')
    if not relative:raise ValueError('Missing file path; review in Radarr.')
    if any(str(s.get('file') or '').endswith('/'+relative) or (not s.get('file') and normal(s.get('title'))==normal(movie['title'])) for s in sessions):raise ValueError(r['title']+' is playing now.')
    releases=await c.releases(movie['id'])
    release=next((x for x in releases if release_key(x)==selection['candidate']),None)
    if not release or not eligible(movie,release) or release['size']!=r['candidate']['bytes']:raise ValueError(r['title']+': release is no longer approved. Run the check again.')
    if audit.state(db,'compact-release-'+digest([r['key'],r['fingerprint'],selection['candidate']])):raise ValueError(r['title']+': already submitted; check Downloads.')
    return c,release,r

async def approve(db, confirmation, actor):
    data=serializer().loads(confirmation,max_age=600); rows=data.get('rows',[])
    if not rows or len(rows)>50 or len({r['key'] for r in rows})!=len(rows):raise ValueError('Invalid batch.')
    async with action_lock:
        if scan_lock.locked():raise ValueError('Search in progress. Wait for it to finish.')
        op='compact-grab-'+digest(confirmation)
        if audit.state(db,op):raise ValueError('Already submitted. Check Downloads before retrying.')
        instances=libraries.arr_clients('radarr')
        audit.save(db,KEY+'-approval',dict(status='checking',active=True,total=len(rows),done=0,submitted=0))
        for number,selected in enumerate(rows):
            await inspect_one(db,selected,instances)
            audit.save(db,KEY+'-approval',dict(status='checking',active=True,total=len(rows),done=number+1,submitted=0))
        completed=0
        for selected in rows:
            try:
                c,release,r=await inspect_one(db,selected,instances)
                # Persist before sending. An ambiguous network response is never retried automatically.
                audit.save(db,op,dict(status='submitting',completed=completed,actor=actor,at=stamp(),keys=[s['key'] for s in rows]))
                audit.save(db,'compact-release-'+digest([r['key'],r['fingerprint'],selected['candidate']]),dict(status='submitted',at=stamp(),actor=actor))
                await c.post_json('/api/v3/release',dict(guid=release['guid'],indexerId=release['indexerId'],movieId=r['movie_id']))
                audit.save(db,KEY+'-approval',dict(status='downloading',active=True,total=len(rows),done=len(rows),submitted=completed+1))
                report=audit.state(db,KEY)
                next(x for x in report['rows'] if x['key']==r['key'])['status']='Submitted'
                audit.save(db,KEY,report);completed+=1
                audit.save(db,op,dict(status='completed' if completed==len(rows) else 'partial',completed=completed,actor=actor,at=stamp()))
            except Exception as exc:
                audit.save(db,op,dict(status='stopped',completed=completed,actor=actor,at=stamp()))
                raise ValueError(f'{completed} submitted. Batch stopped; check Downloads before running another check.') from exc
        audit.save(db,KEY+'-approval',dict(status='complete',active=False,total=len(rows),done=len(rows),submitted=completed))
        return completed
