"""Shape-selected initial stop — the recent level the signal's *form* justifies.

Every other policy in :mod:`src.stops` answers "how far?" with one formula and
applies it to every market. This one answers "**which level?**" first: it builds
several candidate stops out of the epic's recent history and picks the one the
current shape of the curve makes meaningful. The distance is a consequence of that
choice, never the input.

Why the shape decides the level
-------------------------------
A protective stop is only informative when the level it sits under would, if
broken, *invalidate the reason for the trade*. Whether a given low carries that
meaning depends entirely on how the price got there:

- On a **clean directional path**, the last hour's low is a level the price has
  *left behind*. It is only revisited if the move breaks, so a stop just under it
  says something. Tight is correct here.
- On a **noisy path with deep pull-backs**, the hour's low is inside the
  breathing of the move: the price has already dipped there and will again
  without the trade being wrong. The invalidation sits at the wider structure —
  the three-hour low.
- On a **directionless path** (a range), the hour's low is simply wherever the
  noise last happened to poke. The price has swept the whole band and will sweep
  it again, so no recent low means anything; only the session's own extreme is
  outside the churn. Observed on ``IX.D.DOW.IFE.IP`` (2026-07-31 09:37): hourly
  low 7.0 points under the bid while a single candle averaged 10.6 points of
  range — stop hit 21 seconds after the open.

So the same 40-point distance is either a real structural stop or pure noise
depending on the path, and no fixed multiple of ATR can tell the two apart: ATR
grows with a clean trend exactly as it grows with chop.

The three candidate levels
--------------------------
All three are **raw extremes** read on the side the stop is triggered on (the bid
lows for a BUY, the offer highs for a SELL), so the wick *is* the level and the
stop is never inside a range the market has already visited. From tightest to
widest:

``hour_lookback``
    Lowest bid low / highest offer high of the last hour of recording. Same level
    as :class:`~src.stops.stop_hourlow.StopHourLow`.
``long_lookback``
    The same over three hours. Still inside the live buffer's ceiling
    (``buffer_max_candles``), so it costs nothing to read.
``day_extreme``
    The whole session's extreme, passed in by the caller from **outside** the
    buffer (the ``candle`` table) because a session routinely exceeds the buffer's
    3 h 20 of history. ``None`` when unavailable — the policy then falls back on
    the widest window the buffer does hold, so it degrades instead of failing.

Shape classification
--------------------
Two measures over the same ``shape_period`` window, both read on the mid closes so
the verdict is side-agnostic (the same path has to be judged for a long and a
short):

- :func:`~src.core.indicators.efficiency_ratio` — ``|net move| / path travelled``,
  ``1`` for a clean ramp and ``0`` for pure chop. This separates *directional*
  from *directionless*.
- the regression **R²** — how tightly the points follow their own trend line.
  Among directional paths this separates a clean ramp from one that gets there
  through deep retracements.

Note R² is read for *cleanliness only*, not direction: the direction is an input
(the entry strategy already decided it), so a falling path fitted tightly is
"clean" here just like a rising one.

The mapping (see :func:`classify_shape`)::

    ER >= min_efficiency  and  R2 >= min_r_squared   -> CLEAN_TREND  -> hour
    ER >= min_efficiency  and  R2 <  min_r_squared   -> NOISY_TREND  -> three hours
    ER <  min_efficiency                            -> CHOP         -> session

The CHOP branch is a **safety net, not a plan**. No stop placement rescues a trade
taken without direction: measured over 27/07–03/08 (153 positions), refusing the
low-ER opens outright was worth far more than any placement change on them. That
refusal belongs to the entry side — see
:class:`~src.entry.open_ultraranking.OpenUltraRanking`, which vetoes those opens
before they happen. This policy is nonetheless composable with any entry, so when
a chop trade does reach it the widest available level is the only defensible
placement.

Buffer, floor and cap
---------------------
``buffer_atr_k × ATR`` is placed beyond the chosen level. Unlike
``stop_hourlow`` this defaults to a **non-zero** cushion: a stop sitting exactly
*on* the level is taken out by the wick that defines it. Observed on
``PA.D.CC.MONTH2.IP`` (2026-08-03 07:32): the initial stop was pierced by
**0.3 point** and the price returned above the entry within the hour, turning what
would have been a positive trade into -81 €.

The distance is then floored at ``max(noise floor, min_stop_atr_k × ATR,
min_stop_spread_k × spread)`` — see
:func:`~src.stops.stop_hourlow.noise_floor_distance` for the curve-state term,
reused as-is so this policy and ``stop_hourlow`` share one definition of "outside
the band". It is optionally capped at ``max_stop_atr_k × ATR`` (``0`` = no cap,
the default: the point of the policy is to honour the level it selected). The
floor always wins over the cap.

The broker minimum is **not** handled here. IG's own minimum-stop-distance rule is
applied downstream in
:meth:`~src.execution.trading.TradingService.open_position`, which widens the stop
to ``min_stop_price × (1 + stop_min_distance_margin)`` when a policy asks for
tighter and shifts the software levels with it. It is therefore a hard floor under
every branch above, whatever this policy returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.core.indicators import atr, efficiency_ratio, linear_regression
from src.feed.price_buffer import Candle, EpicBuffer
from src.stops.base import StopDistance
from src.stops.stop_hourlow import noise_floor_distance, window_extreme

#: The three shapes :func:`classify_shape` distinguishes, each mapping to one
#: candidate level in :meth:`StopShape.initial_stop`.
Shape = Literal["clean_trend", "noisy_trend", "chop"]

CLEAN_TREND: Shape = "clean_trend"
NOISY_TREND: Shape = "noisy_trend"
CHOP: Shape = "chop"


def classify_shape(
    candles: list[Candle],
    *,
    min_efficiency: float,
    min_r_squared: float,
) -> Shape:
    """Classify the path shape of ``candles`` — the choice of stop level.

    Both measures are read on the mid closes, so the verdict does not depend on
    which side the position takes. See the module docstring for why each shape
    implies a different level.

    Args:
        candles: The classification window's candles, oldest first.
        min_efficiency: Efficiency-ratio floor separating a directional path from
            chop. Below it the path is directionless whatever its R².
        min_r_squared: Regression-R² floor separating a clean directional path
            from one that gets there through deep retracements.

    Returns:
        :data:`CHOP` when the window is directionless *or* too short to measure
        (fewer than three candles — the conservative verdict, since an unmeasured
        window is not evidence of a trend), else :data:`CLEAN_TREND` or
        :data:`NOISY_TREND`.
    """
    mids = [candle.mid_close for candle in candles]
    if len(mids) < 3:
        return CHOP
    # ``efficiency_ratio`` consumes ``period + 1`` values, so the period is the
    # number of steps in the window, not its length.
    if efficiency_ratio(mids, len(mids) - 1) < min_efficiency:
        return CHOP
    if linear_regression(mids).r_squared < min_r_squared:
        return NOISY_TREND
    return CLEAN_TREND


@dataclass
class StopShape(StopDistance):
    """Initial stop at the recent level the current path shape makes meaningful."""

    name = "stop_shape"

    atr_period: int = 14

    # Candidate windows, in candles ≈ minutes on 1-minute data. Both stay within
    # the live buffer's ceiling (``buffer_max_candles``, 200); the session-wide
    # candidate comes from the caller instead (see ``day_extreme``).
    hour_lookback: int = 60  # CLEAN_TREND — the level the move left behind
    long_lookback: int = 180  # NOISY_TREND — outside the pull-back breathing

    # Shape classification (see ``classify_shape``). ``min_efficiency`` is the
    # same regime threshold ``open_ultraranking`` vetoes on, measured over the
    # same 60-candle window, so the entry's "this is chop" and this policy's
    # agree by construction.
    shape_period: int = 60
    min_efficiency: float = 0.15
    min_r_squared: float = 0.50

    # Cushion placed beyond the chosen level. Non-zero on purpose: a stop resting
    # exactly on the level is taken out by the wick that defines it.
    buffer_atr_k: float = 0.3

    # Noise floor — the curve-state term, shared with ``stop_hourlow``. Measured
    # over the same window as the shape verdict, so the floor and the
    # classification describe the same stretch of curve.
    noise_lookback: int = 0  # 0 = reuse ``shape_period``
    noise_trend_k: float = 0.5  # × band, clean trend (ER = 1) — level is real
    noise_chop_k: float = 2.0  # × band, pure noise (ER = 0) — stand outside it

    # Absolute back-stops, for a market whose band has collapsed. IG's own
    # minimum-distance rule is applied downstream and floors all of this.
    min_stop_atr_k: float = 0.5  # distance floor (× ATR) — never inside a flat hour
    min_stop_spread_k: float = 2.0  # distance floor (× spread) — never inside noise
    max_stop_atr_k: float = 0.0  # distance cap (× ATR); 0 = no cap (level as-is)

    @classmethod
    def from_settings(cls, settings) -> StopShape:
        # Parameters are constants of this class (the field defaults above), so
        # the policy builds from those and ignores ``settings``. Tune by editing
        # the constants; select the policy via STOP_STRATEGY in .env.
        return cls()

    def _candidate_level(
        self,
        shape: Shape,
        candles: list[Candle],
        *,
        direction: str,
        day_extreme: float | None,
    ) -> float | None:
        """The extreme this ``shape`` anchors its stop on, or ``None`` if unknown.

        ``None`` means the window held no candle at all (warm-up should prevent
        it); :meth:`initial_stop` then falls back on the distance floor.
        """
        if shape is CHOP and day_extreme is not None:
            # The session extreme lives outside the buffer, so it is the caller's
            # to supply. Only the chop branch needs to reach that far back.
            return day_extreme
        lookback = self.hour_lookback if shape is CLEAN_TREND else self.long_lookback
        # A CHOP verdict with no session extreme available degrades to the widest
        # window the buffer does hold rather than failing the open.
        return window_extreme(candles[-lookback:], direction=direction)

    def initial_stop(
        self,
        *,
        entry_level: float,
        direction: str,
        buf: EpicBuffer,
        day_extreme: float | None = None,
    ) -> float:
        candles = list(buf.candles)
        atr_value = atr(candles, self.atr_period)
        last = buf.last
        spread = last.spread if last else 0.0
        sign = -1 if direction == "SELL" else 1
        # The stop is triggered on the close-out side: the bid for a BUY
        # (``entry_level`` already is the live bid), the offer for a SELL.
        if direction == "SELL":
            reference = last.offer_close if last else entry_level
        else:
            reference = entry_level

        shape = classify_shape(
            candles[-self.shape_period :],
            min_efficiency=self.min_efficiency,
            min_r_squared=self.min_r_squared,
        )
        level = self._candidate_level(
            shape, candles, direction=direction, day_extreme=day_extreme
        )
        if level is None:
            # No history at all (warm-up should prevent this): the floor below
            # takes over entirely.
            distance = 0.0
        else:
            # Negative when the level sits on the wrong side of the reference —
            # the floor below takes over in that case too.
            distance = sign * (reference - level) + self.buffer_atr_k * atr_value

        # The floor that matters on a raw-extreme policy: a level inside a choppy
        # band is not a level, so the stop must clear the band itself. The ATR and
        # spread terms stay as absolute back-stops underneath it.
        noise_window = candles[-(self.noise_lookback or self.shape_period) :]
        min_distance = max(
            noise_floor_distance(
                noise_window,
                trend_k=self.noise_trend_k,
                chop_k=self.noise_chop_k,
            ),
            self.min_stop_atr_k * atr_value,
            self.min_stop_spread_k * spread,
        )
        distance = max(distance, min_distance)
        if self.max_stop_atr_k > 0:
            # The floor always wins over the cap, so a misconfigured cap can never
            # tighten the stop below ``min_distance``.
            distance = min(distance, max(self.max_stop_atr_k * atr_value, min_distance))
        return reference - sign * distance
