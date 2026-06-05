# Integrations

MediaOps exposes integration endpoints for Home Assistant and AI tools. These endpoints use the existing bearer token system; normal browser sign-in is separate.

## Token Scopes

Home Assistant tokens should be created in MediaOps Settings under `Integrations`. MediaOps stores only a hash, shows the token once, records when it was last used, and lets you revoke it later.

Environment-managed tokens are still supported for advanced automation. They must start with `mo_` and be long enough to avoid accidental weak secrets.

```sh
printf 'mo_%s\n' "$(openssl rand -hex 32)"
```

Environment example:

```env
API_TOKENS=mo_xxx=ai:mcp.read
```

Use `API_ADMIN_TOKEN` only for fully trusted admin automation.

## Home Assistant Polling

Endpoint:

```http
GET /api/integrations/homeassistant/status
Authorization: Bearer mo_xxx
```

The response contains live stream counts, transcode counts, active operations, request counts, bandwidth, and compact session/operation lists.

The Home Assistant custom integration lives in:

```text
custom_components/mediaops
```

In MediaOps, create a Home Assistant token in `Settings -> Integrations`.

Recommended install is through HACS as a custom repository:

```text
https://github.com/tknz/MediaOps
```

Choose category `Integration`, install MediaOps, restart Home Assistant, then add MediaOps from Devices & Services with the MediaOps URL and token.

For manual installs, copy `custom_components/mediaops` to Home Assistant's `custom_components/mediaops`.

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
