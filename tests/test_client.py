"""Tests for the IG API client and session management."""

from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
from httpx import Response

from src.core.api.client import IGAPIError, IGClient
from src.core.api.session import IGSession, OAuthToken
from src.core.config import Settings


@pytest.fixture
def settings() -> Settings:
    """Test settings with demo configuration."""
    return Settings(
        ig_env="demo",
        ig_api_key="test_api_key",
        ig_username="test_user",
        ig_password="test_pass",
        ig_account_id="ABC123",
    )


class TestOAuthToken:
    """Tests for the OAuthToken container."""

    def test_not_expired(self):
        token = OAuthToken(
            access_token="access_123",
            refresh_token="refresh_456",
            expires_in=60,
        )
        assert not token.is_expired

    def test_expired(self):
        token = OAuthToken(
            access_token="access_123",
            refresh_token="refresh_456",
            expires_in=-10,  # Already expired
        )
        assert token.is_expired


class TestIGSession:
    """Tests for the IGSession class."""

    def test_not_authenticated_initially(self, settings):
        session = IGSession(settings)
        assert not session.is_authenticated

    def test_auth_headers_raises_without_login(self, settings):
        session = IGSession(settings)
        with pytest.raises(RuntimeError, match="Not authenticated"):
            _ = session.auth_headers


@respx.mock
@pytest.mark.asyncio
async def test_client_login(settings):
    """Test that IGClient authenticates on context entry."""
    respx.post("https://demo-api.ig.com/gateway/deal/session").mock(
        return_value=Response(
            200,
            json={
                "oauthToken": {
                    "access_token": "test_access",
                    "refresh_token": "test_refresh",
                    "token_type": "Bearer",
                    "scope": "profile",
                    "expires_in": "1800",
                },
                "accountId": "ABC123",
            },
        )
    )

    async with IGClient(settings) as client:
        assert client._session.is_authenticated


@respx.mock
@pytest.mark.asyncio
async def test_client_get(settings):
    """Test a simple GET request through the client."""
    respx.post("https://demo-api.ig.com/gateway/deal/session").mock(
        return_value=Response(
            200,
            json={
                "oauthToken": {
                    "access_token": "test_access",
                    "refresh_token": "test_refresh",
                    "token_type": "Bearer",
                    "scope": "profile",
                    "expires_in": "1800",
                },
            },
        )
    )
    respx.get("https://demo-api.ig.com/gateway/deal/accounts").mock(
        return_value=Response(
            200, json={"accounts": [{"accountId": "ABC123", "accountName": "Test"}]}
        )
    )

    async with IGClient(settings) as client:
        data = await client.get("/accounts", version=1)
        assert len(data["accounts"]) == 1
        assert data["accounts"][0]["accountId"] == "ABC123"


@respx.mock
@pytest.mark.asyncio
async def test_get_suppress_error_logging_skips_guard_and_log(settings):
    """An expected (probed) error still raises but is not recorded or reported."""
    respx.post("https://demo-api.ig.com/gateway/deal/session").mock(
        return_value=Response(
            200,
            json={
                "oauthToken": {
                    "access_token": "test_access",
                    "refresh_token": "test_refresh",
                    "token_type": "Bearer",
                    "scope": "profile",
                    "expires_in": "1800",
                },
            },
        )
    )
    respx.get("https://demo-api.ig.com/gateway/deal/markets").mock(
        return_value=Response(500, json={"errorCode": "Transformation failure."})
    )

    error_log = MagicMock()
    guard = MagicMock()
    guard.pre_request = AsyncMock()
    async with IGClient(settings, error_log=error_log, guard=guard) as client:
        with pytest.raises(IGAPIError):
            await client.get(
                "/markets?epics=BAD.EPIC",
                version=2,
                suppress_error_logging=True,
            )

    error_log.record.assert_not_called()
    guard.on_ig_error.assert_not_called()


@respx.mock
@pytest.mark.asyncio
async def test_login_captures_streaming_endpoint(settings):
    """The v3 login response endpoint + accountId are captured for streaming."""
    respx.post("https://demo-api.ig.com/gateway/deal/session").mock(
        return_value=Response(
            200,
            json={
                "oauthToken": {
                    "access_token": "test_access",
                    "refresh_token": "test_refresh",
                    "token_type": "Bearer",
                    "scope": "profile",
                    "expires_in": "1800",
                },
                "accountId": "ZZZ999",
                "lightstreamerEndpoint": "https://demo-stream.ig.com",
            },
        )
    )

    async with IGClient(settings) as client:
        assert client.session is client._session
        assert client.session.lightstreamer_endpoint == "https://demo-stream.ig.com"
        assert client.session.account_id == "ZZZ999"


@respx.mock
@pytest.mark.asyncio
async def test_fetch_session_tokens_reads_headers(settings):
    """fetch_session_tokens returns CST / X-SECURITY-TOKEN from response headers."""
    respx.post("https://demo-api.ig.com/gateway/deal/session").mock(
        return_value=Response(
            200,
            json={
                "oauthToken": {
                    "access_token": "test_access",
                    "refresh_token": "test_refresh",
                    "token_type": "Bearer",
                    "scope": "profile",
                    "expires_in": "1800",
                },
            },
        )
    )
    respx.get(
        "https://demo-api.ig.com/gateway/deal/session?fetchSessionTokens=true"
    ).mock(
        return_value=Response(
            200,
            json={},
            headers={"CST": "cst-value", "X-SECURITY-TOKEN": "xst-value"},
        )
    )

    async with IGClient(settings) as client:
        cst, xst = await client.session.fetch_session_tokens(client.http)
        assert cst == "cst-value"
        assert xst == "xst-value"
