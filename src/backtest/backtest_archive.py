"""Backtest archive — read-only access to the candle dump files.

The live bot archives aged candles to per-ISO-week CSV files (see
:meth:`src.feed.candle_store.CandleStore.dump_and_purge`). This module reads
those files back into :class:`~src.feed.price_buffer.Candle` objects so the
backtester can replay them.

It is deliberately **independent of the database**: it never imports the live
``candle`` table or opens a DB session. That is what lets a backtest run mid-week
while the main process keeps recording new data — the two never share state, only
the on-disk archive.

Any CSV under ``dump_dir`` whose header matches the dump schema is read,
regardless of filename, so legacy ``candles_before_*.csv`` dumps stay usable
alongside the newer ``candles_<year>-W<week>.csv`` week files.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.feed.candle_store import _DUMP_FIELDS, iso_week_label
from src.feed.price_buffer import Candle

logger = logging.getLogger(__name__)

# Numeric candle columns (everything but ``epic`` and ``timestamp``).
_FLOAT_FIELDS = (
    "bid_open",
    "bid_close",
    "bid_high",
    "bid_low",
    "offer_open",
    "offer_close",
    "offer_high",
    "offer_low",
)


@dataclass(slots=True)
class EpicWeekStats:
    """How many candles an epic has in a given ISO week, and its time span."""

    epic: str
    count: int
    first: datetime
    last: datetime


@dataclass(slots=True)
class WeekDataset:
    """A selectable backtest dataset: one ISO week across one or more epics."""

    week: str  # ISO-week label, e.g. "2026-W24"
    epics: list[EpicWeekStats]

    @property
    def total_candles(self) -> int:
        return sum(e.count for e in self.epics)

    @property
    def first(self) -> datetime:
        return min(e.first for e in self.epics)

    @property
    def last(self) -> datetime:
        return max(e.last for e in self.epics)


def _parse_timestamp(raw: str) -> datetime:
    """Parse an ISO timestamp from the archive, forcing tz-awareness (UTC)."""
    moment = datetime.fromisoformat(raw)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def _row_to_candle(row: dict[str, str]) -> Candle:
    """Build a :class:`Candle` from one archive CSV row."""
    values = {field: float(row[field]) for field in _FLOAT_FIELDS}
    return Candle(
        timestamp=_parse_timestamp(row["timestamp"]),
        volume=int(float(row.get("volume") or 0)),
        **values,
    )


class BacktestArchive:
    """Reads candle dump files for offline backtesting (no DB access)."""

    def __init__(self, dump_dir: str | Path = "./dumps") -> None:
        self._dump_dir = Path(dump_dir)

    def _archive_files(self) -> list[Path]:
        """Return every CSV under ``dump_dir`` carrying the dump schema header."""
        if not self._dump_dir.is_dir():
            return []
        files: list[Path] = []
        for path in sorted(self._dump_dir.glob("*.csv")):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    header = fh.readline().strip().split(",")
            except OSError as exc:
                logger.warning("Skipping unreadable archive %s: %s", path, exc)
                continue
            if header == _DUMP_FIELDS:
                files.append(path)
            else:
                logger.debug("Skipping %s: header is not a candle dump", path.name)
        return files

    def _iter_rows(self):
        """Yield every candle row across all archive files as dicts."""
        for path in self._archive_files():
            try:
                with path.open("r", newline="", encoding="utf-8") as fh:
                    yield from csv.DictReader(fh)
            except OSError as exc:
                logger.warning("Skipping unreadable archive %s: %s", path, exc)

    def datasets(self) -> list[WeekDataset]:
        """Summarise available data, grouped by ISO week then epic.

        Returns the most recent week first so the UI can default to it.
        """
        # week -> epic -> [count, first, last]
        agg: dict[str, dict[str, list]] = {}
        for row in self._iter_rows():
            epic = row["epic"]
            ts = _parse_timestamp(row["timestamp"])
            week = iso_week_label(ts)
            per_epic = agg.setdefault(week, {})
            stats = per_epic.get(epic)
            if stats is None:
                per_epic[epic] = [1, ts, ts]
            else:
                stats[0] += 1
                stats[1] = min(stats[1], ts)
                stats[2] = max(stats[2], ts)

        datasets = [
            WeekDataset(
                week=week,
                epics=sorted(
                    (
                        EpicWeekStats(epic=epic, count=c, first=first, last=last)
                        for epic, (c, first, last) in per_epic.items()
                    ),
                    key=lambda e: e.epic,
                ),
            )
            for week, per_epic in agg.items()
        ]
        datasets.sort(key=lambda d: d.week, reverse=True)
        return datasets

    def load(
        self,
        weeks: list[str] | None = None,
        epics: list[str] | None = None,
    ) -> dict[str, list[Candle]]:
        """Load archived candles, optionally filtered by ISO week and/or epic.

        Returns a mapping ``epic -> candles`` sorted oldest-to-newest, with any
        duplicate ``(epic, timestamp)`` pairs collapsed to the first seen (a
        safety net against overlapping archives). When ``weeks``/``epics`` are
        ``None`` everything is loaded.
        """
        week_filter = set(weeks) if weeks else None
        epic_filter = set(epics) if epics else None

        # epic -> {timestamp: Candle}
        collected: dict[str, dict[datetime, Candle]] = {}
        for row in self._iter_rows():
            epic = row["epic"]
            if epic_filter is not None and epic not in epic_filter:
                continue
            ts = _parse_timestamp(row["timestamp"])
            if week_filter is not None and iso_week_label(ts) not in week_filter:
                continue
            by_ts = collected.setdefault(epic, {})
            if ts not in by_ts:
                by_ts[ts] = _row_to_candle(row)

        return {
            epic: [by_ts[ts] for ts in sorted(by_ts)]
            for epic, by_ts in sorted(collected.items())
        }
