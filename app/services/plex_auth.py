from dataclasses import dataclass
from urllib.parse import urlencode
import httpx
from ..config import settings

HEADERS = {
    'X-Plex-Product': settings.plex_product,
    'X-Plex-Client-Identifier': settings.plex_client_id,
    'X-Plex-Version': '0.1.0',
    'X-Plex-Platform': 'Web',
    'X-Plex-Platform-Version': '1.0',
    'X-Plex-Device': 'MediaManager Web',
    'X-Plex-Device-Name': settings.plex_product,
    'X-Plex-Model': 'bundled',
    'Accept': 'application/json',
}


@dataclass
class PlexPin:
    id: int
    code: str


async def create_pin() -> PlexPin:
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post('https://plex.tv/api/v2/pins', headers=HEADERS, params={'strong': 'true'})
        resp.raise_for_status()
        data = resp.json()
        return PlexPin(id=data['id'], code=data['code'])


async def fetch_pin(pin_id: int):
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f'https://plex.tv/api/v2/pins/{pin_id}', headers=HEADERS)
        resp.raise_for_status()
        return resp.json()


async def fetch_identity(token: str):
    headers = {**HEADERS, 'X-Plex-Token': token}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get('https://plex.tv/api/v2/user', headers=headers)
        resp.raise_for_status()
        return resp.json()


async def fetch_resources(token: str):
    headers = {**HEADERS, 'X-Plex-Token': token}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get('https://plex.tv/api/v2/resources', headers=headers, params={'includeHttps': 1, 'includeRelay': 1})
        resp.raise_for_status()
        return resp.json()


def choose_server(resources: list[dict]):
    servers = [r for r in resources if r.get('provides') and 'server' in r.get('provides', '')]
    if not servers:
        return None
    owned = [r for r in servers if r.get('owned')]
    return (owned or servers)[0]


def choose_connection(server: dict):
    conns = server.get('connections') or []
    if not conns:
        return None
    local = [c for c in conns if c.get('local')]
    secure = [c for c in conns if c.get('protocol') == 'https']
    return (local or secure or conns)[0]


def plex_auth_url(code: str, forward_url: str | None = None) -> str:
    params = {
        'clientID': settings.plex_client_id,
        'code': code,
        'context[device][product]': settings.plex_product,
        'context[device][version]': HEADERS['X-Plex-Version'],
        'context[device][platform]': HEADERS['X-Plex-Platform'],
        'context[device][platformVersion]': HEADERS['X-Plex-Platform-Version'],
        'context[device][device]': HEADERS['X-Plex-Device'],
        'context[device][deviceName]': HEADERS['X-Plex-Device-Name'],
        'context[device][model]': HEADERS['X-Plex-Model'],
        'context[device][environment]': 'bundled',
        'context[device][layout]': 'desktop',
    }
    if forward_url:
        params['forwardUrl'] = forward_url
    return f"https://app.plex.tv/auth#?{urlencode(params)}"
