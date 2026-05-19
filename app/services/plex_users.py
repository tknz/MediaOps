from __future__ import annotations

from datetime import datetime
import xml.etree.ElementTree as ET

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import User, UserWatchlistItem
from .settings_store import all_settings, set_settings

PLEX_TV = 'https://plex.tv'
PLEX_METADATA = 'https://metadata.provider.plex.tv'


def _str(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _bool(value):
    if value is None:
        return None
    return str(value).lower() in {'1', 'true', 'yes'}


def _int(value):
    try:
        return int(value)
    except Exception:
        return None


def _dt_from_epoch(value):
    ivalue = _int(value)
    if not ivalue:
        return None
    try:
        return datetime.utcfromtimestamp(ivalue)
    except Exception:
        return None


def _date(value):
    value = _str(value)
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except Exception:
        return None


def _pick_username(payload: dict) -> str:
    return _str(payload.get('username')) or _str(payload.get('title')) or _str(payload.get('email')) or f"plex-{payload.get('plex_id') or payload.get('id')}"


def _find_user(db: Session, plex_id: str | None, username: str | None, email: str | None) -> User | None:
    if plex_id:
        user = db.scalar(select(User).where(User.plex_id == plex_id))
        if user:
            return user
    if username:
        user = db.scalar(select(User).where(func.lower(User.username) == username.lower()))
        if user:
            return user
    if email:
        return db.scalar(select(User).where(func.lower(User.email) == email.lower()))
    return None


def _apply(user: User, payload: dict) -> None:
    user.plex_id = _str(payload.get('plex_id')) or user.plex_id
    user.username = _pick_username(payload)
    user.email = _str(payload.get('email')) or user.email
    user.display_name = _str(payload.get('display_name')) or _str(payload.get('title')) or user.display_name
    user.friendly_name = _str(payload.get('friendly_name')) or user.friendly_name
    user.thumb_url = _str(payload.get('thumb_url')) or user.thumb_url
    user.plex_uuid = _str(payload.get('plex_uuid')) or user.plex_uuid
    user.plex_title = _str(payload.get('title')) or user.plex_title
    user.plex_source = _str(payload.get('source')) or user.plex_source
    for key in [
        'is_home', 'is_restricted', 'is_protected', 'allow_sync', 'allow_channels', 'allow_tuners',
        'allow_camera_upload', 'allow_subtitle_admin', 'shared_all_libraries', 'shared_pending', 'shared_owned'
    ]:
        if key in payload and payload[key] is not None:
            setattr(user, key, payload[key])
    for key in ['filter_all', 'filter_movies', 'filter_music', 'filter_photos', 'filter_television']:
        if key in payload:
            setattr(user, key, _str(payload.get(key)))
    for key in ['shared_server_id', 'shared_server_name']:
        if key in payload:
            setattr(user, key, _str(payload.get(key)) or getattr(user, key))
    if payload.get('shared_num_libraries') is not None:
        user.shared_num_libraries = payload.get('shared_num_libraries')
    if payload.get('shared_last_seen_at') is not None:
        user.shared_last_seen_at = payload.get('shared_last_seen_at')
    user.last_plex_sync_at = datetime.utcnow()


def _upsert_user(db: Session, payload: dict) -> User:
    plex_id = _str(payload.get('plex_id'))
    username = _pick_username(payload)
    email = _str(payload.get('email'))
    user = _find_user(db, plex_id, username, email)
    if not user:
        user = User(plex_id=plex_id or f'plex-{username}', username=username, email=email)
        db.add(user)
    _apply(user, payload)
    db.flush()
    return user


async def _get(client: httpx.AsyncClient, path: str, token: str, accept: str = 'application/xml'):
    response = await client.get(f"{PLEX_TV}{path}", headers={'X-Plex-Token': token, 'Accept': accept})
    response.raise_for_status()
    return response


async def _get_url(client: httpx.AsyncClient, url: str, token: str, accept: str = 'application/xml'):
    response = await client.get(url, headers={'X-Plex-Token': token, 'Accept': accept})
    response.raise_for_status()
    return response


def _watchlist_item_payload(node: ET.Element, user: User) -> dict:
    guid = _str(node.attrib.get('guid')) or _str(node.attrib.get('ratingKey')) or _str(node.attrib.get('key'))
    return {
        'plex_user_id': str(user.plex_id),
        'username': user.username,
        'guid': guid,
        'rating_key': _str(node.attrib.get('ratingKey')),
        'title': _str(node.attrib.get('title')) or _str(node.attrib.get('grandparentTitle')) or 'Unknown title',
        'media_type': _str(node.attrib.get('type')),
        'year': _int(node.attrib.get('year')),
        'thumb_url': _str(node.attrib.get('thumb')),
        'added_at': _dt_from_epoch(node.attrib.get('addedAt')),
        'originally_available_at': _date(node.attrib.get('originallyAvailableAt')),
        'summary': _str(node.attrib.get('summary')),
        'source': 'plex-watchlist',
    }


def _upsert_watchlist_item(db: Session, payload: dict) -> bool:
    if not payload.get('guid'):
        return False
    row = db.scalar(select(UserWatchlistItem).where(
        UserWatchlistItem.plex_user_id == payload['plex_user_id'],
        UserWatchlistItem.guid == payload['guid'],
    ))
    if not row:
        row = UserWatchlistItem(
            plex_user_id=payload['plex_user_id'],
            username=payload['username'],
            guid=payload['guid'],
            title=payload['title'],
        )
        db.add(row)
    for key in [
        'username', 'rating_key', 'title', 'media_type', 'year', 'thumb_url',
        'added_at', 'originally_available_at', 'summary', 'source',
    ]:
        setattr(row, key, payload.get(key))
    row.last_seen_at = datetime.utcnow()
    db.flush()
    return True


async def _refresh_owner_watchlist(db: Session, client: httpx.AsyncClient, token: str, owner: User | None) -> int:
    if not owner:
        return 0
    refresh_started = datetime.utcnow()
    try:
        root = ET.fromstring((await _get_url(client, f'{PLEX_METADATA}/library/sections/watchlist/all', token)).text)
    except Exception:
        return 0
    seen = 0
    for node in root:
        payload = _watchlist_item_payload(node, owner)
        if _upsert_watchlist_item(db, payload):
            seen += 1
    db.query(UserWatchlistItem).filter(
        UserWatchlistItem.plex_user_id == str(owner.plex_id),
        UserWatchlistItem.last_seen_at < refresh_started,
    ).delete(synchronize_session=False)
    return seen


async def refresh_plex_accounts(db: Session) -> dict[str, int]:
    cfg = all_settings()
    token = cfg.get('plex_server_token')
    machine_id = cfg.get('plex_machine_id')
    if not token:
        return {'users': 0, 'shared_servers': 0, 'watchlist_items': 0}

    count = 0
    shared_count = 0
    owner_user = None
    watchlist_count = 0
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            owner = (await _get(client, '/api/v2/user', token, 'application/json')).json()
            owner_payload = {
                'plex_id': _str(owner.get('id')), 'plex_uuid': _str(owner.get('uuid')),
                'username': _str(owner.get('username')), 'title': _str(owner.get('title')),
                'display_name': _str(owner.get('friendlyName')) or _str(owner.get('title')),
                'friendly_name': _str(owner.get('friendlyName')), 'email': _str(owner.get('email')),
                'thumb_url': _str(owner.get('thumb')), 'is_home': _bool(owner.get('home')), 'source': 'plex-owner',
            }
            user = _upsert_user(db, owner_payload)
            owner_id = _str(owner.get('id'))
            if owner_id and not cfg.get('plex_owner_id'):
                set_settings({'plex_owner_id': owner_id})
                cfg['plex_owner_id'] = owner_id
            if owner_id == cfg.get('plex_owner_id'):
                user.is_admin = True
            owner_user = user
            count += 1
        except Exception:
            pass

        try:
            root = ET.fromstring((await _get(client, '/api/users', token)).text)
            for node in root.findall('User'):
                a = node.attrib
                payload = {
                    'plex_id': _str(a.get('id')), 'username': _str(a.get('username')) or _str(a.get('title')),
                    'title': _str(a.get('title')), 'display_name': _str(a.get('title')), 'email': _str(a.get('email')),
                    'thumb_url': _str(a.get('thumb')), 'is_home': _bool(a.get('home')), 'is_restricted': _bool(a.get('restricted')),
                    'is_protected': _bool(a.get('protected')), 'allow_sync': _bool(a.get('allowSync')),
                    'allow_channels': _bool(a.get('allowChannels')), 'allow_tuners': _bool(a.get('allowTuners')),
                    'allow_camera_upload': _bool(a.get('allowCameraUpload')), 'allow_subtitle_admin': _bool(a.get('allowSubtitleAdmin')),
                    'filter_all': a.get('filterAll'), 'filter_movies': a.get('filterMovies'), 'filter_music': a.get('filterMusic'),
                    'filter_photos': a.get('filterPhotos'), 'filter_television': a.get('filterTelevision'), 'source': 'plex-friend',
                }
                server = node.find('Server')
                if server is not None:
                    s = server.attrib
                    payload.update({
                        'shared_server_id': _str(s.get('id')), 'shared_server_name': _str(s.get('name')),
                        'shared_last_seen_at': _dt_from_epoch(s.get('lastSeenAt')),
                        'shared_num_libraries': _int(s.get('numLibraries')), 'shared_all_libraries': _bool(s.get('allLibraries')),
                        'shared_pending': _bool(s.get('pending')), 'shared_owned': _bool(s.get('owned')),
                    })
                _upsert_user(db, payload)
                count += 1
        except Exception:
            pass

        if machine_id:
            try:
                root = ET.fromstring((await _get(client, f'/api/servers/{machine_id}/shared_servers', token)).text)
                for node in root.findall('SharedServer'):
                    a = node.attrib
                    sections = node.findall('Section')
                    payload = {
                        'plex_id': _str(a.get('userID')), 'username': _str(a.get('username')) or _str(a.get('email')),
                        'email': _str(a.get('email')), 'display_name': _str(a.get('username')) or _str(a.get('email')),
                        'allow_sync': _bool(a.get('allowSync')), 'shared_server_id': _str(a.get('id')),
                        'shared_server_name': _str(a.get('name')), 'shared_num_libraries': len(sections),
                        'shared_all_libraries': False if sections else None, 'shared_pending': _bool(a.get('pending')),
                        'source': 'plex-shared-server',
                    }
                    _upsert_user(db, payload)
                    shared_count += 1
            except Exception:
                pass

        watchlist_count += await _refresh_owner_watchlist(db, client, token, owner_user)

    db.commit()
    return {'users': count, 'shared_servers': shared_count, 'watchlist_items': watchlist_count}
