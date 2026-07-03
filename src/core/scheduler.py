"""Scheduler service — replaces CRON scripts.

Uses APScheduler to run periodic tasks:
- Price collection and analysis (every 30s-2min)
- Position monitoring (every 30s)
- End-of-day forced close and summary
- Daily/weekly summaries
- Daily epic list refresh from IG market search (7:30 AM)
- Hourly active epic refresh (filter by TRADEABLE status)
"""

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.api.client import IGAPIError, IGClient
from src.core.api_queue import APIQueue, Priority
from src.core.config import Settings
from src.core.recorder import Recorder
from src.entry import ENTRY_STRATEGIES, EntryIntent, EntryStrategy, get_entry_strategy
from src.execution.recovery import is_recovery_trigger
from src.execution.trading import (
    DEFAULT_MARKET_CLOSE_HOUR_UTC,
    TradeConfig,
    TradingService,
)
from src.exit import (
    ZONEMARGE_UPDATERS,
    ZONEPROFIT_UPDATERS,
    ZONESTART_UPDATERS,
    CloseProfile,
    get_close_profile,
)
from src.feed.candle_store import CandleStore
from src.feed.market_data import MarketDataService
from src.feed.price_buffer import DEFAULT_MAX_CANDLES, EpicBuffer, PriceBuffer
from src.markets.market_scanner import MarketInfo, MarketScanner
from src.models.day import Day, DayState
from src.models.epic import Epic
from src.models.job_preference import JobPreference
from src.models.position import Position, PositionState
from src.models.resume import Resume
from src.stops import STOP_DISTANCES

if TYPE_CHECKING:
    from src.feed.streaming import IGStreamingClient

logger = logging.getLogger(__name__)


def validate_strategy_selection(settings: Settings) -> None:
    """Ensure the three strategy selections from ``.env`` are set and known.

    The ``.env`` file is the single source of truth for the open / stop / close
    selection — there is no code default and no persistence. A missing, empty or
    unknown name raises a single actionable error naming every offending
    variable, so the bot and the dashboard can surface a "configure your .env"
    message instead of failing obscurely deep in the pipeline.

    Raises:
        ValueError: when any of ``OPEN_STRATEGY`` / ``STOP_STRATEGY`` /
            ``CLOSE_ZONESTART`` / ``CLOSE_ZONEMARGE`` / ``CLOSE_ZONEPROFIT`` is
            empty or not a registered name.
    """
    checks = (
        ("OPEN_STRATEGY", settings.open_strategy, ENTRY_STRATEGIES),
        ("STOP_STRATEGY", settings.stop_strategy, STOP_DISTANCES),
        ("CLOSE_ZONESTART", settings.close_zonestart, ZONESTART_UPDATERS),
        ("CLOSE_ZONEMARGE", settings.close_zonemarge, ZONEMARGE_UPDATERS),
        ("CLOSE_ZONEPROFIT", settings.close_zoneprofit, ZONEPROFIT_UPDATERS),
    )
    problems: list[str] = []
    for var, name, registry in checks:
        if not name:
            problems.append(f"{var} is not set")
        elif name not in registry:
            problems.append(
                f"{var}={name!r} is unknown (available: {sorted(registry)})"
            )
    if problems:
        raise ValueError(
            "Invalid strategy selection — please configure your .env file:\n  - "
            + "\n  - ".join(problems)
        )


# Registry of schedulable jobs surfaced in the dashboard "Actions" section.
# ``action`` is the manual-trigger key (maps to ``trigger_<action>`` and the
# ``/api/actions`` endpoint); ``job_id`` is the APScheduler job id, which differs
# from the action for the analysis loop. ``danger`` drives the Run-button colour
# and whether a confirmation prompt is shown on the dashboard.
#
# ``catch_up`` (optional, default False) marks fixed-time jobs that should be
# replayed once on startup if their scheduled time was missed while the server
# was down — see ``BotScheduler.run_catch_up``. Only idempotent jobs that simply
# refresh or recompute from the database are enabled: re-running them late is
# harmless. Frequent recurring jobs (collect/monitor/sync/reconcile/hourly
# refresh) self-heal on their next tick and are never caught up; ``end_of_day``
# (would force-close live positions) and ``daily_reset`` (would wipe the buffer
# already rehydrated for today) are deliberately excluded.
JOB_DEFINITIONS: list[dict[str, str | bool]] = [
    {
        "action": "refresh_epic_list",
        "job_id": "refresh_epic_list",
        "name": "Refresh Epic List",
        "description": "Search IG markets and rebuild the full epic list.",
        "schedule": "Daily 07:30 · Mon–Fri",
        "danger": "safe",
        "catch_up": True,
    },
    {
        "action": "refresh_tradable_epics",
        "job_id": "refresh_tradable_epics",
        "name": "Refresh Tradable Epics",
        "description": "Filter the epic list to those currently open and TRADEABLE.",
        "schedule": "Hourly 08–17 · Mon–Fri",
        "danger": "safe",
    },
    {
        "action": "collect_and_analyze",
        "job_id": "collect_analyze",
        "name": "Collect & Analyze",
        "description": "Fetch latest prices, compute signals, open positions on BUY.",
        "schedule": "Every 30s · 08–17 · Mon–Fri",
        "danger": "safe",
    },
    {
        "action": "trend_select",
        "job_id": "trend_select",
        "name": "Trend Select (backstop)",
        "description": (
            "Backstop for the rolling cross-epic selection (also runs every "
            "analysis tick): re-ranks tradable epics and tops the portfolio up "
            "to its target open-position count. Active only with a cross-epic "
            "ranker entry (e.g. open_ranking); a no-op for per-epic entries."
        ),
        "schedule": "Hourly · 09–16 · Mon–Fri",
        "danger": "safe",
    },
    {
        "action": "monitor_positions",
        "job_id": "monitor_positions",
        "name": "Monitor Positions",
        "description": "Check all open positions and apply the close strategy.",
        "schedule": "Every 30s · 08–18 · Mon–Fri",
        "danger": "safe",
    },
    {
        "action": "sync_positions",
        "job_id": "sync_positions",
        "name": "Sync Positions",
        "description": (
            "Reconcile DB positions against IG's live list: refresh euro/bid "
            "and close positions shut outside the bot."
        ),
        "schedule": "Every 20s · 24/7",
        "danger": "safe",
    },
    {
        "action": "end_of_day",
        "job_id": "end_of_day",
        "name": "End of Day",
        "description": "Force close ALL open positions immediately.",
        "schedule": "Daily — close hour · Mon–Fri",
        "danger": "danger",
    },
    {
        "action": "reconcile_pnl",
        "job_id": "reconcile_pnl",
        "name": "Reconcile P&L",
        "description": (
            "Overwrite today's closed-position euro P&L and open/close levels "
            "with IG's authoritative transaction history."
        ),
        "schedule": "Every 10 min · 09–19 · Mon–Fri",
        "danger": "safe",
    },
    {
        "action": "daily_summary",
        "job_id": "daily_summary",
        "name": "Daily Summary",
        "description": "Generate or update today's P&L record in the database.",
        "schedule": "Daily 18:00 · Mon–Fri",
        "danger": "safe",
        "catch_up": True,
    },
    {
        "action": "weekly_summary",
        "job_id": "weekly_summary",
        "name": "Weekly Summary",
        "description": "Generate per-epic direction summaries for the current week.",
        "schedule": "Friday 18:30",
        "danger": "safe",
        "catch_up": True,
    },
    {
        "action": "daily_reset",
        "job_id": "daily_reset",
        "name": "Daily Reset",
        "description": "Clear the price buffer — all in-memory candle history is lost.",
        "schedule": "Daily 00:00",
        "danger": "warn",
    },
    {
        "action": "dump_and_purge_candles",
        "job_id": "dump_and_purge_candles",
        "name": "Dump & Purge Candles",
        "description": (
            "Export candles past the retention window to CSV, then delete them."
        ),
        "schedule": "Daily 02:00",
        "danger": "warn",
        "catch_up": True,
    },
]


class BotScheduler:
    """Manages all scheduled tasks for the trading bot.

    Replaces the PHP CRON-based architecture with in-process scheduling.

    Epic list lifecycle:
    - ``_all_epics``: full deduplicated list discovered from IG market search
      (term-based + watchlists), refreshed once daily at 07:30.
    - ``_tradable_epics``: subset of ``_all_epics`` that are currently open and
      TRADEABLE, refreshed hourly during market hours. No spread filter is
      applied here — the spread is checked later at analysis time.
    - The analysis loop uses ``_tradable_epics`` only.
    """

    def __init__(
        self,
        settings: Settings,
        client: IGClient | APIQueue,
        buffer: PriceBuffer,
        market_data: MarketDataService,
        recorder: Recorder,
        epics: list[str],
        session_factory: async_sessionmaker[AsyncSession],
        candle_store: CandleStore | None = None,
        streaming: IGStreamingClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._buffer = buffer
        self._market_data = market_data
        self._recorder = recorder
        self._session_factory = session_factory
        self._candle_store = candle_store
        self._streaming = streaming
        self._scheduler = AsyncIOScheduler()
        self._running = False
        self._paused = False
        # Entry strategy (open) and close profile (exit) — decoupled and chosen
        # independently (OPEN_STRATEGY / the three CLOSE_ZONE* selectors). Built
        # lazily so a scheduler constructed with stub settings (tests) never
        # resolves them.
        self._strategy: EntryStrategy | None = None
        self._close_profile_obj: CloseProfile | None = None
        # Serialises the rolling cross-epic selection: it is invoked both by the
        # 30-second analysis loop and the hourly backstop job, which could
        # otherwise both see "0 open" and double-open. The lock + an in-band
        # open-count re-check keep the target position count exact.
        self._select_lock = asyncio.Lock()
        # Per-epic open lock: serialises the "is this epic already open?" gate
        # and the order placement so two concurrent callers (the analysis tick,
        # a manual dashboard open, the rolling selector — or the same dashboard
        # open fired from two browser tabs / a double-click) can never both pass
        # the gate before either has committed its provisional row and so place
        # two orders on the same epic. Opens on different epics still run in
        # parallel (one lock each). Bounded by the tradable universe, so the dict
        # never grows without limit.
        self._open_locks: dict[str, asyncio.Lock] = {}

        # Epic lists — start with the provided seed list
        self._all_epics: list[str] = list(epics)
        self._tradable_epics: list[str] = list(epics)
        self._tradable_markets: list[MarketInfo] = []
        self._tradable_last_refresh: datetime | None = None
        self._epic_last_refresh: datetime | None = None
        # UTC timestamp of the last successful open-position sync (the moment the
        # stored ``euro`` figures were last refreshed from IG). Surfaced on the
        # dashboard so the displayed P&L carries its "as of" time.
        self._positions_synced_at: datetime | None = None
        # Epics that returned 403 on /prices — skipped until next hourly refresh
        self._pricing_blacklist: set[str] = set()

        self._scanner = MarketScanner(
            client=client,
            settings=settings,
        )

    def start(self) -> None:
        """Start the scheduler with all configured jobs."""
        # Daily epic discovery at 07:30 (before market open)
        self._scheduler.add_job(
            self._refresh_epic_list,
            "cron",
            day_of_week="mon-fri",
            hour=7,
            minute=30,
            id="refresh_epic_list",
            name="Daily epic list refresh (market search)",
        )

        # Hourly tradable-epic refresh during market hours
        self._scheduler.add_job(
            self._refresh_tradable_epics,
            "cron",
            day_of_week="mon-fri",
            hour="8-17",
            minute=0,
            id="refresh_tradable_epics",
            name="Hourly tradable epic refresh (open/TRADEABLE filter)",
        )

        # Main analysis loop: every 30 seconds during market hours
        self._scheduler.add_job(
            self._collect_and_analyze,
            "cron",
            day_of_week="mon-fri",
            hour="8-17",
            second="*/30",
            id="collect_analyze",
            name="Collect prices and analyze",
        )

        # Hourly cross-epic selection: kept registered but currently a no-op
        # (see ``_hourly_trend_select``) — the reference entry opens per-epic.
        # The slot is retained so a future cross-epic entry strategy can restore
        # hourly selection without changing the schedule.
        self._scheduler.add_job(
            self._hourly_trend_select,
            "cron",
            day_of_week="mon-fri",
            hour="9-16",
            minute=0,
            id="trend_select",
            name="Hourly trend-template selection",
        )

        # Position monitoring: every 30 seconds
        self._scheduler.add_job(
            self._monitor_positions,
            "cron",
            day_of_week="mon-fri",
            hour="8-18",
            second="15,45",
            id="monitor_positions",
            name="Monitor open positions",
        )

        # Position sync: every 20 seconds — reconcile DB against IG's live list.
        # A single GET /positions call (cheap, well under the rate-limit guard)
        # keeps euro/bid fresh, catches positions closed outside the bot, and
        # adopts any live IG position the DB is not tracking. No hour/day
        # restriction: CFD/forex positions can be open (and closed by broker-side
        # stops) outside index-market hours and over weekends. The call runs every
        # tick even with an empty DB — that is precisely how an untracked position
        # at the broker gets noticed and adopted rather than silently eating margin.
        self._scheduler.add_job(
            self._sync_positions,
            "cron",
            second="*/20",
            id="sync_positions",
            name="Sync open positions with IG",
        )

        # End of day: force close + summary
        self._scheduler.add_job(
            self._end_of_day,
            "cron",
            day_of_week="mon-fri",
            hour=DEFAULT_MARKET_CLOSE_HOUR_UTC,
            minute=30,
            id="end_of_day",
            name="End of day close",
        )

        # Realized P&L reconciliation: pull IG's transaction history and overwrite
        # today's closed-position euro/levels with the broker's true figures.
        # Runs every 10 min during/after market hours so externally-closed
        # positions are corrected without waiting for the daily summary.
        self._scheduler.add_job(
            self._reconcile_pnl,
            "cron",
            day_of_week="mon-fri",
            hour="9-19",
            minute="*/10",
            id="reconcile_pnl",
            name="Reconcile realized P&L with IG",
        )

        # Daily summary at 18:00
        self._scheduler.add_job(
            self._daily_summary,
            "cron",
            day_of_week="mon-fri",
            hour=18,
            minute=0,
            id="daily_summary",
            name="Daily summary",
        )

        # Weekly summary: Friday 18:30
        self._scheduler.add_job(
            self._weekly_summary,
            "cron",
            day_of_week="fri",
            hour=18,
            minute=30,
            id="weekly_summary",
            name="Weekly summary",
        )

        # Buffer reset at midnight
        self._scheduler.add_job(
            self._daily_reset,
            "cron",
            hour=0,
            minute=0,
            id="daily_reset",
            name="Daily buffer reset",
        )

        # Candle retention: dump + purge old candles nightly (off-market hours)
        self._scheduler.add_job(
            self._dump_and_purge_candles,
            "cron",
            hour=2,
            minute=0,
            id="dump_and_purge_candles",
            name="Dump and purge old candles",
        )

        # Trigger an immediate epic discovery on startup so we don't wait
        # until 07:30 the next day when the bot is launched mid-session.
        self._scheduler.add_job(
            self._refresh_epic_list,
            "date",
            run_date=datetime.now(UTC) + timedelta(seconds=15),
            id="startup_epic_refresh",
            name="Epic list refresh on startup",
        )

        # Refresh the tradable set shortly after the crawl so streaming seeds and
        # subscribes immediately (instead of waiting for the next hour boundary).
        self._scheduler.add_job(
            self._refresh_tradable_epics,
            "date",
            run_date=datetime.now(UTC) + timedelta(seconds=30),
            id="startup_tradable_refresh",
            name="Tradable epic refresh on startup",
        )

        self._scheduler.start()
        self._running = True
        # Start every job in manual mode (individually paused) — the user enables
        # jobs one by one from the dashboard "Actions" section. Pausing per-job
        # (rather than the whole scheduler) is what allows mixing automatic and
        # manual jobs afterwards.
        for job in self._scheduler.get_jobs():
            job.pause()
        self._paused = True
        logger.info(
            "Scheduler started — all jobs in manual mode — %d seed epics — "
            "enable jobs via web dashboard",
            len(self._all_epics),
        )

    def stop(self) -> None:
        """Stop the scheduler."""
        if self._running:
            self._scheduler.shutdown(wait=False)
            self._running = False
            logger.info("Scheduler stopped")

    @property
    def is_running(self) -> bool:
        """Check if the scheduler is active."""
        return self._running

    @property
    def is_paused(self) -> bool:
        """Return True when no registered job is currently active (all manual)."""
        if not self._running:
            return True
        return not any(
            (job := self._scheduler.get_job(entry["job_id"])) is not None
            and job.next_run_time is not None
            for entry in JOB_DEFINITIONS
        )

    def jobs_status(self) -> list[dict]:
        """Return every registered job with its current auto/manual mode.

        ``auto`` is True when the APScheduler job has a pending next run time; a
        paused (manual) job reports ``next_run_time is None``. The end-of-day
        schedule label is derived from the configured close hour.
        """
        statuses: list[dict] = []
        for entry in JOB_DEFINITIONS:
            job = self._scheduler.get_job(entry["job_id"]) if self._running else None
            schedule = entry["schedule"]
            if entry["action"] == "end_of_day":
                schedule = f"Daily {DEFAULT_MARKET_CLOSE_HOUR_UTC}:30 · Mon–Fri"
            statuses.append(
                {
                    "action": entry["action"],
                    "name": entry["name"],
                    "description": entry["description"],
                    "schedule": schedule,
                    "danger": entry["danger"],
                    "auto": bool(job and job.next_run_time is not None),
                }
            )
        return statuses

    async def set_job_mode(self, action: str, auto: bool) -> bool:
        """Switch a single job between automatic (active) and manual (paused).

        Persists the choice to the database so the mode survives restarts.
        Returns False when the scheduler is not running or the action is unknown.
        """
        if not self._running:
            return False
        entry = next((e for e in JOB_DEFINITIONS if e["action"] == action), None)
        if entry is None:
            return False
        job = self._scheduler.get_job(entry["job_id"])
        if job is None:
            return False
        if auto:
            job.resume()
            logger.info("Job '%s' switched to automatic", action)
        else:
            job.pause()
            logger.info("Job '%s' switched to manual", action)
        await self._save_job_preference(action, auto)
        return True

    async def pause_bot(self) -> None:
        """Switch every registered job to manual mode (all paused)."""
        if not self._running:
            return
        for entry in JOB_DEFINITIONS:
            job = self._scheduler.get_job(entry["job_id"])
            if job:
                job.pause()
        self._paused = True
        await self._save_all_job_preferences(auto=False)
        logger.info("All jobs switched to manual (paused)")

    async def resume_bot(self) -> None:
        """Switch every registered job to automatic mode (all active)."""
        if not self._running:
            return
        for entry in JOB_DEFINITIONS:
            job = self._scheduler.get_job(entry["job_id"])
            if job:
                job.resume()
        self._paused = False
        await self._save_all_job_preferences(auto=True)
        logger.info("All jobs switched to automatic (active)")

    async def load_job_preferences(self) -> None:
        """Restore the last-saved auto/manual mode for each registered job.

        Called once on startup (after ``start()``) so the bot resumes with the
        same job configuration the user had before the server was stopped.
        Jobs with no persisted preference stay in manual (the startup default).
        """
        try:
            async with self._session_factory() as session:
                rows = await session.scalars(select(JobPreference))
                prefs = {row.action: row.auto for row in rows}
        except Exception as exc:
            logger.warning("Could not load job preferences: %s", exc)
            return

        for entry in JOB_DEFINITIONS:
            action = entry["action"]
            if action not in prefs:
                continue
            job = self._scheduler.get_job(entry["job_id"])
            if job is None:
                continue
            if prefs[action]:
                job.resume()
                logger.debug("Job '%s' restored to automatic", action)
            else:
                job.pause()

        active = sum(1 for a, v in prefs.items() if v)
        logger.info(
            "Job preferences restored: %d/%d automatic", active, len(JOB_DEFINITIONS)
        )

    async def _save_job_preference(self, action: str, auto: bool) -> None:
        """Upsert a single job preference row."""
        now = datetime.now(UTC)
        try:
            async with self._session_factory() as session:
                existing = await session.get(JobPreference, action)
                if existing:
                    existing.auto = auto
                    existing.updated_at = now
                else:
                    session.add(JobPreference(action=action, auto=auto, updated_at=now))
                await session.commit()
        except Exception as exc:
            logger.error("Failed to save job preference for '%s': %s", action, exc)

    async def _save_all_job_preferences(self, auto: bool) -> None:
        """Overwrite every registered job preference with the same mode."""
        now = datetime.now(UTC)
        try:
            async with self._session_factory() as session:
                for entry in JOB_DEFINITIONS:
                    existing = await session.get(JobPreference, entry["action"])
                    if existing:
                        existing.auto = auto
                        existing.updated_at = now
                    else:
                        session.add(
                            JobPreference(
                                action=entry["action"], auto=auto, updated_at=now
                            )
                        )
                await session.commit()
        except Exception as exc:
            logger.error("Failed to save all job preferences: %s", exc)

    # ------------------------------------------------------------------
    # Missed-run catch-up (server started after a fixed-time job's slot)
    # ------------------------------------------------------------------

    @staticmethod
    def _last_scheduled_fire(
        trigger, now: datetime, lookback_days: int = 8
    ) -> datetime | None:
        """Return the most recent scheduled fire time at or before ``now``.

        Walks the APScheduler ``trigger`` forward from ``lookback_days`` ago up to
        ``now`` and returns the last fire that should have happened. ``None`` if the
        trigger fires nowhere in that window. The 8-day window covers weekly
        triggers; daily triggers simply yield yesterday's (or today's) slot.
        """
        window_start = now - timedelta(days=lookback_days)
        fire = trigger.get_next_fire_time(None, window_start)
        last: datetime | None = None
        while fire is not None and fire <= now:
            last = fire
            # Passing (fire, fire) advances strictly past the current fire.
            fire = trigger.get_next_fire_time(fire, fire)
        return last

    async def run_catch_up(self) -> None:
        """Replay fixed-time jobs whose scheduled slot was missed while down.

        APScheduler's in-memory jobstore keeps no record of fires that should have
        happened while the process was stopped, so a server started after (say)
        07:30 never runs that day's epic refresh. For each ``catch_up``-eligible
        job the user has left in automatic mode, this compares the most recent
        scheduled fire time with the last successful run persisted in
        ``job_preference``; if a slot was missed, it runs the job once now.

        Manual (paused) jobs are skipped — a disabled job is not silently run.
        Called once on startup, after ``load_job_preferences``.
        """
        if not self._running:
            return
        now = datetime.now(UTC)
        try:
            async with self._session_factory() as session:
                rows = await session.scalars(select(JobPreference))
                last_runs = {row.action: row.last_run_at for row in rows}
        except Exception as exc:
            logger.warning("Could not load job run history for catch-up: %s", exc)
            return

        replayed = 0
        for entry in JOB_DEFINITIONS:
            if not entry.get("catch_up"):
                continue
            action = entry["action"]
            job = self._scheduler.get_job(entry["job_id"])
            # Only catch up jobs the user has enabled (automatic mode has a
            # pending next_run_time; a paused/manual job reports None).
            if job is None or job.next_run_time is None:
                continue
            scheduled = self._last_scheduled_fire(job.trigger, now)
            if scheduled is None:
                continue
            last_run = last_runs.get(action)
            # last_run is always persisted in UTC; some drivers (SQLite) return it
            # naive, so coerce before comparing with the tz-aware fire time.
            if last_run is not None and last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=UTC)
            if last_run is not None and last_run >= scheduled:
                continue  # already ran on or after the last scheduled slot
            logger.info(
                "Catch-up: '%s' missed its %s slot (last run: %s) — running now",
                action,
                scheduled.isoformat(),
                last_run.isoformat() if last_run else "never",
            )
            try:
                await getattr(self, f"trigger_{action}")()
                replayed += 1
            except Exception as exc:
                logger.error("Catch-up run for '%s' failed: %s", action, exc)

        if replayed:
            logger.info("Catch-up complete: %d missed job(s) replayed", replayed)

    async def _record_job_run(self, action: str) -> None:
        """Persist the last successful-run time for a catch-up-eligible job.

        Called at the end of each eligible job (scheduled or manually triggered)
        so ``run_catch_up`` can tell a missed slot from one already covered.
        """
        now = datetime.now(UTC)
        try:
            async with self._session_factory() as session:
                existing = await session.get(JobPreference, action)
                if existing:
                    existing.last_run_at = now
                else:
                    # No mode preference saved yet: create the row in manual mode
                    # (the startup default) and only stamp the run time.
                    session.add(
                        JobPreference(
                            action=action, auto=False, updated_at=now, last_run_at=now
                        )
                    )
                await session.commit()
        except Exception as exc:
            logger.error("Failed to record job run for '%s': %s", action, exc)

    # ------------------------------------------------------------------
    # Manual trigger methods (called from web dashboard)
    # ------------------------------------------------------------------

    async def trigger_refresh_epic_list(self) -> None:
        """Manually run the daily epic list refresh."""
        await self._refresh_epic_list()

    async def trigger_refresh_tradable_epics(self) -> None:
        """Manually run the hourly tradable epic filter."""
        await self._refresh_tradable_epics()

    async def trigger_collect_and_analyze(self) -> None:
        """Manually run one collect-and-analyze cycle."""
        await self._collect_and_analyze()

    async def trigger_trend_select(self) -> None:
        """Manually run one hourly trend-template selection pass."""
        await self._hourly_trend_select()

    async def trigger_monitor_positions(self) -> None:
        """Manually run one position monitoring pass."""
        await self._monitor_positions()

    async def trigger_sync_positions(self) -> None:
        """Manually run one position sync (reconcile DB against IG's live list)."""
        await self._sync_positions()

    async def trigger_end_of_day(self) -> None:
        """Manually trigger end-of-day force close."""
        await self._end_of_day()

    async def trigger_reconcile_pnl(self) -> None:
        """Manually reconcile today's realized P&L against IG's history."""
        await self._reconcile_pnl()

    async def trigger_daily_summary(self) -> None:
        """Manually generate today's P&L summary."""
        await self._daily_summary()

    async def trigger_weekly_summary(self) -> None:
        """Manually generate per-epic weekly summaries."""
        await self._weekly_summary()

    async def trigger_daily_reset(self) -> None:
        """Manually clear the price buffer."""
        await self._daily_reset()

    async def trigger_dump_and_purge_candles(self) -> None:
        """Manually dump + purge candles older than the retention window."""
        await self._dump_and_purge_candles()

    @property
    def tradable_epics(self) -> list[str]:
        """Currently open/TRADEABLE epics used by the analysis loop."""
        return list(self._tradable_epics)

    @property
    def tradable_markets(self) -> list[MarketInfo]:
        """Market snapshots from the last tradable-epic refresh (for display)."""
        return list(self._tradable_markets)

    @property
    def tradable_last_refresh(self) -> datetime | None:
        """UTC timestamp of the last successful tradable-epic refresh."""
        return self._tradable_last_refresh

    @property
    def all_epics(self) -> list[str]:
        """Full deduplicated epic list from the last market-search discovery."""
        return list(self._all_epics)

    @property
    def epic_last_refresh(self) -> datetime | None:
        """UTC timestamp of the last successful epic-list discovery."""
        return self._epic_last_refresh

    @property
    def positions_synced_at(self) -> datetime | None:
        """UTC timestamp of the last successful open-position sync from IG."""
        return self._positions_synced_at

    # ------------------------------------------------------------------
    # Epic list persistence (survives restarts)
    # ------------------------------------------------------------------

    async def load_persisted_state(self) -> None:
        """Restore the epic list, tradable subset, and price buffer from the database.

        Epic discovery is expensive and runs at most once per day, so its
        result is persisted to the ``epic`` table. Loading it here means a
        restart keeps the day's discovered epics instead of falling back to the
        small seed list. ``_epic_last_refresh`` is restored from the newest
        ``updated_at`` so the dashboard KPI still reflects freshness.

        The tradable subset (``is_tradable=True``) is also restored so the
        streaming subscriptions and analysis loop have a valid epic set immediately
        on startup, without waiting for the +30 s ``startup_tradable_refresh`` job.

        Finally, today's candles are loaded from the ``candle`` table back into the
        ``PriceBuffer`` so the indicator pipeline is not blind on restart — it
        resumes with the same history it had before the stop.
        """
        try:
            async with self._session_factory() as session:
                names = list(
                    (await session.scalars(select(Epic.name).order_by(Epic.name))).all()
                )
                last = (
                    await session.scalars(select(func.max(Epic.updated_at)))
                ).one_or_none()
                tradable = list(
                    (
                        await session.scalars(
                            select(Epic.name)
                            .where(Epic.is_tradable.is_(True))
                            .order_by(Epic.name)
                        )
                    ).all()
                )
        except Exception as exc:
            logger.warning("Could not load persisted epic list: %s", exc)
            return

        if names:
            self._all_epics = names
            self._epic_last_refresh = last
            logger.info(
                "Restored %d epics from database (last refresh: %s)",
                len(names),
                last.isoformat() if last else "unknown",
            )

        if tradable:
            self._tradable_epics = tradable
            # Treat the restore time as the effective refresh so the KPI card turns
            # green ("Today HH:MM") instead of red ("Not refreshed") on startup.
            self._tradable_last_refresh = datetime.now(UTC)
            logger.info("Restored %d tradable epics from database", len(tradable))

        if tradable and self._candle_store is not None:
            await self._rehydrate_buffer(tradable)

        # Re-establish the Lightstreamer subscriptions from the persisted set so
        # the feed resumes immediately on restart. Without this, the client stays
        # connected with zero subscriptions until the user manually enables the
        # hourly refresh job — leaving large gaps in candle history. This does not
        # depend on the scheduler's job state (the one-shot startup refresh job is
        # paused by the manual-mode default and may never run).
        if tradable and self._streaming is not None:
            await self._streaming.set_epics(tradable)
            logger.info(
                "Streaming: re-subscribed %d persisted tradable epics on startup",
                len(tradable),
            )

    async def _persist_epic_list(
        self, epics: list[str], refreshed_at: datetime
    ) -> None:
        """Replace the persisted epic list with the latest crawl result.

        Existing rows are pruned and the new list inserted so the table always
        mirrors the current day's epic discovery. Enrichment columns
        (name/funds) are populated separately by ``_persist_epic_enrichment``
        during the hourly tradable refresh.
        """
        try:
            async with self._session_factory() as session:
                await session.execute(delete(Epic).where(Epic.name.notin_(epics)))
                existing = set(
                    (
                        await session.scalars(
                            select(Epic.name).where(Epic.name.in_(epics))
                        )
                    ).all()
                )
                for name in epics:
                    if name in existing:
                        await session.execute(
                            Epic.__table__.update()
                            .where(Epic.name == name)
                            .values(updated_at=refreshed_at)
                        )
                    else:
                        session.add(Epic(name=name, updated_at=refreshed_at))
                await session.commit()
            logger.info("Persisted %d epics to database", len(epics))
        except Exception as exc:
            logger.error("Failed to persist epic list: %s", exc)

    async def _persist_epic_enrichment(self, infos: list) -> None:
        """Store the human name and funds-needed for each fetched epic.

        Reuses the existing ``Epic.description`` (instrument name) and
        ``Epic.deposit`` (margin in EUR to open one minimum-size BUY) columns,
        plus ``Epic.stop_loss`` (EUR loss if that BUY is stopped out) and
        ``Epic.market_close_utc`` (today's UTC close from IG's openingHours, used
        by the per-epic close rule), so the Epic List modal can show real data.
        Only updates rows that already exist (created by ``_persist_epic_list``);
        a missing figure is stored as NULL so the UI renders it as unknown.
        """
        if not infos:
            return
        try:
            async with self._session_factory() as session:
                for info in infos:
                    funds = (
                        round(float(info.funds_needed), 3)
                        if info.funds_needed is not None
                        else None
                    )
                    stop_loss = (
                        round(float(info.stop_loss_eur), 3)
                        if info.stop_loss_eur is not None
                        else None
                    )
                    await session.execute(
                        Epic.__table__.update()
                        .where(Epic.name == info.epic)
                        .values(
                            description=info.name or None,
                            deposit=funds,
                            stop_loss=stop_loss,
                            market_close_utc=getattr(info, "market_close_utc", None),
                        )
                    )
                await session.commit()
            logger.debug("Persisted enrichment for %d epics", len(infos))
        except Exception as exc:
            logger.error("Failed to persist epic enrichment: %s", exc)

    async def _persist_tradable_flags(
        self,
        tradable_epics: list[str],
        reasons: dict[str, str] | None = None,
    ) -> None:
        """Mark which epics are currently tradable in the database.

        Resets all ``is_tradable`` flags to False and clears reasons, then sets
        True only for the given subset. When ``reasons`` is provided, each
        non-tradable epic gets its exclusion reason persisted so the Epic List
        modal can show it.
        """
        try:
            async with self._session_factory() as session:
                await session.execute(
                    Epic.__table__.update().values(
                        is_tradable=False, not_tradable_reason=None
                    )
                )
                if tradable_epics:
                    await session.execute(
                        Epic.__table__.update()
                        .where(Epic.name.in_(tradable_epics))
                        .values(is_tradable=True)
                    )
                if reasons:
                    for epic_name, reason in reasons.items():
                        await session.execute(
                            Epic.__table__.update()
                            .where(Epic.name == epic_name)
                            .values(not_tradable_reason=reason)
                        )
                await session.commit()
            logger.debug("Persisted tradable flags for %d epics", len(tradable_epics))
        except Exception as exc:
            logger.error("Failed to persist tradable flags: %s", exc)

    async def _rehydrate_buffer(self, epics: list[str]) -> None:
        """Load today's candles from the DB into the price buffer.

        Avoids a cold-start after a restart: the indicator pipeline resumes with
        the same intra-day history it had before the server stopped, without any
        extra IG API calls.  Only candles from today (midnight UTC) are loaded to
        match the scope of the daily buffer reset in ``_daily_reset``.
        """
        if self._candle_store is None:
            return

        today_midnight = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        max_candles = DEFAULT_MAX_CANDLES
        loaded = 0
        for epic in epics:
            try:
                candles = await self._candle_store.fetch_candles(
                    epic, since=today_midnight
                )
                if candles:
                    self._buffer.update(epic, candles[-max_candles:])
                    loaded += 1
            except Exception as exc:
                logger.warning("Buffer rehydration failed for %s: %s", epic, exc)
        if loaded:
            logger.info(
                "Buffer rehydrated from DB: %d / %d epics had candles for today",
                loaded,
                len(epics),
            )

    def _build_trade_config(self) -> TradeConfig:
        """Create TradeConfig from current settings."""
        return TradeConfig.from_settings(self._settings)

    @property
    def strategy(self) -> EntryStrategy:
        """The entry strategy selected by ``OPEN_STRATEGY`` (built once)."""
        if self._strategy is None:
            self._strategy = get_entry_strategy(
                self._settings.open_strategy, self._settings
            )
            logger.info(
                "Entry strategy plugged in: '%s' (warmup=%d candles)",
                self._strategy.name,
                self._strategy.warmup,
            )
        return self._strategy

    @property
    def close_profile(self) -> CloseProfile:
        """The close profile composed from the three zone selectors (built once).

        Independent of the entry strategy: it owns every exit decision for the
        positions opened this session. Each zone (``CLOSE_ZONESTART`` /
        ``CLOSE_ZONEMARGE`` / ``CLOSE_ZONEPROFIT``) is selected independently.
        """
        if self._close_profile_obj is None:
            self._close_profile_obj = get_close_profile(self._settings)
            logger.info(
                "Close profile plugged in: '%s' (zones: start=%s margin=%s profit=%s)",
                self._close_profile_obj.name,
                self._settings.close_zonestart,
                self._settings.close_zonemarge,
                self._settings.close_zoneprofit,
            )
        return self._close_profile_obj

    # The active selection is read straight from settings (``.env`` is the single
    # source of truth): there is no runtime switching or database persistence, so
    # these are plain read-only views used by the dashboard header.
    @property
    def active_strategy_name(self) -> str:
        """Name of the selected entry (open) strategy."""
        return self._settings.open_strategy

    @property
    def active_close_zone_names(self) -> tuple[str, str, str]:
        """The three selected per-zone updater names (start, margin, profit)."""
        return (
            self._settings.close_zonestart,
            self._settings.close_zonemarge,
            self._settings.close_zoneprofit,
        )

    @property
    def active_close_profile_name(self) -> str:
        """Compact ``start/margin/profit`` label of the active close zones."""
        return "/".join(self.active_close_zone_names)

    @property
    def active_stop_distance_name(self) -> str:
        """Name of the selected stop policy."""
        return self._settings.stop_strategy

    # ------------------------------------------------------------------
    # Epic list management
    # ------------------------------------------------------------------

    async def _refresh_epic_list(self) -> None:
        """Discover the full epic list from IG and rebuild ``_all_epics``.

        Runs at 07:30 daily. Candidates come from the scanner (search terms +
        watchlists), already narrowed to the configured asset classes — the
        scanner drops off-class instruments (chiefly SHARES with ``.CASH`` epics
        surfaced by broad commodity/index terms) at the source via each result's
        ``instrumentType``, so the Epic List never lists them. Results are stored
        in ``_all_epics``; the tradable list is refreshed separately via
        ``_refresh_tradable_epics`` at 08:00.
        """
        logger.info("Starting daily epic list refresh")
        try:
            epics = await self._scanner.get_tradeable_epics()
        except Exception as exc:
            logger.error("Epic list refresh failed: %s", exc)
            return

        if not epics:
            logger.warning("Epic discovery returned 0 epics — keeping current list")
            return

        self._all_epics = epics
        self._epic_last_refresh = datetime.now(UTC)
        await self._persist_epic_list(epics, self._epic_last_refresh)
        logger.info("Epic list refreshed: %d epics discovered", len(epics))
        self._recorder.info(f"Daily epic refresh: {len(epics)} epics")
        await self._record_job_run("refresh_epic_list")

    async def _open_position_epics(self) -> set[str]:
        """Return the epics that currently hold an OPEN position.

        These are pinned onto the streaming feed in ``_refresh_tradable_epics`` so
        a trade's price history (and the bid the position monitor reads) keeps
        recording until it closes — even when the subscription cap would otherwise
        drop the epic. Returns an empty set on any DB error, in which case the feed
        simply falls back to the tradable set.
        """
        try:
            async with self._session_factory() as session:
                rows = await session.scalars(
                    select(Position.epic).where(Position.state == PositionState.OPEN)
                )
                return set(rows.all())
        except Exception as exc:
            logger.warning("Could not load open-position epics for streaming: %s", exc)
            return set()

    @staticmethod
    async def _traded_today_epics(session: AsyncSession) -> set[str]:
        """Epics that already had an opening today (any state, open or closed).

        Used by the rolling cross-epic ranker (:meth:`_select_and_open`) to enforce
        one opening per epic per day: once a market has been *used*, it is excluded
        from re-ranking so the rolling position keeps rotating across epics
        (diversity) instead of re-opening the same rising curve the moment it
        closes. Keyed on ``Position.date`` to match the daily P&L / trade-count
        gates in the execution domain.
        """
        rows = await session.scalars(
            select(Position.epic).where(Position.date == date.today())
        )
        return set(rows.all())

    async def _refresh_tradable_epics(self) -> None:
        """Filter ``_all_epics`` to those currently open and TRADEABLE.

        Runs hourly during market hours. Batch-fetches market details (v1, groups
        of 25) and applies the open/TRADEABLE filter. No spread filter is applied
        here — the spread is checked later at analysis time.

        When streaming is active, subscriptions are updated immediately after the
        filter so the Lightstreamer feed delivers prices for the new set. No
        historical priming is done — prices arrive via the live stream.
        """
        logger.info(
            "Refreshing tradable epics from %d candidates", len(self._all_epics)
        )
        if not self._all_epics:
            logger.warning("No candidate epics — skipping tradable epic refresh")
            return

        try:
            infos = await self._scanner.get_all_market_infos(self._all_epics)
        except Exception as exc:
            logger.error("Tradable epic refresh failed: %s", exc)
            return

        # Enrich the persisted epic list (name + funds needed) from the same
        # batch fetch — the Epic List modal reads these columns from the DB.
        await self._persist_epic_enrichment(infos)

        # Full set passing the open/affordable/dedupe filters, before any
        # streaming-subscription cap. Reasons are computed against THIS set so
        # the only exclusions it explains are genuine ones (closed, no price,
        # too expensive, product-variant duplicate) — never the cap below.
        selected = self._scanner.select_tradable(infos)
        tradeable = selected

        # IG caps Lightstreamer at 40 subscriptions per connection. When streaming
        # is active and more markets are tradable than fit, pick a subset balanced
        # across asset classes (indices / forex / commodities) rather than the
        # globally tightest spreads — which would otherwise fill every slot with
        # FX pairs and starve the other classes.
        capped_out: list[str] = []
        if (
            self._streaming is not None
            and len(selected) > self._settings.streaming_max_epics
        ):
            tradeable = self._scanner.select_diversified_subset(
                selected, self._settings.streaming_max_epics
            )
            kept = {m.epic for m in tradeable}
            capped_out = [m.epic for m in selected if m.epic not in kept]
            mix: dict[str, int] = {}
            for m in tradeable:
                cls = m.instrument_type.upper() or "OTHER"
                mix[cls] = mix.get(cls, 0) + 1
            breakdown = ", ".join(f"{k}={v}" for k, v in sorted(mix.items()))
            logger.info(
                "Tradable epics capped to %d (diversified across classes: %s) — "
                "dropped %d over the IG streaming limit",
                self._settings.streaming_max_epics,
                breakdown,
                len(capped_out),
            )

        self._tradable_markets = tradeable
        self._tradable_epics = [m.epic for m in tradeable]
        self._tradable_last_refresh = datetime.now(UTC)
        self._pricing_blacklist.clear()
        logger.info(
            "Tradable epics: %d / %d are open/TRADEABLE (pricing blacklist cleared)",
            len(self._tradable_epics),
            len(self._all_epics),
        )

        # Epics holding an open position must keep their live feed until the trade
        # closes, so the position's candle history (and the bid the monitor reads)
        # never stops mid-trade — even if the spread/diversity cap would otherwise
        # drop them. They are streamed regardless of the cap below.
        open_epics = await self._open_position_epics()

        # Safety net for the per-epic close rule: if an open position's market is
        # no longer TRADEABLE (it closed, or went to auction/edits-only), force it
        # shut so a trade can't be stranded past its market's close. Only act on a
        # status we actually observed this refresh (unknown epics are left alone).
        status_by_epic = {m.epic: m.status for m in infos}
        not_tradeable_open = {
            e for e in open_epics if status_by_epic.get(e, "TRADEABLE") != "TRADEABLE"
        }
        if not_tradeable_open:
            logger.warning(
                "Open positions on non-TRADEABLE markets, force-closing: %s",
                ", ".join(sorted(not_tradeable_open)),
            )
            async with self._session_factory() as session:
                trading = TradingService(
                    self._client, session, self._build_trade_config()
                )
                await trading.close_epics(not_tradeable_open, "market_closed")
            open_epics = await self._open_position_epics()

        # Reasons against the pre-cap set, then overlay the streaming-cap reason
        # for markets that were tradable but cut to fit the 40-subscription limit
        # — so the Epic List doesn't mislabel them as "duplicate".
        reasons = self._scanner.get_non_tradable_reasons(infos, selected)
        for epic in capped_out:
            # A capped epic that still holds an open position stays on the feed
            # below, so don't mislabel it as dropped by the cap.
            if epic not in open_epics:
                reasons[epic] = "streaming_cap"
        await self._persist_tradable_flags(self._tradable_epics, reasons)

        if self._streaming is not None:
            # Open-position epics first so they survive ``set_epics``' truncation
            # to the IG subscription cap; the tradable set fills the rest.
            stream_epics = [
                *sorted(open_epics),
                *(e for e in self._tradable_epics if e not in open_epics),
            ]
            await self._streaming.set_epics(stream_epics)

    # ------------------------------------------------------------------
    # Scheduled tasks
    # ------------------------------------------------------------------

    async def _ensure_open_epics_streamed(self) -> None:
        """Watchdog: keep a guaranteed live feed on every open-position epic.

        An open position's chart (and the bid the monitor reads) must never go
        dark mid-trade. Two failure modes are recovered here:

        * the epic dropped off the subscription set entirely — re-subscribe it;
        * the subscription is still registered but has gone silent (no candle for
          longer than ``streaming_stale_seconds``, e.g. an expired/stalled
          Lightstreamer subscription) — force a fresh subscription.

        Runs every analysis tick. A just-opened epic with no candle yet is left
        alone (it is subscribed and simply hasn't printed its first bar); age
        tracking takes over once the first candle arrives.
        """
        streaming = self._streaming
        if streaming is None or not streaming.is_connected:
            return
        open_epics = await self._open_position_epics()
        if not open_epics:
            return
        subscribed = set(streaming.subscribed_epics)
        threshold = float(self._settings.streaming_stale_seconds)
        now = datetime.now(UTC)
        for epic in open_epics:
            if epic not in subscribed:
                logger.warning("Streaming: open epic %s missing from feed", epic)
                await streaming.resubscribe(epic)
                continue
            buf = self._buffer.get(epic)
            last = buf.last if buf else None
            if last is None:
                continue  # subscribed but no candle yet — give it time
            age = (now - last.timestamp).total_seconds()
            if age > threshold:
                logger.warning(
                    "Streaming: open epic %s feed stale (%.0fs > %.0fs)",
                    epic,
                    age,
                    threshold,
                )
                await streaming.resubscribe(epic)

    async def _collect_and_analyze(self) -> None:
        """Compute signals for all active epics and open positions on BUY.

        Equivalent to apiGetMarketAndPostOpenClose.php. With streaming enabled the
        price buffer is fed live by the Lightstreamer feed, so this job is pure
        CPU: read the buffer, compute the signal, act. The legacy polling path
        (streaming disabled) fetches prices first via the APIQueue.
        """
        if self._streaming is None:
            await self._collect_and_analyze_polling()
            return

        # Guarantee every open position keeps a live feed before anything else —
        # a stalled subscription must be recovered even if the tradable set is
        # empty this tick.
        await self._ensure_open_epics_streamed()

        config = self._build_trade_config()
        epics = [e for e in self._tradable_epics if e not in self._pricing_blacklist]
        if not epics:
            logger.debug("No tradable epics to analyze")
            return
        # Cross-epic rankers don't open per-epic: the buffer is already live, so
        # just top the rolling portfolio back up to its target position count.
        if self.strategy.cross_epic_selection:
            await self._select_and_open()
            return
        for epic in epics:
            await self._evaluate_epic(epic, config)

    async def _collect_and_analyze_polling(self) -> None:
        """Legacy path: poll /prices, then compute signals (streaming disabled).

        Two-phase design:
          Phase 1 — all price fetches are enqueued at once via asyncio.gather so
                    the APIQueue receives every request immediately and can drain
                    them sequentially under its own rate-limit control.  High-
                    priority calls (position open/close) issued by other coroutines
                    can jump ahead in the queue while price fetches are pending.
          Phase 2 — pure CPU: compute signals and open positions from the buffer.
        """
        config = self._build_trade_config()
        epics = [e for e in self._tradable_epics if e not in self._pricing_blacklist]

        if not epics:
            logger.debug("No tradable epics to analyze")
            return

        # Phase 1 — fill the queue with all price fetches at once.
        async def _fetch(epic: str) -> tuple[str, BaseException | None]:
            try:
                candles = await self._market_data.fetch_latest_candles(
                    epic, "MINUTE", 2
                )
                # Tap the freshly fetched candles into durable storage. This
                # reuses the data already pulled for the buffer — no extra API
                # call — so the chart pages keep a full history per epic.
                if self._candle_store is not None and candles:
                    await self._candle_store.save(epic, candles)
                return epic, None
            except Exception as exc:
                return epic, exc

        results: list[tuple[str, BaseException | None]] = await asyncio.gather(
            *(_fetch(e) for e in epics)
        )

        # Cross-epic rankers don't open per-epic: prices are now in the buffer, so
        # top the rolling portfolio back up to its target position count instead.
        if self.strategy.cross_epic_selection:
            for epic, error in results:
                if isinstance(error, IGAPIError) and error.response.status_code == 403:
                    self._pricing_blacklist.add(epic)
            await self._select_and_open()
            return

        # Phase 2 — compute signals and act; all I/O is already done.
        for epic, error in results:
            if error is not None:
                if isinstance(error, IGAPIError):
                    if error.response.status_code == 403:
                        logger.warning(
                            "Prices 403 for %s (IG code: %s) — "
                            "blacklisted until next hourly refresh",
                            epic,
                            error.ig_error_code or "no errorCode in response",
                        )
                        self._pricing_blacklist.add(epic)
                    else:
                        logger.error(
                            "API error %d for %s — IG code: %s",
                            error.response.status_code,
                            epic,
                            error.ig_error_code or str(error),
                        )
                else:
                    logger.error("Error analyzing %s: %s", epic, error)
                continue

            await self._evaluate_epic(epic, config)

    def _open_lock(self, epic: str) -> asyncio.Lock:
        """Return (creating on first use) the per-epic open lock.

        Safe to build lazily: lock creation is synchronous and the whole stack
        runs on a single event loop, so there is no thread race on the dict.
        """
        lock = self._open_locks.get(epic)
        if lock is None:
            lock = asyncio.Lock()
            self._open_locks[epic] = lock
        return lock

    async def open_epic_guarded(
        self,
        trading: "TradingService",
        intent: EntryIntent,
        buf: EpicBuffer,
    ) -> tuple[Position | None, str | None]:
        """Open ``intent.epic`` under its per-epic lock (single-flight per epic).

        Holds the lock across the duplicate-epic gate **and** the order placement
        so a second concurrent caller (analysis tick, manual dashboard open,
        rolling selector, or the same open fired twice from two tabs) re-runs the
        gate only after the first has committed its provisional row — and is then
        correctly refused instead of placing a second order on the same epic.

        Returns ``(position, None)`` on success, ``(None, reason)`` when the gate
        refuses the open, and ``(None, None)`` when IG rejected the order.
        """
        async with self._open_lock(intent.epic):
            allowed, reason = await trading.can_open_intent(intent)
            if not allowed:
                return None, reason
            position = await trading.open_from_intent(intent, buf)
            return position, None

    async def _evaluate_epic(self, epic: str, config: TradeConfig) -> None:
        """Run the entry strategy on one epic's buffer and open on an intent.

        Decoupled open path: the entry strategy decides only direction; the
        independently chosen close profile decides the stop inside
        ``open_from_intent``. Gates, order placement and monitoring are shared
        whatever the entry/close choice.
        """
        strategy = self.strategy
        # Cross-epic rankers open through the rolling selection routine, not the
        # per-epic loop — otherwise every BUY-scored epic would be opened at once.
        # The buffer is still fed (streaming / polling save) regardless.
        if strategy.cross_epic_selection:
            return
        buf = self._buffer.get(epic)
        if not buf or len(buf) < strategy.warmup:
            return
        intent = strategy.evaluate(epic, buf)
        if intent and intent.direction == "BUY":
            logger.info(
                "Entry intent [%s]: %s %s (score=%.2f)",
                strategy.name,
                intent.direction,
                epic,
                intent.score,
            )
            async with self._session_factory() as session:
                trading = TradingService(
                    self._client, session, config, close_profile=self.close_profile
                )
                position, reason = await self.open_epic_guarded(trading, intent, buf)
                if position:
                    self._recorder.info(
                        f"Position opened: {epic} @ {position.level_open}"
                    )
                elif reason:
                    logger.debug("Cannot open %s: %s", epic, reason)

    async def _hourly_trend_select(self) -> None:
        """Backstop / manual trigger for the rolling cross-epic selection.

        The selection runs continuously in the 30-second analysis loop
        (``_collect_and_analyze``) — that is what re-opens a fresh position the
        moment the previous one closes. This hourly job (and its dashboard *Run*
        button) simply invokes the same guarded routine, so a missed tick or a
        manual trigger still tops the portfolio back up to its target. A no-op
        for per-epic entries.
        """
        await self._select_and_open()

    async def _select_and_open(self) -> None:
        """Maintain the rolling cross-epic portfolio at its target size.

        Active only for a cross-epic ranker entry (``cross_epic_selection``, e.g.
        ``open_ranking``); a no-op otherwise. The selection knobs are
        constants on the strategy class (``concurrent_positions``,
        ``wallet_reserve``), not settings. The goal is to
        stay in the market all day: hold ``concurrent_positions`` open positions
        (default 1, a single rolling position) and re-fill a slot as soon as one
        closes — win or loss.

        Each invocation:

        1. returns early when the target position count is already met (the cheap
           steady state while a position is running);
        2. otherwise scores every tradable epic, ranks the BUY candidates by score
           and opens the best ones that pass the shared open gates **and** the
           wallet check (available balance minus ``wallet_reserve`` must cover the
           epic's margin) until the target is reached.

        There is no wall-clock warm-up: an epic may be opened as soon as its
        market is open (it is in ``_tradable_epics``, filtered to ``TRADEABLE``)
        and it has enough buffered candles (``len(buf) >= strategy.warmup``). The
        candle count is the warm-up — the bid curve must be long enough to score.

        The lock serialises concurrent callers (analysis loop + hourly backstop)
        and the open-count is re-checked inside it, so the target is never
        overshot.
        """
        strategy = self.strategy
        if not strategy.cross_epic_selection:
            return  # per-epic entry strategy drives opens via _collect_and_analyze

        config = self._build_trade_config()

        epics = [e for e in self._tradable_epics if e not in self._pricing_blacklist]
        if not epics:
            logger.debug("Rolling select: no tradable epics (after pricing blacklist)")
            return

        async with self._select_lock:
            async with self._session_factory() as session:
                open_count = (
                    await session.scalar(
                        select(func.count())
                        .select_from(Position)
                        .where(Position.state == PositionState.OPEN)
                    )
                ) or 0
                target = max(int(strategy.concurrent_positions), 1)
                slots = target - int(open_count)
                if slots <= 0:
                    logger.debug(
                        "Rolling select: target met (%d/%d open) — holding",
                        open_count,
                        target,
                    )
                    return  # target met — a position is already running

                # Diversity rule: an epic already *used* today (it had an opening,
                # now open or closed) is dropped from the candidate set so the
                # rolling position rotates across markets instead of re-opening the
                # same epic the moment it closes. The shared ``epic_already_open``
                # gate only blocks *concurrent* duplicates, not a same-day re-open.
                traded_today = await self._traded_today_epics(session)
                candidates = [e for e in epics if e not in traded_today]
                if not candidates:
                    logger.debug("Rolling select: every epic already used today")
                    return

                # Score every epic with enough buffered history; keep BUY
                # candidates, then rank by score (highest first).
                ranked: list[tuple[EntryIntent, EpicBuffer]] = []
                for epic in candidates:
                    buf = self._buffer.get(epic)
                    if not buf or len(buf) < strategy.warmup:
                        continue
                    intent = strategy.evaluate(epic, buf)
                    if intent and intent.direction == "BUY":
                        ranked.append((intent, buf))
                if not ranked:
                    logger.debug("Rolling select: no scorable epic yet")
                    return
                ranked.sort(key=lambda item: item[0].score, reverse=True)

                available = await self._account_available_funds()
                reserve = max(0.0, min(1.0, strategy.wallet_reserve))
                spendable = (
                    available * (1.0 - reserve) if available is not None else None
                )
                funds_map = {
                    m.epic: m.funds_needed
                    for m in self._tradable_markets
                    if m.funds_needed is not None
                }

                trading = TradingService(
                    self._client, session, config, close_profile=self.close_profile
                )
                opened = 0
                for intent, buf in ranked:
                    if opened >= slots:
                        break
                    allowed, reason = await trading.can_open_intent(intent)
                    if not allowed:
                        logger.debug("Rolling select skip %s: %s", intent.epic, reason)
                        continue
                    # Wallet gate: only open while the available balance (minus the
                    # reserve) covers this epic's margin. Unknown margin -> allow.
                    need = funds_map.get(intent.epic)
                    if spendable is not None and need is not None and need > spendable:
                        logger.info(
                            "Rolling select skip %s: wallet %.0f€ < margin %.0f€",
                            intent.epic,
                            spendable,
                            need,
                        )
                        continue
                    logger.info(
                        "Rolling select [%s]: opening %s (score=%.3f)",
                        strategy.name,
                        intent.epic,
                        intent.score,
                    )
                    # Single-flight per epic: re-checks the duplicate gate under
                    # the per-epic lock so a manual dashboard open on the same
                    # epic cannot slip a second order through the race window.
                    position, _ = await self.open_epic_guarded(trading, intent, buf)
                    if position:
                        opened += 1
                        if spendable is not None and need is not None:
                            spendable -= need
                        self._recorder.info(
                            f"Rolling open: {intent.epic} @ {position.level_open} "
                            f"(score={intent.score:.3f})"
                        )

                if opened:
                    logger.info("Rolling select: opened %d position(s)", opened)

    async def _account_available_funds(self) -> float | None:
        """Live available balance (EUR) on the trading account, or None.

        The wallet gate for the rolling selector: a new position is opened only
        while the available funds (minus the configured reserve) cover the epic's
        margin. Returns ``None`` when the balance cannot be read, in which case
        the selector falls back to the shared risk gates only.
        """
        try:
            data = await self._client.get(
                "/accounts",
                version=1,
                priority=Priority.HIGH,
                label="rolling select: balance",
            )
        except Exception as exc:
            logger.warning("Rolling select: could not read account balance: %s", exc)
            return None
        for account in data.get("accounts", []):
            if account.get("accountId") == self._settings.ig_account_id:
                available = account.get("balance", {}).get("available")
                return float(available) if available is not None else None
        return None

    async def _monitor_positions(self) -> None:
        """Check open positions and apply close strategy.

        Equivalent to apiCheckPosition.php.
        """
        config = self._build_trade_config()

        async with self._session_factory() as session:
            result = await session.execute(
                select(Position).where(Position.state == PositionState.OPEN)
            )
            positions = result.scalars().all()

            if not positions:
                return

            trading = TradingService(
                self._client, session, config, close_profile=self.close_profile
            )

            for position in positions:
                try:
                    # Get current bid for this epic
                    buf = self._buffer.get(position.epic)
                    if buf and buf.last:
                        current_bid = buf.last.bid_close
                    else:
                        # Fallback: fetch from API
                        market = await self._client.get(
                            f"/markets/{position.epic}", version=3
                        )
                        current_bid = float(market.get("snapshot", {}).get("bid", 0))

                    if current_bid > 0:
                        closed = await trading.manage_position(
                            position, current_bid, buf=buf
                        )
                        if closed:
                            self._recorder.info(
                                f"Position closed: {position.epic} "
                                f"reason={position.reason_close} "
                                f"P&L={position.euro}€"
                            )
                            await self._maybe_open_recovery(
                                trading, session, position, buf
                            )
                except Exception as exc:
                    logger.error("Error monitoring position %s: %s", position.epic, exc)

    async def _maybe_open_recovery(
        self,
        trading: TradingService,
        session: AsyncSession,
        closed_position: Position,
        buf: EpicBuffer | None,
    ) -> None:
        """Open a double-size SELL when a long stopped out on the reversal pattern.

        Gated by ``RECOVERY_ENABLED``. Fires only when the just-closed position
        matches :func:`~src.execution.recovery.is_recovery_trigger` (a quick,
        never-in-profit long stop-out). The open is serialised under the same
        lock as the rolling ranking selector and re-checks that no position is
        open, so the recovery short and a ranker BUY can never both grab the
        single freed slot — the recovery short takes it and, counting as the one
        open position, blocks any further BUY while it runs.
        """
        if not self._settings.recovery_enabled:
            return
        if buf is None or buf.last is None:
            return
        if not is_recovery_trigger(closed_position):
            return

        async with self._select_lock:
            open_count = (
                await session.scalar(
                    select(func.count())
                    .select_from(Position)
                    .where(Position.state == PositionState.OPEN)
                )
            ) or 0
            if open_count > 0:
                logger.info(
                    "Recovery skipped for %s: %d position(s) already open",
                    closed_position.epic,
                    open_count,
                )
                return
            logger.info(
                "Recovery trigger on %s (loss=%s€) — opening double-size SELL",
                closed_position.epic,
                closed_position.euro,
            )
            short = await trading.open_recovery_short(closed_position, buf)
            if short is not None:
                self._recorder.info(
                    f"Recovery short opened: {closed_position.epic} "
                    f"@ {short.level_open} (x{short.quantity})"
                )

    async def _sync_positions(self) -> None:
        """Reconcile DB open positions against IG's live position list.

        Source of truth is a single ``GET /positions`` call. For every position
        the DB still considers OPEN, ``TradingService.sync_open_positions`` refreshes
        the live unrealized P&L (``euro`` / ``euro_max`` / ``euro_min``) from the
        current bid, repairs a stale ``deal_id``, and marks as ``closed_externally``
        any position IG no longer reports (closed by a broker-side stop/limit or
        manually outside the bot). This is what keeps the dashboard in step with the
        broker between strategy passes.
        """
        config = self._build_trade_config()
        async with self._session_factory() as session:
            trading = TradingService(self._client, session, config)
            try:
                live = await trading.sync_open_positions()
            except Exception as exc:
                logger.error("Position sync failed: %s", exc)
                return
        # Stamp the "as of" time only on a successful sync so the dashboard can
        # show when the displayed P&L figures were last refreshed from IG.
        self._positions_synced_at = datetime.now(UTC)
        if live:
            logger.debug("Position sync: %d position(s) live at IG", len(live))

    async def _end_of_day(self) -> None:
        """Force close all positions and generate daily summary."""
        logger.info("End of day: closing all positions")

        config = self._build_trade_config()

        async with self._session_factory() as session:
            trading = TradingService(self._client, session, config)
            closed = await trading.close_all_positions()

        self._recorder.info(f"End of day: {closed} positions force-closed")

        # Replace the just-recorded estimates with IG's authoritative figures.
        await self._reconcile_pnl()

    async def _reconcile_pnl(self) -> None:
        """Reconcile today's realized P&L with IG's authoritative transactions."""
        config = self._build_trade_config()
        async with self._session_factory() as session:
            trading = TradingService(self._client, session, config)
            try:
                updated = await trading.reconcile_realized_pnl()
            except Exception as exc:
                logger.error("Realized P&L reconcile failed: %s", exc)
                return
        if updated:
            logger.info("Realized P&L reconcile: %d position(s) corrected", updated)

    async def _daily_summary(self) -> None:
        """Generate or update daily summary in the Day table."""
        today = date.today()

        # Pull IG's authoritative realized P&L first so the summary totals are
        # computed from the broker's figures, not our intra-day estimates.
        await self._reconcile_pnl()

        async with self._session_factory() as session:
            # Get all closed positions for today
            result = await session.execute(
                select(Position).where(
                    Position.date == today,
                    Position.state == PositionState.CLOSE,
                )
            )
            # Exclude "never_opened" phantoms (provisional rows whose order never
            # confirmed at IG): they are not real trades and must not inflate the
            # day's trade count or P&L. Mirrors the live dashboard aggregation.
            positions = [
                p for p in result.scalars().all() if p.reason_close != "never_opened"
            ]

            euro_total = sum(float(p.euro or 0) for p in positions)
            euro_list = (
                ",".join(f"{p.epic}:{p.euro}" for p in positions) if positions else ""
            )

            # Upsert Day record
            day_result = await session.execute(select(Day).where(Day.date == today))
            day_record = day_result.scalar_one_or_none()

            if day_record:
                day_record.state = DayState.CLOSE
                day_record.euro_total = Decimal(str(round(euro_total, 3)))
                day_record.euro_list = euro_list
            else:
                day_record = Day(
                    date=today,
                    state=DayState.CLOSE,
                    euro_total=Decimal(str(round(euro_total, 3))),
                    euro_list=euro_list,
                )
                session.add(day_record)

            await session.commit()

        logger.info("Daily summary: %d trades, P&L=%.2f€", len(positions), euro_total)
        self._recorder.info(
            f"Daily summary: {len(positions)} trades, P&L={euro_total:.2f}€"
        )
        await self._record_job_run("daily_summary")

    async def _weekly_summary(self) -> None:
        """Generate per-epic direction summaries for the week."""
        today = date.today()
        week_str = today.strftime("%Y-W%W")

        async with self._session_factory() as session:
            for epic in self._all_epics:
                # Count winning BUY signals this week
                result = await session.execute(
                    select(Position).where(
                        Position.epic == epic,
                        Position.state == PositionState.CLOSE,
                    )
                )
                positions = result.scalars().all()
                # Filter to this week
                week_positions = [
                    p
                    for p in positions
                    if p.date and p.date.strftime("%Y-W%W") == week_str
                ]

                if not week_positions:
                    continue

                wins = sum(1 for p in week_positions if (p.win or 0) > 0)
                total = len(week_positions)
                direction = "BUY" if wins > total / 2 else "SELL"

                # Upsert Resume
                res_result = await session.execute(
                    select(Resume).where(
                        Resume.epic == epic,
                        Resume.week == week_str,
                    )
                )
                resume = res_result.scalar_one_or_none()

                if resume:
                    resume.direction = direction
                    resume.day = today
                else:
                    resume = Resume(
                        epic=epic,
                        day=today,
                        week=week_str,
                        direction=direction,
                    )
                    session.add(resume)

            await session.commit()

        logger.info("Weekly summary generated for week %s", week_str)
        await self._record_job_run("weekly_summary")

    async def _daily_reset(self) -> None:
        """Clear the price buffer at midnight for a clean start of the new
        trading day."""
        self._buffer.clear()
        logger.info("Daily reset: price buffer cleared")

    async def _dump_and_purge_candles(self) -> None:
        """Export candles past the retention window to disk, then delete them."""
        if self._candle_store is None:
            return
        try:
            count, paths = await self._candle_store.dump_and_purge()
        except Exception as exc:
            logger.error("Candle dump/purge failed: %s", exc)
            return
        if count:
            names = ", ".join(p.name for p in paths)
            self._recorder.info(f"Candle retention: archived {count} rows to {names}")
        await self._record_job_run("dump_and_purge_candles")
