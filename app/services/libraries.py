from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from ..models import PlexSession
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


def enrich_inventory_usage(db: Session, inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not inventory:
        return inventory
    wanted = {inv_watch_key(item) for item in inventory if item.get('title')}
    if not wanted:
        return inventory
    rows = db.scalars(select(PlexSession).where(PlexSession.title.is_not(None)).order_by(PlexSession.started_at.desc()).limit(60000)).all()
    usage: dict[tuple[str, str], dict[str, Any]] = {}
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
        data = usage.get(inv_watch_key(item), {})
        last = data.get('last')
        item['plays'] = int(data.get('plays') or 0)
        item['watch_hours'] = round(float(data.get('seconds') or 0) / 3600, 1)
        item['streamed_bytes'] = int(data.get('bytes') or 0)
        item['last_watched_at'] = last
        item['last_watched_label'] = relative_time(last)
        item['stale_days'] = (datetime.utcnow() - last).days if last else 99999
        item['delete_score'] = (item.get('size') or 0) * (1 if item['plays'] == 0 else min(item['stale_days'], 730) / 730)
    return inventory


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


async def library_inventory(db: Session, values: dict[str, str] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
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
    enrich_inventory_usage(db, inventory)
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


def search_library_items(inventory: list[dict[str, Any]], q: str = '', kind: str = 'all', source: str = 'all', limit: int = 250) -> list[dict[str, Any]]:
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
        'stale_large': sorted([i for i in inventory if i.get('plays', 0) > 0 and i.get('stale_days', 0) >= 180], key=lambda i: (i.get('size') or 0) * i.get('stale_days', 0), reverse=True)[:8],
        'never_watched': sorted([i for i in inventory if i.get('plays', 0) == 0], key=lambda i: i.get('size') or 0, reverse=True)[:8],
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


def library_item_detail(db: Session, item: dict[str, Any]) -> dict[str, Any]:
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


async def browse_libraries_payload(db: Session, q: str = '', kind: str = 'all', source: str = 'all', values: dict[str, str] | None = None) -> dict[str, Any]:
    cfg = values or all_settings()
    try:
        libs = await plex_libraries(cfg)
    except Exception:
        libs = []
    inventory, errors = await library_inventory(db, cfg)
    selected_item = selected_library_item(inventory, q, kind, source)
    return {
        'libraries': [library_item_payload(lib) for lib in libs],
        'items': [library_item_payload(item) for item in search_library_items(inventory, q, kind, source)],
        'q': q,
        'kind': kind,
        'source': source,
        'filters': {'q': q, 'kind': kind, 'source': source},
        'sources': sorted({i['source'] for i in inventory}),
        'stats': json_safe_library_stats(library_stats(inventory)),
        'errors': errors,
        'selected_item': library_item_payload(selected_item) if selected_item else None,
        'selected_detail': library_item_detail(db, selected_item) if selected_item else None,
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
