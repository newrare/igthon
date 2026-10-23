# `open_basket` — the 10:00 basket of three unrelated markets

Once a day, at 10:00 Paris, open the three markets whose morning moved the most
and the most cleanly, provided they have nothing to do with each other; protect
them with a far stop; close the **whole book** as soon as it is one quarter-hour
ATR green, or at 17:30 at the latest. If the book is still open at 11:00 the
dashboard asks you to decide for the day.

It is a full profile made of three parts, each selected in `.env`:

```bash
OPEN_STRATEGY=open_basket
STOP_STRATEGY=stop_far
CLOSE_ZONESTART=basket
CLOSE_ZONEMARGE=hold
CLOSE_ZONESECURE=hold
CLOSE_ZONEPROFIT=basket
OPEN_MODE=auto      # notify = the bot proposes the three, you open them
```

## Measured — not an edge

Daily-basket replay, 2026-10-23, archive W24–W42 (54 usable days: the bot was not
recording at 10:00 on the others). Unit: one ATR of 15-minute bars per position,
net of the spread.

- No selection rule beats a coin flip on the same three markets: best
  z = +1.61 over 96 variants (4 selections × 3 stops × 8 exits).
- The book goes green at some point on 93 % of days (median 10:09) — but a coin
  flip does too (87 %): that is the random walk, not the selection.
- Closing at the first positive sum loses (−0.42 ATR15 per day): 50 small wins
  (+0.35) against 4 days that never recover (−10.1 on average, worst −19.2).
- The frozen variant (this profile: target +1 ATR15, stop 8 ATR15) made
  +0.14 ATR15 per day, z = +1.53 — not significant.
- With a daily standard deviation of ~7.5 ATR15, ~220 days are needed to confirm
  +1 ATR15 per day, ~900 for +0.5. Only forward trading can tell.

## Entry — `open_basket`

[`src/entry/open_basket.py`](../../src/entry/open_basket.py), a cross-epic ranker
(`cross_epic_selection`), two-sided.

| Step         | Rule                                                                                                                        |
| ------------ | --------------------------------------------------------------------------------------------------------------------------- |
| Window       | only between 10:00 and 10:15 Paris (the leeway covers a late start); every epic stays flat otherwise                        |
| History      | at least 60 one-minute candles since 08:00 Paris                                                                            |
| Cost gate    | spread ≤ 0.25 × ATR15 (ATR of 15-minute bars)                                                                               |
| Score        | `ER(morning) × abs(move) / ATR15`, the move measured from the 08:00 open to now                                             |
| Side         | the side of the move: a rising morning is bought, a falling one sold                                                        |
| Independence | best first, a candidate is dropped when its morning one-minute returns correlate above 0.3 (absolute) with one already kept |
| Basket       | the first three survivors that pass the shared open gates, in one pass (`require_flat_book`)                                |
| Once a day   | `one_series_per_day`: once anything has been opened today, the selector opens nothing more                                  |

The independence test uses the absolute correlation, not the signed redundancy of
the same-bet gate: a hedge (two correlated markets taken opposite ways) is refused
as well, because the idea is three independent bets.

## Stop — `stop_far`

[`src/stops/stop_far.py`](../../src/stops/stop_far.py): the initial stop sits
8 × ATR15 from the entry. It is a catastrophe stop, not a management stop; on the
replay it fired on 8 of 162 legs. The quantity is always IG's minimum deal size —
it does not shrink with the stop — so the euro risk per position (`euro_stop`) is
several times what the usual 2.5 × ATR(1 min) stop carries. Check it on the first
baskets before leaving the profile on.

The live buffer holds ~3 h of one-minute candles, i.e. about 13 quarter-hour bars,
so ATR15 is averaged over what is there (at least 8 true ranges, at most 14).

## Close — `basket`

[`src/exit/zones/basket.py`](../../src/exit/zones/basket.py), selected in zone 1. It
makes the close profile group-aware: one decision per monitor tick for the whole
book, applied to every position whatever its zone.

| When                       | Decision                                                           |
| -------------------------- | ------------------------------------------------------------------ |
| 17:30 Paris or later       | close every position (`basket_eod`)                                |
| book > target              | close every position (`basket_win`)                                |
| 11:00 or later, still open | one review notification per day; the automatic close keeps running |
| otherwise                  | hold — no stop ever moves                                          |

- **Book** = the open positions valued at their live close-out price (the spread is
  already paid) + what the day has already banked, so a leg stopped out earlier
  still counts against the other two. Each figure is shaved by 2 % for the gap
  between our euro arithmetic and IG's.
- **Target** = one ATR15 of an average leg in euros: the mean `euro_stop` of the
  open positions ÷ 8 (the `stop_far` multiple). If one position has no `euro_stop`
  the target is unknown and the book only closes at 17:30.
- `CLOSE_ZONEPROFIT=basket` is a plain hold, so a leg running far into profit is
  not trailed out of the basket on its own.

The review reaches the dashboard through the entry-signal channel (desktop
notification, chime, tab badge, a REVIEW card in the signals section — also in
`OPEN_MODE=auto`). Its epic is the worst leg: clicking it opens that chart. You
then close by hand, or let the basket run to its target or to 17:30.

## Limits

- **The day counts as a whole.** The book includes everything closed today and
  `one_series_per_day` counts every opening of the day, manual ones included. Run
  this profile alone.
- **Legs are not equally weighted.** The replay counted each leg in ATR15 units;
  live, each leg is opened at IG's minimum size, so its euro value per ATR15
  differs by market and the book target is their average.
- **A thin morning gives a smaller basket.** When fewer than three unrelated
  markets pass the gates, the basket opens with two (or one) and does not top up.
- **Not backtestable.** `basket` is in `UNTESTABLE_NAMES`: the replay engine has no
  group pre-pass. The measurement above comes from a dedicated replay script, not
  from `make backtest`.
- A basket still open after a restart past 17:30 is closed on the first monitor
  pass; one left open into the next morning (bot stopped through the evening) is
  only closed at 17:30 that day.
