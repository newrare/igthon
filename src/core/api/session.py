"""OAuth v3 session management for the IG API.

Handles login, token storage, and automatic refresh.
Access tokens expire after ~60 seconds; refresh tokens ~10 minutes after.
"""

import asyncio
import logging
import time

import httpx

from src.core.config import Settings

logger = logging.getLogger(__name__)


class OAuthToken:
    """In-memory OAuth token container."""

    def __init__(
        self,
        access_token: str,
        refresh_token: str,
        expires_in: int,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = time.time() + expires_in

    @property
    def is_expired(self) -> bool:
        """Check if the access token is expired (with 5s safety margin)."""
        return time.time() >= (self.expires_at - 5)


class IGSession:
    """IG API session with OAuth v3 authentication.

    Manages login, token refresh, and provides valid auth headers.
    Thread-safe via asyncio.Lock for concurrent access.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token: OAuthToken | None = None
        self._lock = asyncio.Lock()
        # Captured from the v3 login response — needed by the Lightstreamer client.
        self._lightstreamer_endpoint: str | None = None
        self._account_id: str | None = None

    @property
    def is_authenticated(self) -> bool:
        """Check if we have a valid (non-expired) token."""
        return self._token is not None and not self._token.is_expired

    @property
    def lightstreamer_endpoint(self) -> str | None:
        """Lightstreamer server endpoint returned by the v3 login (or None)."""
        return self._lightstreamer_endpoint

    @property
    def account_id(self) -> str:
        """Active account id from the login response (falls back to settings)."""
        return self._account_id or self._settings.ig_account_id

    @property
    def auth_headers(self) -> dict[str, str]:
        """Return authorization headers for API requests."""
        if self._token is None:
            raise RuntimeError("Not authenticated. Call login() first.")
        return {
            "Authorization": f"Bearer {self._token.access_token}",
            "IG-ACCOUNT-ID": self._settings.ig_account_id,
        }

    async def login(self, http_client: httpx.AsyncClient) -> None:
        """Authenticate with the IG API using OAuth v3.

        POST /session with Version: 3 to obtain access + refresh tokens.
        """
        async with self._lock:
            url = f"{self._settings.ig_base_url}/session"
            headers = {
                "Content-Type": "application/json; charset=UTF-8",
                "Accept": "application/json; charset=UTF-8",
                "X-IG-API-KEY": self._settings.ig_api_key,
                "Version": "3",
            }
            payload = {
                "identifier": self._settings.ig_username,
                "password": self._settings.ig_password,
            }

            logger.info("Logging in to IG API (%s)...", self._settings.ig_env.value)
            response = await http_client.post(url, json=payload, headers=headers)
            if response.is_error:
                # Surface IG's errorCode (e.g. "error.security.api-key-invalid",
                # "error.public-api.failure.account.not.subscribed") — it pinpoints
                # the cause far better than a bare 403. The body carries no secret.
                logger.error(
                    "IG login failed (%s) for %s: %s",
                    response.status_code,
                    self._settings.ig_env.value,
                    response.text,
                )
            response.raise_for_status()

            data = response.json()
            oauth = data["oauthToken"]

            self._token = OAuthToken(
                access_token=oauth["access_token"],
                refresh_token=oauth["refresh_token"],
                expires_in=int(oauth["expires_in"]),
            )
            # Streaming connection details are only present in the v3 response.
            self._lightstreamer_endpoint = data.get("lightstreamerEndpoint")
            self._account_id = data.get("accountId")
            logger.info("Login successful. Token expires in %ss.", oauth["expires_in"])

    async def refresh(self, http_client: httpx.AsyncClient) -> None:
        """Refresh the access token using the refresh token.

        POST /session/refresh-token with the current refresh_token.
        """
        async with self._lock:
            if self._token is None:
                raise RuntimeError("No token to refresh. Call login() first.")

            url = f"{self._settings.ig_base_url}/session/refresh-token"
            headers = {
                "Content-Type": "application/json; charset=UTF-8",
                "Accept": "application/json; charset=UTF-8",
                "X-IG-API-KEY": self._settings.ig_api_key,
                "Version": "1",
            }
            payload = {"refresh_token": self._token.refresh_token}

            logger.debug("Refreshing access token...")
            response = await http_client.post(url, json=payload, headers=headers)
            response.raise_for_status()

            data = response.json()

            self._token = OAuthToken(
                access_token=data["access_token"],
                refresh_token=data["refresh_token"],
                expires_in=int(data["expires_in"]),
            )
            logger.debug("Token refreshed successfully.")

    async def ensure_valid_token(self, http_client: httpx.AsyncClient) -> None:
        """Ensure we have a valid token, refreshing if needed."""
        if self._token is None:
            await self.login(http_client)
        elif self._token.is_expired:
            try:
                await self.refresh(http_client)
            except httpx.HTTPStatusError:
                logger.warning("Refresh failed, performing full login.")
                await self.login(http_client)

    async def fetch_session_tokens(
        self, http_client: httpx.AsyncClient
    ) -> tuple[str, str]:
        """Obtain the CST and X-SECURITY-TOKEN required by Lightstreamer.

        The streaming endpoint does not accept OAuth bearer tokens, so an
        OAuth-authenticated session must exchange its bearer for the legacy
        session tokens via ``GET /session?fetchSessionTokens=true``. The tokens
        are returned as response headers, not in the body.

        Returns:
            A ``(cst, x_security_token)`` tuple.
        """
        await self.ensure_valid_token(http_client)
        url = f"{self._settings.ig_base_url}/session?fetchSessionTokens=true"
        headers = {
            "Accept": "application/json; charset=UTF-8",
            "X-IG-API-KEY": self._settings.ig_api_key,
            "Version": "1",
            **self.auth_headers,
        }
        response = await http_client.get(url, headers=headers)
        response.raise_for_status()
        cst = response.headers.get("CST", "")
        xst = response.headers.get("X-SECURITY-TOKEN", "")
        if not cst or not xst:
            raise RuntimeError(
                "fetchSessionTokens did not return CST / X-SECURITY-TOKEN headers"
            )
        logger.debug("Fetched Lightstreamer session tokens (CST/XST).")
        return cst, xst
