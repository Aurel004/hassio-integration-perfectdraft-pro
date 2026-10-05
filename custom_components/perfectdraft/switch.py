"""Switch entities for PerfectDraft."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PerfectDraftDataUpdateCoordinator
from .sensor import _device_info, _get_mode


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PerfectDraft switch entities."""
    coordinator: PerfectDraftDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            PerfectDraftBoostSwitch(coordinator),
            PerfectDraftEcoModeSwitch(coordinator),
        ]
    )


class PerfectDraftBoostSwitch(
    CoordinatorEntity[PerfectDraftDataUpdateCoordinator], SwitchEntity
):
    """Switch entity for PerfectDraft rapid cooling (Boost) mode."""

    _attr_has_entity_name = True
    _attr_translation_key = "boost"
    _attr_icon = "mdi:rocket-launch"

    def __init__(
        self,
        coordinator: PerfectDraftDataUpdateCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self._machine_id = (coordinator.data or {}).get("_machine_id", "unknown")
        self._attr_unique_id = f"{self._machine_id}_boost_switch"
        self._attr_device_info = _device_info(coordinator)

    @property
    def is_on(self) -> bool | None:
        """Return true if boost mode is active."""
        data = self.coordinator.data or {}
        setting = data.get("setting")
        if isinstance(setting, dict):
            boost = setting.get("boost")
            if isinstance(boost, bool):
                return boost
        mode = _get_mode(data)
        if mode is not None:
            return mode == "boost"
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable boost mode."""
        await self.coordinator.async_update_settings(boost=True, mode="boost")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable boost mode."""
        current_mode = _get_mode(self.coordinator.data or {})
        next_mode = "eco" if current_mode == "eco" else "standard"
        await self.coordinator.async_update_settings(boost=False, mode=next_mode)


class PerfectDraftEcoModeSwitch(
    CoordinatorEntity[PerfectDraftDataUpdateCoordinator], SwitchEntity
):
    """Switch entity for PerfectDraft Eco mode."""

    _attr_has_entity_name = True
    _attr_translation_key = "eco_mode"
    _attr_icon = "mdi:leaf"

    def __init__(
        self,
        coordinator: PerfectDraftDataUpdateCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self._machine_id = (coordinator.data or {}).get("_machine_id", "unknown")
        self._attr_unique_id = f"{self._machine_id}_eco_mode_switch"
        self._attr_device_info = _device_info(coordinator)

    @property
    def is_on(self) -> bool | None:
        """Return true if eco mode is enabled."""
        data = self.coordinator.data or {}
        setting = data.get("setting")
        if isinstance(setting, dict):
            eco = setting.get("ecoModeEnabled")
            if isinstance(eco, bool):
                return eco
        mode = _get_mode(data)
        if mode is not None:
            return mode == "eco"
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable eco mode."""
        await self.coordinator.async_update_settings(
            ecoModeEnabled=True,
            boost=False,
            mode="eco",
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable eco mode."""
        current_mode = _get_mode(self.coordinator.data or {})
        next_mode = "boost" if current_mode == "boost" else "standard"
        await self.coordinator.async_update_settings(
            ecoModeEnabled=False,
            mode=next_mode,
        )
