"""Run tracking and controls around the application's existing APScheduler."""
import asyncio
import functools
import uuid
from datetime import datetime, timedelta, timezone
from . import library_audit as audit
from .job_registry import JOBS, BY_ID, interval
from .settings_store import all_settings, set_settings, env_owned
from ..db import SessionLocal

scheduler = None
handlers = {}
locks = {}
HISTORY_LIMIT = 30


class JobBusy(Exception):
    """An existing operation owns the source lock; no scan was performed."""


def stamp(): return datetime.utcnow().isoformat()
def state_key(job_id): return 'job-state-' + job_id
def schedule_key(job_id): return 'job-schedule-' + job_id


def known(job_id):
    if job_id not in BY_ID: raise ValueError('Unknown job.')
    return BY_ID[job_id]


def record(db, job_id, run):
    state = audit.state(db, state_key(job_id)) or dict(runs=[])
    runs = [r for r in state.get('runs', []) if r['id'] != run['id']]
    state['runs'] = [run] + runs[:HISTORY_LIMIT-1]
    audit.save(db, state_key(job_id), state)


def report_summary(db, job_id):
    key = {'library-health':'health', 'library-storage':'storage', 'library-inventory':'inventory-summary',
           'compact-replacements':'compact-replacements', 'compact-replacement-approval':'compact-replacements-approval'}.get(job_id)
    report = audit.state(db, key) if key else None
    if not report: return {}
    result = {k:report[k] for k in ('checked_at','complete','issue_count','total_bytes','done','total','status') if k in report}
    result['error_count'] = len(report.get('errors', []))
    if job_id == 'library-storage':
        result.update(files=len(report.get('files', [])), duplicate_groups=len(report.get('duplicates', [])))
        result['label'] = f"{result['duplicate_groups']:,} duplicate groups · {result['files']:,} files"
    elif job_id == 'library-health': result['label'] = f"{report.get('issue_count', 0)} issues"
    elif job_id == 'library-inventory':
        from .libraries import format_bytes
        result['label'] = f"{format_bytes(report.get('total_bytes'))} · {report.get('movies', 0):,} movies · {report.get('shows', 0):,} shows"
    elif job_id in ('compact-replacements','compact-replacement-approval'):
        result['label'] = f"{report.get('done', 0)} / {report.get('total',len(report.get('rows', [])))} checked"
    if result['error_count']: result['label'] = result.get('label', '') + f" · {result['error_count']} source errors"
    return result


def tracked(job_id):
    """Wrap both scheduled and manual invocations; never retain arguments or secrets."""
    known(job_id)
    def decorate(function):
        @functools.wraps(function)
        async def wrapper(*args, **kwargs):
            lock = locks.setdefault(job_id, asyncio.Lock())
            if lock.locked(): return None
            async with lock:
                with SessionLocal() as db:
                    state = audit.state(db, state_key(job_id)) or {}
                    queued = next((r for r in state.get('runs', []) if r['status']=='queued'), None)
                    run = dict(queued or dict(id=uuid.uuid4().hex, trigger='scheduled'), status='running', started_at=stamp())
                    record(db, job_id, run)
                try:
                    result = await function(*args, **kwargs)
                except JobBusy:
                    with SessionLocal() as db:
                        record(db, job_id, dict(run, status='skipped', finished_at=stamp(), error='A source operation was already running.'))
                    return None
                except BaseException as exc:
                    with SessionLocal() as db:
                        record(db, job_id, dict(run, status='interrupted' if isinstance(exc, asyncio.CancelledError) else 'failed',
                            finished_at=stamp(), duration_seconds=round((datetime.utcnow()-datetime.fromisoformat(run['started_at'])).total_seconds(), 3),
                            error_type=type(exc).__name__, error='Run did not complete. Check the source connection and report.'))
                    raise
                with SessionLocal() as db:
                    summary = report_summary(db, job_id)
                    issues = summary.get('complete') is False or summary.get('error_count', 0) or summary.get('issue_count', 0) or summary.get('status') in ('failed', 'stopped', 'interrupted')
                    record(db, job_id, dict(run, status='completed_with_issues' if issues else 'completed', finished_at=stamp(),
                        duration_seconds=round((datetime.utcnow()-datetime.fromisoformat(run['started_at'])).total_seconds(), 3), summary=summary))
                return result
        return wrapper
    return decorate


def install(engine, namespace):
    global scheduler, handlers
    first_install = not handlers
    scheduler = engine
    handlers = {job.id:namespace[job.handler] for job in JOBS}
    cfg = all_settings()
    with SessionLocal() as db:
        for job in JOBS:
            state = audit.state(db, state_key(job.id)) or {}
            for run in list(state.get('runs', [])) if first_install else []:
                if run['status'] in ('queued', 'running'):
                    record(db, job.id, dict(run, status='interrupted', finished_at=stamp(), error='App restarted before completion; review the report.'))
            if not job.setting: continue
            saved = audit.state(db, schedule_key(job.id)) or {}
            value = interval(job, cfg)
            # Use persisted report age for expensive scans after a restart.
            key = {'library-health':'health', 'library-storage':'storage', 'compact-replacements':'compact-replacements'}.get(job.id)
            previous = audit.state(db, key) if key else None
            due = datetime.now(timezone.utc)+timedelta(seconds=job.delay)
            if previous and previous.get('checked_at'):
                last = datetime.fromisoformat(previous['checked_at'])
                last = last.replace(tzinfo=timezone.utc) if last.tzinfo is None else last.astimezone(timezone.utc)
                due = max(due, last+timedelta(**{job.unit:value}))
            if job.id == 'compact-replacements' and previous and previous.get('status') == 'running':
                due = datetime.now(timezone.utc)+timedelta(seconds=1)
            engine.add_job(handlers[job.id], 'interval', **{job.unit:value}, id=job.id, name=job.label,
                           replace_existing=True, max_instances=1, coalesce=True, next_run_time=due if saved.get('enabled', True) else None)


def listing(db, history=False):
    cfg = all_settings(); result = []
    for spec in JOBS:
        schedule = audit.state(db, schedule_key(spec.id)) or {}
        job = scheduler.get_job(spec.id) if scheduler else None
        pending = scheduler.get_job(spec.id+'-manual') if scheduler else None
        runs = (audit.state(db, state_key(spec.id)) or {}).get('runs', [])
        latest = runs[0] if runs else None
        enabled = bool(spec.setting and schedule.get('enabled', True))
        result.append(dict(id=spec.id, label=spec.label, description=spec.description, setting=spec.setting,
            unit=spec.unit, value=interval(spec, cfg) if spec.setting else None,
            minimum=spec.minimum, maximum=spec.maximum, enabled=enabled, manual=spec.manual, report_url=spec.report_url,
            next_run=getattr(job, 'next_run_time', None), queued=bool(pending), status=(latest or {}).get('status', 'not_run'),
            last_run=latest, last_report=report_summary(db, spec.id), runs=runs if history else [], registered=job is not None or not spec.setting))
    return result


def detail(db, job_id):
    known(job_id)
    return next(r for r in listing(db, history=True) if r['id']==job_id)


def enqueue(db, job_id, *, actor='admin', options=None):
    spec = known(job_id)
    if not spec.manual: raise ValueError('This job runs only through its approval or connection workflow.')
    if scheduler is None: raise ValueError('Scheduler is not running.')
    if job_id == 'compact-replacements':
        from . import replacements
        if replacements.scan_lock.locked() or replacements.action_lock.locked(): raise ValueError('A replacement check or approval is already running.')
    if locks.get(job_id) and locks[job_id].locked(): raise ValueError('This job is already running.')
    pending_id = job_id+'-manual'
    if scheduler.get_job(pending_id): raise ValueError('This job is already queued.')
    options = options or {}
    allowed = {'streaming-availability':{'full'}, 'compact-replacements':{'q','source','min_gb','managed'}}.get(job_id, set())
    if set(options)-allowed: raise ValueError('Unsupported job options.')
    run = dict(id=uuid.uuid4().hex, status='queued', trigger='manual', actor=actor, queued_at=stamp())
    record(db, job_id, run)
    try:
        scheduler.add_job(handlers[job_id], 'date', id=pending_id, run_date=datetime.now(timezone.utc), kwargs=options,
                          replace_existing=False, max_instances=1)
    except Exception:
        record(db, job_id, dict(run, status='failed', finished_at=stamp(), error='Could not queue this job.'))
        raise
    return dict(job_id=job_id, run_id=run['id'], status='queued', url='/jobs#'+job_id)


def schedule(db, job_id, *, value=None, enabled=None):
    spec = known(job_id)
    if not spec.setting: raise ValueError('This workflow has no recurring schedule.')
    if scheduler is None or not scheduler.get_job(job_id): raise ValueError('Scheduler is not running.')
    saved = audit.state(db, schedule_key(job_id)) or {}
    value = interval(spec, all_settings()) if value is None else value
    if not spec.minimum <= value <= spec.maximum: raise ValueError(f'Interval must be {spec.minimum}–{spec.maximum} {spec.unit}.')
    if spec.setting in env_owned() and value != interval(spec, all_settings()):
        raise ValueError('This interval is managed by an environment variable. Change it in the deployment configuration.')
    enabled = saved.get('enabled', True) if enabled is None else enabled
    scheduler.reschedule_job(job_id, trigger='interval', **{spec.unit:value})
    if not enabled: scheduler.pause_job(job_id)
    else: scheduler.resume_job(job_id)
    set_settings({spec.setting:str(value)})
    audit.save(db, schedule_key(job_id), dict(enabled=enabled))
    return detail(db, job_id)
