# `open_basket` — the 10:00 basket of three unrelated markets

Once a day, from 10:00 Paris, open the three markets whose morning moved the most
and the most cleanly, kept as unrelated as the day allows; protect
them with a far stop; close the **whole book** as soon as it is one quarter-hour
ATR green. If the book is still open at 11:00 the dashboard asks you to decide
for the day, and at 17:30 it alerts you again: nothing is closed by the clock.

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

The basket is a **ranking, not a pass/fail test**: as long as three markets can
be scored, three are opened. Variety comes first, then cost, then score.

| Step       | Rule                                                                                                   |
| ---------- | ------------------------------------------------------------------------------------------------------ |
| Window     | from 10:00 to 11:00 Paris (a late start still opens the day's basket); every epic stays flat otherwise |
| History    | at least 60 one-minute candles since 08:00 Paris                                                       |
| Score      | `ER(morning) × abs(move) / ATR15`, the move measured from the 08:00 open to now                        |
| Side       | the side of the move: a rising morning is bought, a falling one sold                                   |
| Cost       | markets with spread ≤ 0.25 × ATR15 are considered before the others, in each pass                      |
| Exposure   | never two markets sharing an exposure: a currency, an index region, a commodity underlying             |
| Pass 1     | one market per asset class (index, forex, energy, agri, metal), morning \|corr\| ≤ 0.3                 |
| Pass 2     | if still short: a class may repeat, \|corr\| ≤ 0.3                                                     |
| Pass 3     | if still short: \|corr\| ≤ 0.6; above 0.6 a market is never taken                                      |
| Settle     | nothing is chosen while the newest candidate candle is the first of the window (10:00)                 |
| Basket     | the first three that pass the shared open gates, in one pass (`require_flat_book`)                     |
| Once a day | `one_series_per_day`: once anything has been opened today, the selector opens nothing more             |

The exposure is read from the epic ([exposure.py](../../src/core/exposure.py)), not
from the curves. On 2026-10-26 the first live basket was DAX + EUR/JPY + EUR/GBP,
both pairs bought: one bet on the euro, which two hours of one-minute returns had
not caught (the yen and the pound add their own noise). Replayed with the exposure
rule, the same morning gives DAX (index) + GBP/USD (forex) + soybean oil (agri).

The settle wait comes from 2026-10-29: the 30-second selection tick at 10:01:00
ran while the 10:00 candles were still arriving. Only the Nikkei held its
in-window candle, so the ranking had one candidate and the basket opened with one
market; `require_flat_book` and `one_series_per_day` then kept it at one for the
day. The basket is now chosen once a candidate holds the 10:01 candle, a minute
after every market could deliver its 10:00 one (about 10:02).

Each pass logs one `Basket ranking:` line (candidates, how many are cheap, the
basket with each member's class) and one `Basket: skipping …` line per
better-ranked market left out, with the reason.

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

ATR15 is `estimated_timeframe_atr` ([indicators.py](../../src/core/indicators.py)),
shared by the entry and the stop. The buffer only holds today's candles (cleared
and rehydrated each morning), so after a late start there are few quarter-hour
bars at 10:00: on 2026-10-26 the bot came up at 08:26 and every market had six
bars, which the first version (8 true ranges required) read as "no ATR" — no
market was scored. ATR15 is now measured on as few as five bars, and below that
the one-minute ATR × √15 stands in. Both were checked against the full 14-bar
measure on 575 archived mornings: median ratio 1.01 (five bars) and 1.03 (scaled
one-minute ATR).

## Close — `basket`

[`src/exit/zones/basket.py`](../../src/exit/zones/basket.py), selected in zone 1. It
makes the close profile group-aware: one decision per monitor tick for the whole
book, applied to every position whatever its zone.

| When                       | Decision                                                           |
| -------------------------- | ------------------------------------------------------------------ |
| book > target              | close every position (`basket_win`)                                |
| 11:00 or later, still open | one review notification per day; the automatic close keeps running |
| 17:30 or later, still open | one close alert per day; nothing is closed, the target still runs  |
| otherwise                  | hold — no stop ever moves                                          |

- **Book** = the open positions valued at their live close-out price (the spread is
  already paid) + what the day has already banked, so a leg stopped out earlier
  still counts against the other two. Each figure is shaved by 2 % for the gap
  between our euro arithmetic and IG's.
- **Target** = one ATR15 of an average leg in euros: the mean `euro_stop` of the
  open positions ÷ 8 (the `stop_far` multiple). If one position has no `euro_stop`
  the target is unknown and the book only closes by its stops or by hand.
- `CLOSE_ZONEPROFIT=basket` is a plain hold, so a leg running far into profit is
  not trailed out of the basket on its own.

The review reaches the dashboard through the entry-signal channel (desktop
notification, chime, tab badge, a REVIEW card in the signals section — also in
`OPEN_MODE=auto`). Its epic is the worst leg: clicking it opens that chart. You
then close by hand, or let the basket run to its target. The 17:30 close alert
uses the same channel; the basket is then carried overnight unless you close it.

## Limits

- **The day counts as a whole.** The book includes everything closed today and
  `one_series_per_day` counts every opening of the day, manual ones included. Run
  this profile alone.
- **Legs are not equally weighted.** The replay counted each leg in ATR15 units;
  live, each leg is opened at IG's minimum size, so its euro value per ATR15
  differs by market and the book target is their average.
- **The fallbacks are not measured.** The replay only kept days with three cheap
  markets unrelated at 0.3; a basket filled with a dearer or a more related
  market is outside what was tested.
- **A thin morning can still give a smaller basket.** Fewer than three scorable
  markets, or shared gates refusing the spares, and the basket opens with two (or
  one) and does not top up.
- **Not backtestable.** `basket` is in `UNTESTABLE_NAMES`: the replay engine has no
  group pre-pass. The measurement above comes from a dedicated replay script, not
  from `make backtest`.
- **No end-of-day close.** A basket still open at 17:30 is only alerted on (once a
  day; after a restart, within the 15-minute signal TTL of the 11:00 review on the
  same epic). It is carried overnight unless closed by hand, its target or its
  stops; `basket_eod` rows are history from before 2026-10-28.
