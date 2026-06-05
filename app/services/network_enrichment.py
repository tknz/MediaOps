import socket
from functools import lru_cache
import time
import httpx


_ISP_CACHE_TTL_SECONDS = 3600
_isp_cache: dict[str, tuple[float, dict]] = {}


@lru_cache(maxsize=2048)
def reverse_dns(ip: str) -> str | None:
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return None


async def lookup_isp(ip: str) -> dict:
    if ip.startswith(('10.', '192.168.', '172.16.', '172.17.', '172.18.', '172.19.', '172.2', '172.30.', '172.31.')):
        return {'isp': 'Private network', 'org': None, 'as': None}
    cached = _isp_cache.get(ip)
    now = time.time()
    if cached and now - cached[0] < _ISP_CACHE_TTL_SECONDS:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f'https://ipwho.is/{ip}', params={'fields': 'success,connection'})
            resp.raise_for_status()
            data = resp.json()
            conn = data.get('connection') or {}
            result = {'isp': conn.get('isp'), 'org': conn.get('org'), 'as': conn.get('asn')}
    except Exception:
        result = {'isp': None, 'org': None, 'as': None}
    _isp_cache[ip] = (now, result)
    return result
