"""Research dataset — what a moment looked like vs. what the next hours paid.

The strategies in :mod:`src.entry` are hand-written rules, each tested on its own
against zero. This module turns the question around: sample every market every
few minutes, describe the moment with a fixed set of **look-ahead-free
features**, and record what a BUY and a SELL opened right then would have paid,
**net of the spread**, under one fixed stop/target bracket. Any rule is then a
filter on those features, and it can be judged out of sample instead of being
tuned on the very days it is scored on.

Rows
----

One :class:`ResearchRow` per ``(epic, minute)`` sampled every ``step`` minutes
between ``first_hour_utc`` and ``last_hour_utc``, on the archived candles only
(same deduped universe as a backtest). Features read the epic's candles of the
same UTC day **up to and including** the sampled minute — never later — so a
row is exactly what the bot could have known. Note that an early sample of a
day, or of a market that opens late, has less than :data:`LONG_ATR_PERIOD` of
history, so its ``vol_ratio`` baseline is shorter.

Outcome (``r_buy`` / ``r_sell``)
--------------------------------

The live geometry, without the trailing: a BUY fills at the offer, its stop sits
``stop_atr × ATR`` under the bid and its target ``target_atr × ATR`` above it; a
SELL mirrors that on the offer side. One R is the fill-to-stop distance, so the
spread is inside the risk exactly as in the live euro risk. Walking forward
candle by candle, the first level touched decides (+target in R, or −1); when a
candle touches both, the stop is assumed first (the conservative reading of an
unknown intra-minute path). With neither touched by ``horizon`` minutes — or by
the ``last_hour_utc`` force-close, as in the simulator — the trade is marked to
the close-out price.

Scale and cost (roadmap point 2)
--------------------------------

On the 1-minute scale the median spread is about one ATR, so the bracket pays a
large share of its risk at entry. ``timeframe`` sizes the bracket on the ATR of
``timeframe``-minute bars instead (the noise grows roughly with the square root
of the bar length, the spread does not), ``overnight`` lifts the daily
force-close and charges IG's overnight funding per night held, and
:func:`market_table` / :func:`cost_filter` rank the markets by movement per unit
of cost (``tf_atr_spread``) on the train half and measure the cheapest ones on
the test half.

Analysis
--------

:func:`analyze` splits the **days** in time order (first ``train_fraction``
train, the rest test), buckets every numeric feature into deciles cut on the
train half only and every categorical one by value, and reports each bucket's
mean R on both halves and both sides. A finding counts only when it is positive
on train **and** test; its p-value is a day-clustered bootstrap on the test half
(samples of one day share the market regime and overlap in time, so days are the
independent unit), to be read against the Bonferroni threshold over every
bucket tested.
"""

from __future__ import annotations

import csv
import math
import random
import statistics
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.backtest.backtester import dedupe_correlated_epics
from src.core.indicators import atr, channel_position, efficiency_ratio, trend_pct
from src.entry.base import EntryStrategy
from src.feed.price_buffer import DEFAULT_MAX_CANDLES, Candle, EpicBuffer

PARIS = ZoneInfo("Europe/Paris")

DEFAULT_STEP = 5  # minutes between two samples of one epic
DEFAULT_STOP_ATR = 2.5  # the live stop_atr distance
DEFAULT_TARGET_ATR = 5.0  # 2 R target
DEFAULT_HORIZON = 120  # minutes before marking to market
DEFAULT_FIRST_HOUR_UTC = 7
DEFAULT_LAST_HOUR_UTC = 17  # also the force-close hour, as in the simulator
DEFAULT_TIMEFRAME = 1  # minutes per bar the bracket's ATR is measured on

ATR_PERIOD = 14
LONG_ATR_PERIOD = 240  # ~4 h: the epic's own volatility baseline
WINDOW = 60  # trend / channel / efficiency window (~1 h)
ROC_WINDOW = 30
MIN_HISTORY = WINDOW + 1

NUMERIC_FEATURES: tuple[str, ...] = (
    "trend60_pct",  # implied % move of the 60-candle fit (signed)
    "r2_60",  # linearity of that fit
    "chan60",  # bid position in the 60-candle high/low channel (0..1)
    "er60",  # Kaufman efficiency over 60 candles (0..1)
    "roc30_atr",  # 30-candle move in ATRs (signed)
    "atr_spread",  # movement per unit of cost: ATR ÷ spread
    "tf_atr_spread",  # same, with the bracket's ATR (timeframe bars) ÷ spread
    "vol_ratio",  # ATR(14) ÷ ATR(240): volatility now vs. its own baseline
    "day_move_atr",  # move since the day's first candle, in ATRs (signed)
    "day_pos",  # bid position in the day's high/low so far (0..1)
)
CATEGORICAL_FEATURES: tuple[str, ...] = ("hour_paris", "weekday", "asset_class")
FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES


@dataclass(slots=True)
class ResearchRow:
    """One sampled moment and what each side would have paid from it."""

    epic: str
    timestamp: datetime
    features: dict[str, float | str]
    r_buy: float
    r_sell: float
    # Side an entry strategy proposed at this minute ("BUY" / "SELL"), or "" when
    # it stayed flat or no strategy was evaluated (see ``build_rows(entry=…)``).
    signal: str = ""

    @property
    def day(self) -> date:
        return self.timestamp.date()


def compute_features(
    epic: str,
    recent: Sequence[Candle],
    *,
    day_open: float,
    day_high: float,
    day_low: float,
    bracket_atr: float | None = None,
) -> dict | None:
    """Features of the last candle of ``recent``.

    ``recent`` holds the epic's candles of the day **ending at the sampled
    minute** (at most :data:`LONG_ATR_PERIOD` + 1 are read); ``day_open`` /
    ``day_high`` / ``day_low`` summarise the day up to that same minute. Nothing
    after it is read, which is what makes the dataset free of look-ahead.
    Returns ``None`` when the history is too short or the market shows no
    measurable volatility or spread (no stop could be sized, no cost measured).

    ``bracket_atr`` is the ATR the outcome bracket is sized on — measured on
    ``timeframe``-minute bars (see :func:`build_rows`); it defaults to the
    1-minute ATR, so ``tf_atr_spread`` equals ``atr_spread`` on the 1-minute
    scale.
    """
    if len(recent) < MIN_HISTORY:
        return None
    history = list(recent[-(LONG_ATR_PERIOD + 1) :])
    last = history[-1]
    bid = last.bid_close
    spread = last.offer_close - last.bid_close
    atr_now = atr(history[-(ATR_PERIOD + 1) :], ATR_PERIOD)
    if bid <= 0 or spread <= 0 or atr_now <= 0:
        return None
    bids = [c.bid_close for c in history[-(WINDOW + 1) :]]
    trend, r2 = trend_pct(bids, WINDOW)
    atr_long = atr(history, len(history) - 1)
    local = last.timestamp.astimezone(PARIS)
    return {
        "trend60_pct": trend,
        "r2_60": r2,
        "chan60": channel_position(history, WINDOW)[0],
        "er60": efficiency_ratio(bids, WINDOW),
        "roc30_atr": (bid - bids[-ROC_WINDOW - 1]) / atr_now,
        "atr_spread": atr_now / spread,
        "tf_atr_spread": (bracket_atr or atr_now) / spread,
        "vol_ratio": atr_now / atr_long if atr_long > 0 else 1.0,
        "day_move_atr": (bid - day_open) / atr_now,
        "day_pos": (
            (bid - day_low) / (day_high - day_low) if day_high > day_low else 0.5
        ),
        "hour_paris": f"{local.hour:02d}",
        "weekday": local.strftime("%a"),
        "asset_class": ".".join(epic.split(".")[:2]),
    }


def label_outcome(
    candles: Sequence[Candle],
    index: int,
    direction: str,
    *,
    atr_value: float,
    stop_atr: float = DEFAULT_STOP_ATR,
    target_atr: float = DEFAULT_TARGET_ATR,
    horizon: int = DEFAULT_HORIZON,
    last_hour_utc: int | None = DEFAULT_LAST_HOUR_UTC,
    funding_per_night: float = 0.0,
) -> float | None:
    """R paid by a ``direction`` trade opened at ``candles[index]``'s close.

    See the module docstring for the bracket. ``horizon`` is in **minutes** of
    wall-clock time (a gap in the feed — a night, a weekend — is not walked
    through as if it were trading time). ``last_hour_utc`` of ``None`` removes
    the daily force-close, so the trade may be held across nights; each UTC
    midnight it is held over then costs ``funding_per_night`` × entry price
    (IG's overnight funding, both sides pay it), charged in R on top of the
    outcome. ``None`` when the risk distance is not positive or there is no later
    candle to resolve on.
    """
    entry = candles[index]
    if direction == "BUY":
        fill = entry.offer_close
        stop = entry.bid_close - stop_atr * atr_value
        target = entry.bid_close + target_atr * atr_value
        risk = fill - stop
    else:
        fill = entry.bid_close
        stop = entry.offer_close + stop_atr * atr_value
        target = entry.offer_close - target_atr * atr_value
        risk = stop - fill
    if risk <= 0:
        return None

    def funded(r: float, at: datetime) -> float:
        nights = (at.date() - entry.timestamp.date()).days
        return r - nights * funding_per_night * entry.bid_close / risk

    deadline = entry.timestamp + timedelta(minutes=horizon)
    last = None
    for candle in candles[index + 1 :]:
        if candle.timestamp > deadline:
            break
        if last_hour_utc is not None and (
            candle.timestamp.hour >= last_hour_utc
            or candle.timestamp.date() != entry.timestamp.date()
        ):
            break  # the simulator force-closes here: mark to this point
        last = candle
        if direction == "BUY":
            if candle.bid_low <= stop:
                return funded(-1.0, candle.timestamp)
            if candle.bid_high >= target:
                return funded((target - fill) / risk, candle.timestamp)
        else:
            if candle.offer_high >= stop:
                return funded(-1.0, candle.timestamp)
            if candle.offer_low <= target:
                return funded((fill - target) / risk, candle.timestamp)
    if last is None:
        return None
    if direction == "BUY":
        return funded((last.bid_close - fill) / risk, last.timestamp)
    return funded((fill - last.offer_close) / risk, last.timestamp)


def resample(candles: Sequence[Candle], minutes: int) -> list[Candle]:
    """Aggregate one-minute candles into ``minutes``-minute bars (bid and offer).

    Bars are grouped on the clock (``floor(epoch minute / minutes)``) and stamped
    with their first candle; the last bar may be partial — it holds only what
    had happened by the last candle, which keeps a resampled history free of
    look-ahead.
    """
    if minutes <= 1:
        return list(candles)
    bars: list[Candle] = []
    current_key = None
    for candle in candles:
        key = int(candle.timestamp.timestamp() // 60) // minutes
        if key != current_key:
            current_key = key
            bars.append(
                Candle(
                    timestamp=candle.timestamp,
                    bid_open=candle.bid_open,
                    bid_close=candle.bid_close,
                    bid_high=candle.bid_high,
                    bid_low=candle.bid_low,
                    offer_open=candle.offer_open,
                    offer_close=candle.offer_close,
                    offer_high=candle.offer_high,
                    offer_low=candle.offer_low,
                    volume=candle.volume,
                )
            )
            continue
        bar = bars[-1]
        bar.bid_close = candle.bid_close
        bar.bid_high = max(bar.bid_high, candle.bid_high)
        bar.bid_low = min(bar.bid_low, candle.bid_low)
        bar.offer_close = candle.offer_close
        bar.offer_high = max(bar.offer_high, candle.offer_high)
        bar.offer_low = min(bar.offer_low, candle.offer_low)
        bar.volume += candle.volume
    return bars


def build_rows(
    candles_by_epic: dict[str, list[Candle]],
    *,
    step: int = DEFAULT_STEP,
    first_hour_utc: int = DEFAULT_FIRST_HOUR_UTC,
    last_hour_utc: int = DEFAULT_LAST_HOUR_UTC,
    stop_atr: float = DEFAULT_STOP_ATR,
    target_atr: float = DEFAULT_TARGET_ATR,
    horizon: int = DEFAULT_HORIZON,
    timeframe: int = DEFAULT_TIMEFRAME,
    overnight: bool = False,
    funding_annual: float = 0.0,
    entry: EntryStrategy | None = None,
) -> Iterator[ResearchRow]:
    """Sample every epic every ``step`` minutes and label both sides.

    The universe is deduped exactly like a backtest's
    (:func:`~src.backtest.backtester.dedupe_correlated_epics`). Features are
    read on the epic's candles of the same UTC day, the boundary the simulator
    resets on.

    ``timeframe`` (minutes) is the scale the bracket is sized on: its ATR is
    measured on ``timeframe``-minute bars of the epic's last
    ``(ATR_PERIOD + 2) × timeframe`` quoted candles (reaching into the previous
    sessions when the day is still too short), so a stop of
    ``stop_atr`` ATRs is that many bars of noise at that scale — and the spread
    weighs ``1 / tf_atr_spread`` of an ATR there. ``overnight`` lifts the daily
    force-close: the trade is walked across nights for up to ``horizon``
    minutes, paying ``funding_annual / 365`` of its price per night held.

    ``entry`` stamps each row with the side that strategy would have proposed at
    that minute (:attr:`ResearchRow.signal`). It reads a live-sized one-minute
    buffer (``DEFAULT_MAX_CANDLES``, reset at each UTC day like the simulator's),
    so the signal is the strategy's own, unchanged, whatever ``timeframe`` the
    outcome is measured on; a SELL is kept only when it ``emits_shorts``.
    """
    kept, _dropped = dedupe_correlated_epics(candles_by_epic)
    funding = funding_annual / 365
    history_candles = (ATR_PERIOD + 2) * timeframe
    for epic in sorted(kept):
        series = sorted(kept[epic], key=lambda c: c.timestamp)
        day_start = 0
        day_open = day_high = day_low = 0.0
        for index, candle in enumerate(series):
            ts = candle.timestamp
            if index == 0 or ts.date() != series[index - 1].timestamp.date():
                day_start, day_open = index, candle.bid_open
                day_high, day_low = -math.inf, math.inf
                buf = EpicBuffer(epic=epic, max_candles=DEFAULT_MAX_CANDLES)
            buf.add(candle)
            # Running day extremes: they include this candle, never a later one.
            day_high = max(day_high, candle.bid_high)
            day_low = min(day_low, candle.bid_low)
            if not first_hour_utc <= ts.hour < last_hour_utc:
                continue
            if (ts.hour * 60 + ts.minute) % step:
                continue
            recent = series[max(day_start, index - LONG_ATR_PERIOD) : index + 1]
            if timeframe <= 1:
                atr_value = atr(recent[-(ATR_PERIOD + 1) :], ATR_PERIOD)
            else:
                # History counted in quoted candles, not clock time: a market
                # quoted ~10 h a day still yields its 15 bars, reaching back over
                # the previous sessions (never forward).
                bars = resample(
                    series[max(0, index - history_candles) : index + 1], timeframe
                )
                atr_value = atr(bars, ATR_PERIOD) if len(bars) > ATR_PERIOD else 0.0
            if atr_value <= 0:
                continue
            features = compute_features(
                epic,
                recent,
                day_open=day_open,
                day_high=day_high,
                day_low=day_low,
                bracket_atr=atr_value,
            )
            if features is None:
                continue
            outcomes = [
                label_outcome(
                    series,
                    index,
                    side,
                    atr_value=atr_value,
                    stop_atr=stop_atr,
                    target_atr=target_atr,
                    horizon=horizon,
                    last_hour_utc=None if overnight else last_hour_utc,
                    funding_per_night=funding,
                )
                for side in ("BUY", "SELL")
            ]
            if None in outcomes:
                continue
            yield ResearchRow(
                epic,
                ts,
                features,
                outcomes[0],
                outcomes[1],
                signal=_signal(entry, epic, buf),
            )


def _signal(entry: EntryStrategy | None, epic: str, buf: EpicBuffer) -> str:
    """Side ``entry`` proposes on ``buf`` right now, or "" (flat / no strategy)."""
    if entry is None or len(buf) < entry.warmup:
        return ""
    intent = entry.evaluate(epic, buf)
    if intent is None:
        return ""
    if intent.direction == "SELL" and not getattr(entry, "emits_shorts", False):
        return ""
    return intent.direction


def write_csv(rows: Iterable[ResearchRow], path: Path) -> int:
    """Write ``rows`` to ``path`` (one line per row); returns the row count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["epic", "timestamp", *FEATURES, "r_buy", "r_sell", "signal"])
        for row in rows:
            writer.writerow(
                [
                    row.epic,
                    row.timestamp.isoformat(),
                    *(row.features[name] for name in FEATURES),
                    f"{row.r_buy:.4f}",
                    f"{row.r_sell:.4f}",
                    row.signal,
                ]
            )
            count += 1
    return count


def read_csv(path: Path) -> list[ResearchRow]:
    """Read a dataset written by :func:`write_csv`."""
    rows: list[ResearchRow] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for record in csv.DictReader(handle):
            # A dataset written before ``tf_atr_spread`` existed was on the
            # 1-minute scale, where it equals ``atr_spread``.
            record.setdefault("tf_atr_spread", record["atr_spread"])
            features: dict[str, float | str] = {
                name: float(record[name]) for name in NUMERIC_FEATURES
            }
            features.update({name: record[name] for name in CATEGORICAL_FEATURES})
            rows.append(
                ResearchRow(
                    epic=record["epic"],
                    timestamp=datetime.fromisoformat(record["timestamp"]),
                    features=features,
                    r_buy=float(record["r_buy"]),
                    r_sell=float(record["r_sell"]),
                    signal=record.get("signal") or "",
                )
            )
    return rows


# --------------------------------------------------------------------------- #
# Train / test analysis                                                        #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Finding:
    """One feature bucket on one side, measured on both halves."""

    feature: str
    bucket: str
    direction: str
    train_n: int
    train_r: float
    test_n: int
    test_r: float
    test_days: int
    test_p: float = float("nan")

    @property
    def robust(self) -> bool:
        """Positive on train *and* test — the minimum to be worth a look."""
        return self.train_r > 0 and self.test_r > 0


@dataclass(slots=True)
class Analysis:
    """Everything :func:`analyze` measured."""

    train_days: list[date]
    test_days: list[date]
    baseline: dict[str, tuple[float, float]]  # side -> (train R, test R)
    findings: list[Finding] = field(default_factory=list)

    @property
    def alpha(self) -> float:
        """Bonferroni threshold over every bucket × side tested."""
        return 0.05 / max(len(self.findings), 1)


def split_days(
    rows: Sequence[ResearchRow],
    train_fraction: float,
    test_from: date | None = None,
) -> tuple[list[date], list[date]]:
    """Distinct days in time order, cut into ``(train, test)``.

    ``test_from`` overrides the fraction with a fixed date — the forward
    validation mode: the cut (and so every decile edge) stays where it was when
    a rule was frozen, and only the days that came after it are scored.
    """
    days = sorted({row.day for row in rows})
    if test_from is not None:
        return [d for d in days if d < test_from], [d for d in days if d >= test_from]
    cut = max(1, min(len(days) - 1, round(len(days) * train_fraction)))
    return days[:cut], days[cut:]


def decile_edges(values: Sequence[float], buckets: int = 10) -> list[float]:
    """Inner cut points splitting ``values`` into ``buckets`` equal-count groups."""
    ordered = sorted(values)
    if not ordered:
        return []
    return [ordered[int(len(ordered) * k / buckets)] for k in range(1, buckets)]


def bucket_of(value: float, edges: Sequence[float]) -> int:
    """Index of the bucket ``value`` falls in (0 .. len(edges))."""
    index = 0
    for edge in edges:
        if value < edge:
            break
        index += 1
    return index


def clustered_p_value(
    values_by_day: dict[date, list[float]], *, resamples: int = 2000, seed: int = 7
) -> float:
    """One-sided bootstrap p-value of ``mean > 0``, resampling whole days."""
    days = [(sum(v), len(v)) for v in values_by_day.values() if v]
    if len(days) < 5:
        return float("nan")
    rng = random.Random(seed)
    at_or_below = 0
    for _ in range(resamples):
        total = count = 0
        for _ in days:
            day_sum, day_count = days[rng.randrange(len(days))]
            total += day_sum
            count += day_count
        if total / count <= 0:
            at_or_below += 1
    return at_or_below / resamples


def analyze(
    rows: Sequence[ResearchRow],
    *,
    train_fraction: float = 0.6,
    test_from: date | None = None,
    buckets: int = 10,
    min_test_days: int = 5,
    resamples: int = 2000,
) -> Analysis:
    """Measure every feature bucket on train and test (see the module doc).

    ``test_from`` fixes the train/test cut at a date instead of a fraction (see
    :func:`split_days`).
    """
    train_days, test_days = split_days(rows, train_fraction, test_from)
    if not train_days or not test_days:
        raise ValueError("analyze needs at least one train day and one test day")
    test_set = set(test_days)
    train = [r for r in rows if r.day not in test_set]
    test = [r for r in rows if r.day in test_set]

    def mean(values: Sequence[float]) -> float:
        return statistics.fmean(values) if values else float("nan")

    baseline = {
        "BUY": (mean([r.r_buy for r in train]), mean([r.r_buy for r in test])),
        "SELL": (mean([r.r_sell for r in train]), mean([r.r_sell for r in test])),
    }
    analysis = Analysis(train_days, test_days, baseline)

    for feature in FEATURES:
        if feature in NUMERIC_FEATURES:
            edges = decile_edges([float(r.features[feature]) for r in train], buckets)

            def key(row, edges=edges, feature=feature) -> str:
                index = bucket_of(float(row.features[feature]), edges)
                return f"D{index + 1}"

        else:

            def key(row, feature=feature) -> str:
                return str(row.features[feature])

        groups: dict[str, dict[str, list[ResearchRow]]] = defaultdict(
            lambda: {"train": [], "test": []}
        )
        for row in train:
            groups[key(row)]["train"].append(row)
        for row in test:
            groups[key(row)]["test"].append(row)

        for bucket in sorted(groups):
            part = groups[bucket]
            for side in ("BUY", "SELL"):
                attr = "r_buy" if side == "BUY" else "r_sell"
                train_values = [getattr(r, attr) for r in part["train"]]
                by_day: dict[date, list[float]] = defaultdict(list)
                for r in part["test"]:
                    by_day[r.day].append(getattr(r, attr))
                test_values = [v for values in by_day.values() for v in values]
                if not train_values or len(by_day) < min_test_days:
                    continue
                finding = Finding(
                    feature=feature,
                    bucket=bucket,
                    direction=side,
                    train_n=len(train_values),
                    train_r=mean(train_values),
                    test_n=len(test_values),
                    test_r=mean(test_values),
                    test_days=len(by_day),
                )
                if finding.robust:
                    finding.test_p = clustered_p_value(by_day, resamples=resamples)
                analysis.findings.append(finding)
    return analysis


def is_significant(finding: Finding, alpha: float) -> bool:
    """Robust and below the multiple-testing threshold on the test half."""
    return finding.robust and not math.isnan(finding.test_p) and finding.test_p < alpha


# --------------------------------------------------------------------------- #
# Cost-first market selection                                                  #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class MarketStat:
    """One epic's cost and unfiltered outcome, on both halves."""

    epic: str
    cost_ratio: float  # train median of tf_atr_spread (movement per unit of cost)
    train_n: int
    test_n: int
    train_buy: float
    train_sell: float
    test_buy: float
    test_sell: float


@dataclass(slots=True)
class CostFilter:
    """Unfiltered test R of the cheapest markets vs. the rest."""

    top_fraction: float
    kept: list[str]
    test_n: int
    test_buy: float
    test_sell: float
    rest_n: int
    rest_buy: float
    rest_sell: float
    test_p_buy: float
    test_p_sell: float


def market_table(
    rows: Sequence[ResearchRow],
    *,
    train_fraction: float = 0.6,
    test_from: date | None = None,
) -> list[MarketStat]:
    """Per-epic cost ratio (train median) and unfiltered R on train and test.

    Sorted cheapest first (highest ``tf_atr_spread``). Epics absent from the
    train half are skipped: their cost could not be ranked without peeking.
    """
    train_days, _test_days = split_days(rows, train_fraction, test_from)
    train_set = set(train_days)
    by_epic: dict[str, dict[str, list[ResearchRow]]] = defaultdict(
        lambda: {"train": [], "test": []}
    )
    for row in rows:
        by_epic[row.epic]["train" if row.day in train_set else "test"].append(row)

    def mean(values: Sequence[float]) -> float:
        return statistics.fmean(values) if values else float("nan")

    stats = []
    for epic, part in by_epic.items():
        if not part["train"]:
            continue
        stats.append(
            MarketStat(
                epic=epic,
                cost_ratio=statistics.median(
                    float(r.features["tf_atr_spread"]) for r in part["train"]
                ),
                train_n=len(part["train"]),
                test_n=len(part["test"]),
                train_buy=mean([r.r_buy for r in part["train"]]),
                train_sell=mean([r.r_sell for r in part["train"]]),
                test_buy=mean([r.r_buy for r in part["test"]]),
                test_sell=mean([r.r_sell for r in part["test"]]),
            )
        )
    stats.sort(key=lambda m: -m.cost_ratio)
    return stats


def cost_filter(
    rows: Sequence[ResearchRow],
    *,
    top_fraction: float = 0.25,
    train_fraction: float = 0.6,
    test_from: date | None = None,
    resamples: int = 2000,
) -> CostFilter:
    """Does trading only the cheapest markets clear the spread out of sample?

    Epics are ranked on the **train** half by their median ``tf_atr_spread``;
    the top ``top_fraction`` are kept and their unfiltered BUY / SELL mean R is
    measured on the **test** half, next to the rest of the universe, with a
    day-clustered bootstrap p-value of ``mean > 0``.
    """
    table = market_table(rows, train_fraction=train_fraction, test_from=test_from)
    count = max(1, round(len(table) * top_fraction))
    kept = {m.epic for m in table[:count]}
    _train_days, test_days = split_days(rows, train_fraction, test_from)
    test_set = set(test_days)
    test = [r for r in rows if r.day in test_set and r.epic in kept]
    rest = [r for r in rows if r.day in test_set and r.epic not in kept]

    def mean(values: Sequence[float]) -> float:
        return statistics.fmean(values) if values else float("nan")

    def p_value(side: str) -> float:
        by_day: dict[date, list[float]] = defaultdict(list)
        for r in test:
            by_day[r.day].append(r.r_buy if side == "BUY" else r.r_sell)
        return clustered_p_value(by_day, resamples=resamples)

    return CostFilter(
        top_fraction=top_fraction,
        kept=[m.epic for m in table[:count]],
        test_n=len(test),
        test_buy=mean([r.r_buy for r in test]),
        test_sell=mean([r.r_sell for r in test]),
        rest_n=len(rest),
        rest_buy=mean([r.r_buy for r in rest]),
        rest_sell=mean([r.r_sell for r in rest]),
        test_p_buy=p_value("BUY"),
        test_p_sell=p_value("SELL"),
    )


# --------------------------------------------------------------------------- #
# A strategy's signal against a coin flip, on the same rows                    #
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SignalHalf:
    """A strategy's signalled rows on one half of the days.

    ``signal_r`` is what following the signal paid; ``coin_r`` is what a coin
    flip paid **on the same rows** — the mean of the BUY and SELL outcomes, so
    the market's drift and the cost are identical in both; ``excess`` is their
    difference, the direction skill. ``p_signal`` / ``p_excess`` are
    day-clustered bootstrap p-values of ``mean > 0``.
    """

    rows: int
    days: int
    signal_r: float
    coin_r: float
    excess: float
    p_signal: float
    p_excess: float


@dataclass(slots=True)
class SignalReport:
    """Train and test halves of a strategy's signal, optionally on cheap markets."""

    top_fraction: float | None
    markets: list[str]
    train: SignalHalf
    test: SignalHalf


def _signal_half(rows: Sequence[ResearchRow], resamples: int) -> SignalHalf:
    paid: dict[date, list[float]] = defaultdict(list)
    skill: dict[date, list[float]] = defaultdict(list)
    coins: list[float] = []
    for row in rows:
        coin = (row.r_buy + row.r_sell) / 2
        r = row.r_buy if row.signal == "BUY" else row.r_sell
        paid[row.day].append(r)
        skill[row.day].append(r - coin)
        coins.append(coin)

    def mean(by_day: dict[date, list[float]]) -> float:
        values = [v for day in by_day.values() for v in day]
        return statistics.fmean(values) if values else float("nan")

    return SignalHalf(
        rows=len(rows),
        days=len(paid),
        signal_r=mean(paid),
        coin_r=statistics.fmean(coins) if coins else float("nan"),
        excess=mean(skill),
        p_signal=clustered_p_value(paid, resamples=resamples),
        p_excess=clustered_p_value(skill, resamples=resamples),
    )


def signal_report(
    rows: Sequence[ResearchRow],
    *,
    top_fraction: float | None = None,
    train_fraction: float = 0.6,
    test_from: date | None = None,
    resamples: int = 2000,
    first_only: bool = False,
) -> SignalReport:
    """Score the rows a strategy signalled on, train and test, vs. a coin flip.

    ``top_fraction`` restricts to the cheapest markets, ranked on the **train**
    half by median ``tf_atr_spread`` over **all** their rows (signalled or not),
    so the market choice never sees the test half nor the signal's outcome.
    ``first_only`` keeps the first signalled row of each epic and day — the live
    one-open-per-epic-per-day — instead of every minute the signal holds.
    """
    train_days, _test_days = split_days(rows, train_fraction, test_from)
    train_set = set(train_days)
    markets: list[str] = []
    if top_fraction is not None:
        table = market_table(rows, train_fraction=train_fraction, test_from=test_from)
        markets = [m.epic for m in table[: max(1, round(len(table) * top_fraction))]]
    allowed = set(markets)
    signalled = [
        r for r in rows if r.signal and (top_fraction is None or r.epic in allowed)
    ]
    if first_only:
        seen: set[tuple[str, date]] = set()
        first: list[ResearchRow] = []
        for row in sorted(signalled, key=lambda r: r.timestamp):
            if (row.epic, row.day) not in seen:
                seen.add((row.epic, row.day))
                first.append(row)
        signalled = first
    return SignalReport(
        top_fraction=top_fraction,
        markets=markets,
        train=_signal_half([r for r in signalled if r.day in train_set], resamples),
        test=_signal_half([r for r in signalled if r.day not in train_set], resamples),
    )
