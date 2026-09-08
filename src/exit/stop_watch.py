"""The touch watcher — is this stop touch a graze, or a real turn?

The software backstop closes the position the moment the close-out price reaches
the follower (:meth:`~src.exit.close_zoneprofit.CloseZoneProfit.evaluate`). That
is one sample of a noisy series: a stop parked behind a running move gets brushed
by pull-backs that recover within the minute, and the trade is booked out of a
move that then continues. Measured on the archive, this pattern costs the system
its largest winners (see ``docs/strategies/close_zoneprofit.md``).

This module holds the rule that decides, on a touch, whether to close **now** or
give the market one minute to disown it. Three bounds keep the deferral from
turning into "hoping":

- **it only ever protects an acquired gain** — the follower must already lock more
  than ``min_gain_noise`` noise bands of profit. A touch on the stop placed at
  open, or on any stop still short of break-even, closes immediately as before:
  the watcher must never widen a loss, only risk a slice of something already won;
- **the give-back is capped** — the price may sit at most ``band`` beyond the
  follower, the tightest of ``band_noise_k × noise`` (an ordinary adverse step),
  the broker gap (see below) and ``band_gain_frac`` of the locked gain. Past it the
  move is a real break and the position closes at once. So a watch that turns out
  wrong costs a *known fraction of the gain*, never an open-ended slide;
- **it lasts one minute** — a completed 1-minute candle whose close-out close sits
  beyond the follower ends the watch: a graze does not survive a minute close.
  :class:`~src.feed.price_buffer.EpicBuffer` holds only completed candles, so the
  touch is granted the minute it happens in and is judged on that minute's close.

The band is also capped at the **broker gap** — one spread plus the wider of the
noise band and IG's minimum stop distance — because the order resting at IG sits
about that far beyond the follower (see
:meth:`~src.execution.trading.TradingService._broker_stop_level`). Watching past
it would be pointless: the broker would fill first, and at a worse level than the
watcher's own threshold. It is re-measured here from the live spread and noise
rather than read from the execution layer, exactly as the group pre-pass does, so
no execution state leaks into the exit domain. Note this cap is *inert at the
default* ``band_noise_k = 0.5``: the gap is at least one whole noise band, so it
only ever bounds a configuration asking for more than that.

``band_noise_k = 0`` disables the watcher: every touch closes on the spot, which is
the behaviour that predates this module.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TouchWatchParams:
    """Shaping constants of the touch watcher (see the module docstring)."""

    #: Watch band as a multiple of the adverse-noise band — the amplitude of an
    #: ordinary adverse step, the very thing a graze is made of.
    band_noise_k: float = 0.5
    #: Hard cap on the band as a share of the gain the follower locks: the watch
    #: may risk at most this fraction of what has already been won.
    band_gain_frac: float = 0.25
    #: Noise bands of locked gain required before a touch is watched at all. Below
    #: it there is no acquired gain worth risking and the touch closes at once.
    min_gain_noise: float = 1.0
    #: Adverse-tick-noise band the three bounds are measured on — the same measure
    #: the zone updaters use, so the watcher and the trailing agree on "ordinary".
    noise_window: int = 20
    noise_std_k: float = 2.0


def watch_band(
    *,
    level_follower: float,
    level_zero: float,
    spread: float,
    min_stop_distance: float,
    noise: float,
    sign: float,
    params: TouchWatchParams,
) -> float:
    """How far beyond the follower the price may sit before the touch is a break.

    Returns ``0.0`` when no watch is allowed — the watcher is disabled, the noise
    is unmeasurable, or the follower locks too little gain to risk any of it — in
    which case the caller must close on the touch.
    """
    if params.band_noise_k <= 0 or noise <= 0:
        return 0.0
    locked = sign * (level_follower - level_zero)
    if locked <= params.min_gain_noise * noise:
        return 0.0

    band = params.band_noise_k * noise
    # Never watch past the order resting at IG: it would fill first, and deeper.
    band = min(band, spread + max(noise, min_stop_distance))
    if params.band_gain_frac > 0:
        band = min(band, params.band_gain_frac * locked)
    return max(band, 0.0)


def touch_closes_position(
    *,
    price: float,
    level_follower: float,
    level_zero: float,
    last_close_out: float,
    spread: float,
    min_stop_distance: float,
    noise: float,
    sign: float,
    params: TouchWatchParams,
) -> bool:
    """``True`` when a touch of the follower must close the position now.

    Called only once the close-out ``price`` has reached the follower. ``False``
    means the touch is a graze inside the band and the position is held for this
    tick — the stop is not moved either way, the watch simply defers the decision.

    ``last_close_out`` is the close-out close of the last **completed** candle
    (bid close for a long, offer close for a short): a graze that is still beyond
    the follower at a minute close is not a graze.
    """
    band = watch_band(
        level_follower=level_follower,
        level_zero=level_zero,
        spread=spread,
        min_stop_distance=min_stop_distance,
        noise=noise,
        sign=sign,
        params=params,
    )
    if band <= 0:
        return True
    if sign * (level_follower - price) > band:
        return True  # deep break — the move is real, close at once
    return sign * (last_close_out - level_follower) <= 0
