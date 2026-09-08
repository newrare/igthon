"""Market catalog models — the full IG instrument catalog and its crawl frontier.

Two tables, both written only by :class:`~src.markets.catalog_crawler.CatalogCrawler`:

- ``market_catalog`` — one row per epic IG exposes, discovered by walking the
  market-navigation tree. This is a **static reference list** (it is never pruned
  by the daily epic discovery, unlike ``epic``): rows accumulate and carry
  ``last_seen_at`` so a market IG has dropped is recognisable by an old stamp.
- ``catalog_node`` — the crawl frontier: every navigation node ever discovered
  with its visit status. It is what makes the crawl **resumable**: a run that
  stops on a quota block leaves its nodes ``pending``, and the next run picks
  them up where it left off.
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models.database import Base


class MarketCatalog(Base):
    """One tradable instrument (epic) as published by IG's market navigation."""

    __tablename__ = "market_catalog"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # IG epic code. Longer than ``Epic.name`` (30) on purpose: share/ETF epics in
    # the full catalog run longer than the forex/index epics the bot trades.
    epic: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    instrument_name: Mapped[str | None] = mapped_column(String(120))
    # IG ``instrumentType`` (CURRENCIES, INDICES, COMMODITIES, SHARES, ...).
    instrument_type: Mapped[str | None] = mapped_column(String(40), index=True)
    expiry: Mapped[str | None] = mapped_column(String(30))
    # Navigation node this market was found under, plus its readable path
    # ("Forex / Major FX pairs"), so the catalog keeps IG's own taxonomy.
    node_id: Mapped[str | None] = mapped_column(String(40), index=True)
    node_path: Mapped[str | None] = mapped_column(String(255))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class CatalogNode(Base):
    """A market-navigation node and its crawl state (the resumable frontier)."""

    __tablename__ = "catalog_node"

    # IG navigation node id — the crawl key, so a node is never visited twice.
    node_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(120))
    parent_id: Mapped[str | None] = mapped_column(String(40))
    path: Mapped[str | None] = mapped_column(String(255))
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # "pending" (to visit), "done" (children + markets recorded) or "error"
    # (IG rejected this node for good — skipped instead of blocking the crawl).
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="pending", index=True
    )
    markets_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(200))
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    visited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
