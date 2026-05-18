from pathlib import Path
import json
from threading import Lock
from ..config import settings

DEFAULTS = {
    'media_server_type': 'plex',
    'plex_server_url': settings.plex_server_url,
    'plex_server_token': settings.plex_server_token,
    'plex_owner_id': settings.plex_owner_id,
    'plex_machine_id': '',
    'plex_server_name': '',
    'seerr_url': settings.seerr_url or 'http://seerr:5055',
    'seerr_api_key': settings.seerr_api_key,
    'tautulli_url': settings.tautulli_url or 'http://tautulli:8181',
    'tautulli_api_key': settings.tautulli_api_key,
    'sabnzbd_url': settings.sabnzbd_url or 'http://sabnzbd:8080',
    'sabnzbd_api_key': settings.sabnzbd_api_key,
    'radarr_url': settings.radarr_url or 'http://radarr:7878',
    'radarr_api_key': settings.radarr_api_key,
    'sonarr_url': settings.sonarr_url or 'http://sonarr:8989',
    'sonarr_api_key': settings.sonarr_api_key,
    'radarr_instances': settings.radarr_instances or '[]',
    'sonarr_instances': settings.sonarr_instances or '[]',
    'job_plex_live_seconds': '30',
    'job_plex_accounts_minutes': '60',
    'job_requests_minutes': str(settings.sync_interval_minutes),
}
ENV_OVERRIDES = {
    'plex_server_url': settings.plex_server_url,
    'plex_server_token': settings.plex_server_token,
    'plex_owner_id': settings.plex_owner_id,
    'seerr_url': settings.seerr_url,
    'seerr_api_key': settings.seerr_api_key,
    'tautulli_url': settings.tautulli_url,
    'tautulli_api_key': settings.tautulli_api_key,
    'sabnzbd_url': settings.sabnzbd_url,
    'sabnzbd_api_key': settings.sabnzbd_api_key,
    'radarr_url': settings.radarr_url,
    'radarr_api_key': settings.radarr_api_key,
    'radarr_instances': settings.radarr_instances,
    'sonarr_url': settings.sonarr_url,
    'sonarr_api_key': settings.sonarr_api_key,
    'sonarr_instances': settings.sonarr_instances,
}
_LOCK = Lock()


def _path() -> Path:
    return Path(settings.config_file)


def _config_values() -> dict[str, str]:
    path = _path()
    if not path.exists():
        return {}
    try:
        values = json.loads(path.read_text())
    except Exception:
        return {}
    return {k: v for k, v in values.items() if k in DEFAULTS}


def env_owned() -> set[str]:
    return {k for k, v in ENV_OVERRIDES.items() if v}


def settings_sources() -> dict[str, str]:
    config_keys = set(_config_values())
    env_keys = env_owned()
    return {
        key: 'env' if key in env_keys else ('config' if key in config_keys else 'default')
        for key in DEFAULTS
    }


def all_settings() -> dict[str, str]:
    values = dict(DEFAULTS)
    values.update(_config_values())
    values.update({k: v for k, v in ENV_OVERRIDES.items() if v})
    return values


def set_settings(values: dict[str, str]) -> None:
    with _LOCK:
        owned = env_owned()
        merged = _config_values()
        merged.update({k: v or '' for k, v in values.items() if k in DEFAULTS and k not in owned})
        for key in owned:
            merged.pop(key, None)
        merged['_sources'] = {key: 'env' for key in sorted(owned)}
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merged, indent=2, sort_keys=True) + '\n')


def configured() -> bool:
    values = all_settings()
    return bool(values['plex_server_url'] and values['plex_server_token'])


def media_server_label() -> str:
    values = all_settings()
    kind = (values.get('media_server_type') or 'plex').strip().lower()
    name = (values.get('plex_server_name') or '').strip()
    if name:
        return name
    return 'Plex' if kind == 'plex' else kind.title()
