from pathlib import Path
import hashlib
import httpx
from ..config import settings
from .settings_store import all_settings


def cache_path_for(plex_path: str) -> Path:
    digest = hashlib.sha1(plex_path.encode()).hexdigest()
    return Path(settings.art_cache_dir) / f'{digest}.jpg'


async def ensure_art_cached(plex_path: str) -> str | None:
    if not plex_path:
        return None
    out = cache_path_for(plex_path)
    if out.exists() and out.stat().st_size > 0:
        return out.name
    cfg = all_settings()
    if not cfg.get('plex_server_url') or not cfg.get('plex_server_token'):
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{cfg['plex_server_url'].rstrip('/')}{plex_path}", headers={'X-Plex-Token': cfg['plex_server_token']})
        if resp.status_code == 200:
            out.write_bytes(resp.content)
            return out.name
    return None
