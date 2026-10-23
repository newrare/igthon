"""Research dataset — build it from the archive, then analyse it train/test.

Three sub-commands, all offline (archive files only, no DB, no IG call)::

    python -m src.scripts.research build                 # every archived week
    python -m src.scripts.research build --weeks 2026-W38 2026-W39 --step 10
    python -m src.scripts.research build --timeframe 15 --horizon 600 \
        --path dumps/research/tf15.csv                   # 15-minute scale
    python -m src.scripts.research build --timeframe 60 --overnight \
        --horizon 4320 --funding 0.04 --step 60          # multi-day holds
    python -m src.scripts.research markets               # cheapest markets first
    python -m src.scripts.research build --timeframe 15 --horizon 600 --step 15 \
        --strategy open_fade --path dumps/research/fade15.csv
    python -m src.scripts.research signal --path dumps/research/fade15.csv --first
    python -m src.scripts.research analyze               # train 60 % / test 40 %
    python -m src.scripts.research analyze --train 0.5 --all
    python -m src.scripts.research analyze --test-from 2026-10-05   # forward check

``build`` writes one CSV row per epic every ``--step`` minutes (features + the R a
BUY and a SELL would have paid, see :mod:`src.backtest.research`) to
``<CANDLE_DUMP_DIR>/research/dataset.csv`` — a sub-folder, so the candle
archive never mistakes it for candles. ``analyze`` reads it back and prints the
buckets positive on both halves, flagging those below the Bonferroni threshold;
``markets`` ranks the epics by movement per unit of cost on the train half and
measures whether the cheapest ones clear the spread on the test half; ``signal``
scores the rows a strategy signalled on (``build --strategy``) against a coin
flip on the same rows.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

from src.backtest.backtest_archive import BacktestArchive
from src.backtest.research import (
    DEFAULT_HORIZON,
    DEFAULT_STEP,
    DEFAULT_STOP_ATR,
    DEFAULT_TARGET_ATR,
    DEFAULT_TIMEFRAME,
    analyze,
    build_rows,
    cost_filter,
    is_significant,
    market_table,
    read_csv,
    signal_report,
    write_csv,
)
from src.core.config import get_settings
from src.entry import get_entry_strategy

logging.basicConfig(
    level=logging.WARNING, format="%(levelname)s %(name)s — %(message)s"
)


def _default_path() -> Path:
    return Path(get_settings().candle_dump_dir) / "research" / "dataset.csv"


def _build(args) -> None:
    settings = get_settings()
    archive = BacktestArchive(settings.candle_dump_dir)
    entry = get_entry_strategy(args.strategy, settings) if args.strategy else None
    weeks = args.weeks or sorted(d.week for d in archive.datasets())
    print(f"Loading {len(weeks)} week(s): {weeks[0]} → {weeks[-1]}…", flush=True)
    rows = build_rows(
        archive.load(weeks=weeks),
        step=args.step,
        stop_atr=args.stop_atr,
        target_atr=args.target_atr,
        horizon=args.horizon,
        timeframe=args.timeframe,
        overnight=args.overnight,
        funding_annual=args.funding,
        entry=entry,
    )
    count = write_csv(rows, args.path)
    print(f"{count} rows written to {args.path}")


def _analyze(args) -> None:
    rows = read_csv(args.path)
    result = analyze(rows, train_fraction=args.train, test_from=args.test_from)
    print(
        f"{len(rows)} rows — train {len(result.train_days)} day(s) "
        f"({result.train_days[0]} → {result.train_days[-1]}), test "
        f"{len(result.test_days)} day(s) ({result.test_days[0]} → "
        f"{result.test_days[-1]})"
    )
    for side, (train_r, test_r) in result.baseline.items():
        print(f"Unfiltered {side:<4}: train {train_r:+.3f} R   test {test_r:+.3f} R")
    (buy_train, buy_test), (sell_train, sell_test) = (
        result.baseline["BUY"],
        result.baseline["SELL"],
    )
    # The market's drift adds to one side what it takes from the other, so the
    # average of the two sides is the pure cost of trading at this scale.
    print(
        f"Drift-neutral (BUY+SELL)/2: train {(buy_train + sell_train) / 2:+.3f} R"
        f"   test {(buy_test + sell_test) / 2:+.3f} R"
    )

    alpha = result.alpha
    shown = [f for f in result.findings if args.all or f.robust]
    shown.sort(key=lambda f: (f.test_p if f.robust else 2.0, -f.test_r))
    print(
        f"\n{len(result.findings)} bucket×side tested, "
        f"{sum(f.robust for f in result.findings)} positive on both halves; "
        f"Bonferroni threshold {alpha:.5f}\n"
    )
    print(
        f"{'feature':<14}{'bucket':>7}{'side':>6}{'train n':>9}{'train R':>9}"
        f"{'test n':>8}{'days':>6}{'test R':>8}{'p test':>8}"
    )
    for f in shown[: args.top]:
        flag = "  SIGNIFICANT" if is_significant(f, alpha) else ""
        print(
            f"{f.feature:<14}{f.bucket:>7}{f.direction:>6}{f.train_n:>9}"
            f"{f.train_r:>+9.3f}{f.test_n:>8}{f.test_days:>6}{f.test_r:>+8.3f}"
            f"{f.test_p:>8.4f}{flag}"
        )


def _markets(args) -> None:
    rows = read_csv(args.path)
    table = market_table(rows, train_fraction=args.train, test_from=args.test_from)
    print(
        f"{'epic':<24}{'ATR/spread':>11}{'train n':>8}{'BUY tr':>8}{'SELL tr':>8}"
        f"{'test n':>7}{'BUY te':>8}{'SELL te':>8}"
    )
    for m in table:
        print(
            f"{m.epic:<24}{m.cost_ratio:>11.2f}{m.train_n:>8}{m.train_buy:>+8.3f}"
            f"{m.train_sell:>+8.3f}{m.test_n:>7}{m.test_buy:>+8.3f}{m.test_sell:>+8.3f}"
        )
    for fraction in args.top:
        result = cost_filter(
            rows,
            top_fraction=fraction,
            train_fraction=args.train,
            test_from=args.test_from,
        )
        print(
            f"\nCheapest {fraction:.0%} ({len(result.kept)} epics, ranked on train) "
            f"on test: BUY {result.test_buy:+.3f} R (p {result.test_p_buy:.3f}), "
            f"SELL {result.test_sell:+.3f} R (p {result.test_p_sell:.3f}), "
            f"drift-neutral {(result.test_buy + result.test_sell) / 2:+.3f} R, "
            f"n={result.test_n} — rest: BUY {result.rest_buy:+.3f} / "
            f"SELL {result.rest_sell:+.3f} R, drift-neutral "
            f"{(result.rest_buy + result.rest_sell) / 2:+.3f} R"
        )


def _signal(args) -> None:
    rows = read_csv(args.path)
    if not any(r.signal for r in rows):
        raise SystemExit(f"{args.path} has no signal column: build it with --strategy")
    print(
        f"{'markets':<16}{'half':<6}{'rows':>6}{'days':>5}{'signal R':>10}"
        f"{'coin R':>8}{'excess':>8}{'p signal':>9}{'p excess':>9}"
    )
    for fraction in [None, *args.top]:
        report = signal_report(
            rows,
            top_fraction=fraction,
            train_fraction=args.train,
            test_from=args.test_from,
            first_only=args.first,
        )
        label = "all" if fraction is None else f"cheapest {fraction:.0%}"
        for name, half in (("train", report.train), ("test", report.test)):
            print(
                f"{label:<16}{name:<6}{half.rows:>6}{half.days:>5}"
                f"{half.signal_r:>+10.3f}{half.coin_r:>+8.3f}{half.excess:>+8.3f}"
                f"{half.p_signal:>9.3f}{half.p_excess:>9.3f}"
            )
    print(
        "\nsignal R = following the signal; coin R = a coin flip on the same rows "
        "(same drift, same cost); excess = direction skill. p = day-clustered "
        "bootstrap of mean > 0."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Build the dataset from the archive.")
    build.add_argument("--weeks", nargs="*", help="ISO weeks (default: all).")
    build.add_argument("--step", type=int, default=DEFAULT_STEP)
    build.add_argument("--stop-atr", type=float, default=DEFAULT_STOP_ATR)
    build.add_argument("--target-atr", type=float, default=DEFAULT_TARGET_ATR)
    build.add_argument("--horizon", type=int, default=DEFAULT_HORIZON, help="Minutes.")
    build.add_argument(
        "--timeframe",
        type=int,
        default=DEFAULT_TIMEFRAME,
        help="Minutes per bar the bracket's ATR is measured on.",
    )
    build.add_argument(
        "--overnight",
        action="store_true",
        help="No daily force-close: hold across nights (pays --funding).",
    )
    build.add_argument(
        "--funding",
        type=float,
        default=0.0,
        help="Annual overnight funding as a fraction of price (0.04 = 4 %%).",
    )
    build.add_argument(
        "--strategy", help="Also record this entry strategy's signal per row."
    )
    build.add_argument("--path", type=Path, default=None)

    report = sub.add_parser("analyze", help="Train/test analysis of the dataset.")
    report.add_argument("--train", type=float, default=0.6, help="Train fraction.")
    report.add_argument(
        "--test-from",
        type=date.fromisoformat,
        default=None,
        help="Fixed cut date (YYYY-MM-DD): train before it, test from it on.",
    )
    report.add_argument("--all", action="store_true", help="Show every bucket.")
    report.add_argument("--top", type=int, default=40, help="Rows to print.")
    report.add_argument("--path", type=Path, default=None)

    markets = sub.add_parser("markets", help="Rank markets by cost; test the top.")
    markets.add_argument("--train", type=float, default=0.6, help="Train fraction.")
    markets.add_argument("--test-from", type=date.fromisoformat, default=None)
    markets.add_argument(
        "--top",
        type=float,
        nargs="*",
        default=[0.1, 0.25, 0.5],
        help="Fractions of the cheapest markets to test.",
    )
    markets.add_argument("--path", type=Path, default=None)

    signal = sub.add_parser("signal", help="A strategy's signal vs a coin flip.")
    signal.add_argument("--train", type=float, default=0.6, help="Train fraction.")
    signal.add_argument("--test-from", type=date.fromisoformat, default=None)
    signal.add_argument(
        "--top",
        type=float,
        nargs="*",
        default=[0.25, 0.5],
        help="Also restrict to these fractions of the cheapest markets.",
    )
    signal.add_argument(
        "--first",
        action="store_true",
        help="Only the first signalled row per epic and day (the live opens).",
    )
    signal.add_argument("--path", type=Path, default=None)

    args = parser.parse_args()
    args.path = args.path or _default_path()
    commands = {
        "build": _build,
        "analyze": _analyze,
        "markets": _markets,
        "signal": _signal,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
