"""Trading service — open/close positions. Ported from Action.php.

Implements the full trading workflow:
- Pre-open checks (market status, duplicates, stop limits, risk)
- Position opening via the IG API
- Position monitoring and closing (win/follower/loose strategies)
- Stop level updates (trailing stop)
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.api.client import IGAPIError, IGClient
from src.core.api_queue import (
    MARKET_ORDER_NOT_SUPPORTED_CODE,
    APIQueue,
    Priority,
)
from src.core.indicators import RegressionResult, TradingLevels, TradingSignal, atr
from src.entry.base import EntryIntent
from src.execution.gates import evaluate_open_gates
from src.exit.base import ACTION_CLOSE, ACTION_UPDATE_STOP, CloseProfile
from src.exit.trailing import (
    clamp_trailing_distance,
    compute_trailing_stop,
    decide_close_reason,
)
from src.feed.price_buffer import EpicBuffer
from src.models.epic import Epic
from src.models.position import Position, PositionState, PositionStrategy
from src.utils.tools import (
    _parse_ig_utc_time,
    _to_float,
    euro_per_point,
    parse_ig_pnl,
)

logger = logging.getLogger(__name__)

# Grace window (seconds) granted to a freshly-opened position whose ``deal_id``
# never bound before the sync job reconciles it. The open path writes a
# provisional row (``deal_id=None``) before ``/confirms`` lands; if that confirm
# fails the row stays unbound and ``GET /positions`` may not list the new
# position yet (IG eventual consistency). Reconciling it immediately would close
# it as a phantom ``closed_externally`` trade at €0. Within this window the sync
# leaves it alone so the epic-level fallback can bind it on a later tick; past
# it, an unbound-and-absent row was never genuinely opened (see
# ``_mark_never_opened``). Comfortably covers a few 20s sync cycles.
RECONCILE_GRACE_SECONDS = 60.0

# Widest gap (seconds) tolerated between a closed position's own clock
# (``time_open`` / ``time_close``) and an IG transaction's UTC execution
# timestamps for the two to be considered the same deal
# (``TradingService._match_cost``). The bot detects a close within one 20s sync
# cycle, so a real pair sits well under a minute apart; the allowance is
# deliberately generous but finite — a position IG has no transaction for must
# stay unmatched rather than steal another deal's P&L, which is exactly how a
# morning loss ended up displaying the afternoon's gain.
RECONCILE_MATCH_MAX_SECONDS = 900.0

# ``MARKET_ORDER_NOT_SUPPORTED_CODE`` (imported from api_queue): the IG code for
# an epic that rejects ``orderType: "MARKET"`` (typically forwards). The metadata
# does not always flag it up front, so a MARKET order can bounce at deal time —
# the open path then retries as a marketable LIMIT priced through the touch.

# Deal-confirmation polling. IG resolves a dealReference asynchronously, so
# ``GET /confirms/{ref}`` can 404 for a short window right after the order POST.
# The APIQueue does not retry 4xx, so ``open_position`` polls the confirm itself:
# ``CONFIRM_MAX_ATTEMPTS`` tries spaced by a linear backoff of
# ``CONFIRM_RETRY_DELAY_SECONDS * attempt``. Kept short — a deal is normally
# resolvable within ~1 s, and the sync job is the backstop for anything slower.
CONFIRM_MAX_ATTEMPTS = 4
CONFIRM_RETRY_DELAY_SECONDS = 0.4

# Wall-clock market-close hour (UTC) used ONLY by the backtest simulator, which
# has no live ``marketStatus`` to read and no per-epic ``Epic.market_close_utc``
# to consult. The LIVE close path never uses it: it is driven solely by each
# epic's own market close (see :meth:`TradingService._is_epic_close_hour`), with
# no hard global fallback.
DEFAULT_MARKET_CLOSE_HOUR_UTC = 17

# Re-exported for backward compatibility: these pure close helpers now live in
# the exit domain (src/exit/trailing.py). Existing importers
# (``from src.execution.trading import decide_close_reason``) keep working.
__all__ = [
    "TradeConfig",
    "TradingService",
    "clamp_trailing_distance",
    "compute_trailing_stop",
    "decide_close_reason",
    "evaluate_open_gates",
]


@dataclass(slots=True)
class TradeConfig:
    """Trading configuration parameters."""

    # Wall-clock close hour (UTC) — used ONLY by the backtest simulator (no live
    # marketStatus / per-epic close). The live close path ignores it.
    hour_close: int = DEFAULT_MARKET_CLOSE_HOUR_UTC
    # Minutes before a market's own close at which an open position on it is
    # force-closed (per-epic close rule, applied to Epic.market_close_utc). When
    # the close time is unknown, no time-based force-close happens at all.
    close_margin_minutes: int = 5
    # Do not open a new position when the epic's own market closes within this
    # many minutes (added on top of ``close_margin_minutes``). Only applies when
    # the epic's close time is known.
    open_close_buffer_minutes: int = 60
    # Global same-day re-open policy (``ALLOW_SAME_DAY_REOPEN`` in .env), applied
    # to EVERY open strategy. False = an epic that already had an opening today
    # (BUY or SELL, still open or closed) cannot be opened again until tomorrow;
    # True = it is eligible again as soon as it holds no open position. Concurrent
    # duplicates are always blocked regardless. Defaults to True here so callers
    # that build a bare ``TradeConfig`` (tests, ad-hoc scripts) keep the historic
    # "only concurrent duplicates are blocked" behaviour; the live path always
    # goes through :meth:`from_settings`, where .env decides.
    allow_same_day_reopen: bool = True
    # Fraction added on top of IG's minimum stop distance when clamping an order's
    # protective stop, so a fast-moving market can't push the stop back under
    # IG's floor between the market snapshot and the order ("Stop trop près").
    stop_min_distance_margin: float = 0.15
    # Slippage buffer (fraction of price) applied when an open falls back from a
    # MARKET order to a marketable LIMIT on an epic that rejects MARKET orders.
    # The limit is priced this far THROUGH the current touch (above the ask for a
    # BUY, below the bid for a SELL) so ``EXECUTE_AND_ELIMINATE`` fills the whole
    # size at the best available price; the buffer only caps acceptable slippage,
    # it is never the fill price. Default 0.2%.
    market_order_limit_slippage: float = 0.002
    # Trailing stop (ATR-based adaptive follower)
    atr_period: int = 14
    atr_k_pre: float = 2.5
    atr_k_post: float = 1.5
    trailing_step_ratio: float = 0.3
    # Noise cushion between the software follower and the broker stop, as a
    # fraction of the current ATR, added on top of one spread. See
    # ``TradingService._broker_stop_level`` / ``_broker_stop_buffer``. 0 keeps
    # the broker stop exactly one spread beyond the follower (legacy behaviour).
    broker_stop_noise_atr: float = 0.5

    @classmethod
    def from_settings(cls, settings) -> "TradeConfig":
        """Build TradeConfig from application Settings."""
        return cls(
            close_margin_minutes=settings.strategy_close_margin_minutes,
            open_close_buffer_minutes=getattr(
                settings, "strategy_open_close_buffer_minutes", 60
            ),
            # ``None`` means the .env line is missing; startup validation already
            # refuses that, so treat it as the strict policy here (fail closed).
            allow_same_day_reopen=bool(
                getattr(settings, "allow_same_day_reopen", False)
            ),
            stop_min_distance_margin=getattr(
                settings, "strategy_stop_min_distance_margin", 0.15
            ),
            market_order_limit_slippage=getattr(
                settings, "strategy_market_order_limit_slippage", 0.002
            ),
            atr_period=settings.strategy_atr_period,
            atr_k_pre=settings.strategy_atr_k_pre,
            atr_k_post=settings.strategy_atr_k_post,
            trailing_step_ratio=settings.strategy_trailing_step_ratio,
            broker_stop_noise_atr=getattr(
                settings, "strategy_broker_stop_noise_atr", 0.5
            ),
        )


class TradingService:
    """Service for opening and closing trading positions.

    Ported from Action.php with improved validation and async support.
    """

    def __init__(
        self,
        client: IGClient | APIQueue,
        db_session: AsyncSession,
        config: TradeConfig,
        close_profile: CloseProfile | None = None,
    ) -> None:
        self._client = client
        self._db = db_session
        self._config = config
        # The close profile owns every exit decision for positions opened
        # through ``open_from_intent`` / managed by ``manage_position``. It is
        # chosen independently of the entry strategy (open/close decoupling).
        self._close_profile = close_profile
        # Dedicated exit for SELL positions (built lazily). The main close profile
        # is long-only; a short is routed here by ``manage_position`` regardless of
        # the configured long profile.
        # Epics that bounced a MARKET order with
        # ``MARKET_ORDER_NOT_SUPPORTED_CODE`` at deal time even though their
        # metadata did not flag it. IG's ``marketOrderPreference`` is an unreliable
        # hint for these (typically forwards), so once an epic proves it we open it
        # with a marketable LIMIT directly — the doomed MARKET (and its ERROR-level
        # queue log / persistent error entry) then happens at most once per epic
        # per process instead of on every scan.
        self._market_order_unsupported: set[str] = set()
        # Positions the last :meth:`sync_open_positions` run reconciled as
        # ``closed_externally`` — the broker-side stop (or a close made outside
        # the bot) took them out. Exposed so the orchestration layer can react to
        # a broker stop-out it never saw itself, chiefly the recovery-revert rule
        # (see ``BotScheduler._revert_after_stop_loss``). Reset on every sync, so
        # it only ever holds the closes of the most recent run.
        self.reconciled_closed: list[Position] = []

    async def _is_epic_open(self, epic: str) -> bool:
        """Check if a position is already open for this epic."""
        result = await self._db.execute(
            select(Position).where(
                Position.epic == epic,
                Position.state == PositionState.OPEN,
            )
        )
        return result.scalar_one_or_none() is not None

    async def _is_epic_traded_today(self, epic: str) -> bool:
        """True when this epic already had an opening today (any state/direction).

        Backs the global ``ALLOW_SAME_DAY_REOPEN`` policy: keyed on
        ``Position.date`` (the trading day stamped at open) and direction-agnostic,
        so a BUY *and* a SELL count as the same "the epic was used today".
        """
        result = await self._db.execute(
            select(Position.id).where(
                Position.epic == epic,
                Position.date == date.today(),
            )
        )
        return result.first() is not None

    async def _epic_close_utc(self, epic: str) -> time | None:
        """The epic's own market close (UTC) from the Epic table, or None."""
        try:
            result = await self._db.scalar(
                select(Epic.market_close_utc).where(Epic.name == epic)
            )
        except Exception:  # pragma: no cover - defensive (fall back to global)
            return None
        return result if isinstance(result, time) else None

    async def _epic_minutes_to_close(self, epic: str) -> float | None:
        """Minutes from now until the epic's own market close (UTC), or None.

        Returns None when the epic exposes no close time (a 24h market such as
        forex, or a market currently closed for which IG returns no
        ``openingHours``). Callers apply no time-based rule at all on None — there
        is deliberately no hard global fallback.
        """
        close_t = await self._epic_close_utc(epic)
        if close_t is None:
            return None
        now = datetime.now(UTC)
        close_dt = datetime.combine(now.date(), close_t, tzinfo=UTC)
        return (close_dt - now).total_seconds() / 60.0

    async def _is_epic_close_hour(self, epic: str) -> bool:
        """True when a position on ``epic`` should be force-closed for the day.

        Driven solely by the epic's own market close (``Epic.market_close_utc``)
        minus the configured margin, so the position is closed just before that
        market actually closes. When the close time is unknown there is NO hard
        fallback: the position is left to its broker-side stop rather than
        force-closed on a guessed global hour.
        """
        minutes = await self._epic_minutes_to_close(epic)
        if minutes is None:
            return False
        return minutes <= self._config.close_margin_minutes

    async def _is_epic_close_soon(self, epic: str) -> bool:
        """True when the epic's market closes within the pre-open buffer.

        Guards the open side: a position opened just before the per-epic close
        rule fires would be force-closed almost immediately, paying the spread
        for nothing. Blocks when the market closes within
        ``close_margin_minutes + open_close_buffer_minutes``. An unknown close
        time (24h market, or a market we could not open anyway) never blocks.
        """
        minutes = await self._epic_minutes_to_close(epic)
        if minutes is None:
            return False
        threshold = (
            self._config.close_margin_minutes + self._config.open_close_buffer_minutes
        )
        return minutes <= threshold

    async def can_open_intent(
        self,
        intent: EntryIntent,
        *,
        allow_short: bool = False,
        allow_reopen: bool = False,
    ) -> tuple[bool, str]:
        """Pre-open gates for a decoupled :class:`EntryIntent`.

        The live market-open gate is the per-epic ``marketStatus == TRADEABLE``
        check (hourly tradable filter + re-check in :meth:`open_position`), not a
        wall-clock window, so ``in_trading_hours`` is always True. The simulator
        keeps its own hour gate (no live status to read).

        A ``closes_soon`` gate additionally rejects the open when the epic's own
        market closes within ``open_close_buffer_minutes`` (see
        :meth:`_is_epic_close_soon`), so we never open a trade the per-epic close
        rule would force-close almost immediately.

        A ``same-day re-open`` gate rejects the open when the global
        ``ALLOW_SAME_DAY_REOPEN`` policy is off and the epic already had an
        opening today (see :meth:`_is_epic_traded_today`). It applies to every
        open strategy, both directions.

        ``allow_short`` lifts the long-only gate for a manual dashboard SELL;
        ``allow_reopen`` lifts the same-day re-open gate the same way (an
        explicit human open is never blocked by the diversity policy). Automatic
        callers leave both ``False``.
        """
        traded_today = (
            not allow_reopen
            and not self._config.allow_same_day_reopen
            and await self._is_epic_traded_today(intent.epic)
        )
        return evaluate_open_gates(
            epic=intent.epic,
            direction=intent.direction,
            in_trading_hours=True,
            epic_already_open=await self._is_epic_open(intent.epic),
            closes_soon=await self._is_epic_close_soon(intent.epic),
            allow_short=allow_short,
            epic_traded_today=traded_today,
        )

    async def open_from_intent(
        self,
        intent: EntryIntent,
        buf: EpicBuffer,
        *,
        close_profile: CloseProfile | None = None,
        quantity_multiplier: int = 1,
    ) -> Position | None:
        """Open a position from a decoupled entry intent + close profile.

        This is the decoupled open path: the entry strategy supplied only a
        direction (``intent``); the **close profile** chooses the initial
        protective stop (and any take-profit) via ``initial_plan``. The two are
        composed here and never reference each other.

        The chosen levels are adapted into the existing :class:`TradingSignal`
        pipeline so all the order placement, dealing-rule validation, sizing,
        confirmation and DB-record logic in :meth:`open_position` is reused
        unchanged; the resulting position is stamped with the close profile's
        name so :meth:`manage_position` keeps using the same exit for its life.

        Both sides use the same close profile: it is direction-aware and mirrors
        every reference for a SELL (stop above entry, margin below break-even).
        """
        profile: CloseProfile | None = close_profile or self._close_profile
        if profile is None:
            raise ValueError("open_from_intent requires a close profile")

        last = buf.last
        if last is None:
            logger.info("No candle for %s — cannot open", intent.epic)
            return None

        plan = profile.initial_plan(
            entry_level=last.bid_close, direction=intent.direction, buf=buf
        )

        # Adapt the (intent, plan) pair to a TradingSignal. The close profile
        # owns the single protective stop, so follower/loose/security collapse
        # to ``plan.stop_level``; level_win carries the optional fixed target.
        bids = buf.bid_closes
        high = max((c.bid_high for c in buf.candles), default=last.bid_close)
        low = min((c.bid_low for c in buf.candles), default=last.bid_close)
        levels = TradingLevels(
            bid=last.bid_close,
            offer=last.offer_close,
            spread=last.spread,
            high=high,
            low=low,
            scope=high - low,
            average=sum(bids) / len(bids) if bids else last.bid_close,
            level_follower=plan.stop_level,
            level_win=plan.target_level,
            level_zero=plan.level_zero,
            level_loose=plan.stop_level,
            level_security=plan.stop_level,
            stop_distance=abs(last.bid_close - plan.stop_level),
            level_margin=plan.level_margin,
        )
        signal = TradingSignal(
            epic=intent.epic,
            score=intent.score,
            direction=intent.direction,
            regression=RegressionResult(slope=0.0, intercept=0.0, r_squared=0.0),
            sma_fast=0.0,
            sma_slow=0.0,
            roc=0.0,
            spread=last.spread,
            avg_spread=sum(buf.spreads) / len(buf) if len(buf) else last.spread,
            position_in_range=0.0,
            levels=levels,
        )

        position = await self.open_position(
            signal,
            quantity_multiplier=quantity_multiplier,
            broker_noise_buffer=self._broker_stop_buffer(buf),
        )
        if position is not None:
            position.close_profile = plan.profile
            await self._db.commit()
        return position

    @staticmethod
    def _rule_to_price_distance(
        rule: dict,
        default: float,
        *,
        reference_price: float,
        scaling_factor: float,
    ) -> float:
        """Convert an IG dealing-rule distance to a price distance.

        IG expresses a stop/limit distance either as a PERCENTAGE of the price or
        in POINTS (1 point = 1/scalingFactor in price). Both open paths need the
        same conversion, keyed off the entry reference price (the bid for a long,
        the sell level for a short).
        """
        raw = rule.get("value")
        if raw is None:
            return default
        value = float(raw)
        if rule.get("unit") == "PERCENTAGE":
            return value * reference_price / 100
        return value / scaling_factor

    @staticmethod
    def _supports_market_orders(instrument: dict) -> bool:
        """Whether the epic accepts ``orderType: "MARKET"`` orders.

        IG's instrument metadata exposes ``marketOrderPreference`` with three
        values: ``AVAILABLE_DEFAULT_ON`` / ``AVAILABLE_DEFAULT_OFF`` (market
        orders allowed) and ``NOT_AVAILABLE`` (only working/limit orders). An
        epic set to ``NOT_AVAILABLE`` bounces a ``orderType: "MARKET"`` POST with
        ``error.trading.otc.market-orders.not-supported-for-epic``. Checking this
        up front lets both open paths open with a marketable LIMIT directly
        instead of sending a doomed MARKET first.

        A missing field is treated as supported — most epics allow market orders
        and IG omits the field on some markets. This is only a hint, not a
        guarantee: an epic that passes here can still reject MARKET at deal time,
        in which case the POST helper falls back to a marketable LIMIT (see
        :meth:`_post_open_order`). Either way the epic is never dropped.
        """
        return instrument.get("marketOrderPreference") != "NOT_AVAILABLE"

    def _to_marketable_limit(self, payload: dict, reference_price: float) -> dict:
        """Convert a MARKET open payload into a marketable LIMIT payload.

        Priced ``market_order_limit_slippage`` THROUGH the current touch — above
        the ask for a BUY, below the bid for a SELL — with
        ``timeInForce=EXECUTE_AND_ELIMINATE``: IG fills the whole size at the best
        available price up to ``level`` and cancels any unfilled remainder, so the
        limit behaves like a market order and only caps acceptable slippage (it is
        never the fill price). ``stopLevel`` / ``forceOpen`` carry over unchanged.
        """
        slippage = max(self._config.market_order_limit_slippage, 0.0)
        if payload["direction"] == "BUY":
            level = reference_price * (1 + slippage)
        else:
            level = reference_price * (1 - slippage)
        limit_payload = dict(payload)
        limit_payload["orderType"] = "LIMIT"
        limit_payload["level"] = round(level, 5)
        limit_payload["timeInForce"] = "EXECUTE_AND_ELIMINATE"
        return limit_payload

    async def _post_open_order(
        self, order_payload: dict, epic: str, label: str, reference_price: float
    ) -> dict | None:
        """POST an open order, falling back to a marketable LIMIT on rejection.

        When the epic rejects ``orderType: "MARKET"`` at deal time
        (``MARKET_ORDER_NOT_SUPPORTED_CODE``) the same order is retried once as a
        marketable LIMIT (see :meth:`_to_marketable_limit`). Any other error — and
        any failure of the LIMIT retry — logs and returns ``None`` so the caller
        aborts the open cleanly. A payload already sent as LIMIT is not retried.
        """
        # A MARKET order may bounce with ``MARKET_ORDER_NOT_SUPPORTED_CODE`` — an
        # outcome this method recovers from — so flag it as expected on the queue:
        # that abandonment is logged at WARNING, not ERROR, and stays out of the
        # persistent error log. Any OTHER failure is still a real error.
        is_market = order_payload.get("orderType") == "MARKET"
        try:
            return await self._client.post(
                "/positions/otc",
                order_payload,
                version=2,
                priority=Priority.URGENT,
                label=label,
                expect_market_order_rejection=is_market,
            )
        except IGAPIError as exc:
            not_supported = (
                getattr(exc, "ig_error_code", "") == MARKET_ORDER_NOT_SUPPORTED_CODE
            )
            if not not_supported or not is_market:
                logger.error("Failed to open position for %s: %s", epic, exc)
                return None
        except Exception as exc:
            logger.error("Failed to open position for %s: %s", epic, exc)
            return None

        # MARKET rejected as unsupported — remember the epic so future opens skip
        # straight to LIMIT (no repeated doomed MARKET), then retry once now.
        self._market_order_unsupported.add(epic)
        limit_payload = self._to_marketable_limit(order_payload, reference_price)
        logger.info(
            "%s rejects MARKET orders — retrying as marketable LIMIT at %.5f",
            epic,
            limit_payload["level"],
        )
        try:
            return await self._client.post(
                "/positions/otc",
                limit_payload,
                version=2,
                priority=Priority.URGENT,
                label=f"{label} (limit)",
            )
        except Exception as exc:
            logger.error("LIMIT fallback failed for %s: %s", epic, exc)
            return None

    async def _delete_close_order(
        self, close_payload: dict, epic: str, label: str, reference_price: float
    ) -> dict:
        """DELETE a close order, falling back to a marketable LIMIT on rejection.

        Mirrors :meth:`_post_open_order` for the close side: an epic that rejects
        ``orderType: "MARKET"`` at deal time (``MARKET_ORDER_NOT_SUPPORTED_CODE``,
        typically forwards) is retried once as a marketable LIMIT priced through
        the touch (see :meth:`_to_marketable_limit`) and remembered in
        ``_market_order_unsupported`` so future orders skip straight to LIMIT. Any
        other error is re-raised so the caller keeps its 404/phantom handling. A
        payload already sent as LIMIT is not retried.
        """
        # A MARKET close may bounce with ``MARKET_ORDER_NOT_SUPPORTED_CODE`` — an
        # outcome this method recovers from — so flag it as expected on the queue:
        # that abandonment is logged at WARNING, not ERROR. Any OTHER failure is a
        # real error and is re-raised.
        is_market = close_payload.get("orderType") == "MARKET"
        try:
            return await self._client.delete(
                "/positions/otc",
                close_payload,
                version=1,
                priority=Priority.URGENT,
                label=label,
                expect_market_order_rejection=is_market,
            )
        except IGAPIError as exc:
            not_supported = (
                is_market
                and getattr(exc, "ig_error_code", "") == MARKET_ORDER_NOT_SUPPORTED_CODE
            )
            if not not_supported:
                raise

        # MARKET rejected as unsupported — remember the epic so future closes (and
        # opens) skip straight to LIMIT, then retry once now as a marketable LIMIT.
        self._market_order_unsupported.add(epic)
        limit_payload = self._to_marketable_limit(close_payload, reference_price)
        logger.info(
            "%s rejects MARKET orders — retrying close as marketable LIMIT at %.5f",
            epic,
            limit_payload["level"],
        )
        return await self._client.delete(
            "/positions/otc",
            limit_payload,
            version=1,
            priority=Priority.URGENT,
            label=f"{label} (limit)",
        )

    async def open_position(
        self,
        signal: TradingSignal,
        quantity_multiplier: int = 1,
        *,
        broker_noise_buffer: float = 0.0,
    ) -> Position | None:
        """Open a position based on a trading signal.

        Workflow (ported from Action::postOpen):
        1. Fetch market info to validate
        2. Check dealing rules (stop min/max)
        3. Calculate quantity
        4. Send order to IG
        5. Confirm deal
        6. Record in DB

        Args:
            signal: Computed trading signal with levels.
            quantity_multiplier: Multiplies the minimum deal size (default 1).
                Optional sizing hook for entries that scale up.
            broker_noise_buffer: ATR-scaled cushion (price units) placed on top of
                one spread between the software follower and the broker stop posted
                at IG (see :meth:`_broker_stop_level`). Computed by the caller from
                the live buffer; 0 keeps the broker stop one spread beyond the
                follower. The underwater updater holds this open stop untouched
                until break-even, so the cushion set here protects the whole
                pre-break-even life — exactly the phase most exposed to bid noise.

        Returns:
            Created Position object, or None if open failed.
        """
        epic = signal.epic
        levels = signal.levels
        # Trade side. A BUY's protective stop sits BELOW the entry and fills at
        # the ask; a SELL mirrors it — stop ABOVE the entry, fills at the bid.
        # Every direction-dependent computation below branches on this.
        direction = signal.direction

        # 1. Fetch market info
        market_data = await self._client.get(
            f"/markets/{epic}",
            version=3,
            priority=Priority.URGENT,
            label=f"open {epic}: market",
        )
        instrument = market_data.get("instrument", {})
        snapshot = market_data.get("snapshot", {})
        dealing_rules = market_data.get("dealingRules", {})

        # Check tradeable
        if snapshot.get("marketStatus") != "TRADEABLE":
            logger.info(
                "Market %s is not tradeable: %s", epic, snapshot.get("marketStatus")
            )
            return None

        # Some epics (forwards, some futures) reject orderType=MARKET. When the
        # instrument metadata flags it up front — or a past open already proved it
        # (``_market_order_unsupported``) — we open with a marketable LIMIT
        # directly; otherwise we send MARKET and fall back to LIMIT only if the
        # broker bounces it (the metadata is not always reliable). Either way the
        # epic stays tradable — the scanner keeps it in the list for diversity.
        use_market_order = (
            self._supports_market_orders(instrument)
            and epic not in self._market_order_unsupported
        )
        if not use_market_order:
            logger.info(
                "Market %s does not support market orders "
                "(marketOrderPreference=%s) — opening with a marketable LIMIT",
                epic,
                instrument.get("marketOrderPreference"),
            )

        # 2. Validate the protective stop against the dealing rules.
        #
        # The strategy computes ``level_security`` as an absolute *price* (e.g.
        # 1.2059 for AUD/NZD). IG's dealing-rule distances, however, are quoted
        # in *points* (1 point = 1 / scalingFactor in price terms). To compare
        # apples with apples we convert every rule to a price distance and work
        # exclusively in price units from here on — this is the same convention
        # used by ``_push_stop_to_ig`` for trailing updates.
        min_stop_rule = dealing_rules.get("minNormalStopOrLimitDistance", {})
        max_stop_rule = dealing_rules.get("maxStopOrLimitDistance", {})
        min_deal_size = dealing_rules.get("minDealSize", {}).get("value", 1)

        scaling_factor = (
            float(str(snapshot.get("scalingFactor", "1")).replace(",", "")) or 1.0
        )

        min_stop_price = self._rule_to_price_distance(
            min_stop_rule,
            0.0,
            reference_price=levels.bid,
            scaling_factor=scaling_factor,
        )
        max_stop_price = self._rule_to_price_distance(
            max_stop_rule,
            float("inf"),
            reference_price=levels.bid,
            scaling_factor=scaling_factor,
        )

        # Pad IG's minimum-distance floor by a safety margin. IG rejects a stop
        # at/inside its minimum, and the price drifts between this snapshot and
        # the order landing — a stop clamped exactly to the minimum is routinely
        # bounced ("Stop trop près"). The margin gives the market room to move
        # without pushing us back under the floor.
        min_stop_price *= 1 + self._config.stop_min_distance_margin

        # Absolute stop level chosen by the strategy, and its distance from the
        # entry in price terms (always positive). A BUY's stop is below the entry,
        # a SELL's above — so the distance is the signed gap taken on the right
        # side.
        stop_level = levels.level_security
        if direction == "SELL":
            stop_price_distance = stop_level - levels.bid
        else:
            stop_price_distance = levels.bid - stop_level

        # Never place the stop tighter than IG allows (margin included): clamp out
        # to the padded minimum, on the correct side of the entry.
        if stop_price_distance < min_stop_price:
            stop_price_distance = min_stop_price
            if direction == "SELL":
                stop_level = levels.bid + stop_price_distance
            else:
                stop_level = levels.bid - stop_price_distance

        if stop_price_distance > max_stop_price:
            logger.info(
                "Stop too large for %s: %.5f > max %.5f",
                epic,
                stop_price_distance,
                max_stop_price,
            )
            return None

        # IG may have widened the order stop to satisfy its minimum-distance rule,
        # leaving ``stop_level`` further from entry than the strategy asked. In the
        # decoupled open path the close profile sets a SINGLE protective stop
        # (``level_follower == level_loose == level_security``), so when the clamp
        # moves it the software backstop must move with it. Otherwise the bot
        # enforces a stop TIGHTER than the one actually resting at the broker and
        # closes the position in the noise at a level IG would never have hit (the
        # euro risk is already sized from the clamped, wider distance). Shift only
        # the levels that equalled the pre-clamp security; a legacy strategy that
        # deliberately set a tighter follower/loose is left untouched.
        clamp_delta = stop_level - levels.level_security
        follower_level = levels.level_follower
        loose_level = levels.level_loose
        if clamp_delta and levels.level_follower == levels.level_security:
            follower_level += clamp_delta
        if clamp_delta and levels.level_loose == levels.level_security:
            loose_level += clamp_delta

        # 3. Quantity — minimum deal size scaled by the (martingale) multiplier.
        quantity = max(int(min_deal_size), 1) * max(int(quantity_multiplier), 1)

        # 4. Compute the worst-case euro risk for logging. ``euro_per_point`` is
        # the currency-converted euro value of one full point of price movement
        # for the whole position, so the worst-case loss is simply
        # distance × euro_per_point. Fall back to a rough estimate only when the
        # contract size is unknown. Both paths are ESTIMATES (IG's exchangeRate is
        # a reference rate on a foreign quote) — the open log below prints the
        # resolved euro-per-point alongside the risk so a suspect figure is
        # traceable, and ``reconcile_realized_pnl`` fixes the realized P&L later.
        currency = instrument.get("currencies", [{}])[0].get("code", "EUR")
        expiry = instrument.get("expiry", "-")
        epp = euro_per_point(market_data, quantity, currency)
        if epp:
            euro_risk = stop_price_distance * epp
        else:
            euro_risk = quantity * stop_price_distance

        # The broker stop sits one spread PLUS an ATR-scaled noise cushion
        # (``broker_noise_buffer``) further from price than the software follower
        # (below it for a long, above it for a short): the app-side stop
        # (``level_follower == stop_level``) is reached first between two bid polls
        # and the broker order only ever fires as a deeper safety net. This is the
        # SAME offset applied on every later ratchet (see ``_broker_stop_level``);
        # applying it here too keeps the bot in control of the exit from the open
        # onward and right through the start zone — the underwater updater holds
        # this stop untouched until break-even, so the cushion set here persists
        # for the whole pre-break-even life (the phase most exposed to bid noise).
        # Pushing the broker stop FURTHER from price never violates IG's
        # minimum-distance rule, and the euro risk is unchanged because the bot
        # still closes at the (nearer) software ``stop_level``.
        spread = max(float(levels.spread or 0.0), 0.0)
        broker_stop = self._broker_stop_level(
            direction, stop_level, spread, broker_noise_buffer
        )

        # 5. Send order with an absolute stop level (avoids any point/price unit
        # conversion on the IG side). A BUY fills at the ask (price through
        # ``levels.offer``), a SELL at the bid (``levels.bid``), so the
        # marketable-LIMIT fallback prices through the matching touch.
        reference_price = levels.bid if direction == "SELL" else levels.offer
        order_payload = {
            "epic": epic,
            "expiry": expiry,
            "direction": direction,
            "size": str(quantity),
            "orderType": "MARKET",
            "currencyCode": currency,
            "guaranteedStop": False,
            "stopLevel": round(broker_stop, 5),
            "forceOpen": True,
        }
        if not use_market_order:
            order_payload = self._to_marketable_limit(order_payload, reference_price)

        logger.info(
            "Opening %s: epic=%s, qty=%d, stop=%.5f (broker %.5f, %.5f), "
            "risk≈%.2f€ (%s %.4f€/pt)",
            direction,
            epic,
            quantity,
            stop_level,
            broker_stop,
            stop_price_distance,
            euro_risk,
            currency,
            epp if epp else float(quantity),
        )

        result = await self._post_open_order(
            order_payload, epic, f"open {epic}: order", reference_price
        )
        if result is None:
            return None

        deal_reference = result.get("dealReference")
        if not deal_reference:
            logger.error("No dealReference returned for %s", epic)
            return None

        # 6. Record in DB *before* confirming. IG has already accepted the order,
        # so a position now exists at the broker. If the /confirms round-trip then
        # fails (timeout, rate-limit, transient 5xx) we must still hold a DB row —
        # otherwise the position runs untracked: no trailing stop, no auto-close,
        # silently tying up margin (the "in use without open position" bug). The
        # provisional row uses the expected entry (``levels.bid``); the confirm
        # below upgrades it with IG's authoritative dealId and fill level. Writing
        # it now also makes the duplicate-epic gate see the epic as open, so a
        # failed confirm can no longer trigger a re-open loop on the next pass.
        #
        # ``epp`` (currency-converted euro value of one point of movement) was
        # already computed above for the risk check; it is the basis for every
        # P&L figure (live and realized).
        now = datetime.now(UTC)
        position = Position(
            epic=epic,
            epic_name=instrument.get("name", epic)[:10],
            deal_reference=deal_reference,
            deal_id=None,
            direction=direction,
            date=now.date(),
            time_open=now.time(),
            state=PositionState.OPEN,
            strategy=PositionStrategy.TARGET,
            reason_open="auto",
            level_open=Decimal(str(round(levels.bid, 5))),
            level_win=Decimal(str(round(levels.level_win, 5))),
            level_zero=Decimal(str(round(levels.level_zero, 5))),
            level_follower=Decimal(str(round(follower_level, 5))),
            level_loose=Decimal(str(round(loose_level, 5))),
            # The hard software backstop and the chart's broker line track the
            # level actually posted at IG — one spread plus the ATR noise cushion
            # below the software follower (see ``broker_stop`` above), so the
            # broker line starts that far under the follower line and the two
            # ratchet together from there.
            level_security=Decimal(str(round(broker_stop, 5))),
            level_stop=Decimal(str(round(broker_stop, 5))),
            level_margin=Decimal(str(round(levels.level_margin, 5))),
            # Padded IG minimum stop distance (price), reused to clamp every
            # later broker-stop ratchet so it is never rejected as too close.
            min_stop_distance=Decimal(str(round(min_stop_price, 5))),
            pip_spread=Decimal(str(round(levels.spread, 5))),
            quantity=quantity,
            size=int(round(stop_price_distance * scaling_factor)),
            euro_stop=Decimal(str(round(euro_risk, 3))),
            euro_per_point=Decimal(str(round(epp, 6))) if epp else None,
            # Seed the stop trajectory with the bot's SOFTWARE stop (the
            # ``level_follower`` the close profile enforces), not the clamped
            # ``stop_level`` sent to IG. The two diverge at open when IG's
            # minimum-stop-distance rule widens the broker stop past the tighter
            # software stop, and the chart must trace the level the bot actually
            # closes on; the IG broker line is rebuilt from ``level_stop``
            # separately. Each later ratchet appends here and is pushed to IG, so
            # the two lines converge from the first ratchet onward.
            stop_history=(
                [
                    {
                        "t": now.isoformat(),
                        "level": round(float(follower_level), 5),
                        "broker": round(float(broker_stop), 5),
                    }
                ]
                if follower_level
                else None
            ),
        )
        self._db.add(position)
        await self._db.commit()
        await self._db.refresh(position)

        # 7. Confirm the deal to capture the authoritative dealId and fill level.
        # IG processes the order asynchronously: GET /confirms/{ref} returns 404
        # for a brief window right after the POST (the deal reference is not yet
        # resolvable). The APIQueue treats 404 as a permanent client error and
        # does NOT retry it, so a single confirm call loses that race and leaves
        # the row unbound -> phantom never_opened. ``_confirm_with_retry`` polls
        # through that window (and logs IG's error code so a genuine failure is
        # diagnosable from ig_bot.log next time).
        confirmation = await self._confirm_with_retry(deal_reference, epic)
        if confirmation is None:
            # Could not retrieve the confirmation at all. The order may or may not
            # have executed at IG; keep the provisional row so sync_open_positions
            # binds it from GET /positions when it appears (or marks it
            # never_opened past the grace window if it never does). Never delete
            # here — that would orphan a real fill, tying up margin invisibly.
            logger.error(
                "Could not confirm deal %s for %s after retries — position kept, "
                "sync will reconcile",
                deal_reference,
                epic,
            )
            return position

        if confirmation.get("dealStatus") != "ACCEPTED":
            # Genuinely rejected by IG — undo the provisional row.
            reason = confirmation.get("reason", "UNKNOWN")
            logger.warning(
                "Deal rejected for %s: %s — removing draft row", epic, reason
            )
            await self._db.delete(position)
            await self._db.commit()
            return None

        deal_id = confirmation.get("dealId", "")
        confirmed_level = confirmation.get("level")
        open_level = (
            float(confirmed_level) if confirmed_level is not None else levels.bid
        )
        position.deal_id = deal_id or None
        position.level_open = Decimal(str(round(open_level, 5)))
        # The exit references were frozen from the pre-order candle; the fill can
        # land several points away. Translate them onto the real entry so the
        # break-even the exit logic (and the chart) uses is the price actually
        # traded. ``reference_price`` is the touch the order priced through, i.e.
        # exactly what the close profile assumed the fill would be.
        delta = (
            self._reanchor_exit_references(position, reference_price, open_level)
            if confirmed_level is not None
            else 0.0
        )
        await self._db.commit()

        logger.info(
            "Position opened: epic=%s, deal=%s, level=%.5f, stop=%.5f",
            epic,
            deal_id,
            open_level,
            stop_level,
        )
        if delta:
            logger.warning(
                "Fill for %s slipped %+.5f from the %.5f snapshot — break-even "
                "re-anchored to %.5f (margin %.5f)",
                epic,
                delta,
                reference_price,
                float(position.level_zero or 0),
                float(position.level_margin or 0),
            )

        return position

    async def _confirm_with_retry(self, deal_reference: str, epic: str) -> dict | None:
        """Poll ``GET /confirms/{ref}`` until IG can resolve the deal reference.

        IG processes the order asynchronously: the confirmation is briefly
        unavailable right after the POST and the endpoint answers ``404``. The
        APIQueue classifies ``404`` as a permanent client error and does not
        retry it, so a single call loses that race and the position is left
        unbound. This polls up to :data:`CONFIRM_MAX_ATTEMPTS` times with a
        linear backoff, retrying only the transient cases:

        - ``404`` — deal not resolvable yet (poll again);
        - network error / ``5xx`` (no/elevated HTTP status) — transient;

        Any other ``4xx`` is permanent (bad request, auth, etc.) and stops the
        loop immediately. The IG ``errorCode`` and HTTP status are logged on
        every failed attempt so a genuine failure is diagnosable afterwards
        (the previous single-line ``except`` swallowed the cause). Returns the
        confirmation dict, or ``None`` if it could never be retrieved.
        """
        last_exc: Exception | None = None
        for attempt in range(1, CONFIRM_MAX_ATTEMPTS + 1):
            try:
                return await self._client.get(
                    f"/confirms/{deal_reference}",
                    version=1,
                    priority=Priority.URGENT,
                    label=f"open {epic}: confirm ({attempt}/{CONFIRM_MAX_ATTEMPTS})",
                    # A 404 here is expected (deal not resolvable yet, or already
                    # gone because the broker closed the market) and handled by
                    # this retry loop — the queue should warn, not error.
                    expect_not_found=True,
                )
            except IGAPIError as exc:
                last_exc = exc
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", None)
                ig_code = getattr(exc, "ig_error_code", "") or "—"
                transient = status == 404 or status is None or status >= 500
                if not transient:
                    logger.error(
                        "Confirm for %s failed permanently (HTTP %s, IG=%s): %s",
                        epic,
                        status,
                        ig_code,
                        exc,
                    )
                    return None
                logger.warning(
                    "Confirm for %s not ready (attempt %d/%d, HTTP %s, IG=%s) "
                    "— retrying",
                    epic,
                    attempt,
                    CONFIRM_MAX_ATTEMPTS,
                    status,
                    ig_code,
                )
            except Exception as exc:  # noqa: BLE001 — network/timeouts are transient
                last_exc = exc
                logger.warning(
                    "Confirm for %s errored (attempt %d/%d) — retrying: %s",
                    epic,
                    attempt,
                    CONFIRM_MAX_ATTEMPTS,
                    exc,
                )
            if attempt < CONFIRM_MAX_ATTEMPTS:
                await asyncio.sleep(CONFIRM_RETRY_DELAY_SECONDS * attempt)

        logger.error(
            "Confirm for %s (dealRef=%s) never retrieved after %d attempts: %s",
            epic,
            deal_reference,
            CONFIRM_MAX_ATTEMPTS,
            last_exc,
        )
        return None

    def _reanchor_exit_references(
        self, position: Position, expected_fill: float, actual_fill: float
    ) -> float:
        """Translate the open-frozen exit references onto the real fill level.

        ``level_zero`` (break-even) and ``level_margin`` are chosen by the close
        profile **before** the order is sent, from the last recorded candle (see
        :meth:`~src.exit.close_zoneprofit.CloseZoneProfit.initial_plan`): the offer
        for a long, the bid for a short — i.e. exactly the touch the order prices
        through (``reference_price``). The market moves between that snapshot and
        the fill, so the confirmed level can land several points away (4 points
        observed on ``CC.D.NG.UNC.IP``), leaving break-even, the margin line and
        the derived profit trigger anchored on a price the position never traded
        at. The whole exit then runs on a shifted frame: the zone classifier reads
        a break-even below the real entry, the margin-zone updater parks the stop
        on what looks like locked-in profit but is not, and the chart draws a
        break-even line the trade never crossed.

        Re-anchoring preserves the profile's geometry — the noise band
        (``level_margin - level_zero``), and therefore the derived profit trigger
        (``2 × margin - zero``), is translated, not rescaled. The protective stops
        are deliberately left untouched: they are market-structure levels
        (support / regression / ATR) already resting at the broker, not
        entry-relative offsets, so a slipped fill widens the real risk rather than
        moving the stop (``euro_stop`` keeps the pre-fill figure).

        Returns the applied delta, ``0.0`` when nothing moved.
        """
        if position.level_zero is None or not expected_fill or not actual_fill:
            return 0.0
        delta = actual_fill - expected_fill
        if abs(delta) < 1e-9:
            return 0.0
        position.level_zero = Decimal(str(round(float(position.level_zero) + delta, 5)))
        # ``0`` means "no margin persisted" everywhere else (the profiles fall back
        # to a per-tick computation), so leave it alone rather than turning it into
        # a bogus ``delta``-sized level.
        if position.level_margin is not None and float(position.level_margin) > 0:
            position.level_margin = Decimal(
                str(round(float(position.level_margin) + delta, 5))
            )
        return delta

    def _bind_real_open_level(self, position: Position, ig_position: dict) -> None:
        """Adopt IG's open level on a row that bound without a confirmation.

        A provisional row (``deal_id`` still ``None``) is one whose ``/confirms``
        round-trip never came back: ``level_open`` holds the pre-order estimate
        (the snapshot bid) and the exit references sit on the snapshot touch. The
        live position IG hands back at bind time carries the real ``level`` — the
        first authoritative fill available for that row — so it is re-anchored here
        exactly as the confirm path does.

        The expected fill is reconstructed from the provisional row: the order
        priced through the offer for a long (bid + spread), the bid for a short.
        """
        ig_level = _to_float(ig_position.get("level"))
        if not ig_level:
            return
        expected = float(position.level_open or 0)
        if position.direction != "SELL":
            expected += float(position.pip_spread or 0)
        position.level_open = Decimal(str(round(ig_level, 5)))
        delta = self._reanchor_exit_references(position, expected, ig_level)
        logger.info(
            "Unconfirmed %s bound to IG open level %.5f (assumed %.5f) — "
            "break-even re-anchored %+.5f to %.5f",
            position.epic,
            ig_level,
            expected,
            delta,
            float(position.level_zero or 0),
        )

    def _euro_pnl(self, position: Position, level: float) -> float:
        """Compute the euro P&L of a position at a given market level.

        Preferred path: ``euro_per_point`` is the currency-converted euro value
        of one full point of movement for the whole position, so the P&L is
        ``(level - open) * euro_per_point`` for a long and ``(open - level) *
        euro_per_point`` for a short. This is correct for JPY/USD pairs (currency
        conversion applied) and indices alike.

        Legacy fallback (positions opened before ``euro_per_point`` existed):
        derive a per-pip value from ``euro_stop`` / ``size`` / ``quantity``.
        Note this fallback ignores currency conversion and is only an estimate
        until ``reconcile_realized_pnl`` overwrites it with IG's figure.
        """
        open_level = float(position.level_open or 0)
        # A short gains when the price falls, so its P&L is the mirror of a long's.
        move = level - open_level
        if position.direction == "SELL":
            move = -move
        if position.euro_per_point is not None and float(position.euro_per_point) != 0:
            return move * float(position.euro_per_point)
        # Legacy fallback (rows opened before euro_per_point existed): reconstruct
        # the euro value of one unit of PRICE movement from the euro risk and the
        # PRICE distance to the stop. The old formula divided ``euro_stop`` by
        # ``size`` — a POINT distance (price × scalingFactor) — then multiplied by
        # a PRICE move, mixing units and understating the P&L by a factor of
        # scalingFactor (10^4 on forex). The stop levels are in price, so
        # ``euro_stop / |open - stop|`` is scalingFactor-independent.
        stop_distance = abs(open_level - float(position.level_loose or 0))
        if stop_distance > 0:
            return move * float(position.euro_stop or 0) / stop_distance
        return 0.0

    async def _fetch_close_result(
        self, deal_reference: str | None, epic: str
    ) -> tuple[float | None, float | None, bool]:
        """Return ``(fill_level, realized_profit_eur, rejected)`` from a close confirm.

        ``rejected`` is True only when IG's confirmation carries an explicit
        non-``ACCEPTED`` ``dealStatus`` (e.g. a ``MARKET_CLOSED`` refusal on an
        ``EDITS_ONLY`` market): the close did NOT happen and the position is
        still live at the broker. A *missing* confirmation (network hiccup) is
        NOT a rejection — ``rejected`` stays False so the caller falls back to
        its observed level and reconcile repairs it later.

        ``fill_level`` / ``profit`` are ``None`` when the confirmation is missing
        or omits them. The confirmation's ``profit`` is already in the account
        currency; it is only trusted when ``profitCurrency`` confirms EUR.
        """
        if not deal_reference:
            return None, None, False
        try:
            confirm = await self._client.get(
                f"/confirms/{deal_reference}",
                version=1,
                priority=Priority.URGENT,
                label=f"close {epic}: confirm",
                # A 404 here is a legitimate outcome (the deal reference may not
                # resolve if the broker already closed the market) that the caller
                # handles by falling back — the queue should warn, not error.
                expect_not_found=True,
            )
        except Exception as exc:
            logger.debug("Could not fetch close confirmation for %s: %s", epic, exc)
            return None, None, False

        # IG accepts the DELETE (200 + dealReference) but reports a market-closed
        # refusal only here, as dealStatus=REJECTED. Recording it as a successful
        # close leaves the position live at IG while the DB thinks it is gone —
        # the next open reuses/duplicates the dealId and the weekend-held deal's
        # real P&L lands on the wrong row. Surface the rejection so the caller
        # keeps the position OPEN and retries when the market reopens.
        status = confirm.get("dealStatus")
        if status is not None and status != "ACCEPTED":
            logger.warning(
                "IG rejected close of %s: %s — position left OPEN for retry",
                epic,
                confirm.get("reason", "UNKNOWN"),
            )
            return None, None, True

        level = confirm.get("level")
        fill_level = float(level) if level is not None else None

        profit = confirm.get("profit")
        profit_ccy = confirm.get("profitCurrency")
        ig_profit = (
            float(profit)
            if profit is not None and profit_ccy in (None, "", "EUR", "E", "€")
            else None
        )
        return fill_level, ig_profit, False

    async def reconcile_realized_pnl(self, day: date | None = None) -> int:
        """Overwrite a day's realized P&L with IG's authoritative figures.

        Source of truth is ``GET /history/transactions``: each deal carries
        ``profitAndLoss`` already converted to the account currency, plus the
        real ``openLevel`` / ``closeLevel``. This repairs every closed position —
        including those closed outside the bot (``closed_externally`` /
        ``not_found_in_ig``), whose levels and euro were only estimated.

        Positions are matched to transactions by instrument name, then paired by
        execution *time* (see :meth:`_match_cost`) using a globally greedy
        assignment: every candidate pair is ranked by cost and consumed
        cheapest-first, so one instrument's several deals of the day land on the
        right rows instead of the first row winning the closest transaction and
        pushing its neighbours onto someone else's deal. Returns the number of
        positions updated.
        """
        day = day or date.today()
        result = await self._db.execute(
            select(Position)
            .where(Position.date == day, Position.state == PositionState.CLOSE)
            .order_by(Position.id)
        )
        closed = list(result.scalars().all())
        if not closed:
            return 0

        midnight = datetime(day.year, day.month, day.day)
        frm = midnight.strftime("%Y-%m-%dT00:00:00")
        to = (midnight + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
        try:
            data = await self._client.get(
                f"/history/transactions?from={frm}&to={to}",
                version=2,
                priority=Priority.HIGH,
                label="reconcile realized P&L",
            )
        except Exception as exc:
            logger.error(
                "Realized P&L reconcile failed — could not fetch history: %s", exc
            )
            return 0

        transactions = [
            t
            for t in data.get("transactions", [])
            if parse_ig_pnl(t.get("profitAndLoss")) is not None
        ]

        # IG's transaction ``reference`` is unrelated to our stored deal
        # reference/id, so we match on instrument name (normalized: the
        # "… converted at <rate>" suffix on currency-converted pairs is dropped)
        # and disambiguate same-instrument positions by execution time. Every
        # (position, transaction) pair is scored, then consumed cheapest-first so
        # the assignment is global rather than first-come: a per-position greedy
        # loop let an early row take a later deal's transaction and cascaded the
        # whole instrument's rows onto the wrong deals (a morning loss then
        # displaying the afternoon's gain).
        pairs = []
        for index, position in enumerate(closed):
            for txn_index, txn in enumerate(transactions):
                if not self._names_match(position.epic_name, txn.get("instrumentName")):
                    continue
                cost = self._match_cost(position, txn)
                if cost is None:
                    continue
                pairs.append((cost, index, txn_index))
        pairs.sort()

        matched_positions: set[int] = set()
        matched_txns: set[int] = set()
        updated = 0
        for _cost, index, txn_index in pairs:
            if index in matched_positions or txn_index in matched_txns:
                continue
            matched_positions.add(index)
            matched_txns.add(txn_index)
            if self._apply_transaction(closed[index], transactions[txn_index]):
                updated += 1

        for index, position in enumerate(closed):
            if index not in matched_positions:
                logger.debug(
                    "No IG transaction matched closed position %s (%s)",
                    position.id,
                    position.epic,
                )

        if updated:
            await self._db.commit()
            logger.info(
                "Realized P&L reconciled from IG: %d/%d closed positions on %s",
                updated,
                len(closed),
                day.isoformat(),
            )
        return updated

    def _apply_transaction(self, position: Position, txn: dict) -> bool:
        """Write a transaction's authoritative P&L, levels and times onto a position.

        ``/history/transactions`` is IG's source of truth for a closed deal: it
        carries the real ``openLevel``/``closeLevel``, the realized
        ``profitAndLoss`` and the exact UTC execution timestamps
        (``openDateUtc``/``dateUtc``). We persist the broker times into the
        dedicated ``time_open_broker``/``time_close_broker`` columns so the chart
        can mark entry/exit at the moment the broker actually filled — not at the
        bot's loop/detection clock (``time_open``/``time_close``), which lags and,
        for externally-closed positions, can be minutes off.
        """
        pnl = parse_ig_pnl(txn.get("profitAndLoss"))
        if pnl is None:
            return False
        position.euro = Decimal(str(round(pnl, 3)))
        position.win = 1 if pnl > 0 else 0
        open_level = _to_float(txn.get("openLevel"), default=0.0)
        close_level = _to_float(txn.get("closeLevel"), default=0.0)
        if open_level:
            position.level_open = Decimal(str(round(open_level, 5)))
        if close_level:
            position.level_close = Decimal(str(round(close_level, 5)))
        else:
            # IG omitted the close level: keep it consistent with the
            # authoritative P&L instead of leaving a stale value. For a long
            # ``pnl = (close - open) × epp`` → ``close = open + pnl/epp``; for a
            # short ``pnl = (open - close) × epp`` → ``close = open - pnl/epp``.
            base_open = float(position.level_open or 0)
            epp = float(position.euro_per_point or 0)
            if base_open and epp:
                delta = pnl / epp
                if position.direction == "SELL":
                    delta = -delta
                position.level_close = Decimal(str(round(base_open + delta, 5)))
        open_time = _parse_ig_utc_time(txn.get("openDateUtc"))
        close_time = _parse_ig_utc_time(txn.get("dateUtc"))
        if open_time is not None:
            position.time_open_broker = open_time
        if close_time is not None:
            position.time_close_broker = close_time
        return True

    @staticmethod
    def _names_match(epic_name: str | None, instrument_name: str | None) -> bool:
        """Whether a stored ``epic_name`` and an IG ``instrumentName`` refer to
        the same instrument.

        IG appends "… converted at <rate>" to currency-converted pairs and uses
        the full display name (e.g. "France 40 Cash (€10)"), while ``epic_name``
        is the IG market name truncated to 10 chars. Comparison is therefore
        prefix-based over the first (≤10) characters of both normalized names.
        """
        a = (epic_name or "").strip().lower()
        base = (instrument_name or "").split(" converted at")[0].strip().lower()
        if not a or not base:
            return False
        n = min(len(a), len(base), 10)
        return a[:n] == base[:n]

    @staticmethod
    def _seconds_between(left: time | None, right: time | None) -> float | None:
        """Absolute gap in seconds between two times of day.

        Returns ``None`` when either side is missing so callers can fall back to
        another discriminator instead of scoring the pair as a perfect match.
        """
        if left is None or right is None:
            return None
        return abs(
            (left.hour * 3600 + left.minute * 60 + left.second + left.microsecond / 1e6)
            - (
                right.hour * 3600
                + right.minute * 60
                + right.second
                + right.microsecond / 1e6
            )
        )

    @classmethod
    def _match_cost(cls, position: Position, txn: dict) -> float | None:
        """Cost of pairing a closed position with an IG transaction, or ``None``
        when they are too far apart in time to be the same deal.

        Execution *times* are the discriminator, not levels: two deals on the
        same instrument the same day sit at nearly identical prices, so level
        distance regularly picked another position's transaction. Times also make
        the match **idempotent** — ``time_open`` / ``time_close`` are the bot's
        own clock and :meth:`_apply_transaction` never rewrites them, whereas the
        level-based cost scored against ``level_open`` / ``level_close`` that a
        previous (bad) match had already overwritten, so every rerun re-confirmed
        the error.

        The close gap dominates and the open gap breaks ties: the bot detects a
        close within one sync cycle, and an adopted row's ``time_open`` comes
        from IG's ``createdDateUTC``. Falls back to level distance only when the
        transaction carries no usable timestamp at all.
        """
        close_gap = cls._seconds_between(
            position.time_close, _parse_ig_utc_time(txn.get("dateUtc"))
        )
        open_gap = cls._seconds_between(
            position.time_open, _parse_ig_utc_time(txn.get("openDateUtc"))
        )
        gaps = [gap for gap in (close_gap, open_gap) if gap is not None]
        if not gaps:
            return cls._level_distance(position, txn)
        if max(gaps) > RECONCILE_MATCH_MAX_SECONDS:
            return None
        return 2.0 * (close_gap if close_gap is not None else open_gap) + (
            open_gap or 0.0
        )

    @staticmethod
    def _level_distance(position: Position, txn: dict) -> float:
        """Sum of |open Δ| + |close Δ| between a position and a transaction.

        Last-resort discriminator when a transaction carries no timestamp (see
        :meth:`_match_cost`). Missing levels contribute nothing.
        """
        distance = 0.0
        if position.level_open is not None:
            distance += abs(
                float(position.level_open) - _to_float(txn.get("openLevel"))
            )
        if position.level_close is not None:
            distance += abs(
                float(position.level_close) - _to_float(txn.get("closeLevel"))
            )
        return distance

    async def sync_open_positions(self) -> dict[str, dict]:
        """Reconcile DB open positions against IG's live position list.

        A single ``GET /positions`` call is the source of truth for what is
        actually open at the broker. For every position the DB still considers
        OPEN this method:

        - refreshes the stored ``deal_id`` when IG reports a different one,
        - recomputes the live unrealized P&L from the current bid and updates
          ``euro`` (running unrealized) plus ``euro_max`` / ``euro_min`` (the
          favourable/adverse excursion),
        - reconciles positions that no longer exist at IG (closed or expired
          outside the bot) by marking them CLOSE with reason
          ``closed_externally``.

        Reconciliation is two-directional: besides repairing/closing the DB rows
        above, any position open at IG that the DB does **not** track is *adopted*
        (a fresh OPEN row is created via :meth:`_adopt_ig_position`) so it becomes
        visible on the dashboard and managed by ``monitor_positions``. Without
        this, an order that executed at IG but never got recorded (e.g. a failed
        ``/confirms`` round-trip) would run forever untracked, tying up margin
        with no open position shown — the bug this method now guards against.

        Positions reconciled as ``closed_externally`` by this run are also
        recorded on :attr:`reconciled_closed` (reset at each call) so the caller
        can act on a broker-side stop-out it never observed itself — the
        recovery-revert rule reads it to open the reverse side.

        Returns:
            Map of ``epic -> live IG entry`` ({"position": ..., "market": ...})
            for every position still open at IG, so callers can reuse the data
            without issuing a second request.
        """
        self.reconciled_closed = []
        result = await self._db.execute(
            select(Position).where(Position.state == PositionState.OPEN)
        )
        db_positions = list(result.scalars().all())

        # Every dealId the DB has *ever* recorded, in any state. Adoption is gated
        # on this so a live IG position is never adopted twice: once a row exists
        # for a dealId (OPEN, or already CLOSEd by reconciliation) it must never
        # spawn a second "adopted" row. Without this guard a position whose
        # provisional row failed to bind — or whose adopted row was reconciled to
        # CLOSE — was re-adopted on the next 20s sync, piling up duplicate rows
        # for one dealId (observed: 6 rows for a single DAX position).
        known = await self._db.execute(
            select(Position.deal_id).where(Position.deal_id.isnot(None))
        )
        known_deal_ids: set[str] = {row[0] for row in known.all()}

        # Always query IG — it is the source of truth for tied-up margin. We must
        # call even when the DB has no OPEN row, otherwise an untracked position
        # at the broker stays invisible forever (it can never be adopted).
        try:
            data = await self._client.get(
                "/positions",
                version=2,
                priority=Priority.HIGH,
                label="sync open positions",
            )
        except Exception as exc:
            logger.error(
                "Position sync failed — could not fetch live positions: %s", exc
            )
            return {}

        entries = data.get("positions", [])
        # An epic may carry several live positions (one per dealId), so index by
        # dealId for exact matching, by dealReference for binding provisional
        # rows whose dealId is not known yet (the order executed but /confirms
        # never landed — the dealReference is the only stable handle we already
        # hold), and keep an epic->entries list as the last-resort fallback.
        live_by_deal: dict[str, dict] = {}
        live_by_ref: dict[str, dict] = {}
        live_by_epic: dict[str, list[dict]] = {}
        for entry in entries:
            ig_position = entry.get("position", {})
            epic = entry.get("market", {}).get("epic")
            deal_id = ig_position.get("dealId")
            deal_ref = ig_position.get("dealReference")
            if deal_id:
                live_by_deal[deal_id] = entry
            if deal_ref:
                live_by_ref[deal_ref] = entry
            if epic:
                live_by_epic.setdefault(epic, []).append(entry)

        dirty = False
        claimed: set[str] = set()  # IG dealIds already bound to a DB row
        for position in db_positions:
            entry = None
            # Exact dealId match, but only if that live position is still free.
            # The claim guard matters when several DB rows share one dealId — a
            # legacy corruption from the old epic-keyed sync — so each live
            # position binds to at most one row instead of all rows grabbing it.
            if position.deal_id and position.deal_id in live_by_deal:
                cand_deal = live_by_deal[position.deal_id]["position"].get("dealId")
                if cand_deal not in claimed:
                    entry = live_by_deal[position.deal_id]
            if entry is None and position.deal_reference:
                # Deterministic binding for a provisional row (deal_id None) whose
                # order DID execute: IG echoes our dealReference on the live
                # position, so match on it directly rather than guessing by level.
                ref_entry = live_by_ref.get(position.deal_reference)
                if ref_entry is not None:
                    cand_deal = ref_entry["position"].get("dealId")
                    if cand_deal and cand_deal not in claimed:
                        entry = ref_entry
            if entry is None:
                # No / stale / already-claimed dealId and no dealReference match —
                # bind to the unclaimed live entry for this epic whose open level
                # is closest (keeps the level<->deal pairing sane when an epic has
                # several positions). Last-resort fallback for rows that never
                # recorded a usable dealReference.
                best_dist: float | None = None
                for candidate in live_by_epic.get(position.epic, []):
                    cand_deal = candidate.get("position", {}).get("dealId")
                    if not cand_deal or cand_deal in claimed:
                        continue
                    cand_level = _to_float(candidate.get("position", {}).get("level"))
                    dist = abs(cand_level - float(position.level_open or 0))
                    if best_dist is None or dist < best_dist:
                        entry, best_dist = candidate, dist

            if entry is None:
                # No live IG position matched this DB row. Split by whether the
                # row ever bound a dealId:
                #   * deal_id set  -> a real position that has now closed or
                #     expired outside the bot: reconcile it as closed_externally.
                #   * deal_id None -> the open never confirmed (the /confirms
                #     round-trip failed, or the order never executed). Such a row
                #     must not be logged as a real closed_externally trade at €0.
                #     Grant it a grace window first: a just-opened position may
                #     simply not be in /positions yet (IG eventual consistency)
                #     and the epic-level fallback above binds it on a later tick.
                #     Past the window with still no match, it was never genuinely
                #     opened -> mark never_opened (excluded from stats).
                # The monitor loop runs in a separate session and may have
                # authoritatively closed this position (stop / win / EOD) during
                # the awaited GET /positions above. Re-read from the DB before
                # reconciling so a real close is never overwritten with a stale
                # ``closed_externally`` estimate and perimed P&L. (#6)
                try:
                    await self._db.refresh(position)
                except Exception:
                    continue  # row vanished under us (deleted) — nothing to do
                if position.state != PositionState.OPEN:
                    continue
                if position.deal_id is None:
                    if self._opened_within(position, RECONCILE_GRACE_SECONDS):
                        continue  # too fresh — let a later sync bind it
                    self._mark_never_opened(position)
                else:
                    # Never trust a single bulk-list miss for a dealId-bound
                    # position: confirm authoritatively with a targeted
                    # GET /positions/{dealId} and reconcile only on a definitive
                    # 404 (see _ig_position_gone). A transient omission — observed
                    # right after a streaming reconnect rotated the session tokens
                    # — otherwise closed a still-open position as a phantom, which
                    # the known_deal_ids adoption guard then made permanent.
                    if not await self._ig_position_gone(position):
                        continue  # still open / uncertain — retry on a later sync
                    self._reconcile_vanished(position)
                    self.reconciled_closed.append(position)
                dirty = True
                continue

            ig_position = entry.get("position", {})
            market = entry.get("market", {})

            # Refresh dealId if IG rotated it (stale id is the 404 root cause).
            ig_deal_id = ig_position.get("dealId")
            if ig_deal_id:
                claimed.add(ig_deal_id)
                if ig_deal_id != position.deal_id:
                    was_provisional = position.deal_id is None
                    logger.info(
                        "Position %s dealId refreshed: %s -> %s",
                        position.epic,
                        position.deal_id,
                        ig_deal_id,
                    )
                    position.deal_id = ig_deal_id
                    dirty = True
                    if was_provisional:
                        self._bind_real_open_level(position, ig_position)

            # Update live unrealized P&L and excursion from the current price. A
            # long marks against the bid (sell-to-close); a short against the
            # offer (buy-to-close). Fall back to the bid if the offer is absent.
            bid = market.get("bid")
            if position.direction == "SELL":
                mark = market.get("offer", bid)
            else:
                mark = bid
            if mark is not None:
                euro_pnl = self._euro_pnl(position, float(mark))
                position.euro = Decimal(str(round(euro_pnl, 3)))
                position.euro_max = Decimal(
                    str(round(max(euro_pnl, float(position.euro_max or euro_pnl)), 3))
                )
                position.euro_min = Decimal(
                    str(round(min(euro_pnl, float(position.euro_min or euro_pnl)), 3))
                )
                dirty = True

        # Adopt every live IG position no DB row claimed — but never one already
        # tracked (claimed this run) or ever recorded (known_deal_ids), so a
        # position is adopted at most once across syncs.
        for entry in entries:
            deal_id = entry.get("position", {}).get("dealId")
            if not deal_id or deal_id in claimed or deal_id in known_deal_ids:
                continue
            adopted = await self._adopt_ig_position(entry)
            if adopted is not None:
                claimed.add(deal_id)
                known_deal_ids.add(deal_id)
                dirty = True

        if dirty:
            await self._db.commit()

        live: dict[str, dict] = {}
        for entry in entries:
            epic = entry.get("market", {}).get("epic")
            if epic and epic not in live:
                live[epic] = entry
        return live

    async def _adopt_ig_position(self, entry: dict) -> Position | None:
        """Create a DB OPEN row for a live IG position the DB does not track.

        Used by :meth:`sync_open_positions` to recover positions that exist at
        the broker but were never (or no longer are) recorded locally. The row is
        seeded so ``monitor_positions`` can manage it like any other: the IG stop
        becomes ``level_loose`` / ``level_follower`` (the trailing stop ratchets
        up from there) and ``euro_per_point`` is derived from a ``/markets`` call
        for currency-correct P&L.

        Both BUY and SELL positions are adopted so a manually opened short
        survives a restart: the close profile that :meth:`manage_position` routes
        to is direction-aware and mirrors every reference for a SELL. An unknown
        direction is logged and skipped rather than mismanaged.

        Returns the created ``Position`` (added to the session, not committed), or
        ``None`` when the entry is unusable or the direction is unknown.
        """
        ig_position = entry.get("position", {})
        market = entry.get("market", {})
        epic = market.get("epic")
        deal_id = ig_position.get("dealId")
        if not epic or not deal_id:
            return None

        direction = ig_position.get("direction")
        if direction not in ("BUY", "SELL"):
            logger.warning(
                "Not adopting IG position %s (unknown direction %s %s)",
                deal_id,
                direction,
                epic,
            )
            return None
        is_sell = direction == "SELL"

        open_level = _to_float(ig_position.get("level"))
        stop_level = _to_float(ig_position.get("stopLevel")) or None
        limit_level = _to_float(ig_position.get("limitLevel")) or None
        size = ig_position.get("size") or 1
        currency = ig_position.get("currency") or "EUR"
        scaling = _to_float(market.get("scalingFactor"), 1.0) or 1.0
        bid = _to_float(market.get("bid"), open_level)
        offer = _to_float(market.get("offer"), bid)
        spread = max(offer - bid, 0.0)

        # Currency-correct euro value of one point of movement. Prefer a /markets
        # payload (carries the quote->EUR rate); fall back to the contract size on
        # the position entry (already in account currency for EUR-quoted markets).
        epp = 0.0
        try:
            market_data = await self._client.get(
                f"/markets/{epic}",
                version=3,
                priority=Priority.HIGH,
                label=f"adopt {epic}: market",
            )
            epp = euro_per_point(market_data, float(size), currency)
        except Exception as exc:
            logger.debug("Adopt %s: /markets lookup failed: %s", epic, exc)
        if not epp:
            epp = float(size) * _to_float(ig_position.get("contractSize"))

        # A short's stop sits ABOVE the entry, a long's below — take the gap on
        # the correct side so the distance stays positive either way.
        if stop_level:
            gap = stop_level - open_level if is_sell else open_level - stop_level
            stop_distance = max(gap, 0.0)
        else:
            stop_distance = 0.0
        euro_stop = stop_distance * epp if epp else 0.0

        created = ig_position.get("createdDateUTC")
        try:
            opened_at = (
                datetime.fromisoformat(created) if created else datetime.now(UTC)
            )
        except ValueError:
            opened_at = datetime.now(UTC)

        # A 0 level means "unset" downstream (e.g. level_win 0 = no fixed target,
        # rides the trailing stop; level_loose 0 = no hard stop close).
        stop_dec = Decimal(str(round(stop_level, 5))) if stop_level else Decimal("0")
        # Running P&L seed, mirrored for a short (profit as price falls). The
        # monitor recomputes it every tick via ``_euro_pnl``; this is only the
        # initial value shown until the first tick lands.
        move = (open_level - bid) if is_sell else (bid - open_level)
        position = Position(
            epic=epic,
            epic_name=(market.get("instrumentName") or epic)[:10],
            deal_reference=ig_position.get("dealReference"),
            deal_id=deal_id,
            direction=direction,
            date=opened_at.date(),
            time_open=opened_at.time(),
            state=PositionState.OPEN,
            strategy=PositionStrategy.TARGET,
            reason_open="adopted",
            level_open=Decimal(str(round(open_level, 5))),
            level_win=(
                Decimal(str(round(limit_level, 5))) if limit_level else Decimal("0")
            ),
            level_zero=Decimal(str(round(open_level, 5))),
            level_follower=stop_dec,
            level_loose=stop_dec,
            level_security=stop_dec,
            level_stop=stop_dec,
            # Seed the stop trajectory at the IG-reported open time (later
            # ratchets append) so adopted positions also get a stepped line.
            stop_history=(
                [{"t": opened_at.isoformat(), "level": round(stop_level, 5)}]
                if stop_level
                else None
            ),
            pip_spread=Decimal(str(round(spread, 5))),
            quantity=int(size),
            size=int(round(stop_distance * scaling)),
            euro=Decimal(str(round(move * epp, 3))) if epp else None,
            euro_stop=Decimal(str(round(euro_stop, 3))),
            euro_per_point=Decimal(str(round(epp, 6))) if epp else None,
        )
        self._db.add(position)
        logger.warning(
            "Adopted untracked IG position %s %s (%s) dealId=%s open=%.5f stop=%s "
            "epp=%.4f — now managed by the bot",
            direction,
            epic,
            position.epic_name,
            deal_id,
            open_level,
            stop_level,
            epp,
        )
        return position

    def _reconcile_vanished(self, position: Position) -> None:
        """Mark a position closed because IG no longer reports it as open.

        The actual close happened outside the bot, so there is no fresh close
        level to record; the best estimate is the last live unrealized P&L
        computed by the most recent sync (stored in ``euro``). This estimate is
        later overwritten by ``reconcile_realized_pnl`` with IG's true figure.

        ``level_close`` must stay consistent with that ``euro``. For a long
        ``P&L = (close - open) × epp`` → ``close = open + euro/epp``; for a short
        ``P&L = (open - close) × epp`` → ``close = open - euro/epp``. Backing the
        close level out this way (rather than defaulting to the open level) avoids
        ``level_close == level_open`` while ``euro`` shows a real loss, which read
        as "closed at break-even for −89€" on the chart. A short stopped out
        *above* its entry must show ``level_close`` above the entry, not below.
        Only derived when no genuine close fill was ever captured.
        """
        now = datetime.now(UTC)
        open_level = float(position.level_open or 0)
        epp = float(position.euro_per_point or 0)
        euro_pnl = (
            float(position.euro)
            if position.euro is not None
            else self._euro_pnl(position, float(position.level_close or open_level))
        )
        position.state = PositionState.CLOSE
        position.time_close = now.time()
        if position.level_close is None:
            if position.euro is not None and epp:
                delta = euro_pnl / epp
                if position.direction == "SELL":
                    delta = -delta
                close_level = open_level + delta
            else:
                close_level = open_level
            position.level_close = Decimal(str(round(close_level, 5)))
        position.reason_close = "closed_externally"
        position.euro = Decimal(str(round(euro_pnl, 3)))
        position.win = 1 if euro_pnl > 0 else 0
        logger.warning(
            "Position %s no longer open at IG — reconciled as "
            "closed_externally (P&L=%.2f€)",
            position.epic,
            euro_pnl,
        )

    async def _ig_position_gone(self, position: Position) -> bool:
        """Authoritatively confirm a position is no longer open at IG.

        A position bound to a ``deal_id`` can drop out of the bulk
        ``GET /positions`` list transiently: that list is eventually consistent
        and has been observed to omit a still-open position right after a
        streaming reconnect rotated the session tokens. The old code reconciled
        on that single miss, writing the position off as a phantom
        ``closed_externally`` while it was still live at the broker — and, once
        its ``deal_id`` sat in ``known_deal_ids``, it was never re-adopted, so the
        real position then ran untracked.

        This probes the single-position endpoint ``GET /positions/{dealId}`` and
        trusts **only** a definitive ``404`` (IG says the deal is not open). Any
        other outcome — the position still returned (``200``), a network error, a
        ``5xx``, or any non-404 status — is treated as *uncertain*: return
        ``False`` so the caller leaves the position OPEN and a later sync decides.
        Uncertainty must never close a live position.

        Reaching this path already means the bulk list held no entry for the epic
        under *any* handle (dealId, dealReference, or epic-level fallback), so a
        rotated dealId cannot be the cause of the miss — the targeted 404 is a
        genuine confirmation, not a stale-id artifact.
        """
        if not position.deal_id:
            return False
        try:
            await self._client.get(
                f"/positions/{position.deal_id}",
                version=2,
                priority=Priority.HIGH,
                label=f"confirm vanished {position.epic}",
                # A 404 here is the definitive, expected confirmation that the
                # deal is gone (see below) — not a failure. Flag it so both the
                # queue and the client log it at WARNING, not ERROR, and keep it
                # out of the persistent error log / guard.
                expect_not_found=True,
            )
        except IGAPIError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 404:
                return True  # IG confirms the deal is not open — safe to reconcile
            logger.warning(
                "Vanished-confirm for %s inconclusive (HTTP %s) — keeping OPEN",
                position.epic,
                status,
            )
            return False
        except Exception as exc:  # noqa: BLE001 — network/timeout is transient
            logger.warning(
                "Vanished-confirm for %s errored (%s) — keeping OPEN",
                position.epic,
                exc,
            )
            return False
        # 200: the position is still open at IG — the bulk-list miss was transient.
        logger.info(
            "Position %s still open at IG on targeted re-fetch — transient "
            "bulk /positions miss, not reconciling",
            position.epic,
        )
        return False

    def _opened_within(self, position: Position, seconds: float) -> bool:
        """Whether ``position`` was opened less than ``seconds`` ago (UTC).

        ``time_open`` is stored naive-UTC (``datetime.now(UTC).time()`` at open),
        so it is recombined with ``date`` and compared against the current naive
        UTC clock. Returns ``False`` when either field is missing — an undated row
        is never treated as "fresh".
        """
        if position.date is None or position.time_open is None:
            return False
        opened_at = datetime.combine(position.date, position.time_open)
        now = datetime.now(UTC).replace(tzinfo=None)
        return (now - opened_at).total_seconds() < seconds

    def _mark_never_opened(self, position: Position) -> None:
        """Mark a provisional row that never became a real IG position.

        A row whose ``deal_id`` never bound and that ``GET /positions`` never
        lists (past the grace window) corresponds to an order that did not
        execute — the ``/confirms`` round-trip failed or IG rejected it without a
        clean rejection signal. It was never a trade, so it must NOT be recorded
        as a ``closed_externally`` close at €0: that manufactures a phantom that
        is counted in the day's trade count and drags the win rate. It is flagged
        with a distinct ``never_opened`` reason instead, which the dashboard
        excludes from every win/loss/trade aggregation. The row is kept (not
        deleted) for an audit trail; should the position actually surface at IG
        later it carries no ``deal_id``, so the adoption path re-creates it.
        """
        now = datetime.now(UTC)
        position.state = PositionState.CLOSE
        position.time_close = now.time()
        position.reason_close = "never_opened"
        position.euro = Decimal("0")
        position.win = 0
        if position.level_close is None:
            position.level_close = position.level_open
        logger.warning(
            "Position %s (dealRef=%s) never bound a dealId and is absent from IG "
            "%.0fs after open — marking never_opened (excluded from stats)",
            position.epic,
            position.deal_reference,
            RECONCILE_GRACE_SECONDS,
        )

    async def manage_position(
        self,
        position: Position,
        current_bid: float,
        buf: EpicBuffer | None = None,
        *,
        group_tighten: float | None = None,
    ) -> bool:
        """Decoupled close path: let the position's close profile decide.

        The exit is owned entirely by the :class:`CloseProfile` (configured
        independently of the entry strategy). This delegates the per-tick
        decision to it and applies the result — close the position, ratchet the
        protective stop, or hold. Falls back to :meth:`check_and_close` when no
        close profile is wired (keeps older call sites working).

        ``group_tighten`` is the pre-resolved stop level from a group-aware
        profile's portfolio pre-pass (``smartgroup``), computed once per monitor
        tick across the whole book and passed straight to the profile; ``None``
        for the ordinary per-position path.

        Returns:
            True if the position was closed, False otherwise.
        """
        # One direction-aware close profile manages both sides (see
        # :mod:`src.exit.close_zoneprofit`): the zones, their ``CLOSE_ZONE*``
        # selectors and the ratchet all mirror themselves for a SELL.
        profile: CloseProfile | None = self._close_profile

        if profile is None or buf is None or buf.last is None:
            # ``check_and_close`` implements long-only close maths (``loose`` fires
            # on ``bid <= stop``). A SELL must NEVER fall into it: a short's stop
            # sits ABOVE the price, so that test is true on almost every tick and
            # would close the short at market — e.g. on the first
            # monitor tick after a restart, before the epic's price buffer is
            # streamed (``buf is None``). Without a buffer the close profile cannot
            # run either, so hold and rely on the broker-side stop pushed at open.
            if position.direction == "SELL":
                return False
            return await self.check_and_close(position, current_bid, buf)

        decision = profile.evaluate(
            position,
            current_bid,
            buf,
            is_close_hour=await self._is_epic_close_hour(position.epic),
            group_tighten=group_tighten,
        )
        # A hard close (backstop / end-of-day) is always honoured, even while a
        # manual stop override is active — the user's placed stop IS the follower
        # the backstop fires on.
        if decision.action == ACTION_CLOSE:
            return await self._close_position(position, current_bid, decision.reason)

        # Manual stop override (dashboard chart buttons): while the bid stays in
        # the zone the stop was raised in, hold it and suspend automatic
        # ratcheting; resume automatic management the moment the bid crosses into
        # a different zone. A missing/unclassifiable zone keeps holding (never
        # clears on a transient blip), so the override only releases on a
        # definite zone change.
        manual_zone = getattr(position, "manual_stop_zone", None)
        if manual_zone:
            zone = profile.current_zone(position, current_bid, buf)
            if zone is None or zone.value == manual_zone:
                return False
            position.manual_stop_zone = None
            await self._db.commit()
            logger.info(
                "Manual stop hold released for %s (bid left zone %s) — auto resumes",
                position.epic,
                manual_zone,
            )

        if (
            decision.action == ACTION_UPDATE_STOP
            and decision.new_stop_level is not None
        ):
            new_stop = decision.new_stop_level
            current = float(position.level_follower or 0)
            # Ratchet invariant, enforced here regardless of what the updater
            # returned: a long's protective stop only ever moves up, a short's
            # only ever down. Defence-in-depth on top of each updater's own guard
            # — a stop already secured (e.g. pushed by the profit zone) must never
            # be pulled back when a pull-back re-enters a lower zone. Rounding is
            # 5 dp (matching the level column and stop_history) so the comparison
            # is exact; a coarser round would let a genuinely lower stop slip past.
            if current > 0 and not self._is_favourable_stop_move(
                position.direction, new_stop, current
            ):
                return False
            # Advance the follower and push the matching broker stop — clamped to
            # IG's minimum-distance floor and only persisted on acceptance (see
            # :meth:`_ratchet_stop`). The broker stop sits a spread plus an
            # ATR-scaled noise cushion beyond the follower so tick noise can't trip
            # it before the (poll-sampled) follower would fire. The min-distance
            # clamp is measured from the close-out price (the offer for a short), so
            # a short's floor is not read one spread too close.
            spread = float(buf.last.spread or 0)
            close_out = (
                current_bid + spread if position.direction == "SELL" else current_bid
            )
            await self._ratchet_stop(
                position,
                new_stop,
                close_out,
                spread,
                self._broker_stop_buffer(buf),
            )
            logger.debug(
                "Trailing stop for %s -> %.3f (profile=%s)",
                position.epic,
                new_stop,
                profile.name,
            )
        return False

    @staticmethod
    def _is_favourable_stop_move(
        direction: str | None, new_stop: float, current: float
    ) -> bool:
        """True when moving the follower to ``new_stop`` tightens protection.

        A long's stop only ever ratchets **up**; a short's only ever **down**.
        Used to enforce the ratchet invariant when applying a stop update.
        """
        if direction == "SELL":
            return new_stop < current
        return new_stop > current

    async def check_and_close(
        self,
        position: Position,
        current_bid: float,
        buf: EpicBuffer | None = None,
    ) -> bool:
        """Check if a position should be closed based on current price.

        Implements closing strategies from apiCheckPosition.php:
        - Win: close when bid reaches level_win
        - Follower: trail the stop, close when bid drops below level_follower
        - Loose: close when bid drops below level_loose

        Args:
            position: Open position to evaluate.
            current_bid: Current market bid price.
            buf: Price buffer for the epic, used to compute the ATR-based
                trailing distance. Without it the follower stop is not updated.

        Returns:
            True if position was closed, False otherwise.
        """
        level_open = float(position.level_open or 0)

        reason = decide_close_reason(
            current_bid,
            level_win=float(position.level_win or 0),
            level_loose=float(position.level_loose or 0),
            is_close_hour=await self._is_epic_close_hour(position.epic),
        )

        if reason is None:
            # Trail the stop upward with an ATR-based distance once in profit.
            if current_bid > level_open:
                await self._update_trailing_stop(position, current_bid, buf)
            return False

        # Close the position
        return await self._close_position(position, current_bid, reason)

    async def _update_trailing_stop(
        self, position: Position, current_bid: float, buf: EpicBuffer | None
    ) -> None:
        """Trail the stop upward with a volatility-adaptive (ATR) distance.

        The distance is sized from the recent ATR so the stop sits beyond
        normal market noise: wide before break-even to let the trade breathe,
        tighter once the price clears ``level_zero`` to lock in the gain. The
        stop only ratchets up, never down, and is pushed to IG so it survives a
        bot restart.
        """
        if buf is None or buf.last is None:
            return

        atr_value = atr(list(buf.candles), self._config.atr_period)

        new_stop = compute_trailing_stop(
            current_bid,
            atr_value=atr_value,
            spread=buf.last.spread,
            level_zero=float(position.level_zero or 0),
            level_follower=float(position.level_follower or 0),
            euro_per_point=float(position.euro_per_point or 0),
            euro_stop=abs(float(position.euro_stop or 0)),
            config=self._config,
        )
        if new_stop is None:
            return

        # Advance the follower and push the matching broker stop — clamped to
        # IG's minimum-distance floor and only persisted on acceptance (see
        # :meth:`_ratchet_stop`). 5 dp rounding there matches the level column and
        # stop_history so the next tick's guard compares against the real level.
        # The broker stop sits a spread plus an ATR-scaled noise cushion below the
        # follower so bid noise can't trip it before the follower would fire.
        await self._ratchet_stop(
            position,
            new_stop,
            current_bid,
            float(buf.last.spread or 0),
            self._broker_stop_buffer(buf),
        )
        logger.debug(
            "Trailing stop for %s -> %.3f (ATR=%.3f)",
            position.epic,
            new_stop,
            atr_value,
        )

    @staticmethod
    def _append_stop_history(
        position: Position, level: float, broker_level: float | None = None
    ) -> None:
        """Record a timestamped point on the stop's trajectory.

        Appended on every ratchet (and seeded with the initial stop at open) so
        the chart can draw the stop's real stepped path rather than a single
        flat line at the frozen initial level. A fresh list is assigned (not an
        in-place append) so the ORM detects the change on the plain JSON column.

        ``level`` is the software follower the close profile enforces. When
        ``broker_level`` is given it is the level actually posted at IG — one
        spread beyond the follower (see :meth:`_broker_stop_level`) — recorded
        per point so the chart's broker ("Loose") line reflects the real pushed
        level rather than the software follower.
        """
        point = {"t": datetime.now(UTC).isoformat(), "level": round(level, 5)}
        if broker_level is not None:
            point["broker"] = round(broker_level, 5)
        position.stop_history = [*(position.stop_history or []), point]

    @staticmethod
    def _broker_stop_level(
        direction: str | None,
        software_stop: float,
        spread: float,
        buffer: float = 0.0,
    ) -> float:
        """Broker stop level: one spread plus a noise cushion beyond the follower.

        The software follower (``level_follower``) is the level the close profile
        decides a close on between two bid polls, so it is inherently
        noise-tolerant: a transient down-spike that recovers before the next poll
        never closes the trade. The stop actually posted at IG is a hard resting
        order that fires on any real-time tick. Placed only one spread beyond the
        follower, bid noise trips it before the follower gets its chance — closing
        a trade the software stop would have ridden through. So the broker stop is
        pushed a full spread PLUS ``buffer`` (an ATR-scaled noise cushion, see
        :meth:`_broker_stop_buffer`) further from price — BELOW for a long, ABOVE
        for a short — so only a sustained move (which the follower would honour
        too) reaches it, and the broker order stays a genuine deeper safety net
        for missed touches (e.g. ticks dropped from the livestream). Both ratchet
        together: each follower raise pushes a matching broker level this far
        below (a short's this far above). ``buffer=0`` restores the old one-spread
        offset.
        """
        cushion = spread + max(buffer, 0.0)
        if direction == "SELL":
            return software_stop + cushion
        return software_stop - cushion

    def _broker_stop_buffer(self, buf: EpicBuffer | None) -> float:
        """ATR-scaled noise cushion added between the follower and broker stop.

        Returns ``broker_stop_noise_atr × ATR`` in price units, sized from the
        same recent-ATR the trailing distance uses so the cushion tracks each
        market's live volatility (DAX noise ≠ forex noise). Zero when there is no
        buffer or the config disables it — the broker stop then sits exactly one
        spread beyond the follower (see :meth:`_broker_stop_level`).
        """
        k = self._config.broker_stop_noise_atr
        if buf is None or k <= 0:
            return 0.0
        atr_value = atr(list(buf.candles), self._config.atr_period)
        return max(k * atr_value, 0.0)

    @staticmethod
    def _clamp_broker_stop_to_min_distance(
        direction: str | None,
        broker_level: float,
        current_price: float,
        position: Position,
    ) -> float:
        """Pull the broker stop back onto IG's minimum-distance floor if needed.

        IG rejects a stop posted closer than ``minNormalStopOrLimitDistance``
        from the current price ("Stop trop près"), and the swallowed rejection
        then leaves the previous, far broker order live. ``min_stop_distance`` is
        that floor (padded, price units) captured at open. When the desired
        broker level sits inside the floor, move it out to the floor — the
        deepest still-accepted level. Returned unchanged when the floor is
        unknown (adopted/legacy rows) or the level is already outside it.
        """
        min_dist = float(position.min_stop_distance or 0)
        if min_dist <= 0:
            return broker_level
        if direction == "SELL":
            # A short's stop sits ABOVE price: keep it at least min_dist above.
            return max(broker_level, current_price + min_dist)
        # A long's stop sits BELOW price: keep it at least min_dist below.
        return min(broker_level, current_price - min_dist)

    async def _ratchet_stop(
        self,
        position: Position,
        new_stop: float,
        current_price: float,
        spread: float,
        buffer: float = 0.0,
    ) -> bool:
        """Advance the software follower and push the matching broker stop to IG.

        The broker stop rests one spread plus an ATR-scaled noise cushion
        (``buffer``) beyond the follower (a deeper, noise-tolerant safety net),
        clamped so it never sits inside IG's minimum-distance floor — the
        tightest level IG still accepts. The persisted broker levels
        (``level_stop`` / ``level_security``) and the chart's broker ("Loose")
        point advance ONLY when IG accepts the push; on a rejection they keep the
        last accepted level, so the broker line always reflects the order truly
        resting at IG. The software follower advances either way — it is the
        local guard that closes the position between bid polls.

        Returns True when IG accepted the pushed broker stop.
        """
        direction = position.direction
        broker_target = self._broker_stop_level(direction, new_stop, spread, buffer)
        broker_target = self._clamp_broker_stop_to_min_distance(
            direction, broker_target, current_price, position
        )
        # Broker stop is raise-only, mirroring the follower: never loosen a level
        # already resting at IG (the clamp above can pull it back on a shrinking
        # cushion, but a previously accepted, further level must stand).
        last_broker = float(position.level_stop or 0)
        if last_broker > 0 and not self._is_favourable_stop_move(
            direction, broker_target, last_broker
        ):
            broker_target = last_broker

        position.level_follower = Decimal(str(round(new_stop, 5)))
        position.stop_update = (position.stop_update or 0) + 1

        # Nothing to push when the broker level is unchanged (the follower crept
        # up but the min-distance floor pinned the broker stop): advance the
        # software follower and record the flat broker step without an IG call.
        if last_broker > 0 and round(last_broker, 5) == round(broker_target, 5):
            self._append_stop_history(position, new_stop, last_broker)
            await self._db.commit()
            return True

        pushed = await self._push_stop_to_ig(position, broker_target)
        if pushed:
            position.level_stop = Decimal(str(round(broker_target, 5)))
            position.level_security = Decimal(str(round(broker_target, 5)))
            accepted_broker = broker_target
        else:
            # Push refused (e.g. price drifted inside the floor between read and
            # landing): keep the last accepted broker level so the chart's Loose
            # line stays truthful; the software follower still guards locally.
            accepted_broker = float(position.level_stop or broker_target)
            logger.warning(
                "IG rejected the stop update for %s (follower->%.5f, broker "
                "target %.5f); broker stop stays at last accepted %.5f",
                position.epic,
                new_stop,
                broker_target,
                accepted_broker,
            )
        self._append_stop_history(position, new_stop, accepted_broker)
        await self._db.commit()
        return pushed

    def _clamp_trailing_distance(
        self, raw_distance: float, position: Position, spread: float
    ) -> float:
        """Bound the trailing distance — see :func:`clamp_trailing_distance`."""
        return clamp_trailing_distance(
            raw_distance,
            spread=spread,
            euro_per_point=float(position.euro_per_point or 0),
            euro_stop=abs(float(position.euro_stop or 0)),
        )

    async def _push_stop_to_ig(
        self, position: Position, stop_level: float, *, label: str | None = None
    ) -> bool:
        """Send the new stop level to IG via PUT /positions/otc/{dealId}.

        Uses URGENT priority so the write jumps ahead of price-collection reads.
        Failures are logged but not raised: the local ``level_follower`` still
        guards the position through ``check_and_close``. Returns True when the PUT
        was accepted by IG, False when it could not be pushed (no dealId or an IG
        rejection) so a caller can surface the outcome — the automated trailing
        path ignores the result, the manual raise reports it to the dashboard.

        ``label`` overrides the queue task label so a manual raise is
        distinguishable from an automatic trail in the API-queue view.
        """
        deal_id = await self._ensure_deal_id(position, f"trail {position.epic}")
        if not deal_id:
            logger.warning("Cannot push trailing stop for %s: no dealId", position.epic)
            return False

        payload = {
            # 5 dp, matching the open order (``round(stop_level, 5)``) and the
            # persisted ``level_follower``. Rounding to 3 dp on a 5-dp forex price
            # could shift the broker stop up to ~5 points from the software
            # follower — even to the wrong side of the bid, so IG rejects the PUT
            # and the swallowed warning leaves a stale broker stop behind.
            "stopLevel": round(stop_level, 5),
            "trailingStop": False,
        }
        try:
            await self._client.put(
                f"/positions/otc/{deal_id}",
                payload,
                version=2,
                priority=Priority.URGENT,
                label=label or f"trail {position.epic}: stop->{stop_level:.5f}",
            )
        except IGAPIError as exc:
            logger.warning("Failed to update IG stop for %s: %s", position.epic, exc)
            return False
        return True

    async def _ensure_deal_id(self, position: Position, label: str) -> str | None:
        """Return the position's dealId, resolving it from IG's list if missing."""
        if position.deal_id:
            return position.deal_id
        try:
            positions_data = await self._client.get(
                "/positions",
                version=2,
                priority=Priority.URGENT,
                label=f"{label}: resolve deal_id",
            )
        except Exception as exc:
            logger.warning("Could not resolve dealId for %s: %s", position.epic, exc)
            return None

        for entry in positions_data.get("positions", []):
            if entry.get("market", {}).get("epic") == position.epic:
                deal_id = entry.get("position", {}).get("dealId")
                if deal_id:
                    position.deal_id = deal_id
                    await self._db.commit()
                return deal_id
        return None

    async def set_stop_manually(
        self,
        position: Position,
        target_level: float,
        buf: EpicBuffer | None,
        *,
        profile: CloseProfile | None = None,
    ) -> tuple[bool, str]:
        """Manually move the protective stop to an absolute level (dashboard).

        Triggered by the chart's stop buttons: the user picks a price on the
        chart's scale and both the **software follower** (the level the bot closes
        on) and the **broker stop** (posted one spread beyond it, see
        :meth:`_broker_stop_level`) are moved to it. The stop can be moved **either
        way** — tightened or loosened — for a long or a short; the only constraint
        is that it must stay on the safe side of the live bid (below the bid for a
        long, above it for a short) so it does not force an immediate exit.

        The zone the bid sits in *now* is captured into
        ``position.manual_stop_zone`` so :meth:`manage_position` holds this stop
        (suspending automatic ratcheting) until the bid crosses into a different
        zone.

        Returns ``(ok, message)`` — ``message`` is an error reason when refused.
        """
        if buf is None or buf.last is None:
            return False, "No price data for this epic"
        current_bid = buf.last.bid_close
        spread = float(buf.last.spread or 0)
        direction = position.direction or "BUY"

        # Only safety gate: the stop must stay on the side of the CLOSE-OUT price
        # that does not instantly close the trade — the bid for a long, the offer
        # for a short (that is what the software backstop fires on, so a short's
        # stop parked inside the spread would close it on the next tick). Direction
        # of the move (tighten/loosen) is the user's choice — the buttons span the
        # whole price scale.
        if direction == "SELL":
            if target_level <= current_bid + spread:
                return False, "Stop must be above the current offer for a short"
        else:
            if target_level >= current_bid:
                return False, "Stop must be below the current bid"

        broker_stop = self._broker_stop_level(
            direction, target_level, spread, self._broker_stop_buffer(buf)
        )
        broker_stop = self._clamp_broker_stop_to_min_distance(
            direction, broker_stop, current_bid, position
        )

        position.level_follower = Decimal(str(round(target_level, 5)))
        position.stop_update = (position.stop_update or 0) + 1

        # Capture the zone so the automatic ratcheting stays suspended until price
        # leaves it (see manage_position). The close profile is direction-aware, so
        # the same instance classifies both sides and the manual-hold zone always
        # matches automatic management. None when no profile is wired: the stop is
        # then simply set once and the ratchet invariant takes over next tick.
        zone = (
            profile.current_zone(position, current_bid, buf)
            if profile is not None
            else None
        )
        position.manual_stop_zone = zone.value if zone is not None else None

        # Route the broker update through the API queue (URGENT), labelled as a
        # manual raise so it is identifiable in the queue view. The software
        # follower is already persisted, so a rejected/blocked broker push still
        # leaves the bot closing on the raised stop; we surface the outcome so the
        # dashboard notification says whether IG accepted it. The persisted broker
        # levels and the chart's Loose point advance only on acceptance, so the
        # violet line never shows a stop IG never took.
        pushed = await self._push_stop_to_ig(
            position,
            broker_stop,
            label=f"manual stop {position.epic}: stop->{broker_stop:.5f}",
        )
        if pushed:
            position.level_stop = Decimal(str(round(broker_stop, 5)))
            position.level_security = Decimal(str(round(broker_stop, 5)))
            accepted_broker = broker_stop
        else:
            accepted_broker = float(position.level_stop or broker_stop)
        self._append_stop_history(position, target_level, accepted_broker)
        await self._db.commit()
        logger.info(
            "Manual stop set for %s -> %.5f (broker %.5f, zone=%s, ig_ok=%s)",
            position.epic,
            target_level,
            broker_stop,
            position.manual_stop_zone or "—",
            pushed,
        )
        message = "ok" if pushed else "queued (broker update not confirmed by IG)"
        return True, message

    async def close_manually(self, position: Position, close_level: float) -> bool:
        """Force-close ``position`` now at ``close_level`` (dashboard manual close).

        Public entry point to the shared close path so callers outside this
        service (the dashboard route) delegate here instead of reimplementing the
        direction mirror (BUY to close a SELL), the short-aware P&L sign,
        dealId resolution and the IG confirm — all of which live in
        :meth:`_close_position`. ``reason_close`` is stamped ``"manual"``.
        """
        return await self._close_position(position, close_level, "manual")

    async def _close_position(
        self, position: Position, close_level: float, reason: str
    ) -> bool:
        """Close a position via the IG API and update DB.

        Args:
            position: Position to close.
            close_level: Price at which we're closing.
            reason: Reason for closing.

        Returns:
            True if successfully closed.
        """
        logger.info(
            "Closing position: epic=%s, reason=%s, level=%.2f",
            position.epic,
            reason,
            close_level,
        )

        deal_id = position.deal_id
        if not deal_id:
            try:
                # confirms is transient; fetch the live positions list instead
                positions_data = await self._client.get(
                    "/positions",
                    version=2,
                    priority=Priority.URGENT,
                    label=f"close {position.epic}: resolve deal_id",
                )
                for entry in positions_data.get("positions", []):
                    if entry.get("market", {}).get("epic") == position.epic:
                        deal_id = entry.get("position", {}).get("dealId")
                        if deal_id:
                            position.deal_id = deal_id
                            await self._db.commit()
                        break
            except Exception as exc:
                logger.warning(
                    "Could not resolve dealId for %s from positions list: %s",
                    position.epic,
                    exc,
                )

        if not deal_id:
            logger.warning(
                "Position %s not found in IG live positions — "
                "marking as closed (phantom)",
                position.epic,
            )
            now = datetime.now(UTC)
            # Estimate P&L from close_level (current market price) even for
            # phantom closes; reconcile_realized_pnl corrects it later from IG.
            euro_pnl = self._euro_pnl(position, close_level)
            position.state = PositionState.CLOSE
            position.time_close = now.time()
            position.level_close = Decimal(str(round(close_level, 5)))
            position.reason_close = "not_found_in_ig"
            position.euro = Decimal(str(round(euro_pnl, 3)))
            position.win = 1 if euro_pnl > 0 else 0
            await self._db.commit()
            return True

        logger.info("Closing %s with dealId=%s", position.epic, deal_id)
        # Closing side is the opposite of the open side: SELL to close a long,
        # BUY to close a short.
        close_direction = "BUY" if position.direction == "SELL" else "SELL"
        close_payload = {
            "dealId": deal_id,
            "direction": close_direction,
            "size": position.quantity or 1,
            "orderType": "MARKET",
            "timeInForce": "EXECUTE_AND_ELIMINATE",
            "forceOpen": False,
        }

        # Some epics (forwards, some futures) reject orderType=MARKET. When the
        # instrument already bounced a MARKET order before
        # (``_market_order_unsupported``) we close with a marketable LIMIT
        # directly; otherwise we send MARKET and fall back to LIMIT only if IG
        # rejects it at deal time (mirrors the open path).
        if position.epic in self._market_order_unsupported:
            close_payload = self._to_marketable_limit(close_payload, close_level)
            logger.info(
                "%s known to reject MARKET orders — "
                "closing with a marketable LIMIT at %.5f",
                position.epic,
                close_payload["level"],
            )

        try:
            result = await self._delete_close_order(
                close_payload,
                position.epic,
                f"close {position.epic}: {reason}",
                close_level,
            )
        except IGAPIError as exc:
            if exc.response.status_code == 404:
                # IG can't find the position — verify it's genuinely gone
                return await self._handle_phantom_close(
                    position, close_level, reason, exc
                )
            logger.error("Failed to close %s: %s", position.epic, exc)
            return False
        except Exception as exc:
            logger.error("Failed to close %s: %s", position.epic, exc)
            return False

        # Ask IG for the close confirmation: it carries the real fill level and
        # the realized profit in the account currency — both authoritative,
        # unlike our observed bid. Falls back to the observed level / computed
        # P&L when the confirmation is unavailable.
        fill_level, ig_profit, rejected = await self._fetch_close_result(
            result.get("dealReference"), position.epic
        )
        if rejected:
            # IG refused the close (e.g. market closed / EDITS_ONLY). The
            # position is still live at the broker — do NOT fabricate a close, or
            # a duplicate open plus mis-attributed P&L follows on the next sync.
            # Leave it OPEN so the close retries once the market is tradeable.
            return False
        if fill_level is not None:
            close_level = fill_level

        # Update position in DB
        now = datetime.now(UTC)
        euro_pnl = (
            ig_profit
            if ig_profit is not None
            else self._euro_pnl(position, close_level)
        )

        position.state = PositionState.CLOSE
        position.time_close = now.time()
        position.level_close = Decimal(str(round(close_level, 5)))
        position.reason_close = reason
        position.euro = Decimal(str(round(euro_pnl, 3)))
        position.euro_max = Decimal(
            str(round(max(euro_pnl, float(position.euro_max or 0)), 3))
        )
        position.euro_min = Decimal(
            str(round(min(euro_pnl, float(position.euro_min or 0)), 3))
        )
        position.win = 1 if euro_pnl > 0 else 0

        await self._db.commit()

        logger.info(
            "Position closed: epic=%s, reason=%s, P&L=%.2f€",
            position.epic,
            reason,
            euro_pnl,
        )

        return True

    async def _handle_phantom_close(
        self,
        position: Position,
        close_level: float,
        reason: str,
        original_exc: Exception,
    ) -> bool:
        """Handle a 404 from IG's close endpoint by checking the live positions list.

        IG returns 404 / notional.details.null.error when the position no longer
        exists on their side (expired, already closed, demo glitch). We verify by
        fetching /positions and, if the epic is absent, record a phantom close so
        the DB stays consistent.
        """
        logger.warning(
            "IG returned 404 closing %s — verifying via live positions: %s",
            position.epic,
            original_exc,
        )
        try:
            positions_data = await self._client.get(
                "/positions",
                version=2,
                priority=Priority.URGENT,
                label=f"close {position.epic}: phantom-verify",
            )
            still_open = any(
                entry.get("market", {}).get("epic") == position.epic
                for entry in positions_data.get("positions", [])
            )
        except Exception as verify_exc:
            logger.error(
                "Could not verify live positions for %s: %s",
                position.epic,
                verify_exc,
            )
            return False

        if still_open:
            logger.error(
                "Failed to close %s (still open at IG): %s",
                position.epic,
                original_exc,
            )
            return False

        logger.warning(
            "Position %s not found in IG live positions after 404 — "
            "marking as closed (phantom)",
            position.epic,
        )
        now = datetime.now(UTC)
        euro_pnl = self._euro_pnl(position, close_level)
        position.state = PositionState.CLOSE
        position.time_close = now.time()
        position.level_close = Decimal(str(round(close_level, 5)))
        position.reason_close = "not_found_in_ig"
        position.euro = Decimal(str(round(euro_pnl, 3)))
        position.win = 1 if euro_pnl > 0 else 0
        await self._db.commit()
        return True

    async def close_all_positions(self) -> int:
        """Force close all open positions (end of day)."""
        result = await self._db.execute(
            select(Position).where(Position.state == PositionState.OPEN)
        )
        positions = result.scalars().all()
        return await self._force_close(positions, "end_of_day")

    async def close_epics(self, epics: set[str], reason: str) -> int:
        """Force-close every open position whose epic is in ``epics``.

        Safety net for the per-epic close rule: when an open epic's market is no
        longer TRADEABLE, close it so a position can't be stranded past its
        market's close. Best-effort — IG may reject a deal on a market that has
        already closed, which is logged and counted as a failure.
        """
        if not epics:
            return 0
        result = await self._db.execute(
            select(Position).where(
                Position.state == PositionState.OPEN,
                Position.epic.in_(epics),
            )
        )
        positions = result.scalars().all()
        return await self._force_close(positions, reason)

    async def _force_close(self, positions, reason: str) -> int:
        """Close each given position at its current IG bid, with ``reason``."""
        closed = 0
        for position in positions:
            try:
                market = await self._client.get(
                    f"/markets/{position.epic}",
                    version=3,
                    priority=Priority.URGENT,
                    label=f"close {position.epic}: market",
                )
                bid = float(market.get("snapshot", {}).get("bid", 0))
                if await self._close_position(position, bid, reason):
                    closed += 1
            except Exception as exc:
                logger.error("Failed to close position %s: %s", position.epic, exc)

        if positions:
            logger.info(
                "Forced close (%s): %d/%d positions closed",
                reason,
                closed,
                len(positions),
            )
        return closed
