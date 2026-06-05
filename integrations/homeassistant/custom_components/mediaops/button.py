from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [MediaOpsWebhookTestButton(coordinator)]
    for idx in range(5):
        entities.extend([
            MediaOpsSessionActionButton(coordinator, idx, "stop", "Stop stream"),
            MediaOpsSessionActionButton(coordinator, idx, "ban_ip", "Ban IP"),
            MediaOpsSessionActionButton(coordinator, idx, "ban_device", "Ban device"),
        ])
    for idx in range(10):
        entities.append(MediaOpsUnbanButton(coordinator, idx))
    async_add_entities(entities)


class MediaOpsWebhookTestButton(CoordinatorEntity, ButtonEntity):
    _attr_name = "MediaOps send test webhook"
    _attr_unique_id = "mediaops_send_test_webhook"

    async def async_press(self) -> None:
        await self.coordinator.api.test_webhook()
        await self.coordinator.async_request_refresh()


class MediaOpsSessionActionButton(CoordinatorEntity, ButtonEntity):
    def __init__(self, coordinator, index: int, action: str, label: str) -> None:
        super().__init__(coordinator)
        self._index = index
        self._action = action
        self._label = label
        self._attr_unique_id = f"mediaops_session_{index + 1}_{action}"
        self._attr_name = f"MediaOps session {index + 1} {label.lower()}"

    @property
    def _session(self) -> dict | None:
        sessions = (self.coordinator.data or {}).get("sessions") or []
        return sessions[self._index] if self._index < len(sessions) else None

    @property
    def available(self) -> bool:
        session = self._session
        if not session:
            return False
        if self._action == "ban_ip":
            return bool(session.get("remote_public_address"))
        if self._action == "ban_device":
            return bool(session.get("machine_identifier"))
        return bool(session.get("session_key"))

    async def async_press(self) -> None:
        session = self._session
        if not session or not session.get("session_key"):
            return
        if self._action == "stop":
            await self.coordinator.api.terminate_session(session["session_key"], "Stopped from Home Assistant")
        elif self._action == "ban_ip":
            await self.coordinator.api.ban_session_ip(session["session_key"])
        elif self._action == "ban_device":
            await self.coordinator.api.ban_session_device(session["session_key"])
        await self.coordinator.async_request_refresh()


class MediaOpsUnbanButton(CoordinatorEntity, ButtonEntity):
    def __init__(self, coordinator, index: int) -> None:
        super().__init__(coordinator)
        self._index = index
        self._attr_unique_id = f"mediaops_unban_{index + 1}"
        self._attr_name = f"MediaOps unban {index + 1}"

    @property
    def _ban(self) -> dict | None:
        bans = (self.coordinator.data or {}).get("bans") or []
        return bans[self._index] if self._index < len(bans) else None

    @property
    def available(self) -> bool:
        return bool((self._ban or {}).get("id"))

    @property
    def extra_state_attributes(self):
        ban = self._ban or {}
        return {
            "block_id": ban.get("id"),
            "username": ban.get("username"),
            "type": ban.get("type"),
            "value": ban.get("value"),
            "label": ban.get("label"),
        }

    async def async_press(self) -> None:
        ban = self._ban
        if not ban or not ban.get("id"):
            return
        await self.coordinator.api.unban(int(ban["id"]))
        await self.coordinator.async_request_refresh()
