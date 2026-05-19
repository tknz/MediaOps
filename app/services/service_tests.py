from __future__ import annotations

import httpx


def _url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


async def test_service(kind: str, url: str, api_key: str | None = None, token: str | None = None) -> tuple[bool, str]:
    kind = (kind or '').lower().strip()
    url = (url or '').strip()
    api_key = (api_key or '').strip()
    token = (token or '').strip()
    if not url:
        return False, 'URL is required.'
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            if kind == 'plex':
                resp = await client.get(_url(url, '/identity'), headers={'X-Plex-Token': token or api_key})
                resp.raise_for_status()
                return True, 'Connected to Plex server.'
            if kind == 'seerr':
                headers = {'X-Api-Key': api_key} if api_key else {}
                resp = await client.get(_url(url, '/api/v1/status'), headers=headers)
                if resp.status_code == 404:
                    resp = await client.get(_url(url, '/api/v1/settings/public'), headers=headers)
                resp.raise_for_status()
                return True, 'Connected to Seerr.'
            if kind in {'radarr', 'sonarr'}:
                resp = await client.get(_url(url, '/api/v3/system/status'), headers={'X-Api-Key': api_key})
                resp.raise_for_status()
                data = resp.json()
                name = data.get('appName') or kind.title()
                version = data.get('version') or 'unknown version'
                return True, f'Connected to {name} {version}.'
            if kind == 'tautulli':
                resp = await client.get(_url(url, '/api/v2'), params={'apikey': api_key, 'cmd': 'get_server_info'})
                resp.raise_for_status()
                data = resp.json()
                if str(data.get('response', {}).get('result', '')).lower() == 'success':
                    return True, 'Connected to Tautulli API.'
                return False, 'Tautulli responded, but the API key was not accepted.'
            if kind == 'sabnzbd':
                resp = await client.get(_url(url, '/api'), params={'mode': 'version', 'apikey': api_key, 'output': 'json'})
                resp.raise_for_status()
                data = resp.json()
                return True, f"Connected to SABnzbd {data.get('version', '')}.".strip()
            return False, f'Unknown service type: {kind}'
    except httpx.HTTPStatusError as exc:
        return False, f'HTTP {exc.response.status_code}: check the URL and API key.'
    except Exception as exc:
        return False, f'Connection failed: {exc.__class__.__name__}'
