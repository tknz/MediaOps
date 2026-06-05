from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MediaOpsOnlineSensor(coordinator), MediaOpsStreamingSensor(coordinator)])


class MediaOpsOnlineSensor(CoordinatorEntity, BinarySensorEntity):
    _attr_name = "MediaOps online"
    _attr_unique_id = "mediaops_online"

    @property
    def is_on(self):
        return bool((self.coordinator.data or {}).get("ok"))


class MediaOpsStreamingSensor(CoordinatorEntity, BinarySensorEntity):
    _attr_name = "MediaOps has active streams"
    _attr_unique_id = "mediaops_has_active_streams"

    @property
    def is_on(self):
        return int((self.coordinator.data or {}).get("active_streams") or 0) > 0
