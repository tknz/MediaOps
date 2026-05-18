from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import Session
import httpx
from .settings_store import all_settings
from ..models import MediaRequest

STATUS = {1: 'pending', 2: 'approved', 3: 'declined', 4: 'available', 5: 'available'}


def _headers(key: str):
    return {'X-Api-Key': key}


def _request_title(item: dict, movie_map: dict, series_map: dict):
    media = item.get('media') or {}
    arr_id = media.get('externalServiceId')
    if item.get('type') == 'movie' and arr_id in movie_map:
        return movie_map[arr_id]
    if item.get('type') == 'tv' and arr_id in series_map:
        return series_map[arr_id]
    title = media.get('title') or media.get('name')
    return title, None


async def sync_requests(db: Session) -> int:
    cfg = all_settings()
    if not all([cfg['seerr_url'], cfg['seerr_api_key'], cfg['radarr_url'], cfg['radarr_api_key'], cfg['sonarr_url'], cfg['sonarr_api_key']]):
        return 0
    changed = 0
    async with httpx.AsyncClient(timeout=60) as client:
        movie_map = {}
        series_map = {}
        try:
            movies = (await client.get(f"{cfg['radarr_url'].rstrip('/')}/api/v3/movie", headers=_headers(cfg['radarr_api_key']))).json()
            movie_map = {m.get('id'): (m.get('title'), ((m.get('movieFile') or {}).get('size'))) for m in movies if m.get('id') is not None}
        except Exception:
            movie_map = {}
        try:
            series = (await client.get(f"{cfg['sonarr_url'].rstrip('/')}/api/v3/series", headers=_headers(cfg['sonarr_api_key']))).json()
            series_map = {s.get('id'): (s.get('title'), ((s.get('statistics') or {}).get('sizeOnDisk'))) for s in series if s.get('id') is not None}
        except Exception:
            series_map = {}

        skip = 0
        take = 100
        while True:
            req_resp = await client.get(
                f"{cfg['seerr_url'].rstrip('/')}/api/v1/request",
                headers=_headers(cfg['seerr_api_key']),
                params={'take': take, 'skip': skip, 'sort': 'added'},
            )
            req_resp.raise_for_status()
            payload = req_resp.json()
            items = payload.get('results', [])
            for item in items:
                sid = str(item['id'])
                media = item.get('media') or {}
                title, fulfilled_bytes = _request_title(item, movie_map, series_map)
                title = title or f"{item.get('type','media')} #{sid}"
                requester = item.get('requestedBy') or {}
                requester_name = requester.get('plexUsername') or requester.get('username') or requester.get('displayName') or requester.get('email') or 'unknown'
                requester_plex_id = str(requester.get('plexId')) if requester.get('plexId') is not None else None
                seasons = item.get('seasons') or []
                season_numbers = ','.join(str(s.get('seasonNumber')) for s in seasons if s.get('seasonNumber') is not None) or None
                requested_at = datetime.fromisoformat(item['createdAt'].replace('Z','+00:00')).replace(tzinfo=None)
                row = db.scalar(select(MediaRequest).where(MediaRequest.source == 'seerr', MediaRequest.source_id == sid))
                if not row:
                    row = MediaRequest(source='seerr', source_id=sid, requester_name=requester_name, request_type=item.get('type','unknown'), title=title, status=STATUS.get(item.get('status'), str(item.get('status','unknown'))), requested_at=requested_at)
                    db.add(row)
                    changed += 1
                old = (row.title, row.fulfilled_bytes, row.requester_name, row.requester_plex_id, row.status)
                row.requester_plex_id = requester_plex_id
                row.requester_name = requester_name
                row.request_type = item.get('type','unknown')
                row.title = title
                row.seasons = season_numbers
                row.status = STATUS.get(item.get('status'), str(item.get('status','unknown')))
                row.requested_at = requested_at
                if fulfilled_bytes:
                    row.fulfilled_bytes = fulfilled_bytes
                if old != (row.title, row.fulfilled_bytes, row.requester_name, row.requester_plex_id, row.status):
                    changed += 1
            if len(items) < take:
                break
            skip += take
    db.commit()
    return changed
