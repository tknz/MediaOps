from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import PlexSession


LOCAL_PREFIXES = ('10.', '192.168.', '172.16.', '172.17.', '172.18.', '172.19.', '172.2', '172.30.', '172.31.')


def _readonly_sqlite(path: str) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + '?mode=ro&immutable=1'
    return sqlite3.connect(uri, uri=True)


@dataclass
class TautulliImportResult:
    added: int = 0
    updated: int = 0
    seen: int = 0
    total: int = 0
    pages: int = 0
    message: str = ''


def _as_int(value: Any, default: int = 0) -> int:
    if value in (None, ''):
        return default
    try:
        return int(float(value))
    except Exception:
        return default


def _nullable_int(value: Any) -> int | None:
    if value in (None, ''):
        return None
    try:
        return int(float(value))
    except Exception:
        return None


def _as_float(value: Any) -> float | None:
    if value in (None, ''):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _dt_from_epoch(value: Any) -> datetime | None:
    ts = _as_int(value, 0)
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts)


def _reach(location: str | None, ip_address: str | None) -> str:
    loc = (location or '').lower()
    if loc in {'lan', 'local'}:
        return 'local'
    if loc in {'wan', 'remote'}:
        return 'remote'
    return 'local' if (ip_address or '').startswith(LOCAL_PREFIXES) else 'remote'


def _title(row: dict[str, Any]) -> str | None:
    return row.get('full_title') or row.get('title') or row.get('original_title')


def _content_title(row: dict[str, Any]) -> str | None:
    return row.get('title') or row.get('original_title') or row.get('full_title')


def _source_id(row: dict[str, Any]) -> str:
    return str(row.get('row_id') or row.get('id') or row.get('reference_id') or row.get('session_key') or '')


def _watched_seconds(row: dict[str, Any]) -> int:
    duration = _as_int(row.get('duration') or row.get('play_duration'), 0)
    if duration:
        return max(0, duration)
    started = _as_int(row.get('started') or row.get('date'), 0)
    stopped = _as_int(row.get('stopped'), 0)
    paused = _as_int(row.get('paused_counter'), 0)
    if started and stopped:
        return max(0, stopped - started - paused)
    return 0


def _bytes_streamed(row: dict[str, Any], watched: int, bandwidth_kbps: float | None) -> int | None:
    if row.get('bytes_streamed') not in (None, ''):
        return _as_int(row.get('bytes_streamed'), 0)
    if bandwidth_kbps:
        return int(bandwidth_kbps * 1000 / 8 * watched)
    # Tautulli's get_history API normally does not include bandwidth. Keep this
    # unknown rather than inventing traffic from media bitrate.
    return None


def _upsert_session(db: Session, row: dict[str, Any], source: str = 'tautulli') -> tuple[bool, bool]:
    source_id = _source_id(row)
    if not source_id:
        return False, False
    existing_id = db.scalar(select(PlexSession.id).where(PlexSession.source == source, PlexSession.source_id == source_id))
    watched = _watched_seconds(row)
    bandwidth = _as_float(row.get('bandwidth'))
    started_at = _dt_from_epoch(row.get('started') or row.get('date')) or datetime.utcnow()
    stopped_at = _dt_from_epoch(row.get('stopped')) or started_at
    bytes_streamed = _bytes_streamed(row, watched, bandwidth)
    values = {
        'plex_user_id': str(row.get('user_id') or ''),
        'username': row.get('user') or row.get('friendly_name') or 'unknown',
        'title': _title(row),
        'grandparent_title': row.get('grandparent_title'),
        'parent_title': row.get('parent_title'),
        'content_title': _content_title(row),
        'rating_key': str(row.get('rating_key') or row.get('reference_id') or '') or None,
        'media_index': _nullable_int(row.get('media_index')),
        'parent_media_index': _nullable_int(row.get('parent_media_index')),
        'media_type': row.get('media_type'),
        'started_at': started_at,
        'stopped_at': stopped_at,
        'watched_seconds': watched,
        'bandwidth_kbps': bandwidth,
        'bytes_streamed': bytes_streamed,
        'transcode_decision': row.get('transcode_decision'),
        'player': row.get('player') or row.get('product'),
        'platform': row.get('platform'),
        'ip_address': row.get('ip_address'),
        'machine_id': row.get('machine_id'),
        'thumb_path': row.get('grandparent_thumb') or row.get('parent_thumb') or row.get('thumb'),
        'reach': _reach(row.get('location'), row.get('ip_address')),
    }
    if existing_id:
        existing = db.get(PlexSession, existing_id)
        if not existing:
            return False, False
        changed = False
        for key, value in values.items():
            if value not in (None, '') and getattr(existing, key) in (None, ''):
                setattr(existing, key, value)
                changed = True
        # Keep these fresh if the older DB/API import had weaker values.
        for key in ('title', 'grandparent_title', 'parent_title', 'content_title', 'rating_key', 'media_index', 'parent_media_index', 'ip_address', 'thumb_path', 'machine_id', 'reach'):
            value = values.get(key)
            if value not in (None, '') and getattr(existing, key) != value:
                setattr(existing, key, value)
                changed = True
        return False, changed
    db.add(PlexSession(source=source, source_id=source_id, **values))
    return True, False


def import_tautulli(db: Session, path: str) -> int:
    if not path:
        return 0
    src = _readonly_sqlite(path)
    src.row_factory = sqlite3.Row
    metadata_columns = {r['name'] for r in src.execute('pragma table_info(session_history_metadata)').fetchall()}
    optional_metadata = []
    for col in ('rating_key', 'media_index', 'parent_media_index'):
        optional_metadata.append(f'md.{col}' if col in metadata_columns else f'NULL as {col}')
    rows = src.execute(f'''
        select h.id, h.user_id, h.user, h.started, h.stopped, coalesce(h.paused_counter,0) paused_counter,
               nullif(h.bandwidth,'') bandwidth, h.player, h.platform, h.ip_address, h.machine_id, h.media_type,
               m.transcode_decision, {', '.join(optional_metadata)}, md.title, md.full_title, md.parent_title, md.grandparent_title, md.thumb, md.parent_thumb, md.grandparent_thumb
        from session_history h
        join session_history_media_info m on m.id=h.id
        left join session_history_metadata md on md.id=h.id
        where h.stopped is not null
    ''').fetchall()
    added = 0
    for raw in rows:
        row = dict(raw)
        row['row_id'] = row.pop('id')
        ok, _ = _upsert_session(db, row)
        added += 1 if ok else 0
    db.commit()
    return added


async def import_tautulli_api(
    db: Session,
    url: str,
    api_key: str,
    *,
    full: bool = False,
    page_size: int = 1000,
    max_pages: int = 250,
) -> TautulliImportResult:
    url = (url or '').rstrip('/')
    api_key = (api_key or '').strip()
    if not url or not api_key:
        return TautulliImportResult(message='Tautulli URL and API key are required.')

    latest = None
    if not full:
        latest = db.scalar(select(func.max(PlexSession.started_at)).where(PlexSession.source == 'tautulli'))
    start_date = latest.strftime('%Y-%m-%d') if latest else None
    result = TautulliImportResult(message='No rows imported.')
    start = 0
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for page in range(max_pages):
            params = {
                'apikey': api_key,
                'cmd': 'get_history',
                'grouping': 0,
                'include_activity': 0,
                'order_column': 'date',
                'order_dir': 'asc',
                'start': start,
                'length': page_size,
            }
            if start_date:
                params['start_date'] = start_date
            resp = await client.get(f'{url}/api/v2', params=params)
            resp.raise_for_status()
            payload = resp.json().get('response', {})
            if str(payload.get('result', '')).lower() != 'success':
                return TautulliImportResult(message=payload.get('message') or 'Tautulli API did not return success.')
            data = payload.get('data') or {}
            rows = data.get('data') or []
            result.total = _as_int(data.get('recordsFiltered') or data.get('recordsTotal'), result.total)
            if not rows:
                break
            for row in rows:
                result.seen += 1
                added, updated = _upsert_session(db, row)
                result.added += 1 if added else 0
                result.updated += 1 if updated else 0
            db.commit()
            result.pages += 1
            start += len(rows)
            if len(rows) < page_size or (result.total and start >= result.total):
                break
    result.message = f'Imported {result.added} new Tautulli sessions; updated {result.updated}; scanned {result.seen} rows.'
    return result


async def enrich_tautulli_bandwidth(
    db: Session,
    url: str,
    api_key: str,
    *,
    limit: int = 20000,
    concurrency: int = 12,
) -> TautulliImportResult:
    """Backfill bandwidth from Tautulli get_stream_data for imported history rows.

    Tautulli get_history does not include the stream bitrate used by the UI's
    bandwidth graphs. get_stream_data exposes it per row, so this runs as a
    heavier explicit enrichment pass rather than hiding thousands of API calls
    inside the normal history import.
    """
    url = (url or '').rstrip('/')
    api_key = (api_key or '').strip()
    if not url or not api_key:
        return TautulliImportResult(message='Tautulli URL and API key are required.')
    rows = db.execute(
        select(PlexSession.id, PlexSession.source_id, PlexSession.watched_seconds)
        .where(PlexSession.source == 'tautulli', PlexSession.bytes_streamed.is_(None))
        .order_by(PlexSession.started_at.desc())
        .limit(limit)
    ).all()
    result = TautulliImportResult(seen=len(rows), total=len(rows), message='No Tautulli rows needed bandwidth enrichment.')
    if not rows:
        return result

    sem = __import__('asyncio').Semaphore(concurrency)

    async def fetch_one(client: httpx.AsyncClient, row):
        async with sem:
            try:
                resp = await client.get(f'{url}/api/v2', params={'apikey': api_key, 'cmd': 'get_stream_data', 'row_id': row.source_id})
                resp.raise_for_status()
                payload = resp.json().get('response', {})
                if str(payload.get('result', '')).lower() != 'success':
                    return row.id, None, None
                data = payload.get('data') or {}
                kbps = _as_float(data.get('stream_bitrate') or data.get('bitrate'))
                if not kbps:
                    return row.id, None, None
                bytes_streamed = int(kbps * 1000 / 8 * int(row.watched_seconds or 0))
                return row.id, kbps, bytes_streamed
            except Exception:
                return row.id, None, None

    import asyncio
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        tasks = [fetch_one(client, row) for row in rows]
        done = 0
        for coro in asyncio.as_completed(tasks):
            session_id, kbps, bytes_streamed = await coro
            done += 1
            if kbps and bytes_streamed is not None:
                session = db.get(PlexSession, session_id)
                if session:
                    session.bandwidth_kbps = kbps
                    session.bytes_streamed = bytes_streamed
                    result.updated += 1
            if done % 250 == 0:
                db.commit()
        db.commit()
    result.message = f'Enriched bandwidth for {result.updated} of {result.seen} Tautulli history rows.'
    return result
