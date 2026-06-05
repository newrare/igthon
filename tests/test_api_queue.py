"""Tests for the in-memory API call queue (APIQueue).

The underlying IG client is always mocked — these tests never hit the network.
They cover the worker's ordering, retry, rate-limit-resume and 3-strike logic.
"""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from src.api.client import IGAPIError
from src.services.api_guard import APIGuard
from src.services.api_queue import APIQueue, Priority, QueueStatus


def _ig_error(status_code: int, ig_error_code: str = "") -> IGAPIError:
    """Build an IGAPIError mimicking a failed IG call with the given status."""
    request = httpx.Request("GET", "https://demo-api.ig.com/gateway/deal/x")
    response = httpx.Response(status_code, request=request)
    return IGAPIError(
        "boom", request=request, response=response, ig_error_code=ig_error_code
    )


def _make_queue(client, **kwargs) -> APIQueue:
    """Build a queue with no guard and zero retry margin (fast tests)."""
    kwargs.setdefault("retry_margin_seconds", 0)
    return APIQueue(client, guard=None, **kwargs)


@pytest.mark.asyncio
async def test_result_is_returned_to_producer():
    """A successful GET resolves the awaited future with the parsed JSON."""
    client = AsyncMock()
    client.get = AsyncMock(return_value={"ok": 1})
    queue = _make_queue(client)
    await queue.start()
    try:
        result = await queue.get("/accounts", version=1)
        assert result == {"ok": 1}
        stats = queue.stats()
        assert stats.succeeded == 1
        assert stats.failed == 0
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_urgent_priority_jumps_ahead_of_normal():
    """An URGENT call enqueued last is processed before queued NORMAL calls."""
    order: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_get(endpoint, *, version=1, suppress_error_logging=False):
        order.append(endpoint)
        if endpoint == "/blocker":
            started.set()
            await release.wait()
        return {"endpoint": endpoint}

    client = AsyncMock()
    client.get = AsyncMock(side_effect=fake_get)
    queue = _make_queue(client)
    await queue.start()
    try:
        # Worker picks up the blocker and stalls on it while we fill the queue.
        blocker = asyncio.ensure_future(queue.get("/blocker"))
        await started.wait()

        n1 = asyncio.ensure_future(queue.get("/n1", priority=Priority.NORMAL))
        n2 = asyncio.ensure_future(queue.get("/n2", priority=Priority.NORMAL))
        urgent = asyncio.ensure_future(queue.get("/urgent", priority=Priority.URGENT))
        await asyncio.sleep(0)  # let the enqueues register

        release.set()
        await asyncio.gather(blocker, n1, n2, urgent)

        # After the blocker, URGENT runs before the two NORMAL calls (FIFO within).
        assert order == ["/blocker", "/urgent", "/n1", "/n2"]
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_get_retried_up_to_max_then_marked_error():
    """A GET that keeps failing transiently is retried, then abandoned (3 strikes)."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=_ig_error(500, "server.error"))
    queue = _make_queue(client, max_attempts=3)
    await queue.start()
    try:
        with pytest.raises(IGAPIError):
            await queue.get("/prices/X/MINUTE/2", version=2)

        stats = queue.stats()
        assert stats.failed == 1
        assert stats.retried == 2  # 2 retries before the 3rd-and-final attempt
        assert client.get.await_count == 3

        recent = queue.recent()
        assert recent[0].status == QueueStatus.ERROR.value
        assert recent[0].attempts == 3
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_write_is_not_retried():
    """A POST (order) failing on a business error is abandoned immediately."""
    client = AsyncMock()
    client.post = AsyncMock(side_effect=_ig_error(500, "server.error"))
    queue = _make_queue(client, max_attempts=3)
    await queue.start()
    try:
        with pytest.raises(IGAPIError):
            await queue.post("/positions/otc", {"epic": "X"}, version=2)

        stats = queue.stats()
        assert stats.failed == 1
        assert stats.retried == 0
        assert client.post.await_count == 1  # no retry on writes
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_client_error_4xx_is_not_retried():
    """A 4xx (e.g. data-allowance 403) won't be fixed by retrying — fail fast."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=_ig_error(403, "some.account.limit"))
    queue = _make_queue(client, max_attempts=3)
    await queue.start()
    try:
        with pytest.raises(IGAPIError):
            await queue.get("/prices/X/MINUTE/2", version=2)
        assert client.get.await_count == 1
        assert queue.stats().retried == 0
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_suppressed_probe_is_not_retried():
    """Probe calls (batch bisection) fail fast so the caller can isolate them."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=_ig_error(500, "Transformation failure"))
    queue = _make_queue(client, max_attempts=3)
    await queue.start()
    try:
        with pytest.raises(IGAPIError):
            await queue.get(
                "/markets?epics=BAD", version=1, suppress_error_logging=True
            )
        assert client.get.await_count == 1  # no retry for probes
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_rate_limit_waits_then_resumes_without_counting_a_strike():
    """A quota error pauses-and-resumes the call without consuming a retry strike."""
    quota = _ig_error(403, "error.public-api.exceeded-api-key-allowance")
    client = AsyncMock()
    client.get = AsyncMock(side_effect=[quota, {"ok": 1}])
    queue = _make_queue(client, max_attempts=3)
    await queue.start()
    try:
        result = await queue.get("/accounts", version=1)
        assert result == {"ok": 1}

        stats = queue.stats()
        assert stats.rate_limited == 1
        assert stats.succeeded == 1
        assert stats.failed == 0
        assert client.get.await_count == 2

        recent = queue.recent()
        assert recent[0].status == QueueStatus.DONE.value
        assert recent[0].attempts == 1  # the rate-limit hit was not counted
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_guard_wait_until_ready_returns_when_idle():
    """wait_until_ready returns immediately when the guard is not blocked."""
    guard = APIGuard(max_per_minute=25, max_per_second=3)
    await asyncio.wait_for(guard.wait_until_ready(), timeout=1.0)


@pytest.mark.asyncio
async def test_guard_wait_until_ready_waits_while_blocked():
    """wait_until_ready blocks while the guard is in BLOCKED state, then returns."""
    guard = APIGuard(max_per_minute=25, max_per_second=3)
    guard.on_ig_error("error.request.too.frequent")
    assert not guard.is_available

    with pytest.raises(asyncio.TimeoutError):
        # Still blocked (120s cooldown) — must not return yet.
        await asyncio.wait_for(guard.wait_until_ready(), timeout=0.3)
