"""Number entities for PerfectDraft."""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PerfectDraftDataUpdateCoordinator
from .sensor import _device_info, _get_target_temperature

DEFAULT_MIN_TEMP = 0.0
DEFAULT_MAX_TEMP = 12.0


def _get_temp_bounds(data: dict[str, Any]) -> tuple[float, float]:
    """Return validated (min_temp, max_temp) bounds from machine settings."""
    setting = data.get("setting")
    if isinstance(setting, dict):
        raw_min = setting.get("temperatureMin")
        raw_max = setting.get("temperatureMax")
        if (
            isinstance(raw_min, (int, float))
            and not isinstance(raw_min, bool)
            and isinstance(raw_max, (int, float))
            and not isinstance(raw_max, bool)
            and -5 <= raw_min < raw_max <= 25
        ):
            return float(raw_min), float(raw_max)
    return DEFAULT_MIN_TEMP, DEFAULT_MAX_TEMP


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PerfectDraft number entities."""
    coordinator: PerfectDraftDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PerfectDraftTargetTemperatureNumber(coordinator)])


class PerfectDraftTargetTemperatureNumber(
    CoordinatorEntity[PerfectDraftDataUpdateCoordinator], NumberEntity
):
    """Number entity controlling the PerfectDraft target beer temperature."""

    _attr_has_entity_name = True
    _attr_translation_key = "target_temperature"
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.SLIDER
    _attr_native_step = 1.0
    _attr_icon = "mdi:thermometer-check"

    def __init__(
        self,
        coordinator: PerfectDraftDataUpdateCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self._machine_id = (coordinator.data or {}).get("_machine_id", "unknown")
        self._attr_unique_id = f"{self._machine_id}_target_temperature_number"
        self._attr_device_info = _device_info(coordinator)

    @property
    def native_min_value(self) -> float:
        """Return the minimum target temperature supported by the machine."""
        min_temp, _ = _get_temp_bounds(self.coordinator.data or {})
        return min_temp

    @property
    def native_max_value(self) -> float:
        """Return the maximum target temperature supported by the machine."""
        _, max_temp = _get_temp_bounds(self.coordinator.data or {})
        return max_temp

    @property
    def native_value(self) -> float | None:
        """Return the current target temperature."""
        return _get_target_temperature(self.coordinator.data or {})

    async def async_set_native_value(self, value: float) -> None:
        """Set a new target temperature on the PerfectDraft machine."""
        min_temp, max_temp = _get_temp_bounds(self.coordinator.data or {})
        if not (min_temp <= value <= max_temp):
            raise HomeAssistantError(
                f"Target temperature {value}°C is outside allowed range ({min_temp}°C - {max_temp}°C)"
            )
        target = int(round(value))
        await self.coordinator.async_update_settings(temperature=target)
