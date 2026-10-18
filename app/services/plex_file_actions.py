"""Reviewed exact-version deletion through Plex; never access media mounts."""
import asyncio
import base64
import hashlib
import json
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import quote
from xml.etree import ElementTree as ET
import httpx
from itsdangerous import URLSafeTimedSerializer
from ..config import settings
from . import library_audit as audit, libraries, space_review, streaming_availability
from .duplicate_actions import client

lock=asyncio.Lock()


def signer(): return URLSafeTimedSerializer(settings.secret_key,salt='plex-file-delete')
def digest(value): return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def selected(db,key):
    files=libraries.plex_catalogue(db).get('files',[])
    found=[f for f in files if space_review.file_key(f['path'])==key]
    if len(found)!=1:raise ValueError('This file is no longer in the inventory. Refresh Large files.')
    return found[0]


def folder_path(path):
    cls=PureWindowsPath if '\\' in path or (len(path)>1 and path[1]==':') else PurePosixPath
    value=cls(path)
    if not value.is_absolute() or '..' in value.parts:raise ValueError('Invalid Plex file path.')
    return value.parent


async def browse(plex,path):
    encoded=base64.b64encode(str(path).encode()).decode()
    root=ET.fromstring(await plex.get('/services/browse/'+quote(encoded,safe=''),includeFiles=1))
    if root.tag!='MediaContainer' or any(node.tag not in ('Path','File') or not node.get('path') for node in root):
        raise ValueError('Plex directory check unavailable')
    return [dict(type=node.tag,path=node.get('path'),name=node.get('name') or node.get('title')) for node in root]


async def file_state(plex,path,movie=False):
    """Check the directory via Plex's own browser; absence is never inferred from HTTP errors."""
    folder=folder_path(path)
    result=dict(file='unverified',folder='not_applicable' if not movie else 'unverified',folder_path=str(folder))
    try:
        # Parent listing establishes that the movie folder is absent, rather than
        # interpreting a 404/permission failure as successful directory removal.
        parent=await browse(plex,folder.parent)
        paths=[p for p in parent if p['type']=='Path']
        if not all(p.get('path') for p in paths):return result
        normal=lambda path:str(PureWindowsPath(path)).casefold() if isinstance(folder,PureWindowsPath) else str(PurePosixPath(path))
        if folder!=folder.parent and normal(folder) not in {normal(p['path']) for p in paths}:
            return dict(result,file='removed',folder='removed' if movie else 'not_applicable')
        entries=await browse(plex,folder)
        if not all(e.get('path') for e in entries):return result
        if normal(path) in {normal(e['path']) for e in entries}:result['file']='present'
        else:result['file']='removed'
        if movie:result['folder']='retained_with_contents' if entries else 'retained_empty'
        result['remaining_entries']=len(entries)
    except Exception:pass
    return result


async def inspect(db,key,*,plex=None,inventory=None):
    cached=selected(db,key);plex=plex or client()
    rating=str(cached.get('rating_key') or '')
    if not rating.isdigit():raise ValueError('Refresh the Plex inventory before deleting this file.')
    prefs=ET.fromstring(await plex.get('/:/prefs'))
    if not any(p.get('id')=='allowMediaDeletion' and p.get('value') in ('1','true') for p in prefs):
        raise ValueError('Plex media deletion is disabled or this token cannot delete. Enable Allow media deletion in Plex; MediaOps will not change that setting.')
    root=ET.fromstring(await plex.get('/library/metadata/'+rating,checkFiles=1,includeGuids=1))
    found=[]
    for node in root:
        if str(node.get('ratingKey'))!=rating:continue
        if cached.get('owner_guid') and cached['kind']!='show' and node.get('guid')!=cached['owner_guid']:
            raise ValueError('The Plex title identity changed. Refresh the inventory.')
        for media in node.findall('Media'):
            parts=media.findall('Part')
            for part in parts:
                if part.get('file')!=cached['path']:continue
                if len(parts)!=1:raise ValueError('This is a multipart version. Manage its parts in Plex to avoid deleting additional files.')
                if part.get('exists')=='0' or part.get('accessible')=='0':raise ValueError('Plex cannot access this file. Check storage permissions first.')
                if not str(media.get('id') or '').isdigit():raise ValueError('Missing Plex media version ID.')
                found.append(dict(path=part.get('file'),bytes=int(part.get('size') or 0),rating_key=rating,media_id=media.get('id'),part_id=part.get('id'),
                                  guid=node.get('guid'),type=node.get('type'),edition=node.get('editionTitle') or '',
                                  other_versions=sum(m.get('id')!=media.get('id') for m in node.findall('Media'))))
    if len(found)!=1:raise ValueError('The selected file is missing or ambiguous. Refresh the Plex inventory.')
    target=found[0]
    if target['bytes']!=cached['bytes']:raise ValueError('File size changed. Refresh Large files before deleting.')
    playing=ET.fromstring(await plex.get('/status/sessions'))
    for node in playing:
        paths=[p.get('file') for p in node.findall('.//Part') if p.get('file')]
        if target['path'] in paths or (not paths and node.get('ratingKey')==rating):
            raise ValueError('This file is playing now. Wait until playback ends.')
    if inventory is None:inventory,errors=await libraries.library_inventory(db,include_usage=False)
    else:errors=[]
    row=next(r for r in space_review.file_rows(db,inventory) if r['key']==key)
    streaming_item=dict(row,kind='series' if row['kind']=='show' else row['kind'])
    streaming_availability.attach(db,[streaming_item])
    row['streaming']=streaming_item['streaming']
    state=await file_state(plex,target['path'],cached['kind']=='movie')
    if state['file']=='removed':raise ValueError('Plex no longer finds this file on disk. Refresh the inventory.')
    snapshot=dict(target=target,server=digest(getattr(getattr(plex,'cfg',None),'url','Plex')))
    return dict(key=key,item=row,target=target,fingerprint=digest(snapshot),folder_check=state,arr_unavailable=bool(errors))


def token(review):return signer().dumps(dict(key=review['key'],fingerprint=review['fingerprint']))


def evict(db,path):
    catalogue=libraries.plex_catalogue(db)
    catalogue['files']=[f for f in catalogue.get('files',[]) if f['path']!=path]
    audit.save(db,'plex-file-catalogue',catalogue)


async def remove(db,confirmation,actor='admin',*,plex=None):
    data=signer().loads(confirmation,max_age=600)
    async with lock:
        operation='plex-file-delete-'+digest([data['key'],data['fingerprint']])[:40]
        if audit.state(db,operation):raise ValueError('This deletion was already submitted. Check its result before retrying.')
        plex=plex or client();review=await inspect(db,data['key'],plex=plex)
        if review['fingerprint']!=data['fingerprint']:raise ValueError('The file changed. Review it again.')
        target=review['target']
        event=dict(status='submitted',at=datetime.utcnow().isoformat(),actor=actor,target=target,title=review['item']['title'],folder_check=review['folder_check'])
        audit.save(db,operation,event)
        try:
            # Native Plex version deletion can also remove the last copy, but cannot
            # remove any sibling version or trigger an Arr/library-wide deletion.
            await plex.delete(f"/library/metadata/{target['rating_key']}/media/{target['media_id']}")
            state=await file_state(plex,target['path'],review['item']['kind']=='movie')
            status=('completed' if state['folder'] in ('removed','not_applicable') else 'folder_cleanup_required') if state['file']=='removed' else 'needs_verification'
            if state['file']=='removed':evict(db,target['path'])
            result=dict(ok=status=='completed',status=status,title=review['item']['title'],bytes=target['bytes'],file=state['file'],folder=state['folder'],folder_path=state['folder_path'],remaining_entries=state.get('remaining_entries'),operation_id=operation)
            audit.save(db,operation,dict(event,**result,finished_at=datetime.utcnow().isoformat()))
            return result
        except Exception as exc:
            # A lost response may follow a successful server-side delete. Only
            # recheck through GETs; never repeat the destructive request.
            state=await file_state(plex,target['path'],review['item']['kind']=='movie')
            if state['file']=='removed':
                evict(db,target['path'])
                status='completed' if state['folder'] in ('removed','not_applicable') else 'folder_cleanup_required'
                result=dict(ok=status=='completed',status=status,title=review['item']['title'],bytes=target['bytes'],file=state['file'],folder=state['folder'],folder_path=state['folder_path'],remaining_entries=state.get('remaining_entries'),operation_id=operation)
                audit.save(db,operation,dict(event,**result,finished_at=datetime.utcnow().isoformat()))
                return result
            refused=isinstance(exc,httpx.HTTPStatusError) and exc.response.status_code in (400,401,403,404,405)
            result=dict(ok=False,status='failed' if refused else 'needs_verification',title=review['item']['title'],bytes=target['bytes'],file='unverified',folder='unverified' if review['item']['kind']=='movie' else 'not_applicable',folder_path=str(folder_path(target['path'])),operation_id=operation,
                        error='Plex refused deletion. Check media deletion and storage permissions.' if refused else 'Plex deletion was not confirmed. Check the file before doing anything else.')
            audit.save(db,operation,dict(event,**result,finished_at=datetime.utcnow().isoformat()))
            return result


async def status(db,operation_id,*,plex=None):
    if not operation_id.startswith('plex-file-delete-') or len(operation_id)!=57:raise ValueError('Invalid deletion operation.')
    result=audit.state(db,operation_id)
    if not result:raise ValueError('Deletion operation not found.')
    if result.get('status') in ('needs_verification','folder_cleanup_required') and result.get('target'):
        target=result['target'];plex=plex or client()
        state=await file_state(plex,target['path'],result.get('folder')!='not_applicable')
        if state['file']=='removed':
            evict(db,target['path'])
            status='completed' if state['folder'] in ('removed','not_applicable') else 'folder_cleanup_required'
            result.update(ok=status=='completed',status=status,file=state['file'],folder=state['folder'],folder_path=state['folder_path'],remaining_entries=state.get('remaining_entries'),checked_at=datetime.utcnow().isoformat())
            result.pop('error',None)
            audit.save(db,operation_id,result)
    return result
