"""Unit tests for PerfectDraftApiClient.authenticate() against Cognito."""
import asyncio
import importlib
import json
import os
import sys
import types
import unittest

PKG_DIR = os.path.join(
    os.path.dirname(__file__), "..", "custom_components", "perfectdraft"
)

try:
    import aiohttp  # noqa: F401
except ImportError:  # pragma: no cover - depends on the environment
    aiohttp = types.ModuleType("aiohttp")

    class ClientError(Exception):
        """Stand-in for aiohttp.ClientError."""

    aiohttp.ClientError = ClientError
    aiohttp.ClientSession = object
    sys.modules["aiohttp"] = aiohttp

if "perfectdraft" not in sys.modules:
    _pkg = types.ModuleType("perfectdraft")
    _pkg.__path__ = [PKG_DIR]
    sys.modules["perfectdraft"] = _pkg

api = importlib.import_module("perfectdraft.api")
const = importlib.import_module("perfectdraft.const")
exceptions = importlib.import_module("perfectdraft.exceptions")


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def text(self):
        return self._body

    async def json(self, content_type="application/json"):
        if content_type is not None:
            raise AssertionError("json() must be called with content_type=None")
        return json.loads(self._body)


class FakeContext:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    async def __aenter__(self):
        if self._error is not None:
            raise self._error
        return self._response

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, status=200, body="{}", error=None):
        self.status = status
        self.body = body
        self.error = error
        self.calls = []

    def post(self, url, json=None, headers=None):
        self.calls.append({"method": "POST", "url": url, "json": json, "headers": headers})
        return FakeContext(FakeResponse(self.status, self.body), self.error)

    def request(self, method, url, headers=None, **kwargs):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, **kwargs}
        )
        return FakeContext(FakeResponse(self.status, self.body), self.error)


SUCCESS_BODY = json.dumps(
    {
        "AuthenticationResult": {
            "AccessToken": "access-abc",
            "IdToken": "id-def",
            "RefreshToken": "refresh-ghi",
            "ExpiresIn": 3600,
            "TokenType": "Bearer",
        },
        "ChallengeParameters": {},
    }
)


def run(coro):
    return asyncio.run(coro)


class TestAuthenticate(unittest.TestCase):
    def test_posts_initiate_auth_to_cognito_with_domain(self):
        session = FakeSession(body=SUCCESS_BODY)
        client = api.PerfectDraftApiClient(session)

        result = run(client.authenticate("me@example.com", "hunter2", "captcha-token"))

        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["url"], "https://cognito-idp.eu-west-1.amazonaws.com/")
        self.assertEqual(
            call["headers"],
            {
                "Content-Type": "application/x-amz-json-1.1",
                "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
            },
        )
        self.assertEqual(
            call["json"],
            {
                "AuthFlow": "USER_PASSWORD_AUTH",
                "ClientId": const.COGNITO_CLIENT_ID,
                "AuthParameters": {
                    "USERNAME": "me@example.com",
                    "PASSWORD": "hunter2",
                },
                "ClientMetadata": {
                    "recaptchaToken": "captcha-token",
                    "recaptchaAction": "Magento/login",
                    "domain": "https://www.perfectdraft.com",
                },
            },
        )
        self.assertEqual(
            result,
            {
                "AccessToken": "access-abc",
                "IdToken": "id-def",
                "RefreshToken": "refresh-ghi",
            },
        )
        self.assertEqual(client.access_token, "access-abc")
        self.assertEqual(client.id_token, "id-def")
        self.assertEqual(client.refresh_token, "refresh-ghi")

    def test_challenge_instead_of_tokens_raises_auth_error(self):
        body = json.dumps({"ChallengeName": "NEW_PASSWORD_REQUIRED"})
        client = api.PerfectDraftApiClient(FakeSession(body=body))
        with self.assertRaises(exceptions.AuthenticationError):
            run(client.authenticate("a", "b", "c"))
        self.assertIsNone(client.access_token)

    def test_400_raises_auth_error(self):
        body = json.dumps(
            {
                "__type": "UserLambdaValidationException",
                "message": "PreAuthentication failed with error 'domain'.",
            }
        )
        client = api.PerfectDraftApiClient(FakeSession(status=400, body=body))
        with self.assertRaises(exceptions.AuthenticationError) as ctx:
            run(client.authenticate("a", "b", "c"))
        self.assertIn("400", str(ctx.exception))

    def test_500_raises_api_error(self):
        client = api.PerfectDraftApiClient(
            FakeSession(status=500, body="Internal Server Error")
        )
        with self.assertRaises(exceptions.PerfectDraftApiError) as ctx:
            run(client.authenticate("a", "b", "c"))
        self.assertEqual(ctx.exception.status, 500)

    def test_network_error_raises_connection_error(self):
        client = api.PerfectDraftApiClient(
            FakeSession(error=aiohttp.ClientError("boom"))
        )
        with self.assertRaises(exceptions.PerfectDraftConnectionError):
            run(client.authenticate("a", "b", "c"))


class TestMachineControlApi(unittest.TestCase):
    def test_update_machine_settings_preserves_writable_fields(self):
        updated = json.dumps({"id": 42, "temperature": 4, "mode": "eco", "boost": False})
        session = FakeSession(status=200, body=updated)
        client = api.PerfectDraftApiClient(session)
        client.set_tokens(access_token="token-123")

        current_setting = {
            "id": 42,
            "temperature": 3,
            "temperatureMin": 0,
            "temperatureMax": 12,
            "temperatureUnit": "C",
            "mode": "standard",
            "boost": False,
            "ecoModeEnabled": False,
            "machines": ["/api/perfectdraft_machines/42"],  # read-only field
        }

        result = run(
            client.update_machine_settings(
                42, current_setting=current_setting, temperature=4, mode="eco"
            )
        )
        self.assertEqual(result["temperature"], 4)
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["method"], "PUT")
        self.assertEqual(
            call["url"],
            "https://api.perfectdraft.com/api/perfectdraft_machine_settings/42",
        )
        self.assertEqual(
            call["json"],
            {
                "temperature": 4,
                "temperatureMin": 0,
                "temperatureMax": 12,
                "temperatureUnit": "C",
                "mode": "eco",
                "boost": False,
                "ecoModeEnabled": False,
            },
        )
        self.assertNotIn("machines", call["json"])

    def test_update_machine_settings_rejects_invalid_id(self):
        client = api.PerfectDraftApiClient(FakeSession())
        client.set_tokens(access_token="token-123")
        with self.assertRaises(exceptions.PerfectDraftApiError):
            run(client.update_machine_settings(None, temperature=3))

    def test_set_active_keg_posts_iri_references(self):
        created = json.dumps(
            {
                "id": 999,
                "machine": "/api/perfectdraft_machines/42",
                "keg": "/api/products/1775",
                "insertedAt": "2026-10-05T12:00:00Z",
            }
        )
        session = FakeSession(status=201, body=created)
        client = api.PerfectDraftApiClient(session)
        client.set_tokens(access_token="token-123")

        result = run(
            client.set_active_keg(42, 1775, inserted_at="2026-10-05T12:00:00Z")
        )
        self.assertEqual(result["id"], 999)
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(
            call["url"],
            "https://api.perfectdraft.com/api/perfectdraft_machine_kegs",
        )
        self.assertEqual(
            call["json"],
            {
                "machine": "/api/perfectdraft_machines/42",
                "keg": "/api/products/1775",
                "insertedAt": "2026-10-05T12:00:00Z",
            },
        )


if __name__ == "__main__":
    unittest.main()

