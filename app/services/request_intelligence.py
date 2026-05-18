from __future__ import annotations

from collections import Counter
from datetime import datetime
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import MediaRequest
from .clients import RadarrClient, SeerrClient, SonarrClient


PENDING_STATUSES = {1, '1', 'pending', 'requested', 'approval_needed', 'approval-needed'}
STATUS_LABELS = {
    1: 'pending',
    2: 'approved',
    3: 'declined',
    4: 'available',
    5: 'available',
    '1': 'pending',
    '2': 'approved',
    '3': 'declined',
    '4': 'available',
    '5': 'available',
}


def _status(value) -> str:
    return STATUS_LABELS.get(value, str(value or 'unknown').lower())


def _is_pending(value) -> bool:
    return _status(value) in PENDING_STATUSES or value in PENDING_STATUSES


def _request_results(payload) -> list[dict]:
    if isinstance(payload, dict):
        rows = payload.get('results') or payload.get('requests') or []
        return rows if isinstance(rows, list) else []
    return payload if isinstance(payload, list) else []


def _requester(item: dict) -> dict:
    user = item.get('requestedBy') or item.get('requester') or {}
    name = user.get('displayName') or user.get('plexUsername') or user.get('username') or user.get('email') or 'unknown'
    return {
        'id': user.get('id'),
        'plex_id': str(user.get('plexId')) if user.get('plexId') is not None else None,
        'name': name,
        'email': user.get('email'),
    }


def _title(item: dict) -> str:
    media = item.get('media') or {}
    return (
        media.get('title') or media.get('name') or media.get('originalTitle') or
        item.get('title') or item.get('name') or
        f"{item.get('type') or 'media'} #{item.get('mediaId') or item.get('id') or 'unknown'}"
    )


def _seasons(item: dict):
    seasons = item.get('seasons') or item.get('requestedSeasons') or []
    values = []
    for row in seasons:
        if isinstance(row, dict):
            num = row.get('seasonNumber') or row.get('season') or row.get('number')
        else:
            num = row
        if num is not None:
            values.append(int(num) if str(num).isdigit() else num)
    return values


def _requested_at(item: dict):
    raw = item.get('createdAt') or item.get('updatedAt')
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace('Z', '+00:00')).isoformat()
    except Exception:
        return raw


def _quality_name(release: dict):
    quality = release.get('quality') or {}
    if isinstance(quality, dict):
        inner = quality.get('quality') or quality
        if isinstance(inner, dict):
            return inner.get('name')
    return None


def _release_size(release: dict) -> int:
    try:
        return int(release.get('size') or 0)
    except Exception:
        return 0


def _release_estimate(releases: list[dict], source: str) -> dict:
    sizes = [size for size in (_release_size(r) for r in releases) if size > 0]
    qualities = [_quality_name(r) for r in releases]
    qualities = [q for q in qualities if q]
    estimate = int(median(sizes)) if sizes else None
    return {
        'estimate_bytes': estimate,
        'estimate_gb': round(estimate / 1_000_000_000, 2) if estimate else None,
        'quality': Counter(qualities).most_common(1)[0][0] if qualities else None,
        'release_count': len(releases),
        'source': source,
    }


def _empty_estimate(source: str) -> dict:
    return {
        'estimate_bytes': None,
        'estimate_gb': None,
        'quality': None,
        'release_count': 0,
        'source': source,
    }


async def _movie_estimate(title: str, radarr: RadarrClient | None, movie_map: dict) -> dict:
    if not radarr:
        return _empty_estimate('Radarr not configured')
    movie = movie_map.get(title.lower().strip())
    if not movie or not movie.get('id'):
        return _empty_estimate('No exact Radarr title match; no release search run')
    try:
        releases = await radarr.releases(int(movie['id']))
        return _release_estimate(releases if isinstance(releases, list) else [], 'Radarr release search exact title match')
    except Exception as exc:
        return _empty_estimate(f'Radarr release search unavailable: {exc.__class__.__name__}')


async def _tv_estimate(title: str, seasons: list, sonarr: SonarrClient | None, series_map: dict) -> dict:
    if not sonarr:
        return _empty_estimate('Sonarr not configured')
    series = series_map.get(title.lower().strip())
    if not series or not series.get('id'):
        return _empty_estimate('No exact Sonarr title match; no release search run')
    try:
        episodes = await sonarr.episodes(int(series['id']))
        season_set = {int(s) for s in seasons if str(s).isdigit()}
        scoped = [
            e for e in (episodes if isinstance(episodes, list) else [])
            if not season_set or int(e.get('seasonNumber') or -1) in season_set
        ]
        sample = [e for e in scoped if e.get('id')][:3]
        releases = []
        for episode in sample:
            rows = await sonarr.releases(int(episode['id']))
            if isinstance(rows, list):
                releases.extend(rows)
        estimate = _release_estimate(releases, f'Sonarr release search sample ({len(sample)} of {len(scoped)} episodes)')
        if estimate['estimate_bytes'] and scoped and sample:
            estimate['estimate_bytes'] = int(estimate['estimate_bytes'] * len(scoped))
            estimate['estimate_gb'] = round(estimate['estimate_bytes'] / 1_000_000_000, 2)
        return estimate
    except Exception as exc:
        return _empty_estimate(f'Sonarr release search unavailable: {exc.__class__.__name__}')


async def pending_request_payload(
    db: Session,
    seerr: SeerrClient,
    radarr: RadarrClient | None = None,
    sonarr: SonarrClient | None = None,
) -> dict:
    payload = await seerr.requests(take=100, skip=0)
    items = [item for item in _request_results(payload) if _is_pending(item.get('status'))]
    source_ids = [str(item.get('id')) for item in items if item.get('id') is not None]
    local_rows = {}
    if source_ids:
        rows = db.scalars(select(MediaRequest).where(MediaRequest.source == 'seerr', MediaRequest.source_id.in_(source_ids))).all()
        local_rows = {str(row.source_id): row for row in rows}

    movie_map = {}
    series_map = {}
    if radarr:
        try:
            movie_map = {(m.get('title') or '').lower().strip(): m for m in await radarr.movies() if m.get('title')}
        except Exception:
            movie_map = {}
    if sonarr:
        try:
            series_map = {(s.get('title') or '').lower().strip(): s for s in await sonarr.series() if s.get('title')}
        except Exception:
            series_map = {}

    enriched = []
    for item in items:
        source_id = str(item.get('id'))
        local = local_rows.get(source_id)
        title = _title(item)
        request_type = item.get('type') or (local.request_type if local else 'unknown')
        seasons = _seasons(item)
        estimate = (
            await _movie_estimate(title, radarr, movie_map)
            if request_type == 'movie'
            else await _tv_estimate(title, seasons, sonarr, series_map)
        )
        enriched.append({
            'id': item.get('id'),
            'source_id': source_id,
            'local_id': local.id if local else None,
            'requester': _requester(item),
            'title': title,
            'type': request_type,
            'seasons': seasons,
            'status': _status(item.get('status')),
            'raw_status': item.get('status'),
            'requested_at': _requested_at(item),
            'fulfilled_bytes': local.fulfilled_bytes if local else None,
            'estimate': estimate,
        })
    return {'requests': enriched, 'count': len(enriched)}
