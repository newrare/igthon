"""Tests for the dashboard reading panel builder (presentation of src/core/reading)."""

from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace

from src.feed.price_buffer import Candle, EpicBuffer
from src.web.routes.dashboard.reading_view import build_reading_view

BASE = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)


def _buffer(bids: list[float], *, spread: float = 0.2, wick: float = 0.3) -> EpicBuffer:
    buf = EpicBuffer(epic="CS.D.EURUSD.CFD.IP", max_candles=500)
    for index, bid in enumerate(bids):
        buf.add(
            Candle(
                timestamp=BASE + timedelta(minutes=index),
                bid_open=bid,
                bid_close=bid,
                bid_high=bid + wick,
                bid_low=bid - wick,
                offer_open=bid + spread,
                offer_close=bid + spread,
                offer_high=bid + spread + wick,
                offer_low=bid + spread - wick,
            )
        )
    return buf


def _titles(view: dict) -> list[str]:
    return [section["title"] for section in view["sections"]]


def _rows(view: dict, title: str) -> dict[str, dict]:
    for section in view["sections"]:
        if section["title"] == title:
            return {row["label"]: row for row in section["rows"]}
    raise AssertionError(f"section {title!r} not in {_titles(view)}")


class TestMarketPanel:
    """The market half of the panel."""

    def test_sections_and_headline(self):
        view = build_reading_view(
            _buffer([100.0 + i * 0.3 for i in range(60)]), epic="X", name="Test market"
        )
        assert _titles(view) == [
            "State",
            "Trend per horizon",
            "Candle pressure (15 min)",
            "Amplitude & noise",
            "Momentum",
            "Readable levels",
        ]
        assert view["epic"] == "X"
        assert view["name"] == "Test market"
        assert view["minutes"] == 60
        labels = [chip["label"] for chip in view["headline"]]
        assert labels == ["Regime", "Price", "ATR", "Alignment", "History"]

    def test_a_clean_ramp_reads_as_an_up_trend(self):
        view = build_reading_view(
            _buffer([100.0 + i * 0.3 for i in range(60)]), epic="X"
        )
        state = _rows(view, "State")
        assert state["Regime"]["value"] == "Trend Up"
        assert state["Regime"]["tone"] == "good"
        assert state["Horizon alignment"]["value"] == "+1.00"

        trend = _rows(view, "Trend per horizon")
        assert set(trend) == {"5 min", "15 min", "30 min", "60 min"}
        assert trend["15 min"]["value"].startswith("+")
        assert trend["15 min"]["tone"] == "good"
        assert "R²" in trend["15 min"]["detail"]

    def test_a_fall_reads_as_a_down_trend(self):
        view = build_reading_view(
            _buffer([100.0 - i * 0.3 for i in range(60)]), epic="X"
        )
        assert _rows(view, "State")["Regime"]["value"] == "Trend Down"
        assert _rows(view, "Trend per horizon")["30 min"]["tone"] == "bad"

    def test_prices_are_formatted_at_the_epic_scale(self):
        # A forex-scale quote needs 5 decimals; an index-scale one must not show them.
        forex = build_reading_view(
            _buffer(
                [1.0800 + i * 0.0002 for i in range(40)], spread=0.0002, wick=0.0003
            ),
            epic="FX",
        )
        index = build_reading_view(
            _buffer([18000.0 + i for i in range(40)], spread=1.0, wick=2.0), epic="IX"
        )
        assert _rows(forex, "Amplitude & noise")["ATR (14)"]["value"].count("0") >= 3
        assert (
            len(_rows(index, "Amplitude & noise")["ATR (14)"]["value"].split(".")[1])
            == 1
        )

    def test_spread_row_carries_the_cached_euro_cost(self):
        view = build_reading_view(
            _buffer([100.0] * 40), epic="X", spread={"euro": 2.5, "points": 0.2}
        )
        assert "2.50 €" in _rows(view, "Momentum")["Spread"]["detail"]

    def test_levels_report_absence_without_breaking(self):
        # A monotone ramp confirms no top, so there is no resistance to show.
        view = build_reading_view(
            _buffer([100.0 + i for i in range(40)], wick=0.0), epic="X"
        )
        levels = _rows(view, "Readable levels")
        assert levels["Nearest resistance"]["value"] == "—"
        assert levels["Nearest resistance"]["tone"] == "muted"

    def test_levels_report_a_repeated_low(self):
        wave = [100.0, 101.5, 103.0, 104.0, 102.5, 100.0]
        view = build_reading_view(_buffer(wave * 5 + [102.0], wick=0.1), epic="X")
        support = _rows(view, "Readable levels")["Nearest support"]
        assert support["value"] != "—"
        assert "ATR" in support["detail"]

    def test_a_cold_buffer_still_answers(self):
        view = build_reading_view(_buffer([100.0, 100.5]), epic="X")
        assert _rows(view, "State")["Regime"]["value"] == "Unknown"
        assert _rows(view, "Momentum")["EMA 9/21"]["value"] == "—"
        assert view["headline"][-1]["tone"] == "warn"  # not enough history


def _position(**overrides) -> SimpleNamespace:
    """Duck-typed stand-in for a persisted open ``Position`` row."""
    fields = dict(
        epic="CS.D.EURUSD.CFD.IP",
        epic_name="EUR/USD",
        direction="BUY",
        date=BASE.date(),
        time_open=time(10, 10),
        level_open=100.0,
        level_zero=100.2,
        level_margin=101.2,
        level_follower=99.5,
        euro_per_point=2.0,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


class TestPositionPanel:
    """The since-open half, added only when a trade is open."""

    @staticmethod
    def _view(**overrides) -> dict:
        # 10 candles before the open, then a rise to 103 and a pull-back to 102.
        bids = [100.0] * 10 + [100.2, 101.0, 103.0, 101.0, 102.0]
        return build_reading_view(
            _buffer(bids, wick=0.5), epic="X", position=_position(**overrides)
        )

    def test_position_section_comes_first(self):
        view = self._view()
        assert view["sections"][0]["title"] == "Open position (BUY)"

    def test_gain_and_excursions(self):
        rows = _rows(self._view(), "Open position (BUY)")
        assert rows["Gain"]["tone"] == "good"
        assert "+3.60 €" in rows["Gain"]["detail"]
        assert "5 min open" in rows["Gain"]["detail"]
        assert rows["Best / worst excursion"]["value"].startswith("3.3")
        assert rows["Break-even"]["value"] == "crossed"
        assert rows["Break-even"]["tone"] == "good"

    def test_giveback_is_flagged_once_most_of_the_gain_leaked(self):
        # Peak 103.5, now 100.3: 3.2 of the 3.3 point excursion handed back.
        bids = [100.0] * 10 + [100.2, 103.0, 101.0, 100.3]
        view = build_reading_view(
            _buffer(bids, wick=0.5), epic="X", position=_position()
        )
        assert _rows(view, "Open position (BUY)")["Given back"]["tone"] == "bad"

    def test_a_stop_under_water_is_reported_as_such(self):
        rows = _rows(self._view(), "Open position (BUY)")
        assert rows["Locked at the stop"]["tone"] == "bad"
        secured = _rows(self._view(level_follower=101.0), "Open position (BUY)")
        assert secured["Locked at the stop"]["tone"] == "good"

    def test_a_short_reads_the_same_way(self):
        # Mirrored book: profit is down, so the same numbers must come back.
        bids = [100.0] * 10 + [100.2, 101.0, 103.0, 101.0, 102.0]
        buf = EpicBuffer(epic="X", max_candles=500)
        for index, bid in enumerate(bids):
            offer = 200.0 - bid
            buf.add(
                Candle(
                    timestamp=BASE + timedelta(minutes=index),
                    bid_open=offer - 0.2,
                    bid_close=offer - 0.2,
                    bid_high=offer - 0.2 + 0.5,
                    bid_low=offer - 0.2 - 0.5,
                    offer_open=offer,
                    offer_close=offer,
                    offer_high=offer + 0.5,
                    offer_low=offer - 0.5,
                )
            )
        view = build_reading_view(
            buf,
            epic="X",
            position=_position(
                direction="SELL",
                level_open=100.0,
                level_zero=200.0 - 100.2,
                level_margin=200.0 - 101.2,
                level_follower=200.0 - 99.5,
            ),
        )
        rows = _rows(view, "Open position (SELL)")
        assert rows["Gain"]["tone"] == "good"
        assert "+3.60 €" in rows["Gain"]["detail"]

    def test_a_row_without_any_level_falls_back_to_the_market_panel(self):
        view = build_reading_view(
            _buffer([100.0] * 20),
            epic="X",
            position=_position(level_zero=None, level_open=None),
        )
        assert "State" in _titles(view)
        assert not any(title.startswith("Open position") for title in _titles(view))
