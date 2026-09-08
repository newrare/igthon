"""Tests for the static market catalog crawler (walk, resume, upsert)."""

import asyncio

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.api.client import IGAPIError
from src.markets.catalog_crawler import (
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_PENDING,
    CatalogCrawler,
)
from src.models.database import Base
from src.models.market_catalog import CatalogNode, MarketCatalog

# A small navigation tree: two root nodes, one of them with a child.
TREE: dict[str, dict] = {
    "/marketnavigation": {
        "nodes": [{"id": "1", "name": "Forex"}, {"id": "2", "name": "Indices"}],
        "markets": None,
    },
    "/marketnavigation/1": {
        "nodes": [{"id": "11", "name": "Major FX"}],
        "markets": [
            {
                "epic": "CS.D.EURUSD.CFD.IP",
                "instrumentName": "EUR/USD",
                "instrumentType": "CURRENCIES",
                "expiry": "-",
            }
        ],
    },
    "/marketnavigation/11": {
        "nodes": [],
        "markets": [
            {
                "epic": "CS.D.GBPUSD.CFD.IP",
                "instrumentName": "GBP/USD",
                "instrumentType": "CURRENCIES",
            }
        ],
    },
    "/marketnavigation/2": {
        "nodes": [],
        "markets": [
            {
                "epic": "IX.D.DAX.IFMM.IP",
                "instrumentName": "Germany 40",
                "instrumentType": "INDICES",
            }
        ],
    },
}


def _ig_error(status: int, code: str = "") -> IGAPIError:
    """Build an IGAPIError carrying a real HTTP status and IG error code."""
    request = httpx.Request("GET", "https://api.ig.com/marketnavigation/2")
    response = httpx.Response(status, request=request)
    return IGAPIError("boom", request=request, response=response, ig_error_code=code)


class FakeClient:
    """Minimal APIQueue stand-in: serves a static tree, can fail on demand."""

    def __init__(
        self,
        tree: dict[str, dict] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.tree = tree if tree is not None else TREE
        self.errors = errors or {}
        self.calls: list[str] = []

    async def get(
        self,
        endpoint: str,
        *,
        version: int = 1,
        priority: int = 0,
        label: str | None = None,
    ) -> dict:
        self.calls.append(endpoint)
        # An error is consumed on first use, so a "retry succeeds" case is easy.
        exc = self.errors.pop(endpoint, None)
        if exc is not None:
            raise exc
        return self.tree[endpoint]


@pytest.fixture
async def session_factory():
    """In-memory SQLite session factory with the schema created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _nodes(session_factory) -> dict[str, CatalogNode]:
    """Every frontier row, keyed by node id."""
    async with session_factory() as session:
        rows = (await session.scalars(select(CatalogNode))).all()
    return {n.node_id: n for n in rows}


async def _markets(session_factory) -> dict[str, MarketCatalog]:
    """Every catalog row, keyed by epic."""
    async with session_factory() as session:
        rows = (await session.scalars(select(MarketCatalog))).all()
    return {m.epic: m for m in rows}


class TestFullCrawl:
    async def test_walks_the_whole_tree(self, session_factory):
        crawler = CatalogCrawler(client=FakeClient(), session_factory=session_factory)

        progress = await crawler.crawl()

        assert progress.complete
        assert progress.nodes_total == 3
        assert progress.nodes_done == 3
        assert progress.nodes_pending == 0
        assert progress.markets == 3
        assert progress.markets_added == 3
        nodes = await _nodes(session_factory)
        assert {n.status for n in nodes.values()} == {STATUS_DONE}

    async def test_stores_instrument_details_and_taxonomy(self, session_factory):
        crawler = CatalogCrawler(client=FakeClient(), session_factory=session_factory)

        await crawler.crawl()

        markets = await _markets(session_factory)
        gbp = markets["CS.D.GBPUSD.CFD.IP"]
        assert gbp.instrument_name == "GBP/USD"
        assert gbp.instrument_type == "CURRENCIES"
        assert gbp.node_id == "11"
        # The readable path keeps IG's own hierarchy.
        assert gbp.node_path == "Forex / Major FX"

    async def test_breadth_first_order(self, session_factory):
        """Shallow nodes are visited first, so a partial crawl covers every class."""
        client = FakeClient()
        crawler = CatalogCrawler(client=client, session_factory=session_factory)

        await crawler.crawl()

        assert client.calls == [
            "/marketnavigation",
            "/marketnavigation/1",
            "/marketnavigation/2",
            "/marketnavigation/11",
        ]

    async def test_max_nodes_bounds_the_run(self, session_factory):
        client = FakeClient()
        crawler = CatalogCrawler(client=client, session_factory=session_factory)

        progress = await crawler.crawl(max_nodes=1)

        assert progress.nodes_visited == 1
        assert not progress.complete
        assert progress.nodes_pending == 2  # node 2 + the child found under node 1


class TestResume:
    async def test_quota_block_leaves_the_node_pending(self, session_factory):
        client = FakeClient(
            errors={
                "/marketnavigation/2": _ig_error(
                    403, "error.public-api.exceeded-api-key-allowance"
                )
            }
        )
        crawler = CatalogCrawler(client=client, session_factory=session_factory)

        progress = await crawler.crawl()

        assert not progress.complete
        nodes = await _nodes(session_factory)
        assert nodes["1"].status == STATUS_DONE
        assert nodes["2"].status == STATUS_PENDING
        assert "boom" in progress.last_error
        # The run stopped on the blocked node: nothing beyond it was fetched.
        assert client.calls == [
            "/marketnavigation",
            "/marketnavigation/1",
            "/marketnavigation/2",
        ]

    async def test_next_run_resumes_without_refetching(self, session_factory):
        client = FakeClient(errors={"/marketnavigation/2": _ig_error(500)})
        crawler = CatalogCrawler(client=client, session_factory=session_factory)
        await crawler.crawl()
        calls_before = list(client.calls)

        progress = await crawler.crawl()

        assert progress.complete
        assert progress.markets == 3
        # Second run picks up at the blocked node — no root re-seed, no re-visit
        # of the nodes already done.
        assert client.calls[len(calls_before) :] == [
            "/marketnavigation/2",
            "/marketnavigation/11",
        ]

    async def test_network_error_is_recoverable(self, session_factory):
        client = FakeClient(
            errors={"/marketnavigation/2": httpx.ConnectError("connection reset")}
        )
        crawler = CatalogCrawler(client=client, session_factory=session_factory)

        first = await crawler.crawl()
        assert not first.complete
        assert (await _nodes(session_factory))["2"].status == STATUS_PENDING

        second = await crawler.crawl()
        assert second.complete


class TestPermanentErrors:
    async def test_rejected_node_is_skipped_not_retried(self, session_factory):
        client = FakeClient(errors={"/marketnavigation/2": _ig_error(404)})
        crawler = CatalogCrawler(client=client, session_factory=session_factory)

        progress = await crawler.crawl()

        nodes = await _nodes(session_factory)
        assert nodes["2"].status == STATUS_ERROR
        assert nodes["2"].last_error and "boom" in nodes["2"].last_error
        # The rest of the tree was still crawled, and the frontier is drained.
        assert progress.nodes_error == 1
        assert progress.nodes_pending == 0
        assert progress.complete
        assert "/marketnavigation/11" in client.calls


class TestUpsert:
    async def test_rerun_restamps_instead_of_duplicating(self, session_factory):
        crawler = CatalogCrawler(client=FakeClient(), session_factory=session_factory)
        await crawler.crawl()
        before = await _markets(session_factory)
        first_seen = before["CS.D.EURUSD.CFD.IP"].first_seen_at

        progress = await crawler.crawl(reset=True)

        after = await _markets(session_factory)
        assert len(after) == 3
        assert progress.markets == 3
        assert progress.markets_added == 0  # nothing new: all three re-stamped
        row = after["CS.D.EURUSD.CFD.IP"]
        assert row.first_seen_at == first_seen
        assert row.last_seen_at >= first_seen

    async def test_reset_rewalks_the_tree(self, session_factory):
        client = FakeClient()
        crawler = CatalogCrawler(client=client, session_factory=session_factory)
        await crawler.crawl()

        await crawler.crawl(reset=True)

        # Root seed + three nodes, twice.
        assert len(client.calls) == 8
        assert (await _nodes(session_factory))["1"].status == STATUS_DONE

    async def test_market_without_epic_is_ignored(self, session_factory):
        tree = {
            "/marketnavigation": {"nodes": [{"id": "1", "name": "Forex"}]},
            "/marketnavigation/1": {
                "nodes": [],
                "markets": [{"instrumentName": "no epic here"}, {"epic": " "}],
            },
        }
        crawler = CatalogCrawler(
            client=FakeClient(tree=tree), session_factory=session_factory
        )

        progress = await crawler.crawl()

        assert progress.complete
        assert progress.markets == 0


class TestPruning:
    async def test_excluded_node_and_its_subtree_are_skipped(self, session_factory):
        client = FakeClient()
        crawler = CatalogCrawler(
            client=client,
            session_factory=session_factory,
            exclude_names=("forex",),  # case-insensitive
        )

        progress = await crawler.crawl()

        nodes = await _nodes(session_factory)
        assert set(nodes) == {"2"}
        assert progress.markets == 1
        assert "/marketnavigation/1" not in client.calls

    async def test_max_depth_stops_the_descent(self, session_factory):
        client = FakeClient()
        crawler = CatalogCrawler(
            client=client, session_factory=session_factory, max_depth=0
        )

        progress = await crawler.crawl()

        # Roots are depth 0 and still crawled; their children are not enqueued.
        assert progress.nodes_total == 2
        assert progress.complete
        assert "/marketnavigation/11" not in client.calls


class TestSingleRun:
    async def test_second_call_while_running_is_a_noop(self, session_factory):
        release = asyncio.Event()

        class BlockingClient(FakeClient):
            """Hangs on the first inner node until the test releases it."""

            def __init__(self) -> None:
                super().__init__()
                self.reached = asyncio.Event()

            async def get(self, endpoint: str, **kwargs) -> dict:
                if endpoint == "/marketnavigation/1":
                    self.reached.set()
                    await release.wait()
                return await super().get(endpoint, **kwargs)

        client = BlockingClient()
        crawler = CatalogCrawler(client=client, session_factory=session_factory)
        task = asyncio.create_task(crawler.crawl())
        await client.reached.wait()  # the crawl is now inside a node fetch

        assert crawler.is_running
        second = await crawler.crawl()
        assert not second.complete  # returned the live snapshot, started nothing

        release.set()
        progress = await task
        assert progress.complete
        # One visit per node despite the concurrent request.
        assert client.calls.count("/marketnavigation/1") == 1
