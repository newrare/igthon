"""Curve reading — one read-only object holding everything measurable on an epic.

Shared, purely-functional infrastructure: **no trading decision is taken here**.
A :class:`CurveReading` wraps an :class:`~src.feed.price_buffer.EpicBuffer` and
exposes, as plain attributes and small methods, the characteristics of the curve a
strategy would otherwise recompute by hand every time — candle balance, trend per
horizon, noise, channel position, readable supports and resistances, round-number
levels, indicator crossings, market regime. A :class:`PositionReading` does the
same for an **open position**, measuring everything from the moment it opened.

Why this exists
---------------

Every entry, stop and zone updater in the project used to start with the same ten
lines: slice the bid closes, fit a regression, divide by an ATR, look for a swing
low. The measures were re-derived slightly differently each time (bid here, mid
there; 30 candles here, 20 there), which makes two strategies impossible to
compare and a bug impossible to fix once. Concentrating the *reading* in one place
leaves the strategies with only their *decision*:

    reading = CurveReading.of(buf)
    if reading.regime is Regime.TREND_UP and reading.trend_15m.is_clean:
        support = reading.nearest_support
        ...

Design rules
------------

- **Read-only and side-effect free.** Nothing here mutates the buffer or the
  position; everything is derived from the candles.
- **A reading is a snapshot of one tick.** The candle list is copied at
  construction and every measure is cached, so building one is cheap and reading
  fifty fields costs nothing extra. It also means a reading must be **rebuilt each
  pass** and never stored across ticks — :attr:`~CurveReading.is_stale` says
  whether the buffer has moved on since.
- **Scale-free by default.** Absolute point distances are meaningless across a
  universe of 50 epics, so trends are returned in percent, steepness as a chart
  angle (:func:`~src.core.indicators.slope_angle`), and any distance can be
  converted into volatility units with :meth:`~CurveReading.in_atr`.
- **One candle = one minute.** The application holds no sub-minute data (see
  ``docs/DATAFLOW.md`` §1), so a "tick" below is one 1-minute candle and every
  ``minutes`` argument is a candle count, silently clamped to the history
  available.
- **Which price side.** Shape measures run on the **mid** by default, so that a
  spread that widens does not read as a move; a caller that needs what its own
  side actually trades at (an exit, always in close-out terms) builds the reading
  with ``side="bid"`` or ``side="offer"`` — which is what :class:`PositionReading`
  does automatically from the position's direction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import cached_property

from src.core.indicators import (
    adverse_tick_noise,
    atr_from_series,
    band_noise,
    efficiency_ratio,
    ema,
    linear_regression,
    rate_of_change,
    retracement_depth,
    rsi,
    slope_angle,
    sma,
)
from src.core.levels import (
    Pivot,
    PriceLevel,
    cluster_levels,
    psychological_levels,
    round_step,
    support_resistance,
    swing_pivots,
)
from src.feed.price_buffer import Candle, EpicBuffer, PriceBuffer

#: Horizons (in minutes) used by the shortcut trend properties and by
#: :attr:`CurveReading.alignment`.
STANDARD_HORIZONS = (5, 15, 30, 60)

#: Below this many candles nothing can honestly be said about a regime.
MIN_REGIME_MINUTES = 10


class Trend(StrEnum):
    """Sign of a fitted trend."""

    UP = "UP"
    DOWN = "DOWN"
    FLAT = "FLAT"


class Regime(StrEnum):
    """Coarse market state — the one field most strategies gate on.

    - ``TREND_UP`` / ``TREND_DOWN``: the path is directional (Kaufman efficiency
      ratio past the threshold) and the fit has a sign.
    - ``RANGE``: choppy *and* contained — the window's total travel is no more than
      a random walk of its own per-candle size would produce. A mean-reverting
      market.
    - ``VOLATILE``: choppy but travelling far — big swings that go nowhere. The
      dangerous one: stops get hit in both directions.
    - ``UNKNOWN``: not enough history to judge.
    """

    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    VOLATILE = "VOLATILE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class TickCounts:
    """Balance of rising / falling / undecided candles over a window.

    A candle counts as *flat* when its close-to-close move is smaller than the
    quote's own thickness (half the spread by default): the market did not
    resolve, and counting it as a direction would inflate whichever side the
    rounding fell on.
    """

    minutes: int
    up: int
    down: int
    flat: int

    @property
    def total(self) -> int:
        """Number of close-to-close steps measured."""
        return self.up + self.down + self.flat

    @property
    def up_ratio(self) -> float:
        """Share of rising candles in [0, 1]."""
        return self.up / self.total if self.total else 0.0

    @property
    def down_ratio(self) -> float:
        """Share of falling candles in [0, 1]."""
        return self.down / self.total if self.total else 0.0

    @property
    def flat_ratio(self) -> float:
        """Share of undecided candles in [0, 1] — the market's hesitation."""
        return self.flat / self.total if self.total else 0.0

    @property
    def bias(self) -> float:
        """``(up − down) / total`` in [-1, 1] — pressure, ignoring amplitude.

        Deliberately blind to how *big* the moves were: a curve of many small ups
        and few large downs has a positive bias and a negative trend, and that
        divergence is the signal (distribution into a rising tape).
        """
        return (self.up - self.down) / self.total if self.total else 0.0

    @property
    def dominant(self) -> Trend:
        """Which side won the count, or ``FLAT`` on a tie."""
        if self.up > self.down:
            return Trend.UP
        if self.down > self.up:
            return Trend.DOWN
        return Trend.FLAT


@dataclass(frozen=True, slots=True)
class TrendReading:
    """Fitted trend over one horizon.

    Attributes:
        minutes: Candles the fit spans (already clamped to available history).
        pct: Implied move across the whole window, in percent of the last price —
            the cross-epic comparable figure (same maths as
            :func:`~src.core.indicators.trend_pct`).
        net_pct: Actual first-to-last move over the window, in percent. It differs
            from :attr:`pct` exactly when the path is not linear, and the gap
            between the two is itself informative.
        slope: Raw least-squares slope, in price units per minute.
        degrees: Steepness as the angle read on the chart (see
            :func:`~src.core.indicators.slope_angle`): 45° = the fit climbs the
            window's full price range across the window.
        r_squared: Linearity of the fit, 0-1.
    """

    minutes: int
    pct: float
    net_pct: float
    slope: float
    degrees: float
    r_squared: float

    @property
    def direction(self) -> Trend:
        """Sign of the trend, or ``FLAT`` when the fit says essentially nothing."""
        if self.r_squared < 0.05 or abs(self.pct) < 0.01:
            return Trend.FLAT
        return Trend.UP if self.pct > 0 else Trend.DOWN

    @property
    def per_minute_pct(self) -> float:
        """Implied move per minute, in percent — comparable across horizons."""
        return self.pct / self.minutes if self.minutes else 0.0

    @property
    def is_clean(self) -> bool:
        """True when the move is a line rather than a wander (R² ≥ 0.5)."""
        return self.r_squared >= 0.5


@dataclass(frozen=True, slots=True)
class AmplitudeReading:
    """How much the curve moves over a window, measured four different ways.

    They are not interchangeable — each is the right floor for a different rule:

    Attributes:
        minutes: Candles measured.
        range: Highest high − lowest low: the window's total vertical extent.
        range_pct: Same, in percent of the last price.
        atr: Mean per-candle true range — the size of one candle.
        band_noise: Standard deviation around the trend line — the *thickness* of
            the band price wanders in, detrended, so a clean ramp scores ~0.
        adverse_noise: Typical single adverse close-to-close step *plus* a
            volatility band — what one bad candle can be expected to give back.
        retracement: Deepest cumulative give-back from a running peak — what a
            *sequence* of bad candles actually gave back over the window. Neither
            of the two dominates the other: a window of a few large down-steps
            scores a high ``adverse_noise`` (the band is mean + 2σ) and a modest
            ``retracement``, while a long slow slide does the opposite. A stop
            wants both, which is why both are here.
        efficiency: Kaufman efficiency ratio, 0-1: how much of the path travelled
            ended up as net movement.
    """

    minutes: int
    range: float
    range_pct: float
    atr: float
    band_noise: float
    adverse_noise: float
    retracement: float
    efficiency: float

    @property
    def is_choppy(self) -> bool:
        """True when the path wanders (efficiency ratio below 0.3)."""
        return self.efficiency < 0.3


@dataclass(frozen=True, slots=True)
class ChannelReading:
    """Where price sits inside the high/low channel of a window.

    Attributes:
        minutes: Candles the channel spans.
        high: Highest high of the window.
        low: Lowest low of the window.
        price: Last price, the one located inside the channel.
        position: 0 at the channel low, 1 at its high, 0.5 for a flat channel.
    """

    minutes: int
    high: float
    low: float
    price: float
    position: float

    @property
    def width(self) -> float:
        """Channel height in price units."""
        return self.high - self.low

    @property
    def width_pct(self) -> float:
        """Channel height in percent of the last price."""
        return self.width / self.price * 100 if self.price else 0.0

    @property
    def from_high(self) -> float:
        """Distance below the channel high — the pull-back already given back."""
        return self.high - self.price

    @property
    def from_low(self) -> float:
        """Distance above the channel low — the rally already made."""
        return self.price - self.low


@dataclass(frozen=True, slots=True)
class CrossReading:
    """State of a moving-average pair — the classic "turn is coming" reading.

    A crossing on its own is a weak signal; what makes it usable is *when* it
    happened and how far apart the averages are now, both of which are here.

    Attributes:
        fast_period: Period of the fast average.
        slow_period: Period of the slow average.
        fast: Current fast value (0.0 when there is not enough history).
        slow: Current slow value.
        gap: ``fast − slow`` in price units; its sign is the state.
        bars_since_cross: Candles since the sign of ``gap`` last changed. Equal to
            the measured history when no crossing is visible in it, i.e. "at least
            this long" — see :attr:`is_established`.
        crossed_in_history: False when no sign change was visible, which is what
            makes ``bars_since_cross`` a lower bound.
    """

    fast_period: int
    slow_period: int
    fast: float
    slow: float
    gap: float
    bars_since_cross: int
    crossed_in_history: bool

    @property
    def state(self) -> Trend:
        """``UP`` while the fast average leads, ``DOWN`` below it."""
        if self.gap > 0:
            return Trend.UP
        if self.gap < 0:
            return Trend.DOWN
        return Trend.FLAT

    @property
    def just_crossed(self) -> bool:
        """True on the candle of the crossing and the one after it."""
        return self.crossed_in_history and self.bars_since_cross <= 1

    @property
    def is_established(self) -> bool:
        """True when the current side has held for more than 5 candles."""
        return self.bars_since_cross > 5


class CurveReading:
    """Read-only view of one epic's curve — the characteristics, not the decision.

    Build one per epic per pass and read whatever is needed; every measure is
    computed at most once and then cached. Windows are expressed in minutes and
    clamped to the history the buffer holds, so a call never raises on a
    half-warmed epic — it answers on what exists (check :attr:`minutes` or
    :meth:`is_warm` when that matters).

    Example::

        r = CurveReading.of(buf)
        r.trend_15m.degrees          # steepness as seen on the chart
        r.ticks(15).bias             # candle pressure, amplitude-blind
        r.amplitude(5).band_noise    # thickness of the band price wanders in
        r.regime                     # TREND_UP / RANGE / VOLATILE / …
        r.nearest_support            # closest level the market already respected
        r.cross(9, 21).just_crossed  # EMA 9/21 turn on this candle

    Args:
        buf: The epic's rolling buffer. Its candles are snapshotted at
            construction, so later feed updates do not mutate this reading.
        side: Price side every shape measure reads — ``"mid"`` (default),
            ``"bid"`` or ``"offer"``.
        atr_period: Period of the headline :attr:`atr`.
        regime_window: Horizon :attr:`regime` and the level detection judge on.
        pivot_strength: Neighbours required on each side of a swing pivot
            (:func:`~src.core.levels.swing_pivots`).
        level_tolerance_atr: Cluster tolerance for swing levels, in ATR units.
        round_radius_atr: How far around the price to look for the *in-reach*
            round-number levels, in ATR units (:attr:`nearest_round_level` ignores
            it and always answers).
        trend_efficiency: Efficiency-ratio threshold above which :attr:`regime`
            calls the market trending.
        volatile_ratio: :attr:`volatility_ratio` above which a non-directional
            market is called ``VOLATILE`` rather than ``RANGE``.
    """

    def __init__(
        self,
        buf: EpicBuffer,
        *,
        side: str = "mid",
        atr_period: int = 14,
        regime_window: int = 30,
        pivot_strength: int = 2,
        level_tolerance_atr: float = 0.5,
        round_radius_atr: float = 6.0,
        trend_efficiency: float = 0.35,
        volatile_ratio: float = 1.5,
    ) -> None:
        if side not in ("mid", "bid", "offer"):
            raise ValueError(f"side must be mid/bid/offer, got {side!r}")
        self._buf = buf
        self._candles: list[Candle] = list(buf.candles)
        self.epic = buf.epic
        self.side = side
        self.atr_period = atr_period
        self.regime_window = regime_window
        self.pivot_strength = pivot_strength
        self.level_tolerance_atr = level_tolerance_atr
        self.round_radius_atr = round_radius_atr
        self.trend_efficiency = trend_efficiency
        self.volatile_ratio = volatile_ratio
        self._memo: dict[tuple, object] = {}

    @classmethod
    def of(cls, buf: EpicBuffer, **options) -> CurveReading:
        """Alias for the constructor, for a fluent call site."""
        return cls(buf, **options)

    # ---- identity and freshness ------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"CurveReading({self.epic}, {self.minutes}min, "
            f"side={self.side}, regime={self.regime})"
        )

    @property
    def candles(self) -> list[Candle]:
        """The snapshotted candles, oldest first."""
        return self._candles

    @property
    def minutes(self) -> int:
        """Candles held — the maximum honest lookback for this reading."""
        return len(self._candles)

    @property
    def last_timestamp(self) -> datetime | None:
        """Timestamp of the newest candle, or ``None`` on an empty buffer."""
        return self._candles[-1].timestamp if self._candles else None

    @property
    def age_minutes(self) -> float:
        """Minutes since the newest candle — staleness of the feed for this epic."""
        stamp = self.last_timestamp
        if stamp is None:
            return math.inf
        return (datetime.now(UTC) - stamp).total_seconds() / 60

    @property
    def is_stale(self) -> bool:
        """True once the live buffer has moved past this snapshot.

        A reading is a one-tick object; this is the cheap guard against a caller
        that cached one and kept reading it on later passes.
        """
        return len(self._buf) != len(self._candles)

    def is_warm(self, minutes: int) -> bool:
        """True when the buffer holds at least ``minutes`` candles."""
        return self.minutes >= minutes

    # ---- price series ----------------------------------------------------------

    def _close(self, candle: Candle) -> float:
        if self.side == "bid":
            return candle.bid_close
        if self.side == "offer":
            return candle.offer_close
        return candle.mid_close

    def _high(self, candle: Candle) -> float:
        if self.side == "bid":
            return candle.bid_high
        if self.side == "offer":
            return candle.offer_high
        return (candle.bid_high + candle.offer_high) / 2

    def _low(self, candle: Candle) -> float:
        if self.side == "bid":
            return candle.bid_low
        if self.side == "offer":
            return candle.offer_low
        return (candle.bid_low + candle.offer_low) / 2

    @cached_property
    def closes(self) -> list[float]:
        """Closes of the chosen side, oldest first."""
        return [self._close(c) for c in self._candles]

    @cached_property
    def highs(self) -> list[float]:
        """Highs of the chosen side, oldest first."""
        return [self._high(c) for c in self._candles]

    @cached_property
    def lows(self) -> list[float]:
        """Lows of the chosen side, oldest first."""
        return [self._low(c) for c in self._candles]

    @property
    def price(self) -> float:
        """Last close of the chosen side — the reference price of this reading."""
        return self.closes[-1] if self._candles else 0.0

    @property
    def bid(self) -> float:
        """Last bid close (0.0 on an empty buffer)."""
        return self._candles[-1].bid_close if self._candles else 0.0

    @property
    def offer(self) -> float:
        """Last offer close (0.0 on an empty buffer)."""
        return self._candles[-1].offer_close if self._candles else 0.0

    @property
    def spread(self) -> float:
        """Current spread (offer − bid) in price units."""
        return self._candles[-1].spread if self._candles else 0.0

    def spread_avg(self, minutes: int = 30) -> float:
        """Mean spread over the window — the baseline the live spread is judged on."""
        window = self._window(minutes)
        if not window:
            return 0.0
        spreads = [c.spread for c in self._candles[-window:]]
        return sum(spreads) / len(spreads)

    @property
    def spread_ratio(self) -> float:
        """Live spread divided by its 30-minute mean.

        Above ~1.5 the market is quoting defensively (news, thin book) — a state
        worth refusing to open into, and one no price measure reveals.
        """
        baseline = self.spread_avg(30)
        return self.spread / baseline if baseline > 0 else 0.0

    @property
    def spread_in_atr(self) -> float:
        """Spread as a fraction of one candle's range — the real cost of the trade.

        The only honest way to compare a "tight" spread across epics: 0.1 means a
        tenth of a candle's movement is lost to the round trip, 1.0 means a whole
        candle must go the right way to break even.
        """
        return self.in_atr(self.spread)

    # ---- unit conversion -------------------------------------------------------

    def in_atr(self, distance: float) -> float:
        """Convert a price distance into ATR units (0.0 when ATR is unknown)."""
        return distance / self.atr if self.atr > 0 else 0.0

    def in_pct(self, distance: float) -> float:
        """Convert a price distance into percent of the current price."""
        return distance / self.price * 100 if self.price else 0.0

    def in_euro(self, distance: float, euro_per_point: float) -> float:
        """Convert a price distance into euros for a given €/point."""
        return distance * euro_per_point

    # ---- windows and caching ---------------------------------------------------

    def _window(self, minutes: int) -> int:
        """Clamp a requested window to the available history."""
        return max(0, min(minutes, self.minutes))

    def _cached(self, key: tuple, build):
        """Memoise ``build()`` under ``key`` (parametrised measures)."""
        if key not in self._memo:
            self._memo[key] = build()
        return self._memo[key]

    # ---- candle balance --------------------------------------------------------

    def ticks(
        self, minutes: int = 15, flat_threshold: float | None = None
    ) -> TickCounts:
        """Count rising / falling / undecided candles over the last ``minutes``.

        Args:
            minutes: Window in candles.
            flat_threshold: Move below which a candle counts as flat. Defaults to
                half the window's mean spread — the quote's own thickness, below
                which a "direction" is not information. Pass ``0`` to classify on
                the strict sign.
        """
        return self._cached(
            ("ticks", minutes, flat_threshold),
            lambda: self._ticks(minutes, flat_threshold),
        )

    def _ticks(self, minutes: int, flat_threshold: float | None) -> TickCounts:
        window = self._window(minutes)
        closes = self.closes[-(window + 1) :] if window else []
        if len(closes) < 2:
            return TickCounts(minutes=window, up=0, down=0, flat=0)

        threshold = (
            self.spread_avg(minutes) / 2 if flat_threshold is None else flat_threshold
        )
        up = down = flat = 0
        for previous, current in zip(closes, closes[1:]):
            move = current - previous
            if abs(move) <= threshold:
                flat += 1
            elif move > 0:
                up += 1
            else:
                down += 1
        return TickCounts(minutes=len(closes) - 1, up=up, down=down, flat=flat)

    @property
    def ticks_15m(self) -> TickCounts:
        """Candle balance over the last 15 minutes."""
        return self.ticks(15)

    @cached_property
    def streak(self) -> int:
        """Signed length of the current run of same-direction candles.

        ``+4`` = four rising candles in a row, ``−2`` = two falling. A long run is
        both a confirmation (the move is one-sided) and a warning (runs end), which
        is why it is reported raw rather than interpreted.
        """
        closes = self.closes
        if len(closes) < 2:
            return 0
        moves = [b - a for a, b in zip(closes, closes[1:])]
        last = moves[-1]
        if last == 0:
            return 0
        sign = 1 if last > 0 else -1
        run = 0
        for move in reversed(moves):
            if (move > 0 and sign < 0) or (move < 0 and sign > 0) or move == 0:
                break
            run += 1
        return run * sign

    # ---- trend -----------------------------------------------------------------

    def trend(self, minutes: int) -> TrendReading:
        """Fit the trend over the last ``minutes`` candles."""
        return self._cached(("trend", minutes), lambda: self._trend(minutes))

    def _trend(self, minutes: int) -> TrendReading:
        window = self._window(minutes)
        values = self.closes[-window:] if window else []
        if len(values) < 2:
            return TrendReading(
                minutes=len(values),
                pct=0.0,
                net_pct=0.0,
                slope=0.0,
                degrees=0.0,
                r_squared=0.0,
            )

        regression = linear_regression(values)
        price = values[-1]
        span = max(self.highs[-window:]) - min(self.lows[-window:])
        pct = regression.slope * len(values) / price * 100 if price > 0 else 0.0
        net = (values[-1] - values[0]) / values[0] * 100 if values[0] > 0 else 0.0
        return TrendReading(
            minutes=len(values),
            pct=pct,
            net_pct=net,
            slope=regression.slope,
            degrees=slope_angle(regression.slope, len(values), span),
            r_squared=regression.r_squared,
        )

    @property
    def trend_5m(self) -> TrendReading:
        """Trend over the last 5 minutes."""
        return self.trend(5)

    @property
    def trend_15m(self) -> TrendReading:
        """Trend over the last 15 minutes."""
        return self.trend(15)

    @property
    def trend_30m(self) -> TrendReading:
        """Trend over the last 30 minutes."""
        return self.trend(30)

    @property
    def trend_60m(self) -> TrendReading:
        """Trend over the last hour."""
        return self.trend(60)

    @cached_property
    def trends(self) -> dict[int, TrendReading]:
        """The four standard horizons at once, keyed by minutes."""
        return {minutes: self.trend(minutes) for minutes in STANDARD_HORIZONS}

    @cached_property
    def alignment(self) -> float:
        """Agreement of the standard horizons, in [-1, 1].

        The mean of their directions (+1 up, −1 down, 0 flat). ``+1`` means 5, 15,
        30 and 60 minutes all point up — the state a trend-follower wants — while a
        value near 0 means the horizons contradict each other, which is exactly when
        a breakout signal is worth the least. Horizons the buffer cannot cover are
        skipped, so a young epic is judged on what it has.
        """
        signs = []
        for minutes, reading in self.trends.items():
            if reading.minutes < min(minutes, 3):
                continue
            signs.append(
                1.0
                if reading.direction is Trend.UP
                else -1.0 if reading.direction is Trend.DOWN else 0.0
            )
        return sum(signs) / len(signs) if signs else 0.0

    @property
    def is_aligned(self) -> bool:
        """True when every measured horizon agrees on the direction."""
        return abs(self.alignment) == 1.0

    @cached_property
    def acceleration(self) -> float:
        """Short-horizon speed minus long-horizon speed, in percent per minute.

        Positive with an up trend (or negative with a down trend) means the move is
        *speeding up*; the opposite sign means it is fading while still pointing the
        same way — the earliest readable warning that a trend is ending, and the one
        a single-horizon slope cannot express.
        """
        return self.trend(5).per_minute_pct - self.trend(30).per_minute_pct

    @property
    def is_fading(self) -> bool:
        """True when the 30-minute trend has a sign and the 5-minute speed opposes it.

        The readable form of a fading trend (see :attr:`acceleration`).
        """
        long_trend = self.trend(30)
        if long_trend.direction is Trend.FLAT:
            return False
        sign = 1.0 if long_trend.direction is Trend.UP else -1.0
        return sign * self.acceleration < 0

    # ---- amplitude and noise ---------------------------------------------------

    @cached_property
    def atr(self) -> float:
        """Headline ATR on the chosen side, in price units.

        The period is ``atr_period``, **clamped to the history available**: on a
        buffer that has only just started filling, a shorter ATR is noisy but a
        zero one is actively harmful — it silently turns every ``in_atr`` reading
        into 0.0, i.e. "this move is nothing", on exactly the epics whose history
        is thin.
        """
        return atr_from_series(
            self.highs, self.lows, self.closes, min(self.atr_period, self.minutes - 1)
        )

    @cached_property
    def baseline_atr(self) -> float:
        """ATR over the **whole** snapshot — this epic's own normal candle size.

        The reference :attr:`volatility_ratio` compares the recent window against.
        """
        return atr_from_series(self.highs, self.lows, self.closes, self.minutes - 1)

    @cached_property
    def volatility_ratio(self) -> float:
        """Recent candle size divided by this epic's baseline (1.0 = business as usual).

        The only honest way to say "this market is agitated right now": an absolute
        ATR cannot be compared between epics, and ``range / atr`` cannot either —
        it turns out to measure how *jagged* the path is, not how *big* the swings
        are (a quiet random walk scores higher than a slow 16-point wave). Comparing
        an epic with its own recent past does say it, and 1.5 is a spike.

        Returns 0.0 when the baseline is unknown, and lands near 1.0 whenever the
        buffer is barely longer than the window — a safe "nothing unusual".
        """
        if self.baseline_atr <= 0:
            return 0.0
        return self.amplitude(self.regime_window).atr / self.baseline_atr

    @property
    def atr_pct(self) -> float:
        """Headline ATR in percent of the price — cross-epic volatility."""
        return self.in_pct(self.atr)

    def amplitude(self, minutes: int = 15) -> AmplitudeReading:
        """Measure the window's movement four ways (see :class:`AmplitudeReading`)."""
        return self._cached(("amplitude", minutes), lambda: self._amplitude(minutes))

    def _amplitude(self, minutes: int) -> AmplitudeReading:
        window = self._window(minutes)
        if window < 2:
            return AmplitudeReading(
                minutes=window,
                range=0.0,
                range_pct=0.0,
                atr=0.0,
                band_noise=0.0,
                adverse_noise=0.0,
                retracement=0.0,
                efficiency=0.0,
            )

        highs = self.highs[-window:]
        lows = self.lows[-window:]
        closes = self.closes[-window:]
        extent = max(highs) - min(lows)
        # ATR and the efficiency ratio both consume one extra step of history, so
        # they are asked for one period less than the window they are given.
        period = max(1, window - 1)
        return AmplitudeReading(
            minutes=window,
            range=extent,
            range_pct=self.in_pct(extent),
            atr=atr_from_series(highs, lows, closes, period),
            band_noise=band_noise(closes),
            adverse_noise=adverse_tick_noise(closes, window=period),
            retracement=retracement_depth(closes, window=period),
            efficiency=efficiency_ratio(closes, period),
        )

    @property
    def noise_5m(self) -> float:
        """Thickness of the band price wandered in over 5 minutes."""
        return self.amplitude(5).band_noise

    @property
    def noise_15m(self) -> float:
        """Thickness of the band price wandered in over 15 minutes."""
        return self.amplitude(15).band_noise

    def efficiency(self, minutes: int = 30) -> float:
        """Kaufman efficiency ratio over the window, 0-1 (1 = a straight line)."""
        return self.amplitude(minutes).efficiency

    # ---- channel and momentum --------------------------------------------------

    def channel(self, minutes: int = 60) -> ChannelReading:
        """Locate the price inside the high/low channel of the window."""
        return self._cached(("channel", minutes), lambda: self._channel(minutes))

    def _channel(self, minutes: int) -> ChannelReading:
        window = self._window(minutes)
        if not window:
            return ChannelReading(minutes=0, high=0.0, low=0.0, price=0.0, position=0.5)
        high = max(self.highs[-window:])
        low = min(self.lows[-window:])
        price = self.price
        span = high - low
        position = (price - low) / span if span > 0 else 0.5
        return ChannelReading(
            minutes=window, high=high, low=low, price=price, position=position
        )

    @property
    def position_in_range(self) -> float:
        """Where the price sits in its last hour: 0 = the low, 1 = the high."""
        return self.channel(60).position

    def sma(self, period: int) -> float:
        """Simple moving average of the chosen side's closes."""
        return self._cached(("sma", period), lambda: sma(self.closes, period))

    def ema(self, period: int) -> float:
        """Exponential moving average of the chosen side's closes."""
        return self._cached(("ema", period), lambda: ema(self.closes, period))

    def roc(self, period: int = 10) -> float:
        """Rate of change over ``period`` candles, in percent."""
        return self._cached(
            ("roc", period), lambda: rate_of_change(self.closes, period)
        )

    def rsi(self, period: int = 14) -> float:
        """Wilder RSI of the chosen side's closes, 0-100 (50 = balance)."""
        return self._cached(("rsi", period), lambda: rsi(self.closes, period))

    @property
    def is_stretched_up(self) -> bool:
        """True when the RSI is in the classic overbought zone (≥ 70)."""
        return self.rsi() >= 70

    @property
    def is_stretched_down(self) -> bool:
        """True when the RSI is in the classic oversold zone (≤ 30)."""
        return self.rsi() <= 30

    def cross(self, fast: int = 9, slow: int = 21, kind: str = "ema") -> CrossReading:
        """State and age of a moving-average crossing.

        Args:
            fast: Fast period.
            slow: Slow period (must be greater than ``fast``).
            kind: ``"ema"`` (default, reacts sooner) or ``"sma"``.
        """
        return self._cached(
            ("cross", fast, slow, kind), lambda: self._cross(fast, slow, kind)
        )

    def _cross(self, fast: int, slow: int, kind: str) -> CrossReading:
        if fast >= slow:
            raise ValueError("fast period must be shorter than slow period")
        closes = self.closes
        empty = CrossReading(
            fast_period=fast,
            slow_period=slow,
            fast=0.0,
            slow=0.0,
            gap=0.0,
            bars_since_cross=0,
            crossed_in_history=False,
        )
        if len(closes) < slow + 1:
            return empty

        series = _ema_series if kind == "ema" else _sma_series
        fast_series = series(closes, fast)
        slow_series = series(closes, slow)
        # Both series are aligned on the original indices, so only the tail where
        # the slow one exists can be compared.
        gaps = [f - s for f, s in zip(fast_series[slow - 1 :], slow_series[slow - 1 :])]
        if not gaps:
            return empty

        current = gaps[-1]
        sign = 1 if current > 0 else -1 if current < 0 else 0
        bars = 0
        crossed = False
        for gap in reversed(gaps):
            gap_sign = 1 if gap > 0 else -1 if gap < 0 else 0
            if gap_sign != sign and gap_sign != 0:
                crossed = True
                break
            bars += 1
        return CrossReading(
            fast_period=fast,
            slow_period=slow,
            fast=fast_series[-1],
            slow=slow_series[-1],
            gap=current,
            bars_since_cross=max(0, bars - 1),
            crossed_in_history=crossed,
        )

    # ---- regime ----------------------------------------------------------------

    @cached_property
    def regime(self) -> Regime:
        """Classify the market state over :attr:`regime_window` (see :class:`Regime`).

        Two independent measures decide it: the efficiency ratio says whether the
        path is *directional*, and :attr:`volatility_ratio` — the window's candle
        size against the epic's own baseline — says whether a non-directional market
        is merely *contained* or genuinely *agitated*. Neither alone separates the
        three useful states.
        """
        window = self._window(self.regime_window)
        if window < MIN_REGIME_MINUTES:
            return Regime.UNKNOWN

        amplitude = self.amplitude(window)
        trend = self.trend(window)
        if amplitude.efficiency >= self.trend_efficiency:
            if trend.direction is Trend.UP:
                return Regime.TREND_UP
            if trend.direction is Trend.DOWN:
                return Regime.TREND_DOWN

        if self.volatility_ratio >= self.volatile_ratio:
            return Regime.VOLATILE
        return Regime.RANGE

    @property
    def is_trending(self) -> bool:
        """True when the regime is a trend, either way."""
        return self.regime in (Regime.TREND_UP, Regime.TREND_DOWN)

    # ---- readable levels -------------------------------------------------------

    @property
    def level_tolerance(self) -> float:
        """Price tolerance used to merge pivots into one level.

        ``level_tolerance_atr × ATR``, with a floor of one basis point of the price
        so an epic whose ATR is still 0 (cold buffer) does not collapse every pivot
        into a single level.
        """
        return max(self.atr * self.level_tolerance_atr, self.price * 1e-4)

    @cached_property
    def pivots(self) -> list[Pivot]:
        """Confirmed local tops and bottoms of the whole snapshot."""
        return swing_pivots(self.highs, self.lows, self.pivot_strength)

    @cached_property
    def swing_levels(self) -> list[PriceLevel]:
        """Pivots merged into levels, strongest first (touches then recency)."""
        return cluster_levels(
            self.pivots, self.level_tolerance, series_length=self.minutes
        )

    @cached_property
    def supports(self) -> list[PriceLevel]:
        """Swing levels below the price, nearest first."""
        return support_resistance(self.swing_levels, self.price)[0]

    @cached_property
    def resistances(self) -> list[PriceLevel]:
        """Swing levels above the price, nearest first."""
        return support_resistance(self.swing_levels, self.price)[1]

    @property
    def nearest_support(self) -> PriceLevel | None:
        """Closest level under the market, or ``None`` when none was found."""
        return self.supports[0] if self.supports else None

    @property
    def nearest_resistance(self) -> PriceLevel | None:
        """Closest level above the market, or ``None`` when none was found."""
        return self.resistances[0] if self.resistances else None

    @property
    def room_below(self) -> float:
        """Distance down to the nearest support, ``inf`` when there is none.

        The natural denominator for a stop: a stop placed closer than this sits in
        front of a level the market has already respected.
        """
        support = self.nearest_support
        return self.price - support.price if support else math.inf

    @property
    def room_above(self) -> float:
        """Distance up to the nearest resistance, ``inf`` when there is none."""
        resistance = self.nearest_resistance
        return resistance.price - self.price if resistance else math.inf

    @property
    def swing_high(self) -> float | None:
        """Price of the most recent confirmed top, or ``None``."""
        highs = [p for p in self.pivots if p.kind == "high"]
        return highs[-1].price if highs else None

    @property
    def swing_low(self) -> float | None:
        """Price of the most recent confirmed bottom, or ``None``."""
        lows = [p for p in self.pivots if p.kind == "low"]
        return lows[-1].price if lows else None

    @cached_property
    def round_levels(self) -> list[PriceLevel]:
        """Round levels within reach (``round_radius_atr × ATR``), strongest first.

        Falls back to a 0.5 % radius when the ATR is still unknown, so a cold epic
        still reports the big figures around the price.
        """
        radius = self.atr * self.round_radius_atr or self.price * 0.005
        return psychological_levels(self.price, radius)

    @cached_property
    def nearest_round_level(self) -> PriceLevel | None:
        """Closest round number to the price, whatever its side — **always** one.

        Deliberately *not* read from :attr:`round_levels`, which is bounded by
        ``round_radius_atr``: on a quiet market the nearest big figure often sits
        further than that, and "there is no round number within reach" is a
        different (and much less useful) answer than "it is here, twelve ATR away".
        Searching one step either side of the price always brackets a multiple, so
        this returns ``None`` only for a degenerate price.
        """
        step = round_step(self.price)
        if step <= 0:
            return None
        candidates = psychological_levels(self.price, radius=step, step=step)
        if not candidates:
            return None
        return min(candidates, key=lambda level: level.distance(self.price))

    @cached_property
    def _round_neighbours(self) -> tuple[PriceLevel | None, PriceLevel | None]:
        """The round numbers bracketing the price, ``(below, above)``."""
        step = round_step(self.price)
        if step <= 0:
            return None, None
        levels = psychological_levels(self.price, radius=step, step=step)
        below, above = support_resistance(levels, self.price)
        return (below[0] if below else None, above[0] if above else None)

    @property
    def round_below(self) -> PriceLevel | None:
        """Next round number under the price — where a fall would find orders."""
        return self._round_neighbours[0]

    @property
    def round_above(self) -> PriceLevel | None:
        """Next round number above the price — where a rally would meet them."""
        return self._round_neighbours[1]

    @property
    def distance_to_round(self) -> float:
        """Distance to the nearest round number, ``inf`` when none is in range."""
        level = self.nearest_round_level
        return level.distance(self.price) if level else math.inf

    @property
    def strong_round_levels(self) -> list[PriceLevel]:
        """Only the big figures in range (strength ≥ 0.6), nearest first."""
        return sorted(
            (level for level in self.round_levels if level.strength >= 0.6),
            key=lambda level: level.distance(self.price),
        )

    # ---- summary ---------------------------------------------------------------

    def as_dict(self) -> dict:
        """Flat snapshot of the headline figures — for logs, the dashboard and tests."""
        return {
            "epic": self.epic,
            "minutes": self.minutes,
            "price": self.price,
            "spread": self.spread,
            "spread_ratio": self.spread_ratio,
            "atr": self.atr,
            "atr_pct": self.atr_pct,
            "volatility_ratio": self.volatility_ratio,
            "regime": str(self.regime),
            "alignment": self.alignment,
            "acceleration": self.acceleration,
            "streak": self.streak,
            "rsi": self.rsi(),
            "position_in_range": self.position_in_range,
            "trend_5m_pct": self.trend_5m.pct,
            "trend_15m_pct": self.trend_15m.pct,
            "trend_30m_pct": self.trend_30m.pct,
            "trend_60m_pct": self.trend_60m.pct,
            "trend_15m_degrees": self.trend_15m.degrees,
            "trend_15m_r2": self.trend_15m.r_squared,
            "ticks_15m_bias": self.ticks_15m.bias,
            "ticks_15m_flat_ratio": self.ticks_15m.flat_ratio,
            "noise_5m": self.noise_5m,
            "noise_15m": self.noise_15m,
            "efficiency_30m": self.efficiency(30),
            "nearest_support": (
                self.nearest_support.price if self.nearest_support else None
            ),
            "nearest_resistance": (
                self.nearest_resistance.price if self.nearest_resistance else None
            ),
            "nearest_round": (
                self.nearest_round_level.price if self.nearest_round_level else None
            ),
        }


def _sma_series(values: list[float], period: int) -> list[float]:
    """Rolling SMA aligned on ``values`` (0.0 before the window is full).

    Computed in one pass rather than by calling :func:`~src.core.indicators.sma`
    per index, which would be quadratic on a 200-candle buffer.
    """
    out = [0.0] * len(values)
    running = 0.0
    for i, value in enumerate(values):
        running += value
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def _ema_series(values: list[float], period: int) -> list[float]:
    """Rolling EMA aligned on ``values`` (0.0 before the seed, then SMA-seeded)."""
    out = [0.0] * len(values)
    if len(values) < period or period < 1:
        return out
    alpha = 2.0 / (period + 1)
    current = sum(values[:period]) / period
    out[period - 1] = current
    for i in range(period, len(values)):
        current += alpha * (values[i] - current)
        out[i] = current
    return out


def read_all(buffer: PriceBuffer, **options) -> dict[str, CurveReading]:
    """Build one :class:`CurveReading` per tracked epic — the cross-epic entry point.

    Readings are lazy, so this is cheap: nothing is computed until a field is read.
    Intended for a ranker, which needs the same measures on every candidate to
    compare them.
    """
    return {
        epic: CurveReading(buffer.get_or_create(epic), **options)
        for epic in buffer.tracked_epics
    }


class PositionReading:
    """Read-only view of an **open position** — everything measured since its open.

    The market reading answers "what is this curve doing?"; this one answers "what
    has it done *to me* since I entered?", which is a different question and the one
    an exit rule needs. Both are available from here: :attr:`curve` is a
    :class:`CurveReading` bounded to the candles recorded after the open (so a
    pre-entry rally can never satisfy a since-open test — the trap documented on
    :meth:`~src.feed.price_buffer.EpicBuffer.since`), and :attr:`market` is the
    unbounded one, for the amplitude measures that legitimately want history.

    Everything is expressed in **profit terms** rather than "up/down": the reading
    carries the position's ``direction`` and normalises internally, so the same
    field means the same thing for a long and a short (the convention
    :class:`~src.exit.zones.base.StopContext` already uses on the exit path).
    Gains are counted from ``level_zero`` — the break-even level, spread included —
    not from the raw fill, so a "gain" here is the money actually on the table.

    Example::

        p = PositionReading.from_position(position, buf)
        p.gain_in_atr        # is the profit big relative to this market's noise?
        p.giveback_ratio     # how much of the best moment has been handed back
        p.time_in_profit     # has it ever really worked, or only flickered green?
        p.curve.trend_15m    # what the curve has done since I entered

    Args:
        buf: The epic's **full** live buffer; it is sliced internally.
        direction: ``"BUY"`` or ``"SELL"``.
        level_zero: Break-even level — the reference every gain is counted from.
        level_open: Raw fill level, for :attr:`gain_from_open` (defaults to
            ``level_zero``).
        current_price: Live close-out price; defaults to the last close-out close
            in the buffer.
        opened_at: Instant the position opened; ``None`` disables the slicing and
            :attr:`curve` then spans the whole buffer.
        level_margin: The noise-margin level, for :attr:`progress_to_margin`.
        level_follower: The live software stop, for :attr:`stop_room`.
        euro_per_point: Contract value, for the euro-denominated fields.
        options: Forwarded to the underlying :class:`CurveReading` (``side`` is
            imposed by the direction and cannot be overridden).
    """

    def __init__(
        self,
        buf: EpicBuffer,
        *,
        direction: str = "BUY",
        level_zero: float,
        level_open: float | None = None,
        current_price: float | None = None,
        opened_at: datetime | None = None,
        level_margin: float | None = None,
        level_follower: float | None = None,
        euro_per_point: float = 0.0,
        **options,
    ) -> None:
        options.pop("side", None)
        self.direction = direction
        self.level_zero = level_zero
        self.level_open = level_open if level_open is not None else level_zero
        self.level_margin = level_margin
        self.level_follower = level_follower
        self.euro_per_point = euro_per_point
        self.opened_at = opened_at

        side = "offer" if direction == "SELL" else "bid"
        self.market = CurveReading(buf, side=side, **options)
        self.curve = CurveReading(buf.since(opened_at), side=side, **options)
        self.current_price = (
            current_price if current_price is not None else self.market.price
        )

    @classmethod
    def from_position(
        cls, position, buf: EpicBuffer, current_price: float | None = None, **options
    ) -> PositionReading:
        """Build a reading from a persisted ``Position`` row.

        Duck-typed on purpose (``getattr`` + ``float``), like the close profile
        does: this module is shared infrastructure and must not import the ORM
        layer, and it keeps bare test stubs usable. ``time_open`` is a naive
        time-of-day stamped from UTC at open, so it is recombined with ``date`` and
        marked UTC to line up with the buffer's aware timestamps.
        """

        def level(name: str) -> float | None:
            value = getattr(position, name, None)
            return None if value is None else float(value)

        day = getattr(position, "date", None)
        time_open = getattr(position, "time_open", None)
        opened_at = (
            datetime.combine(day, time_open, tzinfo=UTC)
            if day is not None and time_open is not None
            else None
        )
        zero = level("level_zero")
        opened = level("level_open")
        if zero is None:
            zero = opened
        if zero is None:
            raise ValueError("position carries neither level_zero nor level_open")

        return cls(
            buf,
            direction=getattr(position, "direction", "BUY") or "BUY",
            level_zero=zero,
            level_open=opened,
            current_price=current_price,
            opened_at=opened_at,
            level_margin=level("level_margin"),
            level_follower=level("level_follower"),
            euro_per_point=level("euro_per_point") or 0.0,
            **options,
        )

    def __repr__(self) -> str:
        return (
            f"PositionReading({self.curve.epic} {self.direction}, "
            f"{self.minutes_open}min, gain={self.gain:+.5f})"
        )

    # ---- direction-aware primitives -------------------------------------------

    @property
    def sign(self) -> float:
        """``+1`` when profit is up (BUY), ``−1`` when profit is down (SELL)."""
        return -1.0 if self.direction == "SELL" else 1.0

    def favourable(self, level: float) -> float:
        """``level`` in sign-normalised space, where rising always means profit."""
        return self.sign * level

    def gain_of(self, level: float) -> float:
        """Profit distance of ``level`` from break-even — positive = in profit."""
        return self.sign * (level - self.level_zero)

    # ---- age -------------------------------------------------------------------

    @property
    def minutes_open(self) -> int:
        """Candles recorded since the open — the position's age in minutes.

        ``0`` when the position opened inside the current minute (no completed
        candle yet), which is a real state and not an error: nothing since-open can
        be measured on it, so every excursion field reads 0.0.
        """
        return self.curve.minutes

    @property
    def closes(self) -> list[float]:
        """Close-out closes since the open — bid for a BUY, offer for a SELL."""
        return self.curve.closes

    @cached_property
    def favourable_closes(self) -> list[float]:
        """:attr:`closes` sign-normalised: rising always means going into profit."""
        return [self.sign * close for close in self.closes]

    # ---- current gain ----------------------------------------------------------

    @property
    def gain(self) -> float:
        """Live gain in price points, counted from break-even (negative = losing)."""
        return self.gain_of(self.current_price)

    @property
    def gain_from_open(self) -> float:
        """Live gain counted from the raw fill instead of break-even.

        The difference between the two is the spread paid on entry; comparing them
        is the only way to see a position that is "up" on the fill while still
        costing money to close.
        """
        return self.sign * (self.current_price - self.level_open)

    @property
    def gain_pct(self) -> float:
        """Live gain in percent of the price."""
        return self.gain / self.current_price * 100 if self.current_price else 0.0

    @property
    def gain_euro(self) -> float:
        """Live gain in euros (0.0 when the contract value is unknown)."""
        return self.gain * self.euro_per_point

    @property
    def gain_in_atr(self) -> float:
        """Live gain in units of one candle's range — big *for this market* or not?

        The field that makes two positions comparable: +8 points on an index and
        +0.0008 on a forex pair can both be "half a candle", and a rule written on
        ATR units applies to the whole book at once.
        """
        return self.market.in_atr(self.gain)

    @property
    def is_profitable(self) -> bool:
        """True when closing now would return more than break-even."""
        return self.gain > 0

    # ---- excursions ------------------------------------------------------------

    @cached_property
    def _extremes(self) -> tuple[float, float, int]:
        """``(best, worst, best_index)`` in sign-normalised space, since the open."""
        highs = self.curve.highs
        lows = self.curve.lows
        if not highs or not lows:
            zero = self.favourable(self.current_price)
            return zero, zero, 0

        per_candle_best = [
            max(self.sign * high, self.sign * low) for high, low in zip(highs, lows)
        ]
        per_candle_worst = [
            min(self.sign * high, self.sign * low) for high, low in zip(highs, lows)
        ]
        best = max(per_candle_best)
        return best, min(per_candle_worst), per_candle_best.index(best)

    @property
    def peak_price(self) -> float:
        """Best close-out price reached since the open (real price, not normalised)."""
        return self.sign * self._extremes[0]

    @property
    def trough_price(self) -> float:
        """Worst close-out price reached since the open."""
        return self.sign * self._extremes[1]

    @property
    def mfe(self) -> float:
        """Maximum favourable excursion: the best gain ever available, in points.

        The number that turns a losing trade into a diagnosis — a trade closed at
        0 € after showing +40 points was an exit problem, not an entry problem.
        """
        return max(0.0, self._extremes[0] - self.favourable(self.level_zero))

    @property
    def mae(self) -> float:
        """Maximum adverse excursion: the worst drawdown suffered, in points (≥ 0)."""
        return max(0.0, self.favourable(self.level_zero) - self._extremes[1])

    @property
    def mfe_in_atr(self) -> float:
        """Best available gain in ATR units."""
        return self.market.in_atr(self.mfe)

    @property
    def mae_in_atr(self) -> float:
        """Worst drawdown in ATR units — how much noise the trade had to absorb."""
        return self.market.in_atr(self.mae)

    @property
    def mfe_euro(self) -> float:
        """Best available gain in euros."""
        return self.mfe * self.euro_per_point

    @property
    def mae_euro(self) -> float:
        """Worst drawdown in euros."""
        return self.mae * self.euro_per_point

    @property
    def giveback(self) -> float:
        """Points handed back from the best moment — ``mfe − gain``, never negative."""
        return max(0.0, self.mfe - self.gain)

    @property
    def giveback_ratio(self) -> float:
        """Share of the best available gain already handed back, 0-1.

        The live measure of a trailing stop's quality: 0 means the position is at
        its high, 1 means the whole excursion has evaporated. A ratio drifting past
        ~0.5 while still green is the signal that the gain is leaking.
        """
        return min(1.0, self.giveback / self.mfe) if self.mfe > 0 else 0.0

    @property
    def minutes_since_peak(self) -> int:
        """Candles since the best moment — how long the position has been drifting."""
        if not self.minutes_open:
            return 0
        return self.minutes_open - 1 - self._extremes[2]

    @property
    def edge_ratio(self) -> float:
        """``mfe / mae`` — what the trade offered against what it cost to hold.

        ``inf`` when the position never went adverse; below 1 the market gave more
        pain than opportunity, whatever the current gain says.
        """
        return self.mfe / self.mae if self.mae > 0 else math.inf

    # ---- behaviour since the open ---------------------------------------------

    @cached_property
    def _profit_flags(self) -> list[bool]:
        """Per-candle "was the close in profit?" since the open."""
        threshold = self.favourable(self.level_zero)
        return [close > threshold for close in self.favourable_closes]

    @property
    def crossed_breakeven(self) -> bool:
        """True when at least one candle since the open closed past break-even."""
        return any(self._profit_flags)

    @property
    def minutes_to_breakeven(self) -> int | None:
        """Minutes the position took to first close past break-even, or ``None``.

        A trade that has never cleared break-even after 40 minutes is a different
        animal from one that cleared it in 2 and came back, and no gain field tells
        them apart.
        """
        for index, in_profit in enumerate(self._profit_flags):
            if in_profit:
                return index
        return None

    @property
    def time_in_profit(self) -> float:
        """Share of the candles since the open that closed in profit, 0-1."""
        flags = self._profit_flags
        return sum(flags) / len(flags) if flags else 0.0

    @property
    def favourable_streak(self) -> int:
        """Signed run of candles moving *into profit* (+) or against it (−)."""
        return int(self.sign * self.curve.streak)

    @property
    def trend_since_open(self) -> TrendReading:
        """Fitted trend of the close-out price over the whole life of the position."""
        return self.curve.trend(self.minutes_open)

    @property
    def efficiency_since_open(self) -> float:
        """Efficiency ratio since the open, 0-1: has the move been a line or a fight?"""
        return self.curve.efficiency(self.minutes_open)

    def is_stalling(self, minutes: int = 5) -> bool:
        """True when the last ``minutes`` went nowhere relative to their own noise.

        Compares the net favourable move over the window with the window's adverse
        noise band: below it, the position is not progressing — it is being held for
        nothing while the stop stays exposed.
        """
        window = min(minutes, self.minutes_open)
        if window < 2:
            return False
        closes = self.favourable_closes[-window:]
        net = abs(closes[-1] - closes[0])
        # ``<=`` so a dead-flat window (net and noise both 0) reads as stalling,
        # which is precisely what it is.
        return net <= self.curve.amplitude(window).adverse_noise

    # ---- distances to the plan -------------------------------------------------

    @property
    def stop_room(self) -> float:
        """Points between the live price and the software stop, ``inf`` when unknown."""
        if self.level_follower is None:
            return math.inf
        return self.favourable(self.current_price) - self.favourable(
            self.level_follower
        )

    @property
    def stop_room_in_atr(self) -> float:
        """Stop distance in ATR units — below ~1 an ordinary candle can reach it."""
        room = self.stop_room
        return self.market.in_atr(room) if room != math.inf else math.inf

    @property
    def stop_room_in_noise(self) -> float:
        """Stop distance divided by the recent adverse-noise band.

        The complement of :attr:`stop_room_in_atr`: ATR spans a whole candle's
        range, this one only what a close-to-close step gives back, which is what
        actually reaches a stop between two candles.
        """
        room = self.stop_room
        noise = self.market.amplitude(20).adverse_noise
        if room == math.inf or noise <= 0:
            return math.inf
        return room / noise

    @property
    def locked_gain(self) -> float:
        """Gain the stop would return if hit now — negative while under water."""
        if self.level_follower is None:
            return 0.0
        return self.gain_of(self.level_follower)

    @property
    def is_secured(self) -> bool:
        """True when the stop itself sits in profit: the trade cannot lose any more."""
        return self.locked_gain > 0

    @property
    def progress_to_margin(self) -> float:
        """Gain as a fraction of the noise margin, ``0.0`` when the margin is unknown.

        1.0 means the move has exactly cleared the band inside which a "gain" is
        indistinguishable from bid/offer churn.
        """
        if self.level_margin is None:
            return 0.0
        margin_gain = self.gain_of(self.level_margin)
        return self.gain / margin_gain if margin_gain > 0 else 0.0

    # ---- summary ---------------------------------------------------------------

    def as_dict(self) -> dict:
        """Flat snapshot of the position figures — for logs, the dashboard and tests."""
        return {
            "epic": self.curve.epic,
            "direction": self.direction,
            "minutes_open": self.minutes_open,
            "gain": self.gain,
            "gain_euro": self.gain_euro,
            "gain_in_atr": self.gain_in_atr,
            "mfe": self.mfe,
            "mae": self.mae,
            "mfe_in_atr": self.mfe_in_atr,
            "mae_in_atr": self.mae_in_atr,
            "giveback": self.giveback,
            "giveback_ratio": self.giveback_ratio,
            "minutes_since_peak": self.minutes_since_peak,
            "edge_ratio": self.edge_ratio,
            "crossed_breakeven": self.crossed_breakeven,
            "minutes_to_breakeven": self.minutes_to_breakeven,
            "time_in_profit": self.time_in_profit,
            "favourable_streak": self.favourable_streak,
            "trend_since_open_pct": self.trend_since_open.pct,
            "efficiency_since_open": self.efficiency_since_open,
            "stop_room": self.stop_room,
            "stop_room_in_atr": self.stop_room_in_atr,
            "locked_gain": self.locked_gain,
            "is_secured": self.is_secured,
            "progress_to_margin": self.progress_to_margin,
            "regime": str(self.market.regime),
        }
