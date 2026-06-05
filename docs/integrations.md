# Integrations

MediaOps exposes integration endpoints for Home Assistant and AI tools. These endpoints use the existing bearer token system; normal browser sign-in is separate.

## Token Scopes

Tokens must start with `mo_` and be long enough to avoid accidental weak secrets.

```sh
printf 'mo_%s\n' "$(openssl rand -hex 32)"
```

Example:

```env
API_TOKENS=mo_xxx=homeassistant:ha.read integrations.read;mo_yyy=ai:mcp.read
```

Use `API_ADMIN_TOKEN` only for fully trusted admin automation.

## Home Assistant Polling

Endpoint:

```http
GET /api/integrations/homeassistant/status
Authorization: Bearer mo_xxx
```

The response contains live stream counts, transcode counts, active operations, request counts, bandwidth, and compact session/operation lists.

The custom component lives in:

```text
integrations/homeassistant/custom_components/mediaops
```

Copy that folder to Home Assistant's `custom_components/mediaops`, restart Home Assistant, then add MediaOps from Devices & Services.

## Home Assistant Webhooks

Set a Home Assistant webhook URL in Settings or with:

```env
HOMEASSISTANT_WEBHOOK_URL=https://homeassistant.example.com/api/webhook/mediaops
HOMEASSISTANT_WEBHOOK_TOKEN=
```

MediaOps posts JSON like:

```json
{
  "source": "mediaops",
  "event": "requests_changed",
  "sent_at": "2026-06-05T00:00:00Z",
  "payload": {
    "changed": 1,
    "pending_requests": 3
  }
}
```

Home Assistant webhook triggers do not require the extra token. `HOMEASSISTANT_WEBHOOK_TOKEN` is included as `X-MediaOps-Webhook-Token` for generic webhook receivers or custom HA validation.

## MCP-Style AI Endpoint

Endpoint:

```http
POST /api/mcp
Authorization: Bearer mo_yyy
Content-Type: application/json
```

Supported methods:

- `initialize`
- `tools/list`
- `tools/call`

Tools:

- `mediaops.status`
- `mediaops.overview`
- `mediaops.pending_requests`
- `mediaops.history_search`
