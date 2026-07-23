"""Tests for the IG API client and session management."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
from httpx import Response

from src.core.api.client import (
    MARKET_ORDER_NOT_SUPPORTED_CODE,
    IGAPIError,
    IGClient,
)
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


@pytest.mark.asyncio
async def test_headers_built_after_auth_refresh(settings):
    """Regression: the bearer must be read AFTER _ensure_auth() refreshes it.

    IG v3 tokens live ~60s. Building the header before the refresh sent a dead
    bearer on the expiry edge → 401, which the APIQueue classifies as a
    non-retryable client error and drops for a non-GET (an order). The header is
    now built inside _request_context, after the refresh has run.
    """
    client = IGClient(settings)

    captured: dict = {}

    async def fake_post(url, json=None, headers=None):
        captured.update(headers or {})
        resp = MagicMock()
        resp.is_error = False
        resp.json.return_value = {"ok": True}
        return resp

    http = MagicMock()
    http.post = AsyncMock(side_effect=fake_post)
    client._http = http

    # Session carries a STALE bearer until ensure_valid_token() "refreshes" it.
    session = MagicMock()
    session.auth_headers = {"Authorization": "Bearer STALE"}

    async def refresh(_http):
        session.auth_headers = {"Authorization": "Bearer FRESH"}

    session.ensure_valid_token = AsyncMock(side_effect=refresh)
    client._session = session

    await client.post("/positions/otc", {"epic": "X"}, version=2)

    assert captured["Authorization"] == "Bearer FRESH"


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
async def test_expect_not_found_404_warns_and_skips_guard_and_log(settings, caplog):
    """An expected 404 (expect_not_found) still raises but logs at WARNING and is
    kept out of the persistent error log / guard — mirroring the APIQueue side."""
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
    respx.get("https://demo-api.ig.com/gateway/deal/confirms/REF").mock(
        return_value=Response(404, json={"errorCode": "error.service.execution.find"})
    )

    error_log = MagicMock()
    guard = MagicMock()
    guard.pre_request = AsyncMock()
    async with IGClient(settings, error_log=error_log, guard=guard) as client:
        with caplog.at_level(logging.WARNING, logger="src.core.api.client"):
            with pytest.raises(IGAPIError):
                await client.get("/confirms/REF", version=1, expect_not_found=True)

    # Logged, but at WARNING — never ERROR.
    records = [r for r in caplog.records if "/confirms/REF" in r.getMessage()]
    assert records and all(r.levelno == logging.WARNING for r in records)
    # Expected outcome: kept out of the persistent error log and away from the guard.
    error_log.record.assert_not_called()
    guard.on_ig_error.assert_not_called()


@respx.mock
@pytest.mark.asyncio
async def test_expect_not_found_flag_only_downgrades_404(settings):
    """expect_not_found only tolerates a 404 — any other status still errors and
    is recorded/reported normally."""
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
    respx.get("https://demo-api.ig.com/gateway/deal/confirms/REF").mock(
        return_value=Response(500, json={"errorCode": "Transformation failure."})
    )

    error_log = MagicMock()
    guard = MagicMock()
    guard.pre_request = AsyncMock()
    async with IGClient(settings, error_log=error_log, guard=guard) as client:
        with pytest.raises(IGAPIError):
            await client.get("/confirms/REF", version=1, expect_not_found=True)

    # A 500 is a genuine error even with expect_not_found set.
    error_log.record.assert_called_once()
    guard.on_ig_error.assert_called_once()


@respx.mock
@pytest.mark.asyncio
async def test_expect_market_order_rejection_warns_and_skips_guard_and_log(
    settings, caplog
):
    """A MARKET_ORDER_NOT_SUPPORTED_CODE rejection flagged as expected still raises
    but logs at WARNING and is kept out of the persistent error log / guard — the
    caller recovers by retrying as a marketable LIMIT."""
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
    respx.post("https://demo-api.ig.com/gateway/deal/positions/otc").mock(
        return_value=Response(400, json={"errorCode": MARKET_ORDER_NOT_SUPPORTED_CODE})
    )

    error_log = MagicMock()
    guard = MagicMock()
    guard.pre_request = AsyncMock()
    async with IGClient(settings, error_log=error_log, guard=guard) as client:
        with caplog.at_level(logging.WARNING, logger="src.core.api.client"):
            with pytest.raises(IGAPIError):
                await client.post(
                    "/positions/otc",
                    {"orderType": "MARKET"},
                    version=2,
                    expect_market_order_rejection=True,
                )

    # Logged, but at WARNING — never ERROR.
    records = [r for r in caplog.records if "/positions/otc" in r.getMessage()]
    assert records and all(r.levelno == logging.WARNING for r in records)
    # Expected outcome: kept out of the persistent error log and away from the guard.
    error_log.record.assert_not_called()
    guard.on_ig_error.assert_not_called()


@respx.mock
@pytest.mark.asyncio
async def test_expect_market_order_rejection_only_downgrades_matching_code(settings):
    """The flag only tolerates MARKET_ORDER_NOT_SUPPORTED_CODE — any other error
    on the same call is still recorded/reported normally."""
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
    respx.post("https://demo-api.ig.com/gateway/deal/positions/otc").mock(
        return_value=Response(400, json={"errorCode": "error.trading.otc.insufficient"})
    )

    error_log = MagicMock()
    guard = MagicMock()
    guard.pre_request = AsyncMock()
    async with IGClient(settings, error_log=error_log, guard=guard) as client:
        with pytest.raises(IGAPIError):
            await client.post(
                "/positions/otc",
                {"orderType": "MARKET"},
                version=2,
                expect_market_order_rejection=True,
            )

    # A different error code is a genuine error even with the flag set.
    error_log.record.assert_called_once()
    guard.on_ig_error.assert_called_once()


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


class TestSoftErrorLogLevel:
    """The API-key quota error logs at WARNING until it recurs within the window."""

    def test_unknown_code_is_error(self, settings):
        client = IGClient(settings)
        assert client._error_log_level("error.something.else") == logging.ERROR

    def test_soft_code_starts_at_warning(self, settings):
        client = IGClient(settings)
        assert (
            client._error_log_level("error.public-api.exceeded-api-key-allowance")
            == logging.WARNING
        )

    def test_soft_code_escalates_after_threshold(self, settings):
        client = IGClient(settings)
        code = "error.public-api.exceeded-api-key-allowance"
        # First two occurrences stay at WARNING, the third escalates to ERROR.
        assert client._error_log_level(code) == logging.WARNING
        assert client._error_log_level(code) == logging.WARNING
        assert client._error_log_level(code) == logging.ERROR
