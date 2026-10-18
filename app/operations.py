"""Validated operations shared by REST and MCP; no transport-specific mutations."""
from dataclasses import dataclass
from typing import Literal
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Form
from fastapi.responses import RedirectResponse
from urllib.parse import urlencode
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError
from sqlalchemy.orm import Session
from .api_auth import AuthContext, require_auth
from .db import get_db
from .services import job_engine, library_audit as audit, library_summary, libraries, duplicate_actions, replacements, replacement_tracking, library_actions, streaming_availability, download_controls, compact_policy
from .services.settings_store import all_settings, set_settings
from .services import plex_file_actions

router = APIRouter()


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class JobId(Input):
    job_id: str


class JobRun(JobId):
    options: dict = Field(default_factory=dict)


class JobSchedule(JobId):
    interval: int | None = Field(None, ge=1, le=8760)
    enabled: StrictBool | None = None


class Browse(Input):
    q: str = ''
    kind: Literal['all','movie','series'] = 'all'
    source: str = 'all'
    preset: Literal['','tv-cleanup'] = ''
    months: Literal[6,12,24] = 24
    before: str = ''
    history: Literal['','all','recorded','unknown','include','ambiguous'] = ''
    viewer: str = ''
    sort: Literal['','title','size','last_watched'] = ''
    direction: Literal['asc','desc'] = 'asc'
    page: int = Field(1, ge=1)
    page_size: int = Field(50, ge=1, le=200)
    provider: str = 'all'


class Audit(Input):
    source: str = 'all'
    page: int = Field(1, ge=1)
    target_mbps: float | None = Field(None, gt=0, le=1000)


class Space(Input):
    q: str = ''
    source: str = 'all'
    kind: Literal['all','movie','show','artist','video'] = 'all'
    managed: Literal['all','managed','unmanaged'] = 'all'
    min_gb: float | None = Field(None, ge=0, le=100000)
    sort: Literal['size','title','last_watched'] = 'size'
    direction: Literal['asc','desc'] = 'desc'
    page: int = Field(1, ge=1)
    page_size: int = Field(50, ge=1, le=200)


class ReplacementFilters(Input):
    q: str = ''
    source: str = 'all'
    min_gb: float = Field(0, ge=0, le=100000)
    managed: Literal['all','managed','unmanaged'] = 'all'


class Selection(Input):
    group_key: str
    copy_key: str


class DuplicatesReview(Input):
    selections: list[Selection] = Field(min_length=1, max_length=200)


class DeleteConfirmation(Input):
    confirmation: str = Field(min_length=1)
    confirm: Literal['delete']


class PlexFileReview(Input):
    file_key: str = Field(pattern=r'^[a-f0-9]{24}$')


class PlexFileOperation(Input):
    operation_id: str = Field(pattern=r'^plex-file-delete-[a-f0-9]{40}$')


class LibraryId(Input):
    kind: Literal['movie','series']
    source_index: int = Field(ge=0)
    item_id: int = Field(gt=0)


class Monitor(LibraryId):
    monitored: StrictBool


class LibraryDelete(DeleteConfirmation):
    mode: Literal['remove','files_future']


class ReplacementReview(Input):
    selections: list[str] = Field(min_length=1, max_length=50)


class ReplacementApproval(Input):
    confirmation: str = Field(min_length=1)
    confirm: Literal['replace']


class ReplacementJobs(Input):
    view: Literal['pending','complete','all'] = 'pending'


class DownloadControl(Input):
    download_id: str = Field(pattern=r'^SABnzbd_nzo_[A-Za-z0-9_-]+$')
    action: Literal['pause','resume','priority']
    priority: Literal['-1','0','1'] = '0'


class StreamingRefresh(Input):
    full: StrictBool = False


class PreferencesUpdate(Input):
    streaming_region: str | None = Field(None, pattern=r'^[A-Za-z]{2}$')
    space_review_min_gb: float | None = Field(None, ge=0, le=100000)
    replacement_language_id: int | None = Field(None, ge=0)
    replacement_4k_codec: Literal['hevc','any'] | None = None
    replacement_hdr: Literal['prefer_sdr','exclude','allow'] | None = None
    replacement_audio: Literal['stereo','smallest'] | None = None
    replacement_lossless: Literal['exclude','allow'] | None = None
    replacement_max_gb: float | None = Field(None, ge=0, le=100000)
    replacement_min_percent: float | None = Field(None, ge=0, le=95)
    replacement_min_gb: float | None = Field(None, ge=0, le=100000)
    replacement_scan_limit: int | None = Field(None, ge=1, le=500)


@dataclass(frozen=True)
class Operation:
    name: str
    description: str
    model: type[BaseModel]
    scope: str
    destructive: bool = False
    method: str = ''
    path: str = ''


OPERATIONS = (
    Operation('jobs_list', 'List every registered job, schedule, current state and last result.', Input, 'jobs.read', method='GET', path='/api/jobs'),
    Operation('job_detail', 'Job details and the last 30 runs, including failures and interruptions.', JobId, 'jobs.read', method='POST', path='/api/jobs/detail'),
    Operation('job_run', 'Queue an existing job. Library scans/searches are read-only; polling retains configured policy enforcement. Cannot approve replacements or delete media.', JobRun, 'jobs.write', method='POST', path='/api/jobs/run'),
    Operation('job_schedule', 'Change a recurring job interval or pause/resume its schedule.', JobSchedule, 'jobs.write', method='POST', path='/api/jobs/schedule'),
    Operation('library_browse', 'Browse all library matches with playback scope, streaming provider, size sort, full totals and pagination.', Browse, 'libraries.read'),
    Operation('library_inventory', 'Stored library bytes and movie/TV/episode/music counts from the cached Plex-first inventory.', Input, 'libraries.read', method='GET', path='/api/libraries/inventory'),
    Operation('library_audit', 'Read duplicate, codec/size efficiency and Arr health reports. Run library-storage or library-health to refresh.', Audit, 'libraries.read'),
    Operation('large_files', 'Browse Plex large files, including unmanaged media, with full totals and pagination.', Space, 'libraries.read'),
    Operation('duplicates_review', 'Recheck chosen duplicate copies and return a short-lived confirmation token; keep at least one copy.', DuplicatesReview, 'libraries.delete', True, 'POST', '/api/libraries/duplicates/review'),
    Operation('duplicates_delete', 'Delete ONLY the reviewed duplicate copies. Requires the review token and explicit confirm=delete; rechecks playback and files.', DeleteConfirmation, 'libraries.delete', True, 'POST', '/api/libraries/duplicates/delete'),
    Operation('library_monitor', 'Set monitoring on one exact Arr source and item.', Monitor, 'libraries.write', method='POST', path='/api/libraries/monitor'),
    Operation('library_delete_review', 'Review current files, size and NZ/configured streaming availability before choosing a deletion mode.', LibraryId, 'libraries.delete', True),
    Operation('library_delete', 'Execute a reviewed deletion: remove completely, or delete TV files and keep monitoring future episodes. Requires confirm=delete.', LibraryDelete, 'libraries.delete', True),
    Operation('replacements_list', 'Read proposed smaller releases for the largest movies, including unmonitored titles.', ReplacementFilters, 'libraries.read'),
    Operation('replacements_scan', 'Queue a replacement search using current preferences; never downloads automatically.', ReplacementFilters, 'jobs.write', method='POST', path='/api/libraries/replacements/scan'),
    Operation('replacements_review', 'Review selected proposed releases and return an approval token without submitting downloads.', ReplacementReview, 'replacements.write', True, 'POST', '/api/libraries/replacements/review'),
    Operation('replacements_approve', 'Approve reviewed replacement downloads using a short-lived token and explicit confirm=replace. Duplicate submissions are rejected.', ReplacementApproval, 'replacements.write', True, 'POST', '/api/libraries/replacements/approve'),
    Operation('replacement_jobs', 'Track each approved replacement through grab, download, import and completion; includes completed history.', ReplacementJobs, 'libraries.read', method='POST', path='/api/libraries/replacements/jobs'),
    Operation('downloads_list', 'Current downloads, speed, progress and available controls.', Input, 'downloads.read', method='GET', path='/api/downloads/live'),
    Operation('download_control', 'Pause, resume or reprioritise one currently active SAB job. No removal or retry.', DownloadControl, 'downloads.write', method='POST', path='/api/downloads/control'),
    Operation('streaming_refresh', 'Queue subscription availability refresh for the configured country. Full refresh rechecks all titles.', StreamingRefresh, 'jobs.write', method='POST', path='/api/libraries/streaming/refresh'),
    Operation('replacement_preferences', 'Read the current size, codec, HDR and audio preferences; native Arr profiles remain authoritative.', Input, 'libraries.read', method='GET', path='/api/libraries/replacements/preferences'),
    Operation('library_preferences_update', 'Update MediaOps storage/replacement preferences and streaming country. Does not change native Arr profiles or submit downloads.', PreferencesUpdate, 'libraries.write', True, 'POST', '/api/libraries/preferences'),
    Operation('plex_file_review', 'Review one exact Plex file, including outside Arr, with playback, versions and folder checks. Does not delete.', PlexFileReview, 'libraries.delete', True, 'POST', '/api/libraries/files/review'),
    Operation('plex_file_delete', 'Delete ONLY the reviewed Plex version through Plex, then verify its file and movie folder. Requires signed review and confirm=delete. No media mount or shell access.', DeleteConfirmation, 'libraries.delete', True, 'POST', '/api/libraries/files/delete'),
    Operation('plex_file_delete_status', 'Read the recorded file/folder deletion result, including any remaining folder or uncertain response.', PlexFileOperation, 'libraries.delete', True, 'POST', '/api/libraries/files/status'),
)
BY_NAME = {operation.name:operation for operation in OPERATIONS}


def permitted(operation, context):
    return context.has_scope(operation.scope) and (context.is_admin or not operation.destructive)


def tools(context=None):
    return [dict(name='mediaops.'+op.name, description=op.description, inputSchema=op.model.model_json_schema(),
                 annotations=dict(readOnlyHint=op.scope.endswith('.read') or op.name.endswith(('_review','_status')), destructiveHint=op.name in ('duplicates_delete','library_delete','replacements_approve','plex_file_delete'), idempotentHint=op.scope.endswith('.read') or op.name.endswith('_status')))
            for op in OPERATIONS if context is None or permitted(op, context)]


def queue_replacement_approval(db, confirmation, actor):
    """The UI, API and MCP share the exact same reservation and approval ledger."""
    from . import main
    reserved = []
    try:
        if replacements.scan_lock.locked() or replacements.action_lock.locked() or (audit.state(db,replacements.KEY+'-approval') or {}).get('active'):
            raise ValueError('A check or approval is already running.')
        data = replacements.serializer().loads(confirmation, max_age=600)
        rows = replacements.selections(db, [r['key'] for r in data['rows']])
        expected = [dict(key=r['key'],fingerprint=r['fingerprint'],candidate=r['candidate']['key']) for r in rows]
        if data['rows'] != expected: raise ValueError('Results changed. Review again.')
        reserved = replacement_tracking.reserve(db, rows, actor)
        audit.save(db,replacements.KEY+'-approval',dict(status='queued',active=True,total=len(rows),done=0,submitted=0,plan=data,actor=actor,approved_at=replacements.stamp(),job_id=replacements.operation_key('compact-grab-',confirmation)))
        main.scheduler.add_job(main.scheduled_replacement_approval, 'date', run_date=main.datetime.now(main.timezone.utc), id='compact-replacement-approval', replace_existing=False, args=[confirmation,actor])
    except Exception:
        db.rollback()
        if reserved:
            for task in reserved: replacement_tracking.save(db, task, status='Not submitted', message='Approval could not be queued. Review before retrying.')
            replacements.progress(db,status='stopped',active=False,message='Approval could not be queued. Review before retrying.')
        raise
    return dict(status='queued', total=len(rows), url='/libraries/replacements/jobs')


async def execute(name, arguments, db, context):
    op = BY_NAME.get(name)
    if op is None: raise HTTPException(404, 'Unknown operation')
    if not permitted(op, context): raise HTTPException(403, 'Admin and scope required' if op.destructive else 'Scope required: '+op.scope)
    try: args = op.model.model_validate(arguments).model_dump()
    except ValidationError: raise HTTPException(422, 'Invalid arguments for '+name)
    if name == 'job_run':
        try:
            model = {'compact-replacements':ReplacementFilters, 'streaming-availability':StreamingRefresh}.get(args['job_id'], Input)
            args['options'] = model.model_validate(args['options']).model_dump()
        except ValidationError: raise HTTPException(422, 'Invalid job options')
    try:
        return await dispatch(name, args, db, context)
    except HTTPException: raise
    except ValueError as exc: raise HTTPException(409, str(exc))
    except Exception: raise HTTPException(409, 'Operation could not be confirmed. Refresh the report before retrying.')


async def dispatch(name, args, db, context):
    from . import main
    if name == 'jobs_list': return dict(jobs=job_engine.listing(db))
    if name == 'job_detail': return job_engine.detail(db, **args)
    if name == 'job_run': return job_engine.enqueue(db, actor=context.username, **args)
    if name == 'job_schedule': return job_engine.schedule(db, args['job_id'], value=args['interval'], enabled=args['enabled'])
    if name == 'library_browse':
        return await main.browse_library_request(db, **args)
    if name == 'library_inventory': return await library_summary.summary(db)
    if name == 'library_audit':
        report = main.library_audit_payload(db, **args)
        audit.audit_view(report)
        return report
    if name == 'large_files': return await main.space_review_payload(db, **args)
    if name == 'duplicates_review':
        reviews = await duplicate_actions.review_many(db, [(s['group_key'],s['copy_key']) for s in args['selections']])
        return dict(reviews=reviews, confirmation=duplicate_actions.batch_token(reviews), bytes=sum(r['target']['size'] for r in reviews))
    if name == 'duplicates_delete':
        try: count = await duplicate_actions.remove_many(db, args['confirmation'], actor=context.username)
        finally: main.refresh_inventory_after_delete(db)
        return dict(ok=True, removed=count)
    if name == 'library_monitor': return await libraries.set_library_item_monitoring(**args)
    if name == 'library_delete_review':
        review = await library_actions.inspect(db, **args)
        await streaming_availability.refresh(db, [review['item']], limit=1)
        return dict(library_actions.public(review), confirmation=library_actions.token(review))
    if name == 'library_delete':
        result = await library_actions.execute(db, args['confirmation'], args['mode'], context.username)
        main.refresh_inventory_after_delete(db)
        return result
    if name == 'replacements_list': return await replacements.current_payload(db, **args)
    if name == 'replacements_scan': return job_engine.enqueue(db, 'compact-replacements', actor=context.username, options=args)
    if name == 'replacements_review':
        rows = replacements.selections(db, args['selections'])
        return dict(rows=rows, confirmation=replacements.token(rows), saving_bytes=sum(r['candidate']['saving_bytes'] for r in rows))
    if name == 'replacements_approve': return queue_replacement_approval(db, args['confirmation'], context.username)
    if name == 'replacement_jobs':
        result = replacement_tracking.summary(db)
        result['rows'] = [t for t in result['rows'] if args['view']=='all' or (t['status']=='Imported')==(args['view']=='complete')]
        return dict(result, approval={k:v for k,v in (audit.state(db,replacements.KEY+'-approval') or {}).items() if k!='plan'})
    if name == 'downloads_list': return dict(downloads=main.active_download_payloads(db), summary=main.download_queue_stats(db))
    if name == 'download_control': return await download_controls.control(args['download_id'], args['action'], args['priority'])
    if name == 'streaming_refresh': return job_engine.enqueue(db, 'streaming-availability', actor=context.username, options=args)
    if name == 'replacement_preferences':
        cfg = all_settings()
        return dict(compact_policy.preferences(cfg), streaming_region=streaming_availability.region(cfg), space_review_min_gb=float(cfg.get('space_review_min_gb',25)))
    if name == 'library_preferences_update':
        changes = {key:str(value) for key,value in args.items() if value is not None}
        if 'streaming_region' in changes: changes['streaming_region']=changes['streaming_region'].upper()
        set_settings(changes)
        return await dispatch('replacement_preferences', {}, db, context)
    if name == 'plex_file_review':
        review=await plex_file_actions.inspect(db,args['file_key'])
        return dict(review,confirmation=plex_file_actions.token(review))
    if name == 'plex_file_delete':
        result=await plex_file_actions.remove(db,args['confirmation'],context.username)
        main.refresh_inventory_after_delete(db)
        return result
    if name == 'plex_file_delete_status':return await plex_file_actions.status(db,args['operation_id'])
    raise HTTPException(404, 'Unknown operation')


def endpoint(operation):
    async def get(db: Session=Depends(get_db), context: AuthContext=Depends(require_auth)):
        return await execute(operation.name, {}, db, context)
    async def post(payload=Body(...), db: Session=Depends(get_db), context: AuthContext=Depends(require_auth)):
        return await execute(operation.name, payload.model_dump(), db, context)
    post.__annotations__['payload'] = operation.model
    return get if operation.method=='GET' else post


for operation in OPERATIONS:
    if operation.method:
        router.add_api_route(operation.path, endpoint(operation), methods=[operation.method], name=operation.name)


@router.get('/api/jobs/{job_id}')
def job_detail_api(job_id: str, db: Session=Depends(get_db), context: AuthContext=Depends(require_auth)):
    if not context.has_scope('jobs.read'): raise HTTPException(403, 'Scope required: jobs.read')
    try: return job_engine.detail(db, job_id)
    except ValueError as exc: raise HTTPException(404, str(exc))


@router.get('/jobs')
def jobs_page(request: Request, db: Session=Depends(get_db)):
    from . import main
    user = main.current_user(request, db)
    if not user or not user.is_admin: raise HTTPException(403, 'Admin required')
    return main.templates.TemplateResponse('jobs.html', dict(request=request, user=user, jobs=job_engine.listing(db, history=True), server_timezone=str(main.SERVER_TZ)))


@router.post('/jobs/{job_id}/run')
def job_run_page(job_id: str, request: Request, db: Session=Depends(get_db)):
    from . import main
    user = main.current_user(request, db)
    if not user or not user.is_admin: raise HTTPException(403, 'Admin required')
    try: job_engine.enqueue(db, job_id, actor=user.username)
    except ValueError as exc: return RedirectResponse('/jobs?'+urlencode({'error':str(exc)})+'#'+job_id, status_code=303)
    return RedirectResponse('/jobs#'+job_id, status_code=303)


@router.post('/jobs/{job_id}/schedule')
def job_schedule_page(job_id: str, request: Request, interval: int=Form(...), enabled: bool=Form(...), db: Session=Depends(get_db)):
    from . import main
    user = main.current_user(request, db)
    if not user or not user.is_admin: raise HTTPException(403, 'Admin required')
    try: job_engine.schedule(db, job_id, value=interval, enabled=enabled)
    except ValueError as exc: return RedirectResponse('/jobs?'+urlencode({'error':str(exc)})+'#'+job_id, status_code=303)
    return RedirectResponse('/jobs#'+job_id, status_code=303)


@router.get('/libraries/space/files/{file_key}/delete')
async def plex_file_delete_review_page(file_key: str, request: Request, db: Session=Depends(get_db)):
    from . import main
    user=main.current_user(request,db)
    if not user or not user.is_admin:raise HTTPException(403,'Admin required')
    try:review=await plex_file_actions.inspect(db,file_key)
    except ValueError as exc:raise HTTPException(409,str(exc))
    except Exception:raise HTTPException(409,'Could not inspect this file through Plex. Check its connection and permissions.')
    return main.templates.TemplateResponse('plex_file_delete.html',dict(request=request,user=user,review=review,confirmation=plex_file_actions.token(review)))


@router.post('/libraries/space/files/delete')
async def plex_file_delete_page(request: Request, confirmation: str=Form(...), confirm: Literal['delete']=Form(...), db: Session=Depends(get_db)):
    from . import main
    user=main.current_user(request,db)
    if not user or not user.is_admin:raise HTTPException(403,'Admin required')
    try:result=await plex_file_actions.remove(db,confirmation,user.username)
    except ValueError as exc:raise HTTPException(409,str(exc))
    except Exception:raise HTTPException(409,'Deletion was not confirmed. Check Plex before retrying.')
    main.refresh_inventory_after_delete(db)
    return RedirectResponse('/libraries/space/files/operations/'+result['operation_id'],status_code=303)


@router.get('/libraries/space/files/operations/{operation_id}')
async def plex_file_delete_result_page(operation_id: str, request: Request, db: Session=Depends(get_db)):
    from . import main
    user=main.current_user(request,db)
    if not user or not user.is_admin:raise HTTPException(403,'Admin required')
    try:result=await plex_file_actions.status(db,operation_id)
    except ValueError as exc:raise HTTPException(404,str(exc))
    return main.templates.TemplateResponse('plex_file_delete_result.html',dict(request=request,user=user,result=result))
