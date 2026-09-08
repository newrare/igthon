"""Per-zone stop updaters — the *close* side split by where price sits.

A position's stop is managed differently depending on where the live **close-out
price** sits relative to three references frozen at open. The close-out price is
the one a close would actually be filled at: the **bid** for a BUY (sell to
close), the **offer** for a SELL (buy to close).

- ``level_zero`` — the break-even level (the entry offer for a BUY, the entry bid
  for a SELL) — the **white** line on the dashboard chart;
- ``level_margin`` — break-even plus the epic's noise margin *in the direction of
  profit* (above break-even for a BUY, below it for a SELL): the smallest move
  that counts as real profit rather than bid/offer churn — the **dotted blue**
  line;
- ``level_profit`` — the *profit trigger*, one further noise margin past the
  margin line (``2 × level_margin − level_zero``, which mirrors itself) — the
  **dotted green** line, past which the stop trails progressively.

That splits the price axis into four zones, each its own responsibility and its
own :class:`StopUpdater` (so each can be reasoned about and unit-tested alone):

- :class:`StopZone.UNDERWATER` — between the live follower (the red stop line)
  and break-even — ``CLOSE_ZONESTART``,
  :mod:`~src.exit.zones.underwater` / :mod:`~src.exit.zones.smartgroup`. Price has
  not cleared break-even, so the whole zone is about reducing the risk carried;
- :class:`StopZone.BREAKEVEN_BAND` — break-even → margin — ``CLOSE_ZONEMARGE``,
  :mod:`~src.exit.zones.breakeven_band`. The delicate band where the gain is still
  the size of ordinary churn;
- :class:`StopZone.SECURE` — margin → profit trigger — ``CLOSE_ZONESECURE``,
  :mod:`~src.exit.zones.secure`. The move has cleared the noise band but is not yet
  a sustained trend: the zone's job is to secure the acquired gain with a single
  deliberate stop, not to trail;
- :class:`StopZone.PROFIT` — past the profit trigger — ``CLOSE_ZONEPROFIT``,
  :class:`~src.exit.zones.trailing_ratchet.TrailingRatchetStop`. Real, sustained
  profit: the stop trails progressively beyond the margin line.

The margin→profit region used to be swallowed by the break-even band (zone 2 ran
all the way to the profit trigger), so no updater was ever selected for it on its
own. It is now :class:`StopZone.SECURE`, selected independently like the others.

A :class:`~src.exit.base.CloseProfile` composes the three updaters: on each tick
it classifies the zone and delegates to the matching updater, which returns a new
stop level to ratchet to (always tighter than the current one) or ``None`` to
hold.

**Direction.** Every zone works identically for a BUY and a SELL — only the sign
of "forward" changes. Rather than duplicating each updater, :class:`StopContext`
carries the position's ``direction`` and exposes the direction-aware primitives
the updaters reason with (:meth:`~StopContext.gain`,
:meth:`~StopContext.beyond`, :meth:`~StopContext.offset`) plus a *sign-normalised*
view of the price series (:attr:`~StopContext.favourable_closes`), in which
"rising" always means "moving into profit" whichever side the position is on. An
updater that computes in that normalised space converts its answer back with
:meth:`~StopContext.absolute`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from src.core.indicators import adverse_tick_noise, retracement_depth
from src.core.reading import PositionReading
from src.feed.price_buffer import Candle, EpicBuffer

#: ``reason_close`` of a provisional row whose order never confirmed at IG. Not a
#: trade, so it never enters the day's accounting (mirrors the dashboard).
_PHANTOM_CLOSE_REASON = "never_opened"
#: ``reason_close`` written by the under-water flat cut. Its losses are charged to
#: a dedicated budget rather than to the day's result (see :class:`DayBook`).
FLAT_CUT_CLOSE_REASON = "flat_cut"


@dataclass(frozen=True, slots=True)
class DayBook:
    """What the day has banked so far, and what the flat cut has already spent.

    The one input an exit rule cannot read off a curve. It exists because a rule
    that closes a stagnating position at a loss (see
    :class:`~src.exit.zones.adaptive.AdaptiveReadStop`) is only defensible while
    the day can absorb it — and "can absorb it" is an arithmetic claim about the
    **whole day**, not about the position in front of the updater.

    The two figures are kept apart on purpose. Charging the cuts against a single
    net total leaks: each cut lowers the net, the next budget is a share of the
    lowered net, and the series ``0.35 × net`` telescopes until the cuts have eaten
    **the entire** banked gain (measured live on 2026-08-28: five cuts totalling
    −51.52 € against the +38.60 € that was banked when the first one fired). So
    :attr:`realized_euro` is the day's result *without* the flat cuts — a base that
    the rule cannot erode — and :attr:`flat_cut_euro` is the running bill charged
    against a fixed share of it.

    Advanced with :meth:`after_close` as positions close, so several positions
    closing **inside the same monitor pass** each see the budget the previous one
    left, instead of all spending the same opening snapshot.

    Attributes:
        realized_euro: Net euros banked today on closed positions, **excluding**
            the flat cuts and the ``never_opened`` phantoms. Can be negative.
        flat_cut_euro: Euros already spent today on flat cuts, as a positive
            number.
    """

    realized_euro: float = 0.0
    flat_cut_euro: float = 0.0

    def after_close(self, reason: str | None, euro: float | None) -> DayBook:
        """The book once a position has closed for ``reason`` at ``euro``.

        The exit domain owns which reasons spend which budget, so a caller only
        has to hand over the close it just recorded.
        """
        value = float(euro or 0.0)
        if reason == _PHANTOM_CLOSE_REASON:
            return self
        if reason == FLAT_CUT_CLOSE_REASON:
            # A flat cut in profit costs nothing; only the losses are billed.
            return DayBook(self.realized_euro, self.flat_cut_euro + max(0.0, -value))
        return DayBook(self.realized_euro + value, self.flat_cut_euro)

    @classmethod
    def from_closes(cls, closes) -> DayBook:
        """Build the book from ``(reason_close, euro)`` pairs of today's closes."""
        book = cls()
        for reason, euro in closes:
            book = book.after_close(reason, euro)
        return book

    def flat_cut_budget(self, ratio: float) -> float:
        """Euros the flat cut may still spend today: ``ratio × gains − already spent``.

        A hard cumulative cap, not a per-decision test: whatever the sequence of
        cuts, together they can never cost more than ``ratio`` of what the day
        banked without them. Never negative.
        """
        return max(0.0, ratio * max(0.0, self.realized_euro) - self.flat_cut_euro)


class StopZone(Enum):
    """Which price zone the live close-out price sits in (see module docstring)."""

    UNDERWATER = "underwater"
    BREAKEVEN_BAND = "breakeven_band"
    SECURE = "secure"
    PROFIT = "profit"


def classify_zone(
    current_price: float,
    level_zero: float,
    level_margin: float,
    level_profit: float,
    sign: float = 1.0,
) -> StopZone:
    """Classify the live close-out price into a :class:`StopZone`.

    Three open-frozen references split the price axis into the four zones (see the
    module docstring): break-even (``level_zero``), the margin line
    (``level_margin``) and the profit trigger (``level_profit`` — one further noise
    margin past the margin line):

    - ``UNDERWATER`` while price has not cleared break-even;
    - ``BREAKEVEN_BAND`` from break-even up to the margin line;
    - ``SECURE`` from the margin line up to the profit trigger — the move has
      cleared the noise band, so the acquired gain is secured with one deliberate
      stop while the trend is not yet established;
    - ``PROFIT`` once price clears the profit trigger — real, sustained profit,
      where the stop trails progressively beyond the margin line.

    ``sign`` is ``+1`` for a BUY (profit is up) and ``−1`` for a SELL (profit is
    down); the comparisons are written on the signed distance so both sides
    classify with one rule. Each boundary belongs to the *lower* zone: price
    exactly on the margin line is still in the break-even band.
    """
    if sign * (current_price - level_profit) > 0:
        return StopZone.PROFIT
    if sign * (current_price - level_margin) > 0:
        return StopZone.SECURE
    if sign * (current_price - level_zero) > 0:
        return StopZone.BREAKEVEN_BAND
    return StopZone.UNDERWATER


@dataclass(slots=True)
class StopContext:
    """Everything a :class:`StopUpdater` needs to decide a new stop this tick.

    Assembled once per tick by the close profile from the live market state and
    the position's persisted levels, so the updaters stay pure and side-effect
    free.

    All price fields are in **close-out terms** — the price a close would be
    filled at, which is the bid for a BUY and the offer for a SELL. The
    direction-aware helpers below are what let one updater serve both sides.
    """

    #: Live close-out price: the bid for a BUY, the offer for a SELL.
    current_price: float
    level_open: float
    level_zero: float
    level_margin: float
    level_follower: float
    atr_value: float
    spread: float
    euro_per_point: float
    buf: EpicBuffer
    #: Trade side — ``"BUY"`` or ``"SELL"``. Drives :attr:`sign` and with it every
    #: direction-aware helper, so an updater never tests the side itself.
    direction: str = "BUY"
    #: IG's minimum stop distance for this epic, in price units (0 when unknown).
    #: Read by the updaters that place a stop relative to the live price, so they
    #: never propose a level closer than the broker would accept.
    min_stop_distance: float = 0.0
    #: The **unsliced** rolling buffer, pre-open candles included, when the caller
    #: has one (the close profile bounds :attr:`buf` to the position's open, see
    #: ``_buffer_since``). Only *amplitude* measures read this — a volatility-like
    #: quantity wants history and must be warm from the first tick, exactly the
    #: reason ``CloseZoneProfit`` computes ATR on the full buffer. Every *level*
    #: gate keeps reading :attr:`buf`: a swing low or a "held past break-even"
    #: test must never see a pre-entry excursion. ``None`` falls back to
    #: :attr:`buf`.
    full_buf: EpicBuffer | None = None
    #: Pre-resolved group decision for this position, set only by the portfolio
    #: pre-pass of a group-aware zone-1 updater (``smartgroup``): the absolute
    #: stop level this position should tighten to this tick, or ``None`` to hold.
    #: The group maths runs upstream (once per monitor tick across the whole book,
    #: see :mod:`src.exit.zones.smartgroup`) so the updater itself stays pure.
    group_tighten: float | None = None
    #: Instant the position opened (UTC), when the caller knows it. Only used to
    #: build :meth:`reading` — :attr:`buf` is already bounded to it by the close
    #: profile, but a :class:`~src.core.reading.PositionReading` slices the
    #: *unbounded* buffer itself so it can expose both views at once.
    opened_at: datetime | None = None
    #: The day's running result and flat-cut bill (see :class:`DayBook`), empty
    #: when the caller does not track it. Read by
    #: :class:`~src.exit.zones.adaptive.AdaptiveReadStop` to decide whether closing
    #: a stagnating position at a loss is affordable *today*.
    day_book: DayBook = field(default_factory=DayBook)
    #: Lazily-built market reading, memoised for the tick (see :meth:`reading`).
    _reading: PositionReading | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def reading(self) -> PositionReading:
        """The position's :class:`~src.core.reading.PositionReading` for this tick.

        The shared reading facade (``src/core/reading.py``) — the same one the
        dashboard's *read this market* view shows — expressed in this position's
        own profit terms: its ``curve`` measures only what happened **since the
        open**, its ``market`` keeps the full history for the amplitude measures,
        and every gain field is counted from break-even.

        Built from the unsliced buffer (:attr:`full_buf`) plus :attr:`opened_at`,
        so the reading does its own slicing and both views are correct; it falls
        back to :attr:`buf` when no unsliced buffer was provided. Memoised, so an
        updater may read fifty fields off it without rebuilding anything.
        """
        if self._reading is None:
            self._reading = PositionReading(
                self.full_buf if self.full_buf is not None else self.buf,
                direction=self.direction,
                level_zero=self.level_zero,
                level_open=self.level_open or None,
                current_price=self.current_price,
                opened_at=self.opened_at,
                level_margin=self.level_margin,
                level_follower=self.level_follower or None,
                euro_per_point=self.euro_per_point,
            )
        return self._reading

    # ---- direction-aware primitives -------------------------------------------
    # Every zone rule is the same trade for a long and a short; only the sign of
    # "forward" flips. These helpers express the rules in profit terms so no
    # updater has to branch on the side (the branching that left shorts unmanaged).

    @property
    def sign(self) -> float:
        """``+1`` when profit is up (BUY), ``−1`` when profit is down (SELL)."""
        return -1.0 if self.direction == "SELL" else 1.0

    def gain(self, level: float) -> float:
        """Profit distance of ``level`` from break-even — positive = in profit."""
        return self.sign * (level - self.level_zero)

    def beyond(self, level: float, reference: float) -> bool:
        """True when ``level`` sits strictly further into profit than ``reference``.

        The direction-free form of "above" for a long / "below" for a short — used
        for every up-only ratchet guard and every "is the stop safely inside the
        market" test.
        """
        return self.sign * (level - reference) > 0

    def offset(self, reference: float, distance: float) -> float:
        """``reference`` moved ``distance`` **towards profit** (negative = adverse)."""
        return reference + self.sign * distance

    def favourable(self, level: float) -> float:
        """``level`` in sign-normalised space (see :attr:`favourable_closes`)."""
        return self.sign * level

    def absolute(self, favourable_level: float) -> float:
        """Convert a sign-normalised level back to a real price level."""
        return self.sign * favourable_level

    @property
    def closes(self) -> list[float]:
        """Recorded close-out prices — bid closes for a BUY, offer closes for a SELL."""
        return self.buf.offer_closes if self.sign < 0 else self.buf.bid_closes

    @property
    def favourable_closes(self) -> list[float]:
        """:attr:`closes` sign-normalised so *rising always means going into profit*.

        Multiplying by :attr:`sign` maps a short's falling offer onto a rising
        series, so a rule written for a long ("the swing low held above break-even",
        "the last two ticks rose") reads correctly for both sides with no branch.
        Levels compared against it must be mapped with :meth:`favourable`, and a
        level computed in this space mapped back with :meth:`absolute`.
        """
        return [self.sign * c for c in self.closes]

    def adverse_noise(self, window: int, std_k: float) -> float:
        """Adverse tick-noise band of the close-out series, in price units.

        The adverse direction is a down-move for a long and an up-move for a
        short; measuring on :attr:`favourable_closes` makes
        :func:`~src.core.indicators.adverse_tick_noise` (which only counts
        down-steps) the right measure on both sides.
        """
        return adverse_tick_noise(self.favourable_closes, window, std_k)

    def retracement(self, window: int) -> float:
        """Deepest multi-candle give-back of the close-out series, in price units.

        The companion of :meth:`adverse_noise`: that one measures a single adverse
        step, this one the worst *cumulative* pull-back over the window (see
        :func:`~src.core.indicators.retracement_depth`). Sign-normalised like
        :attr:`favourable_closes`, so it is the give-back this position's own side
        has to survive — a down-move for a long, an up-move for a short.

        Read from :attr:`full_buf` when the caller provided it: this is an
        amplitude measure, so it wants the pre-open history (a position open for
        five minutes has shown no pull-back of its own, and would otherwise be
        managed as if the market had none).
        """
        buf = self.full_buf if self.full_buf is not None else self.buf
        closes = buf.offer_closes if self.sign < 0 else buf.bid_closes
        return retracement_depth([self.sign * c for c in closes], window)

    def bar_close(self, candle: Candle) -> float:
        """The candle's close-out close — bid close (BUY) / offer close (SELL)."""
        return candle.offer_close if self.sign < 0 else candle.bid_close

    def bar_open(self, candle: Candle) -> float:
        """The candle's close-out open (bid open for a BUY, offer open for a SELL)."""
        return candle.offer_open if self.sign < 0 else candle.bid_open

    def bar_adverse(self, candle: Candle) -> float:
        """The candle's worst close-out print — its bid low (BUY) / offer high (SELL).

        The level a stop must sit beyond to survive the wicks the market really
        traded through on this side.
        """
        return candle.offer_high if self.sign < 0 else candle.bid_low

    def price_range(self) -> float:
        """Range of the close-out price over the buffer — its vertical scale.

        The server-side analogue of the dashboard chart's ``hi − lo``, measured on
        the series this side actually closes at.
        """
        candles = self.buf.candles
        if not candles:
            return 0.0
        if self.sign < 0:
            return max(c.offer_high for c in candles) - min(
                c.offer_low for c in candles
            )
        return max(c.bid_high for c in candles) - min(c.bid_low for c in candles)


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

    #: Recent candles whose close-out prices must all have held past break-even
    #: (net of noise) before the lock arms — the persistence gate.
    confirm_window: int = 10
    #: Where the stop is parked, as a fraction of the break-even→swing-low gap
    #: (``0 < f ≤ 1``): ``f=1`` sits at the swing low, smaller values keep a
    #: safety buffer behind it. Always clamped to at least one spread into
    #: profit so a sliver of gain is locked.
    lock_fraction: float = 0.6
    #: Adverse-tick-noise band (same measure as the profit trailing floor) used
    #: to require the move to have cleared break-even beyond ordinary jitter.
    noise_window: int = 20
    noise_std_k: float = 2.0
    noise_mult: float = 2.0


def breakeven_lock_level(ctx: StopContext, params: BreakevenLockParams) -> float | None:
    """Support-anchored break-even lock level, or ``None`` while the move has not held.

    The stop is parked ``lock_fraction`` of the way from break-even towards the
    recent swing low (the least-profitable close in the confirmation window), but
    only once that swing low sits a full adverse-noise band **past** break-even.
    That persistence-and-noise gate is exactly the dashboard's ``price − noise``
    curve holding beyond the break-even line: a move that genuinely holds rather
    than bid/offer churn.

    Anchoring behind a real swing low (not a fixed spread offset) is what lets this
    stop sit safely inside the old dead band between break-even and the margin —
    ordinary noise cannot reach a level placed beyond a low the market has already
    respected, so this does not reintroduce the "everything exits at 0 €" pin.

    The whole computation runs in :attr:`~StopContext.favourable_closes` space, so
    "swing low", "above break-even" and "at least one spread of profit" all read
    the same for a long and a short; the answer is mapped back to a real price
    level at the end.

    Returns the absolute stop level (never less than one spread into profit, so a
    sliver of gain is always locked), or ``None`` when there are too few ticks or
    the move has not yet cleared break-even net of noise.
    """
    closes = ctx.favourable_closes
    if params.confirm_window < 1 or len(closes) < params.confirm_window:
        return None
    noise = params.noise_mult * adverse_tick_noise(
        closes, params.noise_window, params.noise_std_k
    )
    zero = ctx.favourable(ctx.level_zero)
    swing_low = min(closes[-params.confirm_window :])
    # The worst pull-back in the window, net of the noise band, must still be
    # past break-even — otherwise the move has not truly held beyond it yet.
    if swing_low - noise <= zero:
        return None
    target = zero + params.lock_fraction * (swing_low - zero)
    level = max(target, zero + ctx.spread)
    # Never return a lock at or beyond the live price. The close profile's software
    # backstop closes the position as soon as price reaches the follower (see
    # :meth:`~src.exit.close_zoneprofit.CloseZoneProfit.evaluate`), so a lock placed
    # at/past the current price forces an immediate exit at ~break-even — exactly the
    # "everything exits at 0 €" pin this module exists to avoid. It slips through on
    # a flat/monotone plateau hugging break-even, where ``adverse_tick_noise`` is 0
    # (it only measures adverse steps): the noise cushion in the guard above vanishes
    # and the one-spread floor can pass a price sitting just inside a spread of
    # break-even. When there is no room to lock safely behind price, hold (the
    # previous, safer follower still protects the position).
    if level >= ctx.favourable(ctx.current_price):
        return None
    return ctx.absolute(level)


@dataclass(frozen=True, slots=True)
class ZoneDecision:
    """What a zone updater decides on one tick — move the stop, close, or hold.

    Most updaters only ever move a stop, which is why
    :meth:`StopUpdater.propose` (a bare level, or ``None`` to hold) remains the
    primitive every zone is written against. Some rules, though, conclude that the
    *position* is what should go, not the stop: a reading of the market since the
    open can show that the move the trade was built on has stopped happening (see
    :class:`~src.exit.zones.adaptive.AdaptiveReadStop`). Expressing that as a stop
    parked just behind the live price would work only by accident — it goes through
    the touch watcher, fills a spread away, and reads in the history as a stop-out
    rather than as a decision. So a zone updater may return this instead.

    Exactly one of the two fields is meaningful: ``close_reason`` set means "close
    the position now, for this reason" and ``stop_level`` is ignored; otherwise
    ``stop_level`` is the level to ratchet to, ``None`` to hold.

    Attributes:
        stop_level: New absolute stop level to ratchet to, or ``None`` to hold.
        close_reason: Close reason (persisted on the position as
            ``reason_close``) when the zone wants the position closed now.
    """

    stop_level: float | None = None
    close_reason: str | None = None

    @property
    def is_close(self) -> bool:
        """True when this decision closes the position rather than moving a stop."""
        return self.close_reason is not None


class StopUpdater(ABC):
    """Decides the stop move for one price zone.

    Each updater is a named, independently-selectable strategy for its zone: the
    four zones are chosen separately in ``.env`` (``CLOSE_ZONESTART`` /
    ``CLOSE_ZONEMARGE`` / ``CLOSE_ZONESECURE`` / ``CLOSE_ZONEPROFIT``) and composed by
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
        """New absolute stop level to ratchet to, or ``None`` to hold this tick.

        The level must be strictly tighter than the current follower in the
        position's own direction (:meth:`StopContext.beyond`) — higher for a BUY,
        lower for a SELL. The composer applies what is returned verbatim.
        """

    def decide(self, ctx: StopContext) -> ZoneDecision:
        """Full decision for this tick — a stop move, a close, or a hold.

        The composer (:class:`~src.exit.close_zoneprofit.CloseZoneProfit`) calls
        **this**, not :meth:`propose`. The default wraps :meth:`propose`, so every
        stop-only updater keeps working unchanged; an updater that can also decide
        to close the position overrides it (see
        :class:`~src.exit.zones.adaptive.AdaptiveReadStop`) and keeps
        :meth:`propose` as the stop-only view of the same decision.
        """
        return ZoneDecision(stop_level=self.propose(ctx))


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
