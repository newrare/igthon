"""Pluggable trading strategies and a long/short backtest engine.

The project's live strategy (:mod:`src.services.compute` + the open/close rules
in :mod:`src.services.trading`) is a single hard-wired *long-only* trend
follower. To explore alternatives without touching the live path, this module
provides:

- a small :class:`Strategy` interface (entry signal + optional signal exit +
  optional ATR trailing), so a strategy is just data + two pure methods;
- a :class:`BacktestEngine` that replays any strategy over synthetic candles
  with **both long and short** support and honest spread costs (a long fills at
  the offer and exits at the bid; a short fills at the bid and exits at the
  offer), so every round-trip pays the spread exactly like the live broker;
- five concrete strategies spanning the main regime families (mean reversion,
  breakout, momentum), deliberately different from the live trend follower.

Everything is in-memory and synthetic — no IG API, no DB. Indicators reuse
:mod:`src.services.compute` where possible (``atr``, ``efficiency_ratio``);
RSI/EMA/z-score helpers are added here.

This module is the **research lab** only: candidates promoted to production are
re-implemented against the pluggable live interface in :mod:`src.strategies`
(e.g. ``DonchianBreakout`` here → ``src.strategies.donchian.DonchianER`` live)
and selected via the ``STRATEGY_NAME`` setting.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field

from src.services.compute import atr, efficiency_ratio
from src.services.price_buffer import Candle, EpicBuffer

LONG = "LONG"
SHORT = "SHORT"


# --------------------------------------------------------------------------- #
# Indicator helpers (only the ones not already in compute.py)                 #
# --------------------------------------------------------------------------- #


def ema(values: list[float], period: int) -> float:
    """Exponential moving average of the last values (returns the final EMA)."""
    if len(values) < period or period < 1:
        return 0.0
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    e = seed
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(values: list[float], period: int = 14) -> float:
    """Wilder's RSI over the last ``period`` deltas (0-100, 50 if flat/short)."""
    if len(values) <= period:
        return 50.0
    gains = 0.0
    losses = 0.0
    for prev, cur in zip(values[-period - 1 : -1], values[-period:]):
        delta = cur - prev
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100 - 100 / (1 + rs)


def zscore(values: list[float], period: int) -> tuple[float, float, float]:
    """Return ``(z, mean, std)`` of the last value against the prior window."""
    if len(values) < period:
        return 0.0, values[-1] if values else 0.0, 0.0
    window = values[-period:]
    mean = sum(window) / period
    std = statistics.pstdev(window)
    if std == 0:
        return 0.0, mean, 0.0
    return (values[-1] - mean) / std, mean, std


# --------------------------------------------------------------------------- #
# Strategy interface                                                          #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class EntrySignal:
    """An entry decision: direction plus protective stop and optional target."""

    direction: str  # LONG | SHORT
    stop: float  # absolute protective stop level (price units)
    target: float | None = None  # absolute take-profit level, or None


class Strategy:
    """Base class: an entry rule, an optional signal exit, optional ATR trail.

    Subclasses set ``name`` and ``warmup`` and implement :meth:`entry`. They may
    override :meth:`should_exit` for a signal-based close (e.g. "price reverted
    to the mean") and set ``trail_atr_k`` to enable a generic ATR trailing stop.
    """

    name: str = "base"
    warmup: int = 30  # candles required before the first entry
    trail_atr_k: float | None = None  # ATR multiple for trailing stop (None = off)
    atr_period: int = 14
    # Regime gate: when ``efficiency_period`` > 0 the engine skips every entry
    # whose Kaufman Efficiency Ratio over that window is below ``min_efficiency``
    # — i.e. only trade markets that are actually trending, stay flat in chop.
    efficiency_period: int = 0
    min_efficiency: float = 0.0

    def entry(self, buf: EpicBuffer) -> EntrySignal | None:  # noqa: ARG002
        """Return an entry signal for the latest candle, or None to stay flat."""
        raise NotImplementedError

    def should_exit(self, direction: str, buf: EpicBuffer) -> bool:  # noqa: ARG002
        """Signal-based exit (independent of stop/target). Default: never."""
        return False


# --------------------------------------------------------------------------- #
# Concrete strategies                                                         #
# --------------------------------------------------------------------------- #


@dataclass
class MeanReversionZScore(Strategy):
    """Fade extremes: buy cheap below the mean, sell rich above it.

    Long when the z-score of the mid drops to ``-entry_z`` (stretched down),
    short at ``+entry_z``. Exit when price reverts through the mean. Stop sits
    ``stop_z`` further out so a regime break is cut. Best in ranging markets.
    """

    name: str = "MeanRev-Zscore"
    period: int = 30
    entry_z: float = 2.0
    stop_z: float = 3.5
    warmup: int = 30

    def entry(self, buf: EpicBuffer) -> EntrySignal | None:
        z, mean, std = zscore(buf.mid_closes, self.period)
        if std == 0:
            return None
        last = buf.last
        if z <= -self.entry_z:
            return EntrySignal(
                LONG, stop=last.bid_close - self.stop_z * std, target=mean
            )
        if z >= self.entry_z:
            return EntrySignal(
                SHORT, stop=last.offer_close + self.stop_z * std, target=mean
            )
        return None

    def should_exit(self, direction: str, buf: EpicBuffer) -> bool:
        z, _, _ = zscore(buf.mid_closes, self.period)
        # Close once the stretch has fully unwound (mean touched / crossed).
        return z >= 0 if direction == LONG else z <= 0


@dataclass
class DonchianBreakout(Strategy):
    """Trade breakouts of the N-period price channel, trail with ATR.

    Long when the bid closes above the prior ``channel``-period high, short
    below the prior low. The ATR trailing stop rides the trend and is the only
    exit (no fixed target). Best in trending markets, bleeds when ranging.
    """

    name: str = "Donchian-Breakout"
    channel: int = 20
    stop_atr_k: float = 2.5
    warmup: int = 25
    atr_period: int = 14

    def __post_init__(self) -> None:
        self.trail_atr_k = 2.5

    def entry(self, buf: EpicBuffer) -> EntrySignal | None:
        candles = list(buf.candles)
        if len(candles) < self.channel + 1:
            return None
        prior = candles[-self.channel - 1 : -1]
        hi = max(c.bid_high for c in prior)
        lo = min(c.bid_low for c in prior)
        last = buf.last
        a = atr(candles, self.atr_period)
        if a <= 0:
            return None
        if last.bid_close > hi:
            return EntrySignal(LONG, stop=last.bid_close - self.stop_atr_k * a)
        if last.bid_close < lo:
            return EntrySignal(SHORT, stop=last.offer_close + self.stop_atr_k * a)
        return None


@dataclass
class RSIReversion(Strategy):
    """Classic oscillator mean reversion: buy oversold, sell overbought.

    Long when RSI < ``oversold``, short when RSI > ``overbought``; exit when RSI
    returns to the midline. ATR-sized stop guards against a runaway move.
    """

    name: str = "RSI-Reversion"
    period: int = 14
    oversold: float = 30.0
    overbought: float = 70.0
    stop_atr_k: float = 3.0
    warmup: int = 20
    atr_period: int = 14

    def entry(self, buf: EpicBuffer) -> EntrySignal | None:
        candles = list(buf.candles)
        r = rsi(buf.mid_closes, self.period)
        a = atr(candles, self.atr_period)
        if a <= 0:
            return None
        last = buf.last
        if r < self.oversold:
            return EntrySignal(LONG, stop=last.bid_close - self.stop_atr_k * a)
        if r > self.overbought:
            return EntrySignal(SHORT, stop=last.offer_close + self.stop_atr_k * a)
        return None

    def should_exit(self, direction: str, buf: EpicBuffer) -> bool:
        r = rsi(buf.mid_closes, self.period)
        return r >= 50 if direction == LONG else r <= 50


@dataclass
class MACDMomentum(Strategy):
    """EMA-crossover momentum: ride the side the fast EMA is on.

    Long while EMA(fast) > EMA(slow), short otherwise; the position is opened on
    the crossover and exits on the opposite cross (or its ATR trailing stop).
    """

    name: str = "MACD-Momentum"
    fast: int = 12
    slow: int = 26
    stop_atr_k: float = 2.5
    warmup: int = 30
    atr_period: int = 14

    def __post_init__(self) -> None:
        self.trail_atr_k = 2.0
        self.warmup = max(self.warmup, self.slow + 2)

    def _cross(self, buf: EpicBuffer) -> int:
        mids = buf.mid_closes
        if len(mids) < self.slow + 2:
            return 0
        fast_now = ema(mids, self.fast)
        slow_now = ema(mids, self.slow)
        fast_prev = ema(mids[:-1], self.fast)
        slow_prev = ema(mids[:-1], self.slow)
        if fast_prev <= slow_prev and fast_now > slow_now:
            return 1
        if fast_prev >= slow_prev and fast_now < slow_now:
            return -1
        return 0

    def entry(self, buf: EpicBuffer) -> EntrySignal | None:
        cross = self._cross(buf)
        if cross == 0:
            return None
        candles = list(buf.candles)
        a = atr(candles, self.atr_period)
        if a <= 0:
            return None
        last = buf.last
        if cross > 0:
            return EntrySignal(LONG, stop=last.bid_close - self.stop_atr_k * a)
        return EntrySignal(SHORT, stop=last.offer_close + self.stop_atr_k * a)

    def should_exit(self, direction: str, buf: EpicBuffer) -> bool:
        cross = self._cross(buf)
        return cross < 0 if direction == LONG else cross > 0


@dataclass
class DualThrustORB(Strategy):
    """Opening-range / volatility breakout (Dual Thrust style).

    Builds a reference range from the first ``opening`` candles of the day, then
    goes long if price breaks above ``open + k*range`` or short below
    ``open - k*range``. One shot per direction per day; ATR-sized stop.
    """

    name: str = "DualThrust-ORB"
    opening: int = 30
    k: float = 0.5
    stop_atr_k: float = 2.5
    warmup: int = 30
    atr_period: int = 14

    def __post_init__(self) -> None:
        self.warmup = max(self.warmup, self.opening)
        self.trail_atr_k = 2.0

    def entry(self, buf: EpicBuffer) -> EntrySignal | None:
        candles = list(buf.candles)
        opening = candles[: self.opening]
        if len(opening) < self.opening:
            return None
        day_open = opening[0].bid_open
        rng = max(c.bid_high for c in opening) - min(c.bid_low for c in opening)
        if rng <= 0:
            return None
        upper = day_open + self.k * rng
        lower = day_open - self.k * rng
        a = atr(candles, self.atr_period)
        if a <= 0:
            return None
        last = buf.last
        if last.bid_close > upper:
            return EntrySignal(LONG, stop=last.bid_close - self.stop_atr_k * a)
        if last.bid_close < lower:
            return EntrySignal(SHORT, stop=last.offer_close + self.stop_atr_k * a)
        return None


def all_strategies() -> list[Strategy]:
    """The five candidate strategies, freshly instantiated."""
    return [
        MeanReversionZScore(),
        DonchianBreakout(),
        RSIReversion(),
        MACDMomentum(),
        DualThrustORB(),
    ]


# --------------------------------------------------------------------------- #
# Backtest engine (long + short)                                              #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class BTTrade:
    """One closed long/short round-trip."""

    epic: str
    day: int
    direction: str
    entry_level: float
    exit_level: float
    reason: str
    euro: float

    @property
    def win(self) -> bool:
        return self.euro > 0


@dataclass(slots=True)
class _OpenPos:
    direction: str
    entry_level: float
    stop: float
    target: float | None
    epic: str
    day: int


@dataclass
class BacktestResult:
    """Aggregated outcome, comparable across strategies."""

    strategy: str
    trades: list[BTTrade] = field(default_factory=list)
    days: int = 0

    def summary(self) -> dict:
        pnls = [t.euro for t in self.trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        equity: list[float] = []
        total = peak = max_dd = 0.0
        for p in pnls:
            total += p
            peak = max(peak, total)
            max_dd = max(max_dd, peak - total)
            equity.append(round(total, 2))
        gross_win = sum(wins)
        gross_loss = -sum(losses)
        n = len(pnls)
        return {
            "strategy": self.strategy,
            "trades": n,
            "win_rate": round(len(wins) / n, 4) if n else 0.0,
            "total_pnl": round(total, 2),
            "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else 0.0,
            "expectancy": round(total / n, 2) if n else 0.0,
            "max_drawdown": round(max_dd, 2),
            "longs": sum(1 for t in self.trades if t.direction == LONG),
            "shorts": sum(1 for t in self.trades if t.direction == SHORT),
            "reasons": dict(Counter(t.reason for t in self.trades)),
            "days": self.days,
            "equity": equity,
        }


class BacktestEngine:
    """Replays one strategy over synthetic days, long and short, with costs."""

    def __init__(
        self,
        strategy: Strategy,
        *,
        euro_per_point: float = 1.0,
        quantity: int = 1,
        no_entry_tail: int = 30,
    ) -> None:
        self._s = strategy
        self._epp = euro_per_point * quantity
        self._no_entry_tail = no_entry_tail

    def run_day(
        self, day: int, curves: list[list[Candle]], result: BacktestResult
    ) -> None:
        buffers = [
            EpicBuffer(epic=f"SIM.{day}.{e}", max_candles=600)
            for e in range(len(curves))
        ]
        positions: dict[int, _OpenPos] = {}
        n = min((len(c) for c in curves), default=0)
        cutoff = n - self._no_entry_tail

        for tick in range(n):
            for e, curve in enumerate(curves):
                candle = curve[tick]
                buf = buffers[e]
                buf.add(candle)
                pos = positions.get(e)
                if pos is not None:
                    if self._manage(pos, candle, buf, result):
                        del positions[e]
                elif (
                    tick >= self._s.warmup
                    and tick < cutoff
                    and len(buf) > self._s.warmup
                ):
                    if self._regime_blocks(buf):
                        continue
                    sig = self._s.entry(buf)
                    if sig is not None:
                        positions[e] = self._open(sig, candle, buf.epic, day)

        # Force-close everything still open at end of day.
        for e, pos in positions.items():
            candle = curves[e][n - 1]
            level = candle.bid_close if pos.direction == LONG else candle.offer_close
            self._record(pos, level, "end_of_day", result)

    def _regime_blocks(self, buf: EpicBuffer) -> bool:
        """True when the regime gate is on and the market is not trending enough."""
        if self._s.efficiency_period <= 0:
            return False
        er = efficiency_ratio(buf.mid_closes, self._s.efficiency_period)
        return er < self._s.min_efficiency

    def _open(self, sig: EntrySignal, candle: Candle, epic: str, day: int) -> _OpenPos:
        # Long fills at the offer (ask); short fills at the bid — spread paid on entry.
        entry = candle.offer_close if sig.direction == LONG else candle.bid_close
        return _OpenPos(sig.direction, entry, sig.stop, sig.target, epic, day)

    def _manage(
        self, pos: _OpenPos, candle: Candle, buf: EpicBuffer, result: BacktestResult
    ) -> bool:
        if pos.direction == LONG:
            if candle.bid_low <= pos.stop:  # stop checked first (conservative)
                self._record(pos, pos.stop, "stop", result)
                return True
            if pos.target is not None and candle.bid_high >= pos.target:
                self._record(pos, pos.target, "target", result)
                return True
        else:
            if candle.offer_high >= pos.stop:
                self._record(pos, pos.stop, "stop", result)
                return True
            if pos.target is not None and candle.offer_low <= pos.target:
                self._record(pos, pos.target, "target", result)
                return True

        if self._s.should_exit(pos.direction, buf):
            level = candle.bid_close if pos.direction == LONG else candle.offer_close
            self._record(pos, level, "signal", result)
            return True

        self._trail(pos, candle, buf)
        return False

    def _trail(self, pos: _OpenPos, candle: Candle, buf: EpicBuffer) -> None:
        k = self._s.trail_atr_k
        if k is None:
            return
        a = atr(list(buf.candles), self._s.atr_period)
        if a <= 0:
            return
        if pos.direction == LONG:
            new_stop = candle.bid_close - k * a
            if new_stop > pos.stop:
                pos.stop = new_stop
        else:
            new_stop = candle.offer_close + k * a
            if new_stop < pos.stop:
                pos.stop = new_stop

    def _record(
        self, pos: _OpenPos, exit_level: float, reason: str, result: BacktestResult
    ) -> None:
        if pos.direction == LONG:
            move = exit_level - pos.entry_level  # bought at offer, sold at bid
        else:
            move = pos.entry_level - exit_level  # sold at bid, bought back at offer
        result.trades.append(
            BTTrade(
                epic=pos.epic,
                day=pos.day,
                direction=pos.direction,
                entry_level=round(pos.entry_level, 5),
                exit_level=round(exit_level, 5),
                reason=reason,
                euro=round(move * self._epp, 2),
            )
        )
