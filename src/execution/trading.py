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
from src.execution.recovery import RECOVERY_QTY_MULTIPLIER
from src.exit.base import ACTION_CLOSE, ACTION_UPDATE_STOP, CloseProfile
from src.exit.recovery_short import RecoveryShortProfile
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

    @classmethod
    def from_settings(cls, settings) -> "TradeConfig":
        """Build TradeConfig from application Settings."""
        return cls(
            close_margin_minutes=settings.strategy_close_margin_minutes,
            open_close_buffer_minutes=getattr(
                settings, "strategy_open_close_buffer_minutes", 60
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
        # Dedicated exit for recovery SELL positions (built lazily). The main
        # close profile is long-only; a short is routed here by ``manage_position``
        # regardless of the configured long profile.
        self._recovery_short_profile: RecoveryShortProfile | None = None
        # Epics that bounced a MARKET order with
        # ``MARKET_ORDER_NOT_SUPPORTED_CODE`` at deal time even though their
        # metadata did not flag it. IG's ``marketOrderPreference`` is an unreliable
        # hint for these (typically forwards), so once an epic proves it we open it
        # with a marketable LIMIT directly — the doomed MARKET (and its ERROR-level
        # queue log / persistent error entry) then happens at most once per epic
        # per process instead of on every scan.
        self._market_order_unsupported: set[str] = set()

    async def _is_epic_open(self, epic: str) -> bool:
        """Check if a position is already open for this epic."""
        result = await self._db.execute(
            select(Position).where(
                Position.epic == epic,
                Position.state == PositionState.OPEN,
            )
        )
        return result.scalar_one_or_none() is not None

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

    async def can_open_intent(self, intent: EntryIntent) -> tuple[bool, str]:
        """Pre-open gates for a decoupled :class:`EntryIntent`.

        The live market-open gate is the per-epic ``marketStatus == TRADEABLE``
        check (hourly tradable filter + re-check in :meth:`open_position`), not a
        wall-clock window, so ``in_trading_hours`` is always True. The simulator
        keeps its own hour gate (no live status to read).

        A ``closes_soon`` gate additionally rejects the open when the epic's own
        market closes within ``open_close_buffer_minutes`` (see
        :meth:`_is_epic_close_soon`), so we never open a trade the per-epic close
        rule would force-close almost immediately.
        """
        return evaluate_open_gates(
            epic=intent.epic,
            direction=intent.direction,
            in_trading_hours=True,
            epic_already_open=await self._is_epic_open(intent.epic),
            closes_soon=await self._is_epic_close_soon(intent.epic),
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
        """
        profile = close_profile or self._close_profile
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
            signal, quantity_multiplier=quantity_multiplier
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
        self, signal: TradingSignal, quantity_multiplier: int = 1
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

        Returns:
            Created Position object, or None if open failed.
        """
        epic = signal.epic
        levels = signal.levels

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

        # Absolute stop level chosen by the strategy, and its distance below the
        # entry in price terms.
        stop_level = levels.level_security
        stop_price_distance = levels.bid - stop_level

        # Never place the stop tighter than IG allows (margin included): clamp out
        # to the padded minimum.
        if stop_price_distance < min_stop_price:
            stop_price_distance = min_stop_price
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
        # contract size is unknown.
        currency = instrument.get("currencies", [{}])[0].get("code", "EUR")
        expiry = instrument.get("expiry", "-")
        epp = euro_per_point(market_data, quantity, currency)
        if epp:
            euro_risk = stop_price_distance * epp
        else:
            euro_risk = quantity * stop_price_distance

        # The broker stop sits one spread BELOW the software follower (a long):
        # the app-side stop (``level_follower == stop_level``) is reached first
        # between two bid polls and the broker order only ever fires as a deeper
        # safety net. This is the SAME offset applied on every later ratchet
        # (see ``_broker_stop_level``); applying it here too keeps the bot in
        # control of the exit from the open onward and right through the start
        # zone — the underwater updater holds this stop untouched until
        # break-even, so the spread cushion set here persists for the whole
        # pre-break-even life. Pushing the broker stop FURTHER from price never
        # violates IG's minimum-distance rule, and the euro risk is unchanged
        # because the bot still closes at the (nearer) software ``stop_level``.
        spread = max(float(levels.spread or 0.0), 0.0)
        broker_stop = self._broker_stop_level("BUY", stop_level, spread)

        # 5. Send order with an absolute stop level (avoids any point/price
        # unit conversion on the IG side). A BUY fills at the ask, so the
        # marketable-LIMIT fallback prices through ``levels.offer``.
        reference_price = levels.offer
        order_payload = {
            "epic": epic,
            "expiry": expiry,
            "direction": "BUY",
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
            "Opening: epic=%s, qty=%d, stop=%.5f (broker %.5f, -%.5f), risk=%.2f€",
            epic,
            quantity,
            stop_level,
            broker_stop,
            stop_price_distance,
            euro_risk,
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
            # level actually posted at IG — one spread below the software follower
            # (see ``broker_stop`` above), so the broker line starts a spread
            # under the follower line and the two ratchet together from there.
            level_security=Decimal(str(round(broker_stop, 5))),
            level_stop=Decimal(str(round(broker_stop, 5))),
            level_margin=Decimal(str(round(levels.level_margin, 5))),
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
        open_level = float(confirmation.get("level", levels.bid))
        position.deal_id = deal_id or None
        position.level_open = Decimal(str(round(open_level, 5)))
        await self._db.commit()

        logger.info(
            "Position opened: epic=%s, deal=%s, level=%.5f, stop=%.5f",
            epic,
            deal_id,
            open_level,
            stop_level,
        )

        return position

    async def open_recovery_short(
        self,
        closed_position: Position,
        buf: EpicBuffer,
        *,
        quantity_multiplier: int = RECOVERY_QTY_MULTIPLIER,
    ) -> Position | None:
        """Open a double-size SELL to recover a stopped-out long's loss.

        The loss-recovery feature calls this right after a long closes on the
        "trend-reversal at open" pattern (see :func:`src.execution.recovery`).
        It sells the same epic betting on the confirmed decline, at
        ``quantity_multiplier ×`` the closed long's size, with the mirrored short
        exit (:class:`~src.exit.recovery_short.RecoveryShortProfile`) placing the
        initial stop *above* the entry. The short is stamped ``reason_open =
        "recovery"`` so it can never itself trigger another recovery (anti-loop).

        Mirrors :meth:`open_position` for a SELL rather than reusing it, so the
        long-only open path stays untouched. Returns the created ``Position`` or
        ``None`` when the market is not tradeable, the stop breaks the dealing
        rules, or IG rejects the order.
        """
        epic = closed_position.epic
        last = buf.last
        if last is None:
            logger.info("No candle for %s — cannot open recovery short", epic)
            return None

        # A SELL is filled at the bid; that is the recovery short's entry.
        entry_level = last.bid_close
        profile = self._short_profile()
        plan = profile.initial_plan(entry_level=entry_level, direction="SELL", buf=buf)

        # 1. Market info + dealing rules (same validation as open_position).
        market_data = await self._client.get(
            f"/markets/{epic}",
            version=3,
            priority=Priority.URGENT,
            label=f"recovery {epic}: market",
        )
        instrument = market_data.get("instrument", {})
        snapshot = market_data.get("snapshot", {})
        dealing_rules = market_data.get("dealingRules", {})

        if snapshot.get("marketStatus") != "TRADEABLE":
            logger.info(
                "Recovery short skipped — %s not tradeable: %s",
                epic,
                snapshot.get("marketStatus"),
            )
            return None

        use_market_order = (
            self._supports_market_orders(instrument)
            and epic not in self._market_order_unsupported
        )
        if not use_market_order:
            logger.info(
                "Recovery short — %s does not support market orders "
                "(marketOrderPreference=%s) — opening with a marketable LIMIT",
                epic,
                instrument.get("marketOrderPreference"),
            )

        min_stop_rule = dealing_rules.get("minNormalStopOrLimitDistance", {})
        max_stop_rule = dealing_rules.get("maxStopOrLimitDistance", {})
        min_deal_size = dealing_rules.get("minDealSize", {}).get("value", 1)
        scaling_factor = (
            float(str(snapshot.get("scalingFactor", "1")).replace(",", "")) or 1.0
        )

        min_stop_price = self._rule_to_price_distance(
            min_stop_rule,
            0.0,
            reference_price=entry_level,
            scaling_factor=scaling_factor,
        )
        max_stop_price = self._rule_to_price_distance(
            max_stop_rule,
            float("inf"),
            reference_price=entry_level,
            scaling_factor=scaling_factor,
        )
        min_stop_price *= 1 + self._config.stop_min_distance_margin

        # A short's stop sits ABOVE the entry; the distance is positive upward.
        stop_level = plan.stop_level
        stop_price_distance = stop_level - entry_level
        if stop_price_distance < min_stop_price:
            stop_price_distance = min_stop_price
            stop_level = entry_level + stop_price_distance
        if stop_price_distance > max_stop_price:
            logger.info(
                "Recovery short stop too large for %s: %.5f > max %.5f",
                epic,
                stop_price_distance,
                max_stop_price,
            )
            return None

        # 2. Quantity — double the closed long's size (bounded to the min deal).
        base_qty = max(int(closed_position.quantity or int(min_deal_size) or 1), 1)
        quantity = base_qty * max(int(quantity_multiplier), 1)

        currency = instrument.get("currencies", [{}])[0].get("code", "EUR")
        expiry = instrument.get("expiry", "-")
        epp = euro_per_point(market_data, quantity, currency)
        euro_risk = stop_price_distance * epp if epp else quantity * stop_price_distance

        # The broker stop sits one spread ABOVE the software follower (a short):
        # the app-side stop is reached first between two bid polls and the broker
        # order only fires as a deeper safety net — the SAME offset applied on
        # every later ratchet (see ``_broker_stop_level``), so the spread cushion
        # holds from the open through the start zone.
        spread = max(float(last.spread or 0.0), 0.0)
        broker_stop = self._broker_stop_level("SELL", stop_level, spread)

        # 3. Send the SELL order with an absolute stop level (above entry). A SELL
        # fills at the bid, so the marketable-LIMIT fallback prices through the
        # entry bid (``entry_level``).
        reference_price = entry_level
        order_payload = {
            "epic": epic,
            "expiry": expiry,
            "direction": "SELL",
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
            "Recovery: epic=%s, qty=%d, stop=%.5f (broker %.5f, +%.5f), risk=%.2f€",
            epic,
            quantity,
            stop_level,
            broker_stop,
            stop_price_distance,
            euro_risk,
        )
        result = await self._post_open_order(
            order_payload, epic, f"recovery {epic}: order", reference_price
        )
        if result is None:
            return None

        deal_reference = result.get("dealReference")
        if not deal_reference:
            logger.error("No dealReference for recovery short %s", epic)
            return None

        now = datetime.now(UTC)
        position = Position(
            epic=epic,
            epic_name=instrument.get("name", epic)[:10],
            deal_reference=deal_reference,
            deal_id=None,
            direction="SELL",
            date=now.date(),
            time_open=now.time(),
            state=PositionState.OPEN,
            strategy=PositionStrategy.TARGET,
            reason_open="recovery",
            close_profile=plan.profile,
            level_open=Decimal(str(round(entry_level, 5))),
            level_win=Decimal("0"),
            level_zero=Decimal(str(round(plan.level_zero, 5))),
            level_follower=Decimal(str(round(stop_level, 5))),
            level_loose=Decimal(str(round(stop_level, 5))),
            # Broker-side levels track the level actually posted at IG — one spread
            # above the software follower for a short (see ``broker_stop`` above).
            level_security=Decimal(str(round(broker_stop, 5))),
            level_stop=Decimal(str(round(broker_stop, 5))),
            level_margin=Decimal(str(round(plan.level_margin, 5))),
            pip_spread=Decimal(str(round(last.spread, 5))),
            quantity=quantity,
            size=int(round(stop_price_distance * scaling_factor)),
            euro_stop=Decimal(str(round(euro_risk, 3))),
            euro_per_point=Decimal(str(round(epp, 6))) if epp else None,
            stop_history=[
                {
                    "t": now.isoformat(),
                    "level": round(float(stop_level), 5),
                    "broker": round(float(broker_stop), 5),
                }
            ],
        )
        self._db.add(position)
        await self._db.commit()
        await self._db.refresh(position)

        confirmation = await self._confirm_with_retry(deal_reference, epic)
        if confirmation is None:
            logger.error(
                "Could not confirm recovery short %s for %s — kept, sync reconciles",
                deal_reference,
                epic,
            )
            return position
        if confirmation.get("dealStatus") != "ACCEPTED":
            reason = confirmation.get("reason", "UNKNOWN")
            logger.warning(
                "Recovery short rejected for %s: %s — removing draft row", epic, reason
            )
            await self._db.delete(position)
            await self._db.commit()
            return None

        deal_id = confirmation.get("dealId", "")
        open_level = float(confirmation.get("level", entry_level))
        position.deal_id = deal_id or None
        position.level_open = Decimal(str(round(open_level, 5)))
        await self._db.commit()

        logger.info(
            "Recovery short opened: epic=%s, deal=%s, level=%.5f, stop=%.5f",
            epic,
            deal_id,
            open_level,
            stop_level,
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

        Positions are matched to transactions by deal reference first, then by
        instrument name when exactly one unmatched transaction remains for that
        instrument. Returns the number of positions updated.
        """
        day = day or date.today()
        result = await self._db.execute(
            select(Position).where(
                Position.date == day, Position.state == PositionState.CLOSE
            )
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
        # and disambiguate same-instrument positions by the closest open/close
        # levels. Each transaction is consumed once.
        remaining = list(transactions)
        updated = 0
        for position in closed:
            candidates = [
                t
                for t in remaining
                if self._names_match(position.epic_name, t.get("instrumentName"))
            ]
            if not candidates:
                logger.debug(
                    "No IG transaction matched closed position %s (%s)",
                    position.id,
                    position.epic,
                )
                continue
            txn = min(candidates, key=lambda t: self._level_distance(position, t))
            remaining.remove(txn)
            if self._apply_transaction(position, txn):
                updated += 1

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
    def _level_distance(position: Position, txn: dict) -> float:
        """Sum of |open Δ| + |close Δ| between a position and a transaction.

        Used to pick which transaction belongs to which position when several
        share an instrument. Missing levels contribute nothing.
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

        Returns:
            Map of ``epic -> live IG entry`` ({"position": ..., "market": ...})
            for every position still open at IG, so callers can reuse the data
            without issuing a second request.
        """
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
                dirty = True
                continue

            ig_position = entry.get("position", {})
            market = entry.get("market", {})

            # Refresh dealId if IG rotated it (stale id is the 404 root cause).
            ig_deal_id = ig_position.get("dealId")
            if ig_deal_id:
                claimed.add(ig_deal_id)
                if ig_deal_id != position.deal_id:
                    logger.info(
                        "Position %s dealId refreshed: %s -> %s",
                        position.epic,
                        position.deal_id,
                        ig_deal_id,
                    )
                    position.deal_id = ig_deal_id
                    dirty = True

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

        Only BUY positions are adopted — the whole close/trailing engine assumes a
        long; a SELL is logged and skipped rather than mismanaged.

        Returns the created ``Position`` (added to the session, not committed), or
        ``None`` when the entry is unusable or not a BUY.
        """
        ig_position = entry.get("position", {})
        market = entry.get("market", {})
        epic = market.get("epic")
        deal_id = ig_position.get("dealId")
        if not epic or not deal_id:
            return None

        direction = ig_position.get("direction")
        if direction != "BUY":
            logger.warning(
                "Not adopting non-BUY IG position %s (%s %s) — unmanaged by the "
                "long-only engine; close it manually if unwanted",
                deal_id,
                direction,
                epic,
            )
            return None

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

        stop_distance = max(open_level - stop_level, 0.0) if stop_level else 0.0
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
        position = Position(
            epic=epic,
            epic_name=(market.get("instrumentName") or epic)[:10],
            deal_reference=ig_position.get("dealReference"),
            deal_id=deal_id,
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
            euro=Decimal(str(round((bid - open_level) * epp, 3))) if epp else None,
            euro_stop=Decimal(str(round(euro_stop, 3))),
            euro_per_point=Decimal(str(round(epp, 6))) if epp else None,
        )
        self._db.add(position)
        logger.warning(
            "Adopted untracked IG position %s (%s) dealId=%s open=%.5f stop=%s "
            "epp=%.4f — now managed by the bot",
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
    ) -> bool:
        """Decoupled close path: let the position's close profile decide.

        The exit is owned entirely by the :class:`CloseProfile` (configured
        independently of the entry strategy). This delegates the per-tick
        decision to it and applies the result — close the position, ratchet the
        protective stop, or hold. Falls back to :meth:`check_and_close` when no
        close profile is wired (keeps older call sites working).

        Returns:
            True if the position was closed, False otherwise.
        """
        # Recovery SELL positions are managed by the mirrored short profile, never
        # by the long-only close profile — route on the persisted trade side.
        if position.direction == "SELL":
            profile: CloseProfile | None = self._short_profile()
        else:
            profile = self._close_profile

        if profile is None or buf is None or buf.last is None:
            # ``check_and_close`` implements long-only close maths (``loose`` fires
            # on ``bid <= stop``). A recovery SELL must NEVER fall into it: a
            # short's stop sits ABOVE the price, so that test is true on almost
            # every tick and would close the short at market — e.g. on the first
            # monitor tick after a restart, before the epic's price buffer is
            # streamed (``buf is None``). Without a buffer the mirrored short
            # profile cannot run either, so hold and rely on the broker-side stop
            # pushed at open.
            if position.direction == "SELL":
                return False
            return await self.check_and_close(position, current_bid, buf)

        decision = profile.evaluate(
            position,
            current_bid,
            buf,
            is_close_hour=await self._is_epic_close_hour(position.epic),
        )
        if decision.action == ACTION_CLOSE:
            return await self._close_position(position, current_bid, decision.reason)
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
            # The broker stop rests one spread beyond the software follower (live
            # spread at push time), so the app-side stop is hit first between two
            # bid polls and the broker order only fires as a deeper safety net.
            broker_stop = self._broker_stop_level(
                position.direction, new_stop, float(buf.last.spread or 0)
            )
            position.level_follower = Decimal(str(round(new_stop, 5)))
            position.stop_update = (position.stop_update or 0) + 1
            self._append_stop_history(position, new_stop, broker_stop)
            await self._push_stop_to_ig(position, broker_stop)
            await self._db.commit()
            logger.debug(
                "Trailing stop for %s -> %.3f (broker %.3f, profile=%s)",
                position.epic,
                new_stop,
                broker_stop,
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

    def _short_profile(self) -> RecoveryShortProfile:
        """The mirrored short exit for recovery SELL positions (built once)."""
        if self._recovery_short_profile is None:
            self._recovery_short_profile = RecoveryShortProfile()
        return self._recovery_short_profile

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

        # 5 dp to match the level column and stop_history: a coarser round would
        # drop the 4th/5th digit on forex (e.g. 1.62413 → 1.624) and let the next
        # tick's guard compare against a stop up to ~5 points below the real one,
        # which then reads as the follower stepping *down* on the chart.
        # The broker stop rests one spread beyond the software follower (live
        # spread at push time), so the app-side stop is hit first between two bid
        # polls and the broker order only fires as a deeper safety net.
        broker_stop = self._broker_stop_level(
            position.direction, new_stop, float(buf.last.spread or 0)
        )
        position.level_follower = Decimal(str(round(new_stop, 5)))
        position.stop_update = (position.stop_update or 0) + 1
        self._append_stop_history(position, new_stop, broker_stop)
        await self._push_stop_to_ig(position, broker_stop)
        await self._db.commit()
        logger.debug(
            "Trailing stop for %s -> %.3f (broker %.3f, ATR=%.3f)",
            position.epic,
            new_stop,
            broker_stop,
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
        direction: str | None, software_stop: float, spread: float
    ) -> float:
        """Broker stop level: one spread beyond the software follower.

        The software follower (``level_follower``) is the level the close profile
        decides a close on between two bid polls. The stop actually posted at IG
        is placed a full spread further from price — BELOW for a long, ABOVE for a
        short — so in normal operation the app-side stop is reached first and the
        broker order only ever fires as a deeper safety net when the bot misses
        the touch (e.g. ticks dropped from the livestream between two readings).
        Both ratchet together: each follower raise pushes a matching broker level
        a spread below (a short's a spread above).
        """
        if direction == "SELL":
            return software_stop + spread
        return software_stop - spread

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

    async def _push_stop_to_ig(self, position: Position, stop_level: float) -> None:
        """Send the new stop level to IG via PUT /positions/otc/{dealId}.

        Uses URGENT priority so the write jumps ahead of price-collection reads.
        Failures are logged but not raised: the local ``level_follower`` still
        guards the position through ``check_and_close``.
        """
        deal_id = await self._ensure_deal_id(position, f"trail {position.epic}")
        if not deal_id:
            logger.warning("Cannot push trailing stop for %s: no dealId", position.epic)
            return

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
                label=f"trail {position.epic}: stop->{stop_level:.5f}",
            )
        except IGAPIError as exc:
            logger.warning("Failed to update IG stop for %s: %s", position.epic, exc)

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

    async def close_manually(self, position: Position, close_level: float) -> bool:
        """Force-close ``position`` now at ``close_level`` (dashboard manual close).

        Public entry point to the shared close path so callers outside this
        service (the dashboard route) delegate here instead of reimplementing the
        direction mirror (BUY to close a recovery SELL), the short-aware P&L sign,
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
        # BUY to close a short (the recovery SELL).
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
