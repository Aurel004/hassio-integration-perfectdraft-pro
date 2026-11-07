"""Async API client for the PerfectDraft cloud service."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any

import aiohttp

from .const import (
    API_BASE_URL,
    API_KEY,
    COGNITO_CLIENT_ID,
    COGNITO_REGION,
    RECAPTCHA_ACTION,
    RECAPTCHA_DOMAIN,
    SELECTABLE_MODE_OPTIONS,
)
from .exceptions import (
    AuthenticationError,
    PerfectDraftApiError,
    PerfectDraftConnectionError,
)

_LOGGER = logging.getLogger(__name__)

COGNITO_IDP_URL = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/"
COGNITO_HEADERS = {
    "Content-Type": "application/x-amz-json-1.1",
    "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
}

_WRITABLE_SETTING_FIELDS = (
    "temperature",
    "temperatureMin",
    "temperatureMax",
    "temperatureUnit",
    "pressure",
    "boost",
    "volumeThreshold",
    "ecoModeEnabled",
    "ecoModeBeerTemperatureSetPoint",
    "ecoModeSchedulerEnabled",
    "ecoModeAbortedByAppliance",
    "mode",
)


def _parse_cognito_error(body: str) -> tuple[str | None, str | None]:
    """Return (error type, message) from a Cognito error body.

    Cognito answers ``{"__type": "...", "message": "..."}``, sometimes with a
    namespaced type (``com.amazonaws...#NotAuthorizedException``). A body that
    is not JSON, such as an HTML error page, becomes the message, truncated.
    """
    try:
        data = json.loads(body)
    except ValueError:
        return None, body.strip()[:200] or None
    if not isinstance(data, dict):
        return None, None
    code = data.get("__type")
    reason = data.get("message")
    if isinstance(code, str):
        code = code.rsplit("#", 1)[-1]
    else:
        code = None
    return code, reason if isinstance(reason, str) else None


class PerfectDraftApiClient:
    """Client for api.perfectdraft.com."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_token: str | None = None,
        refresh_token: str | None = None,
        base_url: str = API_BASE_URL,
    ) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._access_token = access_token
        self._id_token: str | None = None
        self._refresh_token = refresh_token

    @property
    def access_token(self) -> str | None:
        return self._access_token

    @property
    def id_token(self) -> str | None:
        return self._id_token

    @property
    def refresh_token(self) -> str | None:
        return self._refresh_token

    def set_tokens(
        self,
        access_token: str | None,
        id_token: str | None = None,
        refresh_token: str | None = None,
    ) -> None:
        self._access_token = access_token
        self._id_token = id_token
        if refresh_token is not None:
            self._refresh_token = refresh_token

    async def authenticate(
        self, email: str, password: str, recaptcha_token: str
    ) -> dict[str, str]:
        """Sign in directly against Cognito with a reCAPTCHA token.

        The token must be generated from a real browser on perfectdraft.com
        using the web reCAPTCHA Enterprise key with action Magento/login.
        PerfectDraft's PreAuthentication Lambda also requires the shop's
        origin as ``domain``; the api.perfectdraft.com sign-in gateway does
        not forward it, so sign-in cannot go through the gateway.
        """
        payload = {
            "AuthFlow": "USER_PASSWORD_AUTH",
            "ClientId": COGNITO_CLIENT_ID,
            "AuthParameters": {"USERNAME": email, "PASSWORD": password},
            "ClientMetadata": {
                "recaptchaToken": recaptcha_token,
                "recaptchaAction": RECAPTCHA_ACTION,
                "domain": RECAPTCHA_DOMAIN,
            },
        }

        try:
            async with self._session.post(
                COGNITO_IDP_URL, json=payload, headers=COGNITO_HEADERS
            ) as resp:
                if resp.status in (400, 401, 403):
                    body = await resp.text()
                    code, reason = _parse_cognito_error(body)
                    raise AuthenticationError(
                        f"Sign-in rejected ({resp.status}): {body}",
                        code=code,
                        reason=reason,
                    )
                if resp.status != 200:
                    body = await resp.text()
                    raise PerfectDraftApiError(resp.status, body)
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise PerfectDraftConnectionError(str(exc)) from exc

        result = data.get("AuthenticationResult") if isinstance(data, dict) else None
        if not isinstance(result, dict) or not all(
            result.get(key) for key in ("AccessToken", "IdToken", "RefreshToken")
        ):
            challenge = data.get("ChallengeName") if isinstance(data, dict) else None
            raise AuthenticationError(
                f"Sign-in returned no tokens (challenge: {challenge})",
                code=challenge,
                reason=f"Sign-in requires {challenge}" if challenge else None,
            )

        self._access_token = result["AccessToken"]
        self._id_token = result["IdToken"]
        self._refresh_token = result["RefreshToken"]

        _LOGGER.debug("Authenticated successfully")
        return result

    async def refresh_access_token(self) -> dict[str, Any]:
        """Refresh tokens directly via AWS Cognito (bypasses API gateway + reCAPTCHA)."""
        if not self._refresh_token:
            raise AuthenticationError("No refresh token available")

        payload = {
            "AuthFlow": "REFRESH_TOKEN_AUTH",
            "ClientId": COGNITO_CLIENT_ID,
            "AuthParameters": {
                "REFRESH_TOKEN": self._refresh_token,
            },
        }

        try:
            async with self._session.post(
                COGNITO_IDP_URL, json=payload, headers=COGNITO_HEADERS
            ) as resp:
                if resp.status in (400, 401, 403):
                    body = await resp.text()
                    code, reason = _parse_cognito_error(body)
                    raise AuthenticationError(
                        f"Token refresh rejected ({resp.status}): {body}",
                        code=code,
                        reason=reason,
                    )
                if resp.status != 200:
                    body = await resp.text()
                    raise PerfectDraftApiError(resp.status, body)
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise PerfectDraftConnectionError(str(exc)) from exc

        result = data.get("AuthenticationResult", {})
        if "AccessToken" not in result:
            raise AuthenticationError(
                "Refresh response missing AccessToken: " + json.dumps(data)
            )

        self._access_token = result["AccessToken"]
        if "IdToken" in result:
            self._id_token = result["IdToken"]
        if "RefreshToken" in result:
            self._refresh_token = result["RefreshToken"]

        _LOGGER.debug("Refreshed tokens successfully")
        return result

    async def _request(
        self,
        method: str,
        path: str,
        authenticated: bool = True,
        retry_on_auth_failure: bool = True,
        **kwargs: Any,
    ) -> Any:
        url = f"{self._base}/{path.lstrip('/')}"
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("x-api-key", API_KEY)
        headers.setdefault("Accept", "application/ld+json")

        if authenticated:
            if not self._access_token:
                raise AuthenticationError("Not authenticated")
            headers["Authorization"] = f"Bearer {self._access_token}"

        try:
            async with self._session.request(
                method, url, headers=headers, **kwargs
            ) as resp:
                if resp.status == 401 and retry_on_auth_failure:
                    _LOGGER.info("Received 401, attempting token refresh")
                    try:
                        await self.refresh_access_token()
                    except AuthenticationError as exc:
                        _LOGGER.warning("Token refresh failed: %s", exc)
                        raise
                    return await self._request(
                        method,
                        path,
                        authenticated=authenticated,
                        retry_on_auth_failure=False,
                        headers=headers,
                        **kwargs,
                    )

                if resp.status in (401, 403):
                    body = await resp.text()
                    raise AuthenticationError(f"HTTP {resp.status}: {body}")
                if resp.status >= 400:
                    body = await resp.text()
                    raise PerfectDraftApiError(resp.status, body)
                return await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise PerfectDraftConnectionError(str(exc)) from exc

    async def get_user_profile(self) -> dict[str, Any]:
        """GET /api/me."""
        return await self._request("GET", "/api/me")

    async def get_machines(self) -> list[dict[str, Any]]:
        """Return the user's machines extracted from get_user_profile()."""
        profile = await self.get_user_profile()
        machines = profile.get("perfectdraftMachines")
        if isinstance(machines, list):
            return machines
        return []

    async def get_machine(self, machine_id: str) -> dict[str, Any]:
        """GET /api/perfectdraft_machines/{id} — returns full machine details."""
        return await self._request(
            "GET", f"/api/perfectdraft_machines/{machine_id}"
        )

    async def get_machine_settings(self, setting_id: str | int) -> dict[str, Any]:
        """GET /api/perfectdraft_machine_settings/{setting_id} — full setting resource.

        Includes fields omitted from the machine_pd_pro_read group such as
        ``ecoModeEnabled``, ``ecoModeSchedulerEnabled``, and ``updatedAt``.
        """
        return await self._request(
            "GET", f"/api/perfectdraft_machine_settings/{setting_id}"
        )

    async def get_machine_active_keg(self, machine_id: str) -> dict[str, Any]:
        """GET the machine serialized with the active-keg group.

        Returns only the fields permitted in ``perfectdraft_keg_active_read``,
        including ``activeKeg`` with nested ``product`` information.
        """
        return await self._request(
            "GET",
            f"/api/perfectdraft_machines/{machine_id}",
            params={"groups[]": "perfectdraft_keg_active_read"},
        )

    async def update_machine_settings(
        self,
        setting_id: str | int | None,
        current_setting: dict[str, Any] | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        """PUT /api/perfectdraft_machine_settings/{setting_id}.

        Preserves existing writable fields from ``current_setting`` so a PUT
        does not reset unrelated fields to server defaults.
        """
        if setting_id is None or isinstance(setting_id, bool):
            raise PerfectDraftApiError(400, "Invalid machine setting ID")
        sid = str(setting_id).strip()
        if not sid or sid == "unknown":
            raise PerfectDraftApiError(400, "Invalid machine setting ID")

        if "mode" in changes and changes["mode"] not in SELECTABLE_MODE_OPTIONS:
            raise PerfectDraftApiError(400, f"Unsupported mode: {changes['mode']}")

        payload: dict[str, Any] = {}
        if isinstance(current_setting, dict):
            for key in _WRITABLE_SETTING_FIELDS:
                if key in current_setting and current_setting[key] is not None:
                    payload[key] = current_setting[key]
        for key, val in changes.items():
            if key in _WRITABLE_SETTING_FIELDS:
                payload[key] = val

        # The PerfectDraft backend PHP match($mode) only handles selectable modes
        # ("standard", "eco", "boost") and throws a 500 UnhandledMatchError if a
        # read-only appliance state ("standby", "cooling") is sent back in PUT.
        if "mode" in payload and payload["mode"] not in SELECTABLE_MODE_OPTIONS:
            if payload.get("boost") is True:
                payload["mode"] = "boost"
            elif payload.get("ecoModeEnabled") is True:
                payload["mode"] = "eco"
            else:
                payload["mode"] = "standard"

        return await self._request(
            "PUT",
            f"/api/perfectdraft_machine_settings/{sid}",
            json=payload,
        )

    async def set_active_keg(
        self,
        machine_id: str | int | None,
        product_id: int | str | None,
        inserted_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        """POST /api/perfectdraft_machine_kegs — record the currently tapped keg."""
        if machine_id is None or isinstance(machine_id, bool):
            raise PerfectDraftApiError(400, "Invalid machine ID")
        mid = str(machine_id).strip()
        if not mid or mid == "unknown":
            raise PerfectDraftApiError(400, "Invalid machine ID")

        if product_id is None or isinstance(product_id, bool):
            raise PerfectDraftApiError(400, "Invalid keg product ID")
        try:
            pid = int(product_id)
        except (TypeError, ValueError) as exc:
            raise PerfectDraftApiError(400, f"Invalid keg product ID: {product_id}") from exc
        if pid <= 0:
            raise PerfectDraftApiError(400, f"Invalid keg product ID: {product_id}")

        if isinstance(inserted_at, str) and inserted_at.strip():
            inserted_str = inserted_at.strip()
        else:
            ts = (
                inserted_at
                if isinstance(inserted_at, datetime)
                else datetime.now(timezone.utc)
            ).astimezone(timezone.utc)
            inserted_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")

        payload = {
            "machine": f"/api/perfectdraft_machines/{mid}",
            "keg": f"/api/products/{pid}",
            "insertedAt": inserted_str,
        }
        return await self._request(
            "POST", "/api/perfectdraft_machine_kegs", json=payload
        )
