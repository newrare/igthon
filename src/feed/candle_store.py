"""Candle store — durable persistence of collected price candles.

Taps the candle stream that already feeds the in-memory :class:`PriceBuffer`
during ``collect_and_analyze`` and writes it to the ``candle`` table. This adds
**no** IG API calls — it only persists data the bot already fetched.

Responsibilities:
  - ``save``: append newly fetched candles (deduplicated by timestamp).
  - ``fetch`` / ``epics_with_data``: read back candles for the chart pages.
  - ``dump_and_purge``: enforce the rolling retention window by exporting old
    candles to a CSV dump (for later offline simulation) before deleting them.
"""

from __future__ import annotations

import csv
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.feed.price_buffer import Candle
from src.models.candle import CandleRecord

logger = logging.getLogger(__name__)


def record_to_candle(record: CandleRecord) -> Candle:
    """Convert a persisted ``CandleRecord`` to an in-memory ``Candle``.

    Used to rehydrate the ``PriceBuffer`` from the database on startup without
    re-fetching history from the IG ``/prices`` endpoint.
    """
    return Candle(
        timestamp=record.timestamp,
        bid_open=record.bid_open,
        bid_close=record.bid_close,
        bid_high=record.bid_high,
        bid_low=record.bid_low,
        offer_open=record.offer_open,
        offer_close=record.offer_close,
        offer_high=record.offer_high,
        offer_low=record.offer_low,
        volume=record.volume,
    )


def iso_week_label(moment: datetime) -> str:
    """Return the ISO-week archive label for a timestamp, e.g. ``2026-W24``.

    Archive files are grouped by ISO week so the backtester can list and select
    a week's worth of candles independently of how many purge runs produced it.
    """
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


# Column order used for both CSV dump and reload during backtesting. The
# backtest archive loader (:mod:`src.backtest.backtest_archive`) reads any CSV
# carrying this exact header, so old ``candles_before_*`` dumps stay usable.
_DUMP_FIELDS = [
    "epic",
    "timestamp",
    "bid_open",
    "bid_close",
    "bid_high",
    "bid_low",
    "offer_open",
    "offer_close",
    "offer_high",
    "offer_low",
    "volume",
]


class EpicCandleStats:
    """Summary of stored candles for a single epic (for the chart index page)."""

    __slots__ = ("epic", "count", "first", "last")

    def __init__(
        self, epic: str, count: int, first: datetime | None, last: datetime | None
    ) -> None:
        self.epic = epic
        self.count = count
        self.first = first
        self.last = last


class CandleStore:
    """Persists and retrieves price candles, and enforces retention."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        dump_dir: str | Path = "./dumps",
        retention_days: int = 7,
    ) -> None:
        self._session_factory = session_factory
        self._dump_dir = Path(dump_dir)
        self._retention_days = retention_days

    async def save(self, epic: str, candles: list[Candle]) -> int:
        """Persist candles newer than what is already stored for ``epic``.

        Candles older than or equal to the latest stored timestamp are skipped,
        which deduplicates the overlapping windows returned by repeated bootstrap
        and incremental fetches. Returns the number of rows inserted.
        """
        if not candles:
            return 0

        try:
            async with self._session_factory() as session:
                latest = (
                    await session.scalars(
                        select(func.max(CandleRecord.timestamp)).where(
                            CandleRecord.epic == epic
                        )
                    )
                ).one_or_none()
                if latest is not None and latest.tzinfo is None:
                    latest = latest.replace(tzinfo=UTC)

                fresh = [c for c in candles if latest is None or c.timestamp > latest]
                if not fresh:
                    return 0

                session.add_all(
                    CandleRecord(
                        epic=epic,
                        timestamp=c.timestamp,
                        bid_open=c.bid_open,
                        bid_close=c.bid_close,
                        bid_high=c.bid_high,
                        bid_low=c.bid_low,
                        offer_open=c.offer_open,
                        offer_close=c.offer_close,
                        offer_high=c.offer_high,
                        offer_low=c.offer_low,
                        volume=c.volume,
                    )
                    for c in fresh
                )
                await session.commit()
                logger.debug("Persisted %d candles for %s", len(fresh), epic)
                return len(fresh)
        except Exception as exc:
            logger.error("Failed to persist candles for %s: %s", epic, exc)
            return 0

    async def fetch(
        self,
        epic: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[CandleRecord]:
        """Return stored candles for ``epic`` ordered oldest-to-newest."""
        async with self._session_factory() as session:
            query = (
                select(CandleRecord)
                .where(CandleRecord.epic == epic)
                .order_by(CandleRecord.timestamp.asc())
            )
            if since is not None:
                query = query.where(CandleRecord.timestamp >= since)
            if until is not None:
                query = query.where(CandleRecord.timestamp <= until)
            return list((await session.scalars(query)).all())

    async def fetch_candles(
        self,
        epic: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[Candle]:
        """Return stored candles for ``epic`` as in-memory ``Candle`` objects.

        Convenience wrapper around :meth:`fetch` used to rehydrate the
        ``PriceBuffer`` on startup without an IG API call.
        """
        records = await self.fetch(epic, since=since, until=until)
        return [record_to_candle(r) for r in records]

    async def epics_with_data(self) -> list[EpicCandleStats]:
        """Return per-epic candle counts and time ranges for the index page."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(
                    CandleRecord.epic,
                    func.count(CandleRecord.id),
                    func.min(CandleRecord.timestamp),
                    func.max(CandleRecord.timestamp),
                )
                .group_by(CandleRecord.epic)
                .order_by(CandleRecord.epic)
            )
            return [
                EpicCandleStats(epic=row[0], count=row[1], first=row[2], last=row[3])
                for row in result.all()
            ]

    async def dump_and_purge(self) -> tuple[int, list[Path]]:
        """Export candles older than the retention window, then delete them.

        Old candles are archived to per-ISO-week CSV files under ``dump_dir``
        (``candles_<year>-W<week>.csv``) so the backtester can later replay a
        whole week offline, after which they are deleted from the live table to
        keep it small. A given candle is archived exactly once (it is removed
        right after), so repeated daily runs simply append the newly-aged
        candles to the matching week file. Returns ``(rows_dumped, paths)``.
        """
        cutoff = datetime.now(UTC) - timedelta(days=self._retention_days)

        async with self._session_factory() as session:
            old = list(
                (
                    await session.scalars(
                        select(CandleRecord)
                        .where(CandleRecord.timestamp < cutoff)
                        .order_by(CandleRecord.epic, CandleRecord.timestamp)
                    )
                ).all()
            )

            if not old:
                logger.info("Candle purge: nothing older than %s", cutoff.date())
                return 0, []

            # Purged candles are deleted right after, so each is archived once:
            # plain append is correct and there is no need to dedup against files.
            _, paths = self._archive_rows(old, dedup=False)

            await session.execute(
                delete(CandleRecord)
                .where(CandleRecord.timestamp < cutoff)
                .execution_options(synchronize_session=False)
            )
            await session.commit()

        logger.info(
            "Candle purge: archived %d rows to %s and deleted them from the table",
            len(old),
            ", ".join(p.name for p in paths),
        )
        return len(old), paths

    async def export_to_archive(self) -> tuple[int, list[Path]]:
        """Snapshot **all** currently-stored candles to the archive, no deletion.

        Unlike :meth:`dump_and_purge` — which only archives candles past the
        retention window and then removes them — this copies the live table as-is
        so a backtest can use recent data that has not yet aged out of the
        database. Rows already present in a week file (matched by epic +
        timestamp) are skipped, so it is safe to run repeatedly and it merges
        cleanly with what the retention purge has already written. Returns
        ``(rows_written, paths)`` where ``rows_written`` counts only new rows.
        """
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(CandleRecord).order_by(
                            CandleRecord.epic, CandleRecord.timestamp
                        )
                    )
                ).all()
            )

        if not rows:
            logger.info("Candle export: the table is empty, nothing to archive")
            return 0, []

        written, paths = self._archive_rows(rows, dedup=True)
        logger.info(
            "Candle export: wrote %d new rows to %s",
            written,
            ", ".join(p.name for p in paths),
        )
        return written, paths

    @staticmethod
    def _row_values(r: CandleRecord) -> list:
        """Serialise a candle record to a dump CSV row (matches ``_DUMP_FIELDS``)."""
        return [
            r.epic,
            r.timestamp.isoformat(),
            r.bid_open,
            r.bid_close,
            r.bid_high,
            r.bid_low,
            r.offer_open,
            r.offer_close,
            r.offer_high,
            r.offer_low,
            r.volume,
        ]

    @staticmethod
    def _existing_keys(path: Path) -> set[tuple[str, str]]:
        """Return the ``(epic, timestamp)`` pairs already present in a week file."""
        keys: set[tuple[str, str]] = set()
        with path.open("r", newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                keys.add((row["epic"], row["timestamp"]))
        return keys

    def _archive_rows(
        self, rows: list[CandleRecord], *, dedup: bool
    ) -> tuple[int, list[Path]]:
        """Write candle rows to their per-ISO-week CSV files; return (written, paths).

        Rows are bucketed by the ISO week of their timestamp; a week file gets a
        header only when first created, so successive writes append seamlessly.
        When ``dedup`` is set, rows already in a week file (matched by epic +
        timestamp) are skipped — used by :meth:`export_to_archive` so re-runs and
        overlap with the retention purge never duplicate data.
        """
        self._dump_dir.mkdir(parents=True, exist_ok=True)

        buckets: dict[str, list[CandleRecord]] = {}
        for r in rows:
            buckets.setdefault(iso_week_label(r.timestamp), []).append(r)

        written = 0
        paths: list[Path] = []
        for week, week_rows in sorted(buckets.items()):
            path = self._dump_dir / f"candles_{week}.csv"
            new_file = not path.exists()
            existing = self._existing_keys(path) if dedup and not new_file else set()

            to_write = [
                r
                for r in week_rows
                if not existing or (r.epic, r.timestamp.isoformat()) not in existing
            ]
            paths.append(path)
            if not to_write and not new_file:
                continue

            with path.open("a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                if new_file:
                    writer.writerow(_DUMP_FIELDS)
                for r in to_write:
                    writer.writerow(self._row_values(r))
            written += len(to_write)
        return written, paths
