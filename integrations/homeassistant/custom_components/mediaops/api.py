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

    async def _request(self, method: str, path: str) -> dict:
        async with self._session.request(method, f"{self._url}{path}", headers=self._headers) as response:
            if response.status >= 400:
                text = await response.text()
                raise MediaOpsApiError(f"MediaOps returned HTTP {response.status}: {text[:160]}")
            return await response.json()
