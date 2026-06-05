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

from src.models.candle import CandleRecord
from src.services.price_buffer import Candle

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


# Column order used for both CSV dump and (potential) reload during simulation.
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

    async def dump_and_purge(self) -> tuple[int, Path | None]:
        """Export candles older than the retention window, then delete them.

        Old candles are written to a timestamped CSV under ``dump_dir`` so they
        can later seed offline simulations, after which they are removed from the
        live table to keep it small. Returns ``(rows_dumped, dump_path)``.
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
                return 0, None

            dump_path = self._write_dump(old, cutoff)

            await session.execute(
                delete(CandleRecord)
                .where(CandleRecord.timestamp < cutoff)
                .execution_options(synchronize_session=False)
            )
            await session.commit()

        logger.info(
            "Candle purge: dumped %d rows to %s and deleted them from the table",
            len(old),
            dump_path,
        )
        return len(old), dump_path

    def _write_dump(self, rows: list[CandleRecord], cutoff: datetime) -> Path:
        """Write candle rows to a CSV dump file and return its path."""
        self._dump_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        dump_path = self._dump_dir / f"candles_before_{cutoff.date()}_{stamp}.csv"

        with dump_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(_DUMP_FIELDS)
            for r in rows:
                writer.writerow(
                    [
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
                )
        return dump_path
