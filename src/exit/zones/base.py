"""Per-zone stop updaters — the *close* side split by where price sits.

A position's stop is managed differently depending on where the live bid sits
relative to two references frozen at open:

- ``level_zero`` — the break-even level (the entry offer for a BUY);
- ``level_margin`` — break-even plus the epic's noise margin, the smallest move
  that counts as real profit rather than bid/offer churn.

That splits the price axis into three zones, each its own responsibility and its
own :class:`StopUpdater` (so each can be reasoned about and unit-tested alone):

- :class:`StopZone.UNDERWATER` — ``bid <= level_zero`` —
  :class:`~src.exit.zones.underwater.UnderwaterStop`;
- :class:`StopZone.BREAKEVEN_BAND` — ``level_zero < bid <= level_margin`` —
  :class:`~src.exit.zones.breakeven_band.BreakevenBandStop`;
- :class:`StopZone.PROFIT` — ``bid > level_margin`` —
  :class:`~src.exit.zones.trailing_ratchet.TrailingRatchetStop`.

A :class:`~src.exit.base.CloseProfile` composes the three updaters: on each tick
it classifies the zone and delegates to the matching updater, which returns a new
stop level to ratchet to (always higher than the current one) or ``None`` to hold.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from src.core.indicators import adverse_tick_noise
from src.feed.price_buffer import EpicBuffer


class StopZone(Enum):
    """Which price zone the live bid sits in (see module docstring)."""

    UNDERWATER = "underwater"
    BREAKEVEN_BAND = "breakeven_band"
    PROFIT = "profit"


def classify_zone(
    current_bid: float, level_zero: float, level_margin: float
) -> StopZone:
    """Classify the live bid into a :class:`StopZone`.

    ``UNDERWATER`` at or below break-even, ``BREAKEVEN_BAND`` in the noise band
    just above it, ``PROFIT`` once the bid clears the (open-frozen) margin level.
    """
    if current_bid > level_margin:
        return StopZone.PROFIT
    if current_bid > level_zero:
        return StopZone.BREAKEVEN_BAND
    return StopZone.UNDERWATER


@dataclass(slots=True)
class StopContext:
    """Everything a :class:`StopUpdater` needs to decide a new stop this tick.

    Assembled once per tick by the close profile from the live market state and
    the position's persisted levels, so the updaters stay pure and side-effect
    free.
    """

    current_bid: float
    level_open: float
    level_zero: float
    level_margin: float
    level_follower: float
    atr_value: float
    spread: float
    euro_per_point: float
    buf: EpicBuffer


@dataclass(frozen=True)
class BreakevenLockParams:
    """Shaping constants for the support-anchored break-even lock.

    Shared by the margin-zone lock
    (:class:`~src.exit.zones.breakeven_band.BreakevenLockStop`) and the profit-zone
    floor (:class:`~src.exit.zones.trailing_ratchet.TrailingRatchetStop`) so both
    place the stop with the *same* rule. That shared rule is what stitches the two
    zones together: as the bid climbs from the margin band into real profit, the
    (up-only, persisted) follower keeps moving on one continuous curve rather than
    jumping between two unrelated policies — there is no unmanaged gap between zones.
    """

    #: Recent candles whose bids must all have held above break-even (net of
    #: noise) before the lock arms — the persistence gate.
    confirm_window: int = 10
    #: Where the stop is parked, as a fraction of the break-even→swing-low gap
    #: (``0 < f ≤ 1``): ``f=1`` sits at the swing low, smaller values keep a
    #: safety buffer below it. Always clamped to at least one spread above
    #: break-even so a sliver of profit is locked.
    lock_fraction: float = 0.6
    #: Adverse-tick-noise band (same measure as the profit trailing floor) used
    #: to require the move to have cleared break-even beyond ordinary jitter.
    noise_window: int = 20
    noise_std_k: float = 2.0
    noise_mult: float = 2.0


def breakeven_lock_level(ctx: StopContext, params: BreakevenLockParams) -> float | None:
    """Support-anchored break-even lock level, or ``None`` while the move has not held.

    The stop is parked ``lock_fraction`` of the way from break-even up to the
    recent swing low (the lowest bid close in the confirmation window), but only
    once that swing low sits a full adverse-noise band **above** break-even. That
    persistence-and-noise gate is exactly the dashboard's ``bid − noise`` curve
    holding above the break-even line: a move that genuinely holds rather than
    bid/offer churn.

    Anchoring under a real swing low (not a fixed spread offset) is what lets this
    stop sit safely inside the old dead band between break-even and the margin —
    ordinary noise cannot reach a level placed below a low the market has already
    respected, so this does not reintroduce the "everything exits at 0 €" pin.

    Returns the absolute stop level (never below ``level_zero + spread``, so a
    sliver of profit is always locked), or ``None`` when there are too few bids or
    the move has not yet cleared break-even net of noise.
    """
    closes = ctx.buf.bid_closes
    if params.confirm_window < 1 or len(closes) < params.confirm_window:
        return None
    noise = params.noise_mult * adverse_tick_noise(
        closes, params.noise_window, params.noise_std_k
    )
    swing_low = min(closes[-params.confirm_window :])
    # The worst pull-back in the window, net of the noise band, must still be
    # above break-even — otherwise the move has not truly held above it yet.
    if swing_low - noise <= ctx.level_zero:
        return None
    target = ctx.level_zero + params.lock_fraction * (swing_low - ctx.level_zero)
    level = max(target, ctx.level_zero + ctx.spread)
    # Never return a lock at or above the live bid. The close profile's software
    # backstop closes the position as soon as ``bid <= follower`` (see
    # :meth:`~src.exit.close_zoneprofit.CloseZoneProfit.evaluate`), so a lock placed
    # at/above the current bid forces an immediate exit at ~break-even — exactly the
    # "everything exits at 0 €" pin this module exists to avoid. It slips through on
    # a flat/monotone plateau hugging break-even, where ``adverse_tick_noise`` is 0
    # (it only measures down-moves): the noise cushion in the guard above vanishes
    # and the ``level_zero + spread`` floor can rise above a bid sitting just inside
    # a spread of break-even. When there is no room to lock safely below the bid,
    # hold (the previous, lower follower still protects the position).
    if level >= ctx.current_bid:
        return None
    return level


class StopUpdater(ABC):
    """Decides the stop move for one price zone.

    Each updater is a named, independently-selectable strategy for its zone: the
    three zones are chosen separately in ``.env`` (``CLOSE_ZONESTART`` /
    ``CLOSE_ZONEMARGE`` / ``CLOSE_ZONEPROFIT``) and composed by
    :class:`~src.exit.close_zoneprofit.CloseZoneProfit`. Updaters are registered
    per zone in :mod:`src.exit.zones` and built through :func:`build_zone_updater`.
    """

    #: Registry key and per-zone ``CLOSE_ZONE*`` value (snake_case, stable).
    name: str = "base"

    @classmethod
    def from_settings(cls, settings) -> StopUpdater:
        """Build the updater from application settings.

        Updaters carry their shaping constants on their own class, so the default
        is a bare construction; override when an updater must read ``settings``.
        """
        return cls()

    @abstractmethod
    def propose(self, ctx: StopContext) -> float | None:
        """New absolute stop level to ratchet to (higher than the current follower),
        or ``None`` to hold the stop where it is this tick."""


def build_zone_updater(
    registry: dict[str, type[StopUpdater]], name: str, settings
) -> StopUpdater:
    """Build the zone updater registered under ``name`` from ``settings``.

    Raises:
        ValueError: when ``name`` is not registered for this zone.
    """
    cls = registry.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown zone updater: {name!r} (available: {sorted(registry)})"
        )
    return cls.from_settings(settings)
