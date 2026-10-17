from __future__ import annotations

import json
import calendar
import unicodedata
from urllib.parse import urlencode
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, or_, select, any_, bindparam, Integer
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from ..models import PlexSession, ActivePlexSession, PlexLibraryItem
from .clients import PlexClient, RadarrClient, ServiceConfig, SonarrClient
from .settings_store import all_settings


def format_bytes(value: Any) -> str:
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


def _decode_instances(raw: str, fallback_name: str, fallback_url: str, fallback_key: str) -> list[dict[str, Any]]:
    try:
        items = json.loads(raw or '[]')
    except Exception:
        items = []
    if not items and (fallback_url or fallback_key):
        items = [{'name': fallback_name, 'url': fallback_url, 'api_key': fallback_key}]
    return [i for i in items if i.get('url') or i.get('api_key') or i.get('name')]


def service_context(values: dict[str, str] | None = None) -> dict[str, list[dict[str, Any]]]:
    values = values or all_settings()
    return {
        'radarr': _decode_instances(values.get('radarr_instances', '[]'), 'Radarr', values.get('radarr_url', ''), values.get('radarr_api_key', '')),
        'sonarr': _decode_instances(values.get('sonarr_instances', '[]'), 'Sonarr', values.get('sonarr_url', ''), values.get('sonarr_api_key', '')),
    }


def arr_instance_configs(kind: str, values: dict[str, str] | None = None) -> list[dict[str, Any]]:
    instances = service_context(values).get(kind, [])
    return [i for i in instances if i.get('url') and i.get('api_key')]


def arr_clients(kind: str, values: dict[str, str] | None = None) -> list[dict[str, Any]]:
    cls = RadarrClient if kind == 'radarr' else SonarrClient
    clients = []
    for idx, inst in enumerate(arr_instance_configs(kind, values)):
        clients.append({
            'index': idx,
            'name': inst.get('name') or ('Radarr' if kind == 'radarr' else 'Sonarr'),
            'client': cls(ServiceConfig(url=inst.get('url', ''), api_key=inst.get('api_key', ''))),
        })
    return clients


def movie_size(row: dict[str, Any]) -> int:
    return int(row.get('sizeOnDisk') or 0)


def series_size(row: dict[str, Any]) -> int:
    stats = row.get('statistics') or {}
    return int(stats.get('sizeOnDisk') or row.get('sizeOnDisk') or 0)


def normalise_movie(row: dict[str, Any], source: str, index: int) -> dict[str, Any]:
    size = movie_size(row)
    return {
        'kind': 'movie', 'id': row.get('id'), 'title': row.get('title') or 'Untitled movie',
        'year': row.get('year'), 'source': source, 'source_index': index,
        'tmdb_id': row.get('tmdbId'), 'tvdb_id': row.get('tvdbId'),
        'episode_count': (row.get('statistics') or {}).get('episodeFileCount', 0),
        'path': row.get('path'), 'size': size, 'size_label': format_bytes(size),
        'available': bool(row.get('hasFile')), 'monitored': bool(row.get('monitored')),
        'quality': ((((row.get('movieFile') or {}).get('quality') or {}).get('quality') or {}).get('name')),
        'poster': next((img.get('remoteUrl') or img.get('url') for img in (row.get('images') or []) if img.get('coverType') == 'poster'), None),
    }


def normalise_series(row: dict[str, Any], source: str, index: int) -> dict[str, Any]:
    stats = row.get('statistics') or {}
    size = series_size(row)
    episodes = stats.get('episodeFileCount') or 0
    total = stats.get('totalEpisodeCount') or 0
    return {
        'kind': 'series', 'id': row.get('id'), 'title': row.get('title') or 'Untitled series',
        'year': row.get('year'), 'source': source, 'source_index': index,
        'tmdb_id': row.get('tmdbId'), 'tvdb_id': row.get('tvdbId'),
        'episode_count': (row.get('statistics') or {}).get('episodeFileCount', 0),
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


def session_media_kind(row: PlexSession) -> str:
    media_type = (row.media_type or '').lower()
    if media_type in {'movie', 'film'}:
        return 'movie'
    if media_type in {'episode', 'show', 'season', 'tv'}:
        return 'show'
    if row.grandparent_title or getattr(row, 'grandparent_rating_key', None):
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
        code = f'{season}{episode} - ' if season or episode else ''
        return f'{row.grandparent_title} - {code}{row.content_title}'
    return row.title or row.content_title or 'Unknown title'


def inv_watch_key(item: dict[str, Any]) -> tuple[str, str]:
    title = (item.get('title') or '').strip().lower()
    return ('show' if item.get('kind') == 'series' else 'movie', title)


def normalized_title(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value or '').casefold().split())


def history_index(db: Session, inventory: list[dict]):
    titles, keys = {}, {}
    for i, item in enumerate(inventory):
        titles.setdefault(('show' if item['kind'] == 'series' else 'movie', normalized_title(item['title'])), []).append(i)
    # Plex catalogue key is scoped to this server. Link only a unique title/year identity.
    for row in db.scalars(select(PlexLibraryItem)):
        name = normalized_title(row.title)
        candidates = titles.get((row.media_type, name), [])
        if row.year and any(inventory[i].get('year') and inventory[i]['year'] != row.year for i in candidates):
            keys[('ambiguous', row.media_type, name)] = True
        keys[(row.media_type, str(row.rating_key or row.key))] = []
        candidates = [i for i in candidates if not row.year or inventory[i].get('year') == row.year]
        identities = {(inventory[i].get('tvdb_id'), inventory[i].get('tmdb_id'), inventory[i].get('year')) for i in candidates}
        if candidates and len(identities) == 1:
            keys[(row.media_type, str(row.rating_key or row.key))] = candidates
    return titles, keys


def match_history(row, inventory, titles, keys):
    kind = session_media_kind(row)
    stable_key = getattr(row, 'grandparent_rating_key', None) if kind == 'show' else row.rating_key
    if stable_key and (kind, str(stable_key)) in keys:
        return keys[(kind, str(stable_key))], False
    raw = row.grandparent_title if kind == 'show' else (row.content_title or row.title)
    if kind == 'show' and not raw:
        raw = (row.title or '').split(' - ', 1)[0]
    candidates = titles.get((kind, normalized_title(raw)), [])
    identities = {(inventory[i].get('tvdb_id'), inventory[i].get('tmdb_id'), inventory[i].get('year')) for i in candidates}
    return candidates, len(identities) > 1 or bool(keys.get(('ambiguous', kind, normalized_title(raw))))


def enrich_inventory_usage(db: Session, inventory: list[dict[str, Any]], viewer: str = '') -> list[dict[str, Any]]:
    if not inventory:
        return inventory
    titles, keys = history_index(db, inventory)
    usage = [dict(plays=0, seconds=0, bytes=0, last=None, last_user=None, ambiguous=False, active=[]) for _ in inventory]
    # Stream every session: no newest-N cutoff and no title-not-null exclusion.
    for row in db.scalars(select(PlexSession).execution_options(yield_per=2000)):
        matches, ambiguous = match_history(row, inventory, titles, keys)
        for i in matches:
            data = usage[i]
            if ambiguous:
                data['ambiguous'] = True
                continue
            if viewer and row.username != viewer:
                continue
            data['plays'] += 1
            data['seconds'] += int(row.watched_seconds or 0)
            data['bytes'] += int(row.bytes_streamed or 0)
            if not data['last'] or row.started_at > data['last']:
                data['last'], data['last_user'] = row.started_at, row.username
    for row in db.scalars(select(ActivePlexSession).where(ActivePlexSession.last_seen_at >= datetime.utcnow() - timedelta(minutes=2))):
        matches, ambiguous = match_history(row, inventory, titles, keys)
        for i in matches:
            usage[i]['active'].append(row.username)
    for item, data in zip(inventory, usage):
        last = data['last']
        item.update(plays=data['plays'], watch_hours=round(data['seconds']/3600, 1), streamed_bytes=data['bytes'],
                    last_watched_at=last, last_watched_by=data['last_user'],
                    history_status='ambiguous' if data['ambiguous'] else ('recorded' if last else 'unknown'),
                    active_viewers=data['active'], last_watched_label=relative_time(last) if last else 'No recorded history',
                    stale_days=(datetime.utcnow()-last).days if last else None)
    return inventory


def calendar_cutoff(months: int, now: datetime | None = None) -> datetime:
    now = now or datetime.utcnow()
    y, m = divmod(now.year * 12 + now.month - 1 - months, 12)
    return now.replace(year=y, month=m+1, day=min(now.day, calendar.monthrange(y, m+1)[1]))


def browse_page(inventory, q='', kind='all', source='all', preset='', months=24, before='', history='', viewer='', sort='', direction='asc', page=1, page_size=50, now=None):
    if preset == 'tv-cleanup':
        kind = 'series'
        if not history: history = 'recorded'
        if not sort: sort, direction = 'size', 'desc'
    months = months if months in (6, 12, 24) else 24
    cutoff = datetime.fromisoformat(before) if before else calendar_cutoff(months, now)
    sort = sort if sort in ('title', 'size', 'last_watched') else 'title'
    direction = direction if direction in ('asc', 'desc') else 'asc'
    history = history if history in ('all', 'recorded', 'unknown', 'include', 'ambiguous') else 'all'
    rows = search_library_items(inventory, q, kind, source, limit=None)
    if history != 'all':
        rows = [i for i in rows if
                (history == 'unknown' and i['history_status'] == 'unknown') or
                (history == 'ambiguous' and i['history_status'] == 'ambiguous') or
                (history == 'include' and i['history_status'] == 'unknown') or
                (history in ('recorded', 'include') and i['history_status'] == 'recorded' and i['last_watched_at'] < cutoff)]
    if preset == 'tv-cleanup': rows = [i for i in rows if not i.get('active_viewers')]
    rows.sort(key=lambda i: (normalized_title(i['title']), i['source'], i['id']))
    field = {'title':'title', 'size':'size', 'last_watched':'last_watched_at'}[sort]
    known = [i for i in rows if i.get(field) is not None]
    unknown = [i for i in rows if i.get(field) is None]
    known.sort(key=lambda i: normalized_title(i[field]) if sort == 'title' else i[field], reverse=direction == 'desc')
    rows = known + unknown
    page_size = max(1, min(200, page_size)); pages = max(1, (len(rows)+page_size-1)//page_size)
    page = max(1, min(pages, page))
    filters = dict(q=q, kind=kind, source=source, preset=preset, months=months, before=before, history=history, viewer=viewer, sort=sort, direction=direction, page=page, page_size=page_size)
    def url(n): return '/libraries?' + urlencode(dict(filters, page=n))
    return dict(items=rows[(page-1)*page_size:page*page_size], filters=filters, total_count=len(rows), total_bytes=sum(i['size'] for i in rows),
                total_bytes_label=format_bytes(sum(i['size'] for i in rows)), page=page, pages=pages, cutoff=cutoff.isoformat(),
                previous_url=url(page-1) if page > 1 else None, next_url=url(page+1) if page < pages else None)


async def plex_libraries(values: dict[str, str] | None = None) -> list[dict[str, Any]]:
    cfg = values or all_settings()
    libs: list[dict[str, Any]] = []
    if not cfg.get('plex_server_url') or not cfg.get('plex_server_token'):
        return libs
    client = PlexClient(ServiceConfig(url=cfg['plex_server_url'], token=cfg['plex_server_token']))
    libs = await client.libraries()
    for lib in libs:
        try:
            lib['count'] = await client.library_count(lib['key'])
        except Exception:
            lib['count'] = 0
        lib['bytes'] = None
    return libs


async def library_inventory(db: Session, values: dict[str, str] | None = None, viewer: str = '') -> tuple[list[dict[str, Any]], list[str]]:
    inventory: list[dict[str, Any]] = []
    errors: list[str] = []
    for inst in arr_clients('radarr', values):
        try:
            movies = await inst['client'].movies()
            inventory += [normalise_movie(m, inst['name'], inst['index']) for m in movies]
        except Exception as exc:
            errors.append(f"{inst['name']}: {exc}")
    for inst in arr_clients('sonarr', values):
        try:
            series = await inst['client'].series()
            inventory += [normalise_series(row, inst['name'], inst['index']) for row in series]
        except Exception as exc:
            errors.append(f"{inst['name']}: {exc}")
    enrich_inventory_usage(db, inventory, viewer)
    return inventory, errors


def selected_library_item(inventory: list[dict[str, Any]], q: str, kind: str = 'all', source: str = 'all') -> dict[str, Any] | None:
    query = (q or '').strip().lower()
    if not query:
        return None
    matches = [i for i in inventory if (i.get('title') or '').strip().lower() == query]
    if kind in {'movie', 'series'}:
        matches = [i for i in matches if i.get('kind') == kind]
    if source and source != 'all':
        matches = [i for i in matches if i.get('source') == source]
    return matches[0] if len(matches) == 1 else None


def search_library_items(inventory: list[dict[str, Any]], q: str = '', kind: str = 'all', source: str = 'all', limit: int | None = None) -> list[dict[str, Any]]:
    query = (q or '').strip().lower()
    filtered = inventory
    if kind in {'movie', 'series'}:
        filtered = [i for i in filtered if i['kind'] == kind]
    if source and source != 'all':
        filtered = [i for i in filtered if i['source'] == source]
    if query:
        filtered = [i for i in filtered if query in (i['title'] or '').lower() or query in (i.get('path') or '').lower()]
    return sorted(filtered, key=lambda i: (i.get('title') or '').lower())[:limit]


def library_stats(inventory: list[dict[str, Any]]) -> dict[str, Any]:
    movies = [i for i in inventory if i['kind'] == 'movie']
    series = [i for i in inventory if i['kind'] == 'series']
    return {
        'movies_count': len(movies), 'movies_size': sum(i['size'] for i in movies),
        'series_count': len(series), 'series_size': sum(i['size'] for i in series),
        'total_size': sum(i['size'] for i in inventory),
        'movie_top': sorted(movies, key=lambda i: i['size'], reverse=True)[:8],
        'series_top': sorted(series, key=lambda i: i['size'], reverse=True)[:8],
        'stale_large': sorted([i for i in inventory if i.get('plays', 0) > 0 and (i.get('stale_days') or 0) >= 180], key=lambda i: (i.get('size') or 0) * i.get('stale_days', 0), reverse=True)[:8],
        'never_watched': sorted([i for i in inventory if i.get('history_status') == 'unknown'], key=lambda i: i.get('size') or 0, reverse=True)[:8],
        'recently_watched': sorted([i for i in inventory if i.get('last_watched_at')], key=lambda i: i.get('last_watched_at'), reverse=True)[:8],
    }


def library_item_history_filter(item: dict[str, Any]):
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


def weekly_chart_payload(rows: list[Any], weeks: int = 26) -> dict[str, Any]:
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
    if max_hours < 10:
        ceiling = max(1, round(max_hours + 0.5, 1))
    elif max_hours < 100:
        ceiling = int(((max_hours + 9) // 10) * 10)
    else:
        ceiling = int(((max_hours + 49) // 50) * 50)
    return {'points': points, 'max': ceiling, 'half': ceiling / 2, 'weeks': weeks}


def json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def library_item_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {key: json_safe(value) for key, value in item.items()}


def history_session_payload(row: PlexSession) -> dict[str, Any]:
    return {
        'id': row.id,
        'source': row.source,
        'source_id': row.source_id,
        'plex_user_id': row.plex_user_id,
        'username': row.username,
        'title': row.title,
        'grandparent_title': row.grandparent_title,
        'parent_title': row.parent_title,
        'content_title': row.content_title,
        'rating_key': row.rating_key,
        'media_index': row.media_index,
        'parent_media_index': row.parent_media_index,
        'media_type': row.media_type,
        'started_at': json_safe(row.started_at),
        'stopped_at': json_safe(row.stopped_at),
        'watched_seconds': row.watched_seconds,
        'bandwidth_kbps': row.bandwidth_kbps,
        'bytes_streamed': row.bytes_streamed,
        'transcode_decision': row.transcode_decision,
        'player': row.player,
        'platform': row.platform,
        'ip_address': row.ip_address,
        'thumb_path': row.thumb_path,
        'reach': row.reach,
        'machine_id': row.machine_id,
    }


def library_item_detail(db: Session, item: dict[str, Any], inventory=None, viewer='') -> dict[str, Any]:
    if inventory is not None:
        titles, keys = history_index(db, inventory)
        target = inventory.index(item)
        ids = []
        for row in db.scalars(select(PlexSession).execution_options(yield_per=2000)):
            matches, ambiguous = match_history(row, inventory, titles, keys)
            if not ambiguous and target in matches:
                ids.append(row.id)
        # A single array bind avoids PostgreSQL's parameter limit for very popular titles.
        watch_filter = (PlexSession.id == any_(bindparam('matched_history_ids', value=ids, type_=ARRAY(Integer)))) if db.bind.dialect.name == 'postgresql' else PlexSession.id.in_(ids)
    else:
        watch_filter = library_item_history_filter(item)
    if viewer:
        watch_filter = watch_filter & (PlexSession.username == viewer)
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
    last_watched = max((r.started_at for r in rows), default=None)

    def day(value: datetime | None) -> str | None:
        return value.strftime('%Y-%m-%d') if value else None

    return {
        'item': library_item_payload(item),
        'history_rows': [{
            'row': history_session_payload(r),
            'display_title': media_display_title(r),
            'when_date': r.started_at.strftime('%Y-%m-%d'),
            'when_time': r.started_at.strftime('%H:%M'),
            'user': r.username,
            'title': media_display_title(r),
            'watched_minutes': round((r.watched_seconds or 0) / 60),
            'reach': r.reach or '-',
            'decision': r.transcode_decision or '-',
            'player': r.player or r.platform or '-',
        } for r in rows[:40]],
        'recent_rows': [history_session_payload(r) for r in rows[:8]],
        'user_rows': [{'username': r.username, 'plays': int(r.plays or 0), 'seconds': int(r.seconds or 0), 'hours': round(float(r.seconds or 0) / 3600, 1), 'last': day(r.last)} for r in user_rows],
        'device_rows': [{'device': r.device, 'plays': int(r.plays or 0), 'last': day(r.last)} for r in device_rows],
        'episode_rows': [{'title': r.title, 'parent_media_index': r.parent_media_index, 'media_index': r.media_index, 'plays': int(r.plays or 0), 'seconds': int(r.seconds or 0), 'hours': round(float(r.seconds or 0) / 3600, 1), 'last': day(r.last)} for r in episode_rows],
        'weekly_chart': weekly_chart_payload(weekly_rows) if weekly_rows else {'points': [], 'max': 1, 'half': 0.5, 'weeks': 26},
        'plays': int(plays or 0),
        'watch_hours': round(float(seconds or 0) / 3600, 1),
        'streamed_bytes': int(streamed or 0),
        'streamed_bytes_label': format_bytes(streamed),
        'transcodes': int(transcodes or 0),
        'remote_plays': int(remote_plays or 0),
        'users': int(users or 0),
        'devices': int(devices or 0),
        'first_watched': json_safe(min((r.started_at for r in rows), default=None)),
        'last_watched': json_safe(last_watched),
        'last_watched_at': last_watched.strftime('%Y-%m-%d %H:%M') if last_watched else None,
        'last_watched_label': item.get('last_watched_label') or relative_time(last_watched),
    }


async def browse_libraries_payload(db: Session, q: str = '', kind: str = 'all', source: str = 'all', values: dict[str, str] | None = None, **options) -> dict[str, Any]:
    cfg = values or all_settings()
    try:
        libs = await plex_libraries(cfg)
    except Exception:
        libs = []
    inventory, errors = await library_inventory(db, cfg, options.get('viewer', ''))
    result = browse_page(inventory, q, kind, source, **options)
    selected_item = selected_library_item(inventory, q, kind, source) if not options.get('preset') else None
    viewers = list(db.scalars(select(PlexSession.username).distinct().order_by(PlexSession.username)))
    return {
        **result, 'viewers': viewers,
        'coverage': 'Recorded playback sessions, not completed episodes. Missing/import-incomplete history cannot prove never watched. Ambiguous title identities are excluded from stale results. Active playback is excluded from the cleanup preset; live status can lag.',
        'libraries': [library_item_payload(lib) for lib in libs],
        'items': [library_item_payload(item) for item in result['items']],
        'q': q,
        'kind': kind,
        'source': source,
        'filters': result['filters'],
        'sources': sorted({i['source'] for i in inventory}),
        'stats': json_safe_library_stats(library_stats(inventory)),
        'errors': errors,
        'selected_item': library_item_payload(selected_item) if selected_item else None,
        'selected_detail': library_item_detail(db, selected_item, inventory, options.get('viewer', '')) if selected_item else None,
    }


def json_safe_library_stats(stats: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in stats.items():
        if isinstance(value, list):
            safe[key] = [library_item_payload(item) if isinstance(item, dict) else json_safe(item) for item in value]
        else:
            safe[key] = json_safe(value)
    for key in ('movies_size', 'series_size', 'total_size'):
        if key in stats:
            safe[f'{key}_label'] = format_bytes(stats[key])
    return safe


def _client_for_action(kind: str, source_index: int, values: dict[str, str] | None = None) -> dict[str, Any]:
    if kind == 'movie':
        clients = arr_clients('radarr', values)
    elif kind == 'series':
        clients = arr_clients('sonarr', values)
    else:
        raise ValueError(f'Unsupported library item kind: {kind}')
    return next(c for c in clients if c['index'] == source_index)


async def set_library_item_monitoring(kind: str, source_index: int, item_id: int, monitored: bool, values: dict[str, str] | None = None) -> dict[str, Any]:
    inst = _client_for_action(kind, source_index, values)
    if kind == 'movie':
        result = await inst['client'].set_movie_monitored(item_id, monitored=monitored)
    else:
        result = await inst['client'].set_series_monitored(item_id, monitored=monitored)
    return {'ok': True, 'kind': kind, 'source_index': source_index, 'source': inst['name'], 'item_id': item_id, 'monitored': monitored, 'result': result}


async def delete_library_item(kind: str, source_index: int, item_id: int, delete_files: bool = True, values: dict[str, str] | None = None) -> dict[str, Any]:
    inst = _client_for_action(kind, source_index, values)
    if kind == 'movie':
        result = await inst['client'].delete_movie(item_id, delete_files=delete_files)
    else:
        result = await inst['client'].delete_series(item_id, delete_files=delete_files)
    return {'ok': True, 'kind': kind, 'source_index': source_index, 'source': inst['name'], 'item_id': item_id, 'delete_files': delete_files, 'result': result}
