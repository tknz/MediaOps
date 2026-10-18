"""Review and delete one explicitly confirmed Plex media version, preserving the others."""
import asyncio
import hashlib
import re
from datetime import datetime
from xml.etree import ElementTree as ET
from itsdangerous import URLSafeTimedSerializer
from ..config import settings
from . import library_audit as audit
from .clients import PlexClient, ServiceConfig
from .settings_store import all_settings

lock = asyncio.Lock()

def key(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:24]

def is_split_group(group):
    """Use actual part references and filename suffixes, excluding words in the title."""
    seen={}
    words=re.findall(r'[^\W_]+',str(group.get('title') or ''),re.UNICODE)
    title_pattern=r'[\W_]*'.join(re.escape(word) for word in words)
    for copy in group.get('copies',[]):
        if int(copy.get('part_count') or 1)>1:return True
        for ref in copy.get('references',[]):
            identity=(ref.get('rating_key'),ref.get('media_id'))
            if not all(identity):continue
            if identity in seen and seen[identity]!=copy.get('path'):return True
            seen[identity]=copy.get('path')
        filename=str(copy.get('path') or '').rsplit('/',1)[-1].rsplit('.',1)[0]
        if title_pattern:filename=re.sub(title_pattern,'',filename,flags=re.IGNORECASE)
        if re.search(r'(?i)\b(?:cd|disc|disk|part)[ ._-]*0?[1-9][0-9]?\b',filename):return True
    return False


def annotate(group):
    group['key']=key((group['kind'],group['identity']))
    ranks=[]
    for copy in group['copies']:
        copy.update(key=key(copy.get('path')),lower_resolution=False,auto_delete=False,selection_reason=None)
        value=str(copy.get('resolution') or '').strip().lower()
        ranks.append({'4k':2160,'uhd':2160,'2160':2160,'2160p':2160,'1080':1080,'1080p':1080,'720':720,'720p':720,'480':480,'480p':480,'sd':480}.get(value))
    if len(ranks)<2 or not all(ranks) or len({str(c.get('edition') or '').strip().casefold() for c in group['copies']})!=1 or is_split_group(group):return
    highest=max(ranks)
    sizes=[int(c.get('size') or 0) for c,rank in zip(group['copies'],ranks) if rank==highest]
    smallest=min(sizes) if all(s>0 for s in sizes) else None
    for copy,rank in zip(group['copies'],ranks):
        if rank<highest:
            copy.update(lower_resolution=True,auto_delete=True,selection_reason='Lower resolution')
        elif smallest is not None and int(copy.get('size') or 0)>smallest:
            copy.update(auto_delete=True,selection_reason='Larger at same resolution')


def client():
    cfg=all_settings()
    if not cfg.get('plex_server_url') or not cfg.get('plex_server_token'): raise ValueError('Plex is not configured')
    return PlexClient(ServiceConfig(url=cfg['plex_server_url'],token=cfg['plex_server_token']))

async def refresh(db):
    report=audit.state(db,'storage')
    if not report: raise ValueError('Run a library scan first')
    groups, errors, status=await audit.plex_duplicate_groups()
    if errors: raise ValueError('Plex scan incomplete. Try Scan now after checking the connection.')
    # Keep Arr inventory groups; refresh only Plex file references.
    report['duplicates']=[g for g in report['duplicates'] if not str(g['identity']).startswith('plex://')]+groups
    audit.save(db,'storage',report)


def selected(db, group_key, copy_key):
    report=audit.state(db,'storage') or {}
    for group in report.get('duplicates',[]):
        if key((group['kind'],group['identity']))==group_key:
            chosen=next((c for c in group['copies'] if key(c.get('path'))==copy_key),None)
            if chosen:return group,chosen
    raise ValueError('This copy is no longer in the report. Refresh the scan.')

async def inspect(db, group_key, copy_key, refresh_missing=True, *, remove_paths=(), plex=None, metadata=None, playing=None):
    group,chosen=selected(db,group_key,copy_key)
    if not chosen.get('references'):
        if refresh_missing and str(group['identity']).startswith('plex://'):
            await refresh(db);return await inspect(db,group_key,copy_key,False,remove_paths=remove_paths,plex=plex,metadata=metadata,playing=playing)
        raise ValueError('Use the source library controls for this managed folder.')
    plex=plex or client(); current=metadata if metadata is not None else {}; copies=[]
    for copy in group['copies']:
        found=None
        for ref in copy.get('references',[]):
            rating=str(ref.get('rating_key') or '');media_id=str(ref.get('media_id') or '')
            if not rating.isdigit() or not media_id.isdigit(): continue
            if rating not in current:
                current[rating]=ET.fromstring(await plex.get(f'/library/metadata/{rating}',checkFiles=1))
            node=next((n for n in current[rating] if n.get('guid')==str(group['identity'])),None)
            if node is None:continue
            media=next((m for m in node.findall('Media') if m.get('id')==media_id),None)
            parts=media.findall('Part') if media is not None else []
            # Deleting a media version can delete all its parts. Offer only a single-file version.
            if len(parts)!=1 or parts[0].get('file')!=copy['path'] or parts[0].get('exists')=='0':continue
            found=dict(copy,rating_key=rating,media_id=media_id,size=int(parts[0].get('size') or 0),
                resolution=media.get('videoResolution'),codec=media.get('videoCodec'),edition=node.get('editionTitle') or '')
            break
        if found:copies.append(found)
    target=next((c for c in copies if c['path']==chosen['path']),None)
    keep=[c for c in copies if c['path']!=chosen['path'] and c['path'] not in remove_paths]
    if not target:raise ValueError('The selected copy is no longer available. Refresh the scan.')
    if not keep:raise ValueError('Keep at least one existing copy of '+group['title']+'.')
    if any(c['rating_key']==target['rating_key'] and c['media_id']==target['media_id'] for c in keep):
        raise ValueError('These files belong to one multipart version. Manage it in Plex.')
    # Fresh playback check, including viewers outside MediaOps history.
    if playing is None: playing=ET.fromstring(await plex.get('/status/sessions'))
    for node in playing:
        paths=[p.get('file') for p in node.findall('.//Part') if p.get('file')]
        if target['path'] in paths or (not paths and node.get('ratingKey')==target['rating_key']):
            raise ValueError('This copy is playing now. Wait until playback ends.')
    return dict(title=group['title'],group_key=group_key,copy_key=copy_key,target=target,keep=keep,deleting=sorted(remove_paths),split=is_split_group(dict(title=group['title'],copies=copies)))


def fingerprint(review):
    return key([(c['path'],c['rating_key'],c['media_id'],c['size'],c.get('edition')) for c in [review['target']]+review['keep']])

def token(review):
    return URLSafeTimedSerializer(settings.secret_key,salt='duplicate-version-delete').dumps(dict(group=review['group_key'],copy=review['copy_key'],fingerprint=fingerprint(review),deleting=review.get('deleting',[])))

async def execute(db, data, review, confirmation, actor, plex=None):
    target=review['target']
    operation='duplicate-delete-'+key(confirmation)
    if audit.state(db,operation):raise ValueError('This deletion was already submitted. Refresh the scan.')
    event=dict(status='submitted',actor=actor,at=datetime.utcnow().isoformat(),target=target,kept=review['keep'])
    audit.save(db,operation,event)
    # Exact version endpoint. Never use DELETE /library/metadata/{ratingKey}, which removes all versions.
    await (plex or client()).delete(f"/library/metadata/{target['rating_key']}/media/{target['media_id']}")
    audit.save(db,operation,dict(event,status='completed'))
    # Remove the deleted reference from the cached report; playback history remains untouched.
    report=audit.state(db,'storage')
    for group in report['duplicates']:
        if key((group['kind'],group['identity']))==data['group']:
            group['copies']=[c for c in group['copies'] if c['path']!=target['path']]
    report['duplicates']=[g for g in report['duplicates'] if len(g['copies'])>1]
    audit.save(db,'storage',report)
    return review

async def remove(db, confirmation, actor='admin'):
    data=URLSafeTimedSerializer(settings.secret_key,salt='duplicate-version-delete').loads(confirmation,max_age=600)
    async with lock:
        plex=client()
        review=await inspect(db,data['group'],data['copy'],False,remove_paths=data.get('deleting',[]),plex=plex)
        if fingerprint(review)!=data['fingerprint']:raise ValueError('The files changed. Review the copies again.')
        return await execute(db,data,review,confirmation,actor,plex=plex)


async def review_many(db, selections):
    selections=list(dict.fromkeys(selections))
    if not selections:raise ValueError('Select copies to delete first.')
    if len(selections)>200:raise ValueError('Select at most 200 copies per batch.')
    planned={}; missing=False
    for group_key,copy_key in selections:
        group,copy=selected(db,group_key,copy_key)
        if not str(group['identity']).startswith('plex://'):raise ValueError('Manage Arr folders in Libraries.')
        planned.setdefault(group_key,set()).add(copy['path'])
        missing=missing or not copy.get('references')
    if missing:await refresh(db)
    plex=client(); playing=ET.fromstring(await plex.get('/status/sessions')); metadata={}; reviews=[]
    for group_key,copy_key in selections:
        reviews.append(await inspect(db,group_key,copy_key,False,remove_paths=planned[group_key],plex=plex,metadata=metadata,playing=playing))
    return reviews


def batch_token(reviews):
    return URLSafeTimedSerializer(settings.secret_key,salt='duplicate-batch-delete').dumps(dict(operations=[token(review) for review in reviews]))

class BatchStopped(ValueError):
    def __init__(self, completed):
        super().__init__(f'{completed} copies deleted. Batch stopped; refresh the scan before retrying.')
        self.completed=completed

async def remove_many(db, confirmation, actor='admin'):
    payload=URLSafeTimedSerializer(settings.secret_key,salt='duplicate-batch-delete').loads(confirmation,max_age=600)
    operations=payload['operations']
    if not operations or len(operations)>200 or len(set(operations))!=len(operations):raise ValueError('Invalid batch.')
    serializer=URLSafeTimedSerializer(settings.secret_key,salt='duplicate-version-delete')
    decoded=[serializer.loads(value,max_age=600) for value in operations]
    async with lock:
        # Validate the whole batch before the first write. Recheck each file again just before deletion.
        plex=client(); playing=ET.fromstring(await plex.get('/status/sessions')); metadata={}
        for value,data in zip(operations,decoded):
            if audit.state(db,'duplicate-delete-'+key(value)):raise ValueError('This batch was already submitted. Refresh the scan.')
            review=await inspect(db,data['group'],data['copy'],False,remove_paths=data.get('deleting',[]),plex=plex,metadata=metadata,playing=playing)
            if fingerprint(review)!=data['fingerprint']:raise ValueError('The files changed. Review the batch again.')
        completed=0
        for value,data in zip(operations,decoded):
            try:
                review=await inspect(db,data['group'],data['copy'],False,remove_paths=data.get('deleting',[]),plex=plex)
                if fingerprint(review)!=data['fingerprint']:raise ValueError('Files changed')
                await execute(db,data,review,value,actor,plex=plex)
            except Exception as exc:
                raise BatchStopped(completed) from exc
            completed+=1
        return completed
