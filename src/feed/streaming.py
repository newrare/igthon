"""Lightstreamer streaming client for live IG candle data.

Replaces the historical ``/prices`` polling as the source of live price data.
The IG REST ``/prices`` endpoint consumes a weekly *historical-data-point*
allowance (separate from the request-rate limits) and is not designed for live
polling; the Lightstreamer feed does not touch that allowance.

Design notes:
- ``lightstreamer-client-lib`` delivers updates on **background threads**. Every
  mutation of the shared :class:`~src.feed.price_buffer.PriceBuffer` is
  marshalled back onto the asyncio event loop via ``loop.call_soon_threadsafe``,
  so the buffer keeps its single-threaded (event-loop-only) invariant.
- The streaming server does not accept OAuth tokens: we exchange the OAuth
  bearer for the legacy ``CST`` / ``X-SECURITY-TOKEN`` via
  :meth:`~src.core.api.session.IGSession.fetch_session_tokens` and connect with
  ``password = "CST-{cst}|XST-{xst}"``.
- IG allows at most **40 simultaneous subscriptions per connection**; opening
  several connections breaches IG's terms. ``set_epics`` truncates defensively.

The module stays importable when ``lightstreamer-client-lib`` is absent (e.g.
``streaming_enabled=False`` deployments or CI) — the import is guarded and the
base listener classes degrade to ``object``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from src.feed.price_buffer import Candle

if TYPE_CHECKING:
    from src.core.api.client import IGClient
    from src.core.config import Settings
    from src.feed.price_buffer import PriceBuffer

logger = logging.getLogger(__name__)

try:  # pragma: no cover - exercised only when the optional lib is installed
    from lightstreamer.client import (  # type: ignore[import-not-found]
        ClientListener,
        LightstreamerClient,
        Subscription,
        SubscriptionListener,
    )

    _HAS_LIGHTSTREAMER = True
except ImportError:  # pragma: no cover - import-time fallback
    LightstreamerClient = None  # type: ignore[assignment,misc]
    Subscription = None  # type: ignore[assignment,misc]
    SubscriptionListener = object  # type: ignore[assignment,misc]
    ClientListener = object  # type: ignore[assignment,misc]
    _HAS_LIGHTSTREAMER = False

# IG CHART candle fields (bid/offer OHLC + volume + update time + consolidation).
_FIELDS: list[str] = [
    "BID_OPEN",
    "BID_HIGH",
    "BID_LOW",
    "BID_CLOSE",
    "OFR_OPEN",
    "OFR_HIGH",
    "OFR_LOW",
    "OFR_CLOSE",
    "LTV",
    "UTM",
    "CONS_END",
    "CONS_TICK_COUNT",
]

# IG exposes its CHART/MARKET adapters through the default adapter set.
_ADAPTER_SET = "DEFAULT"

# Disconnect states that warrant our own reconnect (refreshing the session
# tokens), as opposed to states the library recovers from on its own.
_RECONNECT_STATUSES: frozenset[str] = frozenset(
    {"DISCONNECTED", "DISCONNECTED:WILL-RETRY"}
)


def _utm_to_datetime(utm: str | int) -> datetime:
    """Convert an IG ``UTM`` field (epoch milliseconds) to a UTC datetime."""
    return datetime.fromtimestamp(int(utm) / 1000, tz=UTC)


def _parse_stream_candle(get: Callable[[str], str | None]) -> Candle | None:
    """Build a :class:`Candle` from a field getter (``ItemUpdate.getValue``).

    Returns ``None`` if any required field is missing or unparseable — on a
    ``CONS_END=1`` frame all OHLC fields are expected to be present.
    """
    try:
        utm = get("UTM")
        if utm is None:
            return None
        return Candle(
            timestamp=_utm_to_datetime(utm),
            bid_open=float(get("BID_OPEN")),  # type: ignore[arg-type]
            bid_close=float(get("BID_CLOSE")),  # type: ignore[arg-type]
            bid_high=float(get("BID_HIGH")),  # type: ignore[arg-type]
            bid_low=float(get("BID_LOW")),  # type: ignore[arg-type]
            offer_open=float(get("OFR_OPEN")),  # type: ignore[arg-type]
            offer_close=float(get("OFR_CLOSE")),  # type: ignore[arg-type]
            offer_high=float(get("OFR_HIGH")),  # type: ignore[arg-type]
            offer_low=float(get("OFR_LOW")),  # type: ignore[arg-type]
            volume=int(float(get("LTV") or 0)),
        )
    except (TypeError, ValueError):
        return None


class _CandleListener(SubscriptionListener):  # type: ignore[misc,valid-type]
    """Per-epic subscription listener; runs on Lightstreamer worker threads."""

    def __init__(self, streaming: IGStreamingClient, epic: str) -> None:
        self._streaming = streaming
        self._epic = epic

    def onItemUpdate(self, update: Any) -> None:  # noqa: N802 - lib callback name
        # Only act on a finished (consolidated) candle; mid-candle frames update
        # the forming bar and would pollute the buffer with partial data.
        if update.getValue("CONS_END") != "1":
            return
        candle = _parse_stream_candle(update.getValue)
        if candle is None:
            return
        loop = self._streaming.loop
        if loop is not None:
            loop.call_soon_threadsafe(self._streaming.on_candle, self._epic, candle)


class _StatusListener(ClientListener):  # type: ignore[misc,valid-type]
    """Client-level listener bridging status changes onto the event loop."""

    def __init__(self, streaming: IGStreamingClient) -> None:
        self._streaming = streaming

    def onStatusChange(self, status: str) -> None:  # noqa: N802 - lib callback name
        loop = self._streaming.loop
        if loop is not None:
            loop.call_soon_threadsafe(self._streaming.handle_status, status)


class IGStreamingClient:
    """Streams live IG candles into a :class:`PriceBuffer` via Lightstreamer.

    A single connection carries one ``CHART:{epic}:{scale}`` MERGE subscription
    per tracked epic. Completed candles (``CONS_END=1``) are appended to the
    buffer and, optionally, persisted via ``on_candle_persist``.
    """

    def __init__(
        self,
        client: IGClient,
        buffer: PriceBuffer,
        settings: Settings,
        *,
        scale: str | None = None,
        adapter_set: str = _ADAPTER_SET,
        on_candle_persist: Callable[[str, list[Candle]], Awaitable[Any]] | None = None,
    ) -> None:
        self._client = client
        self._buffer = buffer
        self._settings = settings
        self._scale = scale or settings.streaming_resolution
        self._adapter_set = adapter_set
        self._on_candle_persist = on_candle_persist

        self._loop: asyncio.AbstractEventLoop | None = None
        self._ls: Any = None  # LightstreamerClient instance once connected
        self._subscriptions: dict[str, Any] = {}
        self._subscribed_epics: set[str] = set()

        self._started = False
        self._connected = False
        self._reconnecting = False
        self._reconnect_delay = 1
        self._reconnect_lock = asyncio.Lock()

    # ------------------------------------------------------------------ accessors

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """Event loop captured at :meth:`start` (used by the thread bridge)."""
        return self._loop

    @property
    def is_connected(self) -> bool:
        """True while the Lightstreamer session is in a CONNECTED state."""
        return self._connected

    @property
    def subscribed_epics(self) -> list[str]:
        """Epics currently subscribed to the stream."""
        return sorted(self._subscribed_epics)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Capture the loop, fetch session tokens, and connect (idempotent)."""
        if self._started:
            return
        if not _HAS_LIGHTSTREAMER:
            raise RuntimeError(
                "lightstreamer-client-lib is not installed — "
                "set streaming_enabled=False or install the dependency."
            )
        self._loop = asyncio.get_running_loop()
        self._started = True
        await self._connect()

    async def stop(self) -> None:
        """Unsubscribe everything and disconnect."""
        self._started = False
        for epic in list(self._subscribed_epics):
            self._unsubscribe_epic(epic)
        await self._teardown_client()
        logger.info("Streaming stopped")

    async def set_epics(self, epics: list[str]) -> None:
        """Reconcile the active subscriptions with ``epics`` (diff sub/unsub).

        Truncates to ``streaming_max_epics`` (IG's per-connection cap) and logs
        when epics are dropped by the cap.
        """
        desired = list(dict.fromkeys(epics))  # dedupe, preserve order
        cap = self._settings.streaming_max_epics
        if len(desired) > cap:
            logger.warning(
                "Streaming: %d epics requested, capping to %d (IG subscription "
                "limit) — dropping %d",
                len(desired),
                cap,
                len(desired) - cap,
            )
            desired = desired[:cap]

        desired_set = set(desired)
        for epic in self._subscribed_epics - desired_set:
            self._unsubscribe_epic(epic)
        for epic in desired:
            if epic not in self._subscribed_epics:
                self._subscribe_epic(epic)
        logger.info(
            "Streaming: now subscribed to %d epics", len(self._subscribed_epics)
        )

    # ------------------------------------------------------------------ internals

    async def _connect(self) -> None:
        """Open a fresh Lightstreamer session using current session tokens."""
        endpoint = self._client.session.lightstreamer_endpoint
        if not endpoint:
            raise RuntimeError(
                "No lightstreamerEndpoint available — was the v3 login performed?"
            )
        cst, xst = await self._client.session.fetch_session_tokens(self._client.http)

        ls = LightstreamerClient(endpoint, self._adapter_set)
        ls.connectionDetails.setUser(self._client.session.account_id)
        ls.connectionDetails.setPassword(f"CST-{cst}|XST-{xst}")
        ls.addListener(_StatusListener(self))
        self._ls = ls
        ls.connect()
        logger.info(
            "Streaming connecting to %s as account %s",
            endpoint,
            self._client.session.account_id,
        )

    async def _teardown_client(self) -> None:
        """Disconnect the current client and drop subscription objects."""
        if self._ls is not None:
            try:
                self._ls.disconnect()
            except Exception:  # pragma: no cover - defensive
                logger.debug("Streaming: error during disconnect", exc_info=True)
        self._ls = None
        self._subscriptions.clear()
        self._connected = False

    def _make_subscription(self, epic: str) -> Any:
        """Create a MERGE candle subscription for ``epic``."""
        item = f"CHART:{epic}:{self._scale}"
        sub = Subscription("MERGE", [item], _FIELDS)
        sub.addListener(_CandleListener(self, epic))
        return sub

    def _subscribe_epic(self, epic: str) -> None:
        """Subscribe a single epic (no-op if already subscribed or offline)."""
        self._subscribed_epics.add(epic)
        if self._ls is None or epic in self._subscriptions:
            return
        sub = self._make_subscription(epic)
        self._ls.subscribe(sub)
        self._subscriptions[epic] = sub

    def _unsubscribe_epic(self, epic: str) -> None:
        """Unsubscribe a single epic and forget it."""
        self._subscribed_epics.discard(epic)
        sub = self._subscriptions.pop(epic, None)
        if sub is not None and self._ls is not None:
            try:
                self._ls.unsubscribe(sub)
            except Exception:  # pragma: no cover - defensive
                logger.debug("Streaming: error unsubscribing %s", epic, exc_info=True)

    def on_candle(self, epic: str, candle: Candle) -> None:
        """Feed a completed candle into the buffer (runs on the event loop)."""
        self._buffer.append_candles(epic, [candle])
        if self._on_candle_persist is not None:
            asyncio.ensure_future(self._persist(epic, candle))

    async def _persist(self, epic: str, candle: Candle) -> None:
        """Persist a candle via the configured callback, swallowing errors."""
        try:
            await self._on_candle_persist(epic, [candle])  # type: ignore[misc]
        except Exception:  # pragma: no cover - persistence must not crash the bridge
            logger.exception("Streaming: failed to persist candle for %s", epic)

    def handle_status(self, status: str) -> None:
        """Process a connection status change (runs on the event loop)."""
        logger.debug("Streaming status: %s", status)
        if status.startswith("CONNECTED"):
            self._connected = True
            return
        self._connected = False
        if self._started and status in _RECONNECT_STATUSES and not self._reconnecting:
            self._reconnecting = True
            asyncio.ensure_future(self._reconnect())

    async def _reconnect(self) -> None:
        """Rebuild the session with fresh tokens and re-subscribe all epics."""
        async with self._reconnect_lock:
            epics = list(self._subscribed_epics)
            try:
                await asyncio.sleep(self._reconnect_delay)
                logger.warning(
                    "Streaming: reconnecting and re-subscribing %d epics", len(epics)
                )
                await self._teardown_client()
                await self._connect()
                for epic in epics:
                    self._subscribe_epic(epic)
                self._reconnect_delay = 1
                logger.info("Streaming: reconnected")
            except Exception:
                self._reconnect_delay = min(
                    self._reconnect_delay * 2,
                    self._settings.streaming_reconnect_max_backoff_seconds,
                )
                logger.exception(
                    "Streaming: reconnect failed; next attempt backs off to %ss",
                    self._reconnect_delay,
                )
            finally:
                self._reconnecting = False
