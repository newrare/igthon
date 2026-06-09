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
from src.services.compute import TradingSignal
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
        if not self._is_trading_hours():
            return False, "Outside trading hours"

        if signal.direction != "BUY":
            return False, f"Signal direction is {signal.direction}"

        if await self._is_epic_open(signal.epic):
            return False, f"Epic {signal.epic} already open"

        open_count = await self._count_open_positions()
        if open_count >= self._config.max_positions:
            return False, f"Max positions reached ({open_count})"

        daily_pnl = await self._get_daily_pnl()
        if daily_pnl <= self._config.day_euro_finish_loose:
            return False, f"Daily loss limit reached ({daily_pnl:.2f}€)"
        if daily_pnl >= self._config.day_euro_finish_win:
            return False, f"Daily target reached ({daily_pnl:.2f}€)"

        # Trade count and win rate circuit breaker
        trade_count, win_rate = await self._get_daily_stats()
        if trade_count >= self._config.max_trades_day:
            return False, f"Max daily trades reached ({trade_count})"
        if trade_count >= 10 and win_rate < self._config.min_win_rate:
            return (
                False,
                f"Win rate too low ({win_rate:.0%} after {trade_count} trades)",
            )

        return True, "OK"

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

        # 2. Validate stop distance against dealing rules
        min_stop_rule = dealing_rules.get("minNormalStopOrLimitDistance", {})
        max_stop_rule = dealing_rules.get("maxStopOrLimitDistance", {})
        min_deal_size = dealing_rules.get("minDealSize", {}).get("value", 1)

        min_stop = float(min_stop_rule.get("value", 1))
        max_stop = float(max_stop_rule.get("value", 9999))

        # Convert percentage stops to points
        if min_stop_rule.get("unit") == "PERCENTAGE":
            min_stop = min_stop * levels.bid / 100
        if max_stop_rule.get("unit") == "PERCENTAGE":
            max_stop = max_stop * levels.bid / 100

        stop_distance = levels.stop_distance

        if stop_distance < min_stop:
            logger.info(
                "Stop too small for %s: %s < min %s",
                epic,
                stop_distance,
                min_stop,
            )
            return None

        if stop_distance > max_stop:
            logger.info(
                "Stop too large for %s: %s > max %s",
                epic,
                stop_distance,
                max_stop,
            )
            return None

        # 3. Quantity
        quantity = max(int(min_deal_size), 1)

        # Check euro risk
        scaling_factor = float(str(snapshot.get("scalingFactor", "1")).replace(",", ""))
        euro_per_pip = 1.0 / scaling_factor if scaling_factor > 0 else 1.0
        euro_risk = quantity * stop_distance * euro_per_pip

        if euro_risk > self._config.euro_loss_max:
            logger.info(
                "Euro risk too high for %s: %.2f > %.2f",
                epic,
                euro_risk,
                self._config.euro_loss_max,
            )
            return None

        # 4. Send order
        currency = instrument.get("currencies", [{}])[0].get("code", "EUR")
        expiry = instrument.get("expiry", "-")

        order_payload = {
            "epic": epic,
            "expiry": expiry,
            "direction": "BUY",
            "size": str(quantity),
            "orderType": "MARKET",
            "currencyCode": currency,
            "guaranteedStop": False,
            "stopDistance": str(int(stop_distance)),
            "forceOpen": True,
        }

        logger.info(
            "Opening position: epic=%s, qty=%d, stop=%d, risk=%.2f€",
            epic,
            quantity,
            stop_distance,
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

        # Currency-converted euro value of one point of movement for this
        # position — the basis for every P&L figure (live and realized).
        epp = euro_per_point(market_data, quantity, currency)

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
            level_stop=Decimal(str(round(open_level - stop_distance, 5))),
            pip_spread=Decimal(str(round(levels.spread, 5))),
            quantity=quantity,
            size=int(stop_distance),
            euro_stop=Decimal(str(round(euro_risk, 3))),
            euro_per_point=Decimal(str(round(epp, 6))) if epp else None,
        )

        self._db.add(position)
        await self._db.commit()
        await self._db.refresh(position)

        logger.info(
            "Position opened: epic=%s, deal=%s, level=%.2f, stop=%.2f",
            epic,
            deal_id,
            open_level,
            open_level - stop_distance,
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
            "Position %s no longer open at IG — reconciled as closed_externally (P&L=%.2f€)",
            position.epic,
            euro_pnl,
        )

    async def check_and_close(self, position: Position, current_bid: float) -> bool:
        """Check if a position should be closed based on current price.

        Implements closing strategies from apiCheckPosition.php:
        - Win: close when bid reaches level_win
        - Follower: trail the stop, close when bid drops below level_follower
        - Loose: close when bid drops below level_loose

        Args:
            position: Open position to evaluate.
            current_bid: Current market bid price.

        Returns:
            True if position was closed, False otherwise.
        """
        level_win = float(position.level_win or 0)
        level_follower = float(position.level_follower or 0)
        level_loose = float(position.level_loose or 0)
        level_open = float(position.level_open or 0)

        reason = None

        # Forced close at end of day
        if self._is_close_hours():
            reason = "end_of_day"

        # Win level reached
        elif current_bid >= level_win:
            reason = "win"

        # Below loose level
        elif current_bid <= level_loose:
            reason = "loose"

        # Follower strategy: update trailing stop
        elif current_bid > level_open and self._config.close_strategy == "follower":
            # Move follower level up as price rises
            new_follower = current_bid - float(position.pip_spread or 0) * 3
            if new_follower > level_follower:
                position.level_follower = Decimal(str(round(new_follower, 3)))
                position.stop_update = (position.stop_update or 0) + 1
                await self._db.commit()
                logger.debug(
                    "Trailing stop updated for %s: %.3f", position.epic, new_follower
                )
            return False

        if reason is None:
            return False

        # Close the position
        return await self._close_position(position, current_bid, reason)

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
                "Position %s not found in IG live positions — marking as closed (phantom)",
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
            "Position %s not found in IG live positions after 404 — marking as closed (phantom)",
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
