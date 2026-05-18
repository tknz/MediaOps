from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_schema_extensions():
    from sqlalchemy import text
    with engine.begin() as conn:
        extensions = {
            'users': [
                'display_name VARCHAR(255)',
                'friendly_name VARCHAR(255)',
                'thumb_url VARCHAR(1024)',
                'plex_uuid VARCHAR(128)',
                'plex_title VARCHAR(255)',
                'plex_source VARCHAR(64)',
                'is_home BOOLEAN',
                'is_restricted BOOLEAN',
                'is_protected BOOLEAN',
                'allow_sync BOOLEAN',
                'allow_channels BOOLEAN',
                'allow_tuners BOOLEAN',
                'allow_camera_upload BOOLEAN',
                'allow_subtitle_admin BOOLEAN',
                'filter_all TEXT',
                'filter_movies TEXT',
                'filter_music TEXT',
                'filter_photos TEXT',
                'filter_television TEXT',
                'shared_server_id VARCHAR(128)',
                'shared_server_name VARCHAR(255)',
                'shared_last_seen_at TIMESTAMP',
                'shared_num_libraries INTEGER',
                'shared_all_libraries BOOLEAN',
                'shared_pending BOOLEAN',
                'shared_owned BOOLEAN',
                'last_plex_sync_at TIMESTAMP',
            ],
            'plex_sessions': [
                'grandparent_title VARCHAR(512)',
                'parent_title VARCHAR(512)',
                'content_title VARCHAR(512)',
                'rating_key VARCHAR(128)',
                'media_index INTEGER',
                'parent_media_index INTEGER',
                'ip_address VARCHAR(255)',
                'thumb_path VARCHAR(512)',
                'reach VARCHAR(32)',
                'machine_id VARCHAR(255)',
            ],
            'active_plex_sessions': [
                'grandparent_title VARCHAR(512)',
                'parent_title VARCHAR(512)',
                'content_title VARCHAR(512)',
                'media_index INTEGER',
                'parent_media_index INTEGER',
                'machine_id VARCHAR(255)',
                'thumb_path VARCHAR(512)',
                'library VARCHAR(255)',
                'player_address VARCHAR(255)',
                'remote_public_address VARCHAR(255)',
                'device VARCHAR(255)',
                'product VARCHAR(255)',
                'version VARCHAR(255)',
                'platform_version VARCHAR(255)',
                'local VARCHAR(16)',
                'secure VARCHAR(16)',
                'relayed VARCHAR(16)',
                'session_id VARCHAR(128)',
                'container VARCHAR(64)',
                'resolution VARCHAR(64)',
                'video_codec VARCHAR(64)',
                'audio_codec VARCHAR(64)',
                'file TEXT',
                'file_size BIGINT',
                'part_decision VARCHAR(32)',
                'audio_stream_title VARCHAR(255)',
                'duration_ms INTEGER',
            ],
            'user_watchlist_items': [
                'rating_key VARCHAR(128)',
                'summary TEXT',
                'source VARCHAR(64)',
            ],
            'plex_library_items': [
                'key VARCHAR(128)',
                'guid VARCHAR(255)',
                'rating_key VARCHAR(128)',
                'title VARCHAR(512)',
                'media_type VARCHAR(32)',
                'year INTEGER',
                'thumb_path VARCHAR(512)',
                'library VARCHAR(255)',
                'library_key VARCHAR(128)',
                'library_uuid VARCHAR(128)',
                'added_at TIMESTAMP',
                'updated_at TIMESTAMP',
                'last_seen_at TIMESTAMP',
            ],
            'active_download_items': [
                'source VARCHAR(64)',
                'title VARCHAR(512)',
                'status VARCHAR(128)',
                'quality VARCHAR(128)',
                'protocol VARCHAR(64)',
                'indexer VARCHAR(255)',
                'timeleft VARCHAR(128)',
                'size_bytes BIGINT',
                'size_left_bytes BIGINT',
                'progress FLOAT',
                'tracked_download_status VARCHAR(128)',
                'tracked_download_state VARCHAR(128)',
                'message TEXT',
                'download_id VARCHAR(255)',
                'last_seen_at TIMESTAMP',
            ],
        }
        for table, columns in extensions.items():
            for col in columns:
                try:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col}'))
                except Exception:
                    pass
