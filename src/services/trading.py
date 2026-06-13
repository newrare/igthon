"""Trading service — open/close positions. Ported from Action.php.

Implements the full trading workflow:
- Pre-open checks (market status, duplicates, stop limits, risk)
- Position opening via the IG API
- Position monitoring and closing (win/follower/loose strategies)
- Stop level updates (trailing stop)
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.client import IGAPIError, IGClient
from src.models.position import Position, PositionState, PositionStrategy
from src.services.api_queue import APIQueue, Priority
from src.services.compute import TradingSignal, atr
from src.services.price_buffer import EpicBuffer
from src.utils.tools import _to_float, euro_per_point, parse_ig_pnl

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TradeConfig:
    """Trading configuration parameters."""

    max_positions: int = 4
    hour_start: int = 9
    hour_end: int = 16
    hour_close: int = 17
    euro_loss_max: float = 4000.0
    day_euro_finish_win: float = 300.0
    day_euro_finish_loose: float = -500.0
    compensate_loose: bool = False
    close_strategy: str = "follower"  # win | follower | now | zero
    max_trades_day: int = 50
    min_win_rate: float = 0.40
    # Trailing stop (ATR-based adaptive follower)
    atr_period: int = 14
    atr_k_pre: float = 2.5
    atr_k_post: float = 1.5
    trailing_step_ratio: float = 0.3

    @classmethod
    def from_settings(cls, settings) -> "TradeConfig":
        """Build TradeConfig from application Settings."""
        return cls(
            max_positions=settings.strategy_max_positions,
            hour_start=settings.strategy_hour_start,
            hour_end=settings.strategy_hour_end,
            hour_close=settings.strategy_hour_close,
            euro_loss_max=settings.strategy_euro_loss,
            day_euro_finish_win=settings.strategy_daily_win_target,
            day_euro_finish_loose=settings.strategy_daily_loss_limit,
            compensate_loose=settings.strategy_compensate_loose,
            close_strategy=settings.strategy_close_target,
            max_trades_day=settings.strategy_max_trades_day,
            min_win_rate=settings.strategy_min_win_rate,
            atr_period=settings.strategy_atr_period,
            atr_k_pre=settings.strategy_atr_k_pre,
            atr_k_post=settings.strategy_atr_k_post,
            trailing_step_ratio=settings.strategy_trailing_step_ratio,
        )


def evaluate_open_gates(
    *,
    epic: str,
    direction: str,
    in_trading_hours: bool,
    epic_already_open: bool,
    open_count: int,
    daily_pnl: float,
    trade_count: int,
    win_rate: float,
    config: TradeConfig,
) -> tuple[bool, str]:
    """Pure pre-open rule evaluation shared by live trading and the simulator.

    Mirrors ``TradingService.can_open_position``: the service gathers the live
    state (DB counts, daily P&L) and this function applies the rules to it.

    Returns:
        (allowed, reason) — reason explains the first failed gate.
    """
    if not in_trading_hours:
        return False, "Outside trading hours"

    if direction != "BUY":
        return False, f"Signal direction is {direction}"

    if epic_already_open:
        return False, f"Epic {epic} already open"

    if open_count >= config.max_positions:
        return False, f"Max positions reached ({open_count})"

    if daily_pnl <= config.day_euro_finish_loose:
        return False, f"Daily loss limit reached ({daily_pnl:.2f}€)"
    if daily_pnl >= config.day_euro_finish_win:
        return False, f"Daily target reached ({daily_pnl:.2f}€)"

    if trade_count >= config.max_trades_day:
        return False, f"Max daily trades reached ({trade_count})"
    if trade_count >= 10 and win_rate < config.min_win_rate:
        return (
            False,
            f"Win rate too low ({win_rate:.0%} after {trade_count} trades)",
        )

    return True, "OK"


def decide_close_reason(
    current_bid: float,
    *,
    level_win: float,
    level_loose: float,
    is_close_hour: bool,
) -> str | None:
    """Pure close-rule evaluation shared by live trading and the simulator.

    Returns the close reason ("end_of_day" | "win" | "loose") or None when the
    position should stay open (the follower/trailing update is handled
    separately by the caller).

    ``level_win = 0`` means the strategy has no fixed take-profit (e.g. the
    Donchian breakout rides its trailing stop) — the win check is skipped.
    """
    if is_close_hour:
        return "end_of_day"
    if level_win > 0 and current_bid >= level_win:
        return "win"
    if level_loose > 0 and current_bid <= level_loose:
        return "loose"
    return None


def clamp_trailing_distance(
    raw_distance: float,
    *,
    spread: float,
    euro_per_point: float,
    euro_stop: float,
) -> float:
    """Bound the trailing distance between two safety limits.

    Floor: a couple of spreads, so the bid/offer oscillation alone cannot
    trigger the stop (avoids closing on noise). Ceiling: the initial planned
    euro risk (``euro_stop`` / ``euro_per_point``), so the trailing stop is
    never further from price than the loss accepted at open.
    """
    distance = raw_distance
    if euro_per_point > 0 and euro_stop > 0:
        distance = min(distance, euro_stop / euro_per_point)
    floor = max(spread * 2.0, 0.0)
    return max(distance, floor)


def compute_trailing_stop(
    current_bid: float,
    *,
    atr_value: float,
    spread: float,
    level_zero: float,
    level_follower: float,
    euro_per_point: float,
    euro_stop: float,
    config: TradeConfig,
) -> float | None:
    """Pure ATR trailing-stop computation shared by live trading and the simulator.

    Implements the two-speed regime (looser before break-even, tighter once
    past ``level_zero``) and the upward-only ratchet with a minimum step.

    Returns:
        The new stop level, or None when no update is warranted.
    """
    if atr_value <= 0:
        return None

    past_zero = level_zero > 0 and current_bid >= level_zero
    k = config.atr_k_post if past_zero else config.atr_k_pre
    distance = clamp_trailing_distance(
        k * atr_value,
        spread=spread,
        euro_per_point=euro_per_point,
        euro_stop=euro_stop,
    )

    new_stop = current_bid - distance
    # Once break-even is cleared, never let the stop fall back into a loss.
    if past_zero:
        new_stop = max(new_stop, level_zero)

    # Ratchet: only move up, and only when the gain is worth an API write.
    step = config.trailing_step_ratio * atr_value
    if new_stop <= level_follower + step:
        return None
    return new_stop


def compute_breakeven_stop(
    current_bid: float,
    *,
    level_zero: float,
    current_stop: float,
    spread: float,
    min_stop_distance: float = 0.0,
    buffer_mult: float = 1.0,
    margin_mult: float = 2.0,
) -> float | None:
    """Lock the stop just above break-even once price has moved safely past it.

    This is deliberately *independent* of the ATR ratchet in
    :func:`compute_trailing_stop`: its sole purpose is to make a trade that has
    gone positive risk-free. As soon as the bid sits a safe distance above
    ``level_zero`` the stop is pulled up to ``level_zero + buffer_mult * spread``
    — a hair above break-even — so a later reversal closes the trade flat (or
    marginally green) instead of at the opening loss-stop. Because it does not
    need an ATR value, it still protects early in a session or after a restart,
    when the candle buffer is too short for ``atr()`` to return a value.

    The move is only proposed when the broker would accept it: the locked stop
    is kept at least ``safe_distance`` below the current bid, where
    ``safe_distance`` is the larger of IG's minimum stop distance
    (``min_stop_distance``, a price distance) and ``margin_mult * spread``. While
    the bid is not yet that far above break-even, None is returned (wait for more
    room) rather than pushing a stop IG would refuse for being too tight — the
    margin the caller must keep between the new stop and the live market.

    Returns:
        The break-even stop level, or None when locking is not yet safe or would
        not improve on ``current_stop`` (ratchet: the stop never moves down).
    """
    if level_zero <= 0:
        return None
    target = level_zero + buffer_mult * spread
    # Ratchet: never move the stop down — only lock in once it beats the live one.
    if target <= current_stop:
        return None
    # Keep a safety margin so IG neither rejects the update (too tight) nor
    # triggers it on the spread alone.
    safe_distance = max(min_stop_distance, margin_mult * spread)
    if current_bid - target < safe_distance:
        return None
    return target


class TradingService:
    """Service for opening and closing trading positions.

    Ported from Action.php with improved validation and async support.
    """

    def __init__(
        self,
        client: IGClient | APIQueue,
        db_session: AsyncSession,
        config: TradeConfig,
    ) -> None:
        self._client = client
        self._db = db_session
        self._config = config

    async def _count_open_positions(self) -> int:
        """Count currently open positions in DB."""
        result = await self._db.execute(
            select(Position).where(Position.state == PositionState.OPEN)
        )
        return len(result.scalars().all())

    async def _is_epic_open(self, epic: str) -> bool:
        """Check if a position is already open for this epic."""
        result = await self._db.execute(
            select(Position).where(
                Position.epic == epic,
                Position.state == PositionState.OPEN,
            )
        )
        return result.scalar_one_or_none() is not None

    async def _get_daily_pnl(self) -> float:
        """Get today's total P&L from closed positions."""
        today = date.today()
        result = await self._db.execute(
            select(Position).where(
                Position.date == today,
                Position.state == PositionState.CLOSE,
            )
        )
        positions = result.scalars().all()
        return sum(float(p.euro or 0) for p in positions)

    async def _get_daily_stats(self) -> tuple[int, float]:
        """Get today's trade count and win rate.

        Returns:
            (trade_count, win_rate) where win_rate is 0-1.
        """
        today = date.today()
        result = await self._db.execute(
            select(Position).where(
                Position.date == today,
                Position.state == PositionState.CLOSE,
            )
        )
        positions = result.scalars().all()
        count = len(positions)
        if count == 0:
            return 0, 1.0
        wins = sum(1 for p in positions if (p.win or 0) > 0)
        return count, wins / count

    def _is_trading_hours(self) -> bool:
        """Check if we're within allowed trading hours."""
        now = datetime.now(UTC)
        return self._config.hour_start <= now.hour < self._config.hour_end

    def _is_close_hours(self) -> bool:
        """Check if we've passed the forced-close hour."""
        now = datetime.now(UTC)
        return now.hour >= self._config.hour_close

    async def can_open_position(self, signal: TradingSignal) -> tuple[bool, str]:
        """Run all pre-open checks. Returns (allowed, reason).

        Checks:
        1. Trading hours
        2. Signal direction is BUY
        3. No duplicate epic open
        4. Max simultaneous positions
        5. Daily P&L circuit breaker
        6. Market is TRADEABLE (checked during open_position)
        """
        trade_count, win_rate = await self._get_daily_stats()
        return evaluate_open_gates(
            epic=signal.epic,
            direction=signal.direction,
            in_trading_hours=self._is_trading_hours(),
            epic_already_open=await self._is_epic_open(signal.epic),
            open_count=await self._count_open_positions(),
            daily_pnl=await self._get_daily_pnl(),
            trade_count=trade_count,
            win_rate=win_rate,
            config=self._config,
        )

    async def open_position(self, signal: TradingSignal) -> Position | None:
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

        def _rule_to_price_distance(rule: dict, default: float) -> float:
            raw = rule.get("value")
            if raw is None:
                return default
            value = float(raw)
            if rule.get("unit") == "PERCENTAGE":
                return value * levels.bid / 100
            # POINTS (the IG default): scale points back to a price distance.
            return value / scaling_factor

        min_stop_price = _rule_to_price_distance(min_stop_rule, 0.0)
        max_stop_price = _rule_to_price_distance(max_stop_rule, float("inf"))

        # Absolute stop level chosen by the strategy, and its distance below the
        # entry in price terms.
        stop_level = levels.level_security
        stop_price_distance = levels.bid - stop_level

        # Never place the stop tighter than IG allows: clamp out to the minimum.
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

        # 3. Quantity
        quantity = max(int(min_deal_size), 1)

        # 4. Check euro risk. ``euro_per_point`` is the currency-converted euro
        # value of one full point of price movement for the whole position, so
        # the worst-case loss is simply distance × euro_per_point. Fall back to a
        # rough estimate only when the contract size is unknown.
        currency = instrument.get("currencies", [{}])[0].get("code", "EUR")
        expiry = instrument.get("expiry", "-")
        epp = euro_per_point(market_data, quantity, currency)
        if epp:
            euro_risk = stop_price_distance * epp
        else:
            euro_risk = quantity * stop_price_distance

        if euro_risk > self._config.euro_loss_max:
            logger.info(
                "Euro risk too high for %s: %.2f > %.2f",
                epic,
                euro_risk,
                self._config.euro_loss_max,
            )
            return None

        # 5. Send order with an absolute stop level (avoids any point/price
        # unit conversion on the IG side).
        order_payload = {
            "epic": epic,
            "expiry": expiry,
            "direction": "BUY",
            "size": str(quantity),
            "orderType": "MARKET",
            "currencyCode": currency,
            "guaranteedStop": False,
            "stopLevel": round(stop_level, 5),
            "forceOpen": True,
        }

        logger.info(
            "Opening position: epic=%s, qty=%d, stop=%.5f (-%.5f), risk=%.2f€",
            epic,
            quantity,
            stop_level,
            stop_price_distance,
            euro_risk,
        )

        try:
            result = await self._client.post(
                "/positions/otc",
                order_payload,
                version=2,
                priority=Priority.URGENT,
                label=f"open {epic}: order",
            )
        except Exception as exc:
            logger.error("Failed to open position for %s: %s", epic, exc)
            return None

        deal_reference = result.get("dealReference")
        if not deal_reference:
            logger.error("No dealReference returned for %s", epic)
            return None

        # 5. Confirm deal
        try:
            confirmation = await self._client.get(
                f"/confirms/{deal_reference}",
                version=1,
                priority=Priority.URGENT,
                label=f"open {epic}: confirm",
            )
        except Exception as exc:
            logger.error("Failed to confirm deal %s: %s", deal_reference, exc)
            return None

        if confirmation.get("dealStatus") != "ACCEPTED":
            reason = confirmation.get("reason", "UNKNOWN")
            logger.warning("Deal rejected for %s: %s", epic, reason)
            return None

        deal_id = confirmation.get("dealId", "")
        open_level = float(confirmation.get("level", levels.bid))

        # ``epp`` (currency-converted euro value of one point of movement) was
        # already computed above for the risk check; it is the basis for every
        # P&L figure (live and realized).

        # 6. Record in DB
        now = datetime.now(UTC)
        position = Position(
            epic=epic,
            epic_name=instrument.get("name", epic)[:10],
            deal_reference=deal_reference,
            deal_id=deal_id,
            date=now.date(),
            time_open=now.time(),
            state=PositionState.OPEN,
            strategy=PositionStrategy.TARGET,
            reason_open="auto",
            level_open=Decimal(str(round(open_level, 5))),
            level_win=Decimal(str(round(levels.level_win, 5))),
            level_zero=Decimal(str(round(levels.level_zero, 5))),
            level_follower=Decimal(str(round(levels.level_follower, 5))),
            level_loose=Decimal(str(round(levels.level_loose, 5))),
            level_security=Decimal(str(round(levels.level_security, 5))),
            level_stop=Decimal(str(round(stop_level, 5))),
            pip_spread=Decimal(str(round(levels.spread, 5))),
            quantity=quantity,
            size=int(round(stop_price_distance * scaling_factor)),
            euro_stop=Decimal(str(round(euro_risk, 3))),
            euro_per_point=Decimal(str(round(epp, 6))) if epp else None,
        )

        self._db.add(position)
        await self._db.commit()
        await self._db.refresh(position)

        logger.info(
            "Position opened: epic=%s, deal=%s, level=%.5f, stop=%.5f",
            epic,
            deal_id,
            open_level,
            stop_level,
        )

        return position

    def _euro_pnl(self, position: Position, level: float) -> float:
        """Compute the euro P&L of a position at a given market level.

        Preferred path: ``euro_per_point`` is the currency-converted euro value
        of one full point of movement for the whole position, so the P&L is
        simply ``(level - open) * euro_per_point``. This is correct for JPY/USD
        pairs (currency conversion applied) and indices alike.

        Legacy fallback (positions opened before ``euro_per_point`` existed):
        derive a per-pip value from ``euro_stop`` / ``size`` / ``quantity``.
        Note this fallback ignores currency conversion and is only an estimate
        until ``reconcile_realized_pnl`` overwrites it with IG's figure.
        """
        open_level = float(position.level_open or 0)
        move = level - open_level
        if position.euro_per_point is not None and float(position.euro_per_point) != 0:
            return move * float(position.euro_per_point)
        euro_per_pip = (
            float(position.euro_stop or 1)
            / float(position.size or 1)
            / float(position.quantity or 1)
        )
        return move * (position.quantity or 1) * euro_per_pip

    async def _fetch_close_result(
        self, deal_reference: str | None, epic: str
    ) -> tuple[float | None, float | None]:
        """Return ``(fill_level, realized_profit_eur)`` from a close confirmation.

        Either element is ``None`` when the confirmation is missing or omits
        that field. The confirmation's ``profit`` is already in the account
        currency; it is only trusted when ``profitCurrency`` confirms EUR.
        """
        if not deal_reference:
            return None, None
        try:
            confirm = await self._client.get(
                f"/confirms/{deal_reference}",
                version=1,
                priority=Priority.URGENT,
                label=f"close {epic}: confirm",
            )
        except Exception as exc:
            logger.debug("Could not fetch close confirmation for %s: %s", epic, exc)
            return None, None

        level = confirm.get("level")
        fill_level = float(level) if level is not None else None

        profit = confirm.get("profit")
        profit_ccy = confirm.get("profitCurrency")
        ig_profit = (
            float(profit)
            if profit is not None and profit_ccy in (None, "", "EUR", "E", "€")
            else None
        )
        return fill_level, ig_profit

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
        """Write a transaction's authoritative P&L and levels onto a position."""
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

        Returns:
            Map of ``epic -> live IG entry`` ({"position": ..., "market": ...})
            for every position still open at IG, so callers can reuse the data
            without issuing a second request.
        """
        result = await self._db.execute(
            select(Position).where(Position.state == PositionState.OPEN)
        )
        db_positions = result.scalars().all()
        if not db_positions:
            return {}

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

        live: dict[str, dict] = {}
        for entry in data.get("positions", []):
            epic = entry.get("market", {}).get("epic")
            if epic:
                live[epic] = entry

        dirty = False
        for position in db_positions:
            entry = live.get(position.epic)
            if entry is None:
                # Position is gone at IG — closed or expired outside the bot.
                self._reconcile_vanished(position)
                dirty = True
                continue

            ig_position = entry.get("position", {})
            market = entry.get("market", {})

            # Refresh dealId if IG rotated it (stale id is the 404 root cause).
            ig_deal_id = ig_position.get("dealId")
            if ig_deal_id and ig_deal_id != position.deal_id:
                logger.info(
                    "Position %s dealId refreshed: %s -> %s",
                    position.epic,
                    position.deal_id,
                    ig_deal_id,
                )
                position.deal_id = ig_deal_id
                dirty = True

            # Update live unrealized P&L and excursion from the current bid.
            bid = market.get("bid")
            if bid is not None:
                euro_pnl = self._euro_pnl(position, float(bid))
                position.euro = Decimal(str(round(euro_pnl, 3)))
                position.euro_max = Decimal(
                    str(round(max(euro_pnl, float(position.euro_max or euro_pnl)), 3))
                )
                position.euro_min = Decimal(
                    str(round(min(euro_pnl, float(position.euro_min or euro_pnl)), 3))
                )
                dirty = True

        if dirty:
            await self._db.commit()

        return live

    def _reconcile_vanished(self, position: Position) -> None:
        """Mark a position closed because IG no longer reports it as open.

        The actual close happened outside the bot, so there is no fresh close
        level to record; the best estimate is the last live unrealized P&L
        computed by the most recent sync (stored in ``euro``). This estimate is
        later overwritten by ``reconcile_realized_pnl`` with IG's true figure.
        """
        now = datetime.now(UTC)
        close_level = float(position.level_close or position.level_open or 0)
        euro_pnl = (
            float(position.euro)
            if position.euro is not None
            else self._euro_pnl(position, close_level)
        )
        position.state = PositionState.CLOSE
        position.time_close = now.time()
        if position.level_close is None:
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
            is_close_hour=self._is_close_hours(),
        )

        if reason is None:
            # Follower strategy: trail the stop upward with an ATR-based distance
            if current_bid > level_open and self._config.close_strategy == "follower":
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

        position.level_follower = Decimal(str(round(new_stop, 3)))
        position.stop_update = (position.stop_update or 0) + 1
        await self._push_stop_to_ig(position, new_stop)
        await self._db.commit()
        logger.debug(
            "Trailing stop for %s -> %.3f (ATR=%.3f)",
            position.epic,
            new_stop,
            atr_value,
        )

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
            "stopLevel": round(stop_level, 3),
            "trailingStop": False,
        }
        try:
            await self._client.put(
                f"/positions/otc/{deal_id}",
                payload,
                version=2,
                priority=Priority.URGENT,
                label=f"trail {position.epic}: stop->{stop_level:.3f}",
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
        close_payload = {
            "dealId": deal_id,
            "direction": "SELL",
            "size": position.quantity or 1,
            "orderType": "MARKET",
            "timeInForce": "EXECUTE_AND_ELIMINATE",
            "forceOpen": False,
        }

        try:
            result = await self._client.delete(
                "/positions/otc",
                close_payload,
                version=1,
                priority=Priority.URGENT,
                label=f"close {position.epic}: {reason}",
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
        fill_level, ig_profit = await self._fetch_close_result(
            result.get("dealReference"), position.epic
        )
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
        closed = 0

        for position in positions:
            # Get current bid
            try:
                market = await self._client.get(
                    f"/markets/{position.epic}",
                    version=3,
                    priority=Priority.URGENT,
                    label=f"close {position.epic}: market",
                )
                bid = float(market.get("snapshot", {}).get("bid", 0))
                if await self._close_position(position, bid, "end_of_day"):
                    closed += 1
            except Exception as exc:
                logger.error("Failed to close position %s: %s", position.epic, exc)

        logger.info("Forced close: %d/%d positions closed", closed, len(positions))
        return closed
