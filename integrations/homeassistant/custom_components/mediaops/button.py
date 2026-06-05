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
