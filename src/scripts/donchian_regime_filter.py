"""Quantify Donchian-Breakout's trade frequency and the effect of a regime gate.

Answers two questions:
1. How many positions per day (per epic) does Donchian open in each regime?
2. Does gating entries by the Kaufman Efficiency Ratio (only trade trending
   markets) kill the spread-churn in ranging regimes while keeping the wins?

Usage:
    python -m src.scripts.donchian_regime_filter [--days N] [--epics N] [--seed N]
"""

from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta

from src.services.curve_generator import PROFILES, generate_curve
from src.services.strategies import BacktestEngine, BacktestResult, DonchianBreakout

_BASE_DAY = datetime(2024, 1, 1)

# (efficiency_period, min_efficiency) — 0 disables the gate (baseline).
_FILTERS = [(0, 0.0), (30, 0.30), (30, 0.45), (30, 0.60)]


def _build_curves(profile: str, days: int, epics: int, master_seed: int):
    master = random.Random(master_seed)
    out = []
    for day in range(days):
        seeds = [master.randrange(2**32) for _ in range(epics)]
        out.append(
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
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--epics", type=int, default=3)
    parser.add_argument("--seed", type=int, default=12345)
    args = parser.parse_args()
    slots = args.days * args.epics  # epic-days

    for profile in PROFILES:
        curves = _build_curves(profile, args.days, args.epics, args.seed)
        print(f"\n=== {profile}  ({args.days}d × {args.epics} epics) ===")
        print(
            f"  {'filter':<18} {'trades':>6} {'/epic-day':>9} "
            f"{'win%':>6} {'pnl(€)':>11} {'exp(€)':>8} {'PF':>6}"
        )
        for period, min_er in _FILTERS:
            strat = DonchianBreakout()
            strat.efficiency_period = period
            strat.min_efficiency = min_er
            engine = BacktestEngine(strat)
            result = BacktestResult(strategy=strat.name)
            for day, day_curves in enumerate(curves):
                engine.run_day(day, day_curves, result)
                result.days += 1
            s = result.summary()
            label = "none" if period == 0 else f"ER>={min_er:.2f}"
            per_ed = s["trades"] / slots
            print(
                f"  {label:<18} {s['trades']:>6} {per_ed:>9.2f} "
                f"{s['win_rate'] * 100:>5.1f}% {s['total_pnl']:>11.2f} "
                f"{s['expectancy']:>8.2f} {s['profit_factor']:>6.2f}"
            )


if __name__ == "__main__":
    main()
