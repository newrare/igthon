"""Tests for the historical backtester and its web routes.

The backtester replays the project's real open/close rules over archived
candles. These tests check the day-grouping helper, that runs are deterministic
and internally consistent (every trade closed, P&L adds up), and that the
``/backtest`` API loads the archive and replays it.
"""

import csv
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from src.backtest.backtester import (
    BacktestConfig,
    build_days,
    dedupe_correlated_epics,
    percentage_summary,
    run_backtest,
    trade_return_pct,
)
from src.backtest.curve_generator import generate_curve
from src.feed.candle_store import _DUMP_FIELDS
from src.feed.price_buffer import Candle, PriceBuffer
from src.web.app import create_app


def _settings(dump_dir="./dumps") -> SimpleNamespace:
    """Settings stand-in with the strategy attributes the engine reads."""
    return SimpleNamespace(
        ig_env=SimpleNamespace(value="demo"),
        web_port=8000,
        candle_dump_dir=str(dump_dir),
        entry_strategy_name="donchian_er",
        close_profile_name="atr_trailing",
        strategy_name="donchian_er",
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
        strategy_max_trades_day=50,
        strategy_daily_loss_limit=-500.0,
        strategy_daily_win_target=300.0,
        strategy_min_win_rate=0.40,
        strategy_hour_start=9,
        strategy_hour_end=16,
        strategy_hour_close=17,
        strategy_close_margin_minutes=5,
        strategy_close_target="follower",
        strategy_compensate_loose=False,
        strategy_euro_loss=4000.0,
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

    def test_opens_only_within_trading_hours(self):
        days = [("EPIC.A", s, datetime(2026, 6, 8, tzinfo=UTC)) for s in range(4)]
        result = run_backtest(
            _settings(), _archive_candles(days), BacktestConfig(target_trades=50)
        )
        for t in result.trades:
            hour = int(t.open_time.split(":")[0])
            assert 9 <= hour < 16

    def test_empty_candles_no_trades(self):
        result = run_backtest(_settings(), {}, BacktestConfig())
        assert result.trades == []
        assert result.days_simulated == 0


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
        resp = await client.post(
            "/api/backtest/run", json={"weeks": ["2026-W24"], "target_trades": 10}
        )
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
    async def test_datasets_endpoint(self, client):
        resp = await client.get("/api/backtest/datasets")
        assert resp.status_code == 200
        weeks = resp.json()["weeks"]
        assert len(weeks) == 1
        assert weeks[0]["week"] == "2026-W24"
        assert {e["epic"] for e in weeks[0]["epics"]} == {"EPIC.A", "EPIC.B"}

    @pytest.mark.asyncio
    async def test_run_endpoint(self, client):
        resp = await client.post(
            "/api/backtest/run",
            json={"weeks": ["2026-W24"], "target_trades": 20},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["candles_loaded"] > 0
        assert data["epics_loaded"] == 2
        s = data["summary"]
        assert s["wins"] + s["losses"] == s["trades"]
        assert len(data["trades"]) == s["trades"]
        # P&L is reported as percentage return, not euros.
        assert "total_return_pct" in s
        assert len(s["equity_pct"]) == s["trades"]
        if data["trades"]:
            assert "return_pct" in data["trades"][0]
            assert "euro" not in data["trades"][0]

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
        run = await client.post(
            "/api/backtest/run", json={"weeks": ["2026-W24"], "target_trades": 10}
        )
        assert run.status_code == 200
        assert run.json()["epics_loaded"] == 2
