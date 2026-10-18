"""Durable replacement operations, correlated to native grab and import history."""
import hashlib
import json
import re
from datetime import datetime, timezone
from sqlalchemy import select
from ..models import LibraryAuditState
from . import library_audit as audit, libraries, compact_policy as policy

PREFIX='compact-task-'

def now():return datetime.utcnow().isoformat()
def operation_id(row):return PREFIX+hashlib.sha256(json.dumps([row['key'],row['fingerprint'],row['candidate']['key']],sort_keys=True).encode()).hexdigest()[:64-len(PREFIX)]
def tasks(db):return [json.loads(s.payload) for s in db.scalars(select(LibraryAuditState).where(LibraryAuditState.key.startswith(PREFIX)).order_by(LibraryAuditState.updated_at.desc()))]
def save(db, task, **changes):
    task.update(changes,updated_at=now());audit.save(db,task['id'],task);return task

def blocked(task,row):
    return task.get('key')==row['key'] and task.get('status')!='Not submitted' and (task.get('fingerprint')==row['fingerprint'] or task.get('status') not in ('Imported','Failed'))

def for_row(db,row):return next((t for t in tasks(db) if blocked(t,row)),None)
def reserve(db,rows,actor):
    existing=tasks(db)
    if any(blocked(t,r) for r in rows for t in existing):raise ValueError('One or more replacements are already approved. See their status instead of submitting again.')
    result=[]
    for row in rows:
        task=dict(id=operation_id(row),key=row['key'],fingerprint=row['fingerprint'],source=row['source'],index=row['index'],movie_id=row.get('movie_id',int(row['key'].split(':')[1])),title=row['title'],year=row.get('year'),candidate=dict(row['candidate']),original_bytes=row['bytes'],status='Approved',actor=actor,approved_at=now(),attempted=False,download_id=None,progress=None)
        save(db,task);result.append(task)
    return result

def legacy_import(db):
    # Older versions recorded the durable intent but did not retain download/import tracking.
    report=audit.state(db,'compact-replacements') or {}
    for row in report.get('rows',[]):
        if not row.get('candidate') or audit.state(db,operation_id(row)):continue
        old='compact-release-'+hashlib.sha256(json.dumps([row['key'],row['fingerprint'],row['candidate']['key']],sort_keys=True).encode()).hexdigest()[:48]
        ledger=audit.state(db,old)
        if ledger:
            task=reserve(db,[row],ledger.get('actor','admin'))[0]
            save(db,task,status='Awaiting Radarr',attempted=True,confirmed=row.get('status')=='Submitted',approved_at=ledger.get('at') or now())

def time_value(value):
    try:return datetime.fromisoformat(str(value).replace('Z','+00:00')).replace(tzinfo=timezone.utc).timestamp()
    except (ValueError,TypeError):return 0

def retry_candidate(task, grab):
    """Display a native retry without discarding the originally approved release."""
    title=grab['sourceTitle'];size=int((grab.get('data') or {}).get('size') or 0)
    quality=(grab.get('quality') or {}).get('quality') or {}
    return dict(key=hashlib.sha256(json.dumps([grab['downloadId'],title]).encode()).hexdigest(),title=title,bytes=size,
        saving_bytes=task['original_bytes']-size,audio=policy.audio_label(title),format=quality.get('name','Radarr retry'),
        codec='HEVC' if re.search(policy.HEVC,title) else 'Not stated',range='HDR label' if re.search(policy.HDR,title) else 'No HDR label',
        score=int((grab.get('data') or {}).get('customFormatScore') or 0),fallback=False,above_target=False,target_bytes=None)


def follow_retries(result, history):
    # Native failed-download handling emits the next grab immediately. Do not adopt an
    # unrelated later search, another movie, or an unconfirmed download-client response.
    for _ in range(len(history)):
        did=result.get('download_id')
        if not did:break
        failed=next((h for h in history if h.get('downloadId')==did and h.get('eventType')=='downloadFailed'),None)
        if not failed:break
        successors=sorted([h for h in history if h.get('eventType')=='grabbed' and h.get('movieId')==result['movie_id'] and
            h.get('downloadId') and h['downloadId']!=did and h.get('sourceTitle') and int((h.get('data') or {}).get('size') or 0)>0 and
            0<=time_value(h.get('date'))-time_value(failed.get('date'))<=120],key=lambda h:time_value(h.get('date')))
        if not successors:break
        grab=successors[0]
        attempts=list(result.get('attempts',[]))
        if any(a['download_id']==grab['downloadId'] for a in attempts):break
        attempts.append(dict(download_id=did,status='Failed',finished_at=failed.get('date'),title=result['candidate']['title']))
        result.setdefault('requested_candidate',dict(result['candidate']))
        result.update(attempts=attempts,download_id=grab['downloadId'],candidate=retry_candidate(result,grab),retry_count=len(attempts),progress=None)
    return result


def reconcile(task,movie,queue,history):
    result=dict(task,checked_at=now(),tracking_error=None)
    relevant=[h for h in history if time_value(h.get('date'))>=time_value(task['approved_at'])-5 and h.get('movieId',task['movie_id'])==task['movie_id']]
    # Match the exact proposed release and grab epoch before trusting a download ID.
    grab=next((h for h in sorted(relevant,key=lambda x:x.get('date',''),reverse=True) if h.get('eventType')=='grabbed' and h.get('sourceTitle')==task['candidate']['title'] and h.get('downloadId')),None)
    if not result.get('download_id') and grab:result['download_id']=grab['downloadId']
    result=follow_retries(result,relevant)
    did=result.get('download_id')
    matching=[h for h in relevant if did and h.get('downloadId')==did]
    imports=[h for h in matching if h.get('eventType')=='downloadFolderImported']
    f=movie.get('movieFile') or {}
    for imported in sorted(imports,key=lambda x:x.get('date',''),reverse=True):
        data=imported.get('data') or {};file_id=data.get('fileId') or data.get('movieFileId')
        # Download completion is insufficient: require native import + the imported file still present.
        if f.get('id') and ((file_id and str(file_id)==str(f['id'])) or (not file_id and data.get('importedPath')==f.get('path') and int(data.get('size') or -1)==int(f.get('size') or 0))):
            result.update(status='Imported',progress=100,finished_at=imported.get('date'),file_id=f['id'],actual_bytes=f.get('size'),actual_saving_bytes=task['original_bytes']-int(f.get('size') or 0),message='Imported Radarr retry; original failed attempt retained.' if result.get('retry_count') else None)
            return result
    q=next((x for x in queue if did and x.get('downloadId')==did and x.get('movieId')==task['movie_id']),None)
    if q:
        state=str(q.get('trackedDownloadState') or '').lower();status=str(q.get('status') or '').lower();warnings=q.get('statusMessages') or []
        result['status']='Import blocked' if state in ('importblocked','importfailed') or (warnings and str(q.get('trackedDownloadStatus')).lower() in ('warning','error')) else 'Importing' if state in ('importpending','importing','downloadfinished') or status=='completed' else 'Downloading' if status=='downloading' else 'Paused' if status=='paused' else 'Queued'
        size=float(q.get('size') or 0);left=float(q.get('sizeleft') or 0)
        result['progress']=round(max(0,min(100,100*(size-left)/size)),1) if size else None
        result['message']='Review the import in Radarr.' if result['status']=='Import blocked' else 'Radarr retry' if result.get('retry_count') else None
        return result
    failed=next((h for h in matching if h.get('eventType')=='downloadFailed'),None)
    if failed:result.update(status='Failed',message='Radarr reported a failed download. Current file kept.')
    elif imports:result.update(status='Needs review',message='Import recorded, but the current file does not match it.')
    elif did:result.update(status='Awaiting import',message='Download left the queue; import has not been verified.')
    elif time_value(now())-time_value(task['approved_at'])>120:result.update(status='Needs review',message='Submission not confirmed by Radarr. Not resent automatically.')
    else:result.update(status='Awaiting Radarr',message=None)
    return result

async def refresh(db):
    legacy_import(db)
    pending=[t for t in tasks(db) if t.get('attempted') and t['status']!='Imported' and
        (t['status']!='Failed' or time_value(now())-time_value(t['approved_at'])<7*86400)]
    if not pending:return False
    imported_now=False
    for inst in libraries.arr_clients('radarr'):
        batch=[t for t in pending if t['index']==inst['index'] and t['source']==inst['name']]
        if not batch:continue
        c=inst['client']
        try:
            movies={m['id']:m for m in await c.movies()};queue=[];page=1
            while True:
                result=await c.get('/api/v3/queue',page=page,pageSize=100)
                records=result.get('records',[]);queue.extend(records)
                if len(queue)>=result.get('totalRecords',0):break
                if not records:raise ValueError('Incomplete queue')
                page+=1
        except Exception:
            for task in batch:save(db,audit.state(db,task['id']) or task,tracking_error='Radarr unavailable; retaining last known status.')
            continue
        for task in batch:
            try:
                movie=movies.get(task['movie_id'])
                if not movie:save(db,task,status='Needs review',message='Movie no longer in Radarr.');continue
                history=await c.get('/api/v3/history/movie',movieId=task['movie_id'])
                if not isinstance(history,list):raise ValueError('Incomplete history')
                current=audit.state(db,task['id']) or task
                updated=reconcile(current,movie,queue,history)
                imported_now=imported_now or updated['status']=='Imported'
                save(db,updated)
            except Exception:save(db,audit.state(db,task['id']) or task,tracking_error='History unavailable; retaining last known status.')
    return imported_now

def summary(db):
    values=tasks(db)
    return dict(rows=values,imported=sum(t['status']=='Imported' for t in values),active=sum(t['status'] not in ('Imported','Failed','Not submitted') for t in values),failed=sum(t['status'] in ('Failed','Import blocked','Needs review') for t in values),saving_bytes=sum(t.get('actual_saving_bytes',0) for t in values if t['status']=='Imported'))
