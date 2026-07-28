"""Zone 2 updaters — price is in the noise band just past break-even.

The close-out price sits past break-even (``level_zero``) but has not yet cleared
the margin level (``level_zero`` plus one noise margin in the profit direction).
This is the delicate region: parking the stop a hair past break-even here is
exactly where ordinary bid/offer noise alone would trigger it for ~zero profit
(the "everything exits at 0 €" pathology that a naive break-even pin caused live).

Four updaters live here, selected by ``CLOSE_ZONEMARGE``:

- :class:`BreakevenBandStop` (``hold``) — leave the initial stop untouched; the
  stop only ever moves once price clears the margin level (zone 3);
- :class:`BreakevenLockStop` (``breakeven_lock``) — pull the stop in behind the
  recent swing low **once the move has genuinely held past break-even** (a
  persistence-and-noise gate), so a normal pull-back cannot immediately knock it
  out. This secures a hair of profit early on a fast reversal without hugging
  price.
- :class:`BreakevenSafeStop` (``breakeven_safe``) — a single, **one-shot** lock.
  After **two consecutive favourable ticks** confirm the push — and provided the
  most recent bar is not itself an adverse bar — it moves the stop once to the
  *nearer* of two references past break-even — a fixed euro gain (``+10 €``)
  and a fixed fraction of the recent price range (``+3 %`` of the chart scale) —
  then holds that stop for the rest of the margin zone. Taking the nearer of the
  two keeps the stop closer to break-even (the safer, harder-to-knock level), and
  which reference wins varies by epic (euro-per-point and price range differ).
- :class:`BreakevenHalfStop` (``breakeven_half``) — a single, **one-shot** lock at
  a fixed **support line a quarter of the way** from break-even to the margin
  level (25 % of the break-even→margin gap, so close to break-even). It arms once
  price has posted **two consecutive favourable ticks past the margin line** — a
  push that has genuinely cleared the noise band — then moves the stop to that
  support line once and holds it for the rest of the margin zone.

All four are direction-agnostic: they reason in profit terms through
:class:`~src.exit.zones.base.StopContext` (``gain`` / ``beyond`` / ``offset`` and
the sign-normalised ``favourable_closes``), so "rising", "above break-even" and
"raise the stop" mean *towards profit* — up for a BUY, down for a SELL.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.exit.zones.base import (
    BreakevenLockParams,
    StopContext,
    StopUpdater,
    breakeven_lock_level,
)


@dataclass
class BreakevenBandStop(StopUpdater):
    """Hold the stop while price is in the noise band just past break-even."""

    name = "hold"

    def propose(self, ctx: StopContext) -> float | None:
        return None


@dataclass
class BreakevenLockStop(StopUpdater):
    """Lock the stop under the recent swing low once the move holds above break-even.

    The old design pinned the stop a fixed ``spread`` above break-even and only
    fired once the bid ran a noise-sized gap clear of it. That gap
    (``level_zero + 3 × spread`` on a quiet tape) sat **above** the margin level
    whenever the noise margin was thin, so the bid left the band into the profit
    zone before the gate ever opened — a firing region that was empty by
    construction, and the lock never engaged (observed live on CS.D.EURCAD.CFD.IP).

    This replaces the fixed gap with two changes:

    - **trigger** — a persistence-and-noise gate: the recent swing low, net of the
      adverse-tick-noise band, must sit above break-even (see
      :func:`~src.exit.zones.base.breakeven_lock_level`). This is meetable *inside*
      the band: it only asks that the move has genuinely held above break-even,
      not that the bid has run three spreads clear;
    - **level** — the stop is anchored under that real swing low rather than at a
      fixed spread offset, so ordinary noise cannot reach it.

    The stop is a *lock*, not a trailing. Once the bid clears the margin level the
    position enters zone 3 and the profit trailing
    (:class:`~src.exit.zones.trailing_ratchet.TrailingRatchetStop`) takes over,
    using the *same* lock as its floor (via ``breakeven_lock_level``) so the
    follower keeps climbing on one continuous curve across the two zones.
    """

    name = "breakeven_lock"

    #: Shared shaping of the support-anchored lock (see ``breakeven_lock_level``).
    lock: BreakevenLockParams = field(default_factory=BreakevenLockParams)

    def propose(self, ctx: StopContext) -> float | None:
        target = breakeven_lock_level(ctx, self.lock)
        if target is None:
            return None

        # Tighten-only. The composer applies the returned level verbatim (no guard
        # of its own), so never returning a level short of the current follower is
        # this updater's own responsibility — e.g. a follower already pushed by the
        # profit zone on an earlier excursion must not be given back.
        if ctx.level_follower > 0 and not ctx.beyond(target, ctx.level_follower):
            return None

        return target


@dataclass
class BreakevenSafeStop(StopUpdater):
    """One-shot break-even lock at the nearer of the ``+10 €`` / ``+3 %`` references.

    A single, deliberate move for the margin zone — the sibling
    :class:`BreakevenLockStop` anchors behind a real swing low, this one locks a
    small fixed gain and then leaves the stop alone. On every tick price is in the
    band (past break-even, not yet past the profit trigger):

    - it **moves the stop only once**. Once the follower has been pulled past
      break-even — by this updater's own earlier move, or by the profit zone on a
      prior excursion — the margin-zone stop is done and every later tick holds it;
    - the move arms only after :attr:`confirm_ticks` consecutive favourable ticks,
      so ordinary churn does not trigger it;
    - it also refuses to move into a reversal: the streak is measured
      close-to-close, so a bar can lift the streak while its own body has already
      turned against us (it gapped our way, then faded — its close still beats the
      prior close). Locking a stop just behind such a bar is what let the next,
      adverse tick cross the freshly-moved stop with no room to spare (observed
      live). When the most recent bar is itself adverse the updater waits for the
      push to resume;
    - the level it locks is the reference **nearest break-even** of two (see
      :meth:`_lock_level`): a fixed euro gain (:attr:`gain_target_eur`, the
      ``+10 €`` line) and a fixed fraction (:attr:`range_pct`, the ``+3 %`` line) of
      the recent price range. Taking the nearer of the two parks the stop closer to
      break-even — the safer, harder-to-knock level — and which reference wins
      varies by epic, since euro-per-point and the price range differ between them;
    - it only fires when that level sits **strictly behind the live price**. The
      profile's software backstop closes as soon as price reaches the follower, so
      a lock at/past the price would force an immediate exit at ~break-even; when
      the chosen level has not yet been cleared the updater holds and waits.

    The move is **tighten-only**: a follower already further into profit than the
    chosen level is never given back.
    """

    name = "breakeven_safe"

    #: Euro gain of the ``+10 €`` reference (``level_zero`` moved ``gain / €·point``
    #: towards profit).
    gain_target_eur: float = 10.0
    #: Fraction of the recent price range for the ``+3 %`` reference
    #: (``level_zero`` moved ``range_pct × range`` towards profit). ``0.03`` = 3 %
    #: of the chart scale.
    range_pct: float = 0.03
    #: Consecutive favourable livestream ticks that arm the single move.
    confirm_ticks: int = 2

    def propose(self, ctx: StopContext) -> float | None:
        # Single move: once the follower sits past break-even (this updater's own
        # earlier move, or the profit zone on a prior excursion) the margin-zone
        # stop is set for good — hold it for the rest of the zone.
        if ctx.gain(ctx.level_follower) > 0:
            return None

        # Arm only after a short streak of favourable ticks confirms the push.
        if not self._rising_streak(ctx.favourable_closes, self.confirm_ticks):
            return None

        # Do not move into a reversal. The streak above is measured close-to-close,
        # so the last bar can lift it while its own body has already turned against
        # us (it gapped our way then faded — its close still beats the prior close).
        # Locking a stop just behind such a bar is what let the next, adverse tick
        # cross the freshly-moved stop with no room to spare. When the most recent
        # bar is itself adverse, hold and wait for the push to resume.
        if self._last_bar_adverse(ctx):
            return None

        target = self._lock_level(ctx)
        if target is None:
            return None

        # The lock must sit strictly behind the live price: the profile's software
        # backstop closes as soon as price reaches the follower, so a level at/past
        # the price forces an immediate exit at ~break-even. Hold until price clears
        # it.
        if not ctx.beyond(ctx.current_price, target):
            return None

        return self._tighten_only(ctx, target)

    def _lock_level(self, ctx: StopContext) -> float | None:
        """The nearer of the ``+10 €`` and ``+3 %`` levels past break-even.

        Each reference is included only when it is computable (the euro level needs
        a positive euro-per-point; the range level needs a non-empty buffer). The
        one closest to break-even is returned — the safer stop — or ``None`` when
        neither can be computed.
        """
        candidates: list[float] = []
        if ctx.euro_per_point > 0:
            candidates.append(
                ctx.offset(ctx.level_zero, self.gain_target_eur / ctx.euro_per_point)
            )
        price_range = ctx.price_range()
        if price_range > 0:
            candidates.append(ctx.offset(ctx.level_zero, self.range_pct * price_range))
        if not candidates:
            return None
        return min(candidates, key=ctx.gain)

    @staticmethod
    def _rising_streak(closes: list[float], streak: int) -> bool:
        """True when the last ``streak`` tick-to-tick moves all went into profit.

        Fed the sign-normalised :attr:`~src.exit.zones.base.StopContext.
        favourable_closes`, so "up" means "our way" on either side.
        """
        if streak < 1 or len(closes) < streak + 1:
            return False
        recent = closes[-(streak + 1) :]
        return all(b > a for a, b in zip(recent, recent[1:]))

    @staticmethod
    def _last_bar_adverse(ctx: StopContext) -> bool:
        """True when the most recent completed bar closed against the position.

        A close-to-close favourable streak can be satisfied by a bar that gapped our
        way and then faded within the bar: its close still beats the prior close, yet
        the bar itself pushed back. That is the reversal the streak cannot see, so it
        is treated as "not yet safe to move the stop".
        """
        last = ctx.buf.last
        if last is None:
            return False
        return ctx.sign * (ctx.bar_close(last) - ctx.bar_open(last)) < 0

    @staticmethod
    def _tighten_only(ctx: StopContext, target: float) -> float | None:
        # Tighten-only. The composer applies the returned level verbatim (no guard
        # of its own), so never returning a level short of the current follower is
        # this updater's own responsibility.
        if ctx.level_follower > 0 and not ctx.beyond(target, ctx.level_follower):
            return None
        return target


@dataclass
class BreakevenHalfStop(StopUpdater):
    """One-shot lock at a support line a quarter of the way across the margin band.

    A single, deliberate move for the margin zone — a sibling of
    :class:`BreakevenSafeStop` with a simpler, level-fixed rule:

    - the **support line** is parked at a fixed fraction (:attr:`support_fraction`,
      ``0.25``) of the break-even→margin gap, i.e. a quarter of the way from
      break-even (``level_zero``) towards the margin level (``level_margin``). It
      sits close to break-even — the safer, harder-to-knock level — while still
      locking a sliver of the noise band. Because ``level_margin`` is itself frozen
      on the profit side of break-even, the same interpolation lands above
      break-even for a BUY and below it for a SELL;
    - it arms once price has posted :attr:`confirm_ticks` **consecutive favourable
      ticks whose closes all sit past the margin line**. Requiring the confirming
      ticks to clear the margin (not merely move our way) is the persistence gate:
      the push has run clear of the noise band before the stop moves, so ordinary
      bid/offer churn inside the band cannot arm it. The excursion past the margin
      is read from the recorded close-out prices, so the move still applies on the
      pull-back tick that brings price back into the band (where this updater runs);
    - it **moves the stop only once**. Once the follower has been pulled past
      break-even — by this updater's own earlier move, or by the profit zone on a
      prior excursion — the margin-zone stop is done and every later tick holds it;
    - it only fires when the support line sits **strictly behind the live price**.
      The profile's software backstop closes as soon as price reaches the follower,
      so a lock at/past the price would force an immediate exit at ~break-even;
      when price has pulled back past the support line the updater holds and waits.

    The move is **tighten-only**: a follower already further into profit than the
    support line is never given back.
    """

    name = "breakeven_half"

    #: Where the support line sits, as a fraction of the break-even→margin gap
    #: (``0 < f < 1``). ``0.25`` parks it at the first quarter, close to break-even.
    support_fraction: float = 0.25
    #: Consecutive favourable ticks past the margin line that arm the single move.
    confirm_ticks: int = 2

    def propose(self, ctx: StopContext) -> float | None:
        # Single move: once the follower sits past break-even (this updater's own
        # earlier move, or the profit zone on a prior excursion) the margin-zone
        # stop is set for good — hold it for the rest of the zone.
        if ctx.gain(ctx.level_follower) > 0:
            return None

        # The band must exist (margin frozen on the profit side) for the support
        # fraction to mean anything.
        if ctx.gain(ctx.level_margin) <= 0:
            return None

        # Arm only after a streak of favourable ticks that has cleared the margin.
        if not self._confirmed_past_margin(
            ctx.favourable_closes, ctx.favourable(ctx.level_margin), self.confirm_ticks
        ):
            return None

        support = ctx.level_zero + self.support_fraction * (
            ctx.level_margin - ctx.level_zero
        )

        # The lock must sit strictly behind the live price: the profile's software
        # backstop closes as soon as price reaches the follower, so a level at/past
        # the price forces an immediate exit at ~break-even. Hold until price clears
        # it.
        if not ctx.beyond(ctx.current_price, support):
            return None

        # Tighten-only. The composer applies the returned level verbatim (no guard
        # of its own), so never returning a level short of the current follower is
        # this updater's own responsibility.
        if ctx.level_follower > 0 and not ctx.beyond(support, ctx.level_follower):
            return None

        return support

    @staticmethod
    def _confirmed_past_margin(
        closes: list[float], level_margin: float, ticks: int
    ) -> bool:
        """True when the buffer holds ``ticks`` consecutive favourable ticks past
        the margin.

        Scans the sign-normalised closes (:attr:`~src.exit.zones.base.StopContext.
        favourable_closes`, where rising always means "into profit") for any window
        of ``ticks + 1`` strictly increasing values whose ``ticks`` resulting closes
        all sit past ``level_margin`` (itself sign-normalised). Existence anywhere in
        the buffer (not just the last ticks) is what lets the lock still fire on the
        pull-back tick that brings price back into the band.
        """
        if ticks < 1 or len(closes) < ticks + 1:
            return False
        for i in range(len(closes) - ticks):
            window = closes[i : i + ticks + 1]
            rising = all(b > a for a, b in zip(window, window[1:]))
            past = all(c > level_margin for c in window[1:])
            if rising and past:
                return True
        return False
