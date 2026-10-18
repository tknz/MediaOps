"""Read-only discovery of compact replacements, followed by explicit batch approval."""
import asyncio
import hashlib
import json
import re
import unicodedata
from datetime import datetime
from itsdangerous import URLSafeTimedSerializer
from ..config import settings
from . import library_audit as audit, libraries, compact_policy as policy, duplicate_actions, replacement_tracking as tracking

scan_lock=asyncio.Lock()
action_lock=asyncio.Lock()
KEY='compact-replacements'

def stamp():return datetime.utcnow().isoformat()
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
def normal(value):return re.sub(r'[^a-z0-9]','',unicodedata.normalize('NFKD',str(value)).encode('ascii','ignore').decode().lower())
def operation_key(prefix,value):return prefix+digest(value)[:64-len(prefix)]
def release_key(release):return digest([release.get('indexerId'),release.get('guid')])
def file_stamp(movie):
    f=movie.get('movieFile') or {}
    return digest([movie.get('id'),movie.get('tmdbId'),movie.get('qualityProfileId'),movie.get('monitored'),f.get('id'),f.get('path'),f.get('size'),f.get('quality'),f.get('edition')])

def eligible(movie, release, *, above_target=False, preferences=None):
    """Only a size-only native rejection may be offered as an explicit fallback."""
    prefs=preferences if preferences is not None else policy.preferences()
    rejects=release.get('rejections') or []
    size_only=bool(rejects) and all(re.fullmatch(r'.+ is larger than maximum allowed .+ \(for .+\)',str(reason)) for reason in rejects)
    if not release.get('downloadAllowed'):return False
    if not release.get('approved') or rejects:
        if not (above_target and size_only and release.get('customFormatScore',0)>=movie.get('_min_format_score',0)):return False
    title=release.get('title') or ''; q=(release.get('quality') or {}).get('quality') or {}
    current=(movie['movieFile'].get('quality') or {}).get('quality') or {}
    if q.get('resolution')!=current.get('resolution') or q.get('resolution',0)<720:return False
    if q.get('modifier')!='none' or re.search(r'(?i)\b(?:remux|bdremux|bdmv|complete|3d)\b',title):return False
    if prefs['replacement_hdr']=='exclude' and re.search(policy.HDR,title):return False
    if prefs['replacement_4k_codec']=='hevc' and q['resolution']>=2160 and not re.search(policy.HEVC,title):return False
    if prefs['replacement_lossless']=='exclude' and re.search(policy.LOSSLESS,title):return False
    # Require an explicit English match and matching parsed title/year. TMDB alone is insufficient.
    if prefs['replacement_language_id'] and not any(x.get('id')==prefs['replacement_language_id'] for x in release.get('languages',[])):return False
    names={normal(movie['title']),normal(movie.get('originalTitle') or movie['title'])}
    names.update(normal(t.get('title')) for t in movie.get('alternateTitles',[]) if t.get('title'))
    if not names.intersection(normal(t) for t in release.get('movieTitles',[])):return False
    years=re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)',title)
    if str(movie.get('year')) not in years:return False
    if normal(release.get('edition') or '')!=normal(movie['movieFile'].get('edition') or ''):return False
    size=int(release.get('size') or 0); old=int(movie['movieFile'].get('size') or 0)
    ceiling=policy.replacement_ceiling(movie,release,prefs)
    return bool(size and size<old and (above_target or ((not ceiling or size<=ceiling) and size<=old*(1-prefs['replacement_min_percent']/100) and old-size>=prefs['replacement_min_gb']*1e9)))

def choose(movie, releases):
    prefs=policy.preferences()
    candidates=[r for r in releases if eligible(movie,r,preferences=prefs)]
    # Prefer stereo, then 5.1; use smallest acceptable file within that tier.
    def preference(r):return (bool(re.search(policy.HDR,r['title'])) if prefs['replacement_hdr']=='prefer_sdr' else False,
        {'Stereo':0,'5.1':1,'Not stated':2}.get(policy.audio_label(r['title']),3) if prefs['replacement_audio']=='stereo' else 0,r['size'],release_key(r))
    candidates.sort(key=preference)
    above_target=False
    if not candidates:
        candidates=sorted([r for r in releases if eligible(movie,r,above_target=True,preferences=prefs)],key=preference)
        above_target=True
    if not candidates:return None
    r=candidates[0]
    ceiling=policy.replacement_ceiling(movie,r,prefs)
    return dict(key=release_key(r),title=r['title'],bytes=r['size'],saving_bytes=movie['movieFile']['size']-r['size'],audio=policy.audio_label(r['title']),format=r['quality']['quality']['name'],codec='HEVC' if re.search(policy.HEVC,r['title']) else 'Not stated',range='HDR label' if re.search(policy.HDR,r['title']) else 'SDR label' if re.search(r'(?i)\bSDR\b',r['title']) else 'No HDR label',score=r.get('customFormatScore',0),fallback=above_target,above_target=bool(ceiling and r['size']>ceiling),target_bytes=ceiling)

def row(movie, inst):
    f=movie['movieFile']; q=(f.get('quality') or {}).get('quality') or {}; info=f.get('mediaInfo') or {}
    return dict(key=f"{inst['index']}:{movie['id']}",source=inst['name'],index=inst['index'],movie_id=movie['id'],title=movie['title'],year=movie.get('year'),monitored=movie.get('monitored',False),bytes=f.get('size',0),format=q.get('name','Unknown'),codec=info.get('videoCodec','Unknown'),hdr=info.get('videoDynamicRangeType') or info.get('videoDynamicRange') or 'Unknown',fingerprint=file_stamp(movie),candidate=None,status='Waiting')

async def inventory(instances):
    values=[]
    for inst in instances:
        definitions,profiles=await asyncio.gather(inst['client'].get('/api/v3/qualitydefinition'),inst['client'].get('/api/v3/qualityprofile'))
        for movie in await inst['client'].movies():
            if movie.get('movieFile',{}).get('size'):
                native_constraints(movie,definitions,profiles)
                values.append((inst,movie))
    return sorted(values,key=lambda item:(-item[1]['movieFile']['size'],item[0]['index'],item[1]['id']))[:policy.preferences()['replacement_scan_limit']]


def native_constraints(movie,definitions,profiles):
    movie['_replacement_definitions']={str(d['quality']['id']):d.get('maxSize') for d in definitions if d.get('quality')} if isinstance(definitions,list) else {}
    native=next((p for p in profiles if p['id']==movie['qualityProfileId']),{}) if isinstance(profiles,list) else {}
    movie['_min_format_score']=native.get('minFormatScore',0)

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
            report['status']='failed';report['errors']=['Could not load configured Radarr inventories. Run the check again.']
        report['checked_at']=stamp();audit.save(db,KEY,report)
        return report

def payload(db):
    report=audit.state(db,KEY) or dict(rows=[],status='not_run',done=0)
    report['preferences']=policy.preferences()
    report['scan_limit']=report['preferences']['replacement_scan_limit']
    for row in report['rows']:
        if row['status']=='No suitable release':row['note']='No approved release meets the replacement rules.'
    report['operations']=tracking.summary(db)
    for row in report['rows']:
        task=next((t for t in report['operations']['rows'] if tracking.blocked(t,row)),None) if row.get('candidate') else None
        if task:row.update(status=task['status'],operation=task)
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

async def inspect_one(db, selection, instances, *, check_release=True):
    report=audit.state(db,KEY) or {}
    r=next((r for r in report.get('rows',[]) if r['key']==selection['key']),None)
    if not r or not r.get('candidate'):raise ValueError('Result missing. Review again.')
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
    if not check_release:return c,None,r
    definitions,profiles=await asyncio.gather(c.get('/api/v3/qualitydefinition'),c.get('/api/v3/qualityprofile'))
    native_constraints(movie,definitions,profiles)
    releases=await c.releases(movie['id'])
    release=next((x for x in releases if release_key(x)==selection['candidate']),None)
    if not release or not eligible(movie,release,above_target=bool(r['candidate'].get('fallback') or r['candidate'].get('above_target'))) or release['size']!=r['candidate']['bytes']:raise ValueError(r['title']+': release is no longer approved. Run the check again.')
    if audit.state(db,operation_key('compact-release-',[r['key'],r['fingerprint'],selection['candidate']])):raise ValueError(r['title']+': already submitted; check Downloads.')
    return c,release,r

def progress(db, **values):
    previous=audit.state(db,KEY+'-approval') or {}
    audit.save(db,KEY+'-approval',dict(previous,**values))

async def approve(db, confirmation, actor):
    previous=audit.state(db,KEY+'-approval') or {}
    resuming=confirmation is None
    data=previous.get('plan') if resuming else serializer().loads(confirmation,max_age=600)
    if not data:raise ValueError('No saved approval plan.')
    rows=data.get('rows',[])
    if not rows or len(rows)>50 or len({r['key'] for r in rows})!=len(rows):raise ValueError('Invalid batch.')
    async with action_lock:
        if scan_lock.locked():raise ValueError('Search in progress. Wait for it to finish.')
        op=previous.get('job_id') if resuming else operation_key('compact-grab-',confirmation)
        if not op:raise ValueError('Missing operation.')
        if audit.state(db,op) and not resuming:raise ValueError('Already submitted. See replacement status.')
        if resuming and tracking.time_value(stamp())-tracking.time_value(previous.get('approved_at'))>86400:raise ValueError('Saved approval expired. Review the remaining replacements.')
        if not previous.get('plan'):
            raw=audit.state(db,KEY); chosen=[next(r for r in raw['rows'] if r['key']==s['key']) for s in rows]
            tracking.reserve(db,chosen,actor)
        progress(db,status='checking',active=True,total=len(rows),done=0,submitted=0,plan=data,job_id=op,actor=actor,approved_at=previous.get('approved_at') or stamp())
        instances=libraries.arr_clients('radarr'); pending=[];completed=0
        for number,selected in enumerate(rows):
            raw=next(r for r in (audit.state(db,KEY) or {})['rows'] if r['key']==selected['key'])
            task=audit.state(db,tracking.operation_id(raw))
            if task and task.get('attempted'):completed+=int(bool(task.get('confirmed')));continue
            await inspect_one(db,selected,instances,check_release=False)
            pending.append(selected)
            progress(db,status='checking',done=number+1,submitted=completed)
        for selected in pending:
            task=None
            try:
                c,release,r=await inspect_one(db,selected,instances)
                task=audit.state(db,tracking.operation_id(r))
                audit.save(db,op,dict(status='submitting',completed=completed,actor=actor,at=stamp(),keys=[s['key'] for s in rows]))
                audit.save(db,operation_key('compact-release-',[r['key'],r['fingerprint'],selected['candidate']]),dict(status='submitted',at=stamp(),actor=actor))
                tracking.save(db,task,status='Submitting',attempted=True)
                result=await c.post_json('/api/v3/release',dict(guid=release['guid'],indexerId=release['indexerId'],movieId=r['movie_id']))
                tracking.save(db,task,status='Awaiting Radarr',confirmed=True,download_id=result.get('downloadId') if isinstance(result,dict) else None)
                report=audit.state(db,KEY)
                next(x for x in report['rows'] if x['key']==r['key'])['status']='Submitted'
                audit.save(db,KEY,report);completed+=1
                progress(db,status='submitting',done=len(rows),submitted=completed)
                audit.save(db,op,dict(status='completed' if completed==len(rows) else 'partial',completed=completed,actor=actor,at=stamp()))
            except Exception as exc:
                if task and task.get('attempted'):tracking.save(db,task,status='Needs review',message='Submission response uncertain. Not resent automatically.')
                audit.save(db,op,dict(status='stopped',completed=completed,actor=actor,at=stamp()))
                raise ValueError(f'{completed} submitted. Batch stopped; see individual replacement status.') from exc
        progress(db,status='complete',active=False,total=len(rows),done=len(rows),submitted=completed)
        return completed
