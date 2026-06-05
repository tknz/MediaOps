from __future__ import annotations

from homeassistant.components.media_player import MediaPlayerEntity, MediaPlayerEntityFeature, MediaPlayerState
from homeassistant.components.media_player.const import MediaType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

SESSION_SLOTS = 5


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(MediaOpsSessionPlayer(coordinator, idx) for idx in range(SESSION_SLOTS))


class MediaOpsSessionPlayer(CoordinatorEntity, MediaPlayerEntity):
    _attr_supported_features = MediaPlayerEntityFeature.TURN_OFF
    _attr_entity_registry_visible_default = False

    def __init__(self, coordinator, index: int) -> None:
        super().__init__(coordinator)
        self._index = index
        self._attr_unique_id = f"mediaops_session_{index + 1}"
        self._attr_name = f"MediaOps session {index + 1}"

    @property
    def _session(self) -> dict | None:
        sessions = (self.coordinator.data or {}).get("sessions") or []
        return sessions[self._index] if self._index < len(sessions) else None

    @property
    def available(self) -> bool:
        return bool(self.coordinator.last_update_success)

    @property
    def state(self):
        session = self._session
        if not session:
            return MediaPlayerState.IDLE
        if (session.get("state") or "").lower() == "paused":
            return MediaPlayerState.PAUSED
        return MediaPlayerState.PLAYING

    @property
    def media_title(self):
        session = self._session
        return (session or {}).get("title") or "No active stream"

    @property
    def media_artist(self):
        session = self._session
        if not session:
            return None
        parts = [session.get("user"), session.get("player") or session.get("platform")]
        return " · ".join(part for part in parts if part)

    @property
    def media_content_type(self):
        return MediaType.VIDEO

    @property
    def media_image_hash(self):
        session = self._session
        if not session:
            return None
        return f"{session.get('session_key')}:{session.get('thumb')}"

    @property
    def extra_state_attributes(self):
        session = self._session or {}
        return {
            "session_key": session.get("session_key"),
            "session_id": session.get("session_id"),
            "user": session.get("user"),
            "subtitle": session.get("subtitle"),
            "library": session.get("library"),
            "player": session.get("player"),
            "device": session.get("device"),
            "platform": session.get("platform"),
            "player_address": session.get("player_address"),
            "remote_public_address": session.get("remote_public_address"),
            "ip_address": session.get("ip_address"),
            "isp": session.get("isp"),
            "org": session.get("org"),
            "as": session.get("as"),
            "ptr": session.get("ptr"),
            "machine_identifier": session.get("machine_identifier"),
            "bandwidth_kbps": session.get("bandwidth_kbps"),
            "transcode_decision": session.get("transcode_decision"),
            "started_at": session.get("started_at"),
            "last_seen_at": session.get("last_seen_at"),
        }

    async def async_get_media_image(self):
        session = self._session
        if not session or not session.get("thumb") or not session.get("session_key"):
            return None, None
        return await self.coordinator.api.session_art(session["session_key"])

    async def async_turn_off(self) -> None:
        session = self._session
        if not session or not session.get("session_key"):
            return
        await self.coordinator.api.terminate_session(session["session_key"], "Stopped from Home Assistant")
        await self.coordinator.async_request_refresh()
