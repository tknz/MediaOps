from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.const import UnitOfDataRate
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


@dataclass(frozen=True)
class MediaOpsSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict], object | None] = lambda data: None


SENSORS = [
    MediaOpsSensorDescription(key="live_streams", name="MediaOps live streams", native_unit_of_measurement="streams", value_fn=lambda d: d.get("live_streams")),
    MediaOpsSensorDescription(key="active_streams", name="MediaOps active streams", native_unit_of_measurement="streams", value_fn=lambda d: d.get("active_streams")),
    MediaOpsSensorDescription(key="paused_streams", name="MediaOps paused streams", native_unit_of_measurement="streams", value_fn=lambda d: d.get("paused_streams")),
    MediaOpsSensorDescription(key="playback_transcodes", name="MediaOps playback transcodes", native_unit_of_measurement="streams", value_fn=lambda d: d.get("playback_transcodes")),
    MediaOpsSensorDescription(key="background_transcodes", name="MediaOps background transcodes", native_unit_of_measurement="jobs", value_fn=lambda d: d.get("background_transcodes")),
    MediaOpsSensorDescription(key="active_operations", name="MediaOps active operations", native_unit_of_measurement="items", value_fn=lambda d: d.get("active_operations")),
    MediaOpsSensorDescription(key="pending_requests", name="MediaOps pending requests", native_unit_of_measurement="requests", value_fn=lambda d: d.get("pending_requests")),
    MediaOpsSensorDescription(key="bandwidth_mbps", name="MediaOps bandwidth", native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND, value_fn=lambda d: d.get("bandwidth_mbps")),
]


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(MediaOpsSensor(coordinator, description) for description in SENSORS)


class MediaOpsSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, description: MediaOpsSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"mediaops_{description.key}"

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.coordinator.data or {})

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        return {"server": data.get("server"), "updated_at": data.get("updated_at")}
