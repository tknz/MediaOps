from __future__ import annotations

import aiohttp


class MediaOpsApiError(Exception):
    pass


class MediaOpsApi:
    def __init__(self, session: aiohttp.ClientSession, url: str, token: str) -> None:
        self._session = session
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}

    async def status(self) -> dict:
        return await self._request("GET", "/api/integrations/homeassistant/status")

    async def test_webhook(self) -> dict:
        return await self._request("POST", "/api/integrations/homeassistant/webhook/test")

    async def terminate_session(self, session_key: str, reason: str = "") -> dict:
        return await self._request("POST", f"/api/integrations/homeassistant/sessions/{session_key}/terminate", json={"reason": reason})

    async def ban_session_ip(self, session_key: str) -> dict:
        return await self._request("POST", f"/api/integrations/homeassistant/sessions/{session_key}/ban-ip", json={})

    async def ban_session_device(self, session_key: str) -> dict:
        return await self._request("POST", f"/api/integrations/homeassistant/sessions/{session_key}/ban-device", json={})

    async def session_art(self, session_key: str) -> tuple[bytes, str]:
        async with self._session.get(f"{self._url}/api/integrations/homeassistant/sessions/{session_key}/art", headers=self._headers) as response:
            if response.status >= 400:
                text = await response.text()
                raise MediaOpsApiError(f"MediaOps returned HTTP {response.status}: {text[:160]}")
            return await response.read(), response.headers.get("content-type", "image/jpeg")

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        async with self._session.request(method, f"{self._url}{path}", headers=self._headers, **kwargs) as response:
            if response.status >= 400:
                text = await response.text()
                raise MediaOpsApiError(f"MediaOps returned HTTP {response.status}: {text[:160]}")
            return await response.json()
