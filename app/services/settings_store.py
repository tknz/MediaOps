from __future__ import annotations

from pathlib import Path
import json
import os
import shlex
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
    'job_downloads_seconds': '30',
    'job_plex_accounts_minutes': '60',
    'job_requests_minutes': str(settings.sync_interval_minutes),
    'local_auth_username': settings.local_auth_username,
    'local_auth_password_hash': settings.local_auth_password_hash,
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
    'local_auth_username': settings.local_auth_username,
    'local_auth_password_hash': settings.local_auth_password_hash,
}

CONFIG_KEYS = {
    'MEDIA_SERVER_TYPE': 'media_server_type',
    'PLEX_SERVER_URL': 'plex_server_url',
    'PLEX_SERVER_TOKEN': 'plex_server_token',
    'PLEX_OWNER_ID': 'plex_owner_id',
    'PLEX_MACHINE_ID': 'plex_machine_id',
    'PLEX_SERVER_NAME': 'plex_server_name',
    'SEERR_URL': 'seerr_url',
    'SEERR_API_KEY': 'seerr_api_key',
    'TAUTULLI_URL': 'tautulli_url',
    'TAUTULLI_API_KEY': 'tautulli_api_key',
    'SABNZBD_URL': 'sabnzbd_url',
    'SABNZBD_API_KEY': 'sabnzbd_api_key',
    'RADARR_URL': 'radarr_url',
    'RADARR_API_KEY': 'radarr_api_key',
    'SONARR_URL': 'sonarr_url',
    'SONARR_API_KEY': 'sonarr_api_key',
    'JOB_PLEX_LIVE_SECONDS': 'job_plex_live_seconds',
    'JOB_DOWNLOADS_SECONDS': 'job_downloads_seconds',
    'JOB_PLEX_ACCOUNTS_MINUTES': 'job_plex_accounts_minutes',
    'JOB_REQUESTS_MINUTES': 'job_requests_minutes',
    'LOCAL_AUTH_USERNAME': 'local_auth_username',
    'LOCAL_AUTH_PASSWORD_HASH': 'local_auth_password_hash',
}

WRITE_ORDER = [
    'media_server_type',
    'plex_server_name',
    'plex_server_url',
    'plex_server_token',
    'plex_owner_id',
    'plex_machine_id',
    'seerr_url',
    'seerr_api_key',
    'tautulli_url',
    'tautulli_api_key',
    'sabnzbd_url',
    'sabnzbd_api_key',
    'radarr_instances',
    'sonarr_instances',
    'job_plex_live_seconds',
    'job_downloads_seconds',
    'job_plex_accounts_minutes',
    'job_requests_minutes',
    'local_auth_username',
    'local_auth_password_hash',
]

_LOCK = Lock()


def _path() -> Path:
    path = Path(settings.config_file)
    if path.suffix.lower() == '.json':
        return path.with_suffix('.env')
    return path


def _legacy_json_path() -> Path:
    configured_path = Path(settings.config_file)
    if configured_path.suffix.lower() == '.json':
        return configured_path
    return configured_path.with_suffix('.json')


def _parse_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].strip()
        key, sep, value = line.partition('=')
        if not sep:
            continue
        key = key.strip().upper()
        stripped = value.strip()
        if stripped.startswith(('"', "'")):
            try:
                parsed = shlex.split(stripped, comments=False, posix=True)
                values[key] = parsed[0] if parsed else ''
            except ValueError:
                values[key] = stripped.strip('"').strip("'")
        else:
            values[key] = stripped
    return values


def _quote(value: str) -> str:
    text = str(value or '')
    if not text or any(ch.isspace() for ch in text) or any(ch in text for ch in '#"\'\\$`='):
        return json.dumps(text)
    return text


def _decode_instances(raw: str) -> list[dict]:
    try:
        rows = json.loads(raw or '[]')
    except Exception:
        rows = []
    return [row for row in rows if isinstance(row, dict) and (row.get('name') or row.get('url') or row.get('api_key'))]


def _read_instances(raw: dict[str, str], prefix: str) -> list[dict]:
    instances = []
    for idx in range(1, 20):
        name = raw.get(f'{prefix}_{idx}_NAME', '')
        url = raw.get(f'{prefix}_{idx}_URL', '')
        key = raw.get(f'{prefix}_{idx}_API_KEY', '')
        if name or url or key:
            instances.append({'name': name or prefix.title(), 'url': url, 'api_key': key})
    return instances


def _apply_instances(values: dict[str, str], raw: dict[str, str], prefix: str, key: str) -> None:
    instances = _read_instances(raw, prefix)
    if instances:
        values[key] = json.dumps(instances)
        values[f'{prefix.lower()}_url'] = instances[0].get('url', '')
        values[f'{prefix.lower()}_api_key'] = instances[0].get('api_key', '')


def _migrate_legacy_json() -> None:
    path = _path()
    legacy = _legacy_json_path()
    if path.exists() or not legacy.exists():
        return
    try:
        raw = json.loads(legacy.read_text())
    except Exception:
        return
    values = {k: str(v or '') for k, v in raw.items() if k in DEFAULTS}
    _write_config(values, path)
    try:
        legacy.rename(legacy.with_suffix(legacy.suffix + '.migrated'))
    except Exception:
        pass


def _config_values() -> dict[str, str]:
    _migrate_legacy_json()
    path = _path()
    if not path.exists():
        return {}
    raw = _parse_env_text(path.read_text())
    values = {name: raw[key] for key, name in CONFIG_KEYS.items() if key in raw and name in DEFAULTS}
    _apply_instances(values, raw, 'RADARR', 'radarr_instances')
    _apply_instances(values, raw, 'SONARR', 'sonarr_instances')
    return values


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


def _write_instance_lines(lines: list[str], prefix: str, raw: str) -> None:
    instances = _decode_instances(raw)
    if not instances:
        return
    lines.append(f'# {prefix.title()} instances')
    for idx, row in enumerate(instances, 1):
        lines.append(f'{prefix}_{idx}_NAME={_quote(row.get("name") or prefix.title())}')
        lines.append(f'{prefix}_{idx}_URL={_quote(row.get("url") or "")}')
        lines.append(f'{prefix}_{idx}_API_KEY={_quote(row.get("api_key") or "")}')


def _write_config(values: dict[str, str], path: Path) -> None:
    lines = [
        '# MediaOps config',
        '# Plain KEY=value file. Environment variables still override these values.',
    ]
    for key in WRITE_ORDER:
        if key not in DEFAULTS or key not in values:
            continue
        value = str(values.get(key) or '')
        if key == 'radarr_instances':
            _write_instance_lines(lines, 'RADARR', value)
            continue
        if key == 'sonarr_instances':
            _write_instance_lines(lines, 'SONARR', value)
            continue
        env_key = next((env for env, name in CONFIG_KEYS.items() if name == key), key.upper())
        lines.append(f'{env_key}={_quote(value)}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines).rstrip() + '\n')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def set_settings(values: dict[str, str]) -> None:
    with _LOCK:
        owned = env_owned()
        merged = _config_values()
        merged.update({k: v or '' for k, v in values.items() if k in DEFAULTS and k not in owned})
        for key in owned:
            merged.pop(key, None)
        _write_config(merged, _path())


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
