---
name: breakeven-stop-lock
description: Break-even stop-lock — diagnosis of why positive trades reverse to a loss, and the simulate-first implementation status
metadata:
  type: project
---

Trades that went positive (bid past `level_zero`) then reversed and closed at a loss because the ATR trailing stop ([[ig-stop-units-points-vs-price]]) failed to reach break-even: the breakeven clamp `max(new_stop, level_zero)` in `compute_trailing_stop` is gated behind the ATR ratchet (`new_stop <= level_follower + step → None`), needs `atr>0` (blind at session start/restart), can produce a stop too tight for IG (silently rejected by `_push_stop_to_ig`), and the local `decide_close_reason` still closes on the static `level_loose`.

Fix being introduced as a dedicated ATR-independent rule `compute_breakeven_stop(...)` in `src/services/trading.py`: once `bid` is a safe margin above `level_zero`, lock the stop at `level_zero + buffer·spread`, but only when `bid − target ≥ max(ig_min_stop_distance, margin·spread)` so IG won't reject it.

**Status (2026-06-12):** wired into the **simulator only** (A/B flag `breakeven_lock`, off by default; exposed in `/simulator` UI + route). Live `compute_trailing_stop` is UNCHANGED. A/B on synthetic curves: win-rate ↑ (converts losers to ~flat), P&L ~neutral (caps some winners), sweet spot ~`margin_mult≈3`; effect understated vs live because the sim's ATR is always computable.

**Why:** the user wants positive trades made risk-free on reversal, with a margin so IG doesn't reject/auto-close.
**How to apply:** live wiring still TODO — the user chose to **store IG `minNormalStopOrLimitDistance`** on `Position` (needs a new column + Alembic migration) and feed it as `min_stop_distance`; then call `compute_breakeven_stop` from `_update_trailing_stop` before the ATR trail, and tighten the local close off the effective stop.
