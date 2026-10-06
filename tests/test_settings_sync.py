"""Unit tests for setting reconciliation and time-to-target extraction."""
from __future__ import annotations

from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
import types
from typing import Any
import unittest

_PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "perfectdraft"


def _ensure_ha_stubs() -> None:
    if "homeassistant" in sys.modules:
        return
    ha = types.ModuleType("homeassistant")
    ha_config_entries = types.ModuleType("homeassistant.config_entries")
    ha_config_entries.ConfigEntry = object
    ha_const = types.ModuleType("homeassistant.const")
    ha_const.PERCENTAGE = "%"
    ha_const.UnitOfPressure = types.SimpleNamespace(KPA="kPa")
    ha_const.UnitOfTemperature = types.SimpleNamespace(CELSIUS="°C")
    ha_const.UnitOfTime = types.SimpleNamespace(SECONDS="s", MINUTES="min")
    ha_const.UnitOfVolume = types.SimpleNamespace(LITERS="L")
    ha_core = types.ModuleType("homeassistant.core")
    ha_core.HomeAssistant = object
    ha_core.callback = lambda fn: fn
    ha_exc = types.ModuleType("homeassistant.exceptions")
    ha_exc.ConfigEntryAuthFailed = Exception
    ha_exc.HomeAssistantError = Exception
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_dev_reg = types.ModuleType("homeassistant.helpers.device_registry")
    ha_dev_reg.DeviceInfo = dict
    ha_disp = types.ModuleType("homeassistant.helpers.dispatcher")
    ha_disp.async_dispatcher_connect = lambda *a, **kw: None
    ha_ent_plat = types.ModuleType("homeassistant.helpers.entity_platform")
    ha_ent_plat.AddEntitiesCallback = object
    ha_restore = types.ModuleType("homeassistant.helpers.restore_state")
    ha_restore.RestoreEntity = type("RestoreEntity", (), {})
    ha_coord = types.ModuleType("homeassistant.helpers.update_coordinator")

    class _DummyGeneric:
        def __class_getitem__(cls, item):
            return cls

    ha_coord.DataUpdateCoordinator = _DummyGeneric
    ha_coord.CoordinatorEntity = _DummyGeneric
    ha_coord.UpdateFailed = Exception

    ha_comp = types.ModuleType("homeassistant.components")
    ha_sensor = types.ModuleType("homeassistant.components.sensor")
    ha_sensor.SensorDeviceClass = types.SimpleNamespace(
        TEMPERATURE="temperature",
        VOLUME_STORAGE="volume_storage",
        ENUM="enum",
        DURATION="duration",
        PRESSURE="pressure",
    )
    ha_sensor.SensorEntity = type("SensorEntity", (), {})

    @dataclass(frozen=True, kw_only=True)
    class _SensorEntityDescription:
        key: str = ""
        translation_key: str | None = None
        device_class: Any = None
        native_unit_of_measurement: str | None = None
        state_class: Any = None
        icon: str | None = None
        suggested_display_precision: int | None = None
        options: list[str] | None = None
        entity_registry_enabled_default: bool = True

    ha_sensor.SensorEntityDescription = _SensorEntityDescription
    ha_sensor.SensorStateClass = types.SimpleNamespace(
        MEASUREMENT="measurement",
        TOTAL_INCREASING="total_increasing",
    )

    for name, mod in {
        "homeassistant": ha,
        "homeassistant.config_entries": ha_config_entries,
        "homeassistant.const": ha_const,
        "homeassistant.core": ha_core,
        "homeassistant.exceptions": ha_exc,
        "homeassistant.helpers": ha_helpers,
        "homeassistant.helpers.device_registry": ha_dev_reg,
        "homeassistant.helpers.dispatcher": ha_disp,
        "homeassistant.helpers.entity_platform": ha_ent_plat,
        "homeassistant.helpers.restore_state": ha_restore,
        "homeassistant.helpers.update_coordinator": ha_coord,
        "homeassistant.components": ha_comp,
        "homeassistant.components.sensor": ha_sensor,
    }.items():
        sys.modules[name] = mod


_ensure_ha_stubs()

if "custom_components.perfectdraft" not in sys.modules:
    pkg = types.ModuleType("custom_components.perfectdraft")
    pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["custom_components.perfectdraft"] = pkg


def _load(mod_name: str):
    full = f"custom_components.perfectdraft.{mod_name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _PKG_DIR / f"{mod_name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


_load("const")
_load("exceptions")
_load("temperature")
_load("keg_detection")
_load("api")
coordinator_mod = _load("coordinator")
sensor_mod = _load("sensor")


class TestReconcileSettings(unittest.TestCase):
    def test_startup_uses_appliance_hardware_state(self) -> None:
        api_setting = {
            "id": 1,
            "temperature": 3,
            "mode": "eco",
            "ecoModeEnabled": True,
            "updatedAt": "2026-10-06T17:34:08+00:00",
        }
        details_settings = {
            "temperature": 5,
            "ecoModeEnabled": False,
            "ecoModeSchedulerEnabled": False,
        }
        merged, updated_at = coordinator_mod.reconcile_settings(
            api_setting, details_settings, prev_updated_at=None
        )
        self.assertEqual(updated_at, "2026-10-06T17:34:08+00:00")
        self.assertEqual(merged["temperature"], 5)
        self.assertFalse(merged["ecoModeEnabled"])
        self.assertEqual(merged["mode"], "standard")
        self.assertEqual(sensor_mod._get_mode({"setting": merged}), "standard")

    def test_in_flight_put_preserves_new_api_setting_then_reverts_if_unapplied(self) -> None:
        # Poll right after PUT (updatedAt changed from T1 to T2, machine shadow still has old True)
        api_setting = {
            "id": 1,
            "temperature": 3,
            "mode": "standard",
            "ecoModeEnabled": False,
            "updatedAt": "2026-10-06T18:00:00+00:00",
        }
        stale_hw = {
            "temperature": 3,
            "ecoModeEnabled": True,
        }
        merged, updated_at = coordinator_mod.reconcile_settings(
            api_setting, stale_hw, prev_updated_at="2026-10-06T17:34:08+00:00"
        )
        self.assertEqual(updated_at, "2026-10-06T18:00:00+00:00")
        self.assertFalse(merged["ecoModeEnabled"])
        self.assertEqual(merged["mode"], "standard")

        # Next poll (updatedAt still T2): if hardware still reports ecoModeEnabled=True,
        # hardware state wins so HA displays real state, not supposed state.
        merged2, _ = coordinator_mod.reconcile_settings(
            api_setting, stale_hw, prev_updated_at=updated_at
        )
        self.assertTrue(merged2["ecoModeEnabled"])
        self.assertEqual(merged2["mode"], "eco")

    def test_scheduler_enabled_sets_cooling_mode_and_clears_when_disabled(self) -> None:
        api_setting = {
            "id": 1,
            "mode": "standard",
            "ecoModeEnabled": False,
            "ecoModeSchedulerEnabled": False,
            "updatedAt": "2026-10-06T17:34:08+00:00",
        }
        hw_scheduler_on = {
            "ecoModeEnabled": False,
            "ecoModeSchedulerEnabled": True,
        }
        merged, _ = coordinator_mod.reconcile_settings(
            api_setting, hw_scheduler_on, prev_updated_at=None
        )
        self.assertEqual(merged["mode"], "cooling")
        self.assertEqual(
            sensor_mod._get_mode(
                {"setting": merged, "details": {"settings": hw_scheduler_on}}
            ),
            "cooling",
        )

        # When scheduler is disabled on the appliance, mode reverts from cooling to standard
        hw_scheduler_off = {
            "ecoModeEnabled": False,
            "ecoModeSchedulerEnabled": False,
        }
        merged_off, _ = coordinator_mod.reconcile_settings(
            {"mode": "cooling", "updatedAt": "2026-10-06T17:34:08+00:00"},
            hw_scheduler_off,
            prev_updated_at=None,
        )
        self.assertEqual(merged_off["mode"], "standard")
        self.assertEqual(
            sensor_mod._get_mode(
                {"setting": merged_off, "details": {"settings": hw_scheduler_off}}
            ),
            "standard",
        )


class TestTimeToTargetSensor(unittest.TestCase):
    def test_converts_milliseconds_to_minutes(self) -> None:
        data = {"details": {"timeToReachTargetTemperature": 3258999}}
        self.assertEqual(sensor_mod._get_time_to_target(data), 54)

    def test_zero_milliseconds_returns_zero(self) -> None:
        data = {"details": {"timeToReachTargetTemperature": 0}}
        self.assertEqual(sensor_mod._get_time_to_target(data), 0)

    def test_under_one_minute_rounds_up_to_one(self) -> None:
        data = {"details": {"timeToReachTargetTemperature": 15000}}
        self.assertEqual(sensor_mod._get_time_to_target(data), 1)

    def test_invalid_or_negative_returns_none(self) -> None:
        self.assertIsNone(sensor_mod._get_time_to_target({"details": {}}))
        self.assertIsNone(
            sensor_mod._get_time_to_target(
                {"details": {"timeToReachTargetTemperature": -1}}
            )
        )


if __name__ == "__main__":
    unittest.main()
