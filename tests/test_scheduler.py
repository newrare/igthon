"""Tests for the scheduler's catch-up and streaming-subscription behaviour.

Covers the missed-run catch-up (last-scheduled-fire helper, run persistence,
startup replay) and that epics holding an open position stay subscribed to the
live feed through the hourly tradable refresh, so a trade's price history keeps
recording until it closes.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.scheduler import BotScheduler, validate_strategy_selection
from src.entry.base import EntryIntent
from src.entry.open_ranking import OpenRanking
from src.entry.open_saferanking import OpenSafeRanking
from src.feed.price_buffer import Candle, PriceBuffer
from src.models.database import Base
from src.models.job_preference import JobPreference
from src.models.position import Position, PositionState


@pytest.fixture
async def session_factory():
    """In-memory SQLite session factory with the schema created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _make_scheduler(session_factory) -> BotScheduler:
    """Build a BotScheduler with mocked services and a real session factory."""
    settings = MagicMock()
    settings.strategy_hour_close = 17
    return BotScheduler(
        settings=settings,
        client=MagicMock(),
        buffer=MagicMock(),
        market_data=MagicMock(),
        recorder=MagicMock(),
        epics=[],
        session_factory=session_factory,
        candle_store=MagicMock(),
        streaming=None,
    )


class TestActiveSelection:
    """The active open/stop/close names are read straight from settings.

    The ``.env`` file is the single source of truth: there is no runtime
    switching or persistence, so these properties are plain read-only views used
    by the dashboard header.
    """

    def test_active_names_reflect_settings(self):
        sched = _make_scheduler(MagicMock())
        sched._settings.open_strategy = "open_donchian"
        sched._settings.stop_strategy = "stop_support"
        sched._settings.close_zonestart = "hold"
        sched._settings.close_zonemarge = "hold"
        sched._settings.close_zoneprofit = "trailing_ratchet"
        assert sched.active_strategy_name == "open_donchian"
        assert sched.active_stop_distance_name == "stop_support"
        assert sched.active_close_zone_names == ("hold", "hold", "trailing_ratchet")
        assert sched.active_close_profile_name == "hold/hold/trailing_ratchet"


class TestOpenEpicGuarded:
    """The per-epic open lock serialises the gate + order placement so two
    concurrent callers (analysis tick, manual dashboard open, two browser tabs)
    can never both pass the duplicate-epic gate and place two orders.
    """

    def test_lock_is_per_epic_and_stable(self):
        sched = _make_scheduler(MagicMock())
        assert sched._open_lock("A") is sched._open_lock("A")  # same epic reused
        assert sched._open_lock("A") is not sched._open_lock("B")  # distinct epics

    async def test_gate_refusal_short_circuits_open(self):
        sched = _make_scheduler(MagicMock())
        opened = False

        class _Trading:
            async def can_open_intent(self, intent):
                return False, "Epic IX.D.DAX.IFMM.IP already open"

            async def open_from_intent(self, intent, buf):
                nonlocal opened
                opened = True
                return object()

        intent = SimpleNamespace(epic="IX.D.DAX.IFMM.IP", direction="BUY")
        position, reason = await sched.open_epic_guarded(_Trading(), intent, None)
        assert position is None
        assert reason == "Epic IX.D.DAX.IFMM.IP already open"
        assert opened is False  # never reaches the order when the gate refuses

    async def test_success_returns_position_and_no_reason(self):
        sched = _make_scheduler(MagicMock())
        sentinel = object()

        class _Trading:
            async def can_open_intent(self, intent):
                return True, "ok"

            async def open_from_intent(self, intent, buf):
                return sentinel

        intent = SimpleNamespace(epic="E", direction="BUY")
        position, reason = await sched.open_epic_guarded(_Trading(), intent, None)
        assert position is sentinel
        assert reason is None

    async def test_concurrent_same_epic_calls_are_serialised(self):
        sched = _make_scheduler(MagicMock())
        order: list[tuple[str, str]] = []
        release = asyncio.Event()

        class _Trading:
            def __init__(self, tag: str) -> None:
                self.tag = tag

            async def can_open_intent(self, intent):
                order.append(("gate", self.tag))
                return True, "ok"

            async def open_from_intent(self, intent, buf):
                order.append(("open-start", self.tag))
                if self.tag == "A":
                    await release.wait()  # hold the lock until released
                order.append(("open-end", self.tag))
                return object()

        intent = SimpleNamespace(epic="E", direction="BUY")
        task_a = asyncio.create_task(
            sched.open_epic_guarded(_Trading("A"), intent, None)
        )
        await asyncio.sleep(0)  # let A acquire the lock and start its order
        task_b = asyncio.create_task(
            sched.open_epic_guarded(_Trading("B"), intent, None)
        )
        await asyncio.sleep(0)
        # B is blocked on the per-epic lock — its gate must not have run yet.
        assert ("gate", "B") not in order
        release.set()
        await asyncio.gather(task_a, task_b)
        # B's gate ran only after A's order fully completed (true serialisation).
        assert order.index(("open-end", "A")) < order.index(("gate", "B"))


class TestValidateStrategySelection:
    """The ``.env`` selection is validated up front (single source of truth).

    A missing/empty or unknown name must raise a clear error naming every
    offending variable, so the bot and dashboard can tell the user to configure
    the ``.env`` file instead of failing deep in the pipeline.
    """

    def test_valid_selection_passes(self):
        settings = SimpleNamespace(
            open_strategy="open_projection",
            stop_strategy="stop_support",
            close_zonestart="hold",
            close_zonemarge="hold",
            close_zoneprofit="trailing_ratchet",
        )
        validate_strategy_selection(settings)  # does not raise

    def test_empty_selection_is_rejected(self):
        settings = SimpleNamespace(
            open_strategy="",
            stop_strategy="",
            close_zonestart="",
            close_zonemarge="",
            close_zoneprofit="",
        )
        with pytest.raises(ValueError) as exc:
            validate_strategy_selection(settings)
        message = str(exc.value)
        assert "OPEN_STRATEGY" in message
        assert "STOP_STRATEGY" in message
        assert "CLOSE_ZONESTART" in message
        assert "CLOSE_ZONEMARGE" in message
        assert "CLOSE_ZONEPROFIT" in message

    def test_unknown_name_is_rejected(self):
        settings = SimpleNamespace(
            open_strategy="not_a_strategy",
            stop_strategy="stop_support",
            close_zonestart="hold",
            close_zonemarge="hold",
            close_zoneprofit="trailing_ratchet",
        )
        with pytest.raises(ValueError, match="OPEN_STRATEGY"):
            validate_strategy_selection(settings)


class TestLastScheduledFire:
    def test_daily_trigger_returns_todays_slot(self):
        trigger = CronTrigger(day_of_week="mon-fri", hour=7, minute=30, timezone=UTC)
        # A Friday at 09:00 — the 07:30 slot already passed today.
        now = datetime(2026, 6, 12, 9, 0, tzinfo=UTC)
        last = BotScheduler._last_scheduled_fire(trigger, now)
        assert last == datetime(2026, 6, 12, 7, 30, tzinfo=UTC)

    def test_daily_trigger_before_slot_returns_previous_day(self):
        trigger = CronTrigger(day_of_week="mon-fri", hour=7, minute=30, timezone=UTC)
        # A Friday at 06:00 — today's slot has not happened yet.
        now = datetime(2026, 6, 12, 6, 0, tzinfo=UTC)
        last = BotScheduler._last_scheduled_fire(trigger, now)
        # Previous weekday is Thursday the 11th.
        assert last == datetime(2026, 6, 11, 7, 30, tzinfo=UTC)

    def test_weekly_trigger_returns_last_friday(self):
        trigger = CronTrigger(day_of_week="fri", hour=18, minute=30, timezone=UTC)
        # Monday 2026-06-15 — last Friday slot was the 12th.
        now = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
        last = BotScheduler._last_scheduled_fire(trigger, now)
        assert last == datetime(2026, 6, 12, 18, 30, tzinfo=UTC)


class TestRecordJobRun:
    async def test_creates_row_when_absent(self, session_factory):
        scheduler = _make_scheduler(session_factory)
        await scheduler._record_job_run("daily_summary")

        async with session_factory() as session:
            pref = await session.get(JobPreference, "daily_summary")
        assert pref is not None
        assert pref.last_run_at is not None
        # A run-only record defaults to manual mode.
        assert pref.auto is False

    async def test_updates_existing_row_preserving_mode(self, session_factory):
        async with session_factory() as session:
            session.add(
                JobPreference(
                    action="daily_summary",
                    auto=True,
                    updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                    last_run_at=None,
                )
            )
            await session.commit()

        scheduler = _make_scheduler(session_factory)
        await scheduler._record_job_run("daily_summary")

        async with session_factory() as session:
            pref = await session.get(JobPreference, "daily_summary")
        assert pref.last_run_at is not None
        # The user's automatic preference must not be clobbered by a run stamp.
        assert pref.auto is True


class TestRunCatchUp:
    async def test_replays_missed_auto_job(self, session_factory):
        scheduler = _make_scheduler(session_factory)
        scheduler.start()
        try:
            # Enable one eligible job (automatic mode) and stub its trigger so the
            # real job body does not run.
            job = scheduler._scheduler.get_job("refresh_epic_list")
            job.resume()
            stub = AsyncMock()
            scheduler.trigger_refresh_epic_list = stub

            # Last run a week ago → the most recent slot was missed.
            async with session_factory() as session:
                session.add(
                    JobPreference(
                        action="refresh_epic_list",
                        auto=True,
                        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                        last_run_at=datetime.now(UTC) - timedelta(days=7),
                    )
                )
                await session.commit()

            await scheduler.run_catch_up()
            stub.assert_awaited_once()
        finally:
            scheduler.stop()

    async def test_skips_recently_run_job(self, session_factory):
        scheduler = _make_scheduler(session_factory)
        scheduler.start()
        try:
            job = scheduler._scheduler.get_job("refresh_epic_list")
            job.resume()
            stub = AsyncMock()
            scheduler.trigger_refresh_epic_list = stub

            # Ran just now → no missed slot.
            async with session_factory() as session:
                session.add(
                    JobPreference(
                        action="refresh_epic_list",
                        auto=True,
                        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                        last_run_at=datetime.now(UTC),
                    )
                )
                await session.commit()

            await scheduler.run_catch_up()
            stub.assert_not_awaited()
        finally:
            scheduler.stop()

    async def test_skips_manual_job(self, session_factory):
        scheduler = _make_scheduler(session_factory)
        scheduler.start()
        try:
            # Leave refresh_epic_list paused (manual mode — the startup default).
            stub = AsyncMock()
            scheduler.trigger_refresh_epic_list = stub

            async with session_factory() as session:
                session.add(
                    JobPreference(
                        action="refresh_epic_list",
                        auto=False,
                        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                        last_run_at=datetime.now(UTC) - timedelta(days=7),
                    )
                )
                await session.commit()

            await scheduler.run_catch_up()
            # A disabled job is never silently run, even though its slot passed.
            stub.assert_not_awaited()
        finally:
            scheduler.stop()


def _candle(ts: datetime) -> Candle:
    """A minimal candle stamped at ``ts`` for buffer freshness tests."""
    return Candle(
        timestamp=ts,
        bid_open=1.0,
        bid_close=1.0,
        bid_high=1.0,
        bid_low=1.0,
        offer_open=1.0,
        offer_close=1.0,
        offer_high=1.0,
        offer_low=1.0,
    )


class TestTradedTodayEpics:
    """The rolling ranker's one-opening-per-epic-per-day diversity rule."""

    async def test_returns_epics_opened_today_any_state(self, session_factory):
        async with session_factory() as session:
            session.add_all(
                [
                    Position(
                        epic="A",
                        epic_name="A",
                        date=date.today(),
                        state=PositionState.CLOSE,
                    ),
                    Position(
                        epic="B",
                        epic_name="B",
                        date=date.today(),
                        state=PositionState.OPEN,
                    ),
                    Position(
                        epic="C",
                        epic_name="C",
                        date=date.today() - timedelta(days=1),
                        state=PositionState.CLOSE,
                    ),
                ]
            )
            await session.commit()

        async with session_factory() as session:
            traded = await BotScheduler._traded_today_epics(session)

        # A closed today and B open today are "used"; C (yesterday) is free again.
        assert traded == {"A", "B"}


class TestRollingParticipationGate:
    """The rolling ranker refuses to crown a winner from a thin, half-cold pool.

    Guards against "false tournaments" (e.g. just after a mid-session restart):
    a winner is only declared once more than ``min_participation_ratio`` of the
    livestreamed tradable universe has warmed up.
    """

    @staticmethod
    def _warm_up(scheduler: BotScheduler, epic: str, count: int) -> None:
        """Push ``count`` monotonically-stamped candles onto ``epic``'s buffer."""
        base = datetime.now(UTC) - timedelta(minutes=count)
        for i in range(count):
            scheduler._buffer.add_candle(epic, _candle(base + timedelta(minutes=i)))

    def _ranker_scheduler(self, session_factory):
        streaming = MagicMock()
        scheduler = _streaming_scheduler(session_factory, streaming)
        scheduler._strategy = OpenRanking()
        scheduler._tradable_epics = [f"E{i}" for i in range(10)]
        # Spy on scoring: it must run only once the participation gate passes.
        scheduler._strategy.evaluate = MagicMock(return_value=None)
        return scheduler

    async def test_thin_pool_skips_tournament(self, session_factory):
        scheduler = self._ranker_scheduler(session_factory)
        warmup = scheduler._strategy.warmup
        # Only 4 of 10 warmed up (40% <= 50%) — the tournament must be skipped.
        for i in range(4):
            self._warm_up(scheduler, f"E{i}", warmup)

        await scheduler._select_and_open()

        scheduler._strategy.evaluate.assert_not_called()

    async def test_full_pool_runs_tournament(self, session_factory):
        scheduler = self._ranker_scheduler(session_factory)
        warmup = scheduler._strategy.warmup
        # 6 of 10 warmed up (60% > 50%) — scoring proceeds over the ready epics.
        for i in range(6):
            self._warm_up(scheduler, f"E{i}", warmup)

        await scheduler._select_and_open()

        assert scheduler._strategy.evaluate.call_count == 6


class TestWalletBoundedRollingSelect:
    """A wallet-bounded ranker (``open_saferanking``) opens epics until the
    spendable balance can no longer cover another margin, rather than holding a
    single rolling position.
    """

    def _ranker_scheduler(self, session_factory, monkeypatch, available, *, margin):
        """Scheduler with a warmed-up wallet-bounded ranker and stubbed opens.

        Every epic scores a BUY (descending so the order is deterministic) and
        costs ``margin`` euros; ``available`` is what the account reports (or
        ``None`` when unreadable). Returns ``(scheduler, opened)`` where ``opened``
        collects the epics the (stubbed) open path accepted.
        """
        streaming = MagicMock()
        scheduler = _streaming_scheduler(session_factory, streaming)
        scheduler._strategy = OpenSafeRanking()
        # Skip building a real close profile from the MagicMock settings.
        scheduler._close_profile_obj = MagicMock()
        epics = [f"E{i}" for i in range(5)]
        scheduler._tradable_epics = epics

        # Warm every epic so the participation gate passes.
        warmup = scheduler._strategy.warmup
        base = datetime.now(UTC) - timedelta(minutes=warmup)
        for e in epics:
            for i in range(warmup):
                scheduler._buffer.add_candle(e, _candle(base + timedelta(minutes=i)))

        # Deterministic descending ranking: E0 > E1 > ... so opens follow order.
        scores = {e: float(len(epics) - i) for i, e in enumerate(epics)}
        scheduler._strategy.evaluate = MagicMock(
            side_effect=lambda epic, buf: EntryIntent(
                epic=epic, direction="BUY", score=scores[epic]
            )
        )

        scheduler._tradable_markets = [
            SimpleNamespace(epic=e, funds_needed=margin) for e in epics
        ]
        scheduler._account_available_funds = AsyncMock(return_value=available)

        # Stub the guarded open so nothing touches IG; record what was opened.
        opened: list[str] = []

        async def _open(trading, intent, buf):
            opened.append(intent.epic)
            position = Position(
                epic=intent.epic,
                epic_name=intent.epic,
                date=date.today(),
                state=PositionState.OPEN,
                level_open=1.0,
            )
            return position, None

        scheduler.open_epic_guarded = AsyncMock(side_effect=_open)

        # Let every intent through the shared risk gates.
        trading_stub = MagicMock()
        trading_stub.can_open_intent = AsyncMock(return_value=(True, None))
        monkeypatch.setattr(
            "src.core.scheduler.TradingService", lambda *a, **k: trading_stub
        )
        return scheduler, opened

    async def test_opens_until_wallet_is_exhausted(
        self, session_factory, monkeypatch
    ):
        # 450€ spendable (500 − 10% reserve) at 100€/margin → exactly 4 opens,
        # not the single position a count-bounded ranker would hold.
        scheduler, opened = self._ranker_scheduler(
            session_factory, monkeypatch, available=500.0, margin=100.0
        )

        await scheduler._select_and_open()

        assert opened == ["E0", "E1", "E2", "E3"]

    async def test_unknown_balance_falls_back_to_count_cap(
        self, session_factory, monkeypatch
    ):
        # Balance unreadable → cannot size the wallet, so fall back to the count
        # target (concurrent_positions = 1): a hiccup must not dump the ranking.
        scheduler, opened = self._ranker_scheduler(
            session_factory, monkeypatch, available=None, margin=100.0
        )

        await scheduler._select_and_open()

        assert opened == ["E0"]


def _streaming_scheduler(session_factory, streaming):
    """Scheduler wired with a real PriceBuffer and the given streaming stub."""
    settings = MagicMock()
    settings.strategy_hour_close = 17
    settings.streaming_stale_seconds = 180
    return BotScheduler(
        settings=settings,
        client=MagicMock(),
        buffer=PriceBuffer(),
        market_data=MagicMock(),
        recorder=MagicMock(),
        epics=[],
        session_factory=session_factory,
        candle_store=MagicMock(),
        streaming=streaming,
    )


async def _add_open(session_factory, epic: str) -> None:
    async with session_factory() as session:
        session.add(
            Position(
                epic=epic, epic_name=epic, date=date.today(), state=PositionState.OPEN
            )
        )
        await session.commit()


class TestOpenEpicFeedWatchdog:
    """An open position's epic must always keep a live feed."""

    async def test_fresh_feed_is_left_alone(self, session_factory):
        streaming = MagicMock()
        streaming.is_connected = True
        streaming.subscribed_epics = ["A"]
        streaming.resubscribe = AsyncMock()
        scheduler = _streaming_scheduler(session_factory, streaming)
        await _add_open(session_factory, "A")
        scheduler._buffer.add_candle("A", _candle(datetime.now(UTC)))

        await scheduler._ensure_open_epics_streamed()

        streaming.resubscribe.assert_not_awaited()

    async def test_stale_feed_is_resubscribed(self, session_factory):
        streaming = MagicMock()
        streaming.is_connected = True
        streaming.subscribed_epics = ["A"]
        streaming.resubscribe = AsyncMock()
        scheduler = _streaming_scheduler(session_factory, streaming)
        await _add_open(session_factory, "A")
        stale = datetime.now(UTC) - timedelta(seconds=600)
        scheduler._buffer.add_candle("A", _candle(stale))

        await scheduler._ensure_open_epics_streamed()

        streaming.resubscribe.assert_awaited_once_with("A")

    async def test_missing_from_feed_is_resubscribed(self, session_factory):
        streaming = MagicMock()
        streaming.is_connected = True
        streaming.subscribed_epics = []  # open epic dropped off the feed entirely
        streaming.resubscribe = AsyncMock()
        scheduler = _streaming_scheduler(session_factory, streaming)
        await _add_open(session_factory, "A")

        await scheduler._ensure_open_epics_streamed()

        streaming.resubscribe.assert_awaited_once_with("A")

    async def test_just_opened_without_candle_is_left_alone(self, session_factory):
        streaming = MagicMock()
        streaming.is_connected = True
        streaming.subscribed_epics = ["A"]  # subscribed, no candle printed yet
        streaming.resubscribe = AsyncMock()
        scheduler = _streaming_scheduler(session_factory, streaming)
        await _add_open(session_factory, "A")

        await scheduler._ensure_open_epics_streamed()

        streaming.resubscribe.assert_not_awaited()

    async def test_noop_when_disconnected(self, session_factory):
        streaming = MagicMock()
        streaming.is_connected = False
        streaming.resubscribe = AsyncMock()
        scheduler = _streaming_scheduler(session_factory, streaming)
        await _add_open(session_factory, "A")

        await scheduler._ensure_open_epics_streamed()

        streaming.resubscribe.assert_not_awaited()


class TestStreamingHealthCheck:
    """Always-on 24/7 watchdog: reconnect a dead session, else repair per-epic feeds."""

    async def test_forces_reconnect_when_session_down(self, session_factory):
        streaming = MagicMock()
        streaming.is_connected = False
        streaming.ensure_connected = AsyncMock()
        streaming.resubscribe = AsyncMock()
        scheduler = _streaming_scheduler(session_factory, streaming)

        await scheduler._streaming_health_check()

        streaming.ensure_connected.assert_awaited_once()
        # Per-epic repair is skipped while the session is down.
        streaming.resubscribe.assert_not_awaited()

    async def test_repairs_stale_feed_when_connected(self, session_factory):
        streaming = MagicMock()
        streaming.is_connected = True
        streaming.subscribed_epics = ["A"]
        streaming.ensure_connected = AsyncMock()
        streaming.resubscribe = AsyncMock()
        scheduler = _streaming_scheduler(session_factory, streaming)
        await _add_open(session_factory, "A")
        stale = datetime.now(UTC) - timedelta(seconds=600)
        scheduler._buffer.add_candle("A", _candle(stale))

        await scheduler._streaming_health_check()

        # Session is up: no reconnect, but the stalled open-epic feed is resubscribed.
        streaming.ensure_connected.assert_not_awaited()
        streaming.resubscribe.assert_awaited_once_with("A")

    async def test_noop_when_streaming_disabled(self, session_factory):
        scheduler = _make_scheduler(session_factory)  # streaming=None
        await scheduler._streaming_health_check()  # must not raise


def _market(epic: str, status: str = "TRADEABLE") -> SimpleNamespace:
    """A minimal MarketInfo stand-in for the scanner mocks."""
    return SimpleNamespace(
        epic=epic,
        instrument_type="CURRENCIES",
        spread_ratio=1.0,
        status=status,
    )


class TestStreamingKeepsOpenPositions:
    async def test_open_position_epic_stays_subscribed_when_capped(
        self, session_factory
    ):
        settings = MagicMock()
        settings.strategy_hour_close = 17
        settings.streaming_max_epics = 2  # force the cap with 3 candidates
        streaming = MagicMock()
        streaming.set_epics = AsyncMock()
        scheduler = BotScheduler(
            settings=settings,
            client=MagicMock(),
            buffer=MagicMock(),
            market_data=MagicMock(),
            recorder=MagicMock(),
            epics=["A", "B", "C"],
            session_factory=session_factory,
            candle_store=MagicMock(),
            streaming=streaming,
        )

        # Epic A holds an open position but would be dropped by the cap below.
        async with session_factory() as session:
            session.add(
                Position(
                    epic="A",
                    epic_name="A",
                    date=date(2026, 6, 24),
                    state=PositionState.OPEN,
                )
            )
            await session.commit()

        ma, mb, mc = _market("A"), _market("B"), _market("C")
        scheduler._scanner.get_all_market_infos = AsyncMock(return_value=[ma, mb, mc])
        scheduler._scanner.select_tradable = MagicMock(return_value=[ma, mb, mc])
        scheduler._scanner.select_diversified_subset = MagicMock(return_value=[mb, mc])
        scheduler._scanner.get_non_tradable_reasons = MagicMock(return_value={})
        scheduler._persist_epic_enrichment = AsyncMock()
        scheduler._persist_tradable_flags = AsyncMock()

        await scheduler._refresh_tradable_epics()

        # The analysis set is the capped subset — A is dropped for *new* trades.
        assert scheduler._tradable_epics == ["B", "C"]
        # But A (open position) is still streamed, and listed first so it survives
        # set_epics' truncation to the IG subscription cap.
        streaming.set_epics.assert_awaited_once()
        streamed = streaming.set_epics.await_args.args[0]
        assert streamed[0] == "A"
        assert set(streamed) == {"A", "B", "C"}

    async def test_open_epic_no_longer_tradeable_is_force_closed(
        self, session_factory, monkeypatch
    ):
        """Safety net: an open position whose market is no longer TRADEABLE is
        force-closed during the hourly refresh."""
        import src.core.scheduler as scheduler_mod

        settings = MagicMock()
        settings.strategy_hour_close = 17
        settings.streaming_max_epics = 40
        scheduler = BotScheduler(
            settings=settings,
            client=MagicMock(),
            buffer=MagicMock(),
            market_data=MagicMock(),
            recorder=MagicMock(),
            epics=["A", "B"],
            session_factory=session_factory,
            candle_store=MagicMock(),
            streaming=None,
        )

        async with session_factory() as session:
            session.add(
                Position(
                    epic="A",
                    epic_name="A",
                    date=date(2026, 6, 26),
                    state=PositionState.OPEN,
                )
            )
            await session.commit()

        # A is open but now CLOSED on IG; B is fine.
        ma, mb = _market("A", status="CLOSED"), _market("B")
        scheduler._scanner.get_all_market_infos = AsyncMock(return_value=[ma, mb])
        scheduler._scanner.select_tradable = MagicMock(return_value=[mb])
        scheduler._scanner.get_non_tradable_reasons = MagicMock(return_value={})
        scheduler._persist_epic_enrichment = AsyncMock()
        scheduler._persist_tradable_flags = AsyncMock()

        # Capture the close_epics call on the TradingService built inside.
        trading = MagicMock()
        trading.close_epics = AsyncMock(return_value=1)
        monkeypatch.setattr(
            scheduler_mod, "TradingService", MagicMock(return_value=trading)
        )

        await scheduler._refresh_tradable_epics()

        trading.close_epics.assert_awaited_once()
        epics_arg, reason_arg = trading.close_epics.await_args.args
        assert epics_arg == {"A"}
        assert reason_arg == "market_closed"
