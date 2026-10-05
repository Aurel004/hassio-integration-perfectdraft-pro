"""Button entities for PerfectDraft."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, keg_changed_signal
from .coordinator import PerfectDraftDataUpdateCoordinator
from .sensor import _device_info, _get_keg_product_id

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PerfectDraft button entities."""
    coordinator: PerfectDraftDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PerfectDraftKegResetButton(coordinator)])


class PerfectDraftKegResetButton(
    CoordinatorEntity[PerfectDraftDataUpdateCoordinator], ButtonEntity
):
    """Button that marks the current keg as freshly changed as of now."""

    _attr_has_entity_name = True
    _attr_translation_key = "mark_keg_changed"
    _attr_icon = "mdi:keg"

    def __init__(
        self,
        coordinator: PerfectDraftDataUpdateCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self._machine_id = (coordinator.data or {}).get("_machine_id", "unknown")
        self._attr_unique_id = f"{self._machine_id}_mark_keg_changed"
        self._attr_device_info = _device_info(coordinator)

    async def async_press(self) -> None:
        """Reset keg freshness locally and notify the API if a keg product ID is known."""
        async_dispatcher_send(self.hass, keg_changed_signal(self._machine_id))
        product_id = _get_keg_product_id(self.coordinator.data or {})
        if product_id is not None:
            try:
                await self.coordinator.async_set_active_keg(product_id)
            except HomeAssistantError as err:
                _LOGGER.warning(
                    "Local keg freshness reset succeeded, but syncing with PerfectDraft API failed: %s",
                    err,
                )

