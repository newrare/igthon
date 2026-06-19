"""Tests for the candle store (persistence, dedup, retention dump/purge)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.feed.candle_store import CandleStore
from src.feed.price_buffer import Candle
from src.models.database import Base


def _candle(ts: datetime, bid: float = 100.0) -> Candle:
    """Build a candle at a given timestamp."""
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


@pytest.fixture
async def session_factory():
    """In-memory SQLite session factory with the schema created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


class TestSave:
    async def test_save_inserts_candles(self, session_factory, tmp_path):
        store = CandleStore(session_factory, dump_dir=tmp_path)
        base = datetime(2026, 6, 5, 9, 0, tzinfo=UTC)
        candles = [_candle(base + timedelta(minutes=i)) for i in range(5)]

        inserted = await store.save("EPIC.A", candles)

        assert inserted == 5
        stored = await store.fetch("EPIC.A")
        assert len(stored) == 5

    async def test_save_dedupes_older_or_equal(self, session_factory, tmp_path):
        store = CandleStore(session_factory, dump_dir=tmp_path)
        base = datetime(2026, 6, 5, 9, 0, tzinfo=UTC)
        first = [_candle(base + timedelta(minutes=i)) for i in range(3)]
        await store.save("EPIC.A", first)

        # Overlapping batch: 2 already-seen + 2 new.
        overlap = [_candle(base + timedelta(minutes=i)) for i in range(1, 5)]
        inserted = await store.save("EPIC.A", overlap)

        assert inserted == 2  # only minutes 3 and 4 are newer than the stored max
        stored = await store.fetch("EPIC.A")
        assert len(stored) == 5

    async def test_save_empty_is_noop(self, session_factory, tmp_path):
        store = CandleStore(session_factory, dump_dir=tmp_path)
        assert await store.save("EPIC.A", []) == 0


class TestFetchAndStats:
    async def test_fetch_orders_oldest_first(self, session_factory, tmp_path):
        store = CandleStore(session_factory, dump_dir=tmp_path)
        base = datetime(2026, 6, 5, 9, 0, tzinfo=UTC)
        await store.save(
            "EPIC.A", [_candle(base + timedelta(minutes=i)) for i in (2, 0, 1)]
        )

        stored = await store.fetch("EPIC.A")
        timestamps = [c.timestamp for c in stored]
        assert timestamps == sorted(timestamps)

    async def test_epics_with_data(self, session_factory, tmp_path):
        store = CandleStore(session_factory, dump_dir=tmp_path)
        base = datetime(2026, 6, 5, 9, 0, tzinfo=UTC)
        await store.save(
            "EPIC.A", [_candle(base), _candle(base + timedelta(minutes=1))]
        )
        await store.save("EPIC.B", [_candle(base)])

        stats = {s.epic: s for s in await store.epics_with_data()}
        assert stats["EPIC.A"].count == 2
        assert stats["EPIC.B"].count == 1
        assert stats["EPIC.A"].first <= stats["EPIC.A"].last


class TestDumpAndPurge:
    async def test_purges_old_and_dumps_to_csv(self, session_factory, tmp_path):
        store = CandleStore(session_factory, dump_dir=tmp_path, retention_days=7)
        now = datetime.now(UTC)
        old = [
            _candle(now - timedelta(days=10) + timedelta(minutes=i)) for i in range(3)
        ]
        recent = [_candle(now - timedelta(minutes=i)) for i in range(2)]
        await store.save("EPIC.A", old + recent)

        count, paths = await store.dump_and_purge()

        assert count == 3
        # The three old candles fall in one ISO week -> one week archive file.
        assert len(paths) == 1
        path = paths[0]
        assert path.exists()
        assert path.name.startswith("candles_") and path.name.endswith(".csv")
        # CSV holds a header + one row per purged candle.
        assert len(path.read_text().strip().splitlines()) == 3 + 1
        # Only the recent candles survive in the table.
        remaining = await store.fetch("EPIC.A")
        assert len(remaining) == 2

    async def test_nothing_to_purge_returns_empty(self, session_factory, tmp_path):
        store = CandleStore(session_factory, dump_dir=tmp_path, retention_days=7)
        now = datetime.now(UTC)
        await store.save(
            "EPIC.A", [_candle(now - timedelta(minutes=i)) for i in range(3)]
        )

        count, paths = await store.dump_and_purge()

        assert count == 0
        assert paths == []

    async def test_separate_weeks_go_to_separate_files(self, session_factory, tmp_path):
        store = CandleStore(session_factory, dump_dir=tmp_path, retention_days=7)
        now = datetime.now(UTC)
        # Two batches in distinct ISO weeks, both past the retention window.
        week_a = [
            _candle(now - timedelta(days=20) + timedelta(minutes=i)) for i in range(2)
        ]
        week_b = [
            _candle(now - timedelta(days=13) + timedelta(minutes=i)) for i in range(3)
        ]
        await store.save("EPIC.A", week_a + week_b)

        count, paths = await store.dump_and_purge()

        assert count == 5
        assert len(paths) == 2
        assert {p.name for p in paths} == {p.name for p in tmp_path.glob("*.csv")}

    async def test_repeated_purge_appends_to_week_file(self, session_factory, tmp_path):
        store = CandleStore(session_factory, dump_dir=tmp_path, retention_days=7)
        now = datetime.now(UTC)
        # First aged batch.
        base = now - timedelta(days=12)
        await store.save(
            "EPIC.A", [_candle(base + timedelta(minutes=i)) for i in range(2)]
        )
        _, first_paths = await store.dump_and_purge()
        # Second aged batch in the same ISO week -> appended, no new header.
        await store.save(
            "EPIC.A",
            [
                _candle(now - timedelta(days=12, minutes=10) + timedelta(minutes=i))
                for i in range(2)
            ],
        )
        count, second_paths = await store.dump_and_purge()

        assert count == 2
        assert first_paths == second_paths  # same week file reused
        # 1 header + 4 data rows across the two purges.
        assert len(second_paths[0].read_text().strip().splitlines()) == 1 + 4


class TestExportToArchive:
    async def test_exports_without_deleting(self, session_factory, tmp_path):
        store = CandleStore(session_factory, dump_dir=tmp_path, retention_days=7)
        now = datetime.now(UTC)
        # All recent (inside the retention window) -> a purge would archive none.
        candles = [_candle(now - timedelta(minutes=i)) for i in range(5)]
        await store.save("EPIC.A", candles)

        written, paths = await store.export_to_archive()

        assert written == 5
        assert len(paths) == 1 and paths[0].exists()
        # The candles are still in the live table (export does not purge).
        assert len(await store.fetch("EPIC.A")) == 5

    async def test_export_is_idempotent(self, session_factory, tmp_path):
        store = CandleStore(session_factory, dump_dir=tmp_path, retention_days=7)
        now = datetime.now(UTC)
        await store.save(
            "EPIC.A", [_candle(now - timedelta(minutes=i)) for i in range(3)]
        )

        first, paths = await store.export_to_archive()
        second, _ = await store.export_to_archive()

        assert first == 3
        assert second == 0  # already archived -> nothing new written
        # File holds exactly the header + 3 rows, no duplicates.
        assert len(paths[0].read_text().strip().splitlines()) == 1 + 3

    async def test_export_adds_only_new_rows(self, session_factory, tmp_path):
        store = CandleStore(session_factory, dump_dir=tmp_path, retention_days=7)
        now = datetime.now(UTC)
        base = now - timedelta(minutes=10)
        await store.save("EPIC.A", [_candle(base)])
        await store.export_to_archive()
        # A newer candle in the same ISO week is appended on the next export.
        await store.save("EPIC.A", [_candle(base + timedelta(minutes=1))])

        written, paths = await store.export_to_archive()

        assert written == 1
        assert len(paths[0].read_text().strip().splitlines()) == 1 + 2

    async def test_empty_table_returns_empty(self, session_factory, tmp_path):
        store = CandleStore(session_factory, dump_dir=tmp_path, retention_days=7)
        written, paths = await store.export_to_archive()
        assert written == 0 and paths == []
