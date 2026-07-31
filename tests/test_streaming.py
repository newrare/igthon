"""Tests for the Lightstreamer streaming client.

The ``lightstreamer-client-lib`` dependency is mocked entirely — no network and
no background threads. The thread→loop bridge is exercised with a synchronous
fake loop whose ``call_soon_threadsafe`` runs the callback inline.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.config import Settings
from src.feed import streaming as streaming_module
from src.feed.price_buffer import PriceBuffer
from src.feed.streaming import (
    IGStreamingClient,
    _CandleListener,
    _parse_stream_candle,
    _utm_to_datetime,
)

# --------------------------------------------------------------------------- fakes


class FakeSubscription:
    """Records the constructor args and listener of a Lightstreamer Subscription."""

    def __init__(self, mode, items, fields):
        self.mode = mode
        self.items = items
        self.fields = fields
        self.listeners = []

    def addListener(self, listener):  # noqa: N802 - mirrors lib API
        self.listeners.append(listener)

    @property
    def epic(self) -> str:
        # Item format: CHART:{epic}:{scale}
        return self.items[0].split(":", 2)[1]


class FakeLSClient:
    """Minimal stand-in for ``LightstreamerClient`` recording all interactions."""

    def __init__(self, endpoint, adapter_set):
        self.endpoint = endpoint
        self.adapter_set = adapter_set
        self.connectionDetails = MagicMock()
        self.listeners = []
        self.subscribed: list[FakeSubscription] = []
        self.unsubscribed: list[FakeSubscription] = []
        self.connect_calls = 0
        self.disconnected = False

    def addListener(self, listener):  # noqa: N802 - mirrors lib API
        self.listeners.append(listener)

    def connect(self):
        self.connect_calls += 1

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, sub):
        self.subscribed.append(sub)

    def unsubscribe(self, sub):
        self.unsubscribed.append(sub)


class SyncLoop:
    """Fake event loop that runs scheduled callbacks synchronously."""

    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


class FakeUpdate:
    """Stand-in for a Lightstreamer ItemUpdate."""

    def __init__(self, values: dict[str, str | None]):
        self._values = values

    def getValue(self, name):  # noqa: N802 - mirrors lib API
        return self._values.get(name)


def _full_candle_values(cons_end: str = "1") -> dict[str, str]:
    return {
        "UTM": "1717582800000",  # 2024-06-05 09:00:00 UTC
        "BID_OPEN": "100.0",
        "BID_HIGH": "101.0",
        "BID_LOW": "99.0",
        "BID_CLOSE": "100.5",
        "OFR_OPEN": "100.2",
        "OFR_HIGH": "101.2",
        "OFR_LOW": "99.2",
        "OFR_CLOSE": "100.7",
        "LTV": "1234",
        "CONS_END": cons_end,
        "CONS_TICK_COUNT": "60",
    }


def _make_candle():
    """A parsed ``Candle`` matching ``_full_candle_values`` (bid close 100.5)."""
    return _parse_stream_candle(FakeUpdate(_full_candle_values()).getValue)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        ig_env="demo",
        ig_api_key="k",
        ig_username="u",
        ig_password="p",
        ig_account_id="ACC123",
        streaming_max_epics=3,
    )


@pytest.fixture
def fake_client() -> MagicMock:
    """A fake IGClient exposing the session surface the streamer needs."""
    client = MagicMock()
    client.session.lightstreamer_endpoint = "https://demo-stream.ig.com"
    client.session.account_id = "ACC123"
    client.session.fetch_session_tokens = AsyncMock(return_value=("CST_TOK", "XST_TOK"))
    client.http = MagicMock()
    return client


@pytest.fixture
def patch_lib(monkeypatch):
    """Patch the optional Lightstreamer symbols with the in-test fakes."""
    monkeypatch.setattr(streaming_module, "_HAS_LIGHTSTREAMER", True)
    monkeypatch.setattr(streaming_module, "LightstreamerClient", FakeLSClient)
    monkeypatch.setattr(streaming_module, "Subscription", FakeSubscription)


# --------------------------------------------------------------------- pure helpers


def test_utm_to_datetime_is_utc():
    # 1717582800000 ms == 2024-06-05 10:20:00 UTC
    dt = _utm_to_datetime("1717582800000")
    assert dt.tzinfo is not None
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2024, 6, 5, 10, 20)


def test_parse_stream_candle_maps_bid_offer_and_volume():
    candle = _parse_stream_candle(FakeUpdate(_full_candle_values()).getValue)
    assert candle is not None
    assert candle.bid_open == 100.0
    assert candle.bid_close == 100.5
    assert candle.offer_open == 100.2
    assert candle.offer_close == 100.7
    assert candle.volume == 1234
    assert isinstance(candle.volume, int)


def test_parse_stream_candle_missing_field_returns_none():
    values = _full_candle_values()
    values["UTM"] = None
    assert _parse_stream_candle(FakeUpdate(values).getValue) is None

    values = _full_candle_values()
    values["BID_OPEN"] = None
    assert _parse_stream_candle(FakeUpdate(values).getValue) is None


# ----------------------------------------------------------------- candle listener


def test_listener_ignores_unfinished_candle(settings, fake_client):
    buffer = PriceBuffer()
    streamer = IGStreamingClient(fake_client, buffer, settings)
    streamer._loop = SyncLoop()
    listener = _CandleListener(streamer, "EPIC.A")

    listener.onItemUpdate(FakeUpdate(_full_candle_values(cons_end="0")))

    assert buffer.get("EPIC.A") is None  # nothing written


def test_listener_appends_finished_candle(settings, fake_client):
    buffer = PriceBuffer()
    streamer = IGStreamingClient(fake_client, buffer, settings)
    streamer._loop = SyncLoop()
    listener = _CandleListener(streamer, "EPIC.A")

    listener.onItemUpdate(FakeUpdate(_full_candle_values(cons_end="1")))

    buf = buffer.get("EPIC.A")
    assert buf is not None
    assert len(buf) == 1
    assert buf.last.bid_close == 100.5


# ------------------------------------------------------------------ candle listeners


def test_candle_listener_is_notified_after_the_buffer_is_updated(settings, fake_client):
    """Observers must see the new candle already in the buffer.

    They are the event-driven trigger for position management, which reads the
    buffer rather than the callback argument, so ordering here is the contract.
    """
    buffer = PriceBuffer()
    streamer = IGStreamingClient(fake_client, buffer, settings)
    seen: list[tuple[str, float, int]] = []

    streamer.add_candle_listener(
        lambda epic, candle: seen.append(
            (epic, candle.bid_close, len(buffer.get(epic)))
        )
    )
    streamer.on_candle("EPIC.A", _make_candle())

    assert seen == [("EPIC.A", 100.5, 1)]


def test_a_failing_candle_listener_does_not_break_the_feed(settings, fake_client):
    """A misbehaving observer must not stop the buffer, the store or its peers."""
    buffer = PriceBuffer()
    streamer = IGStreamingClient(fake_client, buffer, settings)
    reached: list[str] = []

    def boom(epic, candle):
        raise RuntimeError("observer blew up")

    streamer.add_candle_listener(boom)
    streamer.add_candle_listener(lambda epic, candle: reached.append(epic))

    streamer.on_candle("EPIC.A", _make_candle())

    assert reached == ["EPIC.A"]  # the second observer still ran
    assert len(buffer.get("EPIC.A")) == 1  # and the candle was buffered


# ------------------------------------------------------------------- subscriptions


async def test_set_epics_subscribes_and_unsubscribes(settings, fake_client, patch_lib):
    streamer = IGStreamingClient(fake_client, PriceBuffer(), settings)
    await streamer.start()

    await streamer.set_epics(["EPIC.A", "EPIC.B"])
    assert streamer.subscribed_epics == ["EPIC.A", "EPIC.B"]

    await streamer.set_epics(["EPIC.B", "EPIC.C"])
    assert streamer.subscribed_epics == ["EPIC.B", "EPIC.C"]

    ls = streamer._ls
    subscribed_epics = [s.epic for s in ls.subscribed]
    unsubscribed_epics = [s.epic for s in ls.unsubscribed]
    assert subscribed_epics == ["EPIC.A", "EPIC.B", "EPIC.C"]
    assert unsubscribed_epics == ["EPIC.A"]


async def test_set_epics_caps_at_max(settings, fake_client, patch_lib):
    # settings.streaming_max_epics == 3
    streamer = IGStreamingClient(fake_client, PriceBuffer(), settings)
    await streamer.start()

    await streamer.set_epics(["E1", "E2", "E3", "E4", "E5"])

    assert streamer.subscribed_epics == ["E1", "E2", "E3"]


async def test_start_requires_library(settings, fake_client, monkeypatch):
    monkeypatch.setattr(streaming_module, "_HAS_LIGHTSTREAMER", False)
    streamer = IGStreamingClient(fake_client, PriceBuffer(), settings)
    with pytest.raises(RuntimeError, match="not installed"):
        await streamer.start()


async def test_connect_sets_credentials(settings, fake_client, patch_lib):
    streamer = IGStreamingClient(fake_client, PriceBuffer(), settings)
    await streamer.start()

    ls = streamer._ls
    assert ls.endpoint == "https://demo-stream.ig.com"
    ls.connectionDetails.setUser.assert_called_once_with("ACC123")
    ls.connectionDetails.setPassword.assert_called_once_with("CST-CST_TOK|XST-XST_TOK")
    assert ls.connect_calls == 1


# --------------------------------------------------------------------- reconnection


async def test_reconnect_refetches_tokens_and_resubscribes(
    settings, fake_client, patch_lib
):
    streamer = IGStreamingClient(fake_client, PriceBuffer(), settings)
    streamer._reconnect_delay = 0  # avoid backoff sleep in the test
    await streamer.start()
    await streamer.set_epics(["EPIC.A", "EPIC.B"])
    old_ls = streamer._ls

    await streamer._reconnect()

    # A fresh client was built and the old one torn down.
    assert old_ls.disconnected is True
    assert streamer._ls is not old_ls
    # Tokens fetched on initial connect AND on reconnect.
    assert fake_client.session.fetch_session_tokens.await_count == 2
    # All epics re-subscribed on the new client.
    assert sorted(s.epic for s in streamer._ls.subscribed) == ["EPIC.A", "EPIC.B"]
    assert streamer.subscribed_epics == ["EPIC.A", "EPIC.B"]


async def test_resubscribe_replaces_stalled_subscription(
    settings, fake_client, patch_lib
):
    streamer = IGStreamingClient(fake_client, PriceBuffer(), settings)
    await streamer.start()
    streamer._connected = True
    await streamer.set_epics(["EPIC.A", "EPIC.B"])
    old_sub = streamer._subscriptions["EPIC.A"]

    result = await streamer.resubscribe("EPIC.A")

    assert result is True
    # Still tracked, but a *fresh* subscription replaced the stalled one.
    assert "EPIC.A" in streamer.subscribed_epics
    assert streamer._subscriptions["EPIC.A"] is not old_sub
    assert old_sub in streamer._ls.unsubscribed


async def test_resubscribe_subscribes_missing_epic(settings, fake_client, patch_lib):
    streamer = IGStreamingClient(fake_client, PriceBuffer(), settings)
    await streamer.start()
    streamer._connected = True

    result = await streamer.resubscribe("EPIC.A")

    assert result is True
    assert "EPIC.A" in streamer.subscribed_epics


async def test_resubscribe_noop_when_offline(settings, fake_client, patch_lib):
    streamer = IGStreamingClient(fake_client, PriceBuffer(), settings)
    await streamer.start()  # _connected stays False (no CONNECTED status)
    await streamer.set_epics(["EPIC.A"])

    assert await streamer.resubscribe("EPIC.A") is False


def test_handle_status_tracks_connected(settings, fake_client):
    streamer = IGStreamingClient(fake_client, PriceBuffer(), settings)

    streamer.handle_status("CONNECTED:WS-STREAMING")
    assert streamer.is_connected is True

    streamer.handle_status("STALLED")
    assert streamer.is_connected is False


async def test_ensure_connected_noop_when_already_connected(
    settings, fake_client, patch_lib
):
    streamer = IGStreamingClient(fake_client, PriceBuffer(), settings)
    await streamer.start()
    streamer._connected = True

    assert await streamer.ensure_connected() is True
    # No teardown/reconnect: only the initial connect fetched tokens.
    assert fake_client.session.fetch_session_tokens.await_count == 1


async def test_ensure_connected_reconnects_when_down(settings, fake_client, patch_lib):
    # The watchdog case: the session went dark without a DISCONNECTED callback
    # (laptop resumed from sleep). A forced reconnect rebuilds the client and
    # re-subscribes every epic.
    streamer = IGStreamingClient(fake_client, PriceBuffer(), settings)
    streamer._reconnect_delay = 0  # avoid backoff sleep in the test
    await streamer.start()
    await streamer.set_epics(["EPIC.A"])
    streamer._connected = False
    old_ls = streamer._ls

    await streamer.ensure_connected()

    assert streamer._ls is not old_ls
    assert fake_client.session.fetch_session_tokens.await_count == 2
    assert streamer.subscribed_epics == ["EPIC.A"]
    # The reconnect guard was released for the next call.
    assert streamer._reconnecting is False


async def test_ensure_connected_noop_when_not_started(settings, fake_client, patch_lib):
    streamer = IGStreamingClient(fake_client, PriceBuffer(), settings)

    assert await streamer.ensure_connected() is False
    assert fake_client.session.fetch_session_tokens.await_count == 0


async def test_ensure_connected_skips_when_reconnect_in_flight(
    settings, fake_client, patch_lib
):
    # A reconnect is already running (callback path or a prior call): the
    # watchdog must not launch a second one.
    streamer = IGStreamingClient(fake_client, PriceBuffer(), settings)
    await streamer.start()
    streamer._connected = False
    streamer._reconnecting = True

    assert await streamer.ensure_connected() is False
    assert fake_client.session.fetch_session_tokens.await_count == 1
