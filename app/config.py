from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')

    app_name: str = 'MediaManager'
    base_url: str = 'http://localhost:8000'
    secret_key: str = 'change-me'
    config_file: str = '/config/config.json'
    art_cache_dir: str = '/config/art-cache'
    database_url: str = 'postgresql+psycopg://mediamanager:mediamanager@db:5432/mediamanager'

    plex_client_id: str = 'mediamanager-local'
    plex_product: str = 'MediaManager'
    plex_server_url: str = ''
    plex_server_token: str = ''
    plex_owner_id: str = ''

    seerr_url: str = ''
    seerr_api_key: str = ''
    radarr_url: str = ''
    radarr_api_key: str = ''
    sonarr_url: str = ''
    sonarr_api_key: str = ''

    import_tautulli_db: str = ''
    import_seerr_db: str = ''
    import_radarr_db: str = ''
    import_sonarr_db: str = ''
    dev_bypass_auth: bool = False
    dev_user: str = 'admin'
    sync_interval_minutes: int = 60
    api_admin_token: str = ''
    api_tokens: str = ''


settings = Settings()
