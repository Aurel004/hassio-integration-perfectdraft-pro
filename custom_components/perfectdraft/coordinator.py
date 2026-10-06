"""DataUpdateCoordinator for PerfectDraft."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

try:
    from homeassistant.helpers.event import (
        async_call_later,
        async_track_state_change_event,
    )
except ImportError:  # pragma: no cover - fallback for lightweight test stubs
    async_call_later = None  # type: ignore[assignment]
    async_track_state_change_event = None  # type: ignore[assignment]

from .api import PerfectDraftApiClient
from .const import (
    CONF_MACHINE_ID,
    CONF_POWER_ENTITY_ID,
    CONF_SCAN_INTERVAL,
    CONF_STANDBY_DELAY,
    CONF_STANDBY_POWER_THRESHOLD,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_STANDBY_DELAY,
    DEFAULT_STANDBY_POWER_THRESHOLD,
    DOMAIN,
)
from .exceptions import (
    AuthenticationError,
    PerfectDraftApiError,
    PerfectDraftConnectionError,
    PerfectDraftError,
)

_LOGGER = logging.getLogger(__name__)


def get_setting_id(data: dict[str, Any] | None) -> str | None:
    """Extract the machine setting resource ID from coordinator data."""
    if not isinstance(data, dict):
        return None
    setting = data.get("setting")
    if isinstance(setting, dict):
        if setting.get("id") is not None:
            return str(setting["id"])
        iri = setting.get("@id")
        if isinstance(iri, str) and "/" in iri:
            tail = iri.rstrip("/").rsplit("/", 1)[-1]
            if tail:
                return tail
    machine_id = data.get("_machine_id") or data.get("id")
    return str(machine_id) if machine_id is not None else None


_APPLIANCE_SETTING_KEYS = (
    "temperature",
    "temperatureMin",
    "temperatureMax",
    "temperatureUnit",
    "ecoModeEnabled",
    "ecoModeSchedulerEnabled",
)


def parse_power_watts(raw_state: Any) -> float | None:
    """Parse a Home Assistant sensor state into Watts, or None if unavailable."""
    if raw_state is None or isinstance(raw_state, bool):
        return None
    if isinstance(raw_state, str) and raw_state.strip().lower() in (
        "",
        "unknown",
        "unavailable",
        "none",
    ):
        return None
    try:
        val = float(raw_state)
    except (TypeError, ValueError):
        return None
    return val if val >= 0.0 else None


def evaluate_power_standby(
    power_watts: float | None,
    threshold: float,
    delay_seconds: int,
    low_power_since: datetime | None,
    now: datetime,
) -> tuple[bool, datetime | None, float | None]:
    """Evaluate whether the appliance is in standby based on smart plug power.

    Returns ``(is_standby, new_low_power_since, remaining_delay_seconds)``.
    """
    if power_watts is None or power_watts < 0.0 or power_watts > threshold:
        return False, None, None

    since = low_power_since if low_power_since is not None else now
    elapsed = max(0.0, (now - since).total_seconds())
    delay = max(0, int(delay_seconds))
    if elapsed >= delay:
        return True, since, None

    return False, since, max(0.1, delay - elapsed)


def reconcile_settings(
    api_setting: dict[str, Any] | None,
    details_settings: dict[str, Any] | None,
    prev_updated_at: str | None,
) -> tuple[dict[str, Any], str | None]:
    """Reconcile the cloud setting resource with the live appliance shadow.

    When ``api_setting["updatedAt"]`` has not changed since the previous poll
    (or on initial startup), ``details_settings`` from the machine's live
    telemetry is authoritative for shared fields so changes made on the
    appliance or mobile app are reflected and any unapplied setting reverts to
    the real hardware state.
    """
    merged: dict[str, Any] = dict(api_setting) if isinstance(api_setting, dict) else {}
    hw: dict[str, Any] = (
        dict(details_settings) if isinstance(details_settings, dict) else {}
    )

    updated_at = merged.get("updatedAt")
    updated_at_str = str(updated_at) if updated_at is not None else None
    setting_just_updated = (
        prev_updated_at is not None
        and updated_at_str is not None
        and updated_at_str != prev_updated_at
    )

    for key, val in hw.items():
        if val is not None:
            merged.setdefault(key, val)

    if hw and not setting_just_updated:
        for key in _APPLIANCE_SETTING_KEYS:
            if key in hw and hw[key] is not None:
                merged[key] = hw[key]

    if merged.get("ecoModeSchedulerEnabled") is True:
        merged["mode"] = "cooling"
    else:
        if (
            merged.get("ecoModeSchedulerEnabled") is False
            and merged.get("mode") == "cooling"
        ):
            merged["mode"] = (
                "eco" if merged.get("ecoModeEnabled") is True else "standard"
            )
        if merged.get("ecoModeEnabled") is False and merged.get("mode") == "eco":
            merged["mode"] = "standard"
        elif (
            merged.get("ecoModeEnabled") is True
            and merged.get("mode") in (None, "standard")
            and merged.get("boost") is not True
        ):
            merged["mode"] = "eco"

    return merged, updated_at_str


class PerfectDraftDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Single coordinator that polls the PerfectDraft API for all entities."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: PerfectDraftApiClient,
        config_entry: ConfigEntry,
    ) -> None:
        self.client = client
        self._prev_setting_updated_at: str | None = None
        self._unsub_power_listener: Callable[[], None] | None = None
        self._unsub_standby_timer: Callable[[], None] | None = None
        self._tracked_power_entity_id: str | None = None
        self._low_power_since: datetime | None = None
        self._power_standby: bool = False
        self._power_watts: float | None = None
        interval = config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
            config_entry=config_entry,
        )
        self.setup_power_listener()

    def _cancel_standby_timer(self) -> None:
        if self._unsub_standby_timer is not None:
            self._unsub_standby_timer()
            self._unsub_standby_timer = None

    def async_shutdown_power_listener(self) -> None:
        """Unsubscribe from smart plug state changes and pending delay timers."""
        self._cancel_standby_timer()
        if self._unsub_power_listener is not None:
            self._unsub_power_listener()
            self._unsub_power_listener = None
        self._tracked_power_entity_id = None

    def setup_power_listener(self) -> None:
        """Subscribe to the configured smart plug power sensor if present."""
        options = self.config_entry.options
        raw_entity = options.get(CONF_POWER_ENTITY_ID)
        entity_id = raw_entity.strip() if isinstance(raw_entity, str) and raw_entity.strip() else None

        if entity_id != self._tracked_power_entity_id:
            self.async_shutdown_power_listener()
            self._low_power_since = None
            self._tracked_power_entity_id = entity_id
            if (
                entity_id
                and self.hass is not None
                and async_track_state_change_event is not None
            ):
                self._unsub_power_listener = async_track_state_change_event(
                    self.hass,
                    [entity_id],
                    self._handle_power_state_change,
                )

    @callback
    def _handle_power_state_change(self, event: Any) -> None:
        """Re-evaluate standby state when the smart plug power sensor updates."""
        self._refresh_power_standby_state(notify=True)

    @callback
    def _handle_standby_timer(self, _now: datetime) -> None:
        """Callback fired when standby_delay has elapsed."""
        self._unsub_standby_timer = None
        self._refresh_power_standby_state(notify=True)

    def _refresh_power_standby_state(
        self,
        target: dict[str, Any] | None = None,
        *,
        notify: bool = False,
    ) -> None:
        """Evaluate smart plug power against threshold and delay_on."""
        self._cancel_standby_timer()
        options = self.config_entry.options
        raw_entity = options.get(CONF_POWER_ENTITY_ID)
        entity_id = raw_entity.strip() if isinstance(raw_entity, str) and raw_entity.strip() else None
        threshold = float(
            options.get(
                CONF_STANDBY_POWER_THRESHOLD,
                DEFAULT_STANDBY_POWER_THRESHOLD,
            )
        )
        delay_seconds = int(
            options.get(
                CONF_STANDBY_DELAY,
                DEFAULT_STANDBY_DELAY,
            )
        )

        now = datetime.now(timezone.utc)
        power_watts: float | None = None
        if entity_id and getattr(self.hass, "states", None) is not None:
            state_obj = self.hass.states.get(entity_id)
            if state_obj is not None:
                power_watts = parse_power_watts( getattr(state_obj, "state", None) )
                if (
                    self._low_power_since is None
                    and power_watts is not None
                    and 0.0 <= power_watts <= threshold
                ):
                    last_changed = getattr(state_obj, "last_changed", None)
                    if isinstance(last_changed, datetime):
                        self._low_power_since = (
                            last_changed
                            if last_changed.tzinfo is not None
                            else last_changed.replace(tzinfo=timezone.utc)
                        )

        is_standby, self._low_power_since, remaining = evaluate_power_standby(
            power_watts=power_watts,
            threshold=threshold,
            delay_seconds=delay_seconds,
            low_power_since=self._low_power_since,
            now=now,
        )

        if (
            remaining is not None
            and self.hass is not None
            and async_call_later is not None
        ):
            self._unsub_standby_timer = async_call_later(
                self.hass,
                remaining,
                self._handle_standby_timer,
            )

        changed = (
            is_standby != self._power_standby
            or power_watts != self._power_watts
        )
        self._power_standby = is_standby
        self._power_watts = power_watts

        for container in (target, self.data if isinstance(self.data, dict) else None):
            if isinstance(container, dict):
                container["_power_entity_id"] = entity_id
                container["_power_watts"] = power_watts
                container["_power_standby"] = is_standby

        if notify and changed:
            self.async_update_listeners()

    def update_interval_from_options(self) -> None:
        """Re-read the polling interval and standby power options."""
        interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        self.update_interval = timedelta(seconds=interval)
        self.setup_power_listener()
        self._refresh_power_standby_state(notify=True)
        _LOGGER.debug("Polling interval updated to %s seconds", interval)

    async def _async_update_data(self) -> dict[str, Any]:
        machine_id = self.config_entry.data.get(CONF_MACHINE_ID)

        try:
            if not machine_id:
                profile = await self.client.get_user_profile()
                machines = profile.get("perfectdraftMachines", [])
                if not machines:
                    raise UpdateFailed(
                        "No PerfectDraft machine found on this account"
                    )
                machine_id = str(machines[0].get("id", ""))

            details = await self.client.get_machine_details(machine_id)
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(
                "Authentication failed — please re-authenticate"
            ) from err
        except (PerfectDraftApiError, PerfectDraftConnectionError) as err:
            raise UpdateFailed(str(err)) from err

        details["_machine_id"] = machine_id

        merged_setting: dict[str, Any] = (
            dict(details["setting"])
            if isinstance(details.get("setting"), dict)
            else {}
        )

        # Fetch full machine settings (includes ecoModeEnabled, ecoModeSchedulerEnabled,
        # updatedAt, etc. which are omitted from the machine_pd_pro_read group).
        setting_id = get_setting_id(details)
        if setting_id:
            try:
                full_setting = await self.client.get_machine_settings(setting_id)
                if isinstance(full_setting, dict):
                    merged_setting.update(full_setting)
            except (PerfectDraftApiError, PerfectDraftConnectionError) as err:
                _LOGGER.debug("Full machine setting lookup failed: %s", err)

        details_payload = details.get("details")
        details_settings = (
            details_payload.get("settings")
            if isinstance(details_payload, dict)
            else None
        )
        reconciled_setting, self._prev_setting_updated_at = reconcile_settings(
            merged_setting,
            details_settings,
            self._prev_setting_updated_at,
        )
        details["setting"] = reconciled_setting

        # The active keg lives behind a separate serialization group. Treat it
        # as best-effort so a failure here doesn't take the whole device down.
        try:
            active = await self.client.get_machine_active_keg(machine_id)
            details["kegActive"] = active.get("kegActive")
        except (PerfectDraftApiError, PerfectDraftConnectionError) as err:
            _LOGGER.debug("Active keg lookup failed: %s", err)

        self._refresh_power_standby_state(target=details, notify=False)
        return details

    async def async_update_settings(self, **changes: Any) -> None:
        """Update machine settings via the API and refresh from the real server state."""
        data = self.data or {}
        setting_id = get_setting_id(data) or self.config_entry.data.get(CONF_MACHINE_ID)
        if not setting_id:
            raise HomeAssistantError("PerfectDraft setting ID is unavailable")

        current_setting = (
            dict(data["setting"]) if isinstance(data.get("setting"), dict) else {}
        )
        try:
            updated = await self.client.update_machine_settings(
                setting_id, current_setting, **changes
            )
        except PerfectDraftError as err:
            self.async_update_listeners()
            raise HomeAssistantError(
                f"Failed to update PerfectDraft settings: {err}"
            ) from err

        if isinstance(self.data, dict) and isinstance(updated, dict):
            merged = dict(current_setting)
            merged.update(updated)
            self.data["setting"] = merged
            self.async_update_listeners()

        await self.async_request_refresh()

    async def async_set_active_keg(self, product_id: int) -> None:
        """Set the active keg via the API and refresh from the real server state."""
        data = self.data or {}
        machine_id = data.get("_machine_id") or self.config_entry.data.get(
            CONF_MACHINE_ID
        )
        if not machine_id:
            raise HomeAssistantError("PerfectDraft machine ID is unavailable")

        now = datetime.now(timezone.utc)
        try:
            created = await self.client.set_active_keg(
                machine_id, int(product_id), now
            )
        except PerfectDraftError as err:
            self.async_update_listeners()
            raise HomeAssistantError(
                f"Failed to set active PerfectDraft keg: {err}"
            ) from err

        if isinstance(self.data, dict) and isinstance(created, dict):
            keg_active = dict(self.data.get("kegActive") or {})
            if created.get("keg"):
                keg_active["keg"] = created["keg"]
            if created.get("insertedAt"):
                keg_active["insertedAt"] = created["insertedAt"]
            if keg_active:
                self.data["kegActive"] = keg_active
                self.async_update_listeners()

        await self.async_request_refresh()
