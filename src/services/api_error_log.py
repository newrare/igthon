"""In-memory log of the last N IG API errors, exposed to the web dashboard."""

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class APIErrorEntry:
    """A single recorded API error."""

    timestamp: datetime
    method: str
    endpoint: str
    http_status: int
    ig_error_code: str
    hint: str


# Human-readable translations for every known IG error code.
# Kept here (not in client.py) so both layers can reference them.
IG_ERROR_TRANSLATIONS: dict[str, str] = {
    "error.public-api.failure.encryption.not-enabled": (
        "Encryption not enabled — enable password encryption in your IG "
        "account settings."
    ),
    "error.public-api.failure.kyc.required": (
        "KYC verification required — complete identity verification on IG."
    ),
    "error.public-api.failure.stockbroking-not-supported": (
        "Stockbroking not supported on this account type."
    ),
    "error.service.financial.stockbroking-account-type.not.supported": (
        "Account type does not support stockbroking."
    ),
    "error.public-api.failure.preferred-account-not-set": (
        "No preferred account set — set a default account in IG settings."
    ),
    "error.public.api.failure.trading.position.not.enabled.for.this.epic": (
        "Trading not enabled for this instrument (epic). Check dealing permissions."
    ),
    "error.request.too.frequent": (
        "API rate limit exceeded — too many requests in a short time window."
    ),
    "access.denied.reason.ip.blocked": (
        "IP address blocked by IG — contact IG support or wait for the block to lift."
    ),
    "error.public-api.failure.invalid-deal-size": (
        "Invalid deal size — check minimum/maximum size for this instrument."
    ),
    "error.public-api.failure.epic-does-not-exist": (
        "Epic does not exist or has been delisted."
    ),
    "error.public-api.failure.market-closed": (
        "Market is closed — trading is outside market hours."
    ),
    "error.security.account-token-invalid": (
        "Access token invalid or expired — re-authentication required."
    ),
    "error.security.client-token-missing": ("API key missing from request headers."),
    "error.security.account-token-missing": (
        "Account token (OAuth Bearer) missing from request headers."
    ),
    "error.security.oauth-token-invalid": (
        "OAuth token is invalid or has been revoked."
    ),
    "error.public-api.failure.no-deal-reference-number": (
        "No deal reference returned — the order may not have been placed."
    ),
}


class APIErrorLog:
    """Thread-safe in-memory ring-buffer of the last *max_entries* API errors.

    Thread safety is guaranteed by the GIL for list/deque operations, which is
    sufficient for the single-event-loop async context used here.
    """

    def __init__(self, max_entries: int = 20) -> None:
        self._entries: deque[APIErrorEntry] = deque(maxlen=max_entries)

    def record(
        self,
        method: str,
        endpoint: str,
        http_status: int,
        ig_error_code: str,
    ) -> None:
        """Append a new error entry (most-recent-last internally)."""
        hint = IG_ERROR_TRANSLATIONS.get(ig_error_code, "")
        entry = APIErrorEntry(
            timestamp=datetime.now(UTC),
            method=method,
            endpoint=endpoint,
            http_status=http_status,
            ig_error_code=ig_error_code,
            hint=hint,
        )
        self._entries.append(entry)

    def get_all(self) -> list[APIErrorEntry]:
        """Return errors newest-first (reversed for dashboard display)."""
        return list(reversed(self._entries))

    def clear(self) -> None:
        self._entries.clear()

    @property
    def count(self) -> int:
        return len(self._entries)
