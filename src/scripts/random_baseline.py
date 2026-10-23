"""Random-direction baseline — replay a strategy against coin-flipped copies.

Runs the real strategy once and ``--seeds`` copies whose every open has its side
drawn by a coin flip (same epics, same minutes, same close profile, same gates;
see :mod:`src.backtest.random_baseline`), on the archived candles only — no DB,
no IG call, safe while the bot runs. Prints the real result, the null
distribution and two one-sided p-values: the share of coin flips that did at
least as well as the strategy (empirical, floored at ``1 / (seeds + 1)``) and
the normal approximation from the null's mean and spread (``z``), which is the
one compared with the Bonferroni threshold.

Examples::

    python -m src.scripts.random_baseline --strategy open_fade
    python -m src.scripts.random_baseline --strategy open_steady --seeds 40
    python -m src.scripts.random_baseline --all --seeds 20 --json baseline.json

Each replay is a full backtest (minutes on the whole archive), so they run in
parallel worker processes, each loading the archive once.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from src.backtest.backtest_archive import BacktestArchive
from src.backtest.backtester import StrategySelection, untestable_reason
from src.backtest.contract_values import ContractTable
from src.backtest.random_baseline import BaselineReport, replay
from src.core.config import get_settings
from src.entry import ENTRY_STRATEGIES

logging.basicConfig(
    level=logging.WARNING, format="%(levelname)s %(name)s — %(message)s"
)

# Per-worker state, filled once by :func:`_init_worker` (the archive is the
# expensive part; every replay of the worker reuses it).
_WORKER: dict = {}


def _init_worker(weeks: list[str] | None) -> None:
    settings = get_settings()
    archive = BacktestArchive(settings.candle_dump_dir)
    chosen = weeks or sorted(d.week for d in archive.datasets())
    _WORKER["settings"] = settings
    _WORKER["candles"] = archive.load(weeks=chosen)
    _WORKER["table"] = ContractTable.load(settings.backtest_contract_file)


def _run(task: tuple[str, int | None]) -> tuple[str, int | None, int, float, float]:
    strategy, seed = task
    trades, mean, euro = replay(
        _WORKER["settings"],
        _WORKER["candles"],
        StrategySelection(open_strategy=strategy),
        _WORKER["table"],
        seed=seed,
    )
    return strategy, seed, trades, mean, euro


def _or_zero(value: float) -> float:
    """``value``, or 0.0 when it is NaN (an undefined z sorts as "no signal")."""
    return 0.0 if math.isnan(value) else value


def _strategies(args) -> list[str]:
    if args.all:
        return [
            name
            for name in sorted(ENTRY_STRATEGIES)
            if untestable_reason("open_strategy", name) is None
        ]
    return [args.strategy or get_settings().open_strategy]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--strategy", help="Entry strategy (default: OPEN_STRATEGY).")
    target.add_argument("--all", action="store_true", help="Every testable entry.")
    parser.add_argument("--seeds", type=int, default=20, help="Coin-flip replays.")
    parser.add_argument("--weeks", nargs="*", help="ISO weeks (default: all).")
    parser.add_argument(
        "--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1)
    )
    parser.add_argument("--json", type=Path, help="Also write the reports here.")
    args = parser.parse_args()

    strategies = _strategies(args)
    tasks = [(s, seed) for s in strategies for seed in [None, *range(args.seeds)]]
    print(
        f"{len(strategies)} strategy(ies) × {args.seeds + 1} replays "
        f"on {args.workers} worker(s)…",
        flush=True,
    )

    runs: dict[str, dict] = {s: {"null": []} for s in strategies}
    with ProcessPoolExecutor(
        max_workers=args.workers, initializer=_init_worker, initargs=(args.weeks,)
    ) as pool:
        for strategy, seed, trades, mean, euro in pool.map(_run, tasks):
            if seed is None:
                runs[strategy]["real"] = (trades, mean, euro)
            else:
                runs[strategy]["null"].append((trades, mean, euro))

    reports = []
    for strategy in strategies:
        trades, mean, euro = runs[strategy]["real"]
        null = runs[strategy]["null"]
        reports.append(
            BaselineReport(
                strategy=strategy,
                trades=trades,
                mean_r=mean,
                euro=euro,
                null_trades=[n[0] for n in null],
                null_mean_r=[n[1] for n in null],
                null_euro=[n[2] for n in null],
            ).summary()
        )

    alpha = 0.05 / len(reports)
    print(
        f"\n{'strategy':<19}{'trades':>7}{'R':>8}{'euro':>9}"
        f"{'null R med':>11}{'null R p95':>11}{'null € med':>11}{'z':>7}"
        f"{'p(z)':>8}{'p emp':>7}{'p(€)':>7}"
    )
    for r in sorted(reports, key=lambda r: -_or_zero(r["z_r"])):
        flag = "  beats chance" if r["p_value_r_normal"] < alpha else ""
        print(
            f"{r['strategy']:<19}{r['trades']:>7}{r['mean_r']:>8.3f}{r['euro']:>9.0f}"
            f"{r['null_mean_r_median']:>11.3f}{r['null_mean_r_p95']:>11.3f}"
            f"{r['null_euro_median']:>11.0f}{r['z_r']:>7.2f}"
            f"{r['p_value_r_normal']:>8.4f}{r['p_value_r']:>7.3f}"
            f"{r['p_value_euro']:>7.3f}{flag}"
        )
    print(
        f"\np(z) = normal approximation, compared with the Bonferroni threshold "
        f"over {len(reports)} strategy(ies): {alpha:.4f}. p emp = share of coin "
        f"flips at least as good (floor with {args.seeds} seeds: "
        f"{1 / (args.seeds + 1):.3f})."
    )
    if args.json:
        args.json.write_text(json.dumps(reports, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
