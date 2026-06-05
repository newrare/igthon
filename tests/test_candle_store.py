"""Tests for the candle store (persistence, dedup, retention dump/purge)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.models.database import Base
from src.services.candle_store import CandleStore
from src.services.price_buffer import Candle


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

        count, path = await store.dump_and_purge()

        assert count == 3
        assert path is not None and path.exists()
        # CSV holds a header + one row per purged candle.
        assert len(path.read_text().strip().splitlines()) == 3 + 1
        # Only the recent candles survive in the table.
        remaining = await store.fetch("EPIC.A")
        assert len(remaining) == 2

    async def test_nothing_to_purge_returns_none(self, session_factory, tmp_path):
        store = CandleStore(session_factory, dump_dir=tmp_path, retention_days=7)
        now = datetime.now(UTC)
        await store.save(
            "EPIC.A", [_candle(now - timedelta(minutes=i)) for i in range(3)]
        )

        count, path = await store.dump_and_purge()

        assert count == 0
        assert path is None
