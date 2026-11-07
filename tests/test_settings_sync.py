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

    def test_mobile_app_switches_to_eco_mode_with_stale_hardware_shadow(self) -> None:
        sync_state: dict[str, Any] = {}
        # Poll 1: Initially in standard mode (5°C)
        api_std = {
            "id": 1,
            "temperature": 5,
            "temperatureMin": 0,
            "temperatureMax": 7,
            "mode": "standard",
            "ecoModeEnabled": False,
            "ecoModeSchedulerEnabled": False,
            "updatedAt": "2026-10-06T18:00:00+00:00",
        }
        hw_std = {
            "temperature": 5,
            "temperatureMin": 3,
            "temperatureMax": 7,
            "ecoModeEnabled": False,
            "ecoModeSchedulerEnabled": False,
        }
        merged1, updated1 = coordinator_mod.reconcile_settings(
            api_std, hw_std, prev_updated_at=None, sync_state=sync_state
        )
        self.assertEqual(sensor_mod._get_mode({"setting": merged1, "details": {"settings": hw_std}}), "standard")
        self.assertEqual(sensor_mod._get_target_temperature({"setting": merged1, "details": {"settings": hw_std}}), 5.0)

        # Poll 2: User switches to Eco mode on mobile app (API updatedAt changes, ecoModeEnabled=True,
        # temperature=7, while mode remains "standard" in API and hardware shadow hasn't updated yet)
        api_eco = {
            **api_std,
            "temperature": 7,
            "ecoModeEnabled": True,
            "updatedAt": "2026-10-06T18:05:00+00:00",
        }
        merged2, updated2 = coordinator_mod.reconcile_settings(
            api_eco, hw_std, prev_updated_at=updated1, sync_state=sync_state
        )
        self.assertTrue(merged2["ecoModeEnabled"])
        self.assertEqual(merged2["mode"], "eco")
        self.assertEqual(merged2["temperature"], 7)
        self.assertEqual(sensor_mod._get_mode({"setting": merged2, "details": {"settings": hw_std}}), "eco")
        self.assertEqual(sensor_mod._get_target_temperature({"setting": merged2, "details": {"settings": hw_std}}), 7.0)

        # Poll 3 (2 minutes later): updatedAt is still 18:05:00, and hardware shadow is STILL stale (hw_std).
        # Must NOT revert back to standard mode or 5°C!
        merged3, _ = coordinator_mod.reconcile_settings(
            api_eco, hw_std, prev_updated_at=updated2, sync_state=sync_state
        )
        self.assertTrue(merged3["ecoModeEnabled"])
        self.assertEqual(merged3["mode"], "eco")
        self.assertEqual(merged3["temperature"], 7)
        self.assertEqual(sensor_mod._get_mode({"setting": merged3, "details": {"settings": hw_std}}), "eco")
        self.assertEqual(sensor_mod._get_target_temperature({"setting": merged3, "details": {"settings": hw_std}}), 7.0)

    def test_mobile_app_switches_to_planning_mode_and_updates_real_target_temp(self) -> None:
        sync_state: dict[str, Any] = {}
        api_std = {
            "id": 1,
            "temperature": 5,
            "temperatureMin": 0,
            "temperatureMax": 7,
            "mode": "standard",
            "ecoModeEnabled": False,
            "ecoModeSchedulerEnabled": False,
            "updatedAt": "2026-10-06T18:00:00+00:00",
        }
        hw_std = {
            "temperature": 5,
            "temperatureMin": 3,
            "temperatureMax": 7,
            "ecoModeEnabled": False,
            "ecoModeSchedulerEnabled": False,
        }
        _, updated1 = coordinator_mod.reconcile_settings(
            api_std, hw_std, prev_updated_at=None, sync_state=sync_state
        )

        # User enables Planning (Cooling Scheduler) on mobile app; API reports temperature=7
        api_planning = {
            **api_std,
            "temperature": 7,
            "ecoModeSchedulerEnabled": True,
            "updatedAt": "2026-10-06T18:10:00+00:00",
        }
        merged_plan, updated2 = coordinator_mod.reconcile_settings(
            api_planning, hw_std, prev_updated_at=updated1, sync_state=sync_state
        )
        self.assertTrue(merged_plan["ecoModeSchedulerEnabled"])
        self.assertEqual(merged_plan["mode"], "cooling")
        self.assertEqual(merged_plan["temperature"], 7)
        data_plan = {"setting": merged_plan, "details": {"settings": hw_std}}
        self.assertEqual(sensor_mod._get_mode(data_plan), "cooling")
        self.assertEqual(sensor_mod._get_target_temperature(data_plan), 7.0)

        # Subsequent poll with unchanged updatedAt and still-stale hw_std preserves cooling & 7.0°C
        merged_plan2, _ = coordinator_mod.reconcile_settings(
            api_planning, hw_std, prev_updated_at=updated2, sync_state=sync_state
        )
        data_plan2 = {"setting": merged_plan2, "details": {"settings": hw_std}}
        self.assertEqual(sensor_mod._get_mode(data_plan2), "cooling")
        self.assertEqual(sensor_mod._get_target_temperature(data_plan2), 7.0)

    def test_target_temperature_reports_exact_value_without_guessing_from_mode(self) -> None:
        data = {
            "setting": {
                "_reconciled": True,
                "mode": "eco",
                "ecoModeEnabled": True,
                "temperature": 3,
                "temperatureMax": 7,
            },
            "details": {
                "settings": {
                    "temperature": 3,
                    "temperatureMax": 7,
                    "ecoModeEnabled": True,
                }
            },
        }
        self.assertEqual(sensor_mod._get_mode(data), "eco")
        self.assertEqual(sensor_mod._get_target_temperature(data), 3.0)




class TestStandbyPowerDetection(unittest.TestCase):
    def test_parse_power_watts(self) -> None:
        self.assertEqual(coordinator_mod.parse_power_watts("2.4"), 2.4)
        self.assertEqual(coordinator_mod.parse_power_watts("0.002", "kW"), 2.0)
        self.assertEqual(coordinator_mod.parse_power_watts(0), 0.0)
        self.assertIsNone(coordinator_mod.parse_power_watts("unavailable"))
        self.assertIsNone(coordinator_mod.parse_power_watts("unknown"))
        self.assertIsNone(coordinator_mod.parse_power_watts(-1))

    def test_delay_on_and_hysteresis_below_threshold(self) -> None:
        from datetime import datetime, timedelta, timezone

        t0 = datetime(2026, 10, 6, 20, 0, 0, tzinfo=timezone.utc)
        # First reading at 1.8 W <= 5.0 W with 120s delay -> not standby yet, 120s remaining
        is_standby, since, remaining = coordinator_mod.evaluate_power_standby(
            power_watts=1.8,
            threshold=5.0,
            delay_seconds=120,
            low_power_since=None,
            now=t0,
        )
        self.assertFalse(is_standby)
        self.assertEqual(since, t0)
        self.assertEqual(remaining, 120.0)

        # Minor fluctuation at t0 + 30s (1.9 W <= 5.0 W) keeps original `since`
        t1 = t0 + timedelta(seconds=30)
        is_standby, since2, remaining2 = coordinator_mod.evaluate_power_standby(
            power_watts=1.9,
            threshold=5.0,
            delay_seconds=120,
            low_power_since=since,
            now=t1,
        )
        self.assertFalse(is_standby)
        self.assertEqual(since2, t0)
        self.assertEqual(remaining2, 90.0)

        # At t0 + 120s -> standby is now True!
        t2 = t0 + timedelta(seconds=120)
        is_standby, since3, remaining3 = coordinator_mod.evaluate_power_standby(
            power_watts=1.7,
            threshold=5.0,
            delay_seconds=120,
            low_power_since=since2,
            now=t2,
        )
        self.assertTrue(is_standby)
        self.assertEqual(since3, t0)
        self.assertIsNone(remaining3)

        # Power jumps to 65 W -> exits standby immediately and resets `since`
        t3 = t2 + timedelta(seconds=5)
        is_standby, since4, remaining4 = coordinator_mod.evaluate_power_standby(
            power_watts=65.0,
            threshold=5.0,
            delay_seconds=120,
            low_power_since=since3,
            now=t3,
        )
        self.assertFalse(is_standby)
        self.assertIsNone(since4)
        self.assertIsNone(remaining4)

    def test_mode_sensor_returns_standby_when_power_standby_active(self) -> None:
        data = {
            "_power_standby": True,
            "_power_watts": 1.8,
            "_power_entity_id": "sensor.prise_cuisine_g_power",
            "setting": {"mode": "standard", "ecoModeEnabled": False},
            "details": {"settings": {"ecoModeEnabled": False}},
        }
        self.assertEqual(sensor_mod._get_mode(data), "standby")
        attrs = sensor_mod._get_mode_attributes(data)
        self.assertTrue(attrs["standby_detected_by_power"])
        self.assertEqual(attrs["power_watts"], 1.8)

        data["_power_standby"] = False
        self.assertEqual(sensor_mod._get_mode(data), "standard")


if __name__ == "__main__":
    unittest.main()
