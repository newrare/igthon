"""In-memory API call queue for the IG REST API.

All IG calls are funnelled through a single :class:`APIQueue` instead of hitting
``IGClient`` directly. Producers (scheduler jobs, manual actions, scripts) enqueue
calls quickly — optionally with a priority — and ``await`` the result. A single
background worker drains the queue while respecting the IG rate limits enforced by
:class:`~src.services.api_guard.APIGuard`:

- **Rate-limit / quota errors** (``error.public-api.exceeded-api-key-allowance``,
  ``error.request.too.frequent``, IP block) are NOT counted as failures: the worker
  waits for the guard cooldown (+ a small margin) and re-queues the call so it
  resumes where it stopped.
- **Transient errors** (HTTP 5xx, network) on idempotent GET calls are retried up to
  ``max_attempts`` times. A call that fails ``max_attempts`` times is marked ``ERROR``
  (logged) and never re-presented to the API.
- **Writes** (POST/PUT/DELETE) and **client errors** (HTTP 4xx) are never retried —
  re-sending an order risks a double execution, and a 4xx will not be fixed by a retry.
- **Probe calls** (``suppress_error_logging=True``, used for batch bisection) are never
  retried, so they fail fast and the caller can isolate the offending epic itself.

A ``priority`` argument lets urgent calls (opening/closing a position) jump ahead of
queued price-collection reads while still obeying the IG limits.

The queue is in-memory only (single-process bot) — pending tasks are lost on restart.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from itertools import count
from typing import TYPE_CHECKING

from src.services.api_guard import _QUOTA_EXCEEDED_CODES

if TYPE_CHECKING:
    from src.api.client import IGClient
    from src.services.api_guard import APIGuard

logger = logging.getLogger(__name__)


class Priority(IntEnum):
    """Call priority — lower value is served first by the worker."""

    URGENT = 0  # open / close a position
    HIGH = 5
    NORMAL = 10  # price collection, market reads


class QueueStatus(StrEnum):
    """Lifecycle state of a queued call."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass
class QueuedCall:
    """A single API call waiting in (or moving through) the queue."""

    seq: int
    priority: int
    method: str
    endpoint: str
    payload: dict | None
    version: int
    suppress_error_logging: bool
    label: str
    future: asyncio.Future
    max_attempts: int = 3
    attempts: int = 0
    total_attempts: int = 0  # every execution including rate-limit hits
    status: QueueStatus = QueueStatus.PENDING
    last_error: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass
class QueueTaskView:
    """Read-only snapshot of a processed call, for the dashboard."""

    label: str
    method: str
    endpoint: str
    status: str
    attempts: int
    total_attempts: int
    priority: int
    last_error: str
    created_at: datetime
    finished_at: datetime | None


@dataclass
class QueueErrorView:
    """Persistent record of an abandoned (failed) call, kept for debugging.

    Unlike :class:`QueueTaskView` — a bounded *recent* buffer that any task,
    success or failure, scrolls out of — errors live in their own larger ring
    buffer so a failure stays inspectable even after a burst of successful calls
    would have pushed it out of the recent list. It also carries the extra
    context needed to diagnose a case later: the exact API route + version, the
    HTTP status, the IG error code, and the full (untruncated) error message.
    """

    label: str
    method: str
    endpoint: str
    version: int
    http_status: int | None
    ig_error_code: str
    error: str
    attempts: int
    total_attempts: int
    priority: int
    failed_at: datetime


@dataclass
class APIQueueStats:
    """JSON-serialisable snapshot of the queue counters."""

    pending: int
    running: int
    enqueued: int
    succeeded: int
    failed: int
    retried: int
    rate_limited: int
    max_attempts: int


class APIQueue:
    """Serialises and throttles all IG API calls through a single worker.

    Exposes the same ``get``/``post``/``put``/``delete`` surface as
    :class:`~src.api.client.IGClient` (plus ``priority`` and ``label``) so it is a
    drop-in replacement wherever a client is injected.
    """

    def __init__(
        self,
        client: IGClient,
        guard: APIGuard | None = None,
        *,
        max_attempts: int = 3,
        retry_margin_seconds: int = 5,
        recent_size: int = 50,
        errors_size: int = 100,
    ) -> None:
        self._client = client
        self._guard = guard
        self._max_attempts = max_attempts
        self._retry_margin = retry_margin_seconds

        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = count(1)
        self._task: asyncio.Task | None = None
        self._running = False
        self._running_call: QueuedCall | None = None

        # Counters
        self._enqueued = 0
        self._succeeded = 0
        self._failed = 0
        self._retried = 0
        self._rate_limited = 0

        # Recent processed tasks (newest last); bounded ring buffer for the UI.
        self._recent_size = recent_size
        self._recent: list[QueueTaskView] = []

        # Pending calls waiting in the queue, keyed by seq for O(1) removal.
        self._pending_calls: dict[int, QueuedCall] = {}

        # Abandoned-call errors (newest last); a separate, larger ring buffer so
        # failures survive longer than the recent-task buffer for later debugging.
        self._errors_size = errors_size
        self._errors: list[QueueErrorView] = []

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start the background worker (idempotent)."""
        if self._task is None:
            self._running = True
            self._task = asyncio.create_task(
                self._run_worker(), name="api-queue-worker"
            )
            logger.info("APIQueue worker started")

    async def stop(self) -> None:
        """Stop the background worker and cancel the in-flight wait."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("APIQueue worker stopped")

    # ------------------------------------------------------------------ public API

    async def get(
        self,
        endpoint: str,
        *,
        version: int = 1,
        suppress_error_logging: bool = False,
        priority: int = Priority.NORMAL,
        label: str | None = None,
    ) -> dict:
        """Queue a GET request and await its parsed JSON result."""
        return await self._submit(
            "GET", endpoint, None, version, suppress_error_logging, priority, label
        )

    async def post(
        self,
        endpoint: str,
        payload: dict,
        *,
        version: int = 1,
        priority: int = Priority.NORMAL,
        label: str | None = None,
    ) -> dict:
        """Queue a POST request and await its parsed JSON result."""
        return await self._submit(
            "POST", endpoint, payload, version, False, priority, label
        )

    async def put(
        self,
        endpoint: str,
        payload: dict,
        *,
        version: int = 1,
        priority: int = Priority.NORMAL,
        label: str | None = None,
    ) -> dict:
        """Queue a PUT request and await its parsed JSON result."""
        return await self._submit(
            "PUT", endpoint, payload, version, False, priority, label
        )

    async def delete(
        self,
        endpoint: str,
        payload: dict | None = None,
        *,
        version: int = 1,
        priority: int = Priority.NORMAL,
        label: str | None = None,
    ) -> dict:
        """Queue a DELETE request and await its parsed JSON result."""
        return await self._submit(
            "DELETE", endpoint, payload, version, False, priority, label
        )

    def stats(self) -> APIQueueStats:
        """Return a snapshot of the queue counters."""
        return APIQueueStats(
            pending=self._queue.qsize(),
            running=1 if self._running_call is not None else 0,
            enqueued=self._enqueued,
            succeeded=self._succeeded,
            failed=self._failed,
            retried=self._retried,
            rate_limited=self._rate_limited,
            max_attempts=self._max_attempts,
        )

    def recent(self) -> list[QueueTaskView]:
        """Return recently processed tasks, newest first."""
        return list(reversed(self._recent))

    def pending_tasks(self) -> list[QueueTaskView]:
        """Return tasks currently waiting in the queue, ordered by priority then seq."""
        calls = sorted(self._pending_calls.values(), key=lambda c: (c.priority, c.seq))
        return [
            QueueTaskView(
                label=c.label,
                method=c.method,
                endpoint=c.endpoint,
                status="pending",
                attempts=c.attempts,
                total_attempts=c.total_attempts,
                priority=c.priority,
                last_error=c.last_error,
                created_at=c.created_at,
                finished_at=None,
            )
            for c in calls
        ]

    def errors(self) -> list[QueueErrorView]:
        """Return abandoned-call errors, newest first (persistent debug log)."""
        return list(reversed(self._errors))

    def clear_errors(self) -> None:
        """Drop all recorded queue errors (UI 'Clear' button)."""
        self._errors.clear()

    # ------------------------------------------------------------------ internals

    def _submit(
        self,
        method: str,
        endpoint: str,
        payload: dict | None,
        version: int,
        suppress: bool,
        priority: int,
        label: str | None,
    ) -> asyncio.Future:
        """Build a QueuedCall, enqueue it, and return its result future."""
        seq = next(self._seq)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        call = QueuedCall(
            seq=seq,
            priority=int(priority),
            method=method,
            endpoint=endpoint,
            payload=payload,
            version=version,
            suppress_error_logging=suppress,
            label=label or f"{method} {endpoint}",
            future=future,
            max_attempts=self._max_attempts,
        )
        self._enqueued += 1
        self._enqueue(call)
        return future

    def _enqueue(self, call: QueuedCall) -> None:
        """(Re)insert a call. Items keep their original ``seq`` so a re-queued
        call resumes ahead of newer same-priority calls (FIFO within priority)."""
        call.status = QueueStatus.PENDING
        self._pending_calls[call.seq] = call
        self._queue.put_nowait((call.priority, call.seq, call))

    async def _run_worker(self) -> None:
        """Drain the queue forever, one call at a time, respecting the guard."""
        while self._running:
            _, _, call = await self._queue.get()
            try:
                await self._process(call)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive, worker must survive
                logger.exception(
                    "APIQueue worker: unexpected error processing %s", call.label
                )
            finally:
                self._queue.task_done()

    async def _process(self, call: QueuedCall) -> None:
        """Execute one attempt of ``call`` and handle the outcome."""
        # Back-pressure: wait until the guard genuinely has headroom.
        if self._guard is not None:
            await self._guard.wait_until_ready()

        self._pending_calls.pop(call.seq, None)
        call.status = QueueStatus.RUNNING
        call.started_at = datetime.now(UTC)
        call.attempts += 1
        call.total_attempts += 1
        self._running_call = call
        try:
            result = await self._invoke(call)
        except Exception as exc:
            self._running_call = None
            await self._handle_failure(call, exc)
            return

        # Success
        self._running_call = None
        call.status = QueueStatus.DONE
        call.finished_at = datetime.now(UTC)
        self._succeeded += 1
        if not call.future.done():
            call.future.set_result(result)
        self._record_recent(call)

    async def _invoke(self, call: QueuedCall) -> dict:
        """Dispatch the call to the underlying IG client."""
        if call.method == "GET":
            return await self._client.get(
                call.endpoint,
                version=call.version,
                suppress_error_logging=call.suppress_error_logging,
            )
        if call.method == "POST":
            return await self._client.post(
                call.endpoint, call.payload or {}, version=call.version
            )
        if call.method == "PUT":
            return await self._client.put(
                call.endpoint, call.payload or {}, version=call.version
            )
        if call.method == "DELETE":
            return await self._client.delete(
                call.endpoint, call.payload, version=call.version
            )
        raise ValueError(f"Unsupported method: {call.method}")

    async def _handle_failure(self, call: QueuedCall, exc: Exception) -> None:
        """Classify a failed call: rate-limit wait, transient retry, or give up."""
        ig_code = getattr(exc, "ig_error_code", "")
        response = getattr(exc, "response", None)
        status = response.status_code if response is not None else None

        # Guard refusal (RuntimeError) or quota/rate-limit error → wait for the
        # cooldown and resume where we stopped. Not counted as a strike.
        if isinstance(exc, RuntimeError) or ig_code in _QUOTA_EXCEEDED_CODES:
            call.attempts -= 1
            self._rate_limited += 1
            logger.info(
                "APIQueue: rate-limited on %s (%s) — waiting then resuming",
                call.label,
                ig_code or "guard refused",
            )
            await self._wait_for_unblock()
            self._enqueue(call)
            return

        # Transient (5xx / network) on an idempotent, non-probe GET → retry.
        is_transient = status is None or status >= 500
        retriable = (
            call.method == "GET"
            and not call.suppress_error_logging
            and is_transient
            and call.attempts < call.max_attempts
        )
        if retriable:
            self._retried += 1
            logger.warning(
                "APIQueue: transient error on %s (attempt %d/%d): %s",
                call.label,
                call.attempts,
                call.max_attempts,
                exc,
            )
            self._enqueue(call)
            return

        # Give up: write, client error, probe, or 3-strike GET. Never re-presented.
        call.status = QueueStatus.ERROR
        call.last_error = str(exc)
        call.finished_at = datetime.now(UTC)
        self._failed += 1
        # Probe calls (suppress_error_logging=True) are bisection probes — their
        # failure is expected and handled by the caller, so debug-level is correct.
        log_fn = logger.debug if call.suppress_error_logging else logger.error
        log_fn(
            "APIQueue: task ABANDONED — %s [%s %s] after %d attempt(s): %s",
            call.label,
            call.method,
            call.endpoint,
            call.attempts,
            exc,
        )
        # Probes are expected bisection failures — keep them out of the debug
        # error log so it only surfaces actionable, real failures.
        if not call.suppress_error_logging:
            self._record_error(call, exc, status, ig_code)
        if not call.future.done():
            call.future.set_exception(exc)
        self._record_recent(call)

    async def _wait_for_unblock(self) -> None:
        """Sleep until the guard's block cooldown elapses (+ configured margin)."""
        until = self._guard.blocked_until if self._guard is not None else None
        if until is None:
            await asyncio.sleep(self._retry_margin)
            return
        remaining = (until - datetime.now(UTC)).total_seconds()
        delay = remaining + self._retry_margin
        if delay > 0:
            await asyncio.sleep(delay)

    def _record_recent(self, call: QueuedCall) -> None:
        """Append a snapshot of a finished call to the bounded ring buffer."""
        self._recent.append(
            QueueTaskView(
                label=call.label,
                method=call.method,
                endpoint=call.endpoint,
                status=call.status.value,
                attempts=call.attempts,
                total_attempts=call.total_attempts,
                priority=call.priority,
                last_error=call.last_error,
                created_at=call.created_at,
                finished_at=call.finished_at,
            )
        )
        if len(self._recent) > self._recent_size:
            del self._recent[0 : len(self._recent) - self._recent_size]

    def _record_error(
        self,
        call: QueuedCall,
        exc: Exception,
        status: int | None,
        ig_code: str,
    ) -> None:
        """Append a detailed snapshot of an abandoned call to the error buffer."""
        self._errors.append(
            QueueErrorView(
                label=call.label,
                method=call.method,
                endpoint=call.endpoint,
                version=call.version,
                http_status=status,
                ig_error_code=ig_code or "",
                error=str(exc),
                attempts=call.attempts,
                total_attempts=call.total_attempts,
                priority=call.priority,
                failed_at=call.finished_at or datetime.now(UTC),
            )
        )
        if len(self._errors) > self._errors_size:
            del self._errors[0 : len(self._errors) - self._errors_size]
