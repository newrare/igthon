"""Zone 2 updaters — the bid is in the noise band just above break-even.

The bid sits above break-even (``level_zero``) but has not yet cleared the margin
level (``level_zero + noise_margin``). This is the delicate region: parking the
stop a hair above break-even here is exactly where ordinary bid/offer noise alone
would trigger it for ~zero profit (the "everything exits at 0 €" pathology that a
naive break-even pin caused live).

Four updaters live here, selected by ``CLOSE_ZONEMARGE``:

- :class:`BreakevenBandStop` (``hold``) — leave the initial stop untouched; the
  stop only ever moves once the bid clears the margin level (zone 3);
- :class:`BreakevenLockStop` (``breakeven_lock``) — pull the stop up under the
  recent swing low **once the move has genuinely held above break-even** (a
  persistence-and-noise gate), so a normal pull-back cannot immediately knock it
  out. This secures a hair of profit early on a fast reversal without hugging the
  bid.
- :class:`BreakevenSafeStop` (``breakeven_safe``) — a single, **one-shot** lock.
  After **two consecutive rising ticks** confirm the push — and provided the most
  recent bar is not itself a down bar — it raises the stop once to the *lower* of
  two references above break-even — a fixed euro gain (``+10 €``)
  and a fixed fraction of the recent price range (``+3 %`` of the chart scale) —
  then holds that stop for the rest of the margin zone. Taking the lower of the
  two keeps the stop closer to break-even (the safer, harder-to-knock level), and
  which reference wins varies by epic (euro-per-point and price range differ).
- :class:`BreakevenHalfStop` (``breakeven_half``) — a single, **one-shot** lock at
  a fixed **support line a quarter of the way** from break-even up to the margin
  level (25 % of the break-even→margin gap, so close to break-even). It arms once
  the bid has posted **two consecutive rising ticks above the margin line** — a
  push that has genuinely cleared the noise band — then raises the stop to that
  support line once and holds it for the rest of the margin zone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.exit.zones.base import (
    BreakevenLockParams,
    StopContext,
    StopUpdater,
    breakeven_lock_level,
)
from src.feed.price_buffer import EpicBuffer


@dataclass
class BreakevenBandStop(StopUpdater):
    """Hold the stop while the bid is in the noise band above break-even."""

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

        # Up-only. The composer applies the returned level verbatim (no guard of
        # its own), so never returning a level at or below the current follower is
        # this updater's own responsibility — e.g. a follower already pushed by the
        # profit zone on an earlier excursion must not be pulled back down.
        if ctx.level_follower > 0 and target <= ctx.level_follower:
            return None

        return target


@dataclass
class BreakevenSafeStop(StopUpdater):
    """One-shot break-even lock at the lower of the ``+10 €`` / ``+3 %`` references.

    A single, deliberate raise for the margin zone — the sibling
    :class:`BreakevenLockStop` anchors under a real swing low, this one locks a
    small fixed gain and then leaves the stop alone. On every tick the bid is in
    the band (``level_zero < bid <= level_margin``):

    - it **raises the stop only once**. Once the follower has been lifted above
      break-even — by this updater's own earlier raise, or by the profit zone on a
      prior excursion — the margin-zone stop is done and every later tick holds it;
    - the raise arms only after :attr:`confirm_ticks` consecutive rising ticks, so
      ordinary churn does not trigger it;
    - it also refuses to raise into a reversal: the rising streak is measured
      close-to-close, so a bar can lift the streak while its own body has already
      turned down (it gapped up, then sold off — its close still beats the prior
      close). Locking a stop just under such a bar is what let the next, lower tick
      cross the freshly-raised stop with no room to spare (observed live). When the
      most recent bar is itself bearish the updater waits for the push to resume;
    - the level it locks is the **lower** of two references above break-even (see
      :meth:`_lock_level`): a fixed euro gain (:attr:`gain_target_eur`, the
      ``+10 €`` line) and a fixed fraction (:attr:`range_pct`, the ``+3 %`` line) of
      the recent price range. Taking the lower of the two parks the stop closer to
      break-even — the safer, harder-to-knock level — and which reference is lower
      varies by epic, since euro-per-point and the price range differ between them;
    - it only fires when that level sits **strictly below the live bid**. The
      profile's software backstop closes as soon as ``bid <= follower``, so a lock
      at/above the bid would force an immediate exit at ~break-even; when the
      chosen level has not yet cleared the bid the updater holds and waits.

    The raise is **up-only**: a follower already above the chosen level is never
    pulled back down.
    """

    name = "breakeven_safe"

    #: Euro gain of the ``+10 €`` reference (``level_zero + gain / €·point``).
    gain_target_eur: float = 10.0
    #: Fraction of the recent price range for the ``+3 %`` reference
    #: (``level_zero + range_pct × range``). ``0.03`` = 3 % of the chart scale.
    range_pct: float = 0.03
    #: Consecutive rising livestream ticks that arm the single raise.
    confirm_ticks: int = 2

    def propose(self, ctx: StopContext) -> float | None:
        # Single raise: once the follower sits above break-even (this updater's own
        # earlier raise, or the profit zone on a prior excursion) the margin-zone
        # stop is set for good — hold it for the rest of the zone.
        if ctx.level_follower > ctx.level_zero:
            return None

        # Arm only after a short streak of rising ticks confirms the push.
        if not self._rising_streak(ctx.buf.bid_closes, self.confirm_ticks):
            return None

        # Do not raise into a reversal. The streak above is measured close-to-close,
        # so the last bar can lift it while its own body has already turned down (it
        # gapped up then sold off — its close still beats the prior close). Locking a
        # stop just under such a bar is what let the next, lower tick cross the
        # freshly-raised stop with no room to spare. When the most recent bar is
        # itself bearish, hold and wait for the push to resume.
        if self._last_bar_bearish(ctx.buf):
            return None

        target = self._lock_level(ctx)
        if target is None:
            return None

        # The lock must sit strictly below the live bid: the profile's software
        # backstop closes as soon as ``bid <= follower``, so a level at/above the
        # bid forces an immediate exit at ~break-even. Hold until the bid clears it.
        if target >= ctx.current_bid:
            return None

        return self._raise_only(ctx, target)

    def _lock_level(self, ctx: StopContext) -> float | None:
        """Lower of the ``+10 €`` and ``+3 %`` levels above break-even.

        Each reference is included only when it is computable (the euro level needs
        a positive euro-per-point; the range level needs a non-empty buffer). The
        lower of the available references is returned — the closer-to-break-even,
        safer stop — or ``None`` when neither can be computed.
        """
        candidates: list[float] = []
        if ctx.euro_per_point > 0:
            candidates.append(
                ctx.level_zero + self.gain_target_eur / ctx.euro_per_point
            )
        price_range = self._recent_range(ctx.buf)
        if price_range > 0:
            candidates.append(ctx.level_zero + self.range_pct * price_range)
        if not candidates:
            return None
        return min(candidates)

    @staticmethod
    def _recent_range(buf: EpicBuffer) -> float:
        """Recent bid price range — the server-side analogue of the dashboard's
        vertical scale (``hi − lo`` of the plotted window). ``range_pct`` of this is
        the ``+3 %`` reference line."""
        candles = buf.candles
        if not candles:
            return 0.0
        return max(c.bid_high for c in candles) - min(c.bid_low for c in candles)

    @staticmethod
    def _rising_streak(closes: list[float], streak: int) -> bool:
        """True when the last ``streak`` tick-to-tick moves are all strictly up."""
        if streak < 1 or len(closes) < streak + 1:
            return False
        recent = closes[-(streak + 1) :]
        return all(b > a for a, b in zip(recent, recent[1:]))

    @staticmethod
    def _last_bar_bearish(buf: EpicBuffer) -> bool:
        """True when the most recent completed bar closed below its open.

        A close-to-close rising streak can be satisfied by a bar that gapped up and
        then faded within the bar: its close still beats the prior close, yet the bar
        itself is a down (selling-pressure) bar. That is the reversal the streak
        cannot see, so it is treated as "not yet safe to raise".
        """
        last = buf.last
        return last is not None and last.bid_close < last.bid_open

    @staticmethod
    def _raise_only(ctx: StopContext, target: float) -> float | None:
        # Up-only. The composer applies the returned level verbatim (no guard of
        # its own), so never returning a level at or below the current follower is
        # this updater's own responsibility.
        if ctx.level_follower > 0 and target <= ctx.level_follower:
            return None
        return target


@dataclass
class BreakevenHalfStop(StopUpdater):
    """One-shot lock at a support line a quarter of the way up the margin band.

    A single, deliberate raise for the margin zone — a sibling of
    :class:`BreakevenSafeStop` with a simpler, level-fixed rule:

    - the **support line** is parked at a fixed fraction (:attr:`support_fraction`,
      ``0.25``) of the break-even→margin gap, i.e. a quarter of the way up from
      break-even (``level_zero``) towards the margin level (``level_margin``). It
      sits close to break-even — the safer, harder-to-knock level — while still
      locking a sliver of the noise band;
    - it arms once the bid has posted :attr:`confirm_ticks` **consecutive rising
      ticks whose closes all sit above the margin line**. Requiring the confirming
      ticks to clear the margin (not merely rise) is the persistence gate: the push
      has run clear of the noise band before the stop moves, so ordinary bid/offer
      churn inside the band cannot arm it. The excursion above the margin is read
      from the recorded bid closes, so the raise still applies on the pull-back
      tick that brings the bid back down into the band (where this updater runs);
    - it **raises the stop only once**. Once the follower has been lifted above
      break-even — by this updater's own earlier raise, or by the profit zone on a
      prior excursion — the margin-zone stop is done and every later tick holds it;
    - it only fires when the support line sits **strictly below the live bid**. The
      profile's software backstop closes as soon as ``bid <= follower``, so a lock
      at/above the bid would force an immediate exit at ~break-even; when the bid
      has pulled back below the support line the updater holds and waits.

    The raise is **up-only**: a follower already above the support line is never
    pulled back down.
    """

    name = "breakeven_half"

    #: Where the support line sits, as a fraction of the break-even→margin gap
    #: (``0 < f < 1``). ``0.25`` parks it at the first quarter, close to break-even.
    support_fraction: float = 0.25
    #: Consecutive rising ticks above the margin line that arm the single raise.
    confirm_ticks: int = 2

    def propose(self, ctx: StopContext) -> float | None:
        # Single raise: once the follower sits above break-even (this updater's own
        # earlier raise, or the profit zone on a prior excursion) the margin-zone
        # stop is set for good — hold it for the rest of the zone.
        if ctx.level_follower > ctx.level_zero:
            return None

        # The band must exist for the support fraction to mean anything.
        if ctx.level_margin <= ctx.level_zero:
            return None

        # Arm only after a streak of rising ticks that has cleared the margin line.
        if not self._rising_above_margin(
            ctx.buf.bid_closes, ctx.level_margin, self.confirm_ticks
        ):
            return None

        support = ctx.level_zero + self.support_fraction * (
            ctx.level_margin - ctx.level_zero
        )

        # The lock must sit strictly below the live bid: the profile's software
        # backstop closes as soon as ``bid <= follower``, so a level at/above the
        # bid forces an immediate exit at ~break-even. Hold until the bid clears it.
        if support >= ctx.current_bid:
            return None

        # Up-only. The composer applies the returned level verbatim (no guard of its
        # own), so never returning a level at or below the current follower is this
        # updater's own responsibility.
        if ctx.level_follower > 0 and support <= ctx.level_follower:
            return None

        return support

    @staticmethod
    def _rising_above_margin(
        closes: list[float], level_margin: float, ticks: int
    ) -> bool:
        """True when the buffer holds ``ticks`` consecutive up-ticks above the margin.

        Scans the recorded bid closes for any window of ``ticks + 1`` strictly
        increasing closes whose ``ticks`` resulting up-tick closes all sit above
        ``level_margin``. Existence anywhere in the buffer (not just the last ticks)
        is what lets the lock still fire on the pull-back tick that brings the bid
        back into the band.
        """
        if ticks < 1 or len(closes) < ticks + 1:
            return False
        for i in range(len(closes) - ticks):
            window = closes[i : i + ticks + 1]
            rising = all(b > a for a, b in zip(window, window[1:]))
            above = all(c > level_margin for c in window[1:])
            if rising and above:
                return True
        return False
