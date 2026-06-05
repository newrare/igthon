"""Trading service — open/close positions. Ported from Action.php.

Implements the full trading workflow:
- Pre-open checks (market status, duplicates, stop limits, risk)
- Position opening via the IG API
- Position monitoring and closing (win/follower/loose strategies)
- Stop level updates (trailing stop)
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.client import IGClient
from src.models.position import Position, PositionState, PositionStrategy
from src.services.api_queue import APIQueue, Priority
from src.services.compute import TradingSignal

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

        # 6. Record in DB
        now = datetime.now(UTC)
        position = Position(
            epic=epic,
            epic_name=instrument.get("name", epic)[:10],
            deal_reference=deal_reference,
            date=now.date(),
            time_open=now.time(),
            state=PositionState.OPEN,
            strategy=PositionStrategy.TARGET,
            level_open=Decimal(str(round(open_level, 3))),
            level_win=Decimal(str(round(levels.level_win, 3))),
            level_zero=Decimal(str(round(levels.level_zero, 3))),
            level_follower=Decimal(str(round(levels.level_follower, 3))),
            level_loose=Decimal(str(round(levels.level_loose, 3))),
            level_security=Decimal(str(round(levels.level_security, 3))),
            level_stop=Decimal(str(round(open_level - stop_distance, 3))),
            pip_spread=Decimal(str(round(levels.spread, 3))),
            quantity=quantity,
            size=int(stop_distance),
            euro_stop=Decimal(str(round(euro_risk, 3))),
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

        # IG requires specific close payload
        close_payload = {
            "dealId": position.deal_reference,
            "direction": "SELL",
            "size": str(position.quantity or 1),
            "orderType": "MARKET",
        }

        try:
            # IG close uses DELETE method with _method header workaround
            result = await self._client.post(
                "/positions/otc",
                close_payload,
                version=1,
                priority=Priority.URGENT,
                label=f"close {position.epic}: {reason}",
            )
        except Exception as exc:
            logger.error("Failed to close %s: %s", position.epic, exc)
            return False

        # Update position in DB
        now = datetime.now(UTC)
        open_level = float(position.level_open or 0)
        pip_pnl = close_level - open_level
        euro_per_pip = (
            float(position.euro_stop or 1)
            / float(position.size or 1)
            / float(position.quantity or 1)
        )
        euro_pnl = pip_pnl * (position.quantity or 1) * euro_per_pip

        position.state = PositionState.CLOSE
        position.time_close = now.time()
        position.level_close = Decimal(str(round(close_level, 3)))
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
