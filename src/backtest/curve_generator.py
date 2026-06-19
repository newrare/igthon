"""Pseudo-random synthetic market curve generator.

Produces completely fictional price series as standard :class:`Candle` lists.
This module is self-contained on purpose: the rest of the project only ever
sees the resulting candles and must not depend on *how* they are generated.
Everything below the public ``generate_curve`` function (regimes, drift,
volatility model) is an implementation detail that can change freely.

Generation is seeded, so the same (profile, seed) pair always yields the same
curve — handy to replay a simulation on an identical market.
"""

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.feed.price_buffer import Candle

# Public list of supported curve profiles (shown in the simulator UI).
PROFILES = [
    "random",
    "mixte",
    "trend_up",
    "trend_down",
    "sideways",
    "volatile",
    "mean_reverting",
]

# Single-behaviour profiles that the "random" / "mixte" mixers draw from,
# regime after regime. "mixte" additionally throws mean-reverting phases into
# the mix; "random" stays purely directional/volatility-based.
_RANDOM_MIX = ["trend_up", "trend_down", "sideways", "volatile"]
_MIXTE_MIX = [*_RANDOM_MIX, "mean_reverting"]

DEFAULT_NUM_CANDLES = 600  # one trading day of 1-minute candles (07:00→17:00)
DEFAULT_BASE_PRICE = 8000.0
DEFAULT_START_HOUR = 7  # UTC — before strategy_hour_start so buffers warm up


@dataclass(slots=True)
class _Regime:
    """One market phase: a drift/volatility pair lasting a few candles."""

    drift: float  # average price change per candle, in volatility units
    volatility: float  # per-candle standard deviation, relative to price
    length: int  # number of candles before the next regime is drawn
    mean_revert: bool = False  # if set, drift is recomputed against the anchor


def _draw_regime(rng: random.Random, profile: str) -> _Regime:
    """Draw the next market phase for the given profile."""
    length = rng.randint(20, 60)
    base_vol = rng.uniform(0.00015, 0.00035)

    if profile == "trend_up":
        return _Regime(drift=rng.uniform(0.4, 1.2), volatility=base_vol, length=length)
    if profile == "trend_down":
        return _Regime(
            drift=rng.uniform(-1.2, -0.4), volatility=base_vol, length=length
        )
    if profile == "sideways":
        return _Regime(drift=rng.uniform(-0.1, 0.1), volatility=base_vol, length=length)
    if profile == "volatile":
        return _Regime(
            drift=rng.uniform(-0.6, 0.6), volatility=base_vol * 3.0, length=length
        )
    if profile == "mean_reverting":
        # Drift is recomputed each candle against the anchor; see generator loop.
        return _Regime(drift=0.0, volatility=base_vol, length=length, mean_revert=True)
    # "random" / "mixte": pick a fresh sub-behaviour for each regime.
    pool = _MIXTE_MIX if profile == "mixte" else _RANDOM_MIX
    return _draw_regime(rng, rng.choice(pool))


def generate_curve(
    profile: str = "random",
    *,
    seed: int | None = None,
    num_candles: int = DEFAULT_NUM_CANDLES,
    base_price: float = DEFAULT_BASE_PRICE,
    day: datetime | None = None,
) -> list[Candle]:
    """Generate a fictional intraday price curve as 1-minute candles.

    Args:
        profile: Market shape — one of :data:`PROFILES`.
        seed: RNG seed; the same (profile, seed) always yields the same curve.
        num_candles: Number of 1-minute candles to produce.
        base_price: Price level around which the curve starts.
        day: Day of the series (defaults to today, candles from 07:00 UTC).

    Returns:
        Ordered candles (oldest first) with coherent OHLC and bid/offer.
    """
    if profile not in PROFILES:
        raise ValueError(f"Unknown profile: {profile!r} (expected one of {PROFILES})")

    rng = random.Random(seed)
    start = (day or datetime.now(UTC)).replace(
        hour=DEFAULT_START_HOUR, minute=0, second=0, microsecond=0
    )

    # Slight random offset so two curves never start at the exact same level.
    price = base_price * rng.uniform(0.97, 1.03)
    anchor = price  # mean-reversion anchor
    spread = base_price * rng.uniform(0.00008, 0.00014)

    candles: list[Candle] = []
    regime = _draw_regime(rng, profile)
    remaining = regime.length

    for i in range(num_candles):
        if remaining <= 0:
            regime = _draw_regime(rng, profile)
            remaining = regime.length
        remaining -= 1

        sigma = regime.volatility * price
        drift = regime.drift * sigma
        if regime.mean_revert:
            # Pull back toward the anchor proportionally to the distance.
            drift = 0.05 * (anchor - price)

        # Build the candle from a handful of intra-minute steps.
        steps = [price]
        step_price = price
        for _ in range(4):
            step_price += drift / 4 + rng.gauss(0, sigma / 2)
            steps.append(step_price)

        bid_open = steps[0]
        bid_close = steps[-1]
        bid_high = max(steps) + abs(rng.gauss(0, sigma / 4))
        bid_low = min(steps) - abs(rng.gauss(0, sigma / 4))

        # Spread wobbles a little and widens with volatility bursts.
        candle_spread = max(
            spread * rng.uniform(0.85, 1.15) + sigma * 0.05, spread * 0.5
        )

        candles.append(
            Candle(
                timestamp=start + timedelta(minutes=i),
                bid_open=round(bid_open, 5),
                bid_close=round(bid_close, 5),
                bid_high=round(bid_high, 5),
                bid_low=round(bid_low, 5),
                offer_open=round(bid_open + candle_spread, 5),
                offer_close=round(bid_close + candle_spread, 5),
                offer_high=round(bid_high + candle_spread, 5),
                offer_low=round(bid_low + candle_spread, 5),
                volume=rng.randint(50, 500),
            )
        )
        price = bid_close

    return candles
