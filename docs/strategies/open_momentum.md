# `open_momentum` — the start of an up leg, into a book that has stopped moving

Entry strategy (`OPEN_STRATEGY`, `src/entry/open_momentum.py`). Cross-epic
**ranker** (`cross_epic_selection = True`), **long-only**, one open per pass, and
a hard brake on the state of the book.

```env
OPEN_STRATEGY=open_momentum
```

## Idea

Two rules, and like `open_progressive` they only make sense together.

**Open on MOMENTUM = START inside REGIME = TREND_UP.** The two columns the
dashboard shows side by side (`src/core/reading.py`) answer different questions,
and the entry needs both to agree:

| Reading                                   | Question it answers              | What it cannot say                                                                    |
| ----------------------------------------- | -------------------------------- | ------------------------------------------------------------------------------------- |
| `regime` (`TREND_UP`)                     | is this market going *anywhere*? | how old the move is — it reads the same on the first candle of a rise and on its last |
| `move_phase` (`START`) + `move_direction` | am I *early* in the current leg? | whether the surrounding market is chopping                                            |

`TREND_UP` alone buys established rises, which is exactly where the give-back
lives. `START` alone buys every fresh 9/21 crossing, and most crossings are chop —
which is precisely what the regime filters out. Together they read: *a market that
is going somewhere, caught early*.

The leg's own direction is checked separately (`move_direction` must be `UP`): a
fresh **down** crossing inside a market whose 30-minute regime still reads
`TREND_UP` is a leg turning against the trend, not the start of a rise.

**One bet at a time, and only into a quiet book.** This is not a "hold N
positions" ranker — the limit is the *state* of the book, not a count:

- one open **per selection pass** (`open_cooldown_minutes = 1`, which caps the
  pass at a single open and spaces two opens by a minute), so a basket is never
  fired at once and every open is decided against the book as it actually is;
- **nothing is opened while any open position is still in a move**
  (`require_range_book`). Only when *every* open position's epic reads `RANGE` —
  contained chop, the move it was opened for is over — may a new momentum trade go
  on beside it.

A position still riding its move must not be diluted by a second bet. A position
whose market has gone back to a range is no longer using the attention, and a
fresh start elsewhere is worth more than sitting on it.

## `require_range_book` — what counts as "quiet"

The brake is enforced by the scheduler (`_still_moving_positions`), not by the
strategy: `evaluate()` sees one price curve and never the open positions. Each
open position's epic is read on its **live buffer** over `book_regime_window`
candles (30, the same window the entry's own regime gate uses), and only `RANGE`
lets the pass continue:

| Regime of an open position's epic   | Effect     | Why                                                          |
| ----------------------------------- | ---------- | ------------------------------------------------------------ |
| `RANGE`                             | allows     | contained chop — the move is over, the money is idle         |
| `TREND_UP` / `TREND_DOWN`           | **blocks** | the position is still riding a move; a second bet dilutes it |
| `VOLATILE`                          | **blocks** | big swings going nowhere — the worst market to add risk into |
| `UNKNOWN` (no buffer, < 10 candles) | **blocks** | the book cannot be *shown* to be quiet                       |

Blocking on the unknown is deliberate, and it is the opposite of the choice made
by `block_open_while_alive` (which lets an unjudgeable row through so an idle bot
never freezes). Here the brake *is* the position-sizing rule, so an unreadable
book is a refusal.

Because the count target is dropped (`wallet_bounded = True`), the book grows one
position at a time for exactly as long as the range condition keeps holding; the
wallet (available funds minus the reserve) is the only other limit, and
`concurrent_positions = 1` is just the fallback cap for a pass where the balance
cannot be read.

### Compared with the two other book brakes

| Brake                    | Used by         | Opens while a position is open?                       |
| ------------------------ | --------------- | ----------------------------------------------------- |
| `require_flat_book`      | `open_five`     | never — the next series waits for an empty book       |
| `block_open_while_alive` | `open_steady`   | yes, unless a position has already **locked** a gain  |
| `require_range_book`     | `open_momentum` | yes, but only while **every** open epic is in a range |

## Ranking

Every gate is absolute — an epic either shows a fresh up leg inside an up regime
or it does not — so the score only **orders** candidates that have already
qualified. It is the efficiency ratio of the context window (0-1, 1 = a straight
line): among two markets starting a leg, prefer the one whose path travels rather
than saws.

For the same reason both participation gates are off (`min_participation_ratio = 0.0`, `min_participation_count = 0`): this is not a tournament where the least-bad
of a small pool wins, so a thin universe is no reason to refuse a qualifying
setup.

## What it does not decide

Nothing about the exit. The entry emits an `EntryIntent` (direction only); the
protective stop at open comes from `STOP_STRATEGY` and every subsequent decision
from the four `CLOSE_ZONE*` selectors. The same-day re-open policy
(`ALLOW_SAME_DAY_REOPEN`) and the recovery revert (`ALLOW_RECOVERY_REVERT`) are
global and unchanged.

## Parameters

Constants of `OpenMomentum` (`src/entry/open_momentum.py`) — tune them there, not
in `.env`:

| Parameter               | Default | Role                                                          |
| ----------------------- | ------- | ------------------------------------------------------------- |
| `context_minutes`       | `30`    | window the regime, the give-back and the ranking ER read      |
| `warmup_minutes`        | `60`    | candles required before an epic is scored                     |
| `book_regime_window`    | `30`    | window the `require_range_book` brake reads on each open epic |
| `open_cooldown_minutes` | `1`     | one open per pass, minimum spacing between two opens          |
| `require_range_book`    | `True`  | open only into a book entirely in `RANGE`                     |
| `wallet_bounded`        | `True`  | no fixed position count — the brake and the wallet decide     |
| `emits_shorts`          | `False` | long-only: a fresh down leg emits nothing                     |

The thresholds behind the two readings themselves (`trend_efficiency = 0.35`,
`MIN_MOVE_GAP_ATR`, `LATE_MOVE_HOLD`, `MIN_REGIME_MINUTES`) live in
`src/core/reading.py` and are shared with the dashboard, so what the screen shows
and what this entry gates on can never disagree.

## Tests

- `tests/test_open_momentum.py` — the selection contract, the setup it opens, and
  each refusal on its own (established rise, fresh crossing inside chop, fresh
  down leg, flat market), plus the score and the warm-up.
- `tests/test_scheduler.py::TestRequireRangeBook` — the book brake: an empty book
  runs the tournament, one trending open epic blocks it (even among ranges), a
  book entirely in a range re-opens it, an unreadable epic blocks, and a strategy
  that does not declare the flag is unaffected.
