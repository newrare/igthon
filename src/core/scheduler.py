"""Scheduler service — replaces CRON scripts.

Uses APScheduler to run periodic tasks:
- Price collection and analysis (every 30s-2min)
- End-of-day forced close and summary
- Daily/weekly summaries
- Daily epic list refresh from IG market search (7:30 AM)
- Hourly active epic refresh (filter by TRADEABLE status)

Position monitoring is deliberately NOT one of them: it is driven by the arrival of
candles from the price feed (``on_candle``), because that is the only moment the
price the close profile reads changes. The 30-second cron is kept purely as a
heartbeat for when streaming is off or the feed goes silent. See
``docs/DATAFLOW.md`` §5.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.api.client import IGAPIError, IGClient
from src.core.api_queue import APIQueue, Priority
from src.core.config import Settings
from src.core.reading import MIN_REGIME_MINUTES, CurveReading, Regime
from src.core.recorder import Recorder
from src.core.signals import SignalBook
from src.entry import ENTRY_STRATEGIES, EntryIntent, EntryStrategy, get_entry_strategy
from src.execution.gates import (
    RECOVERY_REVERT_REASON_OPEN,
    curve_supports_revert,
    original_stop_level,
    position_opened_at,
    reverse_direction,
    should_revert_after_stop_loss,
)
from src.execution.trading import (
    TradeConfig,
    TradingService,
)
from src.exit import (
    ZONEMARGE_UPDATERS,
    ZONEPROFIT_UPDATERS,
    ZONESECURE_UPDATERS,
    ZONESTART_UPDATERS,
    CloseProfile,
    get_close_profile,
)
from src.exit.zones.base import DayBook
from src.feed.candle_store import CandleStore
from src.feed.market_data import MarketDataService
from src.feed.price_buffer import DEFAULT_MAX_CANDLES, EpicBuffer, PriceBuffer
from src.markets.catalog_crawler import CatalogCrawler, CatalogProgress
from src.markets.market_scanner import MarketInfo, MarketScanner
from src.markets.risk_tiers import RiskTier, select_by_risk_tier, tier_for_pnl
from src.models.day import Day, DayState
from src.models.epic import Epic
from src.models.job_preference import JobPreference
from src.models.position import Position, PositionState
from src.stops import STOP_DISTANCES

if TYPE_CHECKING:
    from src.feed.streaming import IGStreamingClient

logger = logging.getLogger(__name__)


def validate_strategy_selection(settings: Settings) -> None:
    """Ensure the ``.env`` trading selection is complete and known.

    The ``.env`` file is the single source of truth for the open / stop / close
    selection **and** for the global open policies — there is no code default and
    no persistence. A missing, empty or unknown value raises a single actionable
    error naming every offending variable, so the bot and the dashboard can
    surface a "configure your .env" message instead of failing obscurely deep in
    the pipeline.

    Raises:
        ValueError: when any of ``OPEN_STRATEGY`` / ``STOP_STRATEGY`` /
            ``CLOSE_ZONESTART`` / ``CLOSE_ZONEMARGE`` / ``CLOSE_ZONESECURE`` /
            ``CLOSE_ZONEPROFIT`` is empty or not a registered name, or when
            ``ALLOW_SAME_DAY_REOPEN`` or ``ALLOW_RECOVERY_REVERT`` is missing.
    """
    checks = (
        ("OPEN_STRATEGY", settings.open_strategy, ENTRY_STRATEGIES),
        ("STOP_STRATEGY", settings.stop_strategy, STOP_DISTANCES),
        ("CLOSE_ZONESTART", settings.close_zonestart, ZONESTART_UPDATERS),
        ("CLOSE_ZONEMARGE", settings.close_zonemarge, ZONEMARGE_UPDATERS),
        ("CLOSE_ZONESECURE", settings.close_zonesecure, ZONESECURE_UPDATERS),
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
    # Global same-day re-open policy: a boolean shared by every open strategy, so
    # it must be stated explicitly rather than silently defaulted one way.
    if getattr(settings, "allow_same_day_reopen", None) is None:
        problems.append("ALLOW_SAME_DAY_REOPEN is not set (expected true or false)")
    # Recovery-revert policy: same contract — a rule that opens a position on its
    # own must be stated explicitly, never silently defaulted.
    if getattr(settings, "allow_recovery_revert", None) is None:
        problems.append("ALLOW_RECOVERY_REVERT is not set (expected true or false)")
    # Automatic-open mode: whether a signal becomes an order or a notification.
    # Explicit like the policies above — the difference between the two is
    # whether the bot may spend money on its own.
    if getattr(settings, "open_mode", "") not in OPEN_MODES:
        problems.append(
            f"OPEN_MODE={getattr(settings, 'open_mode', '') or ''!r} is invalid "
            f"(expected one of {sorted(OPEN_MODES)})"
        )
    if problems:
        raise ValueError(
            "Invalid strategy selection — please configure your .env file:\n  - "
            + "\n  - ".join(problems)
        )


@dataclass(frozen=True)
class GroupBookSnapshot:
    """Last whole-book valuation at the planned stops (``smartgroup`` pre-pass).

    What the book would bank if every open position were closed at the stop the
    group pre-pass would give it this tick — the same figure the "Group stop
    plan" log line reports. Kept in memory only (recomputed on every monitoring
    pass) and surfaced on the dashboard next to the live unrealized P&L, which is
    always the more optimistic of the two.

    Attributes:
        total_euro: The book valued at the planned stops, in euros.
        gate_euro: The threshold ``total_euro`` must exceed to arm the tightening.
        armed: True when the gate was cleared and the stops were pushed.
        at: UTC moment the valuation was computed.
    """

    total_euro: float
    gate_euro: float
    armed: bool
    at: datetime


# Reserved ``JobPreference`` key persisting the auto-open switch. Deliberately
# NOT a member of ``JOB_DEFINITIONS``: it is a trading authorisation, not a
# schedulable job, so the "enable/pause all jobs" bulk writes and the per-job
# restore loop never touch it. It shares the table because the row is the same
# shape — a named boolean the user chose, plus the moment they chose it.
AUTO_OPEN_PREF_KEY = "auto_open"

# Refusal reason returned by ``open_epic_guarded`` when the auto-open switch is
# off. Named so the analysis loop can recognise it and report it louder than an
# ordinary gate refusal — the user set this one themselves and wants to see what
# it cost them.
AUTO_OPEN_REFUSAL = "Auto-open disabled for today"

# Accepted ``OPEN_MODE`` values. ``auto`` places the order the entry strategy
# asked for; ``notify`` records it as a signal for the user to act on (or not).
OPEN_MODE_AUTO = "auto"
OPEN_MODE_NOTIFY = "notify"
OPEN_MODES = frozenset({OPEN_MODE_AUTO, OPEN_MODE_NOTIFY})

# Refusal reason returned by ``open_epic_guarded`` in notify mode. The open was
# fully validated (gates passed) and deliberately not taken: the decision is the
# user's, and the dashboard notifies them.
NOTIFY_REFUSAL = "Notify mode — signal raised, waiting for your decision"


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
# (would force-close live positions) is deliberately excluded.
#
# ``toggleable`` (optional, default True) marks jobs that own a recurring cron
# trigger and can therefore be switched between automatic and manual from the
# dashboard. The others have no APScheduler cron at all — ``end_of_day`` is a
# manual button, ``daily_reset`` and ``dump_and_purge_candles`` run once a day on
# startup (see ``_register_startup_jobs``) — so they carry no auto/manual switch:
# only their Run button makes sense.
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
        "action": "crawl_market_catalog",
        "job_id": "crawl_market_catalog",
        "name": "Crawl Market Catalog",
        "description": (
            "Walk IG's market-navigation tree and build the static catalog of "
            "every epic the broker offers. Resumable: a run stopped by the API "
            "quota picks up where it left off on the next Run."
        ),
        "schedule": "Manual only",
        "danger": "safe",
        "toggleable": False,
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
        "action": "monitor_positions",
        "job_id": "monitor_positions",
        "name": "Monitor Positions",
        "description": (
            "Check all open positions and apply the close strategy. Runs on each "
            "candle arrival; the 30s cron is only a heartbeat."
        ),
        "schedule": "On new candle · 30s heartbeat · 24/7",
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
        "description": "Force close ALL open positions immediately (manual only).",
        "schedule": "Manual only",
        "danger": "danger",
        "toggleable": False,
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
        "action": "daily_reset",
        "job_id": "daily_reset",
        "name": "Daily Reset",
        "description": (
            "Drop stale candles from the price buffer, then reload today's "
            "candles from the database. Runs once a day on server startup."
        ),
        "schedule": "On startup · once a day",
        "danger": "warn",
        "toggleable": False,
    },
    {
        "action": "dump_and_purge_candles",
        "job_id": "dump_and_purge_candles",
        "name": "Dump & Purge Candles",
        "description": (
            "Export candles past the retention window to CSV, then delete them. "
            "Runs once a day on server startup."
        ),
        "schedule": "On startup · once a day",
        "danger": "warn",
        "toggleable": False,
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
        # Entry strategy (open) and close profile (exit) — decoupled and chosen
        # independently (OPEN_STRATEGY / the three CLOSE_ZONE* selectors). Built
        # lazily so a scheduler constructed with stub settings (tests) never
        # resolves them.
        self._strategy: EntryStrategy | None = None
        self._close_profile_obj: CloseProfile | None = None
        # Serialises the rolling cross-epic selection: it is invoked both by the
        # 30-second analysis loop and by a manual dashboard open, which could
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
        # Serialises position sync. APScheduler's ``max_instances=1`` only stops
        # two *scheduled* sync jobs overlapping; a manual dashboard "Run" calls
        # ``_sync_positions`` directly and bypasses that. Two concurrent syncs
        # both read ``known_deal_ids`` before either commits, so both adopt the
        # same live IG position → duplicate "adopted" rows (the very bug the
        # idempotent-adoption guard exists to prevent). This lock closes that gap.
        self._sync_lock = asyncio.Lock()
        # Serialises position monitoring. It now has two triggers — the arrival of
        # candles from the feed and the cron heartbeat — so overlapping passes are a
        # real possibility, and two concurrent passes would both read the same open
        # positions and could both push a stop update for them. A pass in flight
        # makes any new trigger a no-op (the data it would read is the data the
        # running pass is already reading), so triggers never queue up.
        self._monitor_lock = asyncio.Lock()
        # Pending debounced monitoring pass scheduled by ``on_candle``. Candles for
        # the streamed universe land together, so the first arrival schedules the
        # pass and the rest of the wave joins it — one whole-book pass per wave,
        # which is also what the group pre-pass requires (see docs/DATAFLOW.md §5).
        self._monitor_debounce_task: asyncio.Task | None = None
        # The feed keeps its listeners for the life of the process, so registration
        # must be idempotent: a second ``start()`` would otherwise subscribe twice
        # and run two passes per wave.
        self._candle_listener_registered = False

        # Epic lists — start with the provided seed list
        self._all_epics: list[str] = list(epics)
        self._tradable_epics: list[str] = list(epics)
        self._tradable_markets: list[MarketInfo] = []
        self._tradable_last_refresh: datetime | None = None
        # Last market snapshot fetched by ``_refresh_tradable_epics``. Kept so the
        # risk tier can re-pick the watched subset intra-day (``_rebuild_tradable_set``)
        # without spending a fresh batch of /markets calls.
        self._market_infos: list[MarketInfo] = []
        # Risk tier the current watched subset was picked for, the open-position
        # epics pinned into it and the already-traded epics followed behind them.
        # All three are the change detector of ``_maybe_reselect_risk_tier``; None
        # means nothing has been picked yet.
        self._risk_tier: RiskTier | None = None
        self._risk_tier_pins: frozenset[str] = frozenset()
        self._risk_tier_follows: tuple[str, ...] = ()
        self._epic_last_refresh: datetime | None = None
        # UTC timestamp of the last successful open-position sync (the moment the
        # stored ``euro`` figures were last refreshed from IG). Surfaced on the
        # dashboard so the displayed P&L carries its "as of" time.
        self._positions_synced_at: datetime | None = None
        # Last whole-book valuation at the planned stops, from the group pre-pass
        # of the monitoring loop. None when there is nothing to value (no open
        # position, no group-aware profile, or a book the pre-pass could not
        # price / a disarmed plan whose partial sum means nothing).
        self._group_book: GroupBookSnapshot | None = None
        # Epics that returned 403 on /prices — skipped until next hourly refresh
        self._pricing_blacklist: set[str] = set()

        # Auto-open switch (the big dashboard button). When False the bot opens
        # NOTHING on its own — every automatic open is refused whatever the
        # selected entry strategy and whatever the direction (BUY or SELL) —
        # while positions already open keep living their normal life (monitoring,
        # stop ratchets, closes). It is a day-scoped decision ("I have decided
        # today is over"): ``_auto_open_day`` records the day the block was set
        # on so the next day starts authorised again with no action needed.
        # Manual dashboard opens are never affected.
        self._auto_open_enabled: bool = True
        self._auto_open_day: date | None = None

        # Proposed opens, in ``OPEN_MODE=notify`` only: an intent that cleared
        # every pre-open gate and was deliberately NOT taken. The dashboard turns
        # each one into a browser notification so the user can go and read the
        # market before opening it themselves. In-memory on purpose — a signal is
        # worth nothing once its market has moved on (see src/core/signals.py).
        self._signals = SignalBook()

        self._scanner = MarketScanner(
            client=client,
            settings=settings,
        )
        # Static market catalog builder (manual action only). It owns its own
        # resume state in the database, so the scheduler just hands it the client
        # and the session factory and never tracks the crawl itself.
        self._catalog_crawler = CatalogCrawler(
            client=client,
            session_factory=session_factory,
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

        # No separate hourly cross-epic selection job: ``_collect_and_analyze``
        # already calls ``_select_and_open`` on every 30-second tick over the same
        # market hours, so an hourly backstop covered nothing the loop had not
        # covered thirty seconds earlier.

        # Position monitoring: HEARTBEAT ONLY. The real trigger is the arrival of a
        # candle from the feed (see ``on_candle`` / ``_register_candle_listener``),
        # because that is the only moment the price the zones read actually changes:
        # a fixed clock offset from the data just adds latency and re-evaluates
        # identical inputs. This cron stays registered so monitoring still runs when
        # streaming is disabled or the feed goes silent, and so the job keeps its
        # dashboard auto/manual toggle. Both paths funnel through ``_monitor_lock``.
        #
        # No hour/day restriction — a position must be watched for the WHOLE time
        # its own market is open, which for CFD/forex and late-closing
        # commodities/indices runs well past 18:00 UTC and across the weekend. The
        # loop self-gates: it returns immediately when there is no open position,
        # and skips any epic without a live bid, so running out of index-market
        # hours is cheap. This is also what lets the per-epic close rule (close
        # ~close_margin before an epic's own market close) fire for markets closing
        # outside the old 8–18 window.
        self._scheduler.add_job(
            self._monitor_positions,
            "cron",
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

        # No automatic end-of-day force-close job: positions are closed by each
        # epic's own market close (the per-epic close rule + the non-TRADEABLE
        # safety sweep on the hourly tradable refresh), never on a hard global
        # hour. ``_end_of_day`` remains available as a MANUAL "force close all"
        # action from the dashboard only.

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

        # No cron for the daily maintenance pair (buffer reset, candle dump +
        # purge): both are once-a-day housekeeping, not clock-critical work, and
        # a nightly cron fired while 24h markets (forex/CFD) were still trading.
        # They now run once per calendar day shortly after startup — see
        # ``_register_startup_jobs``.

        self._scheduler.start()
        self._running = True
        # Start every recurring job in manual mode (individually paused) — the
        # user enables jobs one by one from the dashboard "Actions" section.
        # Pausing per-job (rather than the whole scheduler) is what allows mixing
        # automatic and manual jobs afterwards.
        for job in self._scheduler.get_jobs():
            job.pause()

        # Streaming feed health watchdog: every minute, 24/7. Registered AFTER the
        # pause loop (like the startup jobs below) so it is NEVER paused and NOT a
        # dashboard-toggleable job — it is always-on infrastructure, not a trading
        # action. Reconnects a session that went dark (laptop sleep / network
        # change) and re-subscribes stalled open-position feeds. Independent of the
        # market-hours analysis tick so a feed dying out of hours or while the loop
        # is idle is still recovered. Second offset (:50) avoids the crowded
        # 0/15/20/30/40/45 slots used by the analysis/monitor/sync jobs.
        self._scheduler.add_job(
            self._streaming_health_check,
            "cron",
            second="50",
            id="streaming_health",
            name="Streaming feed health check",
        )

        self._register_startup_jobs()

        # Drive position management from the price feed rather than from the clock.
        self._register_candle_listener()
        self._warn_if_warmup_exceeds_buffer()

        logger.info(
            "Scheduler started — recurring jobs in manual mode — %d seed epics — "
            "startup discovery scheduled — enable jobs via web dashboard",
            len(self._all_epics),
        )

    def _register_startup_jobs(self) -> None:
        """Register the one-shot ``date`` jobs that run shortly after startup.

        Called from ``start()`` AFTER the manual-mode pause loop so these jobs stay
        active and actually fire. They are not in ``JOB_DEFINITIONS``, so the
        dashboard toggles never touch them; APScheduler drops each once it has run.
        (They were once added before the pause loop, so they were paused and the
        promised startup work never happened.)

        Two families, in firing order:

        1. **Discovery** (+15 s / +30 s) — an immediate epic crawl and tradable
           refresh so a mid-session launch doesn't wait for the next 07:30 / hour
           boundary. Every run is useful, so they carry no daily guard.
        2. **Daily maintenance** (+45 s / +60 s) — the buffer reset and the candle
           dump + purge, which replace their old midnight/02:00 crons. These are
           once-a-day tasks, so they are wrapped in ``_run_once_today``: restarting
           the server five times in an afternoon runs them exactly once. They fire
           last because both depend on the epic set the discovery jobs refresh.
        """
        now = datetime.now(UTC)
        self._scheduler.add_job(
            self._refresh_epic_list,
            "date",
            run_date=now + timedelta(seconds=15),
            id="startup_epic_refresh",
            name="Epic list refresh on startup",
        )
        self._scheduler.add_job(
            self._refresh_tradable_epics,
            "date",
            run_date=now + timedelta(seconds=30),
            id="startup_tradable_refresh",
            name="Tradable epic refresh on startup",
        )
        self._scheduler.add_job(
            self._startup_daily_reset,
            "date",
            run_date=now + timedelta(seconds=45),
            id="startup_daily_reset",
            name="Daily buffer reset on startup (once a day)",
        )
        self._scheduler.add_job(
            self._startup_dump_and_purge_candles,
            "date",
            run_date=now + timedelta(seconds=60),
            id="startup_candle_purge",
            name="Candle dump and purge on startup (once a day)",
        )

    async def _startup_daily_reset(self) -> None:
        """Startup wrapper: run the daily buffer reset unless it already ran today."""
        await self._run_once_today("daily_reset", self._daily_reset)

    async def _startup_dump_and_purge_candles(self) -> None:
        """Startup wrapper: run the candle dump + purge once per calendar day."""
        await self._run_once_today(
            "dump_and_purge_candles", self._dump_and_purge_candles
        )

    async def _run_once_today(
        self, action: str, run: Callable[[], Awaitable[None]]
    ) -> None:
        """Run a daily maintenance job unless it already ran today.

        The guard is the ``last_run_at`` stamp each job persists through
        ``_record_job_run``, compared on the UTC calendar day. A job that failed
        (and therefore recorded nothing) is retried on the next restart, which is
        the behaviour wanted for housekeeping: better twice than never.
        """
        last_run = await self._last_job_run(action)
        today = datetime.now(UTC).date()
        if last_run is not None and last_run.date() == today:
            logger.info(
                "Startup job '%s' already ran today (%s) — skipped",
                action,
                last_run.isoformat(),
            )
            return
        logger.info("Startup job '%s': running today's pass", action)
        try:
            await run()
        except Exception as exc:
            logger.error("Startup job '%s' failed: %s", action, exc)

    async def _last_job_run(self, action: str) -> datetime | None:
        """Return the UTC timestamp of a job's last successful run, if any."""
        try:
            async with self._session_factory() as session:
                row = await session.get(JobPreference, action)
        except Exception as exc:
            logger.warning("Could not read last run for '%s': %s", action, exc)
            return None
        if row is None or row.last_run_at is None:
            return None
        stamp = row.last_run_at
        # Always persisted in UTC; some drivers (SQLite) hand it back naive.
        return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)

    def _register_candle_listener(self) -> None:
        """Subscribe position monitoring to candle arrivals from the feed.

        A no-op when streaming is disabled: the cron heartbeat then remains the only
        trigger, which is exactly its purpose.
        """
        if self._streaming is None or self._candle_listener_registered:
            return
        self._streaming.add_candle_listener(self.on_candle)
        self._candle_listener_registered = True
        logger.info(
            "Position monitoring is candle-driven (debounce %.1fs; 30s cron kept "
            "as heartbeat)",
            self._settings.monitor_debounce_seconds,
        )

    def _warn_if_warmup_exceeds_buffer(self) -> None:
        """Warn when the selected strategy needs more history than the buffer holds.

        The buffer window is a hard ceiling on every lookback (see
        ``docs/DATAFLOW.md`` §3). Exceeding it is not an error — the strategy simply
        runs on a truncated window — but it is silent, and a strategy quietly
        evaluated on half the history it was designed for is indistinguishable from
        one that is merely underperforming. So it is said out loud, once, at startup.
        """
        capacity = self._buffer.max_candles
        try:
            warmup = self.strategy.warmup
        except Exception as exc:  # pragma: no cover - stub settings in tests
            logger.debug("Warm-up capacity check skipped: %s", exc)
            return
        if warmup > capacity:
            logger.warning(
                "Entry strategy '%s' needs %d candles but the buffer holds %d: it "
                "will be evaluated on a TRUNCATED window. Raise BUFFER_MAX_CANDLES "
                "to %d instead of shrinking the strategy.",
                self.strategy.name,
                warmup,
                capacity,
                warmup,
            )

    def on_candle(self, epic: str, candle) -> None:  # noqa: ANN001 - feed callback
        """Schedule a debounced monitoring pass when a fresh candle lands.

        Called by the streaming client on the event loop, once per completed candle
        and after the buffer holds it. It must return immediately, so the actual pass
        is a task.

        Candles for the streamed universe arrive together, so the first one schedules
        the pass and the rest of the wave joins it: one whole-book pass per wave,
        never one per epic — the group pre-pass needs every open position priced in
        the same pass to be valid at all.
        """
        if self._monitor_debounce_task is not None:
            return  # a pass is already scheduled for this wave
        if not self._monitor_trigger_allowed():
            return
        self._monitor_debounce_task = asyncio.ensure_future(
            self._run_debounced_monitor()
        )

    def _monitor_trigger_allowed(self) -> bool:
        """True when an event-driven monitoring pass may run right now.

        The user's manual/paused mode is the same single source of truth the cron
        obeys (a paused APScheduler job has no ``next_run_time``): pausing the
        Monitor Positions job from the dashboard must silence the candle-driven path
        too, or the toggle would quietly stop meaning anything.
        """
        if not self._running:
            return False
        job = self._scheduler.get_job("monitor_positions")
        return job is not None and job.next_run_time is not None

    async def _run_debounced_monitor(self) -> None:
        """Wait out the debounce window, then run one whole-book monitoring pass."""
        try:
            await asyncio.sleep(self._settings.monitor_debounce_seconds)
            # Re-checked after the wait: the job may have been paused meanwhile.
            if self._monitor_trigger_allowed():
                await self._monitor_positions()
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception as exc:
            logger.error("Candle-driven monitoring pass failed: %s", exc)
        finally:
            self._monitor_debounce_task = None

    def stop(self) -> None:
        """Stop the scheduler."""
        if self._monitor_debounce_task is not None:
            self._monitor_debounce_task.cancel()
            self._monitor_debounce_task = None
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

    @property
    def signals(self) -> SignalBook:
        """Return the book of proposed opens (``OPEN_MODE=notify``)."""
        return self._signals

    @property
    def notify_only(self) -> bool:
        """Return True when an automatic open must notify instead of ordering.

        Read straight from the settings (``OPEN_MODE``, validated at startup by
        :func:`validate_strategy_selection`) — like the strategy selection, the
        ``.env`` file is the single source of truth and there is no runtime
        switch.
        """
        return getattr(self._settings, "open_mode", "") == OPEN_MODE_NOTIFY

    @property
    def auto_open_enabled(self) -> bool:
        """Return True when the bot is authorised to open on its own right now.

        A block is scoped to the calendar day it was set on: once the day has
        rolled over the switch self-heals back to authorised, so a "no more
        trades today" decision never silently carries into the next session. The
        reset lives here rather than in the ``daily_reset`` job because that job
        only runs on a server startup — a bot left running for days would never
        clear the block.
        """
        if not self._auto_open_enabled and self._auto_open_day != date.today():
            self._auto_open_enabled = True
            self._auto_open_day = None
            logger.info("Auto-open re-authorised — the blocked day is over")
        return self._auto_open_enabled

    async def set_auto_open(self, enabled: bool) -> None:
        """Authorise (``True``) or block (``False``) every automatic open.

        Persisted so a restart in the middle of a blocked day keeps the block;
        the stored row carries the day it was set on and is ignored afterwards
        (see :meth:`_restore_auto_open`).
        """
        self._auto_open_enabled = enabled
        self._auto_open_day = None if enabled else date.today()
        logger.info(
            "Auto-open %s by the user",
            "authorised" if enabled else "blocked for the rest of the day",
        )
        self._recorder.info(
            "Auto-open authorised — the bot may open again"
            if enabled
            else "Auto-open blocked for today — open positions keep running"
        )
        await self._save_job_preference(AUTO_OPEN_PREF_KEY, enabled)

    def jobs_status(self) -> list[dict]:
        """Return every registered job with its current auto/manual mode.

        ``auto`` is True when the APScheduler job has a pending next run time; a
        paused (manual) job reports ``next_run_time is None``.
        """
        statuses: list[dict] = []
        for entry in JOB_DEFINITIONS:
            job = self._scheduler.get_job(entry["job_id"]) if self._running else None
            schedule = entry["schedule"]
            statuses.append(
                {
                    "action": entry["action"],
                    "name": entry["name"],
                    "description": entry["description"],
                    "schedule": schedule,
                    "danger": entry["danger"],
                    "auto": bool(job and job.next_run_time is not None),
                    # No cron behind the job → no auto/manual switch to show.
                    "toggleable": bool(entry.get("toggleable", True)),
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
        await self._save_all_job_preferences(auto=True)
        logger.info("All jobs switched to automatic (active)")

    async def load_job_preferences(self) -> None:
        """Restore the last-saved auto/manual mode for each registered job.

        Called once on startup (after ``start()``) so the bot resumes with the
        same job configuration the user had before the server was stopped.
        Jobs with no persisted preference stay in manual (the startup default).

        The auto-open switch is restored from the same table in the same pass
        (see :meth:`_restore_auto_open`).
        """
        try:
            async with self._session_factory() as session:
                rows = list(await session.scalars(select(JobPreference)))
        except Exception as exc:
            logger.warning("Could not load job preferences: %s", exc)
            return

        prefs = {
            row.action: row.auto for row in rows if row.action != AUTO_OPEN_PREF_KEY
        }
        self._restore_auto_open(
            next((r for r in rows if r.action == AUTO_OPEN_PREF_KEY), None)
        )

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

    def _restore_auto_open(self, row: JobPreference | None) -> None:
        """Re-apply a persisted auto-open block, but only within its own day.

        A stored ``False`` from an earlier day is stale by design — the block is
        a "today is over" decision — so it is logged and ignored, leaving the
        bot authorised. Anything else (no row, or an authorised row) is a no-op:
        authorised is the startup default.
        """
        if row is None or row.auto:
            return
        stamped = row.updated_at
        if stamped.tzinfo is None:
            # SQLite hands back naive datetimes; they were written as UTC.
            stamped = stamped.replace(tzinfo=UTC)
        blocked_on = stamped.astimezone().date()
        if blocked_on != date.today():
            logger.info(
                "Ignoring stale auto-open block from %s — a new day is authorised",
                blocked_on.isoformat(),
            )
            return
        self._auto_open_enabled = False
        self._auto_open_day = blocked_on
        logger.info("Auto-open block restored — still blocked for today")

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
                    if not entry.get("toggleable", True):
                        continue  # no cron behind it — nothing to persist
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
        """Persist the last successful-run time of a job.

        Called at the end of each eligible job (scheduled or manually triggered)
        so ``run_catch_up`` can tell a missed slot from one already covered, and
        so ``_run_once_today`` can tell a startup job that already ran today from
        one still owed its daily pass.
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

    async def trigger_crawl_market_catalog(self) -> None:
        """Manually run (or resume) the IG market-catalog crawl."""
        await self._crawl_market_catalog()

    async def trigger_refresh_tradable_epics(self) -> None:
        """Manually run the hourly tradable epic filter."""
        await self._refresh_tradable_epics()

    async def trigger_collect_and_analyze(self) -> None:
        """Manually run one collect-and-analyze cycle."""
        await self._collect_and_analyze()

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

    async def trigger_resync_day_history(self) -> dict:
        """Manually rebuild the last 30 days of Day summaries from IG history."""
        return await self._resync_day_history()

    async def trigger_daily_reset(self) -> None:
        """Manually run the daily buffer reset (clear + reload today's candles)."""
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

    @property
    def group_book(self) -> GroupBookSnapshot | None:
        """Last whole-book valuation at the planned stops, or None if unavailable."""
        return self._group_book

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
        """The close profile composed from the four zone selectors (built once).

        Independent of the entry strategy: it owns every exit decision for the
        positions opened this session. Each zone (``CLOSE_ZONESTART`` /
        ``CLOSE_ZONEMARGE`` / ``CLOSE_ZONESECURE`` / ``CLOSE_ZONEPROFIT``) is
        selected independently.
        """
        if self._close_profile_obj is None:
            self._close_profile_obj = get_close_profile(self._settings)
            logger.info(
                "Close profile plugged in: '%s' "
                "(zones: start=%s margin=%s secure=%s profit=%s)",
                self._close_profile_obj.name,
                self._settings.close_zonestart,
                self._settings.close_zonemarge,
                self._settings.close_zonesecure,
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
    def active_close_zone_names(self) -> tuple[str, str, str, str]:
        """The four per-zone updater names (start, margin, secure, profit)."""
        return (
            self._settings.close_zonestart,
            self._settings.close_zonemarge,
            self._settings.close_zonesecure,
            self._settings.close_zoneprofit,
        )

    @property
    def active_close_profile_name(self) -> str:
        """Compact ``start/margin/secure/profit`` label of the active close zones."""
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

    async def _crawl_market_catalog(self) -> None:
        """Build/refresh the static market catalog from IG's navigation tree.

        Manual action only (dashboard "Crawl Market Catalog", or
        ``python -m src.scripts.crawl_catalog``): the whole tree is far bigger
        than one API allowance, so this is meant to be launched off-hours and
        re-launched until it reports complete. The crawler persists its frontier,
        so each run resumes at the node the previous one stopped on and nothing
        is re-fetched.

        The job run is only stamped when the frontier is fully drained, so a
        partially-crawled catalog is never mistaken for a finished one.
        """
        if self._catalog_crawler.is_running:
            logger.warning("Market catalog crawl already running — request ignored")
            return

        logger.info("Starting market catalog crawl")
        try:
            progress = await self._catalog_crawler.crawl()
        except Exception as exc:
            logger.error("Market catalog crawl failed: %s", exc)
            return

        self._recorder.info(f"Market catalog crawl: {progress.summary()}")
        if progress.complete:
            await self._record_job_run("crawl_market_catalog")

    async def catalog_progress(self) -> CatalogProgress:
        """Current market-catalog / crawl-frontier snapshot (dashboard)."""
        return await self._catalog_crawler.progress()

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

    async def _followed_today_epics(self) -> list[str]:
        """Epics traded today whose position is already closed, latest close first.

        The day's watched list is not only about what can still be opened: a market
        the bot selected at time T and has since left (won or lost) stays worth
        recording, so the curve after the exit can be read against the trade. These
        epics are handed to :func:`~src.markets.risk_tiers.select_by_risk_tier` as
        *follow-ups* — kept behind the open positions, capped by
        ``epic_follow_closed_max``, and dropped from the ranking downwards when the
        cap leaves no room.

        Ordered by close time descending (rows with no ``time_close`` — a closed
        position whose time was never recorded — come last) so the cap keeps the
        most recent exits, which are the ones still worth watching. Returns an
        empty list on any DB error: a follow-up is a nicety, never a reason to fail
        the refresh.
        """
        limit = self._settings.epic_follow_closed_max
        if limit <= 0:
            return []
        try:
            async with self._session_factory() as session:
                rows = (
                    await session.execute(
                        select(Position.epic, func.max(Position.time_close))
                        .where(
                            Position.date == date.today(),
                            Position.state != PositionState.OPEN,
                        )
                        .group_by(Position.epic)
                    )
                ).all()
        except Exception as exc:
            logger.warning("Could not load today's closed epics for the feed: %s", exc)
            return []

        ordered = sorted(
            rows,
            key=lambda row: (row[1] is not None, row[1] or time.min),
            reverse=True,
        )
        return [epic for epic, _ in ordered]

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

    @staticmethod
    async def _minutes_since_last_open(session: AsyncSession) -> float | None:
        """Minutes since the most recent position was opened today, or None.

        Drives the per-strategy open cooldown (``open_cooldown_minutes``) in
        :meth:`_select_and_open`: the rolling selector waits at least that many
        minutes between two opens so it never fires a burst of positions at once.
        Uses the latest ``time_open`` (stored in UTC) of any position dated today;
        ``None`` means nothing has been opened today yet, so there is no cooldown
        to enforce.
        """
        latest = await session.scalar(
            select(func.max(Position.time_open)).where(
                Position.date == date.today(),
                Position.time_open.is_not(None),
            )
        )
        if latest is None:
            return None
        now = datetime.now(UTC)
        last_dt = datetime.combine(now.date(), latest, tzinfo=UTC)
        return (now - last_dt).total_seconds() / 60.0

    async def _alive_positions(self, session: AsyncSession) -> list[str]:
        """Epics of the open positions whose gain is already **locked in**.

        Drives the ``block_open_while_alive`` brake in :meth:`_select_and_open`.
        A position is *alive* when both hold:

        1. its **software** stop (``level_follower`` — the level the close profile
           enforces between bid polls, not the deeper ``level_stop`` resting at IG)
           has ratcheted to or past ``level_margin``, so the stop itself guarantees
           a profit rather than merely protecting the entry;
        2. the live close-out price (bid for a long, offer for a short) is beyond
           break-even (``level_zero``).

        Both comparisons are written on the signed distance, so a short is judged
        by the same rule mirrored (``sign = -1``).

        Condition 1 is the decisive one — it already implies 2 for any position
        whose stop has not fired — so a missing live price does not make a secured
        position look unsecured. Conversely a row lacking the levels to judge
        (adopted/legacy positions opened without them) is reported as **not** alive:
        the brake exists to stop adding risk beside a *confirmed* winner, never to
        freeze opening on an unknown.

        Returns the epics, so the caller can name them in its log.
        """
        rows = (
            await session.scalars(
                select(Position).where(Position.state == PositionState.OPEN)
            )
        ).all()
        alive: list[str] = []
        for position in rows:
            sign = 1.0 if (position.direction or "BUY") == "BUY" else -1.0
            level_zero = float(position.level_zero or 0)
            level_margin = float(position.level_margin or 0)
            follower = float(position.level_follower or 0)
            if level_zero <= 0 or level_margin <= 0 or follower <= 0:
                continue  # not enough levels to judge — treat as still waiting
            if sign * (follower - level_margin) < 0:
                continue  # software stop has not reached the margin yet
            buf = self._buffer.get(position.epic)
            last = buf.last if buf is not None else None
            if last is not None:
                price = last.bid_close if sign > 0 else last.offer_close
                if sign * (price - level_zero) <= 0:
                    continue  # locked stop but price back at break-even
            alive.append(position.epic)
        return alive

    async def _still_moving_positions(
        self, session: AsyncSession, window: int
    ) -> list[str]:
        """Open epics whose market is **not** a plain range, with their regime.

        Drives the ``require_range_book`` brake in :meth:`_select_and_open`: the
        strategy may only add a position when everything already open has stopped
        moving. An epic qualifies as quiet only when its live curve reads
        :attr:`~src.core.reading.Regime.RANGE` over ``window`` candles — choppy
        *and* contained. Everything else is reported, so the brake blocks on it:

        * ``TREND_UP`` / ``TREND_DOWN`` — the position is still riding its move;
        * ``VOLATILE`` — swings going nowhere, the worst market to add risk into;
        * ``UNKNOWN`` — no buffer or too little history to judge. Deliberately
          blocking: this brake opens only into a book that is *demonstrably*
          quiet, never into one it cannot read (the opposite choice from
          :meth:`_alive_positions`, which must not freeze opening on an unknown).

        Returns ``"EPIC=REGIME"`` strings so the caller can name what is blocking.
        """
        epics = (
            await session.scalars(
                select(Position.epic).where(Position.state == PositionState.OPEN)
            )
        ).all()
        moving: list[str] = []
        for epic in dict.fromkeys(epics):  # de-duplicated, order kept
            buf = self._buffer.get(epic)
            if buf is None or len(buf) < MIN_REGIME_MINUTES:
                moving.append(f"{epic}={Regime.UNKNOWN}")
                continue
            regime = CurveReading.of(buf, regime_window=window).regime
            if regime is not Regime.RANGE:
                moving.append(f"{epic}={regime}")
        return moving

    async def _refresh_tradable_epics(self) -> None:
        """Filter ``_all_epics`` to those currently open and TRADEABLE.

        Runs hourly during market hours. Batch-fetches market details (v1, groups
        of 25) and applies the open/TRADEABLE filter. No spread filter is applied
        here — the spread is checked later at analysis time.

        This is the only path that *fetches*: it refreshes the market snapshot,
        force-closes any open position whose market is no longer tradable, then
        hands the snapshot to :meth:`_rebuild_tradable_set`, which owns the actual
        subscription-set choice and is also re-run intra-day (without any API
        call) when the risk tier moves.
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

        # Safety net for the per-epic close rule: if an open position's market is
        # no longer TRADEABLE (it closed, or went to auction/edits-only), force it
        # shut so a trade can't be stranded past its market's close. Only act on a
        # status we actually observed this refresh (unknown epics are left alone).
        # Done BEFORE the selection so a market being force-closed here no longer
        # claims one of the pinned slots below.
        status_by_epic = {m.epic: m.status for m in infos}
        open_epics = await self._open_position_epics()
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

        # Keep the snapshot so the risk tier can re-pick the watched set intra-day
        # from the same data instead of spending a fresh batch of API calls.
        self._market_infos = infos
        self._pricing_blacklist.clear()
        await self._rebuild_tradable_set(infos)
        logger.debug("Pricing blacklist cleared by the tradable refresh")

    async def _rebuild_tradable_set(self, infos: list[MarketInfo]) -> None:
        """Choose the watched subset from a market snapshot and publish it.

        Split out of :meth:`_refresh_tradable_epics` because it must also run
        *without* fetching: the risk tier (see :mod:`src.markets.risk_tiers`) can
        move at any moment of the day — the instant a close changes the realised
        P&L — and the watched list has to follow it immediately, not at the next
        hourly refresh. Everything here is pure computation plus the two writes
        that publish the result (the Epic List flags and the feed subscriptions).

        Three stages:

        1. ``select_tradable`` — the open/affordable/dedupe filters, applied to
           the whole snapshot. Exclusion reasons are computed against THIS set so
           the only ones it explains are genuine (closed, no price, too expensive,
           product-variant duplicate) — never the cap below.
        2. the cap — IG allows a fixed number of Lightstreamer subscriptions, so
           when more markets are tradable than fit, either the risk-tier ladder
           picks them (``epic_risk_tiers_enabled``: the day's realised P&L chooses
           the euro band, open positions are pinned in and the epics already
           traded today are followed right behind them) or, when it is disabled,
           the previous class-diversified pick does.
        3. publication — persist the flags, then push the subscription set with
           the open-position epics first, then the day's follow-ups, so both
           survive ``set_epics``' own truncation.
        """
        selected = self._scanner.select_tradable(infos)
        open_epics = await self._open_position_epics()
        # Markets already traded today, still watched so the day can show what they
        # did after the exit (see :meth:`_followed_today_epics`).
        followed = [
            e for e in await self._followed_today_epics() if e not in open_epics
        ]
        cap = self._settings.streaming_max_epics if self._streaming is not None else 0

        capped_out: list[str] = []
        reason_capped = "streaming_cap"
        if self._settings.epic_risk_tiers_enabled and cap > 0:
            realised = await self._realized_pnl_today_safe()
            tier = tier_for_pnl(realised, tuple(self._settings.epic_risk_tier_bounds))
            selection = select_by_risk_tier(
                selected,
                cap=cap,
                tier=tier,
                pinned=open_epics,
                followed=followed,
                follow_cap=self._settings.epic_follow_closed_max,
            )
            tradeable = selection.markets
            self._risk_tier = tier
            self._risk_tier_pins = frozenset(open_epics)
            self._risk_tier_follows = tuple(followed)
            reason_capped = f"risk_tier_{tier.label}"
            logger.info(
                "Risk-tier epic selection: %.2f€ realised today -> %s (from %d "
                "tradable candidates)",
                realised,
                selection.summary(),
                len(selected),
            )
        elif cap > 0 and len(selected) > cap:
            tradeable = self._scanner.select_diversified_subset(selected, cap)
            mix: dict[str, int] = {}
            for m in tradeable:
                cls = m.instrument_type.upper() or "OTHER"
                mix[cls] = mix.get(cls, 0) + 1
            breakdown = ", ".join(f"{k}={v}" for k, v in sorted(mix.items()))
            logger.info(
                "Tradable epics capped to %d (diversified across classes: %s) — "
                "dropped %d over the IG streaming limit",
                cap,
                breakdown,
                len(selected) - len(tradeable),
            )
        else:
            tradeable = selected

        kept = {m.epic for m in tradeable}
        capped_out = [m.epic for m in selected if m.epic not in kept]

        self._tradable_markets = tradeable
        self._tradable_epics = [m.epic for m in tradeable]
        self._tradable_last_refresh = datetime.now(UTC)
        logger.info(
            "Tradable epics: %d / %d are open/TRADEABLE",
            len(self._tradable_epics),
            len(self._all_epics),
        )

        # Reasons against the pre-cap set, then overlay the cap reason for markets
        # that were tradable but cut to fit the subscription limit / the unlocked
        # risk tier — so the Epic List doesn't mislabel them as "duplicate".
        reasons = self._scanner.get_non_tradable_reasons(infos, selected)
        for epic in capped_out:
            reasons[epic] = reason_capped
        await self._persist_tradable_flags(self._tradable_epics, reasons)

        if self._streaming is not None:
            # Epics holding an open position must keep their live feed until the
            # trade closes, so the position's candle history (and the bid the
            # monitor reads) never stops mid-trade — even if the selection above
            # dropped them. They come first so they survive ``set_epics``'
            # truncation to the IG subscription cap, then the day's follow-ups
            # (traded and already closed — their curve is still being recorded),
            # and the tradable set fills the rest.
            follow_stream = followed[: max(self._settings.epic_follow_closed_max, 0)]
            head = [*sorted(open_epics), *follow_stream]
            stream_epics = [
                *head,
                *(e for e in self._tradable_epics if e not in set(head)),
            ]
            await self._streaming.set_epics(stream_epics)

    async def _realized_pnl_today_safe(self) -> float:
        """Today's banked P&L, or 0 € when it cannot be read.

        The risk tier degrades to its first rung (the small markets) on a DB
        error, which is the conservative side: an unreadable P&L must never be
        read as permission to escalate.
        """
        try:
            async with self._session_factory() as session:
                return await self._realized_pnl_today(session)
        except Exception as exc:
            logger.warning("Could not read today's realized P&L for the tier: %s", exc)
            return 0.0

    async def _maybe_reselect_risk_tier(self) -> None:
        """Re-pick the watched epics when the day's P&L unlocked another tier.

        Called on every analysis tick and cheap by design: it reads the realised
        P&L and the open-position epics, and returns immediately unless one of
        them actually moved the selection. Without it a tier change would only
        take effect at the next hourly refresh, so a morning that banks its first
        30 € would keep trading the small markets for the rest of the hour.

        The pinned set is part of the trigger, not just the tier: a close turns its
        epic from a pin into a follow-up (still watched, but now droppable), which
        hands the slot back to the tier as soon as the cap needs it.
        """
        if not self._settings.epic_risk_tiers_enabled:
            return
        if not self._market_infos or self._streaming is None:
            return  # nothing to re-pick from, or no subscription cap to respect

        realised = await self._realized_pnl_today_safe()
        tier = tier_for_pnl(realised, tuple(self._settings.epic_risk_tier_bounds))
        pins = frozenset(await self._open_position_epics())
        follows = tuple(e for e in await self._followed_today_epics() if e not in pins)
        if (
            tier == self._risk_tier
            and pins == self._risk_tier_pins
            and follows == self._risk_tier_follows
        ):
            return

        if tier != self._risk_tier:
            previous = self._risk_tier.label if self._risk_tier else "none"
            logger.info(
                "Risk tier moved %s -> %s on %.2f€ realised today — re-picking the "
                "watched epics",
                previous,
                tier.label,
                realised,
            )
        await self._rebuild_tradable_set(self._market_infos)

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

    async def _streaming_health_check(self) -> None:
        """Every-minute streaming watchdog: reconnect a dead feed, repair stale epics.

        Runs 24/7 as always-on infrastructure (never paused, not a toggleable
        job), because the app runs on a laptop that sleeps or changes network
        mid-session: on resume the Lightstreamer socket is dead and the feed goes
        silent. The built-in status-callback reconnect only covers a subset of
        disconnect states (a session wedged in ``DISCONNECTED:TRYING-RECOVERY`` is
        never recovered), and the analysis-tick watchdog is gated to market hours
        and simply bails when disconnected. This closes both gaps.

        Connection-level first: if the session reports down, force a reconnect
        (:meth:`~src.feed.streaming.IGStreamingClient.ensure_connected` tears the
        client down and re-subscribes every epic). When connected, fall back to
        the per-epic repair that re-subscribes a missing/stalled open-position
        feed. A no-op when streaming is disabled (``self._streaming is None``).
        """
        streaming = self._streaming
        if streaming is None:
            return
        if not streaming.is_connected:
            logger.warning("Streaming health: session down — forcing reconnect")
            await streaming.ensure_connected()
            return
        await self._ensure_open_epics_streamed()

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

        # Then make sure the watched universe still matches the risk tier the day
        # has paid for: a close that banks the next 30 € must move the list now,
        # not at the next hourly refresh. A no-op unless the tier or the pinned
        # open positions actually changed.
        await self._maybe_reselect_risk_tier()

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

    @staticmethod
    def _closeout_prices_since(
        buf: EpicBuffer, opened_at: datetime, sign: float
    ) -> list[float]:
        """Close-out prices of the candles recorded since ``opened_at``, oldest first.

        "Close-out" means the price the position would actually be closed at: a long
        is closed on the **bid**, a short on the **offer** (one spread higher). Those
        are the terms every level the exit reasons about is expressed in
        (``level_follower`` / ``level_zero`` / the stop), so a curve built this way
        can be compared to the stop directly on both sides — mirroring
        ``_close_out_price`` in ``src/exit/close_zoneprofit.py``.
        """
        return [
            candle.bid_close + candle.spread if sign < 0 else candle.bid_close
            for candle in buf.candles
            if candle.timestamp >= opened_at
        ]

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
        *,
        allow_short: bool = False,
        allow_reopen: bool = False,
        manual: bool = False,
    ) -> tuple[Position | None, str | None]:
        """Open ``intent.epic`` under its per-epic lock (single-flight per epic).

        Holds the lock across the duplicate-epic gate **and** the order placement
        so a second concurrent caller (analysis tick, manual dashboard open,
        rolling selector, or the same open fired twice from two tabs) re-runs the
        gate only after the first has committed its provisional row — and is then
        correctly refused instead of placing a second order on the same epic.

        ``allow_short`` lifts the long-only gate for a manual dashboard SELL and
        ``allow_reopen`` lifts the global same-day re-open gate
        (``ALLOW_SAME_DAY_REOPEN``); automatic callers leave both ``False``.

        ``manual`` marks a human-triggered open (the dashboard Buy/Sell buttons),
        which bypasses the auto-open switch: blocking auto-open stops the *bot*
        from opening, it never takes the buttons away from the user.

        Returns ``(position, None)`` on success, ``(None, reason)`` when the gate
        refuses the open, and ``(None, None)`` when IG rejected the order.
        """
        # Auto-open switch — the single choke point every automatic open path
        # goes through, so no entry strategy (per-epic or cross-epic ranker) can
        # open while the user has closed the day. Checked before the lock: no
        # point serialising an open that is refused outright.
        if not manual and not self.auto_open_enabled:
            return None, AUTO_OPEN_REFUSAL

        async with self._open_lock(intent.epic):
            allowed, reason = await trading.can_open_intent(
                intent, allow_short=allow_short, allow_reopen=allow_reopen
            )
            if not allowed:
                return None, reason
            # Notify mode — the open is fully validated and deliberately not
            # taken. Recorded *after* the gates so the user is only ever
            # notified about a market the bot could really have opened (no
            # notification for an epic already held, a market about to close or
            # an unaffordable margin), and inside the per-epic lock so two
            # concurrent ticks raise one signal, not two.
            if not manual and self.notify_only:
                self._signals.record(
                    intent.epic,
                    intent.direction,
                    strategy=self.strategy.name,
                    score=intent.score,
                    price=self._signal_price(intent.direction, buf),
                    name=self._market_name(intent.epic),
                )
                return None, NOTIFY_REFUSAL
            position = await trading.open_from_intent(intent, buf)
            return position, None

    def _market_name(self, epic: str) -> str:
        """Return the instrument description for ``epic``, or "" when unknown.

        Best effort from the last market snapshot: a signal is readable without
        it (the epic is displayed either way), it just reads better with it.
        """
        for info in self._market_infos:
            if info.epic == epic:
                return info.name
        return ""

    @staticmethod
    def _signal_price(direction: str, buf: EpicBuffer) -> float:
        """Return the close-out price shown in a signal, 0.0 when unavailable.

        Close-out, not mid: it is the price the user would actually get out at,
        which is the only one worth quoting next to a proposal to enter.
        """
        last = getattr(buf, "last", None)
        if last is None:
            return 0.0
        bid = float(getattr(last, "bid_close", 0.0) or 0.0)
        spread = float(getattr(last, "spread", 0.0) or 0.0)
        return bid + spread if direction.upper() == "SELL" else bid

    def _curve_supports_revert(
        self, position: Position, buf: EpicBuffer
    ) -> tuple[bool, str]:
        """Gather the curve ``position`` walked since its open and judge it.

        The bookkeeping rule (:func:`should_revert_after_stop_loss`) says *which*
        stop fired; this says whether the market actually broke through it. It
        collects what the pure :func:`curve_supports_revert` rule needs — the
        close-out prices since the open, where price is right now, and how long the
        position was held — and returns its verdict unchanged.

        Holding time is **not** a filter in that rule (a market can sit flat for
        twenty minutes and then break in one candle); it only separates a position
        that died before its first candle was recorded from one whose candles are
        missing. It comes from ``time_close`` when the close has been stamped (with
        a day added when it lands after midnight UTC, since ``date`` is the *open*
        day) and from the clock otherwise: both call sites decide the revert within
        seconds of seeing the close.

        Returns ``(False, reason)`` whenever the curve cannot be located at all
        (no open time, no opening level) — an unjudgeable curve is not a licence to
        revert.
        """
        opened_at = position_opened_at(position)
        if opened_at is None:
            return False, "Open time unknown (cannot locate the curve)"

        level_open = float(position.level_open or 0)
        if level_open <= 0:
            return False, "Opening level unknown (cannot judge the curve)"

        closed_at = datetime.now(UTC)
        if position.time_close is not None and position.date is not None:
            closed_at = datetime.combine(position.date, position.time_close, tzinfo=UTC)
            if closed_at < opened_at:
                closed_at += timedelta(days=1)

        sign = -1.0 if position.direction == "SELL" else 1.0
        last = buf.last
        current_price = last.bid_close + (last.spread if sign < 0 else 0.0)
        return curve_supports_revert(
            direction=position.direction,
            level_open=level_open,
            original_stop=original_stop_level(position),
            prices=self._closeout_prices_since(buf, opened_at, sign),
            current_price=float(current_price),
            minutes_held=max(0.0, (closed_at - opened_at).total_seconds() / 60),
        )

    async def _revert_after_stop_loss(
        self,
        session: AsyncSession,
        position: Position,
        config: TradeConfig,
    ) -> Position | None:
        """Open the opposite side when ``position`` was stopped out at a loss.

        The recovery-revert rule (``ALLOW_RECOVERY_REVERT`` in ``.env``): a trade
        — BUY or SELL — whose price came all the way back to the protective stop
        it was **opened with** did not just lose, it was *wrong about the
        direction*. The market walked through the level the trade was built on, so
        the bot immediately takes the reverse side on the same epic to follow the
        turn, instead of leaving the move alone until some entry strategy happens
        to signal it.

        Which closes qualify is decided by **two** pure rules, both of which must
        agree: :func:`~src.execution.gates.should_revert_after_stop_loss` on the
        bookkeeping (stop hit, real loss, original stop, single hop) and
        :func:`~src.execution.gates.curve_supports_revert` on the **curve since the
        open** (see :meth:`_curve_supports_revert`). The second one is deliberately
        permissive — it only drops the blatant cases, where no single candle put a
        real chunk of the risk into the adverse move (a flat curve leaking to the
        stop), where the market chopped its way there, or where the stop was merely
        grazed by a wick price has already left. Called from the two places a
        position can be recorded as closed by its stop:
        :meth:`_monitor_positions` (the software backstop fired) and
        :meth:`_sync_positions` (the broker-side stop resting at IG fired and the
        vanished position was reconciled).

        The revert goes through the shared open path — same per-epic lock, same
        gates, same close profile and sizing as any other open — with two gates
        deliberately lifted:

        * ``allow_short=True``: the reverse of a long *is* a short, so the
          long-only restriction on automatic opens cannot apply here;
        * ``allow_reopen=True``: the epic was traded seconds ago, so
          ``ALLOW_SAME_DAY_REOPEN=false`` would refuse every revert — the rule
          would be dead code.

        Everything else still applies. It is **not** a manual open, so the
        auto-open switch governs it: a user who closed the day gets no reverts.
        The duplicate-epic gate and the "market closes soon" gate apply too.

        Returns the reverse :class:`Position` when one was opened, ``None``
        otherwise (policy off, close not eligible, no price data, gate refusal or
        an IG rejection). Never raises: a failed revert must not break the
        monitor/sync tick that closed the position.
        """
        if not bool(getattr(self._settings, "allow_recovery_revert", False)):
            return None

        revert, reason = should_revert_after_stop_loss(
            direction=position.direction,
            reason_close=position.reason_close,
            reason_open=position.reason_open,
            euro=float(position.euro or 0),
            level_close=float(position.level_close or 0),
            original_stop=original_stop_level(position),
            stop_ratcheted=bool(position.stop_update or 0),
        )
        if not revert:
            logger.debug("No recovery revert for %s: %s", position.epic, reason)
            return None

        direction = reverse_direction(position.direction)
        buf = self._buffer.get(position.epic)
        if not buf or not buf.last:
            logger.warning(
                "Recovery revert for %s skipped: no live price data to open %s on",
                position.epic,
                direction,
            )
            return None

        supported, curve_reason = self._curve_supports_revert(position, buf)
        if not supported:
            logger.info(
                "Recovery revert on %s dropped — the curve since open does not "
                "justify it: %s",
                position.epic,
                curve_reason,
            )
            return None

        logger.info(
            "Recovery revert: %s %s stopped out at its opening stop (%.5f, "
            "P&L=%.2f€) — opening %s to follow the reversal",
            position.direction,
            position.epic,
            original_stop_level(position),
            float(position.euro or 0),
            direction,
        )
        intent = EntryIntent(epic=position.epic, direction=direction)
        try:
            trading = TradingService(
                self._client, session, config, close_profile=self.close_profile
            )
            reverse, refusal = await self.open_epic_guarded(
                trading,
                intent,
                buf,
                allow_short=True,
                allow_reopen=True,
            )
        except Exception as exc:
            logger.error("Recovery revert failed for %s: %s", position.epic, exc)
            return None

        if reverse is None:
            logger.info(
                "Recovery revert on %s not taken: %s",
                position.epic,
                refusal or "IG rejected the order",
            )
            return None

        # Mark the origin so the dashboard shows where the position came from and
        # the single-hop cap can recognise it if it is stopped out in turn.
        reverse.reason_open = RECOVERY_REVERT_REASON_OPEN
        await session.commit()
        self._recorder.info(
            f"Recovery revert: {direction} {position.epic} @ {reverse.level_open} "
            f"after a {float(position.euro or 0):.2f}€ stop-out"
        )
        return reverse

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
        # Long-only unless the strategy declares it trades both ways: a SELL from
        # a strategy that has not opted in is dropped here (and would be refused
        # by the shared pre-open gate anyway).
        allow_short = getattr(strategy, "emits_shorts", False)
        if intent and (intent.direction == "BUY" or allow_short):
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
                position, reason = await self.open_epic_guarded(
                    trading, intent, buf, allow_short=allow_short
                )
                if position:
                    self._recorder.info(
                        f"Position opened: {epic} @ {position.level_open}"
                    )
                elif reason == AUTO_OPEN_REFUSAL:
                    # The strategy did signal an open and the user's own switch
                    # stopped it — worth an INFO line so the dashboard log shows
                    # what the closed day is skipping (intents are rare).
                    logger.info(
                        "Auto-open blocked — skipped %s on %s", intent.direction, epic
                    )
                elif reason:
                    logger.debug("Cannot open %s: %s", epic, reason)

    async def _select_and_open(self) -> None:
        """Maintain the rolling cross-epic portfolio at its target size.

        Active only for a cross-epic ranker entry (``cross_epic_selection``, e.g.
        ``open_ranking``); a no-op otherwise. The selection knobs are
        constants on the strategy class (``concurrent_positions``,
        ``wallet_bounded``, ``wallet_reserve``), not settings. The goal is to
        stay in the market all day and re-fill as soon as a position closes —
        win or loss. Two modes, chosen by the strategy's ``wallet_bounded`` flag:

        - **count-bounded** (default): hold exactly ``concurrent_positions`` open
          positions (default 1, a single rolling position);
        - **wallet-bounded** (``open_saferanking``): no fixed count — keep opening
          the best-ranked affordable epics until the spendable balance can no
          longer cover another margin.

        A count-bounded strategy may also declare ``require_flat_book``
        (``open_five``), which turns the top-up into an all-or-nothing **series**:
        nothing is opened while any position is still open, and when the book is
        empty the whole basket goes on in a single pass. A strategy that declares
        ``require_range_book`` (``open_momentum``) instead makes the book's *state*
        the condition: it opens only while every open position's epic has stopped
        moving (regime ``RANGE``), so a second bet is never taken beside one that
        is still riding its move.

        Each invocation:

        1. count-bounded only — returns early when the target position count is
           already met (the cheap steady state while positions are running);
        2. otherwise scores every tradable epic, ranks the BUY candidates by score
           and opens the best ones that pass the shared open gates **and** the
           wallet check (available balance minus ``wallet_reserve`` must cover the
           epic's margin), until the count target is reached (count-bounded) or
           the wallet is exhausted (wallet-bounded).

        There is no wall-clock warm-up: an epic may be opened as soon as its
        market is open (it is in ``_tradable_epics``, filtered to ``TRADEABLE``)
        and it has enough buffered candles (``len(buf) >= strategy.warmup``). The
        candle count is the warm-up — the bid curve must be long enough to score.

        The lock serialises concurrent callers (analysis loop, manual triggers)
        and the open-count is re-checked inside it, so the target is never
        overshot.
        """
        strategy = self.strategy
        if not strategy.cross_epic_selection:
            return  # per-epic entry strategy drives opens via _collect_and_analyze

        # Auto-open blocked: skip the whole scoring pass rather than rank every
        # epic only to have ``open_epic_guarded`` refuse each candidate.
        if not self.auto_open_enabled:
            logger.debug("Rolling select: auto-open disabled for today — skipping")
            return

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
                # Flat-book brake: a strategy that opens *in series*
                # (``require_flat_book``, e.g. ``open_five``) opens nothing while
                # any position is still open, whatever its state — the next basket
                # waits for the previous one to be entirely closed, so a series is
                # judged as a whole. Stricter than ``block_open_while_alive``
                # below, which only steps aside for an already-secured winner.
                if getattr(strategy, "require_flat_book", False) and open_count:
                    logger.debug(
                        "Rolling select [%s]: %d position(s) still open — a new "
                        "series waits for a flat book",
                        strategy.name,
                        open_count,
                    )
                    return

                target = max(int(strategy.concurrent_positions), 1)
                wallet_bounded = getattr(strategy, "wallet_bounded", False)
                slots = target - int(open_count)
                # A count-bounded ranker holds exactly ``concurrent_positions``
                # open and returns cheaply once the target is met. A wallet-bounded
                # ranker has no fixed target — it keeps opening until the wallet
                # runs dry — so it never short-circuits here; the wallet gate below
                # is its only limit (``slots`` is re-derived once funds are known).
                if not wallet_bounded and slots <= 0:
                    logger.debug(
                        "Rolling select: target met (%d/%d open) — holding",
                        open_count,
                        target,
                    )
                    return  # target met — a position is already running

                # Alive-position brake: a strategy that declares
                # ``block_open_while_alive`` stops opening entirely while one of
                # its positions has already locked its gain in (software stop past
                # the margin) — no point adding risk next to a secured winner. A
                # position still *waiting* for its move is not alive and does not
                # block, so an idle trade never sits on an opportunity.
                if getattr(strategy, "block_open_while_alive", False):
                    alive = await self._alive_positions(session)
                    if alive:
                        logger.info(
                            "Rolling select [%s]: %d position(s) alive (%s) — "
                            "gain already locked in, opening nothing new",
                            strategy.name,
                            len(alive),
                            ", ".join(alive),
                        )
                        return

                # Range-book brake: a strategy that declares
                # ``require_range_book`` adds a position only when everything
                # already open has stopped moving — every open epic reads RANGE.
                # One position still riding a trend (or a market too thin to be
                # read) holds the whole pass: the money already committed is
                # working, and a second bet beside it only dilutes it.
                if getattr(strategy, "require_range_book", False) and open_count:
                    window = int(getattr(strategy, "book_regime_window", 30) or 30)
                    moving = await self._still_moving_positions(session, window)
                    if moving:
                        logger.info(
                            "Rolling select [%s]: %d open position(s) still in a "
                            "move (%s) — opening nothing new",
                            strategy.name,
                            len(moving),
                            ", ".join(moving),
                        )
                        return
                    logger.debug(
                        "Rolling select [%s]: the whole book (%d position(s)) is "
                        "in a range — a new open is allowed",
                        strategy.name,
                        open_count,
                    )

                # Open cooldown: a strategy that spaces its opens out
                # (``open_cooldown_minutes`` > 0) opens at most one position per
                # pass and only once at least that many minutes have elapsed since
                # the most recent open, so positions are not fired in a burst.
                cooldown = int(getattr(strategy, "open_cooldown_minutes", 0) or 0)
                if cooldown > 0:
                    since = await self._minutes_since_last_open(session)
                    if since is not None and since < cooldown:
                        logger.debug(
                            "Rolling select [%s]: open cooldown active "
                            "(%.1f min since last open < %d min) — holding",
                            strategy.name,
                            since,
                            cooldown,
                        )
                        return

                # Diversity rule: an epic already *used* today (it had an opening,
                # now open or closed) is dropped from the candidate set so the
                # rolling position rotates across markets instead of re-opening the
                # same epic the moment it closes. The shared ``epic_already_open``
                # gate only blocks *concurrent* duplicates, not a same-day re-open.
                # The policy is GLOBAL to every open strategy and comes from
                # ``ALLOW_SAME_DAY_REOPEN`` in .env: when it is on this filter is
                # skipped — an epic is a candidate again as soon as it holds no
                # open position, so the same rising market can be opened several
                # times in one day. This is only a cheap pre-filter; the shared
                # pre-open gate re-checks it under the per-epic lock.
                if bool(self._settings.allow_same_day_reopen):
                    candidates = list(epics)
                else:
                    traded_today = await self._traded_today_epics(session)
                    candidates = [e for e in epics if e not in traded_today]
                if not candidates:
                    logger.debug("Rolling select: every epic already used today")
                    return

                # Participation gate: only crown a winner once more than
                # ``min_participation_ratio`` of the livestreamed tradable universe
                # has warmed up (``len(buf) >= warmup``). Measured over the whole
                # tradable set (not just today's untraded candidates) so the
                # denominator reflects the live universe, not how far into the day
                # we are. Guards against "false tournaments" right after a
                # mid-session restart, when only a handful of epics have rebuilt
                # enough history and the ranker would otherwise crown the least-bad
                # of a tiny pool.
                ratio = max(0.0, min(1.0, strategy.min_participation_ratio))
                ready = sum(
                    1
                    for e in epics
                    if (b := self._buffer.get(e)) is not None
                    and len(b) >= strategy.warmup
                )
                if ready <= ratio * len(epics):
                    logger.info(
                        "Rolling select: only %d/%d epics warmed up "
                        "(<= %.0f%% participation) — skipping tournament",
                        ready,
                        len(epics),
                        ratio * 100,
                    )
                    return

                # Same guard expressed as an absolute count, for a strategy whose
                # rule is "a ranking needs at least N candidates" rather than a
                # fraction of a universe that drifts in size across the session.
                # Both gates apply; 0 (the default) disables this one.
                min_candidates = int(
                    getattr(strategy, "min_participation_count", 0) or 0
                )
                if min_candidates and ready < min_candidates:
                    logger.info(
                        "Rolling select [%s]: only %d warmed-up epic(s) < %d "
                        "required candidates — ranking not valid, skipping",
                        strategy.name,
                        ready,
                        min_candidates,
                    )
                    return

                # Score every epic with enough buffered history; keep the
                # tradable candidates, then rank by score (highest first). A SELL
                # is kept only when the strategy declares it trades both ways
                # (``emits_shorts``); otherwise the ranker stays long-only.
                allow_short = getattr(strategy, "emits_shorts", False)
                ranked: list[tuple[EntryIntent, EpicBuffer]] = []
                evaluated = 0
                for epic in candidates:
                    buf = self._buffer.get(epic)
                    if not buf or len(buf) < strategy.warmup:
                        continue
                    evaluated += 1
                    intent = strategy.evaluate(epic, buf)
                    if intent and (intent.direction == "BUY" or allow_short):
                        ranked.append((intent, buf))
                if not ranked:
                    if evaluated:
                        # Epics were warmed up and scored, but every one was
                        # rejected — e.g. none is in a genuine uptrend (the
                        # ``require_uptrend`` gate) or none clears the score floor
                        # (``min_score``, e.g. a rise too flat for open_allincrease).
                        # The wallet has room and any open cooldown has elapsed
                        # (both checked above), so make the "funds free but no
                        # market qualifies" decision visible instead of a silent
                        # no-op.
                        logger.info(
                            "Rolling select [%s]: none of %d warmed-up epic(s) "
                            "qualifies to open (no market rising strongly enough) "
                            "— staying flat",
                            strategy.name,
                            evaluated,
                        )
                    else:
                        logger.debug("Rolling select: no scorable epic yet")
                    return
                ranked.sort(key=lambda item: item[0].score, reverse=True)

                # Post-ranking cross-epic filter (identity for most strategies).
                # A per-epic score cannot see anything that is a property of the
                # *set* of winners, so the strategy gets one look at the sorted
                # ranking before any order is placed — e.g. ``open_five`` drops the
                # candidates whose curve duplicates the shape of a better-ranked
                # one, so a basket of five is five different bets. Filtering the
                # whole ranking (not just the slots) lets a survivor further down
                # take a dropped candidate's place.
                filtered = strategy.filter_ranked(ranked)
                if len(filtered) != len(ranked):
                    logger.info(
                        "Rolling select [%s]: %d of %d ranked candidate(s) kept "
                        "after the cross-epic filter",
                        strategy.name,
                        len(filtered),
                        len(ranked),
                    )
                ranked = filtered
                if not ranked:
                    logger.info(
                        "Rolling select [%s]: every ranked candidate was refused "
                        "by the cross-epic filter — staying flat",
                        strategy.name,
                    )
                    return

                # Risk ladder (``uses_risk_ladder``, e.g. ``open_progressive``):
                # price every surviving candidate in euros — what a stop-out would
                # actually cost — and let the strategy keep only those inside the
                # band the day's *realised* P&L has paid for. This is the one place
                # a strategy sees money: ``evaluate`` reads a price curve and cannot
                # know what an epic costs to be wrong on. Done after the cross-epic
                # filter so the euro estimate (one ``initial_plan`` per candidate)
                # is only computed for candidates still in the running.
                if getattr(strategy, "uses_risk_ladder", False):
                    realised = await self._realized_pnl_today(session)
                    risks = self._risk_estimates(ranked, config)
                    ranked = strategy.filter_by_risk(
                        ranked, risks=risks, realised_today=realised
                    )
                    if not ranked:
                        logger.info(
                            "Rolling select [%s]: no candidate the risk filter "
                            "accepts at %.2f€ realised today — staying flat",
                            strategy.name,
                            realised,
                        )
                        return

                available = await self._account_available_funds()
                reserve = max(0.0, min(1.0, strategy.wallet_reserve))
                spendable = (
                    available * (1.0 - reserve) if available is not None else None
                )
                if wallet_bounded:
                    # Drop the fixed count cap: allow opening every ranked epic and
                    # let the per-epic wallet gate below stop once the spendable
                    # balance is exhausted. If the balance is unreadable we cannot
                    # size the wallet, so fall back to the count target — an API
                    # hiccup must never dump orders across the whole ranking.
                    slots = (
                        len(ranked)
                        if spendable is not None
                        else max(target - int(open_count), 0)
                    )
                    if slots <= 0:
                        return
                # A cooldown strategy opens at most one position per pass (the
                # cooldown gate above already ensured enough time has elapsed since
                # the last open); the next open waits for the next cooldown window.
                if cooldown > 0:
                    slots = min(slots, 1)
                funds_map = {
                    m.epic: m.funds_needed
                    for m in self._tradable_markets
                    if m.funds_needed is not None
                }

                trading = TradingService(
                    self._client, session, config, close_profile=self.close_profile
                )
                opened = 0
                # Notify mode only: candidates proposed to the user instead of
                # opened. Counted against ``slots`` exactly like a real open
                # would be, so the bot signals the best candidate(s) it would
                # have taken — not every ranked epic that happens to pass the
                # gates once nothing ever fills the book.
                signalled = 0
                gate_blocked = 0  # candidates rejected by the base open gates
                wallet_blocked = 0  # candidates the wallet could not cover
                for intent, buf in ranked:
                    if opened + signalled >= slots:
                        break
                    allowed, reason = await trading.can_open_intent(
                        intent, allow_short=allow_short
                    )
                    if not allowed:
                        gate_blocked += 1
                        logger.debug("Rolling select skip %s: %s", intent.epic, reason)
                        continue
                    # Wallet gate: only open while the available balance (minus the
                    # reserve) covers this epic's margin. Unknown margin -> allow.
                    need = funds_map.get(intent.epic)
                    if spendable is not None and need is not None and need > spendable:
                        wallet_blocked += 1
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
                    position, reason = await self.open_epic_guarded(
                        trading, intent, buf, allow_short=allow_short
                    )
                    if position:
                        opened += 1
                        if spendable is not None and need is not None:
                            spendable -= need
                        self._recorder.info(
                            f"Rolling open: {intent.epic} @ {position.level_open} "
                            f"(score={intent.score:.3f})"
                        )
                    elif reason == NOTIFY_REFUSAL:
                        signalled += 1
                        self._recorder.info(
                            f"Rolling signal: {intent.epic} "
                            f"(score={intent.score:.3f}) — your call"
                        )

                if opened:
                    logger.info("Rolling select: opened %d position(s)", opened)
                elif signalled:
                    logger.info(
                        "Rolling select: %d signal(s) raised (OPEN_MODE=notify)",
                        signalled,
                    )
                elif ranked:
                    # The tournament produced ranked candidates and had a free slot
                    # — it *wanted* to open — yet nothing was taken: every candidate
                    # failed a base/elementary open gate (market closed, closes soon,
                    # already open) or the wallet could not cover its margin. This is
                    # otherwise a silent no-op, so surface it, and report how many
                    # markets are still available to open on later today (untraded,
                    # not open, warmed up = ``evaluated`` minus what this pass took).
                    remaining = max(evaluated - opened, 0)
                    logger.warning(
                        "Rolling select [%s]: wanted to open but no candidate "
                        "passed the base checks — %d ranked, %d blocked by base "
                        "gates, %d by wallet; %d epic(s) still available to open "
                        "today",
                        strategy.name,
                        len(ranked),
                        gate_blocked,
                        wallet_blocked,
                        remaining,
                    )

    @staticmethod
    async def _realized_pnl_today(session: AsyncSession) -> float:
        """Euros **banked** today: the net result of today's closed positions.

        Feeds the entry risk ladder (``uses_risk_ladder``), whose whole premise is
        that a bigger bet must be funded by money already in the account — so open
        positions are deliberately excluded, however far in profit they are: an
        unrealised gain is not a gain.

        Mirrors the dashboard's daily P&L tile so the ladder and the screen can
        never disagree: the stored ``euro`` when it holds a real figure, a
        level-based recomputation as the fallback for rows that never got one, and
        ``never_opened`` phantoms (provisional rows whose order never confirmed at
        IG) dropped — they are not trades.
        """
        rows = (
            await session.scalars(
                select(Position).where(
                    Position.date == date.today(),
                    Position.state == PositionState.CLOSE,
                )
            )
        ).all()
        total = 0.0
        for position in rows:
            if position.reason_close == "never_opened":
                continue
            stored = float(position.euro) if position.euro is not None else None
            if stored is not None and abs(stored) >= 0.001:
                total += stored
                continue
            if position.level_open and position.level_close:
                move = float(position.level_close) - float(position.level_open)
                if position.direction == "SELL":
                    move = -move
                epp = (
                    float(position.euro_per_point)
                    if position.euro_per_point is not None
                    else 0.0
                )
                total += move * epp if epp else move * (position.quantity or 1)
            elif stored is not None:
                total += stored
        return total

    def _risk_estimates(
        self,
        ranked: list[tuple[EntryIntent, EpicBuffer]],
        config: TradeConfig,
    ) -> dict[str, float | None]:
        """Estimated worst-case euro loss per candidate epic.

        The same arithmetic the open path runs, one step ahead of it: the composed
        close profile chooses the candidate's initial stop from the live buffer
        (``initial_plan`` — the very call ``TradingService.open_position`` will
        make), the distance from the entry is floored by IG's padded minimum stop
        distance, and the result is converted with the epic's euro-per-point for
        one minimum deal size.

        An epic maps to ``None`` — never to 0 — whenever anything needed is
        missing (no market info, no euro-per-point, an empty buffer, a stop policy
        that raised). The caller must treat that as "unknown", not "free".

        This is an ESTIMATE, and knowingly so: ``euro_per_point`` is exact only for
        a euro-quoted instrument, the quantity assumed is the minimum deal size,
        and the stop is recomputed from the buffer as it stands now rather than as
        it will stand when the order lands.
        """
        markets = {m.epic: m for m in self._tradable_markets}
        margin = 1.0 + max(0.0, float(config.stop_min_distance_margin))
        estimates: dict[str, float | None] = {}
        for intent, buf in ranked:
            info = markets.get(intent.epic)
            last = buf.last
            if info is None or last is None or not info.euro_per_point:
                estimates[intent.epic] = None
                continue
            # The open path measures the stop distance from the bid, whatever the
            # direction (see ``TradingService.open_position``), so do the same.
            entry = last.bid_close
            try:
                plan = self.close_profile.initial_plan(
                    entry_level=entry,
                    direction=intent.direction,
                    buf=buf,
                )
            except Exception as exc:  # a stop policy must not break the selection
                logger.debug("Risk estimate failed for %s: %s", intent.epic, exc)
                estimates[intent.epic] = None
                continue
            risk = abs(entry - plan.stop_level) * float(info.euro_per_point)
            # Floor: IG refuses a stop inside its minimum distance, so a tighter
            # strategy stop is clamped out at open and really costs that minimum.
            # ``stop_loss_eur`` already prices that distance; pad it with the same
            # safety margin the open path applies.
            floor = (
                float(info.stop_loss_eur) * margin
                if info.stop_loss_eur is not None
                else 0.0
            )
            estimates[intent.epic] = max(risk, floor) or None
        return estimates

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
        """Run one monitoring pass, unless one is already in flight.

        Three callers reach this: the candle-driven trigger (the normal one), the
        30 s cron heartbeat, and the dashboard's manual "Run". Skipping rather than
        queueing is deliberate — a pass in flight is already reading the very data a
        new trigger would read, so waiting for it only to re-run on the same candles
        would duplicate work and risk two concurrent stop pushes for one position.

        A position is watched around the clock, for the whole time its own market is
        open (not only 08–18 UTC). The pass returns immediately when no position is
        open, so being triggered often is cheap.

        Equivalent to apiCheckPosition.php.
        """
        if self._monitor_lock.locked():
            logger.debug("Monitoring pass already running — trigger skipped")
            return
        async with self._monitor_lock:
            await self._monitor_positions_pass()

    async def _open_day_book(self, session: AsyncSession) -> DayBook:
        """The day's banked result and flat-cut bill, from today's closed rows.

        The one input the exit domain cannot derive from a curve: it is what makes
        "can today afford to close this stagnating position at a loss?" an
        arithmetic question rather than a guess (see
        :mod:`src.exit.zones.adaptive`). Opened once per monitor pass and then
        advanced position by position, so two positions closing in the same pass
        do not both spend the same budget.

        The split between the two figures — and why the flat cuts are kept out of
        the base rather than merely netted into it — is documented on
        :class:`~src.exit.zones.base.DayBook`. Phantom rows (``never_opened``) are
        dropped by the book itself, exactly as the dashboard drops them. Never
        raises: a read that fails degrades to an empty book (the conservative side
        — nothing is closed on affordability grounds) rather than breaking the pass.
        """
        try:
            result = await session.execute(
                select(Position.reason_close, Position.euro).where(
                    Position.date == date.today(),
                    Position.state == PositionState.CLOSE,
                )
            )
            return DayBook.from_closes(result.all())
        except Exception as exc:
            logger.warning("Could not read today's realized P&L: %s", exc)
            return DayBook()

    async def _monitor_positions_pass(self) -> None:
        """Check every open position and apply its close profile's decision."""
        config = self._build_trade_config()

        async with self._session_factory() as session:
            result = await session.execute(
                select(Position).where(Position.state == PositionState.OPEN)
            )
            positions = result.scalars().all()

            if not positions:
                # Nothing to value: drop the stale book figure so the dashboard
                # never shows a total for a book that no longer exists.
                self._group_book = None
                return

            trading = TradingService(
                self._client, session, config, close_profile=self.close_profile
            )

            # What the day has already secured, and what the flat cut has already
            # spent of it. Opened once here and advanced below as positions close,
            # so a budget spent earlier in this very pass is gone for the positions
            # managed after it (two stagnating positions used to close in the same
            # pass, each authorised against the same opening figure).
            day_book = await self._open_day_book(session)

            # Phase 1 — resolve the live bid + buffer for every open position.
            # Done up front so a group-aware profile can see the whole book before
            # any single position is managed (the group pre-pass below).
            resolved: list[tuple[Position, float, EpicBuffer | None]] = []
            for position in positions:
                try:
                    buf = self._buffer.get(position.epic)
                    if buf and buf.last:
                        current_bid = buf.last.bid_close
                    else:
                        # Fallback: fetch from API
                        market = await self._client.get(
                            f"/markets/{position.epic}", version=3
                        )
                        current_bid = float(market.get("snapshot", {}).get("bid", 0))
                    resolved.append((position, current_bid, buf))
                except Exception as exc:
                    logger.error(
                        "Error reading bid for position %s: %s", position.epic, exc
                    )

            # Group pre-pass — a portfolio-aware close profile (``smartgroup`` in
            # zone 1) decides the whole-book stop tightening once, from every open
            # position's live state. The decision logic lives in the exit domain;
            # this only assembles the members and hands the plan back per position.
            # An ordinary per-position profile yields an empty plan (no behaviour
            # change) since ``is_group_aware`` is False.
            group_plan: dict[int, float] = {}
            self._group_book = None
            if self.close_profile.is_group_aware:
                members = [
                    member
                    for position, current_bid, buf in resolved
                    if current_bid > 0
                    and (
                        member := self.close_profile.group_member(
                            position, current_bid, buf
                        )
                    )
                    is not None
                ]
                # The group decision is an arithmetic claim about the WHOLE book
                # ("all these stops together still book a gain"), so it is only
                # valid when every open position is in the sum. A position the
                # pre-pass could not price (no bid, no candle yet) would silently
                # drop its own — typically negative — contribution and make the
                # book look greener than it is, arming a tightening the group
                # cannot actually afford. Skip the plan entirely for that tick.
                if len(members) == len(positions):
                    report = self.close_profile.explain_group(members)
                    if report is not None:
                        group_plan = report.plan
                        # A disarmed plan leaves ``total_euro`` a partial sum that
                        # means nothing — only publish a full valuation.
                        if report.disarmed_by is None:
                            self._group_book = GroupBookSnapshot(
                                total_euro=report.total_euro,
                                gate_euro=report.gate_euro,
                                armed=report.armed,
                                at=datetime.now(UTC),
                            )
                        self._log_group_plan(report, resolved)
                else:
                    logger.warning(
                        "Group stop pre-pass skipped: only %d of %d open positions "
                        "could be priced this tick",
                        len(members),
                        len(positions),
                    )

            # Phase 2 — manage each position, feeding it its own group decision.
            for position, current_bid, buf in resolved:
                try:
                    if current_bid > 0:
                        closed = await trading.manage_position(
                            position,
                            current_bid,
                            buf=buf,
                            group_tighten=group_plan.get(position.id),
                            day_book=day_book,
                        )
                        if closed:
                            # Charge this close to the day's book straight away:
                            # the next position in the pass must see the budget
                            # this one consumed, not the opening snapshot.
                            day_book = day_book.after_close(
                                position.reason_close, position.euro
                            )
                            self._recorder.info(
                                f"Position closed: {position.epic} "
                                f"reason={position.reason_close} "
                                f"P&L={position.euro}€"
                            )
                            # Recovery revert: the software backstop fired on the
                            # stop this position was opened with — take the
                            # opposite side at once (see
                            # :meth:`_revert_after_stop_loss`; a no-op unless
                            # ALLOW_RECOVERY_REVERT is on and the close qualifies).
                            await self._revert_after_stop_loss(
                                session, position, config
                            )
                except Exception as exc:
                    logger.error("Error monitoring position %s: %s", position.epic, exc)

    @staticmethod
    def _log_group_plan(report, resolved: list[tuple]) -> None:
        """Trace the whole-book stop decision taken by ``smartgroup`` this tick.

        The group gate is an arithmetic claim about the **book** ("closing every
        position at its own candidate stop still banks a net gain"), so a tick where
        no stop moves looks exactly like the pre-pass never running. This makes the
        difference visible: one INFO line per pass with the estimated book total,
        the gate it had to clear, and the live unrealized total for comparison — the
        live P&L is always the more optimistic of the two, since every candidate
        sits one noise cushion the wrong side of its price. Per-position arithmetic
        goes to DEBUG.

        Purely observational: nothing here influences the plan.
        """
        epics = {p.id: p.epic for p, _bid, _buf in resolved}
        live_euro = sum(float(p.euro or 0) for p, _bid, _buf in resolved)

        if report.disarmed_by is not None:
            logger.info(
                "Group stop plan: DISARMED — position %s (%s) has no stop and cannot "
                "be tightened, so the book cannot be claimed green (live %.2f€)",
                report.disarmed_by,
                epics.get(report.disarmed_by, "?"),
                live_euro,
            )
            return

        movable = sum(1 for v in report.valuations if v.tightens)
        logger.info(
            "Group stop plan: %s — book at planned stops %.2f€ vs gate %.2f€ "
            "(live %.2f€), %d/%d position(s) movable%s",
            "ARMED" if report.armed else "hold",
            report.total_euro,
            report.gate_euro,
            live_euro,
            movable,
            len(report.valuations),
            f", tightening {len(report.plan)}" if report.plan else "",
        )
        if report.unpriceable:
            logger.warning(
                "Group stop plan: %d position(s) not priced (no size or no open "
                "level): %s",
                len(report.unpriceable),
                [epics.get(pid, pid) for pid in report.unpriceable],
            )
        for v in report.valuations:
            logger.debug(
                "Group member %s (%s): candidate=%.5f follower=%.5f -> valued at "
                "%.5f = %.2f€ (%s)",
                v.position_id,
                epics.get(v.position_id, "?"),
                v.candidate,
                v.level_follower,
                v.valued_at,
                v.euro,
                "tighten" if v.tightens else "keeps its stop",
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

        It is also where a **broker-side stop-out** is first seen by the bot (the
        IG order fires on a tick the monitor never polls), so each position the
        sync reconciled as ``closed_externally`` is offered to the recovery-revert
        rule (:meth:`_revert_after_stop_loss`) — a no-op unless
        ``ALLOW_RECOVERY_REVERT`` is on and the close hit the stop placed at open.
        """
        # Single-flight: a manual dashboard trigger must not run a second sync
        # concurrently with the scheduled one (see ``self._sync_lock``).
        async with self._sync_lock:
            config = self._build_trade_config()
            async with self._session_factory() as session:
                trading = TradingService(self._client, session, config)
                try:
                    live = await trading.sync_open_positions()
                except Exception as exc:
                    logger.error("Position sync failed: %s", exc)
                    return
                for closed in trading.reconciled_closed:
                    await self._revert_after_stop_loss(session, closed, config)
            # Stamp the "as of" time only on a successful sync so the dashboard
            # can show when the displayed P&L figures were last refreshed from IG.
            self._positions_synced_at = datetime.now(UTC)
            if live:
                logger.debug("Position sync: %d position(s) live at IG", len(live))

    async def _end_of_day(self) -> None:
        """Force close ALL open positions (MANUAL dashboard action only).

        No longer scheduled: the automatic end-of-day sweep was removed so
        positions close on each epic's own market close, never on a hard global
        hour. Kept as a human "close everything now" override, then reconciles
        P&L with IG's authoritative figures.
        """
        logger.info("End of day: closing all positions")
        await self._force_close_all()

    async def _force_close_all(self) -> int:
        """Close every open position at market price and reconcile the P&L.

        Shared by the ``end_of_day`` action and the dashboard "End day" button;
        returns the number of positions closed so the caller can report it. The
        reconcile replaces the just-recorded estimates with IG's authoritative
        figures.
        """
        config = self._build_trade_config()

        async with self._session_factory() as session:
            trading = TradingService(self._client, session, config)
            closed = await trading.close_all_positions()

        self._recorder.info(f"End of day: {closed} positions force-closed")

        await self._reconcile_pnl()
        return closed

    async def end_day(self) -> int:
        """Stop trading for the day: block openings, then close the whole book.

        The dashboard's hard stop. Openings are blocked *first* so the analysis
        loop cannot slip a new position in while the book is being closed, then
        every live position is force-closed. Returns the number of positions
        closed.
        """
        logger.info("End day requested: blocking openings and closing everything")
        await self.set_auto_open(False)
        # Proposals die with the day: a signal raised minutes ago must not keep
        # inviting an open on a book the user has just decided to empty.
        self._signals.clear()
        closed = await self._force_close_all()
        self._recorder.info(
            f"Day ended by the user — {closed} position(s) closed, no further opening"
        )
        return closed

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

    async def _resync_day_history(self, days: int = 30) -> dict:
        """Rebuild the last ``days`` days of the Day summary table from IG.

        Unlike :meth:`_daily_summary` (today only), this re-reconciles every
        past day that has closed positions — pulling each deal's authoritative
        ``profitAndLoss`` from ``GET /history/transactions`` via
        :meth:`TradingService.reconcile_realized_pnl` — and upserts its Day row
        from the corrected figures. Powers the dashboard "Resync" button on the
        Daily History section.

        Only days that actually hold closed positions are queried, so a mostly
        idle month costs a handful of API calls, not one per calendar day.

        Returns a summary dict ``{"days", "updated", "total"}`` where ``days`` is
        the number of days rebuilt, ``updated`` the count of positions whose P&L
        was corrected, and ``total`` the summed euro P&L over those days.
        """
        today = date.today()
        since = today - timedelta(days=days)
        config = self._build_trade_config()
        total_updated = 0
        total_euro = 0.0
        processed = 0

        async with self._session_factory() as session:
            trading = TradingService(self._client, session, config)
            # Only reconcile days that actually have closed positions.
            date_res = await session.execute(
                select(Position.date)
                .where(
                    Position.date >= since,
                    Position.state == PositionState.CLOSE,
                )
                .distinct()
            )
            dates = sorted({d for (d,) in date_res.all() if d is not None})

            for day in dates:
                try:
                    total_updated += await trading.reconcile_realized_pnl(day)
                except Exception as exc:
                    logger.error("Day history resync failed for %s: %s", day, exc)
                    continue

                # Recompute the Day summary from the freshly reconciled positions.
                pos_res = await session.execute(
                    select(Position).where(
                        Position.date == day,
                        Position.state == PositionState.CLOSE,
                    )
                )
                positions = [
                    p
                    for p in pos_res.scalars().all()
                    if p.reason_close != "never_opened"
                ]
                euro_total = sum(float(p.euro or 0) for p in positions)
                euro_list = (
                    ",".join(f"{p.epic}:{p.euro}" for p in positions)
                    if positions
                    else ""
                )

                day_res = await session.execute(select(Day).where(Day.date == day))
                day_record = day_res.scalar_one_or_none()
                if day_record:
                    # Past days are settled; leave today's state untouched so a
                    # prior end-of-day run isn't reverted to OPEN.
                    if day < today:
                        day_record.state = DayState.CLOSE
                    day_record.euro_total = Decimal(str(round(euro_total, 3)))
                    day_record.euro_list = euro_list
                else:
                    session.add(
                        Day(
                            date=day,
                            state=DayState.CLOSE if day < today else DayState.OPEN,
                            euro_total=Decimal(str(round(euro_total, 3))),
                            euro_list=euro_list,
                        )
                    )
                await session.commit()
                total_euro += euro_total
                processed += 1

        logger.info(
            "Day history resync: %d day(s), %d position(s) corrected, total %.2f€",
            processed,
            total_updated,
            total_euro,
        )
        self._recorder.info(
            f"Day history resync: {processed} day(s), "
            f"{total_updated} position(s) corrected, total {total_euro:.2f}€"
        )
        return {
            "days": processed,
            "updated": total_updated,
            "total": round(total_euro, 2),
        }

    async def _daily_reset(self) -> None:
        """Drop stale candles from the price buffer, once per trading day.

        Runs once a day shortly after the server starts (see
        ``_register_startup_jobs``) instead of on a midnight cron: the old cron
        fired while 24h markets (forex/CFD) were still trading and left the
        strategy running on a truncated window until the feed had refilled the
        warm-up.

        The clear is immediately followed by a reload of today's candles from the
        ``candle`` table, so a mid-session restart keeps the history it already
        had — only genuinely stale rows are dropped.
        """
        self._buffer.clear()
        logger.info("Daily reset: price buffer cleared")
        if self._tradable_epics and self._candle_store is not None:
            await self._rehydrate_buffer(self._tradable_epics)
        await self._record_job_run("daily_reset")

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
