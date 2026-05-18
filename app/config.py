from urllib.parse import quote_plus, urlencode

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')

    app_name: str = 'MediaOps'
    base_url: str = 'http://localhost:8000'
    secret_key: str = 'change-me'
    config_file: str = '/config/config.json'
    art_cache_dir: str = '/config/art-cache'
    database_url: str = ''
    db_host: str = 'db'
    db_port: int = 5432
    db_name: str = 'mediaops'
    db_user: str = 'mediaops'
    db_password: str = 'mediaops'
    db_sslmode: str = ''

    plex_client_id: str = 'mediaops-local'
    plex_product: str = 'MediaOps'
    plex_server_url: str = ''
    plex_server_token: str = ''
    plex_owner_id: str = ''

    seerr_url: str = ''
    seerr_api_key: str = ''
    tautulli_url: str = ''
    tautulli_api_key: str = ''
    sabnzbd_url: str = ''
    sabnzbd_api_key: str = ''
    radarr_url: str = ''
    radarr_api_key: str = ''
    radarr_instances: str = ''
    sonarr_url: str = ''
    sonarr_api_key: str = ''
    sonarr_instances: str = ''

    import_tautulli_db: str = ''
    import_seerr_db: str = ''
    import_radarr_db: str = ''
    import_sonarr_db: str = ''
    dev_bypass_auth: bool = False
    dev_user: str = 'admin'
    setup_no_auth: bool = True
    setup_user: str = 'setup'
    sync_interval_minutes: int = 60
    api_admin_token: str = ''
    api_tokens: str = ''

    @model_validator(mode='after')
    def build_database_url(self):
        if self.database_url:
            return self
        user = quote_plus(self.db_user)
        password = quote_plus(self.db_password)
        host = self.db_host.strip() or 'db'
        name = quote_plus(self.db_name)
        url = f'postgresql+psycopg://{user}:{password}@{host}:{self.db_port}/{name}'
        if self.db_sslmode:
            url = f'{url}?{urlencode({"sslmode": self.db_sslmode})}'
        self.database_url = url
        return self


settings = Settings()
