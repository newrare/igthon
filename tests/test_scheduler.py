"""Tests for the scheduler's missed-run catch-up mechanism.

Covers the helper that finds the last scheduled fire time, the persistence of a
job's last successful run, and the startup replay of fixed-time jobs whose slot
was missed while the server was down.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.scheduler import BotScheduler
from src.models.database import Base
from src.models.job_preference import JobPreference


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


class TestSetStrategy:
    """Runtime entry-strategy switching via the dashboard title dropdown.

    Switching swaps the *entry* (open) strategy only; the close profile is
    chosen independently (open/close decoupling). The entry registry currently
    holds ``donchian_er`` (others are ported later).
    """

    def test_switch_to_registered_strategy(self):
        sched = _make_scheduler(MagicMock())
        sched._settings.entry_strategy_name = "something_else"
        assert sched.set_strategy("donchian_er") is True
        assert sched.active_strategy_name == "donchian_er"

    def test_unknown_strategy_is_rejected(self):
        sched = _make_scheduler(MagicMock())
        sched._settings.entry_strategy_name = "donchian_er"
        assert sched.set_strategy("not_a_strategy") is False
        assert sched.active_strategy_name == "donchian_er"

    def test_switch_clears_cached_instance(self):
        sched = _make_scheduler(MagicMock())
        sched._strategy = object()  # pretend a strategy was already built
        sched.set_strategy("donchian_er")
        # Cleared so the ``strategy`` property rebuilds from the new name.
        assert sched._strategy is None


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
