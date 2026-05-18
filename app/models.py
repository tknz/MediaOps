from datetime import datetime, date
from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base


class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(primary_key=True)
    plex_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(255), index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    friendly_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    thumb_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    plex_uuid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    plex_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    plex_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_home: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_restricted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_protected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    allow_sync: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    allow_channels: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    allow_tuners: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    allow_camera_upload: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    allow_subtitle_admin: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    filter_all: Mapped[str | None] = mapped_column(Text, nullable=True)
    filter_movies: Mapped[str | None] = mapped_column(Text, nullable=True)
    filter_music: Mapped[str | None] = mapped_column(Text, nullable=True)
    filter_photos: Mapped[str | None] = mapped_column(Text, nullable=True)
    filter_television: Mapped[str | None] = mapped_column(Text, nullable=True)
    shared_server_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    shared_server_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    shared_last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    shared_num_libraries: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shared_all_libraries: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    shared_pending: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    shared_owned: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_plex_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UserWatchlistItem(Base):
    __tablename__ = 'user_watchlist_items'
    __table_args__ = (UniqueConstraint('plex_user_id', 'guid', name='uq_user_watchlist_guid'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    plex_user_id: Mapped[str] = mapped_column(String(64), index=True)
    username: Mapped[str] = mapped_column(String(255), index=True)
    guid: Mapped[str] = mapped_column(String(255), index=True)
    rating_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(String(512))
    media_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumb_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    added_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    originally_available_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(64), default='plex-watchlist')
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class PlexLibraryItem(Base):
    __tablename__ = 'plex_library_items'
    __table_args__ = (UniqueConstraint('key', name='uq_plex_library_item_key'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    guid: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    rating_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(512), index=True)
    media_type: Mapped[str] = mapped_column(String(32), index=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumb_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    library: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    library_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    library_uuid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    added_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class PlexSession(Base):
    __tablename__ = 'plex_sessions'
    __table_args__ = (UniqueConstraint('source', 'source_id', name='uq_plex_source_session'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), default='plex')
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    plex_user_id: Mapped[str] = mapped_column(String(64), index=True)
    username: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    grandparent_title: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    parent_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    rating_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    media_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parent_media_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    stopped_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    watched_seconds: Mapped[int] = mapped_column(Integer)
    bandwidth_kbps: Mapped[float | None] = mapped_column(Float, nullable=True)
    bytes_streamed: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    transcode_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    player: Mapped[str | None] = mapped_column(String(255), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    thumb_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    reach: Mapped[str | None] = mapped_column(String(32), nullable=True)
    machine_id: Mapped[str | None] = mapped_column(String(255), nullable=True)


class MediaRequest(Base):
    __tablename__ = 'media_requests'
    __table_args__ = (UniqueConstraint('source', 'source_id', name='uq_request_source_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), default='seerr')
    source_id: Mapped[str] = mapped_column(String(128), index=True)
    requester_plex_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    requester_name: Mapped[str] = mapped_column(String(255), index=True)
    request_type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(512))
    seasons: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(64))
    requested_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    fulfilled_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class LibrarySnapshot(Base):
    __tablename__ = 'library_snapshots'
    __table_args__ = (UniqueConstraint('snapshot_date', 'section', name='uq_library_snapshot_day_section'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, index=True)
    section: Mapped[str] = mapped_column(String(128))
    bytes_used: Mapped[int] = mapped_column(BigInteger)


class AppSetting(Base):
    __tablename__ = 'app_settings'
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ActivePlexSession(Base):
    __tablename__ = 'active_plex_sessions'
    session_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    plex_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    username: Mapped[str] = mapped_column(String(255), index=True)
    rating_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    grandparent_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    parent_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    media_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parent_media_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    thumb_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    library: Mapped[str | None] = mapped_column(String(255), nullable=True)
    player: Mapped[str | None] = mapped_column(String(255), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(255), nullable=True)
    player_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remote_public_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    machine_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    device: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product: Mapped[str | None] = mapped_column(String(255), nullable=True)
    version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    platform_version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    local: Mapped[str | None] = mapped_column(String(16), nullable=True)
    secure: Mapped[str | None] = mapped_column(String(16), nullable=True)
    relayed: Mapped[str | None] = mapped_column(String(16), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bandwidth_kbps: Mapped[float | None] = mapped_column(Float, nullable=True)
    container: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(64), nullable=True)
    video_codec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    audio_codec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    part_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    audio_stream_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    transcode_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime)
    last_view_offset_ms: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paused_seconds: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str | None] = mapped_column(String(32), nullable=True)


class UserPolicy(Base):
    __tablename__ = 'user_policies'
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(255), index=True, unique=True)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    block_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_concurrent_streams: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_public_ips: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PolicyActionLog(Base):
    __tablename__ = 'policy_action_log'
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(255), index=True)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class UserBlock(Base):
    __tablename__ = 'user_blocks'
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(255), index=True)
    block_type: Mapped[str] = mapped_column(String(32), index=True)  # ip or device
    value: Mapped[str] = mapped_column(String(255), index=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
