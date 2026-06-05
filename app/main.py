from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import re
import secrets
from zoneinfo import ZoneInfo
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from fastapi import FastAPI, Request, Depends, Form, Body, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session
from .config import settings
from .db import Base, engine, SessionLocal, ensure_schema_extensions
from .models import MediaRequest, PlexSession, User, ActivePlexSession, ActiveDownloadItem, UserPolicy, UserBlock, UserWatchlistItem, AppSetting
from .services.plex_auth import create_pin, fetch_identity, fetch_pin, fetch_resources, choose_server, choose_connection, plex_auth_url
from .services.tautulli_import import import_tautulli, import_tautulli_api, enrich_tautulli_bandwidth
from .services.legacy_requests import import_legacy_requests
from .services.settings_store import all_settings, configured, set_settings, media_server_label, settings_sources
from .services.sync import sync_requests
from .services.clients import PlexClient, RadarrClient, SeerrClient, ServiceConfig, SonarrClient
from .services.analytics import weekly_watch, decision_breakdown
from .services.plex_events import upsert_active_from_live, finalize_session, reconcile_live_sessions
from .services.network_enrichment import reverse_dns, lookup_isp
from .services.policies import enforce_policies
from .services.art_cache import ensure_art_cached
from .services.plex_users import refresh_plex_accounts
from .services.library_catalog import sync_plex_library_catalog
from .services.service_tests import test_service
from .services.request_intelligence import pending_request_payload
from .services.local_auth import hash_password, local_auth_configured, local_plex_id, verify_password
from .api_auth import AuthContext, require_auth
from .services import libraries as library_service
from .integrations import router as integrations_router
from .services.webhooks import notify_homeassistant


PENDING_PLEX_AUTHS: dict[str, dict] = {}


PERM = {
    'ADMIN': 2,
    'MANAGE_USERS': 8,
    'MANAGE_REQUESTS': 16,
    'REQUEST': 32,
    'AUTO_APPROVE': 128,
    'AUTO_APPROVE_MOVIE': 256,
    'AUTO_APPROVE_TV': 512,
    'REQUEST_4K': 1024,
    'REQUEST_4K_MOVIE': 2048,
    'REQUEST_4K_TV': 4096,
    'REQUEST_ADVANCED': 8192,
    'REQUEST_VIEW': 16384,
    'REQUEST_MOVIE': 262144,
    'REQUEST_TV': 524288,
    'AUTO_REQUEST': 8388608,
    'AUTO_REQUEST_MOVIE': 16777216,
    'AUTO_REQUEST_TV': 33554432,
}
MANAGED_SEERR_PERMS = (
    PERM['REQUEST'] | PERM['REQUEST_MOVIE'] | PERM['REQUEST_TV'] |
    PERM['AUTO_APPROVE'] | PERM['AUTO_APPROVE_MOVIE'] | PERM['AUTO_APPROVE_TV'] |
    PERM['REQUEST_4K'] | PERM['REQUEST_4K_MOVIE'] | PERM['REQUEST_4K_TV'] |
    PERM['REQUEST_ADVANCED'] | PERM['REQUEST_VIEW'] |
    PERM['AUTO_REQUEST'] | PERM['AUTO_REQUEST_MOVIE'] | PERM['AUTO_REQUEST_TV']
)

SERVER_TZ = ZoneInfo('Pacific/Auckland')

OVERVIEW_PERIODS = (
    {'key': 'all', 'label': 'All time', 'short_label': 'All time', 'days': None},
    {'key': '12m', 'label': 'Past 12 months', 'short_label': '12 months', 'days': 365},
    {'key': '6m', 'label': 'Past 6 months', 'short_label': '6 months', 'days': 183},
    {'key': '30d', 'label': 'Past 30 days', 'short_label': '30 days', 'days': 30},
)
OVERVIEW_PERIOD_BY_KEY = {period['key']: period for period in OVERVIEW_PERIODS}


def public_url(path: str = '') -> str:
    base = (settings.base_url or '').rstrip('/')
    if not path:
        return base
    return f"{base}/{path.lstrip('/')}"


def format_server_time(value):
    if value is None or value == '':
        return None
    if isinstance(value, int):
        return str(value)
    raw = str(value)
    if raw.isdigit() and len(raw) == 4:
        return raw
    try:
        dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except Exception:
        return raw
    if dt.tzinfo is None:
        if 'T' not in raw:
            return dt.strftime('%Y-%m-%d')
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(SERVER_TZ)
    if local.hour == 0 and local.minute == 0 and local.second == 0 and 'T' not in raw:
        return local.strftime('%Y-%m-%d')
    return local.strftime('%Y-%m-%d %H:%M')


def format_age(value, now=None):
    if not value:
        return '—'
    age = (now or datetime.utcnow()) - value
    if age.days >= 14:
        return f'{age.days//7}w ago'
    if age.days >= 1:
        return f'{age.days}d ago'
    if age.seconds >= 3600:
        return f'{age.seconds//3600}h ago'
    return f'{max(1, age.seconds//60)}m ago'


STATUS_LABEL = {'1': 'Requested', '2': 'Approved', '3': 'Declined', '4': 'Available', '5': 'Available', 'pending': 'Requested', 'approved': 'Approved', 'declined': 'Declined', 'available': 'Available'}
logger = logging.getLogger('mediaops')


def seerr_policy_from_permissions(value: int):
    def has(bit):
        return bool(value & bit)
    auto_all = has(PERM['AUTO_APPROVE'])
    if not has(PERM['REQUEST']):
        request_mode = 'disabled'
    elif auto_all or has(PERM['AUTO_APPROVE_MOVIE']) or has(PERM['AUTO_APPROVE_TV']):
        request_mode = 'auto'
    else:
        request_mode = 'approval'
    if has(PERM['AUTO_REQUEST_MOVIE']) and has(PERM['AUTO_REQUEST_TV']):
        auto_request_mode = 'both'
    elif has(PERM['AUTO_REQUEST_MOVIE']):
        auto_request_mode = 'movies'
    elif has(PERM['AUTO_REQUEST_TV']):
        auto_request_mode = 'tv'
    elif has(PERM['AUTO_REQUEST']):
        auto_request_mode = 'both'
    else:
        auto_request_mode = 'off'
    return {
        'raw': value,
        'base': value & ~MANAGED_SEERR_PERMS,
        'request_mode': request_mode,
        'allow_movies': has(PERM['REQUEST_MOVIE']) or has(PERM['REQUEST']),
        'allow_tv': has(PERM['REQUEST_TV']) or has(PERM['REQUEST']),
        'allow_4k': has(PERM['REQUEST_4K']) or has(PERM['REQUEST_4K_MOVIE']) or has(PERM['REQUEST_4K_TV']),
        'advanced': has(PERM['REQUEST_ADVANCED']),
        'request_view': has(PERM['REQUEST_VIEW']),
        'auto_request_mode': auto_request_mode,
    }

app = FastAPI(title=settings.app_name)
scheduler = AsyncIOScheduler()
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site='lax', https_only=settings.base_url.startswith('https://'))
app.mount('/static', StaticFiles(directory='app/static'), name='static')
app.include_router(integrations_router)
templates = Jinja2Templates(directory='app/templates')
templates.env.globals['media_server_label'] = media_server_label


UNSAFE_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}
WEAK_SECRET_KEYS = {'', 'change-me', 'changeme', 'replace-me-with-a-long-random-value', 'local-wizard-secret-change-me'}
CACHED_ART_RE = re.compile(r'^[a-f0-9]{40}\.jpg$')
PLEX_IMAGE_PREFIXES = ('/library/metadata/', '/photo/:/transcode')


def validate_runtime_security() -> None:
    if settings.secret_key.strip().lower() in WEAK_SECRET_KEYS:
        raise RuntimeError('SECRET_KEY must be set to a long random value before MediaOps will start.')
    if settings.setup_no_auth and not configured():
        logger.warning('SETUP_NO_AUTH=true and Plex is not configured. Bind MediaOps to localhost or a trusted LAN until first-run setup is complete.')


def _base_origin() -> str:
    parts = urlsplit(settings.base_url)
    return f'{parts.scheme}://{parts.netloc}' if parts.scheme and parts.netloc else ''


def _request_host_origin(request: Request) -> str:
    host = request.headers.get('x-forwarded-host') or request.headers.get('host') or ''
    if not host:
        return ''
    proto = request.headers.get('x-forwarded-proto') or request.url.scheme or 'http'
    return f'{proto.split(",")[0].strip()}://{host.split(",")[0].strip()}'


def _request_origin_allowed(request: Request) -> bool:
    observed = request.headers.get('origin') or request.headers.get('referer') or ''
    if not observed:
        return False
    parts = urlsplit(observed)
    origin = f'{parts.scheme}://{parts.netloc}' if parts.scheme and parts.netloc else ''
    allowed_origins = [value for value in {_base_origin(), _request_host_origin(request)} if value]
    if not allowed_origins:
        return True
    if any(hmac.compare_digest(origin, expected) for expected in allowed_origins):
        return True
    loopback_hosts = {'localhost', '127.0.0.1', '::1'}
    for expected in allowed_origins:
        expected_parts = urlsplit(expected)
        if expected_parts.scheme == 'http' and parts.scheme == 'http':
            if expected_parts.hostname in loopback_hosts and parts.hostname in loopback_hosts and expected_parts.port == parts.port:
                return True
    return False


@app.middleware('http')
async def reject_cross_origin_unsafe_requests(request: Request, call_next):
    if request.method.upper() in UNSAFE_METHODS:
        auth_header = request.headers.get('authorization', '')
        if not auth_header.lower().startswith('bearer ') and not request.url.path.startswith('/webhooks/'):
            if not _request_origin_allowed(request):
                return Response('Cross-origin unsafe request blocked', status_code=403)
    return await call_next(request)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def seerr_user_context(username: str):
    cfg = all_settings()
    if not cfg.get('seerr_url') or not cfg.get('seerr_api_key'):
        return None, None, None, []
    client = SeerrClient(ServiceConfig(url=cfg['seerr_url'], api_key=cfg['seerr_api_key']))
    try:
        users_payload = await client.users(take=1000)
        users = users_payload.get('results', []) if isinstance(users_payload, dict) else []
        needle = username.lower()
        seerr_user = next((u for u in users if needle in {str(u.get('plexUsername') or '').lower(), str(u.get('displayName') or '').lower(), str(u.get('username') or '').lower(), str(u.get('email') or '').lower()}), None)
        if not seerr_user:
            return None, None, None, users
        user_id = int(seerr_user['id'])
        full_user = await client.user(user_id)
        quota = await client.user_quota(user_id)
        perms = await client.user_permissions(user_id)
        return full_user or seerr_user, quota, perms, users
    except Exception:
        return None, None, None, []


def plex_permission_context(profile_user: User | None):
    if not profile_user:
        return {}
    return {
        'read_only': True,
        'allow_sync': profile_user.allow_sync,
        'allow_channels': profile_user.allow_channels,
        'allow_tuners': profile_user.allow_tuners,
        'allow_camera_upload': profile_user.allow_camera_upload,
        'allow_subtitle_admin': profile_user.allow_subtitle_admin,
        'shared_all_libraries': profile_user.shared_all_libraries,
        'shared_num_libraries': profile_user.shared_num_libraries,
        'shared_pending': profile_user.shared_pending,
        'shared_owned': profile_user.shared_owned,
        'filter_all': profile_user.filter_all,
        'filter_movies': profile_user.filter_movies,
        'filter_music': profile_user.filter_music,
        'filter_photos': profile_user.filter_photos,
        'filter_television': profile_user.filter_television,
    }


def dedupe_requests(rows):
    best = {}
    def score(r):
        placeholder = r.title.startswith('movie #') or r.title.startswith('tv #') or r.title.startswith('media #') or r.title == 'Unknown'
        return (0 if placeholder else 10) + (3 if r.fulfilled_bytes else 0) + (1 if r.source == 'legacy-seerr' else 0)
    for row in rows:
        key = row.source_id or str(row.id)
        if key not in best or score(row) > score(best[key]):
            best[key] = row
    return sorted(best.values(), key=lambda r: r.requested_at, reverse=True)


async def scheduled_request_sync():
    with SessionLocal() as db:
        changed = await sync_requests(db)
        if changed:
            pending = db.scalar(select(func.count(MediaRequest.id)).where(MediaRequest.status.in_(['pending', 'requested', '1']))) or 0
            await notify_homeassistant('requests_changed', {'changed': changed, 'pending_requests': int(pending)})


async def scheduled_plex_account_refresh():
    with SessionLocal() as db:
        await refresh_plex_accounts(db)


async def scheduled_plex_poll():
    cfg = all_settings()
    if not cfg.get('plex_server_url') or not cfg.get('plex_server_token'):
        return
    client = PlexClient(ServiceConfig(url=cfg['plex_server_url'], token=cfg['plex_server_token']))
    sessions = await client.sessions()
    with SessionLocal() as db:
        reconcile_live_sessions(db, sessions)
        await enforce_policies(db, client, sessions)


async def scheduled_downloads_poll():
    cfg = all_settings()
    radarr, sonarr = await service_clients()
    queue = []
    if cfg.get('plex_server_url') and cfg.get('plex_server_token'):
        try:
            client = PlexClient(ServiceConfig(url=cfg['plex_server_url'], token=cfg['plex_server_token']))
            queue += await client.background_transcodes()
        except Exception:
            pass
    if radarr:
        try:
            payload = await radarr.queue()
            queue += [normalise_queue_item('Radarr', row) for row in payload.get('records', [])]
        except Exception:
            pass
    if sonarr:
        try:
            payload = await sonarr.queue()
            queue += [normalise_queue_item('Sonarr', row) for row in payload.get('records', [])]
        except Exception:
            pass
    with SessionLocal() as db:
        reconcile_download_queue(db, queue)


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def scheduler_values(values: dict | None = None) -> dict[str, int]:
    values = values or all_settings()
    return {
        'plex_live_seconds': _bounded_int(values.get('job_plex_live_seconds'), 30, 10, 300),
        'downloads_seconds': _bounded_int(values.get('job_downloads_seconds'), 30, 10, 300),
        'plex_accounts_minutes': _bounded_int(values.get('job_plex_accounts_minutes'), 60, 15, 1440),
        'requests_minutes': _bounded_int(values.get('job_requests_minutes'), settings.sync_interval_minutes, 5, 1440),
    }


def configure_scheduler() -> None:
    job_values = scheduler_values()
    scheduler.add_job(scheduled_request_sync, 'interval', minutes=job_values['requests_minutes'], id='request-sync', replace_existing=True, next_run_time=datetime.utcnow())
    scheduler.add_job(scheduled_plex_poll, 'interval', seconds=job_values['plex_live_seconds'], id='plex-poll', replace_existing=True, next_run_time=datetime.utcnow())
    scheduler.add_job(scheduled_downloads_poll, 'interval', seconds=job_values['downloads_seconds'], id='downloads-poll', replace_existing=True, next_run_time=datetime.utcnow())
    scheduler.add_job(scheduled_plex_account_refresh, 'interval', minutes=job_values['plex_accounts_minutes'], id='plex-account-refresh', replace_existing=True, next_run_time=datetime.utcnow())


def scheduler_context() -> list[dict]:
    labels = {
        'plex-poll': ('Plex live sessions', 'Keeps live sessions, devices, session endings and policy enforcement current.'),
        'downloads-poll': ('Download queue', 'Checks Radarr and Sonarr queue state for the operations banner and live view.'),
        'plex-account-refresh': ('Plex accounts', 'Refreshes Plex friends, shared access metadata, profile fields and visible watchlist data.'),
        'request-sync': ('Request sync', 'Imports Seerr requests and updates availability and size from Radarr/Sonarr.'),
    }
    units = {'plex-poll': 'seconds', 'downloads-poll': 'seconds', 'plex-account-refresh': 'minutes', 'request-sync': 'minutes'}
    setting_names = {'plex-poll': 'job_plex_live_seconds', 'downloads-poll': 'job_downloads_seconds', 'plex-account-refresh': 'job_plex_accounts_minutes', 'request-sync': 'job_requests_minutes'}
    values = all_settings()
    rows = []
    for job_id in ['plex-poll', 'downloads-poll', 'plex-account-refresh', 'request-sync']:
        job = scheduler.get_job(job_id)
        label, description = labels[job_id]
        rows.append({
            'id': job_id,
            'label': label,
            'description': description,
            'setting': setting_names[job_id],
            'unit': units[job_id],
            'value': values.get(setting_names[job_id], ''),
            'next_run': job.next_run_time if job else None,
        })
    return rows


TAUTULLI_BOOTSTRAP_FINGERPRINT_KEY = 'tautulli_api_bootstrap_fingerprint'
TAUTULLI_BOOTSTRAP_STATUS_KEY = 'tautulli_api_bootstrap_status'


def tautulli_fingerprint(values: dict[str, str]) -> str:
    url = (values.get('tautulli_url') or '').rstrip('/')
    api_key = (values.get('tautulli_api_key') or '').strip()
    if not url or not api_key:
        return ''
    return hashlib.sha256(f'{url}\n{api_key}'.encode()).hexdigest()


def get_app_setting(db: Session, key: str) -> str:
    row = db.get(AppSetting, key)
    return row.value if row else ''


def set_app_setting(db: Session, key: str, value: str, *, is_secret: bool = False) -> None:
    row = db.get(AppSetting, key)
    if row:
        row.value = value
        row.is_secret = is_secret
    else:
        db.add(AppSetting(key=key, value=value, is_secret=is_secret))
    db.commit()


def tautulli_import_ok(result) -> bool:
    message = result.message or ''
    return bool(message and not message.startswith('Tautulli URL') and not message.startswith('Tautulli API did not'))


async def tautulli_initial_sync_job(fingerprint: str, url: str, api_key: str) -> None:
    with SessionLocal() as db:
        set_app_setting(db, TAUTULLI_BOOTSTRAP_STATUS_KEY, 'Syncing Tautulli history and bandwidth estimates...')
        try:
            imported = await import_tautulli_api(db, url, api_key, full=False)
            if not tautulli_import_ok(imported):
                raise RuntimeError(imported.message or 'Tautulli import failed')
            enriched = await enrich_tautulli_bandwidth(db, url, api_key)
            set_app_setting(db, TAUTULLI_BOOTSTRAP_FINGERPRINT_KEY, fingerprint)
            set_app_setting(
                db,
                TAUTULLI_BOOTSTRAP_STATUS_KEY,
                f'Tautulli sync complete. {imported.message} {enriched.message}',
            )
        except Exception as exc:
            logger.exception('Initial Tautulli sync failed')
            set_app_setting(db, TAUTULLI_BOOTSTRAP_STATUS_KEY, f'Tautulli sync failed: {exc.__class__.__name__}')


def maybe_schedule_initial_tautulli_sync(db: Session, values: dict[str, str]) -> str:
    fingerprint = tautulli_fingerprint(values)
    if not fingerprint:
        return ''
    if hmac.compare_digest(get_app_setting(db, TAUTULLI_BOOTSTRAP_FINGERPRINT_KEY), fingerprint):
        return ''
    job_id = f'tautulli-initial-sync-{fingerprint[:12]}'
    if scheduler.get_job(job_id):
        return 'Tautulli history import and bandwidth backfill are already running.'
    scheduler.add_job(
        tautulli_initial_sync_job,
        args=[fingerprint, values.get('tautulli_url', ''), values.get('tautulli_api_key', '')],
        id=job_id,
        replace_existing=True,
        next_run_time=datetime.utcnow(),
    )
    set_app_setting(db, TAUTULLI_BOOTSTRAP_STATUS_KEY, 'Tautulli history import and bandwidth backfill queued.')
    return 'Tautulli connected. History import and bandwidth backfill are running in the background.'


def settings_template_context(request: Request, values: dict, user, test_kind: str = '', test_ok: str = '', test_message: str = '', import_ok: str = '', import_message: str = '') -> dict:
    return {
        'request': request,
        'values': values,
        'sources': settings_sources(),
        'local_auth_configured': local_auth_configured(values),
        'user': user,
        'services': service_context(values),
        'jobs': scheduler_context(),
        'setup_token': request.session.get('setup_token') or '',
        'test': {'kind': test_kind, 'ok': test_ok, 'message': test_message},
        'import_result': {'ok': import_ok, 'message': import_message},
    }


def append_query_preserving_fragment(target: str, params: dict[str, str]) -> str:
    parts = urlsplit(safe_internal_redirect(target, '/settings'))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or '/settings', urlencode(query), parts.fragment))


def safe_internal_redirect(target: str, fallback: str = '/') -> str:
    if not target.startswith('/') or target.startswith('//'):
        return fallback
    return target


def setup_token_valid(request: Request, form) -> bool:
    expected = str(request.session.get('setup_token') or '')
    provided = str(form.get('setup_token') or '')
    return bool(expected and provided and hmac.compare_digest(expected, provided))


def setup_local_auth_required_redirect(values: dict) -> RedirectResponse | None:
    if not configured() and not local_auth_configured(values):
        return RedirectResponse('/setup?local_error=required', status_code=303)
    return None


@app.on_event('startup')
def startup():
    validate_runtime_security()
    Base.metadata.create_all(bind=engine)
    ensure_schema_extensions()
    with SessionLocal() as db:
        if settings.import_tautulli_db:
            import_tautulli(db, settings.import_tautulli_db)
        if settings.import_seerr_db and settings.import_radarr_db and settings.import_sonarr_db:
            import_legacy_requests(db, settings.import_seerr_db, settings.import_radarr_db, settings.import_sonarr_db)
    if not scheduler.running:
        configure_scheduler()
        scheduler.start()


@app.get('/healthz')
def healthz():
    return {'ok': True}


def current_user(request: Request, db: Session):
    if settings.setup_no_auth and not configured():
        setup_name = settings.setup_user or 'setup'
        user = db.scalar(select(User).where(User.plex_id == f'setup-{setup_name}'))
        if not user:
            user = User(plex_id=f'setup-{setup_name}', username=setup_name, email=None, display_name='Setup admin', is_admin=True)
            db.add(user); db.commit(); db.refresh(user)
        return user
    plex_id = request.session.get('plex_id')
    if not plex_id:
        return None
    return db.scalar(select(User).where(User.plex_id == str(plex_id)))


def ensure_local_admin(db: Session, username: str) -> User:
    username = (username or 'admin').strip() or 'admin'
    plex_id = local_plex_id(username)
    user = db.scalar(select(User).where(User.plex_id == plex_id))
    if not user:
        user = User(
            plex_id=plex_id,
            username=username,
            email=None,
            display_name=username,
            friendly_name=username,
            plex_source='local-auth',
            is_admin=True,
        )
        db.add(user)
    user.username = username
    user.display_name = user.display_name or username
    user.friendly_name = user.friendly_name or username
    user.plex_source = 'local-auth'
    user.is_admin = True
    user.last_seen_at = datetime.utcnow()
    db.commit()
    db.refresh(user)
    return user


def media_bucket(media_type: str | None):
    if media_type in {'episode', 'show'}:
        return 'TV'
    if media_type == 'movie':
        return 'Movies'
    if media_type in {'track', 'artist', 'album'}:
        return 'Music'
    return 'Other'


def empty_media_counts():
    return {'TV': 0, 'Movies': 0, 'Music': 0, 'Other': 0}


def stack_rows(rows, label_key, value_key='plays'):
    out = {}
    for row in rows:
        label = getattr(row, label_key)
        out.setdefault(label, empty_media_counts())
        out[label][media_bucket(getattr(row, 'media_type'))] += int(getattr(row, value_key) or 0)
    return out


def graphs_payload(db: Session, username: str | None = None, days: int = 365):
    now = datetime.utcnow()
    days = days if days in {30, 90, 365} else 365
    since = now - timedelta(days=days)
    period_label = 'Last 30 days' if days == 30 else ('Last 90 days' if days == 90 else 'Last 12 months')
    filters = [PlexSession.started_at >= since]
    if username:
        filters.append(func.lower(PlexSession.username) == username.lower())

    plays, seconds, bytes_, transcodes, avg_kbps, peak_kbps = db.execute(select(
        func.count(PlexSession.id),
        func.coalesce(func.sum(PlexSession.watched_seconds), 0),
        func.coalesce(func.sum(PlexSession.bytes_streamed), 0),
        func.coalesce(func.sum(case((PlexSession.transcode_decision == 'transcode', 1), else_=0)), 0),
        func.coalesce(func.avg(PlexSession.bandwidth_kbps), 0),
        func.coalesce(func.max(PlexSession.bandwidth_kbps), 0),
    ).where(*filters)).one()

    # Demand chart uses daily buckets for short windows and weekly buckets for the yearly lens,
    # otherwise 365 tiny bars become visual static and the scale becomes unreadable.
    bucket_kind = 'day' if days <= 90 else 'week'
    bucket = func.date_trunc(bucket_kind, PlexSession.started_at)
    demand_rows = db.execute(select(
        bucket.label('bucket'), PlexSession.media_type,
        func.count(PlexSession.id).label('plays'),
        func.coalesce(func.sum(PlexSession.watched_seconds), 0).label('seconds'),
        func.coalesce(func.sum(PlexSession.bytes_streamed), 0).label('bytes'),
    ).where(*filters).group_by(bucket, PlexSession.media_type).order_by(bucket)).all()
    demand = {}
    cursor = since.date()
    if bucket_kind == 'week':
        cursor = cursor - timedelta(days=cursor.weekday())
        step = timedelta(days=7)
        while cursor <= now.date():
            demand[cursor.isoformat()] = {'TV': 0, 'Movies': 0, 'Music': 0, 'Other': 0, 'hours': 0.0, 'gb': 0.0, 'plays': 0, 'label': cursor.strftime('%d %b')}
            cursor += step
    else:
        while cursor <= now.date():
            demand[cursor.isoformat()] = {'TV': 0, 'Movies': 0, 'Music': 0, 'Other': 0, 'hours': 0.0, 'gb': 0.0, 'plays': 0, 'label': cursor.strftime('%d %b')}
            cursor += timedelta(days=1)
    for r in demand_rows:
        key = r.bucket.date().isoformat()
        if key in demand:
            media = media_bucket(r.media_type)
            demand[key][media] += int(r.plays or 0)
            demand[key]['plays'] += int(r.plays or 0)
            demand[key]['hours'] += float(r.seconds or 0) / 3600
            demand[key]['gb'] += float(r.bytes or 0) / 1e9

    dow_rows = db.execute(select(func.extract('dow', PlexSession.started_at).label('dow'), PlexSession.media_type, func.count(PlexSession.id).label('plays')).where(*filters).group_by('dow', PlexSession.media_type).order_by('dow')).all()
    dow_names = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday']
    dow = {name: empty_media_counts() for name in dow_names}
    for r in dow_rows:
        dow[dow_names[int(r.dow)]][media_bucket(r.media_type)] += int(r.plays or 0)

    hour_rows = db.execute(select(func.extract('hour', PlexSession.started_at).label('hour'), PlexSession.media_type, func.count(PlexSession.id).label('plays')).where(*filters).group_by('hour', PlexSession.media_type).order_by('hour')).all()
    hourly = {f'{h:02d}': empty_media_counts() for h in range(24)}
    for r in hour_rows:
        hourly[f'{int(r.hour):02d}'][media_bucket(r.media_type)] += int(r.plays or 0)

    platform_rows = db.execute(select(PlexSession.platform.label('label'), PlexSession.media_type, func.count(PlexSession.id).label('plays')).where(*filters, PlexSession.platform.is_not(None)).group_by(PlexSession.platform, PlexSession.media_type)).all()
    platform_stacks = stack_rows(platform_rows, 'label')
    top_platforms = sorted(platform_stacks.items(), key=lambda kv: sum(kv[1].values()), reverse=True)[:10]

    user_rows = db.execute(select(PlexSession.username.label('label'), PlexSession.media_type, func.count(PlexSession.id).label('plays')).where(*filters).group_by(PlexSession.username, PlexSession.media_type)).all()
    user_stacks = stack_rows(user_rows, 'label')
    top_users = sorted(user_stacks.items(), key=lambda kv: sum(kv[1].values()), reverse=True)[:10]

    decision_rows = db.execute(select(PlexSession.transcode_decision.label('label'), func.count(PlexSession.id).label('plays'), func.coalesce(func.sum(PlexSession.bytes_streamed),0).label('bytes')).where(*filters).group_by(PlexSession.transcode_decision).order_by(func.count(PlexSession.id).desc())).all()
    request_filters = [MediaRequest.requested_at >= now - timedelta(days=365)]
    if username:
        request_filters.append(func.lower(MediaRequest.requester_name) == username.lower())
    request_rows = db.execute(select(MediaRequest.requester_name.label('label'), MediaRequest.request_type, func.count(MediaRequest.id).label('requests'), func.coalesce(func.sum(MediaRequest.fulfilled_bytes),0).label('bytes')).where(*request_filters).group_by(MediaRequest.requester_name, MediaRequest.request_type)).all()
    request_stacks = {}
    for r in request_rows:
        request_stacks.setdefault(r.label, {'movie': 0, 'tv': 0, 'storage_gb': 0.0})
        request_stacks[r.label][r.request_type] = int(r.requests or 0)
        request_stacks[r.label]['storage_gb'] += float(r.bytes or 0)/1e9
    top_requesters = sorted(request_stacks.items(), key=lambda kv: kv[1]['storage_gb'], reverse=True)[:10]

    demand_list = [{'date': k, **{kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()}} for k, v in demand.items()]
    dow_list = [{'label': k, **v, 'total': sum(v.values())} for k, v in dow.items()]
    hourly_list = [{'label': k, **v, 'total': sum(v.values())} for k, v in hourly.items()]
    platform_list = [{'label': k, **v, 'total': sum(v.values())} for k, v in top_platforms]
    user_list = [{'label': k, **v, 'total': sum(v.values())} for k, v in top_users]
    decision_list = [{'label': r.label or 'unknown', 'plays': int(r.plays or 0), 'tb': round(float(r.bytes or 0)/1e12, 3)} for r in decision_rows]
    requester_list = [{'label': k, **{kk: round(vv, 1) if isinstance(vv, float) else vv for kk, vv in v.items()}, 'requests': int(v.get('movie', 0) + v.get('tv', 0))} for k, v in top_requesters]
    maxes = {
        'demand_plays': max([r['plays'] for r in demand_list] or [1]) or 1,
        'demand_gb': max([r['gb'] for r in demand_list] or [1]) or 1,
        'dow': max([r['total'] for r in dow_list] or [1]) or 1,
        'hourly': max([r['total'] for r in hourly_list] or [1]) or 1,
        'platforms': max([r['total'] for r in platform_list] or [1]) or 1,
        'users': max([r['total'] for r in user_list] or [1]) or 1,
        'decisions': max([r['plays'] for r in decision_list] or [1]) or 1,
        'requesters': max([r['storage_gb'] for r in requester_list] or [1]) or 1,
    }
    return {
        'scope': username or 'all users',
        'period_days': days,
        'period_label': period_label,
        'bucket_label': 'Daily' if bucket_kind == 'day' else 'Weekly',
        'kpis': {
            'plays': int(plays or 0), 'hours': round(float(seconds or 0)/3600, 1),
            'tb': round(float(bytes_ or 0)/1e12, 2), 'transcodes': int(transcodes or 0),
            'avg_mbps': round(float(avg_kbps or 0)/1000, 1), 'peak_mbps': round(float(peak_kbps or 0)/1000, 1),
        },
        'demand': demand_list, 'dow': dow_list, 'hourly': hourly_list,
        'platforms': platform_list, 'users': user_list, 'decisions': decision_list,
        'requesters': requester_list, 'maxes': maxes,
    }


async def service_clients():
    cfg = all_settings()
    return (
        RadarrClient(ServiceConfig(url=cfg['radarr_url'], api_key=cfg['radarr_api_key'])) if cfg.get('radarr_url') and cfg.get('radarr_api_key') else None,
        SonarrClient(ServiceConfig(url=cfg['sonarr_url'], api_key=cfg['sonarr_api_key'])) if cfg.get('sonarr_url') and cfg.get('sonarr_api_key') else None,
    )


def _arr_instance_configs(kind: str) -> list[dict]:
    values = all_settings()
    instances = service_context(values).get(kind, [])
    return [i for i in instances if i.get('url') and i.get('api_key')]


def _arr_clients(kind: str):
    cls = RadarrClient if kind == 'radarr' else SonarrClient
    clients = []
    for idx, inst in enumerate(_arr_instance_configs(kind)):
        clients.append({
            'index': idx,
            'name': inst.get('name') or ('Radarr' if kind == 'radarr' else 'Sonarr'),
            'client': cls(ServiceConfig(url=inst.get('url', ''), api_key=inst.get('api_key', ''))),
        })
    return clients


def format_bytes(value):
    try:
        n = float(value or 0)
    except Exception:
        n = 0
    units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
    idx = 0
    while n >= 1000 and idx < len(units) - 1:
        n /= 1000
        idx += 1
    if idx == 0:
        return f'{int(n)} {units[idx]}'
    return f'{n:.1f} {units[idx]}' if n < 10 else f'{n:.0f} {units[idx]}'

templates.env.filters['bytes'] = format_bytes


def api_user_payload(context: AuthContext) -> dict:
    user = context.user
    return {
        'username': context.username,
        'is_admin': bool(context.is_admin),
        'display_name': user.display_name if user else context.username,
        'email': user.email if user else None,
        'thumb_url': user.thumb_url if user else None,
        'method': context.method,
    }


def require_api_write(context: AuthContext, scope: str) -> None:
    if not context.has_scope(scope):
        raise HTTPException(status_code=403, detail=f'Scope required: {scope}')


def iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def plex_session_payload(row: PlexSession) -> dict:
    return {
        'id': row.id, 'source': row.source, 'source_id': row.source_id,
        'username': row.username, 'title': media_display_title(row),
        'raw_title': row.title, 'media_type': row.media_type,
        'started_at': iso(row.started_at), 'stopped_at': iso(row.stopped_at),
        'watched_seconds': row.watched_seconds,
        'bytes_streamed': row.bytes_streamed,
        'transcode_decision': row.transcode_decision,
        'player': row.player, 'platform': row.platform,
        'ip_address': row.ip_address, 'reach': row.reach,
        'machine_id': row.machine_id, 'thumb_path': row.thumb_path,
    }


def request_payload(row: MediaRequest) -> dict:
    return {
        'id': row.id, 'source': row.source, 'source_id': row.source_id,
        'requester_plex_id': row.requester_plex_id,
        'requester_name': row.requester_name,
        'request_type': row.request_type,
        'title': row.title, 'seasons': row.seasons,
        'status': _status_label(row.status),
        'raw_status': row.status,
        'requested_at': iso(row.requested_at),
        'fulfilled_bytes': row.fulfilled_bytes,
    }


def active_session_payload(row: ActivePlexSession) -> dict:
    return {
        'session_key': row.session_key,
        'session_id': row.session_id,
        'user': row.username,
        'user_id': row.plex_user_id,
        'title': row.content_title or row.title,
        'display_title': row.title,
        'grandparent_title': row.grandparent_title,
        'parent_title': row.parent_title,
        'media_index': row.media_index,
        'parent_media_index': row.parent_media_index,
        'type': row.media_type,
        'library': row.library,
        'thumb': row.thumb_path,
        'view_offset': row.last_view_offset_ms or 0,
        'duration': row.duration_ms,
        'player': row.player,
        'player_address': row.player_address,
        'remote_public_address': row.remote_public_address,
        'device': row.device,
        'machine_identifier': row.machine_id,
        'platform': row.platform,
        'platform_version': row.platform_version,
        'product': row.product,
        'version': row.version,
        'state': row.state,
        'local': row.local,
        'secure': row.secure,
        'relayed': row.relayed,
        'bandwidth': int(row.bandwidth_kbps or 0),
        'container': row.container,
        'resolution': row.resolution,
        'video_codec': row.video_codec,
        'audio_codec': row.audio_codec,
        'file': row.file,
        'file_size': row.file_size,
        'part_decision': row.part_decision,
        'audio_stream_title': row.audio_stream_title,
        'transcode_decision': row.transcode_decision,
        'started_at': iso(row.started_at),
        'last_seen_at': iso(row.last_seen_at),
    }


def active_live_rows(db: Session) -> list[ActivePlexSession]:
    return db.scalars(select(ActivePlexSession).order_by(ActivePlexSession.last_seen_at.desc())).all()


def live_stats_from_payloads(sessions: list[dict]) -> dict:
    active = [s for s in sessions if (s.get('state') or '').lower() != 'paused']
    total_kbps = sum(int(s.get('bandwidth') or 0) for s in active)
    return {
        'total_kbps': total_kbps,
        'total_mbps': total_kbps / 1000,
        'sessions': len(sessions),
        'active_sessions': len(active),
        'paused_sessions': len(sessions) - len(active),
        'transcodes': sum(1 for s in active if s.get('transcode_decision') == 'transcode'),
        'bars': [18, 34, 22, 48, 30, 58, 42, 66, 38, 72, 52, 84, 46, 62, 36, 54, 28, 44],
    }


async def enriched_active_payloads(db: Session) -> list[dict]:
    enriched = []
    for row in active_live_rows(db):
        payload = active_session_payload(row)
        address = payload.get('remote_public_address') or payload.get('player_address') or ''
        enrich = await lookup_isp(address) if address else {'isp': None, 'org': None, 'as': None}
        enriched.append({**payload, 'ptr': reverse_dns(address) if address else None, **enrich})
    return enriched


def movie_size(row: dict) -> int:
    return int(row.get('sizeOnDisk') or 0)


def series_size(row: dict) -> int:
    stats = row.get('statistics') or {}
    return int(stats.get('sizeOnDisk') or row.get('sizeOnDisk') or 0)


def normalise_movie(row: dict, source: str, index: int) -> dict:
    size = movie_size(row)
    return {
        'kind': 'movie', 'id': row.get('id'), 'title': row.get('title') or 'Untitled movie',
        'year': row.get('year'), 'source': source, 'source_index': index,
        'path': row.get('path'), 'size': size, 'size_label': format_bytes(size),
        'available': bool(row.get('hasFile')), 'monitored': bool(row.get('monitored')),
        'quality': ((((row.get('movieFile') or {}).get('quality') or {}).get('quality') or {}).get('name')),
        'poster': next((img.get('remoteUrl') or img.get('url') for img in (row.get('images') or []) if img.get('coverType') == 'poster'), None),
    }


def normalise_series(row: dict, source: str, index: int) -> dict:
    stats = row.get('statistics') or {}
    size = series_size(row)
    episodes = stats.get('episodeFileCount') or 0
    total = stats.get('totalEpisodeCount') or 0
    return {
        'kind': 'series', 'id': row.get('id'), 'title': row.get('title') or 'Untitled series',
        'year': row.get('year'), 'source': source, 'source_index': index,
        'path': row.get('path'), 'size': size, 'size_label': format_bytes(size),
        'available': episodes, 'monitored': bool(row.get('monitored')),
        'quality': f'{episodes}/{total} episodes' if total else f'{episodes} episodes',
        'seasons': stats.get('seasonCount'),
        'poster': next((img.get('remoteUrl') or img.get('url') for img in (row.get('images') or []) if img.get('coverType') == 'poster'), None),
    }



def relative_time(value: datetime | None) -> str:
    if not value:
        return 'Never watched'
    delta = datetime.utcnow() - value.replace(tzinfo=None)
    if delta.days >= 365:
        years = max(1, delta.days // 365)
        return f'{years}y ago'
    if delta.days >= 30:
        months = max(1, delta.days // 30)
        return f'{months}mo ago'
    if delta.days >= 1:
        return f'{delta.days}d ago'
    hours = max(1, delta.seconds // 3600)
    return f'{hours}h ago'


def _inv_watch_key(item: dict) -> tuple[str, str]:
    title = (item.get('title') or '').strip().lower()
    return ('show' if item.get('kind') == 'series' else 'movie', title)


def enrich_inventory_usage(db: Session, inventory: list[dict]) -> None:
    if not inventory:
        return
    wanted = {_inv_watch_key(item) for item in inventory if item.get('title')}
    if not wanted:
        return
    rows = db.scalars(select(PlexSession).where(PlexSession.title.is_not(None)).order_by(PlexSession.started_at.desc()).limit(60000)).all()
    usage: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (session_media_kind(row), canonical_media_title(row))
        if key not in wanted:
            continue
        data = usage.setdefault(key, {'plays': 0, 'seconds': 0, 'bytes': 0, 'last': None})
        data['plays'] += 1
        data['seconds'] += int(row.watched_seconds or 0)
        data['bytes'] += int(row.bytes_streamed or 0)
        if not data['last'] or row.started_at > data['last']:
            data['last'] = row.started_at
    for item in inventory:
        data = usage.get(_inv_watch_key(item), {})
        last = data.get('last')
        item['plays'] = int(data.get('plays') or 0)
        item['watch_hours'] = round(float(data.get('seconds') or 0) / 3600, 1)
        item['streamed_bytes'] = int(data.get('bytes') or 0)
        item['last_watched_at'] = last
        item['last_watched_label'] = relative_time(last)
        item['stale_days'] = (datetime.utcnow() - last).days if last else 99999
        item['delete_score'] = (item.get('size') or 0) * (1 if item['plays'] == 0 else min(item['stale_days'], 730) / 730)


def library_item_history_filter(item: dict):
    title = (item.get('title') or '').strip().lower()
    if not title:
        return PlexSession.id == -1
    title_col = func.lower(PlexSession.title)
    grandparent_col = func.lower(PlexSession.grandparent_title)
    content_col = func.lower(PlexSession.content_title)
    if item.get('kind') == 'series':
        return or_(
            grandparent_col == title,
            title_col == title,
            title_col.like(f'{title} - %'),
        )
    return or_(
        title_col == title,
        content_col == title,
    )


def selected_library_item(inventory: list[dict], q: str, kind: str = 'all', source: str = 'all') -> dict | None:
    query = (q or '').strip().lower()
    if not query:
        return None
    matches = [i for i in inventory if (i.get('title') or '').strip().lower() == query]
    if kind in {'movie', 'series'}:
        matches = [i for i in matches if i.get('kind') == kind]
    if source and source != 'all':
        matches = [i for i in matches if i.get('source') == source]
    return matches[0] if len(matches) == 1 else None


def library_item_detail(db: Session, item: dict) -> dict:
    watch_filter = library_item_history_filter(item)
    rows = db.scalars(select(PlexSession).where(watch_filter).order_by(PlexSession.started_at.desc()).limit(120)).all()
    plays, seconds, streamed, transcodes, remote_plays, users, devices = db.execute(select(
        func.count(PlexSession.id),
        func.coalesce(func.sum(PlexSession.watched_seconds), 0),
        func.coalesce(func.sum(PlexSession.bytes_streamed), 0),
        func.coalesce(func.sum(case((PlexSession.transcode_decision == 'transcode', 1), else_=0)), 0),
        func.coalesce(func.sum(case((PlexSession.reach == 'remote', 1), else_=0)), 0),
        func.count(func.distinct(PlexSession.username)),
        func.count(func.distinct(PlexSession.machine_id)),
    ).where(watch_filter)).one()
    user_rows = db.execute(select(
        PlexSession.username,
        func.count(PlexSession.id).label('plays'),
        func.coalesce(func.sum(PlexSession.watched_seconds), 0).label('seconds'),
        func.max(PlexSession.started_at).label('last'),
    ).where(watch_filter).group_by(PlexSession.username).order_by(func.sum(PlexSession.watched_seconds).desc()).limit(8)).all()
    device_label = func.coalesce(PlexSession.player, PlexSession.platform, PlexSession.machine_id, 'Unknown')
    device_rows = db.execute(select(
        device_label.label('device'),
        func.count(PlexSession.id).label('plays'),
        func.max(PlexSession.started_at).label('last'),
    ).where(watch_filter).group_by(device_label).order_by(func.count(PlexSession.id).desc()).limit(8)).all()
    episode_rows = db.execute(select(
        func.coalesce(PlexSession.content_title, PlexSession.title, 'Unknown').label('title'),
        PlexSession.parent_media_index,
        PlexSession.media_index,
        func.count(PlexSession.id).label('plays'),
        func.coalesce(func.sum(PlexSession.watched_seconds), 0).label('seconds'),
        func.max(PlexSession.started_at).label('last'),
    ).where(watch_filter).group_by(PlexSession.content_title, PlexSession.title, PlexSession.parent_media_index, PlexSession.media_index).order_by(func.max(PlexSession.started_at).desc()).limit(12)).all()
    bucket = func.date_trunc('week', PlexSession.started_at)
    weekly_rows = db.execute(select(bucket.label('week'), func.sum(PlexSession.watched_seconds).label('seconds')).where(watch_filter).group_by(bucket).order_by(bucket)).all()
    return {
        'item': item,
        'history_rows': [{'row': r, 'display_title': media_display_title(r)} for r in rows[:40]],
        'recent_rows': rows[:8],
        'user_rows': user_rows,
        'device_rows': device_rows,
        'episode_rows': episode_rows,
        'weekly_chart': weekly_chart_payload(weekly_rows) if weekly_rows else {'points': [], 'max': 1, 'half': 0.5, 'weeks': 26},
        'plays': int(plays or 0),
        'watch_hours': round(float(seconds or 0) / 3600, 1),
        'streamed_bytes': int(streamed or 0),
        'transcodes': int(transcodes or 0),
        'remote_plays': int(remote_plays or 0),
        'users': int(users or 0),
        'devices': int(devices or 0),
        'first_watched': min((r.started_at for r in rows), default=None),
        'last_watched': max((r.started_at for r in rows), default=None),
    }

def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).replace(tzinfo=None)
    except Exception:
        return None


def _status_label(value):
    return STATUS_LABEL.get(str(value), str(value or 'Unknown'))


def request_redirect(message: str, kind: str = 'ok') -> RedirectResponse:
    return RedirectResponse(f"/requests?{urlencode({'notice': message, 'notice_kind': kind})}", status_code=303)


def _request_key(row):
    return row.source_id or f'{row.source}-{row.id}'


def _request_quality(row):
    placeholder = row.title.startswith('movie #') or row.title.startswith('tv #') or row.title.startswith('media #') or row.title == 'Unknown'
    return (0 if placeholder else 10) + (2 if row.fulfilled_bytes else 0) + (1 if row.status in {'available', 'Available'} else 0)


def consolidated_requests(db: Session, limit: int = 200):
    rows = db.scalars(select(MediaRequest).order_by(MediaRequest.requested_at.desc()).limit(limit * 3)).all()
    best = {}
    for row in rows:
        key = _request_key(row)
        if key not in best or _request_quality(row) > _request_quality(best[key]):
            best[key] = row
    return sorted(best.values(), key=lambda r: r.requested_at, reverse=True)[:limit]


async def arr_maps(radarr: RadarrClient | None, sonarr: SonarrClient | None):
    movie_map = {}
    series_map = {}
    if radarr:
        try:
            for m in await radarr.movies():
                title = m.get('title')
                if title:
                    movie_map[title.lower()] = m
        except Exception:
            pass
    if sonarr:
        try:
            for row in await sonarr.series():
                title = row.get('title')
                if title:
                    series_map[title.lower()] = row
        except Exception:
            pass
    return movie_map, series_map


def release_for_request(row: MediaRequest, movie_map: dict, series_map: dict):
    value = release_value_for_request(row, movie_map, series_map)
    return format_server_time(value)


def release_value_for_request(row: MediaRequest, movie_map: dict, series_map: dict):
    if row.request_type == 'movie':
        m = movie_map.get((row.title or '').lower())
        if not m:
            return None
        return m.get('digitalRelease') or m.get('physicalRelease') or m.get('inCinemas') or m.get('year')
    s = series_map.get((row.title or '').lower())
    if not s:
        return None
    return s.get('nextAiring') or s.get('previousAiring') or s.get('firstAired') or s.get('year')


def request_display_title(row: MediaRequest, movie_map: dict, series_map: dict):
    title = row.title or 'Unknown'
    match = movie_map.get(title.lower()) if row.request_type == 'movie' else series_map.get(title.lower())
    year = match.get('year') if match else None
    if year and str(year) not in title:
        return f'{title} ({year})'
    return title


def release_eta_for_request(row: MediaRequest, movie_map: dict, series_map: dict, status_slug: str):
    if status_slug == 'available':
        return 'Completed'
    value = release_value_for_request(row, movie_map, series_map)
    release_date = _parse_date(str(value)) if value and not (isinstance(value, int) or str(value).isdigit()) else None
    if not release_date:
        return None
    today = datetime.now(SERVER_TZ).date()
    delta = (release_date.date() - today).days
    if delta == 0:
        return 'Today'
    if delta == 1:
        return 'Tomorrow'
    if delta > 1:
        return f'{delta}d'
    return 'Overdue'


def normalise_queue_item(source: str, item: dict):
    movie = item.get('movie') or {}
    series = item.get('series') or {}
    episode = item.get('episode') or {}
    title = movie.get('title') or series.get('title') or item.get('title') or item.get('downloadTitle') or 'Unknown'
    if episode.get('title'):
        title = f"{title} — {episode.get('title')}"
    size = item.get('size') or item.get('sizeleft') or 0
    sizeleft = item.get('sizeleft') or 0
    progress = round(100 - ((float(sizeleft) / float(size)) * 100), 1) if size else None
    messages = item.get('statusMessages') or []
    message = '; '.join(
        m for m in (
            msg.get('message') or msg.get('title') or ''
            for msg in messages if isinstance(msg, dict)
        ) if m
    )
    item_id = item.get('id') or item.get('downloadId') or item.get('trackedDownloadState') or title
    return {
        'item_key': f"{source}:{item_id}:{title}"[:240],
        'source': source,
        'title': title,
        'status': item.get('status') or item.get('trackedDownloadStatus') or 'unknown',
        'quality': ((item.get('quality') or {}).get('quality') or {}).get('name'),
        'protocol': item.get('protocol'),
        'indexer': item.get('indexer'),
        'timeleft': item.get('timeleft'),
        'size_bytes': int(size or 0) if size else None,
        'size_left_bytes': int(sizeleft or 0) if sizeleft else None,
        'size_gb': round(float(size or 0)/1e9, 2) if size else None,
        'progress': progress,
        'tracked_download_status': item.get('trackedDownloadStatus'),
        'tracked_download_state': item.get('trackedDownloadState'),
        'message': message or item.get('errorMessage'),
        'download_id': item.get('downloadId'),
    }


def reconcile_download_queue(db: Session, queue: list[dict]) -> tuple[int, int]:
    now = datetime.utcnow()
    seen = set()
    for item in queue:
        key = str(item.get('item_key') or f"{item.get('source')}:{item.get('title')}")
        seen.add(key)
        row = db.get(ActiveDownloadItem, key)
        values = {
            'source': item.get('source') or 'Unknown',
            'title': item.get('title') or 'Unknown',
            'status': item.get('status'),
            'quality': item.get('quality'),
            'protocol': item.get('protocol'),
            'indexer': item.get('indexer'),
            'timeleft': item.get('timeleft'),
            'size_bytes': item.get('size_bytes'),
            'size_left_bytes': item.get('size_left_bytes'),
            'progress': item.get('progress'),
            'tracked_download_status': item.get('tracked_download_status'),
            'tracked_download_state': item.get('tracked_download_state'),
            'message': item.get('message'),
            'download_id': item.get('download_id'),
            'last_seen_at': now,
        }
        if not row:
            db.add(ActiveDownloadItem(item_key=key, **values))
        else:
            for attr, value in values.items():
                setattr(row, attr, value)
    stale = db.scalars(select(ActiveDownloadItem)).all()
    removed = 0
    for row in stale:
        if row.item_key not in seen:
            db.delete(row)
            removed += 1
    db.commit()
    return len(seen), removed


def download_item_payload(row: ActiveDownloadItem) -> dict:
    size = int(row.size_bytes or 0)
    return {
        'item_key': row.item_key,
        'source': row.source,
        'title': row.title,
        'status': row.status or row.tracked_download_status or 'unknown',
        'quality': row.quality,
        'protocol': row.protocol,
        'indexer': row.indexer,
        'timeleft': row.timeleft,
        'size_bytes': size or None,
        'size_gb': round(size / 1e9, 2) if size else None,
        'size_left_bytes': row.size_left_bytes,
        'progress': row.progress,
        'tracked_download_status': row.tracked_download_status,
        'tracked_download_state': row.tracked_download_state,
        'message': row.message,
        'download_id': row.download_id,
        'last_seen_at': iso(row.last_seen_at),
    }


def active_download_rows(db: Session) -> list[ActiveDownloadItem]:
    return db.scalars(select(ActiveDownloadItem).order_by(ActiveDownloadItem.last_seen_at.desc(), ActiveDownloadItem.title)).all()


def active_download_payloads(db: Session) -> list[dict]:
    return [download_item_payload(row) for row in active_download_rows(db)]


def ops_stats(downloads: list[dict]) -> dict:
    active = [d for d in downloads if (d.get('status') or '').lower() not in {'completed', 'complete'}]
    processing = [d for d in downloads if 'process' in ((d.get('status') or '') + ' ' + (d.get('tracked_download_state') or '')).lower()]
    background_transcodes = [d for d in downloads if (d.get('source') or '').lower() == 'plex transcode']
    size_left = sum(int(d.get('size_left_bytes') or 0) for d in active)
    total_size = sum(int(d.get('size_bytes') or 0) for d in active)
    return {
        'downloads': len(downloads),
        'active_downloads': len(active),
        'processing': len(processing),
        'background_transcodes': len(background_transcodes),
        'remaining_bytes': size_left,
        'remaining_label': format_bytes(size_left) if size_left else '0 B',
        'total_label': format_bytes(total_size) if total_size else '0 B',
    }



def _compact_plex_servers(resources: list[dict]) -> list[dict]:
    servers = []
    for r in resources:
        if not (r.get('provides') and 'server' in r.get('provides', '')):
            continue
        conns = r.get('connections') or []
        best = choose_connection(r) or {}
        connections = [{'uri': c.get('uri'), 'local': bool(c.get('local')), 'protocol': c.get('protocol')} for c in conns if c.get('uri')]
        connections.sort(key=lambda c: (not c.get('local'), c.get('protocol') != 'https', c.get('uri') or ''))
        servers.append({
            'name': r.get('name') or r.get('product') or 'Plex server',
            'clientIdentifier': r.get('clientIdentifier') or '',
            'owned': bool(r.get('owned')),
            'platform': r.get('platform') or '',
            'productVersion': r.get('productVersion') or '',
            'uri': best.get('uri') or (connections[0].get('uri') if connections else ''),
            'connections': connections,
        })
    return servers




async def _finish_plex_auth(request: Request, db: Session, token: str) -> dict:
    ident = await fetch_identity(token)
    plex_id = str(ident['id'])
    values = all_settings()
    first_admin = not values.get('plex_owner_id')
    auth_flow = request.session.get('auth_flow') or ('setup' if first_admin else 'login')
    user = db.scalar(select(User).where(User.plex_id == plex_id))
    if not user:
        user = User(
            plex_id=plex_id,
            username=ident.get('username') or ident.get('title') or ident.get('email') or plex_id,
            email=ident.get('email'),
            display_name=ident.get('friendlyName') or ident.get('title'),
            friendly_name=ident.get('friendlyName'),
            thumb_url=ident.get('thumb'),
            plex_uuid=ident.get('uuid'),
            plex_title=ident.get('title'),
            plex_source='plex-auth',
            is_admin=first_admin or plex_id == values.get('plex_owner_id'),
        )
        db.add(user)
    user.last_seen_at = datetime.utcnow()
    user.email = ident.get('email') or user.email
    user.display_name = ident.get('friendlyName') or ident.get('title') or user.display_name
    user.friendly_name = ident.get('friendlyName') or user.friendly_name
    user.thumb_url = ident.get('thumb') or user.thumb_url
    user.plex_uuid = ident.get('uuid') or user.plex_uuid
    if first_admin or plex_id == values.get('plex_owner_id'):
        user.is_admin = True
    if first_admin:
        set_settings({'plex_owner_id': plex_id})
    db.commit()

    request.session.clear()
    request.session['plex_id'] = plex_id
    if values.get('plex_server_token') and auth_flow != 'setup':
        return {'status': 'signed_in', 'redirect': '/admin' if user.is_admin else f'/users/{user.username}'}

    resources = await fetch_resources(token)
    servers = _compact_plex_servers(resources)
    owned_servers = [s for s in servers if s.get('owned')]
    setup_id = secrets.token_urlsafe(18)
    PENDING_PLEX_AUTHS[setup_id] = {'token': token, 'servers': owned_servers, 'owner': plex_id}
    request.session['setup_auth_id'] = setup_id
    return {'status': 'server_select', 'redirect': '/setup/plex-server', 'server_count': len(owned_servers)}


async def _poll_plex_pin(request: Request, db: Session) -> dict:
    pin_id = request.session.get('pin_id')
    if not pin_id:
        return {'status': 'missing', 'redirect': '/' if configured() else '/setup'}
    try:
        pin = await fetch_pin(pin_id)
    except Exception:
        request.session.pop('pin_id', None)
        request.session.pop('pin_code', None)
        request.session.pop('pin_created_at', None)
        return {'status': 'expired', 'redirect': '/' if configured() else '/setup'}
    token = pin.get('authToken')
    if not token:
        return {'status': 'waiting'}
    return await _finish_plex_auth(request, db, token)

def _decode_instances(raw: str, fallback_name: str, fallback_url: str, fallback_key: str) -> list[dict]:
    try:
        items = json.loads(raw or '[]')
    except Exception:
        items = []
    if not items and (fallback_url or fallback_key):
        items = [{'name': fallback_name, 'url': fallback_url, 'api_key': fallback_key}]
    return [i for i in items if i.get('url') or i.get('api_key') or i.get('name')]


def service_context(values: dict) -> dict:
    return {
        'radarr': _decode_instances(values.get('radarr_instances', '[]'), 'Radarr', values.get('radarr_url', ''), values.get('radarr_api_key', '')),
        'sonarr': _decode_instances(values.get('sonarr_instances', '[]'), 'Sonarr', values.get('sonarr_url', ''), values.get('sonarr_api_key', '')),
    }


async def _settings_from_form(request: Request) -> dict:
    form = await request.form()
    def first(name, default=''):
        return str(form.get(name, default) or '')
    radarr = []
    for name, url, key in zip(form.getlist('radarr_name'), form.getlist('radarr_url_multi'), form.getlist('radarr_api_key_multi')):
        if str(name or url or key).strip():
            radarr.append({'name': str(name or 'Radarr'), 'url': str(url or ''), 'api_key': str(key or '')})
    sonarr = []
    for name, url, key in zip(form.getlist('sonarr_name'), form.getlist('sonarr_url_multi'), form.getlist('sonarr_api_key_multi')):
        if str(name or url or key).strip():
            sonarr.append({'name': str(name or 'Sonarr'), 'url': str(url or ''), 'api_key': str(key or '')})
    values = {
        'plex_server_url': first('plex_server_url'), 'plex_server_token': first('plex_server_token'),
        'media_server_type': first('media_server_type', 'plex'),
        'plex_owner_id': first('plex_owner_id'), 'plex_machine_id': first('plex_machine_id'),
        'plex_server_name': first('plex_server_name'),
        'seerr_url': first('seerr_url'), 'seerr_api_key': first('seerr_api_key'),
        'tautulli_url': first('tautulli_url'), 'tautulli_api_key': first('tautulli_api_key'),
        'sabnzbd_url': first('sabnzbd_url'), 'sabnzbd_api_key': first('sabnzbd_api_key'),
        'radarr_url': radarr[0]['url'] if radarr else first('radarr_url'),
        'radarr_api_key': radarr[0]['api_key'] if radarr else first('radarr_api_key'),
        'sonarr_url': sonarr[0]['url'] if sonarr else first('sonarr_url'),
        'sonarr_api_key': sonarr[0]['api_key'] if sonarr else first('sonarr_api_key'),
        'radarr_instances': json.dumps(radarr),
        'sonarr_instances': json.dumps(sonarr),
        'job_plex_live_seconds': first('job_plex_live_seconds', '30'),
        'job_plex_accounts_minutes': first('job_plex_accounts_minutes', '60'),
        'job_requests_minutes': first('job_requests_minutes', str(settings.sync_interval_minutes)),
        'homeassistant_webhook_url': first('homeassistant_webhook_url'),
        'homeassistant_webhook_token': first('homeassistant_webhook_token'),
    }
    if 'local_auth_username' in form:
        values['local_auth_username'] = first('local_auth_username', 'admin') or 'admin'
    password = first('local_auth_password')
    confirm = first('local_auth_password_confirm')
    if password and password == confirm:
        values['local_auth_password_hash'] = hash_password(password)
    return values




def weekly_chart_payload(rows, weeks: int = 26) -> dict:
    today = datetime.utcnow().date()
    start = today - timedelta(weeks=weeks - 1)
    start = start - timedelta(days=start.weekday())
    by_week = {}
    for row in rows:
        week = row.week.date() if hasattr(row.week, 'date') else row.week
        by_week[week] = float(row.seconds or 0) / 3600
    points = []
    for i in range(weeks):
        day = start + timedelta(weeks=i)
        hours = round(by_week.get(day, 0.0), 2)
        points.append({'label': day.strftime('%d %b'), 'hours': hours, 'date': day.isoformat()})
    max_hours = max([p['hours'] for p in points] or [1]) or 1
    # Round the ceiling up so the axis is stable and readable.
    if max_hours < 10:
        ceiling = max(1, round(max_hours + 0.5, 1))
    elif max_hours < 100:
        ceiling = int(((max_hours + 9) // 10) * 10)
    else:
        ceiling = int(((max_hours + 49) // 50) * 50)
    return {'points': points, 'max': ceiling, 'half': ceiling / 2, 'weeks': weeks}



def session_media_kind(row: PlexSession) -> str:
    media_type = (row.media_type or '').lower()
    if media_type in {'movie', 'film'}:
        return 'movie'
    if media_type in {'episode', 'show', 'season', 'tv'}:
        return 'show'
    title = row.title or ''
    return 'show' if ' - ' in title else 'movie'


def canonical_media_title(row: PlexSession) -> str:
    title = (row.title or '').strip()
    grandparent = (row.grandparent_title or '').strip()
    if session_media_kind(row) == 'show' and grandparent:
        return grandparent.lower()
    if not title:
        return f'unknown-{row.id}'
    if session_media_kind(row) == 'show' and ' - ' in title:
        return title.split(' - ', 1)[0].strip().lower()
    return title.lower()


def media_display_title(row: PlexSession) -> str:
    if row.grandparent_title and row.content_title:
        season = f'S{int(row.parent_media_index):02d}' if row.parent_media_index else ''
        episode = f'E{int(row.media_index):02d}' if row.media_index else ''
        code = f'{season}{episode} · ' if season or episode else ''
        return f'{row.grandparent_title} · {code}{row.content_title}'
    return row.title or row.content_title or 'Unknown title'


def overview_library_href(row: PlexSession) -> str:
    kind = 'series' if session_media_kind(row) == 'show' else 'movie'
    title = (row.grandparent_title if kind == 'series' else row.title) or row.title or row.content_title or ''
    return f"/libraries?{urlencode({'q': title, 'kind': kind})}" if title else '/libraries'


def diverse_media_rows(rows: list[PlexSession], limit: int = 10, *, bucket: str | None = None, max_per_user: int = 2) -> list[PlexSession]:
    """Pick recent media that represents the library, not only the noisiest watcher.

    First pass is strict: one card per canonical title and at most max_per_user cards per user.
    Second pass relaxes user spread a little so the rail still fills on quiet days.
    """
    selected: list[PlexSession] = []
    seen_titles: set[str] = set()
    per_user: dict[str, int] = {}

    def consider(row: PlexSession, user_cap: int) -> bool:
        if bucket and session_media_kind(row) != bucket:
            return False
        key = f'{session_media_kind(row)}:{canonical_media_title(row)}'
        user_key = (row.username or 'unknown').lower()
        if key in seen_titles or per_user.get(user_key, 0) >= user_cap:
            return False
        selected.append(row)
        seen_titles.add(key)
        per_user[user_key] = per_user.get(user_key, 0) + 1
        return len(selected) >= limit

    for row in rows:
        if consider(row, max_per_user):
            return selected
    for row in rows:
        if consider(row, max(max_per_user + 1, 4)):
            return selected
    return selected

def selected_overview_period(period_key: str | None) -> dict:
    return OVERVIEW_PERIOD_BY_KEY.get(period_key or 'all', OVERVIEW_PERIOD_BY_KEY['all'])


def overview_period_since(period: dict) -> datetime | None:
    days = period.get('days')
    return datetime.utcnow() - timedelta(days=int(days)) if days else None


def with_since(stmt, column, since: datetime | None):
    return stmt.where(column >= since) if since else stmt


def summary_query(db: Session, username: str | None = None, since: datetime | None = None):
    q = select(
        func.count(PlexSession.id),
        func.coalesce(func.sum(PlexSession.watched_seconds), 0),
        func.coalesce(func.sum(PlexSession.bytes_streamed), 0),
        func.coalesce(func.sum(case((PlexSession.transcode_decision == 'transcode', 1), else_=0)), 0),
    )
    q = with_since(q, PlexSession.started_at, since)
    if username:
        q = q.where(func.lower(PlexSession.username) == username.lower())
    return db.execute(q).one()


@app.get('/', response_class=HTMLResponse)
async def home(request: Request, db: Session = Depends(get_db)):
    if not configured():
        return RedirectResponse('/setup/services' if settings.setup_no_auth else '/setup', status_code=302)
    user = current_user(request, db)
    if not user:
        plex_auth_url_value = None
        if configured():
            created_raw = request.session.get('pin_created_at')
            expired = True
            if created_raw:
                try:
                    expired = datetime.utcnow() - datetime.fromisoformat(created_raw) > timedelta(minutes=8)
                except Exception:
                    expired = True
            if not request.session.get('pin_id') or expired or request.session.get('auth_flow') != 'login':
                request.session.clear()
                request.session['auth_flow'] = 'login'
                try:
                    pin = await create_pin()
                    request.session['pin_id'] = pin.id
                    request.session['pin_code'] = pin.code
                    request.session['pin_created_at'] = datetime.utcnow().isoformat()
                except Exception:
                    request.session.pop('pin_id', None)
                    request.session.pop('pin_code', None)
                    request.session.pop('pin_created_at', None)
            code = request.session.get('pin_code')
            if code:
                plex_auth_url_value = plex_auth_url(code, public_url('/auth/complete'))
        values = all_settings()
        return templates.TemplateResponse('login.html', {
            'request': request,
            'configured': configured(),
            'plex_auth_url': plex_auth_url_value,
            'local_auth_configured': local_auth_configured(values),
            'local_auth_username': values.get('local_auth_username') or '',
            'local_error': request.query_params.get('local_error') or '',
        })
    if user.is_admin:
        return RedirectResponse('/admin', status_code=302)
    return RedirectResponse('/me', status_code=302)


@app.get('/auth/start')
async def auth_start(request: Request):
    request.session.clear()
    request.session['auth_flow'] = 'login'
    try:
        pin = await create_pin()
    except Exception:
        return RedirectResponse('/', status_code=303)
    request.session['pin_id'] = pin.id
    request.session['pin_code'] = pin.code
    request.session['pin_created_at'] = datetime.utcnow().isoformat()
    forward = plex_auth_url(pin.code, public_url('/auth/complete'))
    return RedirectResponse(forward, status_code=302)


@app.post('/auth/local')
async def auth_local(request: Request, db: Session = Depends(get_db), username: str = Form(''), password: str = Form('')):
    values = all_settings()
    configured_username = (values.get('local_auth_username') or '').strip()
    password_hash = (values.get('local_auth_password_hash') or '').strip()
    if not configured_username or not password_hash:
        return RedirectResponse('/?local_error=not_configured', status_code=303)
    if username.strip().lower() != configured_username.lower() or not verify_password(password, password_hash):
        return RedirectResponse('/?local_error=bad_credentials', status_code=303)
    user = ensure_local_admin(db, configured_username)
    request.session.clear()
    request.session['plex_id'] = user.plex_id
    return RedirectResponse('/admin', status_code=303)


@app.get('/auth/complete')
async def auth_complete(request: Request, db: Session = Depends(get_db)):
    result = await _poll_plex_pin(request, db)
    if result.get('redirect') and result.get('status') != 'waiting':
        response = templates.TemplateResponse('auth_complete.html', {'request': request, 'redirect': result['redirect']})
        response.headers['Cache-Control'] = 'no-store'
        return response
    code = request.session.get('pin_code')
    plex_auth_url_value = plex_auth_url(code, public_url('/auth/complete')) if code else None
    response = templates.TemplateResponse('waiting.html', {'request': request, 'plex_auth_url': plex_auth_url_value})
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/auth/status')
async def auth_status(request: Request, db: Session = Depends(get_db)):
    result = await _poll_plex_pin(request, db)
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@app.post('/logout')
def logout(request: Request):
    request.session.clear()
    return RedirectResponse('/', status_code=302)


@app.get('/setup', response_class=HTMLResponse)
async def setup_get(request: Request, db: Session = Depends(get_db), reconnect: str = ''):
    values = all_settings()
    current = current_user(request, db)
    if not request.session.get('setup_token'):
        request.session['setup_token'] = secrets.token_urlsafe(32)
    plex_auth_url_value = None
    reconnecting = reconnect in {'1', 'true', 'yes'}
    if values.get('plex_server_token') and not reconnecting:
        return RedirectResponse('/settings' if current and current.is_admin else '/', status_code=302)
    if values.get('plex_server_token') and reconnecting and (not current or not current.is_admin):
        return RedirectResponse('/', status_code=302)
    if reconnecting:
        request.session.pop('pin_id', None)
        request.session.pop('pin_code', None)
        request.session.pop('pin_created_at', None)
    if reconnecting or not values.get('plex_server_token'):
        created_raw = request.session.get('pin_created_at')
        expired = True
        if created_raw:
            try:
                expired = datetime.utcnow() - datetime.fromisoformat(created_raw) > timedelta(minutes=8)
            except Exception:
                expired = True
        if not request.session.get('pin_id') or expired:
            request.session['auth_flow'] = 'setup'
            pin = await create_pin()
            request.session['pin_id'] = pin.id
            request.session['pin_code'] = pin.code
            request.session['pin_created_at'] = datetime.utcnow().isoformat()
        code = request.session.get('pin_code')
        if code:
            plex_auth_url_value = plex_auth_url(code, public_url('/auth/complete'))
    return templates.TemplateResponse('setup.html', {
        'request': request,
        'values': values,
        'user': current,
        'services': service_context(values),
        'plex_auth_url': plex_auth_url_value,
        'reconnecting': reconnecting,
        'local_auth_configured': local_auth_configured(values),
        'local_error': request.query_params.get('local_error') or '',
        'setup_token': request.session.get('setup_token') or '',
    })


def clear_plex_setup_state(request: Request) -> None:
    request.session.pop('pin_id', None)
    request.session.pop('pin_code', None)
    request.session.pop('pin_created_at', None)
    setup_id = request.session.pop('setup_auth_id', None)
    if setup_id:
        PENDING_PLEX_AUTHS.pop(setup_id, None)
    request.session.pop('plex_token_pending', None)
    request.session.pop('plex_servers_pending', None)
    request.session.pop('plex_owner_pending', None)


@app.get('/setup/reset-plex')
def setup_reset_plex_get():
    return RedirectResponse('/setup', status_code=302)


@app.post('/setup/reset-plex')
def setup_reset_plex(request: Request):
    clear_plex_setup_state(request)
    return RedirectResponse('/setup', status_code=302)


@app.post('/setup/local-auth')
async def setup_local_auth(
    request: Request,
    db: Session = Depends(get_db),
    local_auth_username: str = Form('admin'),
    local_auth_password: str = Form(''),
    local_auth_password_confirm: str = Form(''),
):
    values = all_settings()
    form = await request.form()
    current = current_user(request, db)
    if configured() and (not current or not current.is_admin):
        return RedirectResponse('/', status_code=302)
    if local_auth_configured(values) and (not current or not current.is_admin):
        return RedirectResponse('/', status_code=302)
    if not configured() and not local_auth_configured(values) and not setup_token_valid(request, form):
        return Response('Setup session token required', status_code=403)
    username = (local_auth_username or 'admin').strip() or 'admin'
    if not local_auth_password or local_auth_password != local_auth_password_confirm:
        return RedirectResponse('/setup?local_error=1', status_code=303)
    set_settings({'local_auth_username': username, 'local_auth_password_hash': hash_password(local_auth_password)})
    user = ensure_local_admin(db, username)
    request.session['plex_id'] = user.plex_id
    return RedirectResponse('/setup', status_code=303)


@app.get('/setup/plex-server', response_class=HTMLResponse)
def setup_plex_server(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    pending = PENDING_PLEX_AUTHS.get(request.session.get('setup_auth_id') or '')
    servers = [s for s in ((pending or {}).get('servers') or []) if s.get('owned')]
    if not user or not servers:
        return RedirectResponse('/setup', status_code=302)
    return templates.TemplateResponse('setup_plex_server.html', {'request': request, 'user': user, 'servers': servers})


@app.post('/setup/plex-server')
async def setup_plex_server_post(request: Request, db: Session = Depends(get_db), machine_id: str = Form(...), uri: str = Form(...), server_name: str = Form('')):
    user = current_user(request, db)
    setup_id = request.session.get('setup_auth_id') or ''
    pending = PENDING_PLEX_AUTHS.get(setup_id) or {}
    token = pending.get('token') or request.session.get('plex_token_pending')
    owner = pending.get('owner') or request.session.get('plex_owner_pending') or (user.plex_id if user else '')
    if not user or not token:
        return RedirectResponse('/setup', status_code=302)
    if not server_name:
        for s in pending.get('servers') or []:
            if s.get('clientIdentifier') == machine_id:
                server_name = s.get('name') or ''
                break
    set_settings({'media_server_type': 'plex', 'plex_server_url': uri, 'plex_server_token': token, 'plex_owner_id': all_settings().get('plex_owner_id') or owner, 'plex_machine_id': machine_id, 'plex_server_name': server_name})
    user.is_admin = True
    db.commit()
    try:
        await refresh_plex_accounts(db)
    except Exception:
        pass
    setup_id = request.session.pop('setup_auth_id', None)
    if setup_id:
        PENDING_PLEX_AUTHS.pop(setup_id, None)
    request.session.pop('plex_token_pending', None)
    request.session.pop('plex_servers_pending', None)
    request.session.pop('plex_owner_pending', None)
    return RedirectResponse('/setup/services', status_code=303)


@app.get('/setup/services', response_class=HTMLResponse)
def setup_services(request: Request, db: Session = Depends(get_db), test_kind: str = '', test_ok: str = '', test_message: str = '', import_ok: str = '', import_message: str = ''):
    user = current_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse('/setup', status_code=302)
    if not request.session.get('setup_token'):
        request.session['setup_token'] = secrets.token_urlsafe(32)
    values = all_settings()
    if redirect := setup_local_auth_required_redirect(values):
        return redirect
    return templates.TemplateResponse('setup_services.html', settings_template_context(request, values, user, test_kind, test_ok, test_message, import_ok, import_message))


@app.post('/setup')
async def setup_post(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    current = current_user(request, db)
    if configured():
        if not current or not current.is_admin:
            return RedirectResponse('/', status_code=302)
    elif not setup_token_valid(request, form):
        return Response('Setup session token required', status_code=403)
    if redirect := setup_local_auth_required_redirect(all_settings()):
        return redirect
    set_settings(await _settings_from_form(request))
    configure_scheduler()
    message = maybe_schedule_initial_tautulli_sync(db, all_settings())
    target = str(form.get('return_to') or '/setup/services')
    if message:
        target = append_query_preserving_fragment(target, {'import_ok': '1', 'import_message': message})
    else:
        target = safe_internal_redirect(target, '/setup/services')
    return RedirectResponse(target, status_code=303)


@app.get('/settings', response_class=HTMLResponse)
def settings_get(request: Request, db: Session = Depends(get_db), test_kind: str = '', test_ok: str = '', test_message: str = '', import_ok: str = '', import_message: str = ''):
    user = current_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse('/', status_code=302)
    values = all_settings()
    return templates.TemplateResponse('settings.html', settings_template_context(request, values, user, test_kind, test_ok, test_message, import_ok, import_message))


@app.post('/settings')
async def settings_post(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse('/', status_code=302)
    form = await request.form()
    local_password = str(form.get('local_auth_password') or '')
    local_confirm = str(form.get('local_auth_password_confirm') or '')
    if local_password and local_password != local_confirm:
        query = urlencode({'test_kind': 'local auth', 'test_ok': '0', 'test_message': 'Local auth passwords do not match.'})
        return RedirectResponse(f'/settings?{query}', status_code=303)
    set_settings(await _settings_from_form(request))
    configure_scheduler()
    message = maybe_schedule_initial_tautulli_sync(db, all_settings())
    target = str(form.get('return_to') or '/settings')
    if message:
        target = append_query_preserving_fragment(target, {'import_ok': '1', 'import_message': message})
    else:
        target = safe_internal_redirect(target, '/settings')
    return RedirectResponse(target, status_code=303)


@app.post('/settings/test-service')
async def settings_test_service(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    form = await request.form()
    if configured():
        if not user or not user.is_admin:
            return RedirectResponse('/', status_code=302)
    elif not setup_token_valid(request, form):
        return Response('Setup session token required', status_code=403)
    set_settings(await _settings_from_form(request))
    configure_scheduler()
    service_kind_raw = str(form.get('service_kind') or '')
    base_kind, _, raw_index = service_kind_raw.partition(':')
    index = int(raw_index) if raw_index.isdigit() else 0
    return_to = str(form.get('return_to') or '/settings')
    if base_kind in {'radarr', 'sonarr'}:
        urls = form.getlist(f'{base_kind}_url_multi')
        keys = form.getlist(f'{base_kind}_api_key_multi')
        service_url = str(urls[index] if index < len(urls) else '')
        service_api_key = str(keys[index] if index < len(keys) else '')
    elif base_kind == 'plex':
        service_url = str(form.get('plex_server_url') or '')
        service_api_key = str(form.get('plex_server_token') or '')
    else:
        service_url = str(form.get(f'{base_kind}_url') or '')
        service_api_key = str(form.get(f'{base_kind}_api_key') or '')
    ok, message = await test_service(base_kind, service_url, service_api_key, service_api_key)
    params = {'test_kind': service_kind_raw or base_kind, 'test_ok': '1' if ok else '0', 'test_message': message}
    if ok and base_kind == 'tautulli':
        sync_message = maybe_schedule_initial_tautulli_sync(db, all_settings())
        if sync_message:
            params.update({'import_ok': '1', 'import_message': sync_message})
    target = append_query_preserving_fragment(return_to, params)
    return RedirectResponse(target, status_code=303)




@app.post('/settings/import-tautulli')
async def settings_import_tautulli(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    form = await request.form()
    if configured():
        if not user or not user.is_admin:
            return RedirectResponse('/', status_code=302)
    elif not setup_token_valid(request, form):
        return Response('Setup session token required', status_code=403)
    set_settings(await _settings_from_form(request))
    configure_scheduler()
    cfg = all_settings()
    return_to = str(form.get('return_to') or '/settings')
    try:
        result = await import_tautulli_api(
            db,
            cfg.get('tautulli_url', ''),
            cfg.get('tautulli_api_key', ''),
            full=str(form.get('import_mode') or '') == 'full',
        )
        ok = tautulli_import_ok(result)
        message = result.message
    except Exception as exc:
        ok = False
        message = f'Tautulli import failed: {exc.__class__.__name__}'
    target = append_query_preserving_fragment(return_to, {'import_ok': '1' if ok else '0', 'import_message': message})
    return RedirectResponse(target, status_code=303)




@app.post('/settings/test-homeassistant-webhook')
async def settings_test_homeassistant_webhook(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    form = await request.form()
    if configured():
        if not user or not user.is_admin:
            return RedirectResponse('/', status_code=302)
    elif not setup_token_valid(request, form):
        return Response('Setup session token required', status_code=403)
    set_settings(await _settings_from_form(request))
    configure_scheduler()
    from .integrations import homeassistant_status_payload

    delivered = await notify_homeassistant('test', homeassistant_status_payload(db))
    return_to = str(form.get('return_to') or '/settings')
    message = 'Sent test webhook.' if delivered else 'Webhook was not delivered. Check the URL and Home Assistant logs.'
    target = append_query_preserving_fragment(return_to, {'test_kind': 'homeassistant', 'test_ok': '1' if delivered else '0', 'test_message': message})
    return RedirectResponse(target, status_code=303)


@app.post('/settings/enrich-tautulli-bandwidth')
async def settings_enrich_tautulli_bandwidth(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    form = await request.form()
    if configured():
        if not user or not user.is_admin:
            return RedirectResponse('/', status_code=302)
    elif not setup_token_valid(request, form):
        return Response('Setup session token required', status_code=403)
    set_settings(await _settings_from_form(request))
    configure_scheduler()
    cfg = all_settings()
    return_to = str(form.get('return_to') or '/settings')
    try:
        result = await enrich_tautulli_bandwidth(db, cfg.get('tautulli_url', ''), cfg.get('tautulli_api_key', ''))
        ok = bool(result.updated or result.seen == 0)
        message = result.message
    except Exception as exc:
        ok = False
        message = f'Tautulli bandwidth enrichment failed: {exc.__class__.__name__}'
    target = append_query_preserving_fragment(return_to, {'import_ok': '1' if ok else '0', 'import_message': message})
    return RedirectResponse(target, status_code=303)


@app.get('/settings/reconnect-plex')
def reconnect_plex(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse('/', status_code=302)
    return RedirectResponse('/setup?reconnect=1', status_code=302)


@app.get('/admin', response_class=HTMLResponse)
async def admin_page(request: Request, db: Session = Depends(get_db), period: str = 'all'):
    user = current_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse('/', status_code=302)
    overview_period = selected_overview_period(period)
    since = overview_period_since(overview_period)
    sessions, seconds, streamed, transcodes = summary_query(db, since=since)
    leaders_stmt = select(
        PlexSession.username,
        func.count(PlexSession.id).label('sessions'),
        func.coalesce(func.sum(PlexSession.watched_seconds), 0).label('seconds'),
        func.coalesce(func.sum(PlexSession.bytes_streamed), 0).label('bytes'),
    ).group_by(PlexSession.username).order_by(func.sum(PlexSession.watched_seconds).desc()).limit(12)
    leaders = db.execute(with_since(leaders_stmt, PlexSession.started_at, since)).all()
    request_leaders_stmt = select(
        MediaRequest.requester_name,
        func.count(MediaRequest.id).label('requests'),
        func.coalesce(func.sum(MediaRequest.fulfilled_bytes), 0).label('bytes'),
    ).group_by(MediaRequest.requester_name).order_by(func.count(MediaRequest.id).desc()).limit(12)
    request_leaders = db.execute(with_since(request_leaders_stmt, MediaRequest.requested_at, since)).all()
    known_usernames = {name.lower() for name in db.execute(select(User.username)).scalars().all()}
    known_usernames.update(name.lower() for name in db.execute(select(PlexSession.username).group_by(PlexSession.username)).scalars().all())
    request_leaders = [{
        'requester_name': row.requester_name,
        'requests': row.requests,
        'bytes': row.bytes,
        'href': f"/users/{quote(row.requester_name, safe='')}?tab=requests" if row.requester_name and row.requester_name.lower() in known_usernames else None,
    } for row in request_leaders]
    requests_stmt = select(MediaRequest).order_by(MediaRequest.requested_at.desc()).limit(20)
    requests = db.scalars(with_since(requests_stmt, MediaRequest.requested_at, since)).all()
    recent_stmt = (
        select(PlexSession)
        .where(PlexSession.thumb_path.is_not(None))
        .order_by(PlexSession.started_at.desc())
        .limit(240)
    )
    recent_rows = db.scalars(with_since(recent_stmt, PlexSession.started_at, since)).all()
    spotlight_sessions = diverse_media_rows(recent_rows, 8, max_per_user=1)
    unique_shows = diverse_media_rows(recent_rows, 10, bucket='show', max_per_user=2)
    unique_movies = diverse_media_rows(recent_rows, 10, bucket='movie', max_per_user=2)
    for row in set(spotlight_sessions + unique_shows + unique_movies):
        row.cached_thumb = await ensure_art_cached(row.thumb_path) if row.thumb_path else None
        row.overview_href = overview_library_href(row)
    return templates.TemplateResponse('admin.html', {
        'request': request, 'user': user, 'hours': int(seconds or 0)/3600, 'sessions': sessions,
        'terabytes': int(streamed or 0)/1e12, 'transcodes': transcodes, 'leaders': leaders,
        'request_leaders': request_leaders, 'requests': requests, 'spotlight_sessions': spotlight_sessions,
        'unique_shows': unique_shows, 'unique_movies': unique_movies,
        'overview_periods': OVERVIEW_PERIODS, 'overview_period': overview_period,
    })


@app.get('/me', response_class=HTMLResponse)
def me_page(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse('/', status_code=302)
    return RedirectResponse(f'/users/{user.username}', status_code=302)


@app.get('/api/me')
def me_api(context: AuthContext = Depends(require_auth)):
    return {'user': api_user_payload(context)}


@app.get('/api/live-summary')
async def live_summary_api(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return JSONResponse({'sessions': [], 'downloads': []})
    sessions = [active_session_payload(row) for row in active_live_rows(db)]
    downloads = active_download_payloads(db)
    active = [s for s in sessions if (s.get('state') or '').lower() != 'paused']
    active_kbps = sum(int(s.get('bandwidth') or 0) for s in active)
    return JSONResponse({
        'sessions': [{
            'session_key': s.get('session_key') or s.get('session_id'),
            'user': s.get('user'),
            'title': s.get('grandparent_title') or s.get('title'),
            'subtitle': s.get('title') if s.get('grandparent_title') else s.get('parent_title'),
            'state': s.get('state'),
            'decision': s.get('transcode_decision'),
            'bandwidth': s.get('bandwidth'),
            'thumb': s.get('thumb'),
        } for s in sessions],
        'downloads': downloads,
        'active_bandwidth_kbps': active_kbps,
        'active_streams': len(active),
        'ops': ops_stats(downloads),
    })


@app.get('/api/seerr/pending-requests')
async def seerr_pending_requests_api(db: Session = Depends(get_db), context: AuthContext = Depends(require_auth)):
    require_api_write(context, '*')
    cfg = all_settings()
    if not cfg.get('seerr_url') or not cfg.get('seerr_api_key'):
        return {'requests': [], 'count': 0, 'error': 'Seerr is not configured'}
    seerr = SeerrClient(ServiceConfig(url=cfg['seerr_url'], api_key=cfg['seerr_api_key']))
    radarr, sonarr = await service_clients()
    return await pending_request_payload(db, seerr, radarr, sonarr)


@app.post('/api/seerr/requests/{request_id}/approve')
async def seerr_approve_request_api(request_id: str, db: Session = Depends(get_db), context: AuthContext = Depends(require_auth)):
    require_api_write(context, '*')
    cfg = all_settings()
    if not cfg.get('seerr_url') or not cfg.get('seerr_api_key'):
        raise HTTPException(status_code=503, detail='Seerr is not configured')
    await SeerrClient(ServiceConfig(url=cfg['seerr_url'], api_key=cfg['seerr_api_key'])).approve_request(request_id)
    row = db.scalar(select(MediaRequest).where(MediaRequest.source == 'seerr', MediaRequest.source_id == str(request_id)))
    if row:
        row.status = 'approved'
        db.commit()
        await notify_homeassistant('request_approved', {'request_id': request_id, 'title': row.title, 'requester': row.requester_name})
    return {'ok': True, 'request_id': request_id, 'status': 'approved'}


@app.post('/api/seerr/requests/{request_id}/decline')
async def seerr_decline_request_api(request_id: str, db: Session = Depends(get_db), context: AuthContext = Depends(require_auth)):
    require_api_write(context, '*')
    cfg = all_settings()
    if not cfg.get('seerr_url') or not cfg.get('seerr_api_key'):
        raise HTTPException(status_code=503, detail='Seerr is not configured')
    await SeerrClient(ServiceConfig(url=cfg['seerr_url'], api_key=cfg['seerr_api_key'])).decline_request(request_id)
    row = db.scalar(select(MediaRequest).where(MediaRequest.source == 'seerr', MediaRequest.source_id == str(request_id)))
    if row:
        row.status = 'declined'
        db.commit()
        await notify_homeassistant('request_declined', {'request_id': request_id, 'title': row.title, 'requester': row.requester_name})
    return {'ok': True, 'request_id': request_id, 'status': 'declined'}


@app.get('/api/libraries')
async def libraries_api(
    q: str = '', kind: str = 'all', source: str = 'all',
    db: Session = Depends(get_db), context: AuthContext = Depends(require_auth)
):
    payload = await library_service.browse_libraries_payload(db, q, kind, source)
    payload['user'] = api_user_payload(context)
    payload['server_label'] = media_server_label()
    return payload


@app.post('/api/libraries/sync-catalog')
async def libraries_sync_catalog_api(db: Session = Depends(get_db), context: AuthContext = Depends(require_auth)):
    require_api_write(context, 'libraries.write')
    return await sync_plex_library_catalog(db)


@app.post('/api/libraries/manage/monitor')
async def libraries_monitor_api(
    payload: dict = Body(...), context: AuthContext = Depends(require_auth)
):
    require_api_write(context, 'libraries.write')
    return await library_service.set_library_item_monitoring(
        str(payload.get('kind') or ''),
        int(payload.get('source_index') or 0),
        int(payload.get('item_id') or 0),
        bool(payload.get('monitored')),
    )


@app.post('/api/libraries/manage/delete')
async def libraries_delete_api(
    payload: dict = Body(...), context: AuthContext = Depends(require_auth)
):
    require_api_write(context, 'libraries.delete')
    return await library_service.delete_library_item(
        str(payload.get('kind') or ''),
        int(payload.get('source_index') or 0),
        int(payload.get('item_id') or 0),
        bool(payload.get('delete_files', True)),
    )


@app.get('/api/graphs')
def graphs_api(
    selected_user: str = '', days: int = 365,
    db: Session = Depends(get_db), context: AuthContext = Depends(require_auth)
):
    scope_user = selected_user if context.is_admin and selected_user else (None if context.is_admin else context.username)
    days = days if days in {30, 90, 365} else 365
    users = db.execute(select(PlexSession.username).group_by(PlexSession.username).order_by(func.lower(PlexSession.username))).scalars().all() if context.is_admin else [context.username]
    return {'user': api_user_payload(context), 'graphs': graphs_payload(db, scope_user, days), 'users': users, 'selected_user': selected_user, 'selected_days': days}


@app.get('/api/history')
async def history_api(
    selected_user: str = '', media_type: str = '', decision: str = '', days: str = '365',
    db: Session = Depends(get_db), context: AuthContext = Depends(require_auth)
):
    allowed_days = {'30', '90', '365', 'all'}
    selected_days = days if days in allowed_days else '365'
    filters = []
    if selected_days != 'all':
        filters.append(PlexSession.started_at >= datetime.utcnow() - timedelta(days=int(selected_days)))
    if selected_user and context.is_admin:
        filters.append(func.lower(PlexSession.username) == selected_user.lower())
    if not context.is_admin:
        filters.append(func.lower(PlexSession.username) == context.username.lower())
    if media_type:
        filters.append(PlexSession.media_type == media_type)
    if decision:
        filters.append(PlexSession.transcode_decision == decision)
    rows = db.scalars(select(PlexSession).where(*filters).order_by(PlexSession.started_at.desc()).limit(300)).all()
    stats_row = db.execute(select(
        func.count(PlexSession.id),
        func.coalesce(func.sum(PlexSession.watched_seconds), 0),
        func.coalesce(func.sum(PlexSession.bytes_streamed), 0),
        func.coalesce(func.sum(case((PlexSession.transcode_decision == 'transcode', 1), else_=0)), 0),
        func.count(PlexSession.bytes_streamed),
    ).where(*filters)).one()
    users = db.execute(select(PlexSession.username).group_by(PlexSession.username).order_by(func.lower(PlexSession.username))).scalars().all() if context.is_admin else [context.username]
    media_types = db.execute(select(PlexSession.media_type).where(PlexSession.media_type.is_not(None)).group_by(PlexSession.media_type).order_by(PlexSession.media_type)).scalars().all()
    decisions = db.execute(select(PlexSession.transcode_decision).where(PlexSession.transcode_decision.is_not(None)).group_by(PlexSession.transcode_decision).order_by(PlexSession.transcode_decision)).scalars().all()
    return {
        'user': api_user_payload(context),
        'rows': [plex_session_payload(row) for row in rows],
        'filters': {'users': users, 'media_types': media_types, 'decisions': decisions, 'selected_user': selected_user, 'media_type': media_type, 'decision': decision, 'selected_days': selected_days},
        'stats': {'sessions': int(stats_row[0] or 0), 'hours': float(stats_row[1] or 0) / 3600, 'tb': float(stats_row[2] or 0) / 1_000_000_000_000, 'transcodes': int(stats_row[3] or 0), 'bandwidth_rows': int(stats_row[4] or 0)},
    }


@app.get('/api/admin/overview')
async def admin_overview_api(period: str = 'all', db: Session = Depends(get_db), context: AuthContext = Depends(require_auth)):
    require_api_write(context, '*')
    overview_period = selected_overview_period(period)
    since = overview_period_since(overview_period)
    sessions, seconds, streamed, transcodes = summary_query(db, since=since)
    leaders_stmt = select(
        PlexSession.username,
        func.count(PlexSession.id).label('sessions'),
        func.coalesce(func.sum(PlexSession.watched_seconds), 0).label('seconds'),
        func.coalesce(func.sum(PlexSession.bytes_streamed), 0).label('bytes'),
    ).group_by(PlexSession.username).order_by(func.sum(PlexSession.watched_seconds).desc()).limit(12)
    leaders = db.execute(with_since(leaders_stmt, PlexSession.started_at, since)).all()
    request_leaders_stmt = select(
        MediaRequest.requester_name,
        func.count(MediaRequest.id).label('requests'),
        func.coalesce(func.sum(MediaRequest.fulfilled_bytes), 0).label('bytes'),
    ).group_by(MediaRequest.requester_name).order_by(func.count(MediaRequest.id).desc()).limit(12)
    request_leaders = db.execute(with_since(request_leaders_stmt, MediaRequest.requested_at, since)).all()
    requests_stmt = select(MediaRequest).order_by(MediaRequest.requested_at.desc()).limit(20)
    requests = db.scalars(with_since(requests_stmt, MediaRequest.requested_at, since)).all()
    return {
        'user': api_user_payload(context),
        'period': {'key': overview_period['key'], 'label': overview_period['label'], 'days': overview_period['days']},
        'kpis': {'hours': int(seconds or 0) / 3600, 'sessions': sessions, 'terabytes': int(streamed or 0) / 1e12, 'transcodes': transcodes},
        'leaders': [{'username': r.username, 'sessions': int(r.sessions or 0), 'seconds': int(r.seconds or 0), 'bytes': int(r.bytes or 0)} for r in leaders],
        'request_leaders': [{'requester_name': r.requester_name, 'requests': int(r.requests or 0), 'bytes': int(r.bytes or 0)} for r in request_leaders],
        'requests': [request_payload(row) for row in requests],
    }


@app.get('/api/live')
async def live_api(db: Session = Depends(get_db), context: AuthContext = Depends(require_auth)):
    sessions = await enriched_active_payloads(db)
    downloads = active_download_payloads(db)
    return {
        'user': api_user_payload(context),
        'stats': live_stats_from_payloads(sessions),
        'sessions': sessions,
        'downloads': downloads,
        'ops': ops_stats(downloads),
    }


@app.get('/api/downloads')
async def downloads_api(status: str = 'all', db: Session = Depends(get_db), context: AuthContext = Depends(require_auth)):
    require_api_write(context, '*')
    radarr, sonarr = await service_clients()
    queue = active_download_payloads(db)
    movie_map, series_map = await arr_maps(radarr, sonarr)
    rows = consolidated_requests(db, 200)
    status_counts = {'pending': 0, 'approved': 0, 'available': 0, 'declined': 0}
    for row in rows:
        slug = (_status_label(row.status) or 'unknown').lower()
        if slug == 'requested':
            slug = 'pending'
        if slug in status_counts:
            status_counts[slug] += 1
    selected_status = (status or 'all').lower()
    if selected_status in status_counts:
        rows = [r for r in rows if (('pending' if (_status_label(r.status) or '').lower() == 'requested' else (_status_label(r.status) or '').lower()) == selected_status)]
    requests = []
    for row in rows[:120]:
        status_label = _status_label(row.status)
        status_slug = (status_label or '').lower()
        if status_slug == 'requested':
            status_slug = 'pending'
        requests.append({
            **request_payload(row),
            'display_title': request_display_title(row, movie_map, series_map),
            'status_slug': status_slug,
            'release': release_for_request(row, movie_map, series_map),
            'release_eta': release_eta_for_request(row, movie_map, series_map, status_slug),
            'can_approve': row.source == 'seerr' and bool(row.source_id) and status_slug in {'pending', 'requested'},
            'can_decline': row.source == 'seerr' and bool(row.source_id) and status_slug in {'pending', 'requested'},
        })
    return {'user': api_user_payload(context), 'queue': queue, 'requests': requests, 'status_counts': status_counts, 'selected_status': selected_status}


@app.get('/requests', response_class=HTMLResponse)
@app.get('/downloads', response_class=HTMLResponse)
async def downloads_page(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse('/', status_code=302)
    radarr, sonarr = await service_clients()
    queue = active_download_payloads(db)
    movie_map, series_map = await arr_maps(radarr, sonarr)
    rows = consolidated_requests(db, 200)
    status_counts = {'pending': 0, 'approved': 0, 'available': 0, 'declined': 0}
    for row in rows:
        slug = (_status_label(row.status) or 'unknown').lower()
        if slug == 'requested':
            slug = 'pending'
        if slug in status_counts:
            status_counts[slug] += 1
    selected_status = (request.query_params.get('status') or 'all').lower()
    if selected_status in status_counts:
        rows = [r for r in rows if (('pending' if (_status_label(r.status) or '').lower() == 'requested' else (_status_label(r.status) or '').lower()) == selected_status)]
    requests = []
    for row in rows[:120]:
        status = _status_label(row.status)
        status_slug = (status or '').lower()
        if status_slug == 'requested':
            status_slug = 'pending'
        requests.append({
            'row': row, 'status': status, 'status_slug': status_slug,
            'display_title': request_display_title(row, movie_map, series_map),
            'release': release_for_request(row, movie_map, series_map),
            'release_eta': release_eta_for_request(row, movie_map, series_map, status_slug),
            'can_approve': row.source == 'seerr' and bool(row.source_id) and status_slug in {'pending', 'requested'},
            'can_decline': row.source == 'seerr' and bool(row.source_id) and status_slug in {'pending', 'requested'},
        })
    return templates.TemplateResponse('downloads.html', {
        'request': request, 'user': user, 'queue': queue, 'requests': requests,
        'notice': request.query_params.get('notice') or '',
        'notice_kind': request.query_params.get('notice_kind') or 'ok',
        'server_time_label': datetime.now(SERVER_TZ).strftime('%Z'),
        'selected_status': selected_status, 'status_counts': status_counts,
    })


@app.post('/downloads/requests/{request_id}/delete')
async def delete_download_request(request_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse('/', status_code=302)
    row = db.get(MediaRequest, request_id)
    if not row:
        return request_redirect('Request not found.', 'bad')
    can_delete_local = row.source != 'seerr'
    if row.source == 'seerr' and row.source_id:
        cfg = all_settings()
        if cfg.get('seerr_url') and cfg.get('seerr_api_key'):
            client = SeerrClient(ServiceConfig(url=cfg['seerr_url'], api_key=cfg['seerr_api_key']))
            try:
                await client.delete_request(row.source_id)
                can_delete_local = True
            except Exception:
                can_delete_local = False
    if can_delete_local:
        title = row.title
        db.delete(row)
        db.commit()
        return request_redirect(f'Deleted request record for {title}. Media files were not deleted.')
    return request_redirect(f'Could not delete request record for {row.title}.', 'bad')


@app.post('/downloads/requests/{request_id}/approve')
async def approve_download_request(request_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse('/', status_code=302)
    row = db.get(MediaRequest, request_id)
    if row and row.source == 'seerr' and row.source_id:
        cfg = all_settings()
        if cfg.get('seerr_url') and cfg.get('seerr_api_key'):
            try:
                await SeerrClient(ServiceConfig(url=cfg['seerr_url'], api_key=cfg['seerr_api_key'])).approve_request(row.source_id)
                row.status = 'approved'
                db.commit()
                await notify_homeassistant('request_approved', {'request_id': row.source_id, 'title': row.title, 'requester': row.requester_name})
            except Exception:
                pass
    return RedirectResponse('/requests', status_code=303)


@app.post('/downloads/requests/{request_id}/decline')
async def decline_download_request(request_id: int, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse('/', status_code=302)
    row = db.get(MediaRequest, request_id)
    if row and row.source == 'seerr' and row.source_id:
        cfg = all_settings()
        if cfg.get('seerr_url') and cfg.get('seerr_api_key'):
            try:
                await SeerrClient(ServiceConfig(url=cfg['seerr_url'], api_key=cfg['seerr_api_key'])).decline_request(row.source_id)
                row.status = 'declined'
                db.commit()
                await notify_homeassistant('request_declined', {'request_id': row.source_id, 'title': row.title, 'requester': row.requester_name})
            except Exception:
                pass
    return RedirectResponse('/requests', status_code=303)


async def find_arr_media(title: str, request_type: str):
    radarr, sonarr = await service_clients()
    if request_type == 'movie':
        if not radarr:
            return None, None, 'Radarr'
        return radarr, await radarr.search_movie(title), 'Radarr'
    if not sonarr:
        return None, None, 'Sonarr'
    return sonarr, await sonarr.search_series(title), 'Sonarr'


@app.post('/downloads/search')
async def trigger_download_search(request: Request, title: str = Form(...), request_type: str = Form(...), db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse('/', status_code=302)
    client, media, target = await find_arr_media(title, request_type)
    if not client:
        return request_redirect(f'{target} is not configured.', 'bad')
    if not media or not media.get('id'):
        return request_redirect(f'{target} did not find {title}.', 'bad')
    try:
        await client.trigger_search(int(media['id']))
    except Exception:
        return request_redirect(f'Could not send request to {target}.', 'bad')
    await scheduled_downloads_poll()
    return request_redirect(f'Request sent to {target} for {title}.')


@app.post('/downloads/media/unmonitor')
async def unmonitor_download_media(request: Request, title: str = Form(...), request_type: str = Form(...), db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse('/', status_code=302)
    client, media, target = await find_arr_media(title, request_type)
    if not client:
        return request_redirect(f'{target} is not configured.', 'bad')
    if not media or not media.get('id'):
        return request_redirect(f'{target} did not find {title}.', 'bad')
    try:
        if request_type == 'movie':
            await client.set_movie_monitored(int(media['id']), False)
        else:
            await client.set_series_monitored(int(media['id']), False)
    except Exception:
        return request_redirect(f'Could not unmonitor {title} in {target}.', 'bad')
    return request_redirect(f'{title} is now unmonitored in {target}.')


@app.post('/downloads/media/delete-files')
async def delete_download_media_files(request: Request, title: str = Form(...), request_type: str = Form(...), db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse('/', status_code=302)
    client, media, target = await find_arr_media(title, request_type)
    if not client:
        return request_redirect(f'{target} is not configured.', 'bad')
    if not media or not media.get('id'):
        return request_redirect(f'{target} did not find {title}.', 'bad')
    try:
        if request_type == 'movie':
            await client.delete_movie(int(media['id']), delete_files=True)
        else:
            await client.delete_series(int(media['id']), delete_files=True)
    except Exception:
        return request_redirect(f'Could not delete files for {title} through {target}.', 'bad')
    return request_redirect(f'Delete files command sent to {target} for {title}.')


@app.get('/graphs', response_class=HTMLResponse)
def graphs_page(request: Request, db: Session = Depends(get_db), selected_user: str = '', days: int = 365):
    user = current_user(request, db)
    if not user:
        return RedirectResponse('/', status_code=302)
    scope_user = selected_user if user.is_admin and selected_user else (None if user.is_admin else user.username)
    days = days if days in {30, 90, 365} else 365
    payload = graphs_payload(db, scope_user, days)
    users = db.execute(select(PlexSession.username).group_by(PlexSession.username).order_by(func.lower(PlexSession.username))).scalars().all() if user.is_admin else [user.username]
    return templates.TemplateResponse('graphs.html', {'request': request, 'user': user, 'graphs': payload, 'users': users, 'selected_user': selected_user, 'selected_days': days})


@app.get('/users', response_class=HTMLResponse)
def users_page(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse('/', status_code=302)
    since = datetime.utcnow() - timedelta(days=365)
    usage_rows = db.execute(select(
        PlexSession.username,
        func.count(PlexSession.id).label('sessions'),
        func.coalesce(func.sum(PlexSession.watched_seconds), 0).label('seconds'),
        func.coalesce(func.sum(PlexSession.bytes_streamed), 0).label('bytes'),
        func.count(func.distinct(PlexSession.player)).label('players'),
        func.max(PlexSession.started_at).label('last_seen'),
    ).where(PlexSession.started_at >= since).group_by(PlexSession.username)).all()
    usage_by_name = {r.username.lower(): r for r in usage_rows}
    profiles = db.scalars(select(User).order_by(func.lower(User.username))).all()
    profile_names = {p.username.lower() for p in profiles}
    rows = []
    for profile in profiles:
        usage = usage_by_name.get(profile.username.lower())
        rows.append({'username': profile.username, 'profile': profile, 'usage': usage})
    for name, usage in usage_by_name.items():
        if name not in profile_names:
            rows.append({'username': usage.username, 'profile': None, 'usage': usage})
    rows.sort(key=lambda r: int((r['usage'].seconds if r['usage'] else 0) or 0), reverse=True)
    chart = weekly_watch(db)
    weekly_chart = weekly_chart_payload(chart)
    return templates.TemplateResponse('users.html', {'request': request, 'user': user, 'rows': rows, 'chart': chart, 'weekly_chart': weekly_chart})




@app.post('/users/{username}/admin')
def update_user_admin(username: str, request: Request, is_admin: str = Form(''), db: Session = Depends(get_db)):
    actor = current_user(request, db)
    if not actor or not actor.is_admin:
        return RedirectResponse('/', status_code=302)
    target = db.scalar(select(User).where(func.lower(User.username) == username.lower()))
    if not target:
        return RedirectResponse('/users', status_code=303)
    make_admin = is_admin in {'1', 'true', 'yes', 'on'}
    if not make_admin and target.is_admin:
        admin_count = db.scalar(select(func.count(User.id)).where(User.is_admin == True)) or 0
        if admin_count <= 1:
            return RedirectResponse('/users?admin_error=last-admin', status_code=303)
    target.is_admin = make_admin
    db.commit()
    return RedirectResponse('/users', status_code=303)


@app.get('/users/{username}', response_class=HTMLResponse)
async def user_detail(username: str, request: Request, db: Session = Depends(get_db), tab: str = 'overview'):
    user = current_user(request, db)
    if not user or (not user.is_admin and user.username.lower() != username.lower()):
        return RedirectResponse('/', status_code=302)
    now = datetime.utcnow()
    def period_stats(days=None):
        q = select(
            func.count(PlexSession.id),
            func.coalesce(func.sum(PlexSession.watched_seconds), 0),
            func.coalesce(func.sum(PlexSession.bytes_streamed), 0),
            func.coalesce(func.avg(PlexSession.bandwidth_kbps), 0),
            func.coalesce(func.max(PlexSession.bandwidth_kbps), 0),
            func.coalesce(func.sum(case((PlexSession.transcode_decision == 'transcode', 1), else_=0)), 0),
        ).where(func.lower(PlexSession.username) == username.lower())
        if days:
            q = q.where(PlexSession.started_at >= now - timedelta(days=days))
        plays, secs, bytes_, avg_kbps, peak_kbps, tx = db.execute(q).one()
        return {'plays': plays, 'seconds': int(secs or 0), 'bytes': int(bytes_ or 0), 'avg_kbps': float(avg_kbps or 0), 'peak_kbps': float(peak_kbps or 0), 'transcodes': tx}
    if tab == 'firewall':
        tab = 'bans'
    tabs = {'overview', 'graphs', 'history', 'devices', 'ips', 'requests', 'permissions', 'watchlist', 'bans'}
    active_tab = tab if tab in tabs else 'overview'
    profile_user = db.scalar(select(User).where(func.lower(User.username) == username.lower()))
    seerr_user, seerr_quota, seerr_permissions, _ = await seerr_user_context(username)
    seerr_perm_value = int((seerr_permissions or {}).get('permissions', (seerr_user or {}).get('permissions', 0)) or 0)
    seerr_policy = seerr_policy_from_permissions(seerr_perm_value)
    periods = {'24h': period_stats(1), '7d': period_stats(7), '30d': period_stats(30), 'all': period_stats(None)}
    sessions, seconds, streamed, transcodes = periods['all']['plays'], periods['all']['seconds'], periods['all']['bytes'], periods['all']['transcodes']
    history_rows = db.scalars(select(PlexSession).where(func.lower(PlexSession.username) == username.lower()).order_by(PlexSession.started_at.desc()).limit(60)).all()
    request_filters = [func.lower(MediaRequest.requester_name) == username.lower()]
    if seerr_user and seerr_user.get('plexId') is not None:
        request_filters.append(MediaRequest.requester_plex_id == str(seerr_user.get('plexId')))
    raw_requests = db.scalars(select(MediaRequest).where(or_(*request_filters)).order_by(MediaRequest.requested_at.desc()).limit(250)).all()
    requests = dedupe_requests(raw_requests)[:80]
    watchlist_filters = [func.lower(UserWatchlistItem.username) == username.lower()]
    if profile_user:
        watchlist_filters.append(UserWatchlistItem.plex_user_id == str(profile_user.plex_id))
    watchlist_items = db.scalars(select(UserWatchlistItem).where(or_(*watchlist_filters)).order_by(UserWatchlistItem.added_at.desc().nullslast(), UserWatchlistItem.title).limit(120)).all()
    raw_ips = db.execute(select(PlexSession.ip_address, func.count(PlexSession.id).label('sessions'), func.max(PlexSession.started_at).label('last_used')).where(func.lower(PlexSession.username) == username.lower(), PlexSession.ip_address.is_not(None)).group_by(PlexSession.ip_address).order_by(func.count(PlexSession.id).desc()).limit(20)).all()
    ips = []
    for row in raw_ips:
        enrich = await lookup_isp(row.ip_address)
        ips.append({'ip_address': row.ip_address, 'sessions': row.sessions, 'last_used': row.last_used, 'ago': format_age(row.last_used, now), 'ptr': reverse_dns(row.ip_address), **enrich})
    chart = weekly_watch(db, username)
    decisions = decision_breakdown(db, username)
    day_bucket = func.date_trunc('day', PlexSession.started_at)
    daily = db.execute(select(day_bucket.label('day'), func.count(PlexSession.id).label('plays'), func.sum(PlexSession.watched_seconds).label('seconds'), func.coalesce(func.sum(PlexSession.bytes_streamed),0).label('bytes')).where(func.lower(PlexSession.username)==username.lower(), PlexSession.started_at >= now - timedelta(days=30)).group_by(day_bucket).order_by(day_bucket)).all()
    hour_bucket = func.extract('hour', PlexSession.started_at)
    hourly = db.execute(select(hour_bucket.label('hour'), func.count(PlexSession.id).label('plays')).where(func.lower(PlexSession.username)==username.lower(), PlexSession.started_at >= now - timedelta(days=30)).group_by(hour_bucket).order_by(hour_bucket)).all()
    chart_data = {
        'weekly': [{'x': r.week.strftime('%b %d'), 'hours': round(float(r.seconds or 0)/3600, 2)} for r in chart],
        'daily': [{'x': r.day.strftime('%b %d'), 'plays': int(r.plays or 0), 'hours': round(float(r.seconds or 0)/3600, 2), 'gb': round(float(r.bytes or 0)/1000000000, 2)} for r in daily],
        'hourly': [{'x': f"{int(r.hour):02d}:00", 'plays': int(r.plays or 0)} for r in hourly],
    }
    raw_devices = db.execute(select(PlexSession.machine_id, PlexSession.player, PlexSession.platform, func.count(PlexSession.id).label('sessions'), func.max(PlexSession.started_at).label('last_used')).where(func.lower(PlexSession.username) == username.lower()).group_by(PlexSession.machine_id, PlexSession.player, PlexSession.platform).order_by(func.max(PlexSession.started_at).desc()).limit(30)).all()
    devices = []
    for d in raw_devices:
        devices.append({'machine_id': d.machine_id, 'player': d.player, 'platform': d.platform, 'sessions': d.sessions, 'last_used': d.last_used, 'ago': format_age(d.last_used, now)})
    policy = db.scalar(select(UserPolicy).where(func.lower(UserPolicy.username) == username.lower()))
    blocks = db.scalars(select(UserBlock).where(func.lower(UserBlock.username) == username.lower(), UserBlock.active == True).order_by(UserBlock.created_at.desc())).all()
    plex_permissions = plex_permission_context(profile_user)
    return templates.TemplateResponse('user_detail.html', {
        'request': request, 'user': user, 'profile_user': profile_user, 'username': username, 'hours': seconds/3600, 'sessions': sessions,
        'terabytes': float(streamed)/1e12, 'transcodes': transcodes, 'history_rows': history_rows,
        'requests': requests, 'chart': chart, 'daily': daily, 'hourly': hourly, 'chart_data': chart_data, 'periods': periods, 'decisions': decisions,
        'devices': devices, 'ips': ips, 'policy': policy, 'blocks': blocks, 'active_tab': active_tab,
        'seerr_user': seerr_user, 'seerr_quota': seerr_quota, 'seerr_permissions': seerr_permissions,
        'seerr_policy': seerr_policy, 'plex_permissions': plex_permissions, 'watchlist_items': watchlist_items, 'perm': PERM, 'status_label': STATUS_LABEL,
    })


@app.get('/plex-image')
async def plex_image(request: Request, path: str, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return Response(status_code=401)
    if not path.startswith(PLEX_IMAGE_PREFIXES) or '..' in path or '\x00' in path:
        return Response(status_code=400)
    cfg = all_settings()
    if not cfg.get('plex_server_url') or not cfg.get('plex_server_token'):
        return Response(status_code=404)
    import httpx
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{cfg['plex_server_url'].rstrip('/')}{path}", headers={'X-Plex-Token': cfg['plex_server_token']})
        ctype = resp.headers.get('content-type', '')
        if resp.status_code >= 400:
            return Response(status_code=resp.status_code)
        if not ctype.startswith('image/'):
            return Response(status_code=415)
        return Response(content=resp.content, media_type=ctype, status_code=resp.status_code)


@app.get('/live', response_class=HTMLResponse)
async def live(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse('/', status_code=302)
    cfg = all_settings()
    enriched_sessions = await enriched_active_payloads(db)
    downloads = active_download_payloads(db)
    live_stats = live_stats_from_payloads(enriched_sessions)
    response = templates.TemplateResponse('live.html', {
        'request': request, 'user': user, 'sessions': enriched_sessions, 'downloads': downloads,
        'ops_stats': ops_stats(downloads), 'config': cfg, 'live_stats': live_stats,
    })
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.post('/live/terminate')
async def terminate_live_stream(request: Request, session_id: str = Form(...), reason: str = Form(''), db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse('/', status_code=302)
    cfg = all_settings()
    if cfg.get('plex_server_url') and cfg.get('plex_server_token'):
        await PlexClient(ServiceConfig(url=cfg['plex_server_url'], token=cfg['plex_server_token'])).terminate_session(session_id, reason or None)
    return RedirectResponse('/live', status_code=303)


@app.get('/cached-art/{name}')
def cached_art(name: str):
    from pathlib import Path
    from .config import settings as app_settings
    if not CACHED_ART_RE.match(name):
        return Response(status_code=404)
    path = Path(app_settings.art_cache_dir) / name
    if not path.exists():
        return Response(status_code=404)
    return Response(content=path.read_bytes(), media_type='image/jpeg')


@app.get('/history', response_class=HTMLResponse)
async def history(request: Request, db: Session = Depends(get_db), selected_user: str = '', media_type: str = '', decision: str = '', days: str = '365'):
    user = current_user(request, db)
    if not user:
        return RedirectResponse('/', status_code=302)
    allowed_days = {'30', '90', '365', 'all'}
    selected_days = days if days in allowed_days else '365'
    filters = []
    if selected_days != 'all':
        filters.append(PlexSession.started_at >= datetime.utcnow() - timedelta(days=int(selected_days)))
    if selected_user:
        filters.append(func.lower(PlexSession.username) == selected_user.lower())
    if not user.is_admin:
        filters.append(func.lower(PlexSession.username) == user.username.lower())
    if media_type:
        filters.append(PlexSession.media_type == media_type)
    if decision:
        filters.append(PlexSession.transcode_decision == decision)
    q = select(PlexSession).where(*filters).order_by(PlexSession.started_at.desc()).limit(300)
    rows = db.scalars(q).all()
    for row in rows[:90]:
        row.cached_thumb = await ensure_art_cached(row.thumb_path) if row.thumb_path else None
    users = db.execute(select(PlexSession.username).group_by(PlexSession.username).order_by(func.lower(PlexSession.username))).scalars().all() if user.is_admin else [user.username]
    media_types = db.execute(select(PlexSession.media_type).where(PlexSession.media_type.is_not(None)).group_by(PlexSession.media_type).order_by(PlexSession.media_type)).scalars().all()
    decisions = db.execute(select(PlexSession.transcode_decision).where(PlexSession.transcode_decision.is_not(None)).group_by(PlexSession.transcode_decision).order_by(PlexSession.transcode_decision)).scalars().all()
    stats_row = db.execute(select(
        func.count(PlexSession.id),
        func.coalesce(func.sum(PlexSession.watched_seconds), 0),
        func.coalesce(func.sum(PlexSession.bytes_streamed), 0),
        func.coalesce(func.sum(case((PlexSession.transcode_decision == 'transcode', 1), else_=0)), 0),
        func.count(PlexSession.bytes_streamed),
    ).where(*filters)).one()
    history_stats = {
        'sessions': int(stats_row[0] or 0),
        'hours': float(stats_row[1] or 0) / 3600,
        'tb': float(stats_row[2] or 0) / 1_000_000_000_000,
        'transcodes': int(stats_row[3] or 0),
        'bandwidth_rows': int(stats_row[4] or 0),
    }
    return templates.TemplateResponse('history.html', {'request': request, 'user': user, 'rows': rows, 'users': users, 'selected_user': selected_user, 'media_type': media_type, 'decision': decision, 'selected_days': selected_days, 'media_types': media_types, 'decisions': decisions, 'history_stats': history_stats})


@app.post('/users/{username}/seerr')
async def save_seerr_user(
    username: str, request: Request, db: Session = Depends(get_db), seerr_user_id: int = Form(...),
    permissions: str = Form(''), request_mode: str = Form(''), auto_request_mode: str = Form(''),
    movie_quota_limit: str = Form(''), movie_quota_days: str = Form(''),
    tv_quota_limit: str = Form(''), tv_quota_days: str = Form(''),
):
    user = current_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse('/', status_code=302)
    cfg = all_settings()
    client = SeerrClient(ServiceConfig(url=cfg['seerr_url'], api_key=cfg['seerr_api_key']))
    try:
        current = await client.user(seerr_user_id)
        if permissions.strip():
            new_permissions = int(permissions)
            await client.update_user_permissions(seerr_user_id, new_permissions)
            current['permissions'] = new_permissions
        for field, raw in {
            'movieQuotaLimit': movie_quota_limit,
            'movieQuotaDays': movie_quota_days,
            'tvQuotaLimit': tv_quota_limit,
            'tvQuotaDays': tv_quota_days,
        }.items():
            current[field] = int(raw) if raw.strip() else None
        await client.update_user(seerr_user_id, current)
    except Exception:
        pass
    return RedirectResponse(f'/users/{username}?tab=permissions', status_code=303)


@app.post('/users/{username}/blocks')
def add_user_block(
    username: str, request: Request, db: Session = Depends(get_db), block_type: str = Form(...),
    value: str = Form(...), label: str = Form(''), message: str = Form(''), return_to: str = Form(''),
):
    user = current_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse('/', status_code=302)
    block_type = block_type.strip().lower()
    value = value.strip()
    if block_type in {'ip', 'device'} and value:
        row = db.scalar(select(UserBlock).where(
            func.lower(UserBlock.username) == username.lower(),
            UserBlock.block_type == block_type,
            UserBlock.value == value,
        ))
        if not row:
            row = UserBlock(username=username, block_type=block_type, value=value)
            db.add(row)
        row.label = label or row.label
        default_message = 'The IP address you are connecting from is banned. Please contact your server administrator.' if block_type == 'ip' else 'The device you are connecting from is banned. Please contact your server administrator.'
        row.message = message or row.message or default_message
        row.active = True
        db.commit()
    target = safe_internal_redirect(return_to, f'/users/{username}?tab=bans')
    return RedirectResponse(target, status_code=303)


@app.post('/users/{username}/blocks/{block_id}/unban')
def unban_user_block(username: str, block_id: int, request: Request, db: Session = Depends(get_db), return_to: str = Form('')):
    user = current_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse('/', status_code=302)
    row = db.get(UserBlock, block_id)
    if row and row.username.lower() == username.lower():
        row.active = False
        db.commit()
    target = safe_internal_redirect(return_to, f'/users/{username}?tab=bans')
    return RedirectResponse(target, status_code=303)


@app.post('/users/{username}/policy')
def save_user_policy(
    username: str, request: Request, db: Session = Depends(get_db), blocked: bool = Form(False),
    block_message: str = Form(''), max_concurrent_streams: str = Form(''), max_public_ips: str = Form(''), max_concurrent_devices: str = Form(''),
):
    user = current_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse('/', status_code=302)
    row = db.scalar(select(UserPolicy).where(func.lower(UserPolicy.username) == username.lower()))
    if not row:
        row = UserPolicy(username=username)
        db.add(row)
    row.blocked = bool(blocked)
    row.block_message = block_message or None
    row.max_concurrent_streams = int(max_concurrent_streams) if max_concurrent_streams else None
    row.max_public_ips = int(max_public_ips) if max_public_ips else None
    row.max_concurrent_devices = int(max_concurrent_devices) if max_concurrent_devices else None
    db.commit()
    return RedirectResponse(f'/users/{username}?tab=permissions', status_code=303)


@app.get('/libraries', response_class=HTMLResponse)
async def libraries(request: Request, db: Session = Depends(get_db), q: str = '', kind: str = 'all', source: str = 'all'):
    user = current_user(request, db)
    if not user:
        return RedirectResponse('/', status_code=302)
    cfg = all_settings()
    libs = []
    if cfg.get('plex_server_url') and cfg.get('plex_server_token'):
        try:
            client = PlexClient(ServiceConfig(url=cfg['plex_server_url'], token=cfg['plex_server_token']))
            libs = await client.libraries()
            for lib in libs:
                try:
                    lib['count'] = await client.library_count(lib['key'])
                except Exception:
                    lib['count'] = 0
                lib['bytes'] = None
        except Exception:
            libs = []

    inventory = []
    errors = []
    for inst in _arr_clients('radarr'):
        try:
            movies = await inst['client'].movies()
            inventory += [normalise_movie(m, inst['name'], inst['index']) for m in movies]
        except Exception as exc:
            errors.append(f"{inst['name']}: {exc}")
    for inst in _arr_clients('sonarr'):
        try:
            series = await inst['client'].series()
            inventory += [normalise_series(row, inst['name'], inst['index']) for row in series]
        except Exception as exc:
            errors.append(f"{inst['name']}: {exc}")

    enrich_inventory_usage(db, inventory)
    movies = [i for i in inventory if i['kind'] == 'movie']
    series = [i for i in inventory if i['kind'] == 'series']
    sources = sorted({i['source'] for i in inventory})
    selected_item = selected_library_item(inventory, q, kind, source)
    selected_detail = library_item_detail(db, selected_item) if selected_item else None
    query = (q or '').strip().lower()
    filtered = inventory
    if kind in {'movie', 'series'}:
        filtered = [i for i in filtered if i['kind'] == kind]
    if source and source != 'all':
        filtered = [i for i in filtered if i['source'] == source]
    if query:
        filtered = [i for i in filtered if query in (i['title'] or '').lower() or query in (i.get('path') or '').lower()]
    filtered = sorted(filtered, key=lambda i: (i.get('title') or '').lower())[:250]
    library_stats = {
        'movies_count': len(movies), 'movies_size': sum(i['size'] for i in movies),
        'series_count': len(series), 'series_size': sum(i['size'] for i in series),
        'total_size': sum(i['size'] for i in inventory),
        'movie_top': sorted(movies, key=lambda i: i['size'], reverse=True)[:8],
        'series_top': sorted(series, key=lambda i: i['size'], reverse=True)[:8],
        'stale_large': sorted([i for i in inventory if i.get('plays', 0) > 0 and i.get('stale_days', 0) >= 180], key=lambda i: (i.get('size') or 0) * i.get('stale_days', 0), reverse=True)[:8],
        'never_watched': sorted([i for i in inventory if i.get('plays', 0) == 0], key=lambda i: i.get('size') or 0, reverse=True)[:8],
        'recently_watched': sorted([i for i in inventory if i.get('last_watched_at')], key=lambda i: i.get('last_watched_at'), reverse=True)[:8],
    }
    return templates.TemplateResponse('libraries.html', {
        'request': request, 'user': user, 'libraries': libs, 'items': filtered,
        'q': q, 'kind': kind, 'source': source, 'sources': sources,
        'stats': library_stats, 'errors': errors, 'selected_item': selected_item, 'selected_detail': selected_detail,
    })


@app.post('/libraries/manage/delete')
async def delete_library_item(
    request: Request, db: Session = Depends(get_db), kind: str = Form(...), source_index: int = Form(...), item_id: int = Form(...),
    delete_files: bool = Form(True), return_to: str = Form('/libraries')
):
    user = current_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse('/', status_code=302)
    try:
        if kind == 'movie':
            clients = _arr_clients('radarr')
            inst = next(c for c in clients if c['index'] == source_index)
            await inst['client'].delete_movie(item_id, delete_files=delete_files)
        elif kind == 'series':
            clients = _arr_clients('sonarr')
            inst = next(c for c in clients if c['index'] == source_index)
            await inst['client'].delete_series(item_id, delete_files=delete_files)
    except Exception:
        logger.exception('Failed to delete library item through Arr')
    target = return_to if return_to.startswith('/libraries') and not return_to.startswith('//') else '/libraries'
    return RedirectResponse(target, status_code=303)


@app.post('/libraries/manage/monitor')
async def set_library_item_monitoring(
    request: Request, db: Session = Depends(get_db), kind: str = Form(...), source_index: int = Form(...), item_id: int = Form(...),
    monitored: bool = Form(...), return_to: str = Form('/libraries')
):
    user = current_user(request, db)
    if not user or not user.is_admin:
        return RedirectResponse('/', status_code=302)
    try:
        if kind == 'movie':
            clients = _arr_clients('radarr')
            inst = next(c for c in clients if c['index'] == source_index)
            await inst['client'].set_movie_monitored(item_id, monitored=monitored)
        elif kind == 'series':
            clients = _arr_clients('sonarr')
            inst = next(c for c in clients if c['index'] == source_index)
            await inst['client'].set_series_monitored(item_id, monitored=monitored)
    except Exception:
        logger.exception('Failed to update library monitoring through Arr')
    target = return_to if return_to.startswith('/libraries') and not return_to.startswith('//') else '/libraries'
    return RedirectResponse(target, status_code=303)


@app.get('/libraries/{section_id}', response_class=HTMLResponse)
async def library_detail(section_id: str, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse('/', status_code=302)
    cfg = all_settings()
    client = PlexClient(ServiceConfig(url=cfg['plex_server_url'], token=cfg['plex_server_token']))
    libs = await client.libraries()
    library = next((l for l in libs if l['key'] == section_id), None)
    if not library:
        return Response(status_code=404)
    title_map = {'movie': 'movie', 'show': 'episode', 'artist': 'track'}
    media_type = title_map.get(library['type'])
    rows = db.execute(select(
        PlexSession.title,
        func.sum(PlexSession.watched_seconds).label('seconds'),
        func.count(PlexSession.id).label('plays'),
    ).where(PlexSession.media_type == media_type, PlexSession.title.is_not(None)).group_by(PlexSession.title).order_by(func.sum(PlexSession.watched_seconds).desc()).limit(50)).all() if media_type else []
    bucket = func.date_trunc('week', PlexSession.started_at)
    chart = db.execute(select(bucket.label('week'), func.sum(PlexSession.watched_seconds).label('seconds')).where(PlexSession.media_type == media_type).group_by(bucket).order_by(bucket)).all() if media_type else []
    library['count'] = await client.library_count(section_id)
    weekly_chart = weekly_chart_payload(chart) if chart else {'points': [], 'max': 1, 'half': 0.5, 'weeks': 26}
    return templates.TemplateResponse('library_detail.html', {'request': request, 'user': user, 'library': library, 'rows': rows, 'chart': chart, 'weekly_chart': weekly_chart})


@app.post('/webhooks/plex')
async def plex_webhook(request: Request, db: Session = Depends(get_db)):
    token = settings.plex_webhook_token.strip()
    if not token:
        return JSONResponse({'ok': False, 'error': 'webhook token not configured'}, status_code=404)
    provided = request.headers.get('x-mediaops-webhook-token') or request.path_params.get('secret') or request.query_params.get('token') or ''
    if not hmac.compare_digest(provided, token):
        return JSONResponse({'ok': False, 'error': 'invalid webhook token'}, status_code=403)
    if int(request.headers.get('content-length') or 0) > 1_000_000:
        return JSONResponse({'ok': False, 'error': 'payload too large'}, status_code=413)
    form = await request.form()
    payload_raw = form.get('payload')
    if not payload_raw:
        return JSONResponse({'ok': False, 'error': 'missing payload'}, status_code=400)
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        return JSONResponse({'ok': False, 'error': 'bad json'}, status_code=400)
    event = payload.get('event')
    account = payload.get('Account') or {}
    player = payload.get('Player') or {}
    metadata = payload.get('Metadata') or {}
    session_key = str(metadata.get('sessionKey') or metadata.get('ratingKey') or '')
    if event in {'media.play', 'media.resume'}:
        data = {
            'session_key': session_key,
            'user_id': account.get('id'),
            'user': account.get('title') or 'unknown',
            'rating_key': metadata.get('ratingKey'),
            'display_title': ((metadata.get('grandparentTitle') + ' - ') if metadata.get('grandparentTitle') else '') + (metadata.get('title') or ''),
            'title': metadata.get('title'),
            'grandparent_title': metadata.get('grandparentTitle'),
            'parent_title': metadata.get('parentTitle'),
            'media_index': int(metadata.get('index') or 0) if metadata.get('index') else None,
            'parent_media_index': int(metadata.get('parentIndex') or 0) if metadata.get('parentIndex') else None,
            'type': metadata.get('type'),
            'player': player.get('title'),
            'platform': player.get('platform'),
            'player_address': player.get('publicAddress') or player.get('address'),
            'view_offset': metadata.get('viewOffset') or 0,
            'state': 'playing',
        }
        upsert_active_from_live(db, data)
    elif event == 'media.pause':
        active = db.get(ActivePlexSession, session_key)
        if active:
            active.state = 'paused'
            active.last_seen_at = datetime.utcnow()
            db.commit()
    elif event == 'media.stop':
        active = db.get(ActivePlexSession, session_key)
        if active:
            finalize_session(db, active)
    return JSONResponse({'ok': True, 'event': event})


@app.post('/webhooks/plex/{secret}')
async def plex_webhook_secret(secret: str, request: Request, db: Session = Depends(get_db)):
    return await plex_webhook(request, db)
