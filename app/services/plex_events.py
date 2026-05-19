from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import ActivePlexSession, PlexSession


def upsert_active_from_live(db: Session, data: dict) -> ActivePlexSession:
    now = datetime.utcnow()
    key = str(data.get('session_key') or data.get('session_id'))
    row = db.get(ActivePlexSession, key)
    values = {
        'plex_user_id': str(data.get('user_id')) if data.get('user_id') else None,
        'username': data.get('user') or 'unknown',
        'rating_key': str(data.get('rating_key')) if data.get('rating_key') else None,
        'title': data.get('display_title') or data.get('title'),
        'grandparent_title': data.get('grandparent_title'),
        'parent_title': data.get('parent_title'),
        'content_title': data.get('title'),
        'media_index': data.get('media_index'),
        'parent_media_index': data.get('parent_media_index'),
        'media_type': data.get('type'),
        'thumb_path': data.get('thumb'),
        'library': data.get('library'),
        'player': data.get('player'),
        'platform': data.get('platform'),
        'player_address': data.get('player_address'),
        'remote_public_address': data.get('remote_public_address'),
        'ip_address': data.get('remote_public_address') or data.get('player_address'),
        'machine_id': data.get('machine_identifier'),
        'device': data.get('device'),
        'product': data.get('product'),
        'version': data.get('version'),
        'platform_version': data.get('platform_version'),
        'local': data.get('local'),
        'secure': data.get('secure'),
        'relayed': data.get('relayed'),
        'session_id': data.get('session_id'),
        'bandwidth_kbps': data.get('bandwidth'),
        'container': data.get('container'),
        'resolution': data.get('resolution'),
        'video_codec': data.get('video_codec'),
        'audio_codec': data.get('audio_codec'),
        'file': data.get('file'),
        'file_size': data.get('file_size'),
        'part_decision': data.get('part_decision'),
        'audio_stream_title': data.get('audio_stream_title'),
        'transcode_decision': data.get('transcode_decision'),
        'last_view_offset_ms': data.get('view_offset') or 0,
        'duration_ms': data.get('duration'),
        'state': data.get('state'),
    }
    if not row:
        row = ActivePlexSession(
            session_key=key,
            started_at=now,
            last_seen_at=now,
            **values,
        )
        db.add(row)
    else:
        row.last_seen_at = now
        for field, value in values.items():
            if value is not None:
                setattr(row, field, value)
    db.commit()
    return row


def _history_source_id(active: ActivePlexSession) -> str:
    started = active.started_at.isoformat(timespec='seconds') if active.started_at else 'unknown-start'
    return f'{active.session_key}:{started}'


def finalize_session(db: Session, active: ActivePlexSession) -> PlexSession | None:
    stopped = datetime.utcnow()
    watched = max(0, int((stopped - active.started_at).total_seconds()) - int(active.paused_seconds or 0))
    source_id = _history_source_id(active)
    existing = db.scalar(select(PlexSession).where(PlexSession.source == 'plex-live', PlexSession.source_id == source_id))
    if existing:
        db.delete(active)
        db.commit()
        return existing
    session = PlexSession(
        source='plex-live',
        source_id=source_id,
        plex_user_id=active.plex_user_id or 'unknown',
        username=active.username,
        title=active.title,
        grandparent_title=active.grandparent_title,
        parent_title=active.parent_title,
        content_title=active.content_title,
        rating_key=active.rating_key,
        media_index=active.media_index,
        parent_media_index=active.parent_media_index,
        media_type=active.media_type,
        started_at=active.started_at,
        stopped_at=stopped,
        watched_seconds=watched,
        bandwidth_kbps=active.bandwidth_kbps,
        bytes_streamed=int(active.bandwidth_kbps * 1000 / 8 * watched) if active.bandwidth_kbps else None,
        transcode_decision=active.transcode_decision,
        player=active.player,
        platform=active.platform,
        ip_address=active.ip_address,
        thumb_path=active.thumb_path,
        machine_id=active.machine_id,
    )
    db.add(session)
    db.delete(active)
    db.commit()
    return session



def reconcile_live_sessions(db: Session, sessions: list[dict]) -> tuple[int, int]:
    seen = set()
    for data in sessions:
        row = upsert_active_from_live(db, data)
        seen.add(row.session_key)
    finalized = 0
    for active in db.scalars(select(ActivePlexSession)).all():
        if active.session_key not in seen:
            finalize_session(db, active)
            finalized += 1
    return len(seen), finalized
