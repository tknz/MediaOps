from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MediaOpsWebhookTestButton(coordinator)])


class MediaOpsWebhookTestButton(CoordinatorEntity, ButtonEntity):
    _attr_name = "MediaOps send test webhook"
    _attr_unique_id = "mediaops_send_test_webhook"

    async def async_press(self) -> None:
        await self.coordinator.api.test_webhook()
        await self.coordinator.async_request_refresh()
