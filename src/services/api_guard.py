"""API rate-limit guard and availability tracker for IG REST API calls.

IG published limits (non-trading):
  - 30 requests / minute per account
  - 60 requests / minute per API key (application-level)

Guard defaults stay safely below both limits:
  - max_per_minute=25  (margin under the 30/min account limit)
  - max_per_second=3   (prevents burst; 3×60=180 theoretical, capped at 25/min)
  - max_inflight=3     (limits truly concurrent in-flight HTTP calls so a quota
                        error from the first response can block the rest before
                        they are dispatched)

When IG responds with a quota-exceeded error the guard enters BLOCKED state and
refuses all further calls until the per-error cooldown window (plus margin) has
elapsed, at which point the block is lifted automatically.
"""

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

# IG error codes that indicate a hard quota/block condition
_QUOTA_EXCEEDED_CODES: frozenset[str] = frozenset(
    {
        "error.request.too.frequent",
        "error.public-api.exceeded-api-key-allowance",
        "error.public-api.exceeded-account-allowance",
        "error.public-api.exceeded-account-trading-allowance",
        "access.denied.reason.ip.blocked",
    }
)

# Cooldown (seconds) before auto-unblocking, per error code (margin already included).
# After this delay the IG quota window should have reset.
_BLOCK_COOLDOWNS: dict[str, int] = {
    "error.request.too.frequent": 120,  # 60 s window + 60 s margin
    "error.public-api.exceeded-api-key-allowance": 120,  # 60 s window + 60 s margin
    "error.public-api.exceeded-account-allowance": 120,  # per-minute window + margin
    "error.public-api.exceeded-account-trading-allowance": 120,  # per-minute + margin
    # NOTE: ``error.public-api.exceeded-account-historical-data-allowance`` is
    # deliberately NOT a global-block code. It is a *weekly* quota scoped to the
    # price-history endpoints; a global block would freeze unrelated market and
    # trading calls for no benefit (the weekly window does not reset for days).
    # Those calls are instead left to fail fast and be abandoned by the queue.
    "access.denied.reason.ip.blocked": 3900,  # IP blocks need longer — 1 h + 5 min
}
_DEFAULT_BLOCK_COOLDOWN: int = 120


@dataclass
class APIGuardStats:
    """Snapshot of the guard's current state, safe to serialise to JSON."""

    total_calls: int
    calls_last_minute: int
    calls_last_second: int
    is_available: bool
    is_blocked: bool
    blocked_since: datetime | None
    blocked_until: datetime | None
    blocked_reason: str
    max_per_minute: int
    max_per_second: int
    max_inflight: int


class APIGuard:
    """Pre-request rate-limit checker and post-error availability tracker.

    Usage::

        guard = APIGuard()

        # Before every API call:
        await guard.pre_request()   # raises RuntimeError if blocked or over limit

        # When IG returns a quota error:
        guard.on_ig_error("error.request.too.frequent")
    """

    def __init__(
        self,
        max_per_minute: int = 25,
        max_per_second: int = 3,
        max_inflight: int = 3,
    ) -> None:
        self._max_per_minute = max_per_minute
        self._max_per_second = max_per_second

        # Monotonic timestamps of recent calls (pruned lazily)
        self._timestamps: deque[float] = deque()
        self._total_calls: int = 0

        # Quota block state (set when IG explicitly refuses us)
        self._blocked: bool = False
        self._blocked_since: datetime | None = None
        self._blocked_reason: str = ""

        self._lock = asyncio.Lock()
        # Limits truly concurrent in-flight HTTP requests so a burst of parallel
        # asyncio tasks cannot all reach IG simultaneously before the guard can
        # react to a quota error from the first one.
        self._inflight = asyncio.Semaphore(max_inflight)

    # ------------------------------------------------------------------ helpers

    def _prune(self, now: float) -> None:
        """Remove timestamps older than 60 s (call while holding the lock)."""
        cutoff = now - 60.0
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    # ------------------------------------------------------------------ public API

    @property
    def is_available(self) -> bool:
        """True when no hard quota block is active (rate-limit headroom aside)."""
        return not self._blocked

    @property
    def blocked_until(self) -> datetime | None:
        """UTC datetime when the block auto-lifts (None if not blocked)."""
        if not self._blocked or self._blocked_since is None:
            return None
        cooldown = _BLOCK_COOLDOWNS.get(self._blocked_reason, _DEFAULT_BLOCK_COOLDOWN)
        return self._blocked_since + timedelta(seconds=cooldown)

    def _auto_unblock_if_ready(self) -> None:
        """Clear block state if the cooldown window has elapsed."""
        if not self._blocked:
            return
        until = self.blocked_until
        if until is not None and datetime.now(UTC) >= until:
            logger.info(
                "APIGuard: auto-unblocking after cooldown (was: %s)",
                self._blocked_reason,
            )
            self._blocked = False
            self._blocked_since = None
            self._blocked_reason = ""

    async def pre_request(self) -> None:
        """Wait until a request slot is available, then claim it.

        - Hard BLOCKED state raises immediately (unless the cooldown has elapsed,
          in which case the block is lifted first).
        - Per-minute limit raises immediately (no point hammering for 60 s).
        - Per-second limit sleeps until the oldest in-window timestamp expires,
          then retries — callers are queued transparently without errors.

        Raises:
            RuntimeError: If the guard is in BLOCKED state or the per-minute
                          limit is reached.
        """
        while True:
            async with self._lock:
                self._auto_unblock_if_ready()
                if self._blocked:
                    since = (
                        self._blocked_since.strftime("%H:%M:%S UTC")
                        if self._blocked_since
                        else "unknown time"
                    )
                    until = self.blocked_until
                    until_str = (
                        until.strftime("%H:%M:%S UTC") if until else "unknown time"
                    )
                    raise RuntimeError(
                        f"IG API blocked since {since} — "
                        f"reason: {self._blocked_reason}. "
                        f"Auto-unblocks at {until_str}."
                    )

                now = time.monotonic()
                self._prune(now)

                calls_last_min = len(self._timestamps)
                calls_last_sec = sum(1 for ts in self._timestamps if now - ts <= 1.0)

                if calls_last_min >= self._max_per_minute:
                    raise RuntimeError(
                        f"Rate limit: {self._max_per_minute} calls/min reached "
                        f"({calls_last_min} in last 60 s). Backing off."
                    )

                if calls_last_sec < self._max_per_second:
                    self._timestamps.append(now)
                    self._total_calls += 1
                    return

                # Per-second limit reached — calculate how long until a slot frees up.
                oldest_in_sec = min(ts for ts in self._timestamps if now - ts <= 1.0)
                wait = 1.0 - (now - oldest_in_sec) + 0.01

            logger.debug(
                "APIGuard: per-second limit reached (%d/%d), sleeping %.3fs",
                calls_last_sec,
                self._max_per_second,
                wait,
            )
            await asyncio.sleep(max(0.01, wait))

    async def wait_until_ready(self) -> None:
        """Sleep until a request slot is genuinely free, then return.

        Worker-friendly counterpart to :meth:`pre_request`: where ``pre_request``
        *raises* when blocked or over the per-minute limit, this method *waits*
        instead so a queue worker can back off and resume without losing the
        call. It does NOT reserve a slot — the reservation and per-second pacing
        still happen inside ``pre_request`` when the actual HTTP call is made.
        """
        while True:
            async with self._lock:
                self._auto_unblock_if_ready()
                if self._blocked:
                    until = self.blocked_until
                else:
                    now = time.monotonic()
                    self._prune(now)
                    if len(self._timestamps) < self._max_per_minute:
                        return
                    # Per-minute window full — wait for the oldest call to age out.
                    until = None
                    wait = 60.0 - (now - self._timestamps[0]) + 0.01

            if self._blocked and until is not None:
                delay = (until - datetime.now(UTC)).total_seconds() + 0.01
                logger.debug("APIGuard: blocked, waiting %.1fs until unblock", delay)
                await asyncio.sleep(max(0.1, delay))
            else:
                await asyncio.sleep(max(0.1, wait))

    def on_ig_error(self, ig_error_code: str) -> None:
        """React to a completed IG API error; may trigger BLOCKED state."""
        if ig_error_code in _QUOTA_EXCEEDED_CODES:
            self._blocked = True
            self._blocked_since = datetime.now(UTC)
            self._blocked_reason = ig_error_code

    @asynccontextmanager
    async def guarded_request(self) -> AsyncIterator[None]:
        """Async context manager wrapping one complete request lifecycle.

        1. Calls ``pre_request()`` — blocks if rate-limited or quota-blocked.
        2. Acquires the inflight semaphore — limits truly concurrent HTTP calls.
        3. Yields — caller performs the HTTP call.
        4. Releases the semaphore on exit (normal or exception).

        This ensures that when IG returns a 403 for request N, the guard enters
        BLOCKED state before request N+1 can leave the inflight semaphore, so
        at most ``max_inflight`` requests can fail before all subsequent ones
        are stopped.
        """
        await self.pre_request()
        async with self._inflight:
            yield

    def stats(self) -> APIGuardStats:
        """Return a JSON-serialisable snapshot of the current guard state."""
        # Auto-unblock before building the snapshot so the UI reflects reality.
        self._auto_unblock_if_ready()
        now = time.monotonic()
        # Read without lock — snapshot may be slightly stale, acceptable for UI
        self._prune(now)
        calls_last_sec = sum(1 for ts in self._timestamps if now - ts <= 1.0)
        return APIGuardStats(
            total_calls=self._total_calls,
            calls_last_minute=len(self._timestamps),
            calls_last_second=calls_last_sec,
            is_available=self.is_available,
            is_blocked=self._blocked,
            blocked_since=self._blocked_since,
            blocked_until=self.blocked_until,
            blocked_reason=self._blocked_reason,
            max_per_minute=self._max_per_minute,
            max_per_second=self._max_per_second,
            max_inflight=self._inflight._value,  # current semaphore capacity
        )
