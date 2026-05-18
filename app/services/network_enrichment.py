import socket
from functools import lru_cache
import httpx


@lru_cache(maxsize=2048)
def reverse_dns(ip: str) -> str | None:
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return None


async def lookup_isp(ip: str) -> dict:
    if ip.startswith(('10.', '192.168.', '172.16.', '172.17.', '172.18.', '172.19.', '172.2', '172.30.', '172.31.')):
        return {'isp': 'Private network', 'org': None, 'as': None}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f'https://ipwho.is/{ip}', params={'fields': 'success,connection'})
            resp.raise_for_status()
            data = resp.json()
            conn = data.get('connection') or {}
            return {'isp': conn.get('isp'), 'org': conn.get('org'), 'as': conn.get('asn')}
    except Exception:
        return {'isp': None, 'org': None, 'as': None}
