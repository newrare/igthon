"""Tests for the backtest archive reader (file-only, DB-independent)."""

import csv
from datetime import UTC, datetime

from src.services.backtest_archive import BacktestArchive
from src.services.candle_store import _DUMP_FIELDS, iso_week_label
from src.services.price_buffer import Candle


def _candle(ts: datetime, bid: float = 100.0) -> Candle:
    offer = bid + 1.8
    return Candle(
        timestamp=ts,
        bid_open=bid - 0.5,
        bid_close=bid,
        bid_high=bid + 1.0,
        bid_low=bid - 1.0,
        offer_open=offer - 0.5,
        offer_close=offer,
        offer_high=offer + 1.0,
        offer_low=offer - 1.0,
        volume=5,
    )


def _write_archive(path, rows: list[tuple[str, Candle]], *, header=True) -> None:
    """Write (epic, candle) rows into a dump-schema CSV at ``path``."""
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if header:
            writer.writerow(_DUMP_FIELDS)
        for epic, c in rows:
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


class TestDatasets:
    def test_empty_dir_returns_nothing(self, tmp_path):
        assert BacktestArchive(tmp_path).datasets() == []

    def test_groups_by_week_and_epic(self, tmp_path):
        # Week 2026-W24 starts Mon 2026-06-08.
        base = datetime(2026, 6, 9, 9, 0, tzinfo=UTC)
        rows = [("EPIC.A", _candle(base)), ("EPIC.A", _candle(base.replace(minute=1)))]
        rows += [("EPIC.B", _candle(base))]
        _write_archive(tmp_path / "candles_2026-W24.csv", rows)

        datasets = BacktestArchive(tmp_path).datasets()

        assert len(datasets) == 1
        ds = datasets[0]
        assert ds.week == "2026-W24"
        assert ds.total_candles == 3
        epics = {e.epic: e.count for e in ds.epics}
        assert epics == {"EPIC.A": 2, "EPIC.B": 1}

    def test_most_recent_week_first(self, tmp_path):
        early = datetime(2026, 6, 2, 9, 0, tzinfo=UTC)  # W23
        late = datetime(2026, 6, 9, 9, 0, tzinfo=UTC)  # W24
        _write_archive(tmp_path / "candles_2026-W23.csv", [("E", _candle(early))])
        _write_archive(tmp_path / "candles_2026-W24.csv", [("E", _candle(late))])

        weeks = [d.week for d in BacktestArchive(tmp_path).datasets()]

        assert weeks == ["2026-W24", "2026-W23"]

    def test_ignores_non_dump_csv(self, tmp_path):
        (tmp_path / "unrelated.csv").write_text("a,b,c\n1,2,3\n")
        assert BacktestArchive(tmp_path).datasets() == []

    def test_legacy_filename_is_read(self, tmp_path):
        base = datetime(2026, 6, 9, 9, 0, tzinfo=UTC)
        # Old timestamped dump name still carries the dump header -> usable.
        _write_archive(
            tmp_path / "candles_before_2026-06-01_20260609_020000.csv",
            [("EPIC.A", _candle(base))],
        )
        datasets = BacktestArchive(tmp_path).datasets()
        assert len(datasets) == 1 and datasets[0].week == iso_week_label(base)


class TestLoad:
    def test_load_returns_sorted_candles_per_epic(self, tmp_path):
        base = datetime(2026, 6, 9, 9, 0, tzinfo=UTC)
        rows = [
            ("EPIC.A", _candle(base.replace(minute=2))),
            ("EPIC.A", _candle(base.replace(minute=0))),
            ("EPIC.A", _candle(base.replace(minute=1))),
        ]
        _write_archive(tmp_path / "candles_2026-W24.csv", rows)

        loaded = BacktestArchive(tmp_path).load()

        assert list(loaded) == ["EPIC.A"]
        minutes = [c.timestamp.minute for c in loaded["EPIC.A"]]
        assert minutes == [0, 1, 2]

    def test_filter_by_week_and_epic(self, tmp_path):
        w23 = datetime(2026, 6, 2, 9, 0, tzinfo=UTC)
        w24 = datetime(2026, 6, 9, 9, 0, tzinfo=UTC)
        _write_archive(
            tmp_path / "candles_2026-W23.csv",
            [("EPIC.A", _candle(w23)), ("EPIC.B", _candle(w23))],
        )
        _write_archive(
            tmp_path / "candles_2026-W24.csv",
            [("EPIC.A", _candle(w24)), ("EPIC.B", _candle(w24))],
        )
        archive = BacktestArchive(tmp_path)

        by_week = archive.load(weeks=["2026-W24"])
        assert all(
            c.timestamp.isocalendar().week == 24 for cs in by_week.values() for c in cs
        )

        by_epic = archive.load(epics=["EPIC.A"])
        assert list(by_epic) == ["EPIC.A"]

    def test_duplicate_timestamps_collapsed(self, tmp_path):
        base = datetime(2026, 6, 9, 9, 0, tzinfo=UTC)
        dup = [("EPIC.A", _candle(base)), ("EPIC.A", _candle(base))]
        _write_archive(tmp_path / "candles_2026-W24.csv", dup)

        loaded = BacktestArchive(tmp_path).load()

        assert len(loaded["EPIC.A"]) == 1

    def test_no_match_returns_empty(self, tmp_path):
        base = datetime(2026, 6, 9, 9, 0, tzinfo=UTC)
        _write_archive(tmp_path / "candles_2026-W24.csv", [("EPIC.A", _candle(base))])
        assert BacktestArchive(tmp_path).load(weeks=["1999-W01"]) == {}
