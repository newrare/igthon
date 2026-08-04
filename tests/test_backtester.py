"""Tests for the historical backtester and its web routes.

The backtester replays the project's real open/close rules over archived
candles. These tests check the day-grouping helper, that runs are deterministic
and internally consistent (every trade closed, P&L adds up), and that the
``/backtest`` API loads the archive and replays it.
"""

import csv
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from src.backtest.backtester import (
    SELECTION_REGISTRIES,
    BacktestConfig,
    StrategySelection,
    build_days,
    dedupe_correlated_epics,
    euro_summary,
    percentage_summary,
    run_backtest,
    trade_euro,
    trade_euro_breakeven,
    trade_return_pct,
)
from src.backtest.contract_values import ContractTable
from src.backtest.curve_generator import generate_curve
from src.backtest.simulator import StrategySimulator
from src.entry.base import EntryIntent, EntryStrategy
from src.execution.trading import TradeConfig
from src.exit import get_close_profile
from src.feed.candle_store import _DUMP_FIELDS
from src.feed.price_buffer import Candle, PriceBuffer
from src.web.app import create_app


def _settings(dump_dir="./dumps", contract_file="") -> SimpleNamespace:
    """Settings stand-in with the strategy attributes the engine reads."""
    return SimpleNamespace(
        ig_env=SimpleNamespace(value="demo"),
        web_port=8000,
        candle_dump_dir=str(dump_dir),
        backtest_contract_file=str(contract_file),
        open_strategy="open_donchian",
        stop_strategy="stop_support",
        close_zonestart="hold",
        close_zonemarge="hold",
        close_zonesecure="hold",
        close_zoneprofit="trailing_ratchet",
        strategy_close_margin_minutes=5,
        strategy_atr_period=14,
        strategy_atr_k_pre=2.5,
        strategy_atr_k_post=1.5,
        strategy_trailing_step_ratio=0.3,
    )


def _candles_for(epic_seed: int, day: datetime, profile: str = "volatile"):
    """Realistic one-day curve, stamped on ``day`` (07:00 UTC start)."""
    return generate_curve(profile, seed=epic_seed, num_candles=600, day=day)


def _archive_candles(seeds_days) -> dict[str, list[Candle]]:
    """Build a ``candles_by_epic`` map from (epic, seed, day) triples."""
    out: dict[str, list[Candle]] = {}
    for epic, seed, day in seeds_days:
        out.setdefault(epic, []).extend(_candles_for(seed, day))
    return out


class TestBuildDays:
    def test_groups_by_calendar_date(self):
        d1 = datetime(2026, 6, 8, tzinfo=UTC)
        d2 = datetime(2026, 6, 9, tzinfo=UTC)
        candles = _archive_candles(
            [("EPIC.A", 1, d1), ("EPIC.B", 2, d1), ("EPIC.A", 3, d2)]
        )

        days = build_days(candles)

        assert len(days) == 2  # two distinct dates
        # Day one holds both epics; day two only EPIC.A.
        day_one = {epic for epic, _ in days[0]}
        day_two = {epic for epic, _ in days[1]}
        assert day_one == {"EPIC.A", "EPIC.B"}
        assert day_two == {"EPIC.A"}

    def test_each_subseries_sorted(self):
        day = datetime(2026, 6, 8, tzinfo=UTC)
        # Hand-build out-of-order candles for one epic on one day.
        unordered = [
            Candle(day + timedelta(minutes=m), 1, 1, 1, 1, 2, 2, 2, 2)
            for m in (5, 1, 3)
        ]
        days = build_days({"E": unordered})
        _, series = days[0][0]
        assert [c.timestamp.minute for c in series] == [1, 3, 5]

    def test_empty_input(self):
        assert build_days({}) == []


class TestDedupeEpics:
    """Correlated duplicate contracts collapse to one epic per underlying."""

    def test_underlying_key(self):
        from src.backtest.backtester import _underlying

        assert _underlying("IX.D.DAX.IDF.IP") == "DAX"
        assert _underlying("IX.D.DAX.IMF.IP") == "DAX"
        assert _underlying("CS.D.EURUSD.CEFM.IP") == "EURUSD"
        assert _underlying("nodots") == "nodots"

    def test_keeps_richest_per_underlying(self):
        day = datetime(2026, 6, 8, tzinfo=UTC)
        candles = {
            "IX.D.DAX.IDF.IP": _candles_for(1, day),  # 600 candles
            "IX.D.DAX.IMF.IP": _candles_for(2, day)[:100],  # shorter -> dropped
            "CS.D.EURUSD.CEF.IP": _candles_for(3, day),  # different underlying
        }
        kept, dropped = dedupe_correlated_epics(candles)
        assert set(kept) == {"IX.D.DAX.IDF.IP", "CS.D.EURUSD.CEF.IP"}
        assert dropped == ["IX.D.DAX.IMF.IP"]

    def test_no_duplicates_keeps_everything(self):
        day = datetime(2026, 6, 8, tzinfo=UTC)
        candles = {
            "IX.D.DAX.IDF.IP": _candles_for(1, day),
            "IX.D.FTSE.CFD.IP": _candles_for(2, day),
        }
        kept, dropped = dedupe_correlated_epics(candles)
        assert set(kept) == set(candles)
        assert dropped == []


class TestPercentageSummary:
    """P&L reported as percentage return computed from the fill prices."""

    @staticmethod
    def _t(open_level, close_level):
        return SimpleNamespace(level_open=open_level, level_close=close_level)

    def test_trade_return_pct(self):
        assert trade_return_pct(self._t(100.0, 101.0)) == pytest.approx(1.0)
        assert trade_return_pct(self._t(100.0, 99.0)) == pytest.approx(-1.0)
        # Forex-scale move stays visible (unlike a euro_per_point=1 figure).
        assert trade_return_pct(self._t(1.15594, 1.15551)) == pytest.approx(
            -0.0372, abs=1e-4
        )

    def test_return_pct_zero_when_unusable(self):
        assert trade_return_pct(self._t(0.0, 5.0)) == 0.0
        assert trade_return_pct(self._t(100.0, None)) == 0.0

    def test_summary_aggregates(self):
        trades = [self._t(100, 101), self._t(100, 99), self._t(100, 102)]  # +1,-1,+2
        s = percentage_summary(trades)
        assert s["total_return_pct"] == pytest.approx(2.0)
        assert s["avg_win_pct"] == pytest.approx(1.5)
        assert s["avg_loss_pct"] == pytest.approx(-1.0)
        assert s["best_pct"] == pytest.approx(2.0)
        assert s["worst_pct"] == pytest.approx(-1.0)
        assert s["equity_pct"] == [1.0, 0.0, 2.0]
        assert s["max_drawdown_pct"] == pytest.approx(1.0)  # peak 1 -> 0

    def test_empty(self):
        s = percentage_summary([])
        assert s["total_return_pct"] == 0.0 and s["equity_pct"] == []


#: One archived day whose curves are known to produce trades (the same pair the
#: route fixtures use), for the assertions that need a non-empty trade list.
_TRADING_DAY = [
    ("EPIC.A", 1, datetime(2026, 6, 8, tzinfo=UTC)),
    ("EPIC.B", 2, datetime(2026, 6, 8, tzinfo=UTC)),
]


class TestRunBacktest:
    def test_deterministic(self):
        days = [
            ("EPIC.A", 11, datetime(2026, 6, 8, tzinfo=UTC)),
            ("EPIC.B", 22, datetime(2026, 6, 8, tzinfo=UTC)),
            ("EPIC.A", 33, datetime(2026, 6, 9, tzinfo=UTC)),
        ]
        cfg = BacktestConfig(target_trades=50)
        a = run_backtest(_settings(), _archive_candles(days), cfg)
        b = run_backtest(_settings(), _archive_candles(days), cfg)
        assert [t.euro for t in a.trades] == [t.euro for t in b.trades]

    def test_every_trade_closed_and_consistent(self):
        days = [("EPIC.A", s, datetime(2026, 6, 8, tzinfo=UTC)) for s in range(6)]
        result = run_backtest(
            _settings(), _archive_candles(days), BacktestConfig(target_trades=50)
        )
        for t in result.trades:
            assert t.reason_close in {"win", "loose", "stop", "follower", "end_of_day"}
            assert t.level_close is not None and t.euro is not None
            assert t.win == (t.euro > 0)
            assert t.euro == pytest.approx(t.level_close - t.level_open, abs=0.01)

    def test_summary_adds_up(self):
        days = [("EPIC.A", s, datetime(2026, 6, 8, tzinfo=UTC)) for s in range(6)]
        result = run_backtest(
            _settings(), _archive_candles(days), BacktestConfig(target_trades=50)
        )
        s = result.summary()
        assert s["wins"] + s["losses"] == s["trades"]
        assert len(s["equity"]) == s["trades"]
        assert sum(s["close_reasons"].values()) == s["trades"]

    def test_empty_candles_no_trades(self):
        result = run_backtest(_settings(), {}, BacktestConfig())
        assert result.trades == []
        assert result.days_simulated == 0

    def test_selection_overrides_are_replayed(self):
        """A different stop policy must produce a different initial stop."""
        candles = _archive_candles(_TRADING_DAY)
        support = run_backtest(
            _settings(),
            candles,
            BacktestConfig(),
            StrategySelection(stop_strategy="stop_support"),
        )
        atr_stop = run_backtest(
            _settings(),
            candles,
            BacktestConfig(),
            StrategySelection(stop_strategy="stop_atr"),
        )
        assert support.trades and atr_stop.trades
        assert [t.level_stop for t in support.trades] != [
            t.level_stop for t in atr_stop.trades
        ]

    def test_breakeven_crossing_is_recorded_above_level_zero(self):
        result = run_backtest(
            _settings(), _archive_candles(_TRADING_DAY), BacktestConfig()
        )
        assert result.trades  # the fixture curves do trade
        for t in result.trades:
            if t.level_breakeven_exit is not None:
                assert t.level_breakeven_exit > t.level_zero
                assert t.time_breakeven_exit is not None
            else:
                # Never went green, so the real close cannot be a win either.
                assert not t.win


class _FixedIntentEntry(EntryStrategy):
    """Entry stub firing one fixed direction as soon as the warmup is covered.

    Lets the SELL mechanics be pinned without depending on a real strategy's
    parameters: ``emits_shorts`` is the same opt-in flag the live scheduler reads.
    """

    name = "stub_fixed"
    warmup = 30

    def __init__(self, direction: str, *, emits_shorts: bool) -> None:
        self.direction = direction
        self.emits_shorts = emits_shorts

    @classmethod
    def from_settings(cls, settings) -> "_FixedIntentEntry":
        return cls("BUY", emits_shorts=False)

    def evaluate(self, epic: str, buf) -> EntryIntent | None:
        return EntryIntent(epic=epic, direction=self.direction, score=1.0)


def _run_with_entry(entry, candles=None, settings=None):
    """Replay archived candles through an explicit entry (bypassing OPEN_STRATEGY)."""
    settings = settings or _settings()
    simulator = StrategySimulator(
        trade_config=TradeConfig.from_settings(settings),
        entry=entry,
        close_profile=get_close_profile(settings),
        sim_config=BacktestConfig().to_simulation_config(),
    )
    return simulator.run_days(build_days(candles or _archive_candles(_TRADING_DAY)))


class TestShortSide:
    """SELL is replayed exactly when the live path would keep it."""

    def test_short_only_entry_opens_shorts(self):
        result = _run_with_entry(_FixedIntentEntry("SELL", emits_shorts=True))
        assert result.trades
        assert {t.direction for t in result.trades} == {"SELL"}

    def test_sell_is_dropped_without_the_emits_shorts_opt_in(self):
        """Mirror of the live gate: a long-only strategy cannot short by accident."""
        result = _run_with_entry(_FixedIntentEntry("SELL", emits_shorts=False))
        assert result.trades == []
        assert result.buy_signals == 0
        assert result.rejections == Counter()  # dropped before the gate, as live does

    def test_short_fills_at_the_bid_and_closes_on_the_offer(self):
        result = _run_with_entry(_FixedIntentEntry("SELL", emits_shorts=True))
        for t in result.trades:
            # Filled at the bid, so break-even (close-out terms) is that same bid.
            assert t.level_open == pytest.approx(t.level_zero)
            # The initial stop of a short sits ABOVE the entry.
            assert t.level_stop > t.level_open

    def test_short_pnl_is_signed(self):
        result = _run_with_entry(_FixedIntentEntry("SELL", emits_shorts=True))
        assert result.trades
        for t in result.trades:
            assert t.euro == pytest.approx(t.level_open - t.level_close, abs=0.01)
            assert t.win == (t.euro > 0)
            # A profitable short closed BELOW its entry, so the raw price move is
            # negative while the return is positive.
            assert (trade_return_pct(t) > 0) == (t.level_close < t.level_open)

    def test_short_breakeven_crossing_is_below_level_zero(self):
        result = _run_with_entry(_FixedIntentEntry("SELL", emits_shorts=True))
        crossed = [t for t in result.trades if t.level_breakeven_exit is not None]
        assert crossed  # a full day of curves has some short go green
        for t in crossed:
            assert t.level_breakeven_exit < t.level_zero

    def test_short_euro_and_scenario_are_signed(self):
        table = _table_from({"EPIC.A": 2.0, "EPIC.B": 2.0})
        result = _run_with_entry(_FixedIntentEntry("SELL", emits_shorts=True))
        s = euro_summary(result.trades, table)
        assert s["priced_trades"] == len(result.trades)
        expected = sum((t.level_open - t.level_close) * 2.0 for t in result.trades)
        assert s["total_euro"] == pytest.approx(expected, abs=0.01)
        assert s["wins_breakeven"] + s["losses_breakeven"] == len(result.trades)

    def test_long_side_is_unchanged_by_short_support(self):
        """A BUY stub still fills at the offer and breaks even there."""
        result = _run_with_entry(_FixedIntentEntry("BUY", emits_shorts=True))
        assert result.trades
        for t in result.trades:
            assert t.direction == "BUY"
            assert t.level_open == pytest.approx(t.level_zero)
            assert t.level_stop < t.level_open
            assert t.euro == pytest.approx(t.level_close - t.level_open, abs=0.01)


class TestStrategySelection:
    """The six selectors are overlaid on settings, not rebuilt from scratch."""

    def test_unset_fields_fall_back_to_settings(self):
        resolved = StrategySelection(stop_strategy="stop_atr").resolve(_settings())
        assert resolved["stop_strategy"] == "stop_atr"
        assert resolved["open_strategy"] == "open_donchian"
        assert resolved["close_zoneprofit"] == "trailing_ratchet"

    def test_apply_keeps_unrelated_parameters(self):
        effective = StrategySelection(close_zonemarge="limitloose").apply(_settings())
        assert effective.close_zonemarge == "limitloose"
        assert effective.strategy_atr_period == 14  # tuning parameters pass through

    def test_no_override_returns_settings_unchanged(self):
        settings = _settings()
        assert StrategySelection().apply(settings) is settings

    def test_unknown_names_are_reported_per_selector(self):
        bad = StrategySelection(
            open_strategy="nope", close_zonesecure="also_nope"
        ).problems(_settings())
        assert set(bad) == {"open_strategy", "close_zonesecure"}
        assert "nope" in bad["open_strategy"]

    def test_a_clean_selection_has_no_problem(self):
        assert StrategySelection().problems(_settings()) == {}

    def test_untestable_name_is_a_problem(self):
        bad = StrategySelection(close_zonestart="smartgroup").problems(_settings())
        assert "not backtestable" in bad["close_zonestart"]

    def test_untestable_live_value_is_a_problem_too(self):
        """An unreplayable .env value must fail, not be replayed as a look-alike."""
        settings = _settings()
        settings.close_zonestart = "smartgroup"
        bad = StrategySelection().problems(settings)
        assert "not backtestable" in bad["close_zonestart"]
        # Overriding it explicitly makes the run usable again.
        assert StrategySelection(close_zonestart="hold").problems(settings) == {}

    def test_every_selector_has_a_registry(self):
        assert set(SELECTION_REGISTRIES) == {
            "open_strategy",
            "stop_strategy",
            "close_zonestart",
            "close_zonemarge",
            "close_zonesecure",
            "close_zoneprofit",
        }
        assert all(SELECTION_REGISTRIES.values())


def _trade(epic="EPIC.A", *, open_=100.0, close=110.0, breakeven=None):
    """Minimal closed-trade stand-in for the euro helpers."""
    return SimpleNamespace(
        epic=epic,
        level_open=open_,
        level_close=close,
        level_breakeven_exit=breakeven,
    )


class TestContractTable:
    def _write(self, tmp_path, payload):
        path = tmp_path / "epp.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_reads_euro_per_point(self, tmp_path):
        path = self._write(
            tmp_path,
            {
                "generated_at": "2026-08-03T07:00:00+00:00",
                "epics": {"EPIC.A": {"euro_per_point": 2.5, "currency": "EUR"}},
            },
        )
        table = ContractTable.load(path)
        assert table.euro_per_point("EPIC.A") == 2.5
        assert table.generated_at.startswith("2026-08-03")
        assert len(table) == 1

    def test_missing_file_is_an_empty_table(self, tmp_path):
        table = ContractTable.load(tmp_path / "absent.json")
        assert len(table) == 0
        assert table.euro_per_point("EPIC.A") is None

    def test_unset_path_is_an_empty_table(self):
        assert len(ContractTable.load(None)) == 0
        assert len(ContractTable.load("")) == 0

    def test_invalid_json_is_an_empty_table(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        assert len(ContractTable.load(path)) == 0

    def test_malformed_entries_are_skipped(self, tmp_path):
        path = self._write(
            tmp_path,
            {
                "epics": {
                    "GOOD": {"euro_per_point": 1.0},
                    "NEGATIVE": {"euro_per_point": -1.0},
                    "TEXT": {"euro_per_point": "n/a"},
                    "EMPTY": {},
                    "NOT_A_DICT": 3,
                }
            },
        )
        table = ContractTable.load(path)
        assert len(table) == 1
        assert table.euro_per_point("GOOD") == 1.0

    def test_missing_lists_the_unpriced_epics(self, tmp_path):
        path = self._write(tmp_path, {"epics": {"EPIC.A": {"euro_per_point": 1.0}}})
        table = ContractTable.load(path)
        assert table.missing(["EPIC.A", "EPIC.B", "EPIC.B"]) == ["EPIC.B"]


class TestEuroSummary:
    """Euros are priced per epic; unpriced epics are excluded, never guessed."""

    def _table(self, **epps) -> ContractTable:
        return ContractTable.load(None) if not epps else _table_from(epps)

    def test_total_is_priced_per_epic(self):
        table = _table_from({"EPIC.A": 2.0, "EPIC.B": 0.5})
        trades = [
            _trade("EPIC.A", open_=100.0, close=110.0),  # +10 pts × 2 = +20 €
            _trade("EPIC.B", open_=100.0, close=90.0),  # -10 pts × 0.5 = -5 €
        ]
        s = euro_summary(trades, table)
        assert s["total_euro"] == pytest.approx(15.0)
        assert s["priced_trades"] == 2
        assert s["unpriced_trades"] == 0

    def test_unpriced_epics_are_excluded_and_reported(self):
        table = _table_from({"EPIC.A": 1.0})
        trades = [
            _trade("EPIC.A", open_=100.0, close=105.0),
            _trade("EPIC.Z", open_=100.0, close=200.0),  # not in the table
        ]
        s = euro_summary(trades, table)
        assert s["total_euro"] == pytest.approx(5.0)
        assert s["priced_trades"] == 1
        assert s["unpriced_trades"] == 1
        assert s["unpriced_epics"] == ["EPIC.Z"]

    def test_breakeven_scenario_cuts_winners_short(self):
        table = _table_from({"EPIC.A": 1.0})
        # Ran to +10 but had crossed break-even at +1.
        trades = [_trade("EPIC.A", open_=100.0, close=110.0, breakeven=101.0)]
        s = euro_summary(trades, table)
        assert s["total_euro"] == pytest.approx(10.0)
        assert s["total_euro_breakeven"] == pytest.approx(1.0)
        assert s["breakeven_crossed"] == 1
        assert (s["wins_breakeven"], s["losses_breakeven"]) == (1, 0)

    def test_breakeven_scenario_leaves_non_crossers_alone(self):
        table = _table_from({"EPIC.A": 1.0})
        trades = [_trade("EPIC.A", open_=100.0, close=95.0, breakeven=None)]
        s = euro_summary(trades, table)
        assert s["total_euro"] == pytest.approx(-5.0)
        assert s["total_euro_breakeven"] == pytest.approx(-5.0)
        assert s["breakeven_crossed"] == 0
        assert (s["wins_breakeven"], s["losses_breakeven"]) == (0, 1)

    def test_equity_curves_and_drawdown(self):
        table = _table_from({"EPIC.A": 1.0})
        trades = [
            _trade("EPIC.A", open_=100.0, close=110.0),  # +10
            _trade("EPIC.A", open_=100.0, close=96.0),  # -4
            _trade("EPIC.A", open_=100.0, close=102.0),  # +2
        ]
        s = euro_summary(trades, table)
        assert s["equity_euro"] == [10.0, 6.0, 8.0]
        assert s["max_drawdown_euro"] == pytest.approx(4.0)
        assert s["best_euro"] == pytest.approx(10.0)
        assert s["worst_euro"] == pytest.approx(-4.0)

    def test_empty(self):
        s = euro_summary([], ContractTable.load(None))
        assert s["total_euro"] == 0.0 and s["equity_euro"] == []

    def test_trade_helpers_return_none_when_unpriced(self):
        trade = _trade("EPIC.A", open_=100.0, close=110.0, breakeven=101.0)
        assert trade_euro(trade, None) is None
        assert trade_euro_breakeven(trade, None) is None


def _table_from(epps: dict[str, float]) -> ContractTable:
    """In-memory ContractTable from an ``{epic: € per point}`` map."""
    from src.backtest.contract_values import ContractValue

    return ContractTable(
        {
            epic: ContractValue(epic=epic, euro_per_point=value)
            for epic, value in epps.items()
        }
    )


def _write_week_archive(dump_dir, week_name: str, candles_by_epic) -> None:
    """Persist a candles_by_epic map to a dump-schema CSV file."""
    dump_dir.mkdir(parents=True, exist_ok=True)
    path = dump_dir / f"candles_{week_name}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_DUMP_FIELDS)
        for epic, candles in candles_by_epic.items():
            for c in candles:
                writer.writerow(
                    [
                        epic,
                        c.timestamp.isoformat(),
                        c.bid_open,
                        c.bid_close,
                        c.bid_high,
                        c.bid_low,
                        c.offer_open,
                        c.offer_close,
                        c.offer_high,
                        c.offer_low,
                        c.volume,
                    ]
                )


class TestBacktestDedupRoute:
    """The run endpoint collapses correlated contracts and reports the drops."""

    @pytest.fixture
    def client(self, tmp_path):
        day = datetime(2026, 6, 8, tzinfo=UTC)
        candles = _archive_candles(
            [
                ("IX.D.DAX.IDF.IP", 1, day),
                ("IX.D.DAX.IMF.IP", 2, day),  # same underlying -> dropped
                ("CS.D.EURUSD.CEF.IP", 3, day),
            ]
        )
        _write_week_archive(tmp_path, "2026-W24", candles)
        app = create_app(settings=_settings(tmp_path), buffer=PriceBuffer())
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")

    @pytest.mark.asyncio
    async def test_run_dedupes_correlated_contracts(self, client):
        resp = await client.post("/api/backtest/run", json={"weeks": ["2026-W24"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["epics_loaded"] == 2  # one DAX + EURUSD
        assert len(data["epics_dropped"]) == 1
        assert data["epics_dropped"][0].startswith("IX.D.DAX")


class TestBacktestRoutes:
    @pytest.fixture
    def dump_dir(self, tmp_path):
        # Week 2026-W24 (Mon 2026-06-08).
        candles = _archive_candles(
            [
                ("EPIC.A", 1, datetime(2026, 6, 8, tzinfo=UTC)),
                ("EPIC.B", 2, datetime(2026, 6, 8, tzinfo=UTC)),
            ]
        )
        _write_week_archive(tmp_path, "2026-W24", candles)
        return tmp_path

    @pytest.fixture
    def client(self, dump_dir):
        app = create_app(settings=_settings(dump_dir), buffer=PriceBuffer())
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")

    @pytest.mark.asyncio
    async def test_page_renders(self, client):
        resp = await client.get("/backtest")
        assert resp.status_code == 200
        assert "Strategy Backtest" in resp.text
        assert "Archived data" in resp.text

    @pytest.mark.asyncio
    async def test_page_has_no_epic_picker_nor_trade_target(self, client):
        """A run always covers every epic and every day — no narrowing controls."""
        resp = await client.get("/backtest")
        assert 'class="bt-epic"' not in resp.text
        assert "bt-target" not in resp.text

    @pytest.mark.asyncio
    async def test_page_omits_untestable_strategies(self, client):
        """open_manual / open_testing carry no signal: not offered for backtest."""
        resp = await client.get("/backtest")
        assert 'value="open_donchian"' in resp.text
        assert 'value="open_manual"' not in resp.text
        assert 'value="open_testing"' not in resp.text

    @pytest.mark.asyncio
    async def test_page_offers_the_six_selectors(self, client):
        """The page mirrors .env: open, stop and the four close zones."""
        resp = await client.get("/backtest")
        for selector in SELECTION_REGISTRIES:
            assert f'id="bt-{selector}"' in resp.text
        # Each one starts on the live value, marked as such.
        assert 'value="open_donchian" selected' in resp.text
        assert 'value="trailing_ratchet" selected' in resp.text

    @pytest.mark.asyncio
    async def test_run_endpoint_rejects_untestable_strategy(self, client):
        resp = await client.post(
            "/api/backtest/run",
            json={"weeks": ["2026-W24"], "strategy": "open_manual"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_datasets_endpoint(self, client):
        resp = await client.get("/api/backtest/datasets")
        assert resp.status_code == 200
        weeks = resp.json()["weeks"]
        assert len(weeks) == 1
        assert weeks[0]["week"] == "2026-W24"
        assert {e["epic"] for e in weeks[0]["epics"]} == {"EPIC.A", "EPIC.B"}

    @pytest.mark.asyncio
    async def test_run_endpoint(self, client):
        resp = await client.post("/api/backtest/run", json={"weeks": ["2026-W24"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["candles_loaded"] > 0
        assert data["epics_loaded"] == 2
        s = data["summary"]
        assert s["wins"] + s["losses"] == s["trades"]
        assert len(data["trades"]) == s["trades"]
        # Percentage returns stay available as the instrument-agnostic lens.
        assert "total_return_pct" in s
        assert len(s["equity_pct"]) == s["trades"]
        # Euro figures are reported too, real and break-even scenario.
        assert {"total_euro", "total_euro_breakeven", "wins_breakeven"} <= s.keys()
        if data["trades"]:
            assert {"return_pct", "euro", "euro_breakeven"} <= data["trades"][0].keys()

    @pytest.mark.asyncio
    async def test_run_endpoint_echoes_the_full_selection(self, client):
        """The response states the six selectors the run actually replayed."""
        resp = await client.post("/api/backtest/run", json={"weeks": ["2026-W24"]})
        selection = resp.json()["selection"]
        assert selection == {
            "open_strategy": "open_donchian",
            "stop_strategy": "stop_support",
            "close_zonestart": "hold",
            "close_zonemarge": "hold",
            "close_zonesecure": "hold",
            "close_zoneprofit": "trailing_ratchet",
        }

    @pytest.mark.asyncio
    async def test_run_endpoint_applies_overridden_selectors(self, client):
        """An overridden zone/stop is echoed back, the rest stays on the live value."""
        resp = await client.post(
            "/api/backtest/run",
            json={
                "weeks": ["2026-W24"],
                "stop_strategy": "stop_atr",
                "close_zonemarge": "breakeven_lock",
            },
        )
        assert resp.status_code == 200
        selection = resp.json()["selection"]
        assert selection["stop_strategy"] == "stop_atr"
        assert selection["close_zonemarge"] == "breakeven_lock"
        assert selection["open_strategy"] == "open_donchian"  # untouched
        assert selection["close_zonestart"] == "hold"  # untouched

    @pytest.mark.asyncio
    async def test_run_endpoint_rejects_untestable_zone(self, client):
        """smartgroup needs the live cross-position pre-pass: refuse, don't fake it."""
        resp = await client.post(
            "/api/backtest/run",
            json={"weeks": ["2026-W24"], "close_zonestart": "smartgroup"},
        )
        assert resp.status_code == 400
        assert "smartgroup" in resp.json()["error"]

    @pytest.mark.asyncio
    async def test_page_shows_untestable_live_value_as_disabled(self, dump_dir):
        """A live smartgroup is listed (so .env stays legible) but unselectable."""
        settings = _settings(dump_dir)
        settings.close_zonestart = "smartgroup"
        app = create_app(settings=settings, buffer=PriceBuffer())
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            page = (await client.get("/backtest")).text
        assert 'value="smartgroup" disabled' in page
        assert "live — not backtestable" in page

    @pytest.mark.asyncio
    async def test_run_endpoint_rejects_unknown_zone(self, client):
        resp = await client.post(
            "/api/backtest/run",
            json={"weeks": ["2026-W24"], "close_zonesecure": "nope"},
        )
        assert resp.status_code == 400
        assert "close_zonesecure" in resp.json()["error"]

    @pytest.mark.asyncio
    async def test_run_endpoint_no_data_is_400(self, client):
        resp = await client.post("/api/backtest/run", json={"weeks": ["1999-W01"]})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_run_endpoint_rejects_unknown_strategy(self, client):
        resp = await client.post(
            "/api/backtest/run", json={"weeks": ["2026-W24"], "strategy": "nope"}
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_export_endpoint_503_without_store(self, client):
        # The default app fixture has no candle store wired.
        resp = await client.post("/api/backtest/export")
        assert resp.status_code == 503


class TestExportEndpoint:
    """The /api/backtest/export snapshot path with a real candle store."""

    @pytest.fixture
    async def store_and_dir(self, tmp_path):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from src.feed.candle_store import CandleStore
        from src.models.database import Base

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        store = CandleStore(factory, dump_dir=tmp_path, retention_days=7)
        # Recent candles (inside the retention window) -> not yet purged to files.
        day = datetime(2026, 6, 8, tzinfo=UTC)
        await store.save("EPIC.A", _candles_for(1, day))
        await store.save("EPIC.B", _candles_for(2, day))
        yield store, tmp_path
        await engine.dispose()

    @pytest.fixture
    def client(self, store_and_dir):
        store, dump_dir = store_and_dir
        app = create_app(
            settings=_settings(dump_dir),
            buffer=PriceBuffer(),
            candle_store=store,
        )
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")

    @pytest.mark.asyncio
    async def test_export_then_backtest_recent_data(self, client):
        # Nothing archived yet: the week list starts empty.
        before = await client.get("/api/backtest/datasets")
        assert before.json()["weeks"] == []

        # Snapshot the live DB into the archive (no deletion).
        exported = await client.post("/api/backtest/export")
        assert exported.status_code == 200
        assert exported.json()["rows_written"] > 0

        # The exported week now appears, with both epics.
        after = await client.get("/api/backtest/datasets")
        weeks = after.json()["weeks"]
        assert weeks and weeks[0]["week"] == "2026-W24"
        assert {e["epic"] for e in weeks[0]["epics"]} == {"EPIC.A", "EPIC.B"}

        # A backtest over all epics of that week now has data to replay.
        run = await client.post("/api/backtest/run", json={"weeks": ["2026-W24"]})
        assert run.status_code == 200
        assert run.json()["epics_loaded"] == 2
