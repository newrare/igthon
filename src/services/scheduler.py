"""Scheduler service — replaces CRON scripts.

Uses APScheduler to run periodic tasks:
- Price collection and analysis (every 30s-2min)
- Position monitoring (every 30s)
- End-of-day forced close and summary
- Daily/weekly summaries
- Daily epic list refresh from IG navigation tree (7:30 AM)
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

from src.api.client import IGAPIError, IGClient
from src.config import Settings
from src.models.day import Day, DayState
from src.models.epic import Epic
from src.models.position import Position, PositionState
from src.models.resume import Resume
from src.services.api_queue import APIQueue
from src.services.candle_store import CandleStore
from src.services.compute import compute_signal
from src.services.market_data import MarketDataService
from src.services.market_scanner import MarketInfo, MarketScanner
from src.services.price_buffer import PriceBuffer
from src.services.recorder import Recorder
from src.services.trading import TradeConfig, TradingService

if TYPE_CHECKING:
    from src.api.streaming import IGStreamingClient

logger = logging.getLogger(__name__)


class BotScheduler:
    """Manages all scheduled tasks for the trading bot.

    Replaces the PHP CRON-based architecture with in-process scheduling.

    Epic list lifecycle:
    - ``_all_epics``: full deduplicated list discovered from the IG navigation
      tree, refreshed once daily at 07:30.
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

        # Epic lists — start with the provided seed list
        self._all_epics: list[str] = list(epics)
        self._tradable_epics: list[str] = list(epics)
        self._tradable_markets: list[MarketInfo] = []
        self._tradable_last_refresh: datetime | None = None
        self._epic_last_refresh: datetime | None = None
        # Epics that returned 403 on /prices — skipped until next hourly refresh
        self._pricing_blacklist: set[str] = set()
        # Epics whose buffer has been bootstrapped (polling path only).
        # Cleared at the daily reset so each epic fetches 50 candles once per day.
        self._bootstrapped_epics: set[str] = set()

        self._scanner = MarketScanner(
            client=client,
            settings=settings,
        )

    def start(self) -> None:
        """Start the scheduler with all configured jobs."""
        # Daily navigation tree crawl at 07:30 (before market open)
        self._scheduler.add_job(
            self._refresh_epic_list,
            "cron",
            day_of_week="mon-fri",
            hour=7,
            minute=30,
            id="refresh_epic_list",
            name="Daily epic list refresh (navigation tree)",
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

        # End of day: force close + summary
        self._scheduler.add_job(
            self._end_of_day,
            "cron",
            day_of_week="mon-fri",
            hour=self._settings.strategy_hour_close,
            minute=30,
            id="end_of_day",
            name="End of day close",
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

        # Trigger an immediate navigation tree crawl on startup so we don't wait
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
        self.pause_bot()  # Start paused — user must resume via web dashboard
        logger.info(
            "Scheduler started in paused state — %d seed epics — resume via web dashboard",
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
        """Return True when all scheduled jobs are suspended."""
        return self._paused

    def pause_bot(self) -> None:
        """Suspend all scheduled jobs — no API calls will fire until resumed."""
        if self._running and not self._paused:
            self._scheduler.pause()
            self._paused = True
            logger.info("Bot paused — all scheduled jobs suspended")

    def resume_bot(self) -> None:
        """Resume all suspended scheduled jobs."""
        if self._paused:
            self._scheduler.resume()
            self._paused = False
            logger.info("Bot resumed — all scheduled jobs active")

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

    async def trigger_monitor_positions(self) -> None:
        """Manually run one position monitoring pass."""
        await self._monitor_positions()

    async def trigger_end_of_day(self) -> None:
        """Manually trigger end-of-day force close."""
        await self._end_of_day()

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
        """Full deduplicated epic list from the last navigation tree crawl."""
        return list(self._all_epics)

    @property
    def epic_last_refresh(self) -> datetime | None:
        """UTC timestamp of the last successful navigation tree crawl."""
        return self._epic_last_refresh

    # ------------------------------------------------------------------
    # Epic list persistence (survives restarts)
    # ------------------------------------------------------------------

    async def load_persisted_state(self) -> None:
        """Restore the epic list from the database on startup.

        The navigation-tree crawl is expensive and runs at most once per day, so
        its result is persisted to the ``epic`` table. Loading it here means a
        restart keeps the day's discovered epics instead of falling back to the
        small seed list. ``_epic_last_refresh`` is restored from the newest
        ``updated_at`` so the dashboard KPI still reflects freshness.
        """
        try:
            async with self._session_factory() as session:
                names = list(
                    (await session.scalars(select(Epic.name).order_by(Epic.name))).all()
                )
                last = (
                    await session.scalars(select(func.max(Epic.updated_at)))
                ).one_or_none()
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

    async def _persist_epic_list(
        self, epics: list[str], refreshed_at: datetime
    ) -> None:
        """Replace the persisted epic list with the latest crawl result.

        Existing rows are pruned and the new list inserted so the table always
        mirrors the current day's navigation-tree crawl. Enrichment columns
        (description/type/deposit) are intentionally not touched here.
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

    def _build_trade_config(self) -> TradeConfig:
        """Create TradeConfig from current settings."""
        return TradeConfig.from_settings(self._settings)

    # ------------------------------------------------------------------
    # Epic list management
    # ------------------------------------------------------------------

    async def _refresh_epic_list(self) -> None:
        """Crawl the full IG navigation tree and rebuild the epic list.

        Runs at 07:30 daily. Results are deduplicated and stored in
        ``_all_epics``. The tradable list is NOT updated here — that happens
        separately via ``_refresh_tradable_epics`` at 08:00.
        """
        logger.info("Starting daily epic list refresh (navigation tree crawl)")
        try:
            epics = await self._scanner.get_tradeable_epics()
        except Exception as exc:
            logger.error("Epic list refresh failed: %s", exc)
            return

        if not epics:
            logger.warning("Navigation tree returned 0 epics — keeping current list")
            return

        self._all_epics = epics
        self._epic_last_refresh = datetime.now(UTC)
        await self._persist_epic_list(epics, self._epic_last_refresh)
        logger.info(
            "Epic list refreshed: %d epics discovered from navigation tree",
            len(epics),
        )
        self._recorder.info(
            f"Daily epic refresh: {len(epics)} epics in navigation tree"
        )

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
            tradeable = await self._scanner.get_tradeable_markets(self._all_epics)
        except Exception as exc:
            logger.error("Tradable epic refresh failed: %s", exc)
            return

        # IG caps Lightstreamer at 40 subscriptions per connection. When streaming
        # is active, keep only the tightest-spread markets (best liquidity proxy,
        # already computed by the scanner — no extra API call) up to that cap.
        if (
            self._streaming is not None
            and len(tradeable) > self._settings.streaming_max_epics
        ):
            dropped = len(tradeable) - self._settings.streaming_max_epics
            tradeable = sorted(tradeable, key=lambda m: m.spread_ratio)[
                : self._settings.streaming_max_epics
            ]
            logger.info(
                "Tradable epics capped to %d (tightest spread) — dropped %d over the "
                "IG streaming limit",
                self._settings.streaming_max_epics,
                dropped,
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

        if self._streaming is not None:
            await self._streaming.set_epics(self._tradable_epics)
        else:
            # Legacy polling path reseeds the 50-candle buffer once per hour.
            self._bootstrapped_epics.clear()

    # ------------------------------------------------------------------
    # Scheduled tasks
    # ------------------------------------------------------------------

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

        config = self._build_trade_config()
        epics = [e for e in self._tradable_epics if e not in self._pricing_blacklist]
        if not epics:
            logger.debug("No tradable epics to analyze")
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
                if epic not in self._bootstrapped_epics:
                    candles = await self._market_data.fetch_candles(epic, "MINUTE", 50)
                    self._bootstrapped_epics.add(epic)
                else:
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

        # Phase 2 — compute signals and act; all I/O is already done.
        for epic, error in results:
            if error is not None:
                if isinstance(error, IGAPIError):
                    if error.response.status_code == 403:
                        logger.warning(
                            "Prices 403 for %s (IG code: %s) — blacklisted until next hourly refresh",
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

    async def _evaluate_epic(self, epic: str, config: TradeConfig) -> None:
        """Compute the signal for one epic from its buffer and act on a BUY."""
        buf = self._buffer.get(epic)
        if not buf or len(buf) < self._settings.strategy_sma_slow:
            return
        signal = compute_signal(
            epic,
            buf,
            regression_period=self._settings.strategy_lookback_points,
            sma_fast_period=self._settings.strategy_sma_fast,
            sma_slow_period=self._settings.strategy_sma_slow,
            roc_period=self._settings.strategy_roc_period,
            min_r2=self._settings.strategy_min_r2,
            min_score=self._settings.strategy_min_score,
            max_spread_ratio=self._settings.strategy_max_spread_ratio,
            follower_mult=self._settings.strategy_stop_multiplier,
            win_mult=self._settings.strategy_target_multiplier,
            loose_mult=self._settings.strategy_stop_multiplier * 3,
            security_mult=self._settings.strategy_stop_multiplier * 2,
            tactic=self._settings.strategy_tactic,
        )
        if signal and signal.direction == "BUY":
            logger.info(
                "BUY signal: %s (score=%.2f, R²=%.2f)",
                epic,
                signal.score,
                signal.regression.r_squared,
            )
            async with self._session_factory() as session:
                trading = TradingService(self._client, session, config)
                allowed, reason = await trading.can_open_position(signal)
                if allowed:
                    position = await trading.open_position(signal)
                    if position:
                        self._recorder.info(
                            f"Position opened: {epic} @ {position.level_open}"
                        )
                else:
                    logger.debug("Cannot open %s: %s", epic, reason)

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

            trading = TradingService(self._client, session, config)

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
                        closed = await trading.check_and_close(position, current_bid)
                        if closed:
                            self._recorder.info(
                                f"Position closed: {position.epic} "
                                f"reason={position.reason_close} "
                                f"P&L={position.euro}€"
                            )
                except Exception as exc:
                    logger.error("Error monitoring position %s: %s", position.epic, exc)

    async def _end_of_day(self) -> None:
        """Force close all positions and generate daily summary."""
        logger.info("End of day: closing all positions")

        config = self._build_trade_config()

        async with self._session_factory() as session:
            trading = TradingService(self._client, session, config)
            closed = await trading.close_all_positions()

        self._recorder.info(f"End of day: {closed} positions force-closed")

    async def _daily_summary(self) -> None:
        """Generate or update daily summary in the Day table."""
        today = date.today()

        async with self._session_factory() as session:
            # Get all closed positions for today
            result = await session.execute(
                select(Position).where(
                    Position.date == today,
                    Position.state == PositionState.CLOSE,
                )
            )
            positions = result.scalars().all()

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

    async def _daily_reset(self) -> None:
        """Reset price buffer at start of new day."""
        self._buffer.clear()
        # Force a fresh seed on the new day (the previous day's candles fall
        # outside the rehydrate window anyway).
        self._bootstrapped_epics.clear()
        logger.info("Daily reset: buffer + bootstrap cache cleared")

    async def _dump_and_purge_candles(self) -> None:
        """Export candles past the retention window to disk, then delete them."""
        if self._candle_store is None:
            return
        try:
            count, path = await self._candle_store.dump_and_purge()
        except Exception as exc:
            logger.error("Candle dump/purge failed: %s", exc)
            return
        if count:
            self._recorder.info(f"Candle retention: dumped {count} rows to {path}")
