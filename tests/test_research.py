"""Tests for the research dataset and its train/test analysis (backtest/research)."""

from datetime import UTC, date, datetime, timedelta

import pytest

from src.backtest.research import (
    FEATURES,
    MIN_HISTORY,
    ResearchRow,
    analyze,
    bucket_of,
    build_rows,
    clustered_p_value,
    compute_features,
    cost_filter,
    decile_edges,
    is_significant,
    label_outcome,
    market_table,
    read_csv,
    resample,
    signal_report,
    split_days,
    write_csv,
)
from src.entry.base import EntryIntent
from src.feed.price_buffer import Candle

T0 = datetime(2026, 7, 7, 7, 0, tzinfo=UTC)


def _candle(i: int, bid: float, high: float | None = None, low: float | None = None):
    high = bid + 0.5 if high is None else high
    low = bid - 0.5 if low is None else low
    return Candle(
        timestamp=T0 + timedelta(minutes=i),
        bid_open=bid,
        bid_close=bid,
        bid_high=high,
        bid_low=low,
        offer_open=bid + 0.1,
        offer_close=bid + 0.1,
        offer_high=high + 0.1,
        offer_low=low + 0.1,
    )


def _flat(n: int, bid: float = 100.0) -> list[Candle]:
    return [_candle(i, bid) for i in range(n)]


class TestLabelOutcome:
    def test_buy_stop_first(self):
        candles = _flat(5) + [_candle(5, 97.0, low=94.0)]
        # Fill 100.1, stop 100 - 2.5×2 = 95 → risk 5.1; the low 94 hits it.
        assert label_outcome(candles, 4, "BUY", atr_value=2.0) == -1.0

    def test_buy_target(self):
        candles = _flat(5) + [_candle(5, 108.0, high=111.0)]
        # Target 100 + 5×2 = 110, fill 100.1, risk 5.1 → (110 − 100.1) / 5.1.
        r = label_outcome(candles, 4, "BUY", atr_value=2.0)
        assert r == pytest.approx((110 - 100.1) / 5.1)

    def test_both_touched_on_one_candle_counts_as_a_stop(self):
        candles = _flat(5) + [_candle(5, 100.0, high=111.0, low=94.0)]
        assert label_outcome(candles, 4, "BUY", atr_value=2.0) == -1.0

    def test_sell_mirrors_on_the_offer(self):
        # SELL fills at the bid 100, stop 100.1 + 5 = 105.1 (risk 5.1),
        # target 100.1 − 10 = 90.1; an offer low of 90.1 reaches it.
        candles = _flat(5) + [_candle(5, 91.0, low=90.0)]
        r = label_outcome(candles, 4, "SELL", atr_value=2.0)
        assert r == pytest.approx((100 - 90.1) / 5.1)

    def test_horizon_marks_to_market(self):
        candles = _flat(5) + [_candle(5 + i, 101.0) for i in range(10)]
        r = label_outcome(candles, 4, "BUY", atr_value=2.0, horizon=3)
        assert r == pytest.approx((101.0 - 100.1) / 5.1)

    def test_force_close_hour_stops_the_walk(self):
        start = datetime(2026, 7, 7, 16, 58, tzinfo=UTC)
        candles = [
            Candle(
                timestamp=start + timedelta(minutes=i),
                bid_open=b,
                bid_close=b,
                bid_high=b + 0.5,
                bid_low=b - 0.5,
                offer_open=b + 0.1,
                offer_close=b + 0.1,
                offer_high=b + 0.6,
                offer_low=b - 0.4,
            )
            for i, b in enumerate([100.0, 100.5, 120.0])  # the 17:00 spike is cut
        ]
        r = label_outcome(candles, 0, "BUY", atr_value=2.0, last_hour_utc=17)
        assert r == pytest.approx((100.5 - 100.1) / 5.1)

    def test_nothing_after_the_entry(self):
        assert label_outcome(_flat(3), 2, "BUY", atr_value=2.0) is None


class TestComputeFeatures:
    def _rising(self, n: int) -> list[Candle]:
        return [_candle(i, 100 + 0.1 * i) for i in range(n)]

    def test_needs_history(self):
        short = self._rising(MIN_HISTORY - 1)
        assert (
            compute_features("CC.D.X", short, day_open=100, day_high=110, day_low=99)
            is None
        )

    def test_values_on_a_clean_rise(self):
        candles = self._rising(120)
        f = compute_features(
            "CC.D.CC.UNC.IP", candles, day_open=100.0, day_high=112.4, day_low=99.5
        )
        assert set(f) == set(FEATURES)
        assert f["trend60_pct"] > 0 and f["r2_60"] == pytest.approx(1.0)
        assert f["er60"] == pytest.approx(1.0)
        assert f["chan60"] > 0.9
        assert f["roc30_atr"] > 0 and f["day_move_atr"] > 0
        assert f["asset_class"] == "CC.D"
        assert f["hour_paris"] == "10"  # last candle 08:59 UTC = 10:59 Paris
        assert f["weekday"] == "Tue"

    def test_no_look_ahead(self):
        # Appending future candles to the day must not change a past row.
        rows_short = list(build_rows({"CC.D.A.IP": self._rising(120)}, step=5))
        rows_long = list(build_rows({"CC.D.A.IP": self._rising(300)}, step=5))
        stamp = rows_short[0].timestamp
        first_long = next(r for r in rows_long if r.timestamp == stamp)
        assert first_long.features == rows_short[0].features


class TestBuildRows:
    def test_samples_on_the_step_inside_the_hours(self):
        rows = list(build_rows({"CC.D.A.IP": _flat_with_noise(400)}, step=10))
        assert rows
        assert all(r.timestamp.minute % 10 == 0 for r in rows)
        assert all(7 <= r.timestamp.hour < 17 for r in rows)

    def test_csv_round_trip(self, tmp_path):
        rows = list(build_rows({"CC.D.A.IP": _flat_with_noise(300)}, step=15))
        path = tmp_path / "ds.csv"
        assert write_csv(rows, path) == len(rows)
        back = read_csv(path)
        assert len(back) == len(rows)
        assert back[0].features["asset_class"] == "CC.D"
        assert back[0].r_buy == pytest.approx(rows[0].r_buy, abs=1e-4)


def _flat_with_noise(n: int) -> list[Candle]:
    return [_candle(i, 100 + (0.3 if i % 3 else -0.4)) for i in range(n)]


def _row(day: int, value: float, r_buy: float, r_sell: float = 0.0) -> ResearchRow:
    features = {name: 0.0 for name in FEATURES}
    features.update(
        {"trend60_pct": value, "hour_paris": "10", "weekday": "Mon", "asset_class": "X"}
    )
    return ResearchRow(
        epic="X",
        timestamp=datetime(2026, 7, 1, 10, tzinfo=UTC) + timedelta(days=day),
        features=features,
        r_buy=r_buy,
        r_sell=r_sell,
    )


class TestAnalysisHelpers:
    def test_split_days_in_time_order(self):
        rows = [_row(d, 0.0, 0.0) for d in (3, 0, 2, 1, 4)]
        train, test = split_days(rows, 0.6)
        assert train == [date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3)]
        assert test == [date(2026, 7, 4), date(2026, 7, 5)]

    def test_split_on_a_fixed_date(self):
        rows = [_row(d, 0.0, 0.0) for d in range(5)]
        train, test = split_days(rows, 0.9, test_from=date(2026, 7, 4))
        assert train == [date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3)]
        assert test == [date(2026, 7, 4), date(2026, 7, 5)]

    def test_analyze_refuses_an_empty_half(self):
        rows = [_row(d, 0.0, 0.0) for d in range(5)]
        with pytest.raises(ValueError):
            analyze(rows, test_from=date(2027, 1, 1))

    def test_deciles(self):
        edges = decile_edges(list(range(100)))
        assert len(edges) == 9 and edges[0] == 10
        assert bucket_of(5, edges) == 0
        assert bucket_of(95, edges) == 9

    def test_clustered_p(self):
        positive = {date(2026, 7, d): [1.0, 0.5] for d in range(1, 11)}
        assert clustered_p_value(positive) == 0.0
        negative = {date(2026, 7, d): [-1.0] for d in range(1, 11)}
        assert clustered_p_value(negative) == 1.0
        assert clustered_p_value({date(2026, 7, 1): [1.0]}) != clustered_p_value(
            {date(2026, 7, 1): [1.0]}
        )  # NaN with too few days


class TestAnalyze:
    def test_finds_a_planted_edge_on_both_halves(self):
        # Top trend decile pays +1 R on BUY every day; everything else pays −0.2.
        rows = []
        for day in range(30):
            for i in range(50):
                value = i / 50
                rows.append(_row(day, value, 1.0 if value >= 0.9 else -0.2))
        result = analyze(rows, train_fraction=0.5)
        top = [
            f
            for f in result.findings
            if f.feature == "trend60_pct" and f.direction == "BUY" and f.bucket == "D10"
        ]
        assert top and top[0].robust
        assert is_significant(top[0], result.alpha)
        # Nothing else on the BUY side of that feature survives.
        others = [
            f
            for f in result.findings
            if f.feature == "trend60_pct" and f.direction == "BUY" and f.bucket != "D10"
        ]
        assert not any(f.robust for f in others)

    def test_baseline_is_the_unfiltered_mean(self):
        rows = [_row(day, 0.0, 0.5, -0.5) for day in range(10)]
        result = analyze(rows, train_fraction=0.5)
        assert result.baseline["BUY"] == pytest.approx((0.5, 0.5))
        assert result.baseline["SELL"] == pytest.approx((-0.5, -0.5))


def _at(ts: datetime, bid: float, high: float | None = None, low: float | None = None):
    high = bid + 0.5 if high is None else high
    low = bid - 0.5 if low is None else low
    return Candle(
        timestamp=ts,
        bid_open=bid,
        bid_close=bid,
        bid_high=high,
        bid_low=low,
        offer_open=bid + 0.1,
        offer_close=bid + 0.1,
        offer_high=high + 0.1,
        offer_low=low + 0.1,
    )


class TestResample:
    def test_aggregates_bid_and_offer(self):
        candles = [_candle(i, 100 + i) for i in range(10)]
        bars = resample(candles, 5)
        assert len(bars) == 2
        first = bars[0]
        assert first.timestamp == candles[0].timestamp
        assert (first.bid_open, first.bid_close) == (100, 104)
        assert (first.bid_high, first.bid_low) == (104.5, 99.5)
        assert first.offer_close == pytest.approx(104.1)

    def test_last_bar_may_be_partial(self):
        bars = resample([_candle(i, 100.0) for i in range(7)], 5)
        assert len(bars) == 2 and bars[1].bid_close == 100.0

    def test_one_minute_is_identity(self):
        candles = _flat(3)
        assert resample(candles, 1) == candles


class TestScaleAndCost:
    def test_horizon_is_wall_clock_minutes(self):
        # A 3-hour gap in the feed is not walked through as 3 candles of time.
        t = datetime(2026, 7, 7, 9, 0, tzinfo=UTC)
        candles = [_at(t, 100.0), _at(t + timedelta(hours=3), 108.0, high=111.0)]
        r = label_outcome(candles, 0, "BUY", atr_value=2.0, horizon=120)
        assert r is None  # nothing inside the horizon

    def test_daily_close_also_cuts_at_midnight(self):
        t = datetime(2026, 7, 7, 9, 0, tzinfo=UTC)
        candles = [
            _at(t, 100.0),
            _at(t + timedelta(minutes=1), 100.5),
            _at(t + timedelta(days=1), 108.0, high=111.0),
        ]
        r = label_outcome(
            candles, 0, "BUY", atr_value=2.0, horizon=3000, last_hour_utc=24
        )
        assert r == pytest.approx((100.5 - 100.1) / 5.1)

    def test_overnight_holds_and_pays_funding(self):
        t = datetime(2026, 7, 7, 15, 0, tzinfo=UTC)
        candles = [_at(t, 100.0), _at(t + timedelta(days=2), 108.0, high=111.0)]
        free = label_outcome(
            candles, 0, "BUY", atr_value=2.0, horizon=4000, last_hour_utc=None
        )
        assert free == pytest.approx((110 - 100.1) / 5.1)
        funded = label_outcome(
            candles,
            0,
            "BUY",
            atr_value=2.0,
            horizon=4000,
            last_hour_utc=None,
            funding_per_night=0.001,
        )
        # Two nights × 0.1 % × 100 = 0.2 points of cost, in R.
        assert funded == pytest.approx(free - 0.2 / 5.1)

    def test_timeframe_widens_the_bracket(self):
        candles = _flat_with_noise(400)
        one = list(build_rows({"CC.D.A.IP": candles}, step=30))
        fifteen = list(build_rows({"CC.D.A.IP": candles}, step=30, timeframe=15))
        assert one and fifteen
        assert one[0].features["tf_atr_spread"] == pytest.approx(
            one[0].features["atr_spread"]
        )
        f = fifteen[-1].features
        assert f["tf_atr_spread"] > f["atr_spread"]


def _market_rows(epic: str, ratio: float, r: float, days: int = 20) -> list:
    rows = []
    for day in range(days):
        row = _row(day, 0.0, r, r)
        row.epic = epic
        row.features["tf_atr_spread"] = ratio
        rows.append(row)
    return rows


class TestMarketSelection:
    def _rows(self):
        return (
            _market_rows("CHEAP", 5.0, 0.3)
            + _market_rows("MID", 1.0, -0.2)
            + _market_rows("DEAR", 0.3, -0.6)
        )

    def test_table_is_cheapest_first(self):
        table = market_table(self._rows(), train_fraction=0.5)
        assert [m.epic for m in table] == ["CHEAP", "MID", "DEAR"]
        assert table[0].cost_ratio == 5.0
        assert table[0].test_buy == pytest.approx(0.3)

    def test_cost_filter_keeps_the_cheapest_and_scores_them_on_test(self):
        result = cost_filter(self._rows(), top_fraction=0.34, train_fraction=0.5)
        assert result.kept == ["CHEAP"]
        assert result.test_buy == pytest.approx(0.3)
        assert result.test_p_buy == 0.0
        assert result.rest_buy == pytest.approx(-0.4)

    def test_old_dataset_without_tf_column_reads(self, tmp_path):
        path = tmp_path / "old.csv"
        rows = list(build_rows({"CC.D.A.IP": _flat_with_noise(300)}, step=15))
        write_csv(rows, path)
        text = path.read_text().splitlines()
        header = text[0].split(",")
        col = header.index("tf_atr_spread")
        trimmed = [
            ",".join(v for i, v in enumerate(line.split(",")) if i != col)
            for line in text
        ]
        path.write_text("\n".join(trimmed) + "\n")
        back = read_csv(path)
        assert back[0].features["tf_atr_spread"] == back[0].features["atr_spread"]


class _EveryMinute:
    """Toy entry: BUY on even minutes, SELL on odd ones (long-only unless told)."""

    name = "toy"
    warmup = 5

    def __init__(self, emits_shorts: bool = True) -> None:
        self.emits_shorts = emits_shorts
        self.seen: list[int] = []

    def evaluate(self, epic, buf):
        self.seen.append(len(buf))
        minute = buf.candles[-1].timestamp.minute
        return EntryIntent(epic=epic, direction="BUY" if minute % 2 == 0 else "SELL")


class TestStrategySignal:
    def test_rows_carry_the_strategy_side(self):
        entry = _EveryMinute()
        rows = list(
            build_rows({"CC.D.A.IP": _flat_with_noise(300)}, step=5, entry=entry)
        )
        assert {r.signal for r in rows} == {"BUY", "SELL"}
        assert all(
            r.signal == ("BUY" if r.timestamp.minute % 2 == 0 else "SELL") for r in rows
        )

    def test_the_buffer_is_live_sized_and_never_sees_ahead(self):
        entry = _EveryMinute()
        rows = list(
            build_rows({"CC.D.A.IP": _flat_with_noise(400)}, step=5, entry=entry)
        )
        assert rows and max(entry.seen) == 200  # DEFAULT_MAX_CANDLES, as live

    def test_long_only_strategy_drops_its_sells(self):
        rows = list(
            build_rows(
                {"CC.D.A.IP": _flat_with_noise(300)},
                step=5,
                entry=_EveryMinute(emits_shorts=False),
            )
        )
        assert {r.signal for r in rows} == {"BUY", ""}

    def test_no_strategy_no_signal_and_csv_round_trip(self, tmp_path):
        rows = list(build_rows({"CC.D.A.IP": _flat_with_noise(300)}, step=15))
        assert {r.signal for r in rows} == {""}
        signalled = list(
            build_rows(
                {"CC.D.A.IP": _flat_with_noise(300)}, step=15, entry=_EveryMinute()
            )
        )
        path = tmp_path / "s.csv"
        write_csv(signalled, path)
        assert [r.signal for r in read_csv(path)] == [r.signal for r in signalled]


class TestSignalReport:
    def _rows(self):
        rows = []
        for day in range(20):
            # The signal is always on the side that paid +1; the other paid −1.
            up = _row(day, 0.0, 1.0, -1.0)
            up.signal = "BUY"
            down = _row(day, 0.0, -1.0, 1.0)
            down.signal = "SELL"
            flat = _row(day, 0.0, 5.0, 5.0)  # unsignalled: must be ignored
            rows += [up, down, flat]
        return rows

    def test_skill_is_measured_against_a_coin_on_the_same_rows(self):
        report = signal_report(self._rows(), train_fraction=0.5)
        for half in (report.train, report.test):
            assert half.rows == 20 and half.days == 10
            assert half.signal_r == pytest.approx(1.0)
            assert half.coin_r == pytest.approx(0.0)
            assert half.excess == pytest.approx(1.0)
            assert half.p_excess == 0.0

    def test_first_only_keeps_one_row_per_epic_and_day(self):
        report = signal_report(self._rows(), train_fraction=0.5, first_only=True)
        assert report.train.rows == 10 and report.test.rows == 10

    def test_cheapest_markets_are_ranked_on_all_rows_of_the_train_half(self):
        rows = self._rows()
        for r in rows:
            r.features["tf_atr_spread"] = 2.0
        other = _market_rows("DEAR", 0.1, 0.0)
        for r in other:
            r.signal = "BUY"
        report = signal_report(rows + other, top_fraction=0.5, train_fraction=0.5)
        assert report.markets == ["X"]
        assert report.test.rows == 20
