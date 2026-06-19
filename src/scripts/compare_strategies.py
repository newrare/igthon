"""Compare the five candidate strategies across every curve profile.

Runs each strategy over the SAME set of synthetic days per profile (identical
markets → fair comparison) and prints a per-profile results matrix, then an
overall ranking. The live long-only trend follower is included as a baseline
(on independently drawn curves of the same profile, via ``run_simulation``).

Usage:
    python -m src.scripts.compare_strategies [--days N] [--epics N] [--seed N]
"""

from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta
from types import SimpleNamespace

from src.backtest.curve_generator import PROFILES, generate_curve
from src.backtest.simulator import SimulationConfig, run_simulation
from src.backtest.strategies import BacktestEngine, BacktestResult, all_strategies

_BASE_DAY = datetime(2024, 1, 1)

# Live-strategy defaults (mirrors src/config.py) for the baseline run.
_LIVE_SETTINGS = SimpleNamespace(
    strategy_name="trend_follower",
    strategy_donchian_channel=20,
    strategy_donchian_stop_atr_k=2.5,
    strategy_efficiency_period=30,
    strategy_min_efficiency=0.45,
    strategy_lookback_points=20,
    strategy_sma_fast=5,
    strategy_sma_slow=20,
    strategy_roc_period=10,
    strategy_min_r2=0.70,
    strategy_min_score=0.75,
    strategy_max_spread_ratio=0.0015,
    strategy_stop_multiplier=2.5,
    strategy_target_multiplier=4.0,
    strategy_tactic="spread",
    strategy_max_positions=6,
    strategy_hour_start=9,
    strategy_hour_end=16,
    strategy_hour_close=17,
    strategy_euro_loss=4000.0,
    strategy_daily_win_target=300.0,
    strategy_daily_loss_limit=-500.0,
    strategy_compensate_loose=False,
    strategy_close_target="follower",
    strategy_max_trades_day=50,
    strategy_min_win_rate=0.40,
    strategy_atr_period=14,
    strategy_atr_k_pre=2.5,
    strategy_atr_k_post=1.5,
    strategy_trailing_step_ratio=0.3,
)


def _build_curves(profile: str, days: int, epics: int, master_seed: int):
    """One identical curve set per profile, reused across all strategies."""
    master = random.Random(master_seed)
    curves_by_day = []
    for day in range(days):
        seeds = [master.randrange(2**32) for _ in range(epics)]
        curves_by_day.append(
            [
                generate_curve(
                    profile,
                    seed=seeds[e],
                    num_candles=600,
                    base_price=8000.0,
                    day=_BASE_DAY + timedelta(days=day),
                )
                for e in range(epics)
            ]
        )
    return curves_by_day


def _run_baseline(profile: str, days: int, epics: int, master_seed: int) -> dict:
    cfg = SimulationConfig(
        target_trades=10_000,
        max_days=days,
        epics_per_day=epics,
        candles_per_day=600,
        profile=profile,
        seed=master_seed,
        base_price=8000.0,
        euro_per_point=1.0,
    )
    res = run_simulation(_LIVE_SETTINGS, cfg)
    s = res.summary()
    pnls = [t.euro or 0.0 for t in res.trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gl = -sum(losses)
    return {
        "strategy": "LIVE-TrendFollower*",
        "trades": s["trades"],
        "win_rate": s["win_rate"],
        "total_pnl": s["total_pnl"],
        "profit_factor": round(sum(wins) / gl, 2) if gl else 0.0,
        "expectancy": round(s["total_pnl"] / s["trades"], 2) if s["trades"] else 0.0,
        "max_drawdown": s["max_drawdown"],
        "longs": s["trades"],
        "shorts": 0,
    }


def _fmt_row(s: dict) -> str:
    return (
        f"  {s['strategy']:<22} {s['trades']:>5}  "
        f"{s['win_rate'] * 100:>5.1f}%  {s['total_pnl']:>10.2f}  "
        f"{s['profit_factor']:>5.2f}  {s['expectancy']:>7.2f}  "
        f"{s['max_drawdown']:>9.2f}  {s['longs']:>4}/{s['shorts']:<4}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--epics", type=int, default=3)
    parser.add_argument("--seed", type=int, default=12345)
    args = parser.parse_args()

    header = (
        f"  {'strategy':<22} {'trades':>5}  {'win%':>5}  {'pnl(€)':>10}  "
        f"{'PF':>5}  {'exp(€)':>7}  {'maxDD(€)':>9}  L/S"
    )

    overall: dict[str, float] = {}
    for profile in PROFILES:
        curves_by_day = _build_curves(profile, args.days, args.epics, args.seed)
        print(f"\n=== profile: {profile}  ({args.days} days × {args.epics} epics) ===")
        print(header)

        rows = []
        for strat in all_strategies():
            engine = BacktestEngine(strat)
            result = BacktestResult(strategy=strat.name)
            for day, curves in enumerate(curves_by_day):
                engine.run_day(day, curves, result)
                result.days += 1
            s = result.summary()
            rows.append(s)
            overall[strat.name] = overall.get(strat.name, 0.0) + s["total_pnl"]

        rows.append(_run_baseline(profile, args.days, args.epics, args.seed))
        for s in sorted(rows, key=lambda r: r["total_pnl"], reverse=True):
            print(_fmt_row(s))

    print("\n=== OVERALL total P&L summed across all profiles ===")
    for name, pnl in sorted(overall.items(), key=lambda kv: kv[1], reverse=True):
        print(f"  {name:<22} {pnl:>12.2f} €")
    print("\n  * LIVE baseline runs on independently drawn curves of the same")
    print("    profile (own seed path); the 5 candidates share identical curves.")


if __name__ == "__main__":
    main()
