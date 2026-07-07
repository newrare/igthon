"""IG API HTTP client with automatic authentication and header management."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from typing import TYPE_CHECKING

import httpx

from src.core.api.session import IGSession
from src.core.config import Settings

if TYPE_CHECKING:
    from src.core.api_error_log import APIErrorLog
    from src.core.api_guard import APIGuard

logger = logging.getLogger(__name__)


class IGAPIError(httpx.HTTPStatusError):
    """HTTPStatusError enriched with the IG-specific errorCode from the response body.

    Attributes:
        ig_error_code: The ``errorCode`` field returned by IG (empty string if absent).
    """

    def __init__(
        self,
        message: str,
        *,
        request: httpx.Request,
        response: httpx.Response,
        ig_error_code: str = "",
    ) -> None:
        super().__init__(message, request=request, response=response)
        self.ig_error_code = ig_error_code


# Known IG error codes mapped to short human-readable hints (legacy; full
# translations live in api_error_log.IG_ERROR_TRANSLATIONS).
_IG_ERROR_HINTS: dict[str, str] = {
    "error.public-api.failure.encryption.not-enabled": (
        "Encryption not enabled on this account"
    ),
    "error.public-api.failure.kyc.required": "KYC verification required",
    "error.public-api.failure.stockbroking-not-supported": "Stockbroking not supported",
    "error.service.financial.stockbroking-account-type.not.supported": (
        "Account type not supported"
    ),
    "error.public-api.failure.preferred-account-not-set": "No preferred account set",
    "error.public.api.failure.trading.position.not.enabled.for.this.epic": (
        "Trading not enabled for this epic"
    ),
    "error.request.too.frequent": "API rate limit hit — too many requests",
    "access.denied.reason.ip.blocked": "IP address blocked by IG",
}


class IGClient:
    """Async HTTP client for the IG REST API.

    Wraps httpx.AsyncClient with:
    - Automatic OAuth token management (login + refresh)
    - Standard IG headers on every request
    - Logging of requests/responses with IG error codes
    - Optional APIErrorLog for dashboard error history
    - Optional APIGuard for pre-call rate-limit checks
    """

    def __init__(
        self,
        settings: Settings,
        error_log: APIErrorLog | None = None,
        guard: APIGuard | None = None,
    ) -> None:
        self._settings = settings
        self._session = IGSession(settings)
        self._http: httpx.AsyncClient | None = None
        self._error_log = error_log
        self._guard = guard

    async def __aenter__(self) -> IGClient:
        self._http = httpx.AsyncClient(timeout=30.0)
        await self._session.login(self._http)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    @property
    def http(self) -> httpx.AsyncClient:
        """Return the underlying HTTP client."""
        if self._http is None:
            raise RuntimeError("Client not initialized. Use 'async with IGClient()'.")
        return self._http

    @property
    def session(self) -> IGSession:
        """Return the underlying session (login state + streaming tokens)."""
        return self._session

    def _build_headers(self, version: int = 1) -> dict[str, str]:
        """Build standard IG request headers."""
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json; charset=UTF-8",
            "X-IG-API-KEY": self._settings.ig_api_key,
            "Version": str(version),
        }
        headers.update(self._session.auth_headers)
        return headers

    async def _ensure_auth(self) -> None:
        """Ensure the token is valid before making a request."""
        await self._session.ensure_valid_token(self.http)

    def _raise_for_status(
        self,
        response: httpx.Response,
        method: str,
        url: str,
        *,
        suppress_error_logging: bool = False,
    ) -> None:
        """Log IG API error details from response body before raising.

        IG returns structured errors like {"errorCode": "error.request.too.frequent"}.
        Also feeds the optional APIErrorLog and notifies the APIGuard so the
        dashboard can reflect errors and quota-blocked state in real time.

        When ``suppress_error_logging`` is set the error is still raised but is
        not logged, recorded in the sidecar, or reported to the guard. Callers
        use this for expected, probed failures (e.g. batch bisection) that would
        otherwise pollute the logs and falsely trip the quota guard.
        """
        if not response.is_error:
            return

        ig_error_code = ""
        hint = ""
        try:
            body = response.json()
            ig_error_code = body.get("errorCode") or body.get("message") or ""
            hint = _IG_ERROR_HINTS.get(ig_error_code, "")
        except Exception:
            ig_error_code = response.text[:300]

        detail = ig_error_code
        if hint:
            detail = f"{ig_error_code} ({hint})"

        if not suppress_error_logging:
            logger.error(
                "%s %s → HTTP %d%s",
                method,
                url,
                response.status_code,
                f" — {detail}" if detail else "",
            )

            # Feed observability sidecar (endpoint path extracted from full URL)
            endpoint = url.replace(self._settings.ig_base_url, "") or url
            if self._error_log is not None:
                self._error_log.record(
                    method=method,
                    endpoint=endpoint,
                    http_status=response.status_code,
                    ig_error_code=ig_error_code,
                )
            if self._guard is not None:
                self._guard.on_ig_error(ig_error_code)

        error_type = "Client error" if response.is_client_error else "Server error"
        msg = (
            f"{error_type} '{response.status_code} {response.reason_phrase}' "
            f"for url '{url}'"
        )
        if detail:
            msg = f"{msg} — IG: {detail}"
        raise IGAPIError(
            msg,
            request=response.request,
            response=response,
            ig_error_code=ig_error_code,
        )

    @asynccontextmanager
    async def _request_context(self) -> AsyncIterator[None]:
        """Auth refresh + guard lifecycle (rate-limit check + inflight slot).

        Using a context manager rather than a plain coroutine ensures the
        guard's inflight semaphore is held for the full duration of the HTTP
        call, so a quota error received by one request blocks the next before
        it is dispatched rather than after.
        """
        await self._ensure_auth()
        if self._guard is not None:
            async with self._guard.guarded_request():
                yield
        else:
            async with nullcontext():
                yield

    async def get(
        self,
        endpoint: str,
        *,
        version: int = 1,
        suppress_error_logging: bool = False,
    ) -> dict:
        """Perform a GET request to the IG API.

        Args:
            endpoint: API path (e.g. "/accounts").
            version: API version number for the endpoint.
            suppress_error_logging: When True, an error response is still raised
                but not logged/recorded/reported to the guard. Use for expected,
                probed failures (e.g. batch bisection).

        Returns:
            Parsed JSON response as a dictionary.
        """
        return await self._send(
            "get",
            "GET",
            endpoint,
            version=version,
            suppress_error_logging=suppress_error_logging,
        )

    async def post(self, endpoint: str, payload: dict, *, version: int = 1) -> dict:
        """POST to the IG API (e.g. open a position). Returns the parsed JSON."""
        return await self._send(
            "post", "POST", endpoint, version=version, payload=payload
        )

    async def put(self, endpoint: str, payload: dict, *, version: int = 1) -> dict:
        """PUT to the IG API (e.g. update a stop). Returns the parsed JSON."""
        return await self._send(
            "put", "PUT", endpoint, version=version, payload=payload
        )

    async def delete(
        self, endpoint: str, payload: dict | None = None, *, version: int = 1
    ) -> dict:
        """DELETE on the IG API (e.g. close a position). Returns the parsed JSON.

        IG does not reliably process DELETE bodies, so the officially supported
        workaround is a POST carrying the ``_method: DELETE`` header.
        """
        return await self._send(
            "post",
            "DELETE",
            endpoint,
            version=version,
            payload=payload,
            extra_headers={"_method": "DELETE"},
        )

    async def _send(
        self,
        http_verb: str,
        label: str,
        endpoint: str,
        *,
        version: int,
        payload: dict | None = None,
        suppress_error_logging: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        """Shared request path for every verb: auth refresh → headers → call → raise.

        ``http_verb`` is the httpx method actually invoked ("get"/"post"/"put");
        ``label`` is the logical method used for logging and error attribution
        ("GET"/"POST"/"PUT"/"DELETE" — a DELETE is sent as a POST + ``_method``).

        Headers are built INSIDE ``_request_context`` — i.e. AFTER ``_ensure_auth``
        refreshes the token — so a bearer renewed on the ~60s expiry edge is used,
        never a stale one. A stale bearer yields a 401 that the APIQueue drops as a
        non-retryable client error, silently losing a mutating order. Centralising
        this means the guarantee is enforced once rather than in four places.
        """
        url = f"{self._settings.ig_base_url}{endpoint}"
        async with self._request_context():
            headers = self._build_headers(version)
            if extra_headers:
                headers.update(extra_headers)
            if payload is None:
                logger.debug("%s %s", label, url)
            else:
                # The body is the audit record of *what* a mutating call asked for.
                # No secrets pass here — login/refresh use the raw http client.
                logger.debug("%s %s payload=%s", label, url, payload)
            caller = getattr(self.http, http_verb)
            if payload is None:
                response = await caller(url, headers=headers)
            else:
                response = await caller(url, json=payload, headers=headers)
        self._raise_for_status(
            response, label, url, suppress_error_logging=suppress_error_logging
        )
        return response.json()
