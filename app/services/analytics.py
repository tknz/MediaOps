from datetime import datetime, timedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from ..models import MediaRequest, PlexSession


def weekly_watch(db: Session, username: str | None = None, weeks: int = 26):
    since = datetime.utcnow() - timedelta(weeks=weeks)
    bucket = func.date_trunc('week', PlexSession.started_at)
    q = select(bucket.label('week'), func.sum(PlexSession.watched_seconds).label('seconds')).where(PlexSession.started_at >= since)
    if username:
        q = q.where(func.lower(PlexSession.username) == username.lower())
    return db.execute(q.group_by(bucket).order_by(bucket)).all()


def decision_breakdown(db: Session, username: str | None = None):
    since = datetime.utcnow() - timedelta(days=365)
    q = select(PlexSession.transcode_decision, func.count(PlexSession.id)).where(PlexSession.started_at >= since).group_by(PlexSession.transcode_decision)
    if username:
        q = q.where(func.lower(PlexSession.username) == username.lower())
    return db.execute(q).all()


def top_media(db: Session, username: str | None = None, media_type: str | None = None, limit: int = 10):
    q = select(PlexSession.title, func.sum(PlexSession.watched_seconds).label('seconds'), func.count(PlexSession.id).label('plays')).where(PlexSession.title.is_not(None))
    if username:
        q = q.where(func.lower(PlexSession.username) == username.lower())
    if media_type:
        q = q.where(PlexSession.media_type == media_type)
    return db.execute(q.group_by(PlexSession.title).order_by(func.sum(PlexSession.watched_seconds).desc()).limit(limit)).all()
