from __future__ import annotations

from datetime import datetime
import logging

import httpx

from .settings_store import all_settings


logger = logging.getLogger('mediaops.webhooks')


async def notify_homeassistant(event: str, payload: dict) -> bool:
    cfg = all_settings()
    url = (cfg.get('homeassistant_webhook_url') or '').strip()
    if not url:
        return False
    body = {
        'source': 'mediaops',
        'event': event,
        'sent_at': datetime.utcnow().isoformat() + 'Z',
        'payload': payload,
    }
    headers = {'Content-Type': 'application/json'}
    token = (cfg.get('homeassistant_webhook_token') or '').strip()
    if token:
        headers['X-MediaOps-Webhook-Token'] = token
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, json=body, headers=headers)
            response.raise_for_status()
        return True
    except Exception:
        logger.exception('Home Assistant webhook delivery failed for %s', event)
        return False
