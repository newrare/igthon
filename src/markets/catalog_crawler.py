"""Static market catalog builder — a resumable crawl of IG's navigation tree.

The daily epic discovery (:class:`~src.markets.market_scanner.MarketScanner`) can
only ever see what its hardcoded search terms match, and ``GET /markets?searchTerm=``
has no pagination (IG truncates the response), so the traded universe is capped by
the term list rather than by what the broker actually offers. This module builds
the missing reference: the **complete** instrument catalog, walked from IG's own
taxonomy instead of guessed from keywords.

IG API endpoints used:
- GET /marketnavigation        (v1) — root nodes
- GET /marketnavigation/{id}   (v1) — ``{nodes: [...], markets: [...]}`` for a node

The crawl is **resumable by design**, because the whole tree is far larger than any
IG per-minute allowance:

- every discovered node is persisted as ``pending`` in ``catalog_node`` before it
  is visited, and flipped to ``done`` in the same transaction that stores its
  children and markets — so a run interrupted at any point leaves a consistent
  frontier;
- a node that fails on a **recoverable** error (quota block, HTTP 5xx, network,
  expired session) stays ``pending`` and the run stops there: the next run picks
  that exact node up first. Nothing is lost and nothing is re-fetched;
- a node IG rejects for good (HTTP 4xx) is marked ``error`` and skipped, so one
  bad node can never block the rest of the catalog;
- markets are upserted, never deleted: ``last_seen_at`` tells a market IG has
  since dropped from a stale one.

Every call goes through the :class:`~src.core.api_queue.APIQueue` at
``Priority.LOW``, so the crawl always yields to trading calls and price collection,
and a quota block makes it *wait* rather than fail.

Typical usage (dashboard action, or ``python -m src.scripts.crawl_catalog``)::

    crawler = CatalogCrawler(api_queue, session_factory)
    progress = await crawler.crawl()
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from sqlalchemy import delete, func, select

from src.core.api.client import IGAPIError
from src.core.api_guard import _QUOTA_EXCEEDED_CODES
from src.core.api_queue import Priority
from src.models.market_catalog import CatalogNode, MarketCatalog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.core.api_queue import APIQueue

logger = logging.getLogger(__name__)

# Node visit states persisted in ``catalog_node.status``.
STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_ERROR = "error"


@dataclass(frozen=True)
class _PendingNode:
    """Plain snapshot of a node to visit.

    A detached ORM instance would be a liability here: the row is read in its own
    short-lived session and used after it closes, so only the four fields the
    crawl needs are copied out.
    """

    node_id: str
    name: str | None
    path: str | None
    depth: int


@dataclass(frozen=True)
class CatalogProgress:
    """Snapshot of the catalog and of the crawl frontier, for the dashboard/CLI."""

    running: bool
    nodes_total: int
    nodes_pending: int
    nodes_done: int
    nodes_error: int
    markets: int
    # Per-run counters (in-memory: reset when the process restarts).
    nodes_visited: int
    markets_added: int
    started_at: datetime | None
    finished_at: datetime | None
    last_error: str

    @property
    def complete(self) -> bool:
        """True when the frontier has been fully drained at least once."""
        return self.nodes_total > 0 and self.nodes_pending == 0

    def summary(self) -> str:
        """One-line human summary (log line / CLI output)."""
        state = "running" if self.running else ("complete" if self.complete else "idle")
        return (
            f"catalog {state}: {self.markets} markets · "
            f"{self.nodes_done}/{self.nodes_total} nodes done, "
            f"{self.nodes_pending} pending, {self.nodes_error} error"
        )


@dataclass
class CatalogCrawler:
    """Walks ``/marketnavigation`` and persists the full instrument catalog.

    Args:
        client: API queue (or any client exposing its ``get``) — all calls are
            enqueued at ``Priority.LOW``.
        session_factory: Async SQLAlchemy session factory.
        max_depth: Safety stop on tree depth. IG's tree is ~5 levels deep; a
            deeper child is not enqueued (guards against a pathological tree).
        exclude_names: Case-insensitive substrings; a node whose name matches one
            is not enqueued, pruning its whole subtree (e.g. ``("Options",)``).
    """

    # IG navigation endpoints (v1).
    ROOT_ENDPOINT: ClassVar[str] = "/marketnavigation"
    NODE_ENDPOINT: ClassVar[str] = "/marketnavigation/{node_id}"
    # Column widths in ``catalog_node`` / ``market_catalog``.
    _PATH_MAX: ClassVar[int] = 255
    _NAME_MAX: ClassVar[int] = 120
    _ERROR_MAX: ClassVar[int] = 200

    client: APIQueue
    session_factory: async_sessionmaker
    max_depth: int = 12
    exclude_names: tuple[str, ...] = ()

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _nodes_visited: int = field(default=0, init=False)
    _markets_added: int = field(default=0, init=False)
    _started_at: datetime | None = field(default=None, init=False)
    _finished_at: datetime | None = field(default=None, init=False)
    _last_error: str = field(default="", init=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """True while a crawl is in progress (only one runs at a time)."""
        return self._lock.locked()

    async def progress(self) -> CatalogProgress:
        """Read the current catalog/frontier counts from the database."""
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(CatalogNode.status, func.count()).group_by(
                        CatalogNode.status
                    )
                )
            ).all()
            counts = {str(status): int(count) for status, count in rows}
            markets = int(
                (await session.scalar(select(func.count()).select_from(MarketCatalog)))
                or 0
            )
        return CatalogProgress(
            running=self.is_running,
            nodes_total=sum(counts.values()),
            nodes_pending=counts.get(STATUS_PENDING, 0),
            nodes_done=counts.get(STATUS_DONE, 0),
            nodes_error=counts.get(STATUS_ERROR, 0),
            markets=markets,
            nodes_visited=self._nodes_visited,
            markets_added=self._markets_added,
            started_at=self._started_at,
            finished_at=self._finished_at,
            last_error=self._last_error,
        )

    async def crawl(
        self, *, reset: bool = False, max_nodes: int | None = None
    ) -> CatalogProgress:
        """Drain the pending frontier, seeding it from the root when empty.

        Args:
            reset: Drop the frontier and re-seed from the root nodes. Catalog
                markets are kept (and re-stamped as they are seen again), so a
                reset re-walks the tree without ever emptying the reference list.
            max_nodes: Stop after this many node visits (``None`` = until the
                frontier is empty or a recoverable error stops the run).

        Returns:
            The progress snapshot at the end of the run. Calling it while a crawl
            is already running is a no-op that returns the live snapshot.
        """
        if self.is_running:
            logger.warning("Catalog crawl already running — ignoring new request")
            return await self.progress()

        async with self._lock:
            self._nodes_visited = 0
            self._markets_added = 0
            self._started_at = datetime.now(UTC)
            self._finished_at = None
            self._last_error = ""

            if reset:
                await self._reset_frontier()
            seeded = await self._seed_root_if_empty()

            while seeded and (max_nodes is None or self._nodes_visited < max_nodes):
                node = await self._next_pending()
                if node is None:
                    logger.info("Catalog crawl: frontier drained")
                    break
                if not await self._visit(node):
                    break  # recoverable failure — node stays pending for next run

            self._finished_at = datetime.now(UTC)
            progress = await self.progress()
            logger.info("Catalog crawl finished — %s", progress.summary())
            return progress

    # ------------------------------------------------------------------
    # Frontier management
    # ------------------------------------------------------------------

    async def _reset_frontier(self) -> None:
        """Drop every known node so the next seed re-walks the whole tree."""
        async with self.session_factory() as session:
            await session.execute(delete(CatalogNode))
            await session.commit()
        logger.info("Catalog crawl: frontier reset")

    async def _seed_root_if_empty(self) -> bool:
        """Insert the root navigation nodes when no node is known yet.

        Returns:
            False when IG does not serve the navigation tree at all (the demo
            gateway answers ``404`` on ``/marketnavigation``, whatever the
            version or auth) — the crawl then has nothing to walk.
        """
        async with self.session_factory() as session:
            known = int(
                (await session.scalar(select(func.count()).select_from(CatalogNode)))
                or 0
            )
        if known:
            return True

        try:
            data = await self._fetch(
                self.ROOT_ENDPOINT,
                label="GET /marketnavigation",
                expect_not_found=True,
            )
        except IGAPIError as exc:
            if exc.response.status_code != 404:
                raise
            self._last_error = "IG navigation unavailable (404 on /marketnavigation)"
            logger.warning(
                "Catalog crawl: IG does not serve /marketnavigation on this "
                "environment (404) — nothing to crawl"
            )
            return False
        async with self.session_factory() as session:
            added = await self._add_children(
                session, data.get("nodes") or [], parent=None
            )
            # The root payload normally carries no market, but store any it does.
            self._markets_added += await self._upsert_markets(
                session, data.get("markets") or [], node_id=None, node_path=None
            )
            await session.commit()
        logger.info("Catalog crawl: seeded %d root nodes", added)
        return True

    async def _next_pending(self) -> _PendingNode | None:
        """Return the next node to visit — shallowest first (breadth-first).

        Breadth-first matters for a crawl that may be stopped at any time: a
        partial catalog then covers every top-level asset class rather than one
        deep branch.
        """
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(
                        CatalogNode.node_id,
                        CatalogNode.name,
                        CatalogNode.path,
                        CatalogNode.depth,
                    )
                    .where(CatalogNode.status == STATUS_PENDING)
                    .order_by(
                        CatalogNode.depth,
                        CatalogNode.discovered_at,
                        CatalogNode.node_id,
                    )
                    .limit(1)
                )
            ).first()
        if row is None:
            return None
        return _PendingNode(node_id=row[0], name=row[1], path=row[2], depth=row[3])

    async def _visit(self, node: _PendingNode) -> bool:
        """Fetch one node, store its children and markets, and mark it done.

        Returns:
            True to keep crawling, False when the run must stop and resume later
            (the node is left ``pending`` in that case).
        """
        endpoint = self.NODE_ENDPOINT.format(node_id=node.node_id)
        try:
            data = await self._fetch(endpoint, label=f"GET nav {node.name or ''}")
        except Exception as exc:
            if _is_recoverable(exc):
                self._last_error = str(exc)[: self._ERROR_MAX]
                logger.warning(
                    "Catalog crawl stopped on node %s (%s) — resumes here next run: %s",
                    node.node_id,
                    node.path,
                    exc,
                )
                return False
            await self._mark_error(node.node_id, str(exc))
            logger.warning(
                "Catalog crawl: node %s (%s) rejected by IG, skipped: %s",
                node.node_id,
                node.path,
                exc,
            )
            return True

        children = data.get("nodes") or []
        markets = data.get("markets") or []
        async with self.session_factory() as session:
            await self._add_children(session, children, parent=node)
            added = await self._upsert_markets(
                session, markets, node_id=node.node_id, node_path=node.path
            )
            await session.execute(
                CatalogNode.__table__.update()
                .where(CatalogNode.node_id == node.node_id)
                .values(
                    status=STATUS_DONE,
                    markets_found=len(markets),
                    last_error=None,
                    visited_at=datetime.now(UTC),
                )
            )
            await session.commit()

        self._nodes_visited += 1
        self._markets_added += added
        logger.debug(
            "Catalog crawl: node %s (%s) → %d children, %d markets (%d new)",
            node.node_id,
            node.path,
            len(children),
            len(markets),
            added,
        )
        return True

    async def _add_children(
        self, session, children: list[dict], *, parent: _PendingNode | None
    ) -> int:
        """Insert unknown child nodes as ``pending``. Returns how many were added.

        Nodes matching ``exclude_names`` and nodes past ``max_depth`` are dropped
        with their whole subtree.
        """
        depth = 0 if parent is None else parent.depth + 1
        if depth > self.max_depth:
            logger.warning(
                "Catalog crawl: depth %d over max_depth %d under %s "
                "— subtree skipped",
                depth,
                self.max_depth,
                parent.path if parent else "root",
            )
            return 0

        candidates: dict[str, dict] = {}
        for child in children:
            node_id = str(child.get("id") or "").strip()
            if not node_id:
                continue
            name = str(child.get("name") or "")[: self._NAME_MAX]
            if self._is_excluded(name):
                logger.debug("Catalog crawl: excluded node %s (%s)", node_id, name)
                continue
            candidates[node_id] = {"name": name}
        if not candidates:
            return 0

        known = set(
            (
                await session.scalars(
                    select(CatalogNode.node_id).where(
                        CatalogNode.node_id.in_(list(candidates))
                    )
                )
            ).all()
        )
        now = datetime.now(UTC)
        added = 0
        for node_id, info in candidates.items():
            if node_id in known:
                continue
            name = info["name"]
            path = name if parent is None else f"{parent.path or ''} / {name}"
            session.add(
                CatalogNode(
                    node_id=node_id,
                    name=name,
                    parent_id=parent.node_id if parent else None,
                    path=path[: self._PATH_MAX],
                    depth=depth,
                    status=STATUS_PENDING,
                    markets_found=0,
                    discovered_at=now,
                )
            )
            added += 1
        return added

    async def _mark_error(self, node_id: str, message: str) -> None:
        """Flag a node as permanently unreachable so it is never retried."""
        async with self.session_factory() as session:
            await session.execute(
                CatalogNode.__table__.update()
                .where(CatalogNode.node_id == node_id)
                .values(
                    status=STATUS_ERROR,
                    last_error=message[: self._ERROR_MAX],
                    visited_at=datetime.now(UTC),
                )
            )
            await session.commit()

    # ------------------------------------------------------------------
    # Catalog persistence
    # ------------------------------------------------------------------

    async def _upsert_markets(
        self,
        session,
        markets: list[dict],
        *,
        node_id: str | None,
        node_path: str | None,
    ) -> int:
        """Insert new epics and re-stamp known ones. Returns the number inserted.

        A known epic keeps its ``first_seen_at`` and gets a fresh ``last_seen_at``
        plus the latest name/type/node, so the catalog reflects IG's current
        taxonomy without ever losing a market.
        """
        parsed: dict[str, dict] = {}
        for market in markets:
            epic = str(market.get("epic") or "").strip()
            if not epic:
                continue
            parsed[epic] = {
                "instrument_name": (
                    str(market.get("instrumentName") or "")[: self._NAME_MAX] or None
                ),
                "instrument_type": str(market.get("instrumentType") or "") or None,
                "expiry": str(market.get("expiry") or "") or None,
                "node_id": node_id,
                "node_path": (node_path or None),
            }
        if not parsed:
            return 0

        now = datetime.now(UTC)
        known = set(
            (
                await session.scalars(
                    select(MarketCatalog.epic).where(
                        MarketCatalog.epic.in_(list(parsed))
                    )
                )
            ).all()
        )
        added = 0
        for epic, values in parsed.items():
            if epic in known:
                await session.execute(
                    MarketCatalog.__table__.update()
                    .where(MarketCatalog.epic == epic)
                    .values(last_seen_at=now, **values)
                )
            else:
                session.add(
                    MarketCatalog(
                        epic=epic,
                        first_seen_at=now,
                        last_seen_at=now,
                        **values,
                    )
                )
                added += 1
        return added

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _fetch(
        self, endpoint: str, *, label: str, expect_not_found: bool = False
    ) -> dict:
        """GET a navigation endpoint through the queue at background priority."""
        return await self.client.get(
            endpoint,
            version=1,
            priority=Priority.LOW,
            label=label,
            expect_not_found=expect_not_found,
        )

    def _is_excluded(self, name: str) -> bool:
        """True when a node name matches one of the configured exclusions."""
        lowered = name.lower()
        return any(term.lower() in lowered for term in self.exclude_names if term)


def _is_recoverable(exc: BaseException) -> bool:
    """True when the failure is worth retrying on the next run.

    Recoverable: quota blocks, HTTP 5xx, auth expiry (401/403), request timeout
    (408), throttling (429), and anything that is not an HTTP status error at all
    (network drop, queue abandonment). The node stays ``pending``.

    Not recoverable: the remaining 4xx — IG telling us this node is malformed or
    gone. Retrying it every run would stall the crawl forever, so it is skipped.
    """
    if isinstance(exc, IGAPIError):
        if getattr(exc, "ig_error_code", "") in _QUOTA_EXCEEDED_CODES:
            return True
        status = exc.response.status_code
        return status >= 500 or status in (401, 403, 408, 429)
    return True
