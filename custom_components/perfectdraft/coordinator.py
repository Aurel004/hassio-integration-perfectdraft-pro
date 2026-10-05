"""DataUpdateCoordinator for PerfectDraft."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import PerfectDraftApiClient
from .const import (
    CONF_MACHINE_ID,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
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

    def update_interval_from_options(self) -> None:
        """Re-read the polling interval from config entry options."""
        interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        self.update_interval = timedelta(seconds=interval)
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

        # Fetch full machine settings (includes ecoModeEnabled, ecoModeSchedulerEnabled,
        # updatedAt, etc. which are omitted from the machine_pd_pro_read group).
        setting_id = get_setting_id(details)
        if setting_id:
            try:
                full_setting = await self.client.get_machine_settings(setting_id)
                if isinstance(full_setting, dict):
                    merged_setting = (
                        dict(details["setting"])
                        if isinstance(details.get("setting"), dict)
                        else {}
                    )
                    merged_setting.update(full_setting)
                    details["setting"] = merged_setting
            except (PerfectDraftApiError, PerfectDraftConnectionError) as err:
                _LOGGER.debug("Full machine setting lookup failed: %s", err)

        # The active keg lives behind a separate serialization group. Treat it
        # as best-effort so a failure here doesn't take the whole device down.
        try:
            active = await self.client.get_machine_active_keg(machine_id)
            details["kegActive"] = active.get("kegActive")
        except (PerfectDraftApiError, PerfectDraftConnectionError) as err:
            _LOGGER.debug("Active keg lookup failed: %s", err)

        return details

    async def async_update_settings(self, **changes: Any) -> None:
        """Update machine settings via the API and refresh local coordinator state."""
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
            raise HomeAssistantError(
                f"Failed to update PerfectDraft settings: {err}"
            ) from err

        if isinstance(self.data, dict):
            merged = dict(current_setting)
            if isinstance(updated, dict):
                merged.update(updated)
            merged.update(changes)
            self.data["setting"] = merged
            self.async_update_listeners()

        await self.async_request_refresh()

    async def async_set_active_keg(self, product_id: int) -> None:
        """Set the active keg via the API and refresh local coordinator state."""
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
            raise HomeAssistantError(
                f"Failed to set active PerfectDraft keg: {err}"
            ) from err

        if isinstance(self.data, dict):
            keg_active = {
                "keg": f"/api/products/{int(product_id)}",
                "insertedAt": now.isoformat(),
            }
            if isinstance(created, dict):
                if created.get("keg"):
                    keg_active["keg"] = created["keg"]
                if created.get("insertedAt"):
                    keg_active["insertedAt"] = created["insertedAt"]
            self.data["kegActive"] = keg_active
            self.async_update_listeners()

        await self.async_request_refresh()
