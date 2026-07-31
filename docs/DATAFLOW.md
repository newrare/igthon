# Data flow — the time and data contract

This document is the **single source of truth** for two questions that are easy
to get wrong and were previously answered nowhere:

1. **What price data does the bot actually hold**, where, and for how long?
1. **When does a decision run**, and how stale is the price it sees?

Everything below is the current behaviour of the code, not an intention. When you
change the feed, the buffer or the monitor cadence, change this document in the
same commit.

______________________________________________________________________

## 1. Vocabulary — the "tick" trap

The word **tick** is used in three different senses across the codebase. This is
the single biggest source of confusion, so it is pinned down here:

| Term used in code                                                    | What it actually is                               |
| -------------------------------------------------------------------- | ------------------------------------------------- |
| "tick" in `favourable_closes`, `adverse_tick_noise`, `confirm_ticks` | a **1-minute candle close**. Never a market tick. |
| "monitor tick"                                                       | one execution of `_monitor_positions`             |
| a Lightstreamer "frame"                                              | one `onItemUpdate` callback from IG               |

**No market-tick data exists anywhere in this application.** The finest
granularity held, stored or reasoned about is the 1-minute candle. Any parameter
named `*_ticks` or `*_tick_*` is counted in minutes.

______________________________________________________________________

## 2. Ingestion — what IG sends and what we keep

```
IG Lightstreamer  CHART:{epic}:1MINUTE  (MERGE subscription)
      │
      │  many frames per second per epic:
      │    · intra-candle frames  (CONS_END != "1")  → DROPPED
      │    · consolidated candle  (CONS_END == "1")  → KEPT
      ▼
_CandleListener.onItemUpdate            (feed/streaming.py)
      │  one Candle per epic per minute, arriving at ~T+0s of the next minute
      ▼
IGStreamingClient.on_candle
      ├──▶ PriceBuffer.append_candles    (in-memory, hot path)
      ├──▶ CandleStore.save              (DB `candle` table, durable)
      └──▶ registered candle listeners   (see §5 — triggers evaluation)
```

Consequences worth stating explicitly, because they are counter-intuitive:

- The market updates **continuously**; the bot discards the overwhelming majority
  of what it receives. The one-minute cadence is a **subscription and filter
  choice**, not a limitation imposed by IG.
- Nothing waits for "the market to move". A candle for minute `T` becomes visible
  at `T+60s`, whatever happened inside the minute.
- The REST `/prices/{epic}/MINUTE` endpoint is a **seed/fallback only** (first
  start, or a gap in the feed). It returns the same 1-minute candles and consumes
  the IG historical-data allowance, which is why the feed is preferred.

______________________________________________________________________

## 3. The two stores — why both exist

|          | `PriceBuffer` (memory)                                                                     | `candle` table (DB)                             |
| -------- | ------------------------------------------------------------------------------------------ | ----------------------------------------------- |
| Content  | 1-minute candles, OHLC bid+offer                                                           | identical                                       |
| Capacity | rolling `BUFFER_MAX_CANDLES` (default 200 ≈ 3 h 20) per epic                               | full retention window (`CANDLE_RETENTION_DAYS`) |
| Lifetime | lost on restart, cleared daily                                                             | durable, archived to CSV then pruned            |
| Access   | **synchronous**, in-process                                                                | `async`, one round-trip                         |
| Read by  | every decision (`EntryStrategy.evaluate`, `CloseProfile.evaluate`, all stop/zone updaters) | charts, buffer rehydration, backtest dumps      |

They are not redundant: the buffer is a **cache of the tail**, sized for
synchronous access.

**Why the buffer must exist.** All decision code is synchronous and takes an
`EpicBuffer` directly — `CloseProfile.evaluate(position, current_bid, buf)`,
`EntryStrategy.evaluate(epic, buf)`. Each cycle recomputes ATR, regressions,
swing lows and noise bands for ~40 epics plus every open position. Reading the DB
there would put async I/O inside the decision path. `EpicBuffer` is also the
interface the **backtest** feeds (`backtest/simulator.py`), which is what makes
the simulator run production code rather than a reimplementation.

**Why the DB must exist.** Restart without a cold start (`_rehydrate_buffer`
reloads today's candles, so indicators are valid immediately and no IG
historical-data allowance is spent); whole-day charts, which exceed the buffer
window; and the CSV dumps the backtester replays.

### Buffer capacity is a real constraint, not a detail

`BUFFER_MAX_CANDLES` bounds how much history **any** strategy can see, whatever
its own parameters say. A strategy declaring a lookback longer than the buffer is
silently evaluated on a truncated window. The scheduler therefore logs a warning
at startup when the selected strategy's `warmup` exceeds buffer capacity — if you
see it, raise `BUFFER_MAX_CANDLES` rather than lowering the strategy's parameter.

______________________________________________________________________

## 4. What each decision layer reads

| Consumer                                                    | Reads                                        | Source                            |
| ----------------------------------------------------------- | -------------------------------------------- | --------------------------------- |
| Zone classification (`classify_zone`)                       | last **bid close** only                      | `buf.last.bid_close`              |
| Software backstop (`reason="stop"`)                         | last **bid close** only                      | same                              |
| ATR                                                         | high / low / close                           | `indicators.atr`                  |
| Swing-low anchors, Donchian bands, channel position         | **high / low**                               | `buf.candles`                     |
| Noise bands (`adverse_tick_noise`)                          | candle **closes**                            | `buf.bid_closes` / `offer_closes` |
| Confirmation streaks (`confirm_ticks`, `favourable_closes`) | candle **closes**                            | `buf`                             |
| Dashboard chart                                             | **closes only** (`bid_close`, `offer_close`) | DB, else buffer                   |

Two asymmetries follow from this table, both **known and currently accepted**:

- **Zones are classified on the close, stops are anchored on high/low.** A wick
  that crosses the profit trigger and returns never changes zone, yet the same
  wick's low can serve as a stop anchor. See §7.
- **The chart shows less than the bot sees.** High/low are stored and used, never
  drawn. What you see is a line of one close per minute — hence "readings", not
  candles.

All levels compared against price are expressed in **close-out terms**: the bid
for a long, the offer (bid + spread) for a short (`_close_out_price`). Zone
updaters read a buffer **sliced to the position's open instant**
(`_buffer_since`), so pre-entry history can never arm a lock.

______________________________________________________________________

## 5. The decision clock

Position management is driven by **the arrival of data**, with a scheduled
heartbeat as a safety net:

```
candle for epic X arrives  ──▶  scheduler.on_candle(epic, candle)
                                      │  debounce MONITOR_DEBOUNCE_SECONDS
                                      │  (one pass per wave, not per epic)
                                      ▼
cron heartbeat (30 s) ─────────▶  _monitor_positions()   ← single-flight
                                      │
                                      ├─ phase 1: resolve bid + buffer per position
                                      ├─ group pre-pass (whole book, atomic)
                                      └─ phase 2: CloseProfile.evaluate() per position
```

- **Event-driven trigger** — the debounce coalesces the burst of candles that
  arrive together for ~40 epics into a single pass, and yields the whole-book
  snapshot the group pre-pass requires (§6).
- **Cron heartbeat** — still registered, so monitoring keeps running when
  streaming is disabled or the feed goes silent.
- **Single-flight** — a run already in progress makes any new trigger a no-op;
  triggers never queue up behind each other.
- **Pause is respected** — an event-driven pass is skipped whenever the
  `monitor_positions` job is in manual (paused) mode, exactly like the cron.

### Latency budget

| Step                                              | Delay                                    |
| ------------------------------------------------- | ---------------------------------------- |
| Level crossed in-market → candle closes           | 0 – 60 s                                 |
| Candle closed → visible in buffer                 | ~0 s (feed callback)                     |
| Buffer updated → evaluation runs                  | `MONITOR_DEBOUNCE_SECONDS` (default 2 s) |
| **Total, event-driven**                           | **~2 – 62 s**                            |
| Worst case if the feed is silent (heartbeat only) | + up to 30 s                             |

The dominant term is the one-minute candle, not the scheduler. Reducing latency
further means going sub-minute (§7), not tightening the cron.

Two other cadences exist and are unrelated to trading decisions: position **sync**
against IG every 20 s (which is how a broker-side stop-out is discovered), and the
dashboard poll every 2 s (display only).

______________________________________________________________________

## 6. Load-bearing invariants

Break any of these and the failure is silent. They are listed here because none
of them is obvious from the call site.

1. **The `CONS_END == "1"` filter is not an optimisation.** Both write paths
   deduplicate on a **strictly increasing** timestamp (`PriceBuffer.append_candles`,
   `CandleStore.save`), and every frame of a given minute carries the *same* `UTM`.
   Letting intra-candle frames through would therefore accept the **first partial
   frame** of each minute and **silently reject the consolidated candle** — the
   whole history would degrade to prices sampled at the start of each minute, with
   no error raised anywhere.
1. **The group pre-pass needs the whole book in one pass.** `smartgroup` asserts an
   arithmetic property of the *sum* of open positions, so it is skipped entirely
   for a cycle in which any open position could not be priced. This is why
   evaluation is debounced into one whole-book pass instead of run per epic.
1. **Stops never loosen.** The ratchet invariant is enforced in
   `TradingService.manage_position`, independently of what a zone updater returned.
1. **Open-frozen references.** `level_zero` and `level_margin` are computed once at
   open and persisted; the profit trigger is derived (`2 × margin − zero`). They
   must not be recomputed per cycle, or the dead band drifts as ATR breathes.
1. **Buffer capacity bounds every lookback.** See §3.

______________________________________________________________________

## 7. Known gaps and open decisions

Documented deliberately, so they are choices rather than accidents:

- **Sub-minute reactivity is not implemented.** It would require a `LivePrice`
  channel (latest bid/offer per epic, in memory, no history) fed by the
  intra-candle frames currently dropped — kept strictly **separate** from the
  candle buffer, both to preserve invariant §6.1 and because every calibrated
  window (ATR, noise band, confirmation streaks) is measured in minutes and would
  collapse if fed frames. Nothing sub-minute should ever be persisted.
- **Zone classification on close vs. on live price** (§4) is unresolved.
  Classifying on a live price would align zones with the high/low the stops
  already use, but needs hysteresis on the way out (enter on crossing, leave only
  after a close beyond the level) so a wick cannot flip a position between zones.
- **The API-snapshot bid fallback cannot reach the zones.** When an epic has no
  buffered candle, `_monitor_positions` fetches a live bid from
  `GET /markets/{epic}`, but `manage_position` short-circuits on `buf is None`:
  the value only ever reaches the legacy long-only `check_and_close` path, never
  `CloseProfile.evaluate`. It is the one place a genuinely real-time price enters
  the application, and it is nearly inert.
