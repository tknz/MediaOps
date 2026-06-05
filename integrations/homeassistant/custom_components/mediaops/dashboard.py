from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.frontend import async_register_built_in_panel
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store


_LOGGER = logging.getLogger(__name__)

DASHBOARD_ID = "mediaops"
DASHBOARD_TITLE = "MediaOps"
DASHBOARD_ICON = "mdi:movie-open-play"
DASHBOARD_STORAGE_KEY = f"lovelace.{DASHBOARD_ID}"
DASHBOARDS_STORAGE_KEY = "lovelace_dashboards"


async def async_ensure_dashboard(hass: HomeAssistant) -> None:
    """Install or repair the built-in MediaOps Lovelace dashboard."""
    try:
        await _upsert_dashboard_record(hass)
        await Store(hass, 1, DASHBOARD_STORAGE_KEY).async_save({"config": _dashboard_config()})
        async_register_built_in_panel(
            hass,
            "lovelace",
            sidebar_title=DASHBOARD_TITLE,
            sidebar_icon=DASHBOARD_ICON,
            sidebar_default_visible=True,
            frontend_url_path=DASHBOARD_ID,
            config={"mode": "storage"},
            require_admin=False,
            update=True,
            show_in_sidebar=True,
        )
    except Exception:
        _LOGGER.exception("Unable to install the MediaOps dashboard")


async def _upsert_dashboard_record(hass: HomeAssistant) -> None:
    store = Store(hass, 1, DASHBOARDS_STORAGE_KEY)
    data = await store.async_load() or {}
    items = list(data.get("items") or [])
    record = {
        "id": DASHBOARD_ID,
        "title": DASHBOARD_TITLE,
        "url_path": DASHBOARD_ID,
        "icon": DASHBOARD_ICON,
        "show_in_sidebar": True,
        "require_admin": False,
        "mode": "storage",
    }
    replaced = False
    for index, item in enumerate(items):
        if item.get("id") == DASHBOARD_ID or item.get("url_path") == DASHBOARD_ID:
            items[index] = record
            replaced = True
            break
    if not replaced:
        items.append(record)
    data["items"] = items
    await store.async_save(data)


def _dashboard_config() -> dict[str, Any]:
    return {
        "title": DASHBOARD_TITLE,
        "views": [
            {
                "title": DASHBOARD_TITLE,
                "path": "default_view",
                "icon": DASHBOARD_ICON,
                "type": "sections",
                "max_columns": 3,
                "sections": [
                    {
                        "type": "grid",
                        "title": "Now playing",
                        "cards": [_session_card(1), _session_card(2)],
                    },
                    {
                        "type": "grid",
                        "title": "More streams",
                        "cards": [_session_card(3), _session_card(4), _session_card(5)],
                    },
                    {
                        "type": "grid",
                        "title": "Live operations",
                        "cards": [
                            _tile("binary_sensor.mediaops_online", "API online"),
                            _tile("binary_sensor.mediaops_has_active_streams", "Streaming now"),
                            _tile("sensor.mediaops_live_streams", "Live streams"),
                            _tile("sensor.mediaops_active_streams", "Active streams"),
                            _tile("sensor.mediaops_paused_streams", "Paused streams"),
                            _tile("sensor.mediaops_pending_requests", "Pending requests"),
                            _operations_card(),
                        ],
                    },
                    {
                        "type": "grid",
                        "title": "Bans",
                        "cards": [
                            _tile("sensor.mediaops_active_bans", "Active bans"),
                            _bans_table(),
                            _unban_grid(),
                        ],
                    },
                    {
                        "type": "grid",
                        "title": "Bandwidth and links",
                        "cards": [
                            {
                                "type": "gauge",
                                "entity": "sensor.mediaops_bandwidth",
                                "name": "Bandwidth",
                                "min": 0,
                                "max": 80,
                                "severity": {"green": 0, "yellow": 25, "red": 55},
                            },
                            {
                                "type": "entities",
                                "entities": [
                                    "sensor.mediaops_playback_transcodes",
                                    "sensor.mediaops_background_transcodes",
                                    "sensor.mediaops_active_operations",
                                    "button.mediaops_send_test_webhook",
                                ],
                            },
                        ],
                    },
                ],
                "cards": [],
            }
        ],
    }


def _session_card(index: int) -> dict[str, Any]:
    entity = f"media_player.mediaops_session_{index}"
    return {
        "type": "conditional",
        "conditions": [
            {"entity": entity, "state_not": "idle"},
            {"entity": entity, "state_not": "unavailable"},
            {"entity": entity, "state_not": "unknown"},
        ],
        "card": {
            "type": "vertical-stack",
            "cards": [
                {"type": "media-control", "entity": entity},
                _session_details(entity),
                {
                    "type": "grid",
                    "columns": 4,
                    "square": False,
                    "cards": [
                        _button(f"button.mediaops_session_{index}_stop_stream", "Stop", "mdi:stop-circle-outline"),
                        _button(f"button.mediaops_session_{index}_ban_user", "Ban user", "mdi:account-cancel-outline"),
                        _button(f"button.mediaops_session_{index}_ban_ip", "Ban IP", "mdi:ip-network-outline"),
                        _button(f"button.mediaops_session_{index}_ban_device", "Ban device", "mdi:monitor-off"),
                    ],
                },
            ],
        },
    }


def _session_details(entity: str) -> dict[str, str]:
    return {
        "type": "markdown",
        "content": (
            "{% set e = '" + entity + "' %}\n"
            "{% set public_ip = state_attr(e, 'remote_public_address') or state_attr(e, 'ip_address') or '-' %}\n"
            "{% set local_ip = state_attr(e, 'player_address') or '-' %}\n"
            "{% set bandwidth = state_attr(e, 'bandwidth_kbps') or 0 %}\n"
            "**{{ state_attr(e, 'user') or 'Unknown user' }}**  \n"
            "{{ state_attr(e, 'player') or state_attr(e, 'device') or 'Unknown player' }}"
            "{% if state_attr(e, 'platform') %} | {{ state_attr(e, 'platform') }}{% endif %}  \n"
            "{{ states(e) | title }}{% if state_attr(e, 'transcode_decision') %}"
            " | {{ state_attr(e, 'transcode_decision') }}{% endif %}"
            "{% if bandwidth %} | {{ (bandwidth / 1000) | round(1) }} Mbit/s{% endif %}\n\n"
            "| Field | Value |\n"
            "|---|---|\n"
            "| Public IP | `{{ public_ip }}` |\n"
            "| Player IP | `{{ local_ip }}` |\n"
            "| ISP | {{ state_attr(e, 'isp') or '-' }} |\n"
            "| Network | {{ state_attr(e, 'org') or '-' }}{% if state_attr(e, 'as') %} / AS{{ state_attr(e, 'as') }}{% endif %} |\n"
            "| PTR | `{{ state_attr(e, 'ptr') or '-' }}` |\n"
            "| Library | {{ state_attr(e, 'library') or '-' }} |\n"
        ),
    }


def _operations_card() -> dict[str, Any]:
    return {
        "type": "entities",
        "title": "Operations",
        "entities": [
            "sensor.mediaops_active_operations",
            "sensor.mediaops_background_transcodes",
            "sensor.mediaops_playback_transcodes",
            "button.mediaops_send_test_webhook",
        ],
    }


def _bans_table() -> dict[str, str]:
    return {
        "type": "markdown",
        "content": (
            "{% set bans = state_attr('sensor.mediaops_active_bans', 'bans') or [] %}\n"
            "{% if bans %}\n"
            "| User | Type | Label | Value |\n"
            "|---|---|---|---|\n"
            "{% for ban in bans[:10] %}"
            "| {{ ban.username or '-' }} | {{ ban.type or '-' }} | {{ ban.label or '-' }} | `{{ ban.value or '-' }}` |\n"
            "{% endfor %}\n"
            "{% else %}\n"
            "No active bans.\n"
            "{% endif %}"
        ),
    }


def _unban_grid() -> dict[str, Any]:
    return {
        "type": "grid",
        "columns": 5,
        "square": False,
        "cards": [
            {
                "type": "conditional",
                "conditions": [
                    {"entity": f"button.mediaops_unban_{index}", "state_not": "unavailable"},
                    {"entity": f"button.mediaops_unban_{index}", "state_not": "unknown"},
                ],
                "card": _button(f"button.mediaops_unban_{index}", f"Unban {index}", "mdi:lock-open-outline"),
            }
            for index in range(1, 11)
        ],
    }


def _tile(entity: str, name: str) -> dict[str, str]:
    return {"type": "tile", "entity": entity, "name": name}


def _button(entity: str, name: str, icon: str) -> dict[str, Any]:
    return {
        "type": "button",
        "entity": entity,
        "name": name,
        "icon": icon,
        "tap_action": {"action": "call-service", "service": "button.press", "target": {"entity_id": entity}},
    }
