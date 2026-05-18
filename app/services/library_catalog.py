from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PlexLibraryItem
from .clients import PlexClient, ServiceConfig
from .settings_store import all_settings


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value not in {None, ''} else None
    except Exception:
        return None


def _plex_epoch(value: Any) -> datetime | None:
    seconds = _int_or_none(value)
    return datetime.utcfromtimestamp(seconds) if seconds else None


def _catalog_key(item: dict[str, Any]) -> str | None:
    raw = item.get('key') or item.get('rating_key')
    return str(raw) if raw not in {None, ''} else None


def upsert_library_catalog_items(db: Session, rows: list[dict[str, Any]], library: dict[str, Any], seen_at: datetime | None = None) -> dict[str, int]:
    seen_at = seen_at or datetime.utcnow()
    result = {'seen': 0, 'created': 0, 'updated': 0, 'skipped': 0}
    for item in rows:
        key = _catalog_key(item)
        title = (item.get('title') or '').strip()
        media_type = item.get('type') or library.get('type')
        if not key or not title or media_type not in {'movie', 'show'}:
            result['skipped'] += 1
            continue
        result['seen'] += 1
        row = db.scalar(select(PlexLibraryItem).where(PlexLibraryItem.key == key))
        if not row:
            row = PlexLibraryItem(key=key, title=title, media_type=media_type)
            db.add(row)
            result['created'] += 1
        else:
            result['updated'] += 1
        row.guid = item.get('guid') or row.guid
        row.rating_key = item.get('rating_key') or row.rating_key
        row.title = title
        row.media_type = media_type
        row.year = _int_or_none(item.get('year'))
        row.thumb_path = item.get('thumb') or row.thumb_path
        row.library = item.get('library') or library.get('title') or row.library
        raw_library_key = library.get('key') or item.get('library_key') or row.library_key
        row.library_key = str(raw_library_key) if raw_library_key not in {None, ''} else None
        row.library_uuid = library.get('uuid') or row.library_uuid
        row.added_at = _plex_epoch(item.get('added_at')) or row.added_at
        row.updated_at = seen_at
        row.last_seen_at = seen_at
    return result


async def sync_plex_library_catalog(db: Session, values: dict[str, str] | None = None, page_size: int = 500) -> dict[str, Any]:
    cfg = values or all_settings()
    if not cfg.get('plex_server_url') or not cfg.get('plex_server_token'):
        return {'ok': False, 'message': 'Plex server is not configured.', 'seen': 0, 'created': 0, 'updated': 0, 'skipped': 0, 'libraries': []}
    client = PlexClient(ServiceConfig(url=cfg['plex_server_url'], token=cfg['plex_server_token']))
    libraries = [lib for lib in await client.libraries() if lib.get('type') in {'movie', 'show'}]
    totals = {'seen': 0, 'created': 0, 'updated': 0, 'skipped': 0}
    seen_at = datetime.utcnow()
    library_results = []
    for library in libraries:
        start = 0
        library_totals = {'seen': 0, 'created': 0, 'updated': 0, 'skipped': 0}
        while True:
            page = await client.library_items(str(library['key']), start=start, size=page_size)
            page_totals = upsert_library_catalog_items(db, page['items'], library, seen_at)
            for key, value in page_totals.items():
                totals[key] += value
                library_totals[key] += value
            start += page_size
            if start >= int(page.get('total') or 0) or not page['items']:
                break
        library_results.append({'key': library.get('key'), 'title': library.get('title'), 'type': library.get('type'), **library_totals})
    db.commit()
    return {'ok': True, 'message': f"Synced {totals['seen']} Plex library items.", **totals, 'libraries': library_results}
