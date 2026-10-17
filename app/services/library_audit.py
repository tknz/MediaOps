"""Read-only Arr audits. Reports are persisted; media/client configuration is never mutated."""
from __future__ import annotations
import asyncio
import hashlib
import json
import re
from datetime import datetime, timedelta
import httpx
from sqlalchemy.orm import Session
from ..models import LibraryAuditState
from .libraries import arr_clients, format_bytes


def state(db, key):
    row = db.get(LibraryAuditState, key)
    return json.loads(row.payload) if row else None


def save(db, key, payload):
    row = db.get(LibraryAuditState, key)
    if not row:
        row = LibraryAuditState(key=key, payload='{}'); db.add(row)
    row.payload = json.dumps(payload); row.updated_at = datetime.utcnow()
    db.commit()


def next_run(db, key, interval, now=None):
    now = now or datetime.utcnow()
    previous = state(db, key)
    if previous and previous.get('checked_at'):
        return max(now + timedelta(seconds=30), datetime.fromisoformat(previous['checked_at']) + interval)
    return now + timedelta(minutes=1 if key == 'health' else 5)


async def read(client, path, **params):
    # Retry a transient GET once. Never retry an authentication failure or mutate services.
    for attempt in range(2):
        try:
            return await client.get(path, **params)
        except httpx.HTTPStatusError as exc:
            if attempt or exc.response.status_code < 500: raise
        except httpx.TransportError:
            if attempt: raise
        await asyncio.sleep(0.5)


def error_label(exc):
    # Exception URLs can contain credentials. Persist only a status/type.
    if isinstance(exc, httpx.HTTPStatusError): return f'HTTP {exc.response.status_code}'
    return type(exc).__name__


async def full_queue(client):
    rows, page = [], 1
    while True:
        result = await read(client, '/api/v3/queue', page=page, pageSize=200)
        batch = result.get('records', [])
        rows.extend(batch)
        if len(rows) >= int(result.get('totalRecords') or 0): return rows
        if not batch: raise ValueError('Queue pagination ended before totalRecords')
        page += 1


def queue_findings(rows, previous, now):
    findings, observations = [], {}
    for row in rows:
        key = str(row.get('downloadId') or row['id'])
        progress = [row.get('sizeleft'), row.get('status'), row.get('trackedDownloadState')]
        old = previous.get(key, {})
        since = old.get('since', now.isoformat()) if old.get('progress') == progress else now.isoformat()
        observations[key] = dict(progress=progress, since=since)
        messages = row.get('statusMessages') or []
        blocked = row.get('trackedDownloadStatus') in ('warning', 'error') or bool(messages)
        stagnant = now - datetime.fromisoformat(since) >= timedelta(hours=2)
        if blocked or stagnant:
            findings.append(dict(key=key, title=row.get('title') or 'Unknown',
                category='import review' if blocked else 'possible stall',
                detail='Import blocked' if blocked else 'No progress for 2+ hours',
                action='Review Activity in the source app.'))
    return findings, observations


async def health_audit(db: Session, values=None, now=None):
    now = now or datetime.utcnow(); old = state(db, 'health') or {}
    services, observations, fingerprint_rows = [], {}, []
    for kind in ('sonarr', 'radarr'):
        for inst in arr_clients(kind, values):
            key = f"{kind}:{inst['index']}"; client = inst['client']
            try:
                health, queue = await asyncio.gather(read(client, '/api/v3/health'), full_queue(client))
                findings, observed = queue_findings(queue, old.get('observations', {}).get(key, {}), now)
                observations[key] = observed
                # Native health covers indexers, import lists, roots and download clients.
                notices = [dict(type=h.get('type'), source=h.get('source'), message=str(h.get('message') or 'Health warning')) for h in health]
                # Never retain arbitrary native text: it can include URLs/credentials.
                for h in notices:
                    h['message'] = health_message(h['source'], h['type'])
                services.append(dict(key=key, name=inst['name'], ok=True, health=notices, queue_count=len(queue), findings=findings))
                fingerprint_rows += [(key,h['source'],h['type']) for h in notices if str(h['type']).lower() != 'notice']
                fingerprint_rows += [(key, f['key'], f['category']) for f in findings]
            except Exception as exc:
                services.append(dict(key=key,name=inst['name'],ok=False,error=error_label(exc)))
                # Preserve progress evidence across temporary outages.
                observations[key] = old.get('observations', {}).get(key, {})
                fingerprint_rows.append((key,'unavailable',error_label(exc)))
    fingerprint = hashlib.sha256(json.dumps(sorted(fingerprint_rows)).encode()).hexdigest()
    report = dict(checked_at=now.isoformat(), services=services, observations=observations, fingerprint=fingerprint,
                  changed=old.get('fingerprint') != fingerprint, issue_count=len(fingerprint_rows), mode='Read-only; transient GET retries only')
    save(db,'health',report)
    return report


def health_message(source, severity):
    key = str(source or '').lower()
    checks = [('removedseries', 'Series removed from TVDB', 'Review affected shows in Sonarr → System → Status.'),
        ('update', 'Update check', 'Review the application update status.'),
        ('list', 'Import list failed', 'Test the list connection; renew its login if needed.'),
        ('indexer', 'Indexer unavailable', 'Test the indexer connection.'),
        ('downloadclient', 'Download client unavailable', 'Test the download client connection.'),
        ('rootfolder', 'Root folder unavailable', 'Check the media mount and folder permissions.'),
        ('diskspace', 'Low disk space', 'Check free space on the download and media volumes.')]
    return next((title for match,title,action in checks if match in key), 'Service warning')


def health_action(source):
    key = str(source or '').lower()
    if 'update' in key: return 'Review System → Updates.'
    if 'removedseries' in key: return 'Review affected shows in System → Status.'
    if 'list' in key: return 'Test the list connection and login.'
    if 'indexer' in key: return 'Test the indexer connection.'
    if 'root' in key: return 'Check the media mount and permissions.'
    return 'Open System → Status for affected items.'


def duration_seconds(value):
    # Arr exposes MediaInfo.RunTime as H:MM:SS or M:SS.
    if not isinstance(value,str) or not re.fullmatch(r'\d+:\d{2}(?::\d{2})?',value): return None
    parts=[int(v) for v in value.split(':')]
    if any(v>=60 for v in parts[1:]): return None
    return sum(v*60**i for i,v in enumerate(reversed(parts))) or None


def file_record(row, owner, source, kind):
    info=row.get('mediaInfo') or {}; size=int(row.get('size') or 0)
    seconds=duration_seconds(info.get('runTime'))
    resolution=info.get('resolution') or 'unknown'; dims=re.fullmatch(r'(\d+)x(\d+)',resolution)
    height=int(dims[2]) if dims else None
    return dict(file_id=row.get('id'), owner_id=owner['id'], title=owner['title'], year=owner.get('year'),
        identity=owner.get('tvdbId') if kind=='series' else owner.get('tmdbId'), kind=kind, source=source,
        path=row.get('path') or ((owner.get('path') or '').rstrip('/')+'/'+(row.get('relativePath') or '')),
        bytes=size,size_label=format_bytes(size), duration_seconds=seconds, bitrate_mbps=round(size*8/seconds/1e6,2) if seconds else None,
        codec=info.get('videoCodec') or 'unknown',audio_codec=info.get('audioCodec') or 'unknown',resolution=resolution,height=height,
        hdr=info.get('videoDynamicRangeType') or info.get('videoDynamicRange') or 'unknown',
        bit_depth=info.get('videoBitDepth'), audio_streams=info.get('audioStreamCount'),edition=row.get('edition') or '',
        quality=((row.get('quality') or {}).get('quality') or {}).get('name'))


def duplicate_groups(owners):
    by_id = {}
    for owner in owners:
        if owner.get('identity'):
            by_id.setdefault((owner['kind'],owner['identity']),[]).append(owner)
    return [dict(kind=key[0],identity=key[1],title=rows[0]['title'],copies=rows,
                 evidence='Same external identity managed more than once. Files/editions may differ; byte and hardlink identity unverified.',
                 reclaimable_bytes=None) for key,rows in by_id.items() if len(rows)>1]


def codec_label(value):
    key = re.sub(r'[^a-z0-9]', '', str(value).lower())
    return {'h264':'H.264','x264':'H.264','avc':'H.264','h265':'HEVC','x265':'HEVC','hevc':'HEVC','av1':'AV1','mpeg2':'MPEG-2','mpeg2video':'MPEG-2','xvid':'MPEG-4','divx':'MPEG-4','mpeg4':'MPEG-4','unknown':'Unknown'}.get(key, str(value))


def resolution_label(row):
    dims = re.fullmatch(r'(\d+)x(\d+)', str(row.get('resolution') or ''))
    if not dims: return row.get('resolution') or 'Unknown'
    width, height = map(int, dims.groups())
    return '4K' if width >= 3000 else '1080p' if width >= 1700 or height > 720 else '720p' if width >= 1200 or height > 576 else 'SD'


def audit_view(payload):
    """Compact view data, also exposed by the JSON API."""
    storage = payload.get('storage')
    if not storage: return
    e = storage['efficiency']; codecs = {}
    for b in e['breakdown']:
        c = codecs.setdefault(b['codec'], dict(codec=b['codec'], bytes=0, count=0))
        c['bytes'] += b['bytes']; c['count'] += b['count']
    e['codecs'] = sorted(codecs.values(), key=lambda c:c['bytes'], reverse=True)
    e['candidate_bytes'] = sum(f['bytes'] for f in e['candidates'])
    e['scenario_saving_bytes'] = sum(f.get('scenario_saving_bytes',0) for f in e['candidates']) if payload.get('target_mbps') else None
    for f in e['candidates']:
        f['codec_label'] = codec_label(f['codec']); f['resolution_label'] = resolution_label(f)
        f['minutes'] = round(f['duration_seconds']/60) if f.get('duration_seconds') else None
    for group in storage['duplicates']:
        copies = group['copies']
        group['reported_bytes'] = sum(c.get('size',c.get('bytes',0)) or 0 for c in copies) or None
        paths = [str(c.get('path') or '') for c in copies]
        group['review_label'] = 'Split movie?' if any(re.search(r'(?i)\b(?:cd|disc|part)[ ._-]*[12]\b', p) for p in paths) else 'Multiple files' if any('size' in c for c in copies) else 'Managed twice'
        from .duplicate_actions import annotate
        annotate(group)
        for c in copies:
            c['codec_label'] = codec_label(c['codec']) if c.get('codec') else ''
            c['version_label'] = {'4k':'4K','1080':'1080p','720':'720p','sd':'SD'}.get(str(c.get('resolution','')).lower(),c.get('resolution'))


def efficiency_report(files, target_mbps=None):
    # A user-selected target is a storage budget scenario, not a quality-equivalent encode prediction.
    if target_mbps is not None and not 0.5 <= target_mbps <= 100: raise ValueError('Target Mbps must be between 0.5 and 100')
    totals, candidates = {}, []
    for f in files:
        key=(codec_label(f['codec']), resolution_label(f), f['source'])
        bucket=totals.setdefault(key,dict(codec=key[0],resolution=key[1],source=key[2],count=0,bytes=0))
        bucket['count']+=1;bucket['bytes']+=f['bytes']
        bitrate=f.get('bitrate_mbps');height=f.get('height')
        budget=40 if height and height>1080 else 15 if height and height>720 else 8 if height else None
        reasons=[]
        if bitrate and budget and bitrate>budget: reasons.append('High bitrate')
        if f['bytes']>=20_000_000_000: reasons.append('20+ GB')
        if not f['duration_seconds'] or not height or f['codec']=='unknown': reasons.append('Missing metadata')
        if reasons:
            row=dict(f,reasons=reasons)
            if target_mbps is not None and f['duration_seconds']:
                projected=int(target_mbps*1e6/8*f['duration_seconds'])
                row.update(scenario_target_mbps=target_mbps,scenario_bytes=projected,scenario_saving_bytes=max(0,f['bytes']-projected))
            candidates.append(row)
    candidates.sort(key=lambda f:f['bytes'],reverse=True)
    return dict(breakdown=sorted(totals.values(),key=lambda r:r['bytes'],reverse=True),candidates=candidates,
        total_bytes=sum(f['bytes'] for f in files),file_count=len(files),
        limitations='Codec-neutral review heuristics, not proof of inefficiency. No quality-equivalent H.265 savings claim without sample encodes. Target Mbps scenarios include the whole file budget; HDR, grain, audio, editions and compatibility need review. Duplicate candidates are not subtracted or counted as savings.')


async def storage_audit(db: Session, values=None):
    files,owners,errors=[],[],[]; semaphore=asyncio.Semaphore(4)
    async def episode_files(client,show):
        async with semaphore:return await read(client,'/api/v3/episodefile',seriesId=show['id'])
    for service_kind in ('radarr','sonarr'):
        kind='movie' if service_kind=='radarr' else 'series'
        for inst in arr_clients(service_kind,values):
            try:
                rows=await read(inst['client'],'/api/v3/movie' if kind=='movie' else '/api/v3/series')
                for row in rows:
                    owners.append(dict(kind=kind,identity=row.get('tmdbId') if kind=='movie' else row.get('tvdbId'),title=row['title'],source=inst['name'],path=row.get('path'),owner_id=row['id']))
                if kind=='movie':
                    for row in rows:
                        if row.get('movieFile'):files.append(file_record(row['movieFile'],row,inst['name'],kind))
                else:
                    # Bound in-flight work and avoid launching one task per library item.
                    for start in range(0,len(rows),4):
                        batch=rows[start:start+4]
                        results=await asyncio.gather(*(episode_files(inst['client'],r) for r in batch),return_exceptions=True)
                        for owner,result in zip(batch,results):
                            if isinstance(result,Exception):errors.append(dict(source=inst['name'],title=owner['title'],error=error_label(result)))
                            else:files.extend(file_record(r,owner,inst['name'],kind) for r in result)
            except Exception as exc:errors.append(dict(source=inst['name'],error=error_label(exc)))
    plex_groups,plex_errors,plex_scan=await plex_duplicate_groups(values)
    errors.extend(plex_errors)
    report=dict(checked_at=datetime.utcnow().isoformat(),files=files,duplicates=duplicate_groups(owners)+plex_groups,plex_scan=plex_scan,efficiency=efficiency_report(files),errors=errors,
                complete=not errors,limitations='Arr metadata inventory only. No filesystem hash/inode verification, unmanaged-file scan, media deletion or transcoding. A Plex combined view is not a duplicate stored file.')
    save(db,'storage',report)
    return report


async def plex_duplicate_groups(values=None):
    """Find multiple distinct Plex file references, collapsing repeated combined-library views."""
    from xml.etree import ElementTree as ET
    from .clients import PlexClient, ServiceConfig
    from .settings_store import all_settings
    cfg=values or all_settings()
    if not cfg.get('plex_server_url') or not cfg.get('plex_server_token'):
        return [], [], 'not configured'
    client=PlexClient(ServiceConfig(url=cfg['plex_server_url'],token=cfg['plex_server_token']))
    grouped,errors={},[]
    try:
        libraries=await client.libraries()
    except Exception as exc:
        return [], [dict(source='Plex',error=error_label(exc))], 'partial'
    for library in libraries:
        if library.get('type') not in ('movie','show'):continue
        start=0
        try:
            while True:
                xml=await read(client,f"/library/sections/{library['key']}/all",type=1 if library['type']=='movie' else 4,includeGuids=1,
                    **{'X-Plex-Container-Start':start,'X-Plex-Container-Size':250})
                page=ET.fromstring(xml);nodes=list(page)
                for node in nodes:
                    identity=node.get('guid')
                    if not identity:continue
                    key=(node.get('type'),identity)
                    title=' - '.join(v for v in [node.get('grandparentTitle'),node.get('title')] if v)
                    group=grouped.setdefault(key,dict(kind=node.get('type'),identity=identity,title=title,paths={}))
                    for media in node.findall('Media'):
                        for part in media.findall('Part'):
                            path=part.get('file')
                            if not path:continue
                            # Same absolute file in All Movies and Kids Movies is one stored reference.
                            copy=group['paths'].setdefault(path,dict(path=path,sources=[],references=[],size=int(part.get('size') or 0),edition=node.get('editionTitle'),codec=media.get('videoCodec'),resolution=media.get('videoResolution')))
                            if library['title'] not in copy['sources']:copy['sources'].append(library['title'])
                            ref=dict(rating_key=node.get('ratingKey'),media_id=media.get('id'))
                            if ref not in copy['references']:copy['references'].append(ref)
                start+=len(nodes)
                total=int(page.get('totalSize') or 0)
                if (total and start>=total) or (not total and len(nodes)<250):break
                if not nodes:raise ValueError('Plex pagination ended before totalSize')
        except Exception as exc:errors.append(dict(source='Plex: '+library['title'],error=error_label(exc)))
    result=[]
    for group in grouped.values():
        if len(group['paths'])<2:continue
        copies=[]
        for copy in group.pop('paths').values():
            copies.append(dict(copy,source=' / '.join(copy['sources'])))
        result.append(dict(group,copies=copies,reclaimable_bytes=None,
            evidence='Same Plex media identity with multiple distinct file paths. Editions, quality versions, aliases and hardlinks require review; byte identity unverified.'))
    return result,errors,'partial' if errors else 'complete'
