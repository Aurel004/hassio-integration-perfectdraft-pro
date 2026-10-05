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
        self.calls.append({"url": url, "json": json, "headers": headers})
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


if __name__ == "__main__":
    unittest.main()
