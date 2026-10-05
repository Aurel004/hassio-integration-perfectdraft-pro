"""Sensor entities for PerfectDraft."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfPressure,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MODE_OPTIONS, keg_changed_signal
from .coordinator import PerfectDraftDataUpdateCoordinator
from .keg_detection import KEG_TOTAL_VOLUME, detect_keg_change
from .temperature import beer_temperature

_LOGGER = logging.getLogger(__name__)

KEG_FRESHNESS_DAYS = 30


def _load_keg_catalog() -> dict[int, str]:
    try:
        raw = json.loads(
            (Path(__file__).parent / "keg_catalog.json").read_text(encoding="utf-8")
        )
        return {int(k): v for k, v in raw.items()}
    except (OSError, ValueError) as err:
        _LOGGER.debug("Could not load keg catalog: %s", err)
        return {}


_KEG_CATALOG: dict[int, str] = _load_keg_catalog()


@dataclass(frozen=True, kw_only=True)
class PerfectDraftSensorDescription(SensorEntityDescription):
    """Extended description with a value extractor."""

    value_fn: Callable[[dict], Any] = lambda d: None
    attr_fn: Callable[[dict], dict[str, Any] | None] | None = None


def _get_details(data: dict) -> dict:
    return data.get("details") or {}


def _get_temperature(data: dict) -> float | None:
    return beer_temperature(_get_details(data))


def _get_target_temperature(data: dict) -> float | None:
    setting = data.get("setting") or {}
    val = setting.get("temperature")
    if val is None:
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if -20.0 <= num <= 50.0 else None


def _get_volume_remaining(data: dict) -> float | None:
    vol = _get_details(data).get("kegVolume")
    if vol is None:
        return None
    try:
        return round(float(vol) / KEG_TOTAL_VOLUME * 100, 1)
    except (TypeError, ValueError):
        return None


def _get_keg_volume_liters(data: dict) -> float | None:
    vol = _get_details(data).get("kegVolume")
    if vol is None:
        return None
    try:
        num = float(vol)
    except (TypeError, ValueError):
        return None
    return round(num, 2) if 0.0 <= num <= 10.0 else None


def _get_connection_state(data: dict) -> str | None:
    state = _get_details(data).get("connectedState")
    if state is None:
        return None
    return "connected" if state else "disconnected"


def _get_door_state(data: dict) -> str | None:
    closed = _get_details(data).get("doorClosed")
    if closed is None:
        return None
    return "closed" if closed else "open"


def _get_pours(data: dict) -> int | None:
    val = _get_details(data).get("numberOfPoursSinceStartup")
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _get_last_pour_volume(data: dict) -> float | None:
    val = _get_details(data).get("volumeOfLastPour")
    if val is None or val == 0:
        return None
    try:
        return round(float(val) * 1000)  # litres -> ml
    except (TypeError, ValueError):
        return None


def _get_last_pour_duration(data: dict) -> float | None:
    val = _get_details(data).get("durationOfLastPour")
    if val is None:
        return None
    try:
        ms = float(val)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return round(ms / 1000.0, 1)


def _get_keg_pressure(data: dict) -> float | None:
    val = _get_details(data).get("kegPressure")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _get_error_code(data: dict) -> int | None:
    val = _get_details(data).get("errorCodes")
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _get_firmware(data: dict) -> str | None:
    return _get_details(data).get("firmwareVersion")


def _get_mode(data: dict) -> str | None:
    setting = data.get("setting") or {}
    details = _get_details(data)
    if setting.get("modeSwitchedOfByAppliance") is True:
        return "standby"
    for src in (setting, details):
        val = src.get("mode")
        if isinstance(val, str):
            lower = val.strip().lower()
            if lower in MODE_OPTIONS and lower != "standard":
                return lower
    if setting.get("boost") is True or details.get("boost") is True:
        return "boost"
    if (
        setting.get("ecoModeEnabled") is True
        or details.get("ecoModeEnabled") is True
    ):
        return "eco"
    for src in (setting, details):
        val = src.get("mode")
        if isinstance(val, str):
            lower = val.strip().lower()
            if lower in MODE_OPTIONS:
                return lower
    return None


def _get_mode_attributes(data: dict) -> dict[str, Any]:
    setting = data.get("setting") or {}
    return {
        "raw_mode": setting.get("mode"),
        "eco_mode_enabled": setting.get("ecoModeEnabled"),
        "eco_temperature_setpoint": setting.get("ecoModeBeerTemperatureSetPoint"),
        "eco_scheduler_enabled": setting.get("ecoModeSchedulerEnabled"),
        "boost": setting.get("boost"),
        "mode_switched_off_by_appliance": setting.get(
            "modeSwitchedOfByAppliance"
        ),
        "setting_updated_at": setting.get("updatedAt"),
    }


def _get_keg_product_id(data: dict) -> int | None:
    ref = (data.get("kegActive") or {}).get("keg")
    if not ref:
        return None
    try:
        return int(str(ref).rstrip("/").rsplit("/", 1)[-1])
    except ValueError:
        return None


def _get_keg_name(data: dict) -> str | None:
    product_id = _get_keg_product_id(data)
    if product_id is None:
        return None
    return _KEG_CATALOG.get(product_id)


def _keg_inserted_at(data: dict) -> datetime | None:
    iso = (data.get("kegActive") or {}).get("insertedAt")
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


SENSOR_DESCRIPTIONS: tuple[PerfectDraftSensorDescription, ...] = (
    PerfectDraftSensorDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_get_temperature,
    ),
    PerfectDraftSensorDescription(
        key="target_temperature",
        translation_key="target_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermostat",
        suggested_display_precision=0,
        value_fn=_get_target_temperature,
    ),
    PerfectDraftSensorDescription(
        key="keg_remaining",
        translation_key="keg_remaining",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:keg",
        suggested_display_precision=0,
        value_fn=_get_volume_remaining,
    ),
    PerfectDraftSensorDescription(
        key="keg_volume",
        translation_key="keg_volume",
        device_class=SensorDeviceClass.VOLUME_STORAGE,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:keg",
        suggested_display_precision=2,
        value_fn=_get_keg_volume_liters,
    ),
    PerfectDraftSensorDescription(
        key="keg_product_id",
        translation_key="keg_product_id",
        icon="mdi:barcode",
        value_fn=_get_keg_product_id,
    ),
    PerfectDraftSensorDescription(
        key="keg_name",
        translation_key="keg_name",
        icon="mdi:beer",
        value_fn=_get_keg_name,
    ),
    PerfectDraftSensorDescription(
        key="connection",
        translation_key="connection",
        device_class=SensorDeviceClass.ENUM,
        options=["connected", "disconnected"],
        icon="mdi:wifi",
        value_fn=_get_connection_state,
    ),
    PerfectDraftSensorDescription(
        key="door",
        translation_key="door",
        device_class=SensorDeviceClass.ENUM,
        options=["closed", "open"],
        icon="mdi:door",
        value_fn=_get_door_state,
    ),
    PerfectDraftSensorDescription(
        key="pours",
        translation_key="pours",
        icon="mdi:beer",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_get_pours,
    ),
    PerfectDraftSensorDescription(
        key="last_pour",
        translation_key="last_pour",
        native_unit_of_measurement="mL",
        icon="mdi:glass-mug-variant",
        value_fn=_get_last_pour_volume,
    ),
    PerfectDraftSensorDescription(
        key="last_pour_duration",
        translation_key="last_pour_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:timer-outline",
        suggested_display_precision=1,
        value_fn=_get_last_pour_duration,
    ),
    PerfectDraftSensorDescription(
        key="keg_pressure",
        translation_key="keg_pressure",
        device_class=SensorDeviceClass.PRESSURE,
        native_unit_of_measurement=UnitOfPressure.KPA,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:gauge",
        suggested_display_precision=1,
        value_fn=_get_keg_pressure,
    ),
    PerfectDraftSensorDescription(
        key="error_code",
        translation_key="error_code",
        icon="mdi:alert-circle-outline",
        value_fn=_get_error_code,
    ),
    PerfectDraftSensorDescription(
        key="firmware",
        translation_key="firmware",
        icon="mdi:chip",
        entity_registry_enabled_default=False,
        value_fn=_get_firmware,
    ),
    PerfectDraftSensorDescription(
        key="mode",
        translation_key="mode",
        device_class=SensorDeviceClass.ENUM,
        options=list(MODE_OPTIONS),
        icon="mdi:thermostat",
        value_fn=_get_mode,
        attr_fn=_get_mode_attributes,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PerfectDraft sensor entities."""
    coordinator: PerfectDraftDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        PerfectDraftSensor(coordinator, description)
        for description in SENSOR_DESCRIPTIONS
    ]
    entities.append(PerfectDraftKegFreshnessSensor(coordinator))

    async_add_entities(entities)


class PerfectDraftSensor(
    CoordinatorEntity[PerfectDraftDataUpdateCoordinator], SensorEntity
):
    """A PerfectDraft sensor backed by the shared coordinator."""

    _attr_has_entity_name = True
    entity_description: PerfectDraftSensorDescription

    def __init__(
        self,
        coordinator: PerfectDraftDataUpdateCoordinator,
        description: PerfectDraftSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        machine_id = (coordinator.data or {}).get("_machine_id", "unknown")
        self._attr_unique_id = f"{machine_id}_{description.key}"
        self._attr_device_info = _device_info(coordinator)

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data
        if not data:
            return None
        return self.entity_description.value_fn(data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if not data or self.entity_description.attr_fn is None:
            return None
        return self.entity_description.attr_fn(data)


class PerfectDraftKegFreshnessSensor(
    CoordinatorEntity[PerfectDraftDataUpdateCoordinator],
    RestoreEntity,
    SensorEntity,
):
    """Tracks keg freshness as a 30-day countdown from insertion.

    Prefers the insertion date reported by the API's active keg. When that is
    unavailable it falls back to detecting insertion locally (pour count reset
    with a near-full keg). The date is persisted across HA restarts via
    RestoreEntity.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "keg_freshness"
    _attr_native_unit_of_measurement = "d"
    _attr_icon = "mdi:calendar-clock"

    def __init__(
        self,
        coordinator: PerfectDraftDataUpdateCoordinator,
    ) -> None:
        super().__init__(coordinator)
        machine_id = (coordinator.data or {}).get("_machine_id", "unknown")
        self._attr_unique_id = f"{machine_id}_keg_freshness"
        self._attr_device_info = _device_info(coordinator)
        self._keg_inserted_at: datetime | None = None
        self._last_pours: int | None = None
        self._last_volume: float | None = None

    async def async_added_to_hass(self) -> None:
        """Restore keg state from the previous session and subscribe to manual resets."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state and last_state.attributes:
            iso = last_state.attributes.get("keg_inserted_at")
            if iso:
                try:
                    dt = datetime.fromisoformat(iso)
                    self._keg_inserted_at = (
                        dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
                    )
                except (ValueError, TypeError):
                    pass
            pours = last_state.attributes.get("last_pours")
            if pours is not None:
                self._last_pours = int(pours)
            volume = last_state.attributes.get("last_volume")
            if volume is not None:
                self._last_volume = float(volume)

        machine_id = (self.coordinator.data or {}).get("_machine_id", "unknown")
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                keg_changed_signal(machine_id),
                self._handle_manual_keg_change,
            )
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose keg insertion date as an attribute (also used for restore)."""
        return {
            "keg_inserted_at": self._keg_inserted_at.isoformat() if self._keg_inserted_at else None,
            "last_pours": self._last_pours,
            "last_volume": self._last_volume,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update the insertion date on each poll."""
        data = self.coordinator.data
        if not data:
            super()._handle_coordinator_update()
            return

        reported = _keg_inserted_at(data)
        if reported is not None:
            if self._keg_inserted_at is None or reported > self._keg_inserted_at:
                self._keg_inserted_at = reported
        else:
            details = data.get("details") or {}
            pours = details.get("numberOfPoursSinceStartup")
            volume = details.get("kegVolume")
            if pours is not None and volume is not None:
                if detect_keg_change(
                    last_pours=self._last_pours,
                    last_volume=self._last_volume,
                    pours=int(pours),
                    volume=float(volume),
                ):
                    self._keg_inserted_at = datetime.now(timezone.utc)

                self._last_pours = int(pours)
                self._last_volume = float(volume)

        super()._handle_coordinator_update()

    @callback
    def _handle_manual_keg_change(self) -> None:
        """Record the keg insertion as now in response to the manual button.

        Rebaselines the tracked pour count and volume to the current readings
        so automatic detection does not immediately re-fire.
        """
        self._keg_inserted_at = datetime.now(timezone.utc)

        details = (self.coordinator.data or {}).get("details") or {}
        pours = details.get("numberOfPoursSinceStartup")
        volume = details.get("kegVolume")
        if pours is not None:
            self._last_pours = int(pours)
        if volume is not None:
            self._last_volume = float(volume)

        self.async_write_ha_state()

    def _inserted_at(self) -> datetime | None:
        """The current insertion date, preferring the latest of API and local state."""
        reported = (
            _keg_inserted_at(self.coordinator.data)
            if self.coordinator.data
            else None
        )
        if reported is not None and self._keg_inserted_at is not None:
            return max(reported, self._keg_inserted_at)
        return reported or self._keg_inserted_at

    @property
    def native_value(self) -> int | None:
        inserted = self._inserted_at()
        if inserted is None:
            return None
        elapsed = (datetime.now(timezone.utc) - inserted).days
        return max(KEG_FRESHNESS_DAYS - elapsed, 0)

    @property
    def available(self) -> bool:
        return super().available and self._inserted_at() is not None


def _device_info(
    coordinator: PerfectDraftDataUpdateCoordinator,
) -> DeviceInfo:
    data = coordinator.data or {}
    machine_id = data.get("_machine_id", "unknown")
    details = data.get("details") or {}
    return DeviceInfo(
        identifiers={(DOMAIN, str(machine_id))},
        name="PerfectDraft Pro",
        manufacturer="PerfectDraft",
        model="Pro",
        sw_version=details.get("firmwareVersion"),
        serial_number=details.get("serialNumber"),
    )
