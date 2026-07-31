"""Tests for the scheduler's catch-up and streaming-subscription behaviour.

Covers the missed-run catch-up (last-scheduled-fire helper, run persistence,
startup replay) and that epics holding an open position stay subscribed to the
live feed through the hourly tradable refresh, so a trade's price history keeps
recording until it closes.
"""

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.scheduler import BotScheduler, validate_strategy_selection
from src.entry.base import EntryIntent
from src.entry.open_allincrease import OpenAllIncrease
from src.entry.open_five import OpenFive
from src.entry.open_ranking import OpenRanking
from src.entry.open_saferanking import OpenSafeRanking
from src.entry.open_steady import OpenSteady
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
    settings.allow_same_day_reopen = True  # global .env policy (real bool)
    settings.allow_recovery_revert = False  # global .env policy (real bool)
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
            async def can_open_intent(
                self, intent, *, allow_short=False, allow_reopen=False
            ):
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
            async def can_open_intent(
                self, intent, *, allow_short=False, allow_reopen=False
            ):
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

            async def can_open_intent(
                self, intent, *, allow_short=False, allow_reopen=False
            ):
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
            allow_same_day_reopen=False,
            allow_recovery_revert=False,
        )
        validate_strategy_selection(settings)  # does not raise

    def test_missing_same_day_reopen_is_rejected(self):
        # The global re-open policy is required too: no code default, so a
        # missing .env line must fail startup instead of picking a policy.
        settings = SimpleNamespace(
            open_strategy="open_projection",
            stop_strategy="stop_support",
            close_zonestart="hold",
            close_zonemarge="hold",
            close_zoneprofit="trailing_ratchet",
            allow_same_day_reopen=None,
            allow_recovery_revert=False,
        )
        with pytest.raises(ValueError, match="ALLOW_SAME_DAY_REOPEN"):
            validate_strategy_selection(settings)

    def test_same_day_reopen_true_passes(self):
        settings = SimpleNamespace(
            open_strategy="open_projection",
            stop_strategy="stop_support",
            close_zonestart="hold",
            close_zonemarge="hold",
            close_zoneprofit="trailing_ratchet",
            allow_same_day_reopen=True,
            allow_recovery_revert=True,
        )
        validate_strategy_selection(settings)  # does not raise

    def test_missing_recovery_revert_is_rejected(self):
        # A rule that opens a position on its own must be stated explicitly.
        settings = SimpleNamespace(
            open_strategy="open_projection",
            stop_strategy="stop_support",
            close_zonestart="hold",
            close_zonemarge="hold",
            close_zoneprofit="trailing_ratchet",
            allow_same_day_reopen=False,
            allow_recovery_revert=None,
        )
        with pytest.raises(ValueError, match="ALLOW_RECOVERY_REVERT"):
            validate_strategy_selection(settings)

    def test_empty_selection_is_rejected(self):
        settings = SimpleNamespace(
            open_strategy="",
            stop_strategy="",
            close_zonestart="",
            close_zonemarge="",
            close_zoneprofit="",
            allow_same_day_reopen=False,
            allow_recovery_revert=False,
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
            allow_same_day_reopen=False,
            allow_recovery_revert=False,
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


class TestAutoOpenSwitch:
    """The dashboard auto-open switch: block every automatic open for the day.

    It must stop the bot whatever the selected entry strategy, leave manual opens
    alone, survive a restart within the same day, and expire on its own once the
    day is over (the midnight job is user-toggleable and may never run).
    """

    async def test_enabled_by_default(self, session_factory):
        scheduler = _make_scheduler(session_factory)
        assert scheduler.auto_open_enabled is True

    async def test_blocking_refuses_automatic_open(self, session_factory):
        scheduler = _make_scheduler(session_factory)
        await scheduler.set_auto_open(False)
        gate_ran = False

        class _Trading:
            async def can_open_intent(
                self, intent, *, allow_short=False, allow_reopen=False
            ):
                nonlocal gate_ran
                gate_ran = True
                return True, "ok"

            async def open_from_intent(self, intent, buf):
                raise AssertionError("must not open while auto-open is blocked")

        intent = SimpleNamespace(epic="E", direction="BUY")
        position, reason = await scheduler.open_epic_guarded(_Trading(), intent, None)
        assert position is None
        assert reason == "Auto-open disabled for today"
        assert gate_ran is False  # refused before the gate, and before the lock

    async def test_blocking_refuses_short_intents_too(self, session_factory):
        # Direction-agnostic: a two-sided strategy's SELL is blocked as well.
        scheduler = _make_scheduler(session_factory)
        await scheduler.set_auto_open(False)

        class _Trading:
            async def can_open_intent(self, intent, **kwargs):
                raise AssertionError("must not reach the gate")

            async def open_from_intent(self, intent, buf):
                raise AssertionError("must not open")

        intent = SimpleNamespace(epic="E", direction="SELL")
        position, reason = await scheduler.open_epic_guarded(
            _Trading(), intent, None, allow_short=True
        )
        assert position is None
        assert reason == "Auto-open disabled for today"

    async def test_manual_open_bypasses_the_block(self, session_factory):
        # Blocking auto-open stops the bot, never the dashboard Buy/Sell buttons.
        scheduler = _make_scheduler(session_factory)
        await scheduler.set_auto_open(False)
        sentinel = object()

        class _Trading:
            async def can_open_intent(
                self, intent, *, allow_short=False, allow_reopen=False
            ):
                return True, "ok"

            async def open_from_intent(self, intent, buf):
                return sentinel

        intent = SimpleNamespace(epic="E", direction="BUY")
        position, reason = await scheduler.open_epic_guarded(
            _Trading(), intent, None, allow_reopen=True, manual=True
        )
        assert position is sentinel
        assert reason is None

    async def test_re_enabling_restores_automatic_opens(self, session_factory):
        scheduler = _make_scheduler(session_factory)
        await scheduler.set_auto_open(False)
        await scheduler.set_auto_open(True)
        assert scheduler.auto_open_enabled is True

    async def test_block_expires_when_the_day_is_over(self, session_factory):
        scheduler = _make_scheduler(session_factory)
        await scheduler.set_auto_open(False)
        # Simulate the calendar day rolling over while the process kept running.
        scheduler._auto_open_day = date.today() - timedelta(days=1)
        assert scheduler.auto_open_enabled is True
        assert scheduler._auto_open_day is None  # self-healed, not re-checked daily

    async def test_choice_is_persisted(self, session_factory):
        scheduler = _make_scheduler(session_factory)
        await scheduler.set_auto_open(False)

        async with session_factory() as session:
            pref = await session.get(JobPreference, "auto_open")
        assert pref is not None
        assert pref.auto is False

    async def test_same_day_block_is_restored_on_startup(self, session_factory):
        async with session_factory() as session:
            session.add(
                JobPreference(
                    action="auto_open", auto=False, updated_at=datetime.now(UTC)
                )
            )
            await session.commit()

        scheduler = _make_scheduler(session_factory)
        scheduler.start()
        try:
            await scheduler.load_job_preferences()
            assert scheduler.auto_open_enabled is False
        finally:
            scheduler.stop()

    async def test_stale_block_is_ignored_on_startup(self, session_factory):
        # A block set yesterday must not silently carry into today's session.
        async with session_factory() as session:
            session.add(
                JobPreference(
                    action="auto_open",
                    auto=False,
                    updated_at=datetime.now(UTC) - timedelta(days=3),
                )
            )
            await session.commit()

        scheduler = _make_scheduler(session_factory)
        scheduler.start()
        try:
            await scheduler.load_job_preferences()
            assert scheduler.auto_open_enabled is True
        finally:
            scheduler.stop()

    async def test_per_epic_intent_blocked_is_logged(
        self, session_factory, monkeypatch, caplog
    ):
        # A per-epic strategy signals a BUY while the day is closed: nothing must
        # open, and the log must say the switch (not a market gate) stopped it.
        scheduler = _make_scheduler(session_factory)
        # Minimal per-epic strategy stub (cross_epic_selection False) that always
        # signals a BUY — the point is the switch, not any real strategy's logic.
        scheduler._strategy = SimpleNamespace(
            cross_epic_selection=False,
            warmup=1,
            name="stub",
            evaluate=MagicMock(
                return_value=EntryIntent(epic="E", direction="BUY", score=1.0)
            ),
        )
        scheduler._close_profile_obj = MagicMock()
        buf = MagicMock()
        buf.__len__ = MagicMock(return_value=1)
        scheduler._buffer = MagicMock()
        scheduler._buffer.get = MagicMock(return_value=buf)
        trading_stub = MagicMock()
        trading_stub.can_open_intent = AsyncMock(
            side_effect=AssertionError("must not reach the gate")
        )
        monkeypatch.setattr(
            "src.core.scheduler.TradingService", lambda *a, **k: trading_stub
        )
        await scheduler.set_auto_open(False)

        with caplog.at_level(logging.INFO, logger="src.core.scheduler"):
            await scheduler._evaluate_epic("E", MagicMock())

        assert any(
            "Auto-open blocked — skipped BUY on E" in r.getMessage()
            for r in caplog.records
        )

    async def test_rolling_selection_skips_scoring_when_blocked(
        self, session_factory, monkeypatch
    ):
        # A cross-epic ranker must not even score the universe while blocked.
        scheduler = _make_scheduler(session_factory)
        scheduler._strategy = OpenRanking()
        scheduler._strategy.evaluate = MagicMock(
            side_effect=AssertionError("must not score while auto-open is blocked")
        )
        scheduler._tradable_epics = ["E0", "E1"]
        await scheduler.set_auto_open(False)

        await scheduler._select_and_open()  # no-op, no exception


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


def intent_strategy_emits_shorts(scheduler) -> bool:
    """What the selector must forward as ``allow_short`` for this strategy."""
    return getattr(scheduler._strategy, "emits_shorts", False)


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

        async def _open(trading, intent, buf, *, allow_short=False):
            # ``allow_short`` mirrors the real signature: the selector
            # forwards the strategy's ``emits_shorts`` flag down to the gate.
            assert allow_short == intent_strategy_emits_shorts(scheduler)
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

    async def test_opens_until_wallet_is_exhausted(self, session_factory, monkeypatch):
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

    async def test_all_candidates_blocked_logs_wanted_to_open(
        self, session_factory, monkeypatch, caplog
    ):
        # The tournament ranks 5 BUY candidates and has funds, but the base open
        # gates reject every one: nothing opens. Instead of a silent no-op the
        # selector must surface the "wanted to open but no candidate passed the
        # base checks" decision and report the remaining openable pool for today.
        scheduler, opened = self._ranker_scheduler(
            session_factory, monkeypatch, available=500.0, margin=100.0
        )
        # Base/elementary open gate refuses every intent (e.g. market closed).
        monkeypatch.setattr(
            "src.core.scheduler.TradingService",
            lambda *a, **k: SimpleNamespace(
                can_open_intent=AsyncMock(return_value=(False, "market closed"))
            ),
        )

        with caplog.at_level(logging.WARNING, logger="src.core.scheduler"):
            await scheduler._select_and_open()

        assert opened == []
        hits = [
            r.getMessage()
            for r in caplog.records
            if "wanted to open but no candidate passed the base checks"
            in r.getMessage()
        ]
        assert hits, "expected the 'wanted to open' warning"
        # 5 warmed-up untraded epics, all base-gate-blocked, none opened → 5 ranked,
        # 5 blocked by base gates, and all 5 still available to open today.
        assert "5 ranked" in hits[0]
        assert "5 blocked by base gates" in hits[0]
        assert "5 epic(s) still available to open today" in hits[0]


class TestMinutesSinceLastOpen:
    """The open-cooldown clock: minutes since today's most recent open."""

    async def test_none_when_nothing_opened_today(self, session_factory):
        async with session_factory() as session:
            assert await BotScheduler._minutes_since_last_open(session) is None

    async def test_uses_latest_time_open_today(self, session_factory):
        now = datetime.now(UTC)
        async with session_factory() as session:
            session.add_all(
                [
                    Position(
                        epic="A",
                        epic_name="A",
                        date=date.today(),
                        state=PositionState.CLOSE,
                        time_open=(now - timedelta(minutes=30)).time(),
                    ),
                    Position(
                        epic="B",
                        epic_name="B",
                        date=date.today(),
                        state=PositionState.OPEN,
                        time_open=(now - timedelta(minutes=5)).time(),
                    ),
                ]
            )
            await session.commit()
        async with session_factory() as session:
            mins = await BotScheduler._minutes_since_last_open(session)
        # The most recent open (B, 5 min ago) drives the clock, not the older A.
        assert mins is not None
        assert 4.0 < mins < 6.0


class TestOpenAllIncreaseSelection:
    """The paced, re-openable wallet-bounded selection knobs, end-to-end.

    Covers the behaviours ``open_allincrease`` relies on: the ≥10-minute open
    cooldown, one open per pass when a cooldown is set, and the GLOBAL
    ``ALLOW_SAME_DAY_REOPEN`` policy driving the ``_traded_today`` filter (the
    policy lives in ``.env``, not on the strategy, so both values are covered).
    """

    def _scheduler(
        self,
        session_factory,
        monkeypatch,
        *,
        available=1000.0,
        margin=100.0,
        allow_same_day_reopen=True,
    ):
        streaming = MagicMock()
        scheduler = _streaming_scheduler(
            session_factory, streaming, allow_same_day_reopen=allow_same_day_reopen
        )
        scheduler._strategy = OpenAllIncrease()
        scheduler._close_profile_obj = MagicMock()
        epics = [f"E{i}" for i in range(5)]
        scheduler._tradable_epics = epics

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

        opened: list[str] = []

        async def _open(trading, intent, buf, *, allow_short=False):
            # ``allow_short`` mirrors the real signature: the selector
            # forwards the strategy's ``emits_shorts`` flag down to the gate.
            assert allow_short == intent_strategy_emits_shorts(scheduler)
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

        trading_stub = MagicMock()
        trading_stub.can_open_intent = AsyncMock(return_value=(True, None))
        monkeypatch.setattr(
            "src.core.scheduler.TradingService", lambda *a, **k: trading_stub
        )
        return scheduler, opened

    async def test_opens_only_one_per_pass_despite_wallet_room(
        self, session_factory, monkeypatch
    ):
        # Wallet covers all 5 (1000€ − 10% = 900€ at 100€/margin), but the 10-min
        # cooldown caps the pass to a single open — the best-ranked epic.
        scheduler, opened = self._scheduler(session_factory, monkeypatch)
        await scheduler._select_and_open()
        assert opened == ["E0"]

    async def test_cooldown_blocks_when_recent_open(self, session_factory, monkeypatch):
        scheduler, opened = self._scheduler(session_factory, monkeypatch)
        # A position opened 3 min ago (< 10) — the whole pass must be skipped.
        async with session_factory() as session:
            session.add(
                Position(
                    epic="X",
                    epic_name="X",
                    date=date.today(),
                    state=PositionState.OPEN,
                    time_open=(datetime.now(UTC) - timedelta(minutes=3)).time(),
                )
            )
            await session.commit()
        await scheduler._select_and_open()
        assert opened == []

    async def test_cooldown_elapsed_allows_one_open(self, session_factory, monkeypatch):
        scheduler, opened = self._scheduler(session_factory, monkeypatch)
        # Last open was 15 min ago (≥ 10) — the cooldown has elapsed, one opens.
        async with session_factory() as session:
            session.add(
                Position(
                    epic="X",
                    epic_name="X",
                    date=date.today(),
                    state=PositionState.CLOSE,
                    time_open=(datetime.now(UTC) - timedelta(minutes=15)).time(),
                )
            )
            await session.commit()
        await scheduler._select_and_open()
        assert opened == ["E0"]

    async def _seed_closed_e0(self, session_factory) -> None:
        """E0 was opened earlier today and has since closed (cooldown elapsed)."""
        async with session_factory() as session:
            session.add(
                Position(
                    epic="E0",
                    epic_name="E0",
                    date=date.today(),
                    state=PositionState.CLOSE,
                    time_open=(datetime.now(UTC) - timedelta(minutes=20)).time(),
                )
            )
            await session.commit()

    async def test_same_day_reopen_keeps_already_traded_epic(
        self, session_factory, monkeypatch
    ):
        # ALLOW_SAME_DAY_REOPEN=true: E0 stays a candidate and, being top-ranked,
        # is re-opened the same day.
        scheduler, opened = self._scheduler(
            session_factory, monkeypatch, allow_same_day_reopen=True
        )
        await self._seed_closed_e0(session_factory)
        await scheduler._select_and_open()
        assert opened == ["E0"]

    async def test_policy_off_skips_already_traded_epic(
        self, session_factory, monkeypatch
    ):
        # ALLOW_SAME_DAY_REOPEN=false: the same top-ranked E0 is dropped from the
        # candidate set for the rest of the day, so the selector falls to E1.
        scheduler, opened = self._scheduler(
            session_factory, monkeypatch, allow_same_day_reopen=False
        )
        await self._seed_closed_e0(session_factory)
        await scheduler._select_and_open()
        assert opened == ["E1"]


def _streaming_scheduler(session_factory, streaming, *, allow_same_day_reopen=True):
    """Scheduler wired with a real PriceBuffer and the given streaming stub.

    ``allow_same_day_reopen`` is the global ``.env`` policy the rolling selector
    reads (a real boolean, not a MagicMock attribute, so the filter is exercised).
    """
    settings = MagicMock()
    settings.strategy_hour_close = 17
    settings.streaming_stale_seconds = 180
    settings.allow_same_day_reopen = allow_same_day_reopen
    settings.allow_recovery_revert = False  # opt-in policy, off unless tested
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


class TestTwoSidedRankerSelection:
    """A ranker declaring ``emits_shorts`` may open SELL on the automatic path.

    Every entry before ``open_fade`` / ``open_pullback`` was long-only, and both
    the selector (which dropped non-BUY intents) and the shared pre-open gate
    (``allow_short=False``) enforced that. A two-sided ranker must have its SELL
    intents kept *and* have ``allow_short`` forwarded down to the gate — the two
    halves are separate, so both are asserted here.
    """

    def _scheduler(self, session_factory, monkeypatch, *, emits_shorts: bool):
        streaming = MagicMock()
        scheduler = _streaming_scheduler(session_factory, streaming)
        strategy = OpenSafeRanking()
        # Toggle only the two-sided flag; everything else stays the sibling
        # ranker's behaviour so the test isolates the SELL plumbing.
        strategy.emits_shorts = emits_shorts
        strategy.allow_same_day_reopen = True
        scheduler._strategy = strategy
        scheduler._close_profile_obj = MagicMock()
        epics = [f"E{i}" for i in range(4)]
        scheduler._tradable_epics = epics

        warmup = strategy.warmup
        base = datetime.now(UTC) - timedelta(minutes=warmup)
        for e in epics:
            for i in range(warmup):
                scheduler._buffer.add_candle(e, _candle(base + timedelta(minutes=i)))

        # E0/E2 score SELL, E1/E3 score BUY, descending so order is deterministic.
        scores = {e: float(len(epics) - i) for i, e in enumerate(epics)}
        scheduler._strategy.evaluate = MagicMock(
            side_effect=lambda epic, buf: EntryIntent(
                epic=epic,
                direction="SELL" if int(epic[1:]) % 2 == 0 else "BUY",
                score=scores[epic],
            )
        )
        scheduler._tradable_markets = [
            SimpleNamespace(epic=e, funds_needed=100.0) for e in epics
        ]
        scheduler._account_available_funds = AsyncMock(return_value=1000.0)

        opened: list[tuple[str, str]] = []
        gate_calls: list[bool] = []

        async def _open(trading, intent, buf, *, allow_short=False):
            opened.append((intent.epic, intent.direction))
            return (
                Position(
                    epic=intent.epic,
                    epic_name=intent.epic,
                    date=date.today(),
                    state=PositionState.OPEN,
                    level_open=1.0,
                ),
                None,
            )

        scheduler.open_epic_guarded = AsyncMock(side_effect=_open)

        async def _can_open(intent, *, allow_short=False):
            gate_calls.append(allow_short)
            return True, None

        trading_stub = MagicMock()
        trading_stub.can_open_intent = AsyncMock(side_effect=_can_open)
        monkeypatch.setattr(
            "src.core.scheduler.TradingService", lambda *a, **k: trading_stub
        )
        return scheduler, opened, gate_calls

    async def test_two_sided_ranker_opens_both_directions(
        self, session_factory, monkeypatch
    ):
        scheduler, opened, gate_calls = self._scheduler(
            session_factory, monkeypatch, emits_shorts=True
        )

        await scheduler._select_and_open()

        # Both sides survive the selector, ranked by score.
        assert opened == [("E0", "SELL"), ("E1", "BUY"), ("E2", "SELL"), ("E3", "BUY")]
        # ...and the long-only pre-open gate was lifted for every one of them.
        assert gate_calls == [True, True, True, True]

    async def test_long_only_ranker_still_drops_sells(
        self, session_factory, monkeypatch
    ):
        scheduler, opened, gate_calls = self._scheduler(
            session_factory, monkeypatch, emits_shorts=False
        )

        await scheduler._select_and_open()

        # The default stays long-only: SELL candidates never reach the open path.
        assert opened == [("E1", "BUY"), ("E3", "BUY")]
        assert gate_calls == [False, False]


class TestMinParticipationCount:
    """``min_participation_count`` — an absolute candidate floor, not a ratio.

    ``open_steady`` requires strictly more than 20 candidate epics for a ranking to
    be valid. No ``min_participation_ratio`` expresses that on a universe whose
    size drifts between 40 and 51 epics across a session, so the count gate carries
    the rule and the ratio gate is disabled.
    """

    @staticmethod
    def _warm_up(scheduler: BotScheduler, epic: str, count: int) -> None:
        base = datetime.now(UTC) - timedelta(minutes=count)
        for i in range(count):
            scheduler._buffer.add_candle(epic, _candle(base + timedelta(minutes=i)))

    def _scheduler(self, session_factory, ready: int):
        scheduler = _streaming_scheduler(session_factory, MagicMock())
        scheduler._strategy = OpenSteady()
        scheduler._tradable_epics = [f"E{i}" for i in range(40)]
        scheduler._strategy.evaluate = MagicMock(return_value=None)
        for i in range(ready):
            self._warm_up(scheduler, f"E{i}", scheduler._strategy.warmup)
        return scheduler

    async def test_twenty_candidates_is_not_enough(self, session_factory):
        # Exactly 20 ready: the spec wants strictly MORE than 20.
        scheduler = self._scheduler(session_factory, ready=20)

        await scheduler._select_and_open()

        scheduler._strategy.evaluate.assert_not_called()

    async def test_twenty_one_candidates_runs_the_tournament(self, session_factory):
        scheduler = self._scheduler(session_factory, ready=21)

        await scheduler._select_and_open()

        assert scheduler._strategy.evaluate.call_count == 21

    async def test_the_ratio_gate_alone_would_have_allowed_it(self, session_factory):
        """The count gate is what blocks — the disabled ratio gate would not.

        20 of 40 warmed up is 50%, which ``min_participation_ratio = 0.5`` would
        also refuse; the point here is that ``open_steady`` sets the ratio to 0.0,
        so without the count gate the thin pool would have been crowned.
        """
        scheduler = self._scheduler(session_factory, ready=20)
        assert scheduler._strategy.min_participation_ratio == 0.0
        # Ratio disabled => ready (20) > 0.0 * 40, so only the count gate can stop it.
        scheduler._strategy.min_participation_count = 0

        await scheduler._select_and_open()

        assert scheduler._strategy.evaluate.call_count == 20


class TestBlockOpenWhileAlive:
    """``block_open_while_alive`` — stop opening beside a secured winner only.

    A position is *alive* once its software stop (``level_follower``) has ratcheted
    past ``level_margin`` while the close-out price is in profit. A position merely
    *waiting* for its move must NOT block: it may not sit on an opportunity.
    """

    @staticmethod
    def _warm_up(scheduler: BotScheduler, epic: str, count: int) -> None:
        base = datetime.now(UTC) - timedelta(minutes=count)
        for i in range(count):
            scheduler._buffer.add_candle(epic, _candle(base + timedelta(minutes=i)))

    def _scheduler(self, session_factory):
        scheduler = _streaming_scheduler(session_factory, MagicMock())
        scheduler._strategy = OpenSteady()
        # The alive brake must be reached before the participation gate matters.
        scheduler._strategy.min_participation_count = 0
        scheduler._tradable_epics = [f"E{i}" for i in range(3)]
        scheduler._strategy.evaluate = MagicMock(return_value=None)
        for i in range(3):
            self._warm_up(scheduler, f"E{i}", scheduler._strategy.warmup)
        return scheduler

    @staticmethod
    async def _add(session_factory, **levels) -> None:
        async with session_factory() as session:
            session.add(
                Position(
                    epic="HELD",
                    epic_name="HELD",
                    date=date.today(),
                    state=PositionState.OPEN,
                    **levels,
                )
            )
            await session.commit()

    async def test_secured_winner_blocks_opening(self, session_factory):
        scheduler = self._scheduler(session_factory)
        # Software stop past the margin: the gain is locked in.
        await self._add(
            session_factory,
            direction="BUY",
            level_zero=100,
            level_margin=102,
            level_follower=103,
        )
        # A live price in profit, so both alive conditions hold.
        scheduler._buffer.add_candle("HELD", _candle(datetime.now(UTC)))
        scheduler._buffer.get("HELD").candles[-1].bid_close = 110.0

        await scheduler._select_and_open()

        scheduler._strategy.evaluate.assert_not_called()

    async def test_waiting_position_does_not_block(self, session_factory):
        scheduler = self._scheduler(session_factory)
        # Stop still short of the margin — the trade is waiting, not alive.
        await self._add(
            session_factory,
            direction="BUY",
            level_zero=100,
            level_margin=102,
            level_follower=98,
        )

        await scheduler._select_and_open()

        assert scheduler._strategy.evaluate.call_count == 3

    async def test_short_is_judged_mirrored(self, session_factory):
        scheduler = self._scheduler(session_factory)
        # SELL: profit is down, so a locked stop sits BELOW the margin.
        await self._add(
            session_factory,
            direction="SELL",
            level_zero=100,
            level_margin=98,
            level_follower=97,
        )
        scheduler._buffer.add_candle("HELD", _candle(datetime.now(UTC)))
        scheduler._buffer.get("HELD").candles[-1].offer_close = 90.0

        await scheduler._select_and_open()

        scheduler._strategy.evaluate.assert_not_called()

    async def test_short_with_a_stop_the_wrong_side_does_not_block(
        self, session_factory
    ):
        scheduler = self._scheduler(session_factory)
        # Same numbers as a locked long, which for a SELL means NOT yet locked.
        await self._add(
            session_factory,
            direction="SELL",
            level_zero=100,
            level_margin=98,
            level_follower=103,
        )

        await scheduler._select_and_open()

        assert scheduler._strategy.evaluate.call_count == 3

    async def test_row_without_levels_does_not_block(self, session_factory):
        """An adopted/legacy row cannot be judged — it must not freeze opening."""
        scheduler = self._scheduler(session_factory)
        await self._add(session_factory, direction="BUY")

        await scheduler._select_and_open()

        assert scheduler._strategy.evaluate.call_count == 3

    async def test_locked_stop_with_no_live_price_still_blocks(self, session_factory):
        """Condition 1 is decisive: a missing feed must not unsecure a winner."""
        scheduler = self._scheduler(session_factory)
        await self._add(
            session_factory,
            direction="BUY",
            level_zero=100,
            level_margin=102,
            level_follower=103,
        )
        # No candle for HELD at all — the live-price confirmation is skipped.
        assert scheduler._buffer.get("HELD") is None

        await scheduler._select_and_open()

        scheduler._strategy.evaluate.assert_not_called()

    async def test_other_strategies_are_unaffected(self, session_factory):
        """The brake is opt-in: a ranker that does not declare it keeps opening."""
        scheduler = _streaming_scheduler(session_factory, MagicMock())
        scheduler._strategy = OpenSafeRanking()
        assert scheduler._strategy.block_open_while_alive is False
        # Neutralise the sibling's own participation ratio so the only thing that
        # could stop this pass is the alive brake it does not declare.
        scheduler._strategy.min_participation_ratio = 0.0
        scheduler._strategy.evaluate = MagicMock(return_value=None)
        scheduler._tradable_epics = [f"E{i}" for i in range(3)]
        for i in range(3):
            self._warm_up(scheduler, f"E{i}", scheduler._strategy.warmup)
        await self._add(
            session_factory,
            direction="BUY",
            level_zero=100,
            level_margin=102,
            level_follower=103,
        )

        await scheduler._select_and_open()

        assert scheduler._strategy.evaluate.call_count == 3


class TestRecoveryRevert:
    """``ALLOW_RECOVERY_REVERT``: flip the side after an opening-stop loss.

    A position taken out at a loss by the stop it was OPENED with means the
    market walked through the level the trade was built on, so the bot opens the
    opposite side at once (see ``BotScheduler._revert_after_stop_loss``). The
    reverse open goes through the shared guarded path with the long-only and
    same-day-re-open gates lifted, and is capped at one hop.
    """

    def _scheduler(self, session_factory, *, allow: bool = True) -> BotScheduler:
        scheduler = _streaming_scheduler(session_factory, MagicMock())
        scheduler._settings.allow_recovery_revert = allow  # real bool, not a Mock
        # Skip building a real close profile from the MagicMock settings.
        scheduler._close_profile_obj = MagicMock()
        # One live candle so the revert has a price to open on.
        scheduler._buffer.add_candle("E", _candle(datetime.now(UTC)))
        return scheduler

    def _stub_open(self, scheduler) -> list[dict]:
        """Record every guarded open instead of touching IG."""
        calls: list[dict] = []

        async def _open(trading, intent, buf, **kwargs):
            calls.append({"direction": intent.direction, "epic": intent.epic, **kwargs})
            return (
                Position(
                    epic=intent.epic,
                    epic_name=intent.epic,
                    date=date.today(),
                    direction=intent.direction,
                    state=PositionState.OPEN,
                    level_open=1.0,
                ),
                None,
            )

        scheduler.open_epic_guarded = AsyncMock(side_effect=_open)
        return calls

    @staticmethod
    def _stopped_out(**overrides) -> Position:
        """A long closed at a loss on the stop placed at open."""
        fields = {
            "epic": "E",
            "epic_name": "E",
            "date": date.today(),
            "direction": "BUY",
            "state": PositionState.CLOSE,
            "reason_open": "auto",
            "reason_close": "closed_externally",
            "level_open": Decimal("1.10000"),
            "level_close": Decimal("1.09800"),
            "level_follower": Decimal("1.09850"),
            "stop_update": 0,
            "stop_history": [{"t": "1", "level": 1.09850, "broker": 1.09800}],
            "euro": Decimal("-12.500"),
        }
        fields.update(overrides)
        return Position(**fields)

    async def test_opening_stop_loss_opens_the_reverse_side(self, session_factory):
        scheduler = self._scheduler(session_factory)
        calls = self._stub_open(scheduler)
        position = self._stopped_out()

        async with session_factory() as session:
            session.add(position)
            await session.commit()
            reverse = await scheduler._revert_after_stop_loss(
                session, position, scheduler._build_trade_config()
            )

        assert reverse is not None
        # The reverse of a long is a short, so the long-only gate must be lifted,
        # and the epic was traded seconds ago so the same-day gate must be too.
        assert calls == [
            {
                "direction": "SELL",
                "epic": "E",
                "allow_short": True,
                "allow_reopen": True,
            }
        ]
        # Not a manual open: the auto-open switch still governs the revert.
        assert "manual" not in calls[0]
        assert reverse.reason_open == "recovery_revert"

    async def test_short_stopped_out_reverts_to_a_long(self, session_factory):
        scheduler = self._scheduler(session_factory)
        calls = self._stub_open(scheduler)
        position = self._stopped_out(
            direction="SELL",
            level_close=Decimal("1.10200"),
            level_follower=Decimal("1.10150"),
            stop_history=[{"t": "1", "level": 1.10150, "broker": 1.10200}],
        )

        async with session_factory() as session:
            await scheduler._revert_after_stop_loss(
                session, position, scheduler._build_trade_config()
            )

        assert [c["direction"] for c in calls] == ["BUY"]

    async def test_policy_off_never_reverts(self, session_factory):
        scheduler = self._scheduler(session_factory, allow=False)
        calls = self._stub_open(scheduler)

        async with session_factory() as session:
            reverse = await scheduler._revert_after_stop_loss(
                session, self._stopped_out(), scheduler._build_trade_config()
            )

        assert reverse is None and calls == []

    async def test_a_win_never_reverts(self, session_factory):
        scheduler = self._scheduler(session_factory)
        calls = self._stub_open(scheduler)

        async with session_factory() as session:
            reverse = await scheduler._revert_after_stop_loss(
                session,
                self._stopped_out(euro=Decimal("9.000"), reason_close="win"),
                scheduler._build_trade_config(),
            )

        assert reverse is None and calls == []

    async def test_a_revert_is_not_reverted_again(self, session_factory):
        # Single hop: a stopped-out revert does not flip back, so a choppy market
        # cannot ping-pong the account through an endless BUY/SELL sequence.
        scheduler = self._scheduler(session_factory)
        calls = self._stub_open(scheduler)

        async with session_factory() as session:
            reverse = await scheduler._revert_after_stop_loss(
                session,
                self._stopped_out(reason_open="recovery_revert"),
                scheduler._build_trade_config(),
            )

        assert reverse is None and calls == []

    async def test_no_price_data_skips_the_revert(self, session_factory):
        scheduler = self._scheduler(session_factory)
        calls = self._stub_open(scheduler)

        async with session_factory() as session:
            reverse = await scheduler._revert_after_stop_loss(
                session,
                self._stopped_out(epic="NOFEED", epic_name="NOFEED"),
                scheduler._build_trade_config(),
            )

        assert reverse is None and calls == []

    async def test_gate_refusal_is_not_an_error(self, session_factory):
        scheduler = self._scheduler(session_factory)
        scheduler.open_epic_guarded = AsyncMock(
            return_value=(None, "Market E closes soon")
        )

        async with session_factory() as session:
            reverse = await scheduler._revert_after_stop_loss(
                session, self._stopped_out(), scheduler._build_trade_config()
            )

        assert reverse is None

    async def test_monitor_reverts_after_a_software_stop_close(
        self, session_factory, monkeypatch
    ):
        """The monitor tick that closes on the follower opens the reverse side."""
        scheduler = self._scheduler(session_factory)
        calls = self._stub_open(scheduler)

        async with session_factory() as session:
            position = self._stopped_out(state=PositionState.OPEN)
            session.add(position)
            await session.commit()

        async def _manage(position, current_bid, buf=None, **kwargs):
            # Stand in for the real close: the software backstop fired on the
            # stop placed at open.
            position.state = PositionState.CLOSE
            position.reason_close = "stop"
            return True

        trading_stub = MagicMock()
        trading_stub.manage_position = AsyncMock(side_effect=_manage)
        monkeypatch.setattr(
            "src.core.scheduler.TradingService", lambda *a, **k: trading_stub
        )

        await scheduler._monitor_positions()

        assert [c["direction"] for c in calls] == ["SELL"]

    async def test_sync_reverts_a_broker_stop_out(self, session_factory, monkeypatch):
        """A stop filled at IG is only seen by the sync — it reverts too."""
        scheduler = self._scheduler(session_factory)
        calls = self._stub_open(scheduler)
        closed = self._stopped_out()

        trading_stub = MagicMock()
        trading_stub.sync_open_positions = AsyncMock(return_value={})
        trading_stub.reconciled_closed = [closed]
        monkeypatch.setattr(
            "src.core.scheduler.TradingService", lambda *a, **k: trading_stub
        )

        await scheduler._sync_positions()

        assert [c["direction"] for c in calls] == ["SELL"]


class TestRequireFlatBook:
    """``require_flat_book`` — open in *series*, never top up.

    ``open_five`` opens a basket of five in one pass and then waits for the book to
    be **completely** empty before opening the next one, so a series can be judged
    as a whole. It is deliberately stricter than ``block_open_while_alive``: any
    open position blocks, secured or not, and a row too incomplete to judge blocks
    too (that brake lets both through).
    """

    @staticmethod
    def _warm_up(scheduler: BotScheduler, epic: str, count: int) -> None:
        base = datetime.now(UTC) - timedelta(minutes=count)
        for i in range(count):
            scheduler._buffer.add_candle(epic, _candle(base + timedelta(minutes=i)))

    def _scheduler(self, session_factory, strategy):
        scheduler = _streaming_scheduler(session_factory, MagicMock())
        scheduler._strategy = strategy
        # The participation gates are exercised on their own elsewhere; keep the
        # universe small so this test isolates the flat-book brake.
        strategy.min_participation_ratio = 0.0
        strategy.min_participation_count = 0
        scheduler._tradable_epics = [f"E{i}" for i in range(3)]
        strategy.evaluate = MagicMock(return_value=None)
        for i in range(3):
            self._warm_up(scheduler, f"E{i}", strategy.warmup)
        return scheduler

    @staticmethod
    async def _add_position(session_factory, **fields) -> None:
        async with session_factory() as session:
            session.add(
                Position(
                    epic="HELD",
                    epic_name="HELD",
                    date=date.today(),
                    state=PositionState.OPEN,
                    **fields,
                )
            )
            await session.commit()

    async def test_an_empty_book_runs_the_tournament(self, session_factory):
        scheduler = self._scheduler(session_factory, OpenFive())

        await scheduler._select_and_open()

        assert scheduler._strategy.evaluate.call_count == 3

    async def test_any_open_position_blocks_the_next_series(self, session_factory):
        scheduler = self._scheduler(session_factory, OpenFive())
        # One open position out of a basket of five: the four free slots are NOT
        # topped up — the whole series waits.
        await self._add_position(session_factory, direction="BUY")

        await scheduler._select_and_open()

        scheduler._strategy.evaluate.assert_not_called()

    async def test_a_waiting_position_blocks_too(self, session_factory):
        """Where it differs from ``block_open_while_alive``.

        This position's software stop is nowhere near its margin, so it is merely
        *waiting* and the alive brake would let the selector keep opening. The
        series model blocks anyway.
        """
        scheduler = self._scheduler(session_factory, OpenFive())
        await self._add_position(
            session_factory,
            direction="BUY",
            level_zero=100,
            level_margin=102,
            level_follower=98,
        )

        await scheduler._select_and_open()

        scheduler._strategy.evaluate.assert_not_called()

    async def test_other_strategies_are_unaffected(self, session_factory):
        """The brake is opt-in: a ranker that does not declare it keeps opening."""
        strategy = OpenSafeRanking()
        scheduler = self._scheduler(session_factory, strategy)
        assert strategy.require_flat_book is False
        await self._add_position(session_factory, direction="BUY")

        await scheduler._select_and_open()

        assert strategy.evaluate.call_count == 3


class TestCrossEpicFilter:
    """``filter_ranked`` — the strategy's look at the sorted ranking before opening.

    The scheduler must apply the hook *after* the sort and *before* the first
    order, and must honour the shortened list: a refused candidate loses its slot
    to the next survivor rather than leaving a hole in the basket. The duplicate
    maths itself lives in ``tests/test_open_five.py``; here the hook is stubbed so
    only the wiring is under test.
    """

    def _scheduler(self, session_factory, monkeypatch, *, epic_count: int = 8):
        scheduler = _streaming_scheduler(session_factory, MagicMock())
        strategy = OpenFive()
        strategy.min_participation_ratio = 0.0
        strategy.min_participation_count = 0
        scheduler._strategy = strategy
        scheduler._close_profile_obj = MagicMock()
        epics = [f"E{i}" for i in range(epic_count)]
        scheduler._tradable_epics = epics

        base = datetime.now(UTC) - timedelta(minutes=strategy.warmup)
        for epic in epics:
            for i in range(strategy.warmup):
                scheduler._buffer.add_candle(epic, _candle(base + timedelta(minutes=i)))

        # Descending scores so the ranking order is deterministic and equals the
        # epic order.
        strategy.evaluate = MagicMock(
            side_effect=lambda epic, buf: EntryIntent(
                epic=epic, direction="BUY", score=float(epic_count - int(epic[1:]))
            )
        )
        scheduler._tradable_markets = [
            SimpleNamespace(epic=e, funds_needed=100.0) for e in epics
        ]
        scheduler._account_available_funds = AsyncMock(return_value=100_000.0)

        opened: list[str] = []

        async def _open(trading, intent, buf, *, allow_short=False):
            opened.append(intent.epic)
            return (
                Position(
                    epic=intent.epic,
                    epic_name=intent.epic,
                    date=date.today(),
                    state=PositionState.OPEN,
                    level_open=1.0,
                ),
                None,
            )

        scheduler.open_epic_guarded = AsyncMock(side_effect=_open)
        trading_stub = MagicMock()
        trading_stub.can_open_intent = AsyncMock(return_value=(True, None))
        monkeypatch.setattr(
            "src.core.scheduler.TradingService", lambda *a, **k: trading_stub
        )
        return scheduler, opened

    async def test_the_basket_is_opened_in_one_pass(self, session_factory, monkeypatch):
        scheduler, opened = self._scheduler(session_factory, monkeypatch)

        await scheduler._select_and_open()

        # Five at once, best-ranked first — the series model.
        assert opened == ["E0", "E1", "E2", "E3", "E4"]

    async def test_refused_candidates_lose_their_slot_to_the_next(
        self, session_factory, monkeypatch
    ):
        scheduler, opened = self._scheduler(session_factory, monkeypatch)
        # Stand in for the duplicate-shape veto: drop ranks 2 and 3.
        scheduler._strategy.filter_ranked = MagicMock(
            side_effect=lambda ranked: [ranked[0]] + ranked[3:]
        )

        await scheduler._select_and_open()

        # The basket is still five deep — E3/E4 were promoted into the free slots.
        assert opened == ["E0", "E3", "E4", "E5", "E6"]

    async def test_the_hook_sees_the_sorted_ranking(self, session_factory, monkeypatch):
        scheduler, _ = self._scheduler(session_factory, monkeypatch)
        seen: list[list[tuple[str, float]]] = []

        def _spy(ranked):
            seen.append([(i.epic, i.score) for i, _ in ranked])
            return ranked

        scheduler._strategy.filter_ranked = MagicMock(side_effect=_spy)

        await scheduler._select_and_open()

        assert len(seen) == 1
        scores = [score for _epic, score in seen[0]]
        assert scores == sorted(scores, reverse=True)

    async def test_filtering_everything_out_opens_nothing(
        self, session_factory, monkeypatch
    ):
        scheduler, opened = self._scheduler(session_factory, monkeypatch)
        scheduler._strategy.filter_ranked = MagicMock(return_value=[])

        await scheduler._select_and_open()

        assert opened == []
        scheduler.open_epic_guarded.assert_not_called()


class TestGroupStopPrePass:
    """The monitor's whole-book pre-pass for a group-aware close profile.

    ``smartgroup`` claims "all these stops together still book a gain", which is
    only true if every open position is in the sum. The scheduler must therefore
    hand the profile a *complete* book or none at all, and feed each position its
    own resolved level.
    """

    def _scheduler(self, session_factory, monkeypatch, *, epics=("A", "B")):
        scheduler = _streaming_scheduler(session_factory, MagicMock())
        for epic in epics:
            scheduler._buffer.add_candle(epic, _candle(datetime.now(UTC)))

        profile = MagicMock()
        profile.is_group_aware = True
        profile.group_member = MagicMock(side_effect=lambda p, bid, buf: p.id)
        profile.plan_group = MagicMock(return_value={1: 1.5, 2: 2.5})
        scheduler._close_profile_obj = profile

        seen: list[tuple[str, float | None]] = []

        async def _manage(position, current_bid, buf=None, *, group_tighten=None):
            seen.append((position.epic, group_tighten))
            return False

        trading_stub = MagicMock()
        trading_stub.manage_position = AsyncMock(side_effect=_manage)
        monkeypatch.setattr(
            "src.core.scheduler.TradingService", lambda *a, **k: trading_stub
        )
        return scheduler, profile, seen

    @staticmethod
    async def _add_positions(session_factory, epics) -> None:
        async with session_factory() as session:
            for i, epic in enumerate(epics, start=1):
                session.add(
                    Position(
                        id=i,
                        epic=epic,
                        epic_name=epic,
                        date=date.today(),
                        direction="BUY",
                        state=PositionState.OPEN,
                        level_open=Decimal("1.10000"),
                    )
                )
            await session.commit()

    async def test_each_position_gets_its_own_level(self, session_factory, monkeypatch):
        scheduler, profile, seen = self._scheduler(session_factory, monkeypatch)
        await self._add_positions(session_factory, ("A", "B"))

        await scheduler._monitor_positions()

        assert profile.plan_group.call_args.args[0] == [1, 2]
        assert sorted(seen) == [("A", 1.5), ("B", 2.5)]

    async def test_an_unpriceable_position_skips_the_whole_plan(
        self, session_factory, monkeypatch
    ):
        # B cannot be priced, so its (possibly negative) contribution is missing
        # from the book total — planning on the remaining member would arm a
        # tightening the group cannot afford. B is still managed normally.
        scheduler, profile, seen = self._scheduler(session_factory, monkeypatch)
        profile.group_member = MagicMock(
            side_effect=lambda p, bid, buf: p.id if p.epic == "A" else None
        )
        await self._add_positions(session_factory, ("A", "B"))

        await scheduler._monitor_positions()

        profile.plan_group.assert_not_called()
        assert sorted(seen) == [("A", None), ("B", None)]


class TestCandleDrivenMonitoring:
    """Position management is triggered by data arrival, not by the clock.

    A candle is the only moment the price the close profile reads changes, so the
    feed drives the pass and the 30 s cron is only a heartbeat. What must hold: one
    pass per *wave* of candles (never one per epic — the group pre-pass needs the
    whole book in a single pass), the user's pause is honoured, and two triggers can
    never run two concurrent passes.
    """

    def _scheduler(self, session_factory, *, paused: bool = False) -> BotScheduler:
        sched = _make_scheduler(session_factory)
        sched._settings.monitor_debounce_seconds = 0.01  # real float, not a Mock
        sched._running = True
        job = SimpleNamespace(next_run_time=None if paused else datetime.now(UTC))
        sched._scheduler = MagicMock()
        sched._scheduler.get_job = MagicMock(return_value=job)
        return sched

    async def test_a_wave_of_candles_runs_exactly_one_pass(self, session_factory):
        sched = self._scheduler(session_factory)
        sched._monitor_positions = AsyncMock()

        for epic in ("A", "B", "C", "D"):  # one wave, ~40 epics in production
            sched.on_candle(epic, MagicMock())
        await asyncio.sleep(0.05)

        sched._monitor_positions.assert_awaited_once()

    async def test_a_later_wave_runs_its_own_pass(self, session_factory):
        sched = self._scheduler(session_factory)
        sched._monitor_positions = AsyncMock()

        sched.on_candle("A", MagicMock())
        await asyncio.sleep(0.05)
        sched.on_candle("A", MagicMock())
        await asyncio.sleep(0.05)

        assert sched._monitor_positions.await_count == 2

    async def test_a_paused_job_silences_the_candle_trigger(self, session_factory):
        # Pausing "Monitor Positions" on the dashboard must stop the event-driven
        # path too, or the toggle would quietly stop meaning anything.
        sched = self._scheduler(session_factory, paused=True)
        sched._monitor_positions = AsyncMock()

        sched.on_candle("A", MagicMock())
        await asyncio.sleep(0.05)

        sched._monitor_positions.assert_not_awaited()

    async def test_pausing_during_the_debounce_cancels_the_pass(self, session_factory):
        sched = self._scheduler(session_factory)
        sched._monitor_positions = AsyncMock()

        sched.on_candle("A", MagicMock())
        sched._scheduler.get_job.return_value = SimpleNamespace(next_run_time=None)
        await asyncio.sleep(0.05)

        sched._monitor_positions.assert_not_awaited()

    async def test_a_pass_in_flight_makes_a_new_trigger_a_no_op(self, session_factory):
        # Skipping, not queueing: the running pass is already reading the data the
        # new trigger would read, and two concurrent passes could both push a stop.
        sched = self._scheduler(session_factory)
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def slow_pass():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()

        sched._monitor_positions_pass = slow_pass
        first = asyncio.ensure_future(sched._monitor_positions())
        await started.wait()

        await sched._monitor_positions()  # cron heartbeat lands mid-pass
        assert calls == 1

        release.set()
        await first
        await sched._monitor_positions()  # free again
        assert calls == 2

    def test_the_listener_is_registered_only_when_streaming_is_on(
        self, session_factory
    ):
        sched = _make_scheduler(session_factory)
        sched._register_candle_listener()  # streaming is None — must not raise

        streaming = MagicMock()
        sched._streaming = streaming
        sched._settings.monitor_debounce_seconds = 2.0
        sched._register_candle_listener()

        streaming.add_candle_listener.assert_called_once_with(sched.on_candle)

        # Idempotent: a second start() must not subscribe twice (two passes per wave)
        sched._register_candle_listener()
        streaming.add_candle_listener.assert_called_once()


class TestWarmupCapacityWarning:
    """A strategy needing more history than the buffer holds must say so.

    The buffer window is a hard ceiling on every lookback, and exceeding it is
    silent: the strategy just runs on a truncated window, which is indistinguishable
    from it merely underperforming.
    """

    def _scheduler(self, session_factory, *, warmup: int, capacity: int):
        sched = _make_scheduler(session_factory)
        sched._buffer = PriceBuffer(max_candles=capacity)
        sched._strategy = SimpleNamespace(name="open_stub", warmup=warmup)
        return sched

    def test_warns_when_the_warmup_exceeds_capacity(self, session_factory, caplog):
        sched = self._scheduler(session_factory, warmup=300, capacity=200)
        with caplog.at_level(logging.WARNING):
            sched._warn_if_warmup_exceeds_buffer()
        assert "TRUNCATED" in caplog.text
        assert "300" in caplog.text

    def test_silent_when_the_buffer_is_big_enough(self, session_factory, caplog):
        sched = self._scheduler(session_factory, warmup=180, capacity=200)
        with caplog.at_level(logging.WARNING):
            sched._warn_if_warmup_exceeds_buffer()
        assert caplog.text == ""
