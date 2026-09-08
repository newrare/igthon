"""Build the static IG market catalog by crawling ``/marketnavigation``.

The bot's daily epic discovery is limited by the hardcoded search terms in
``SCANNER_SEARCH_TERMS`` and by the fact that ``/markets?searchTerm=`` has no
pagination (IG truncates the answer). This tool builds the reference list the
selection should draw from instead: **every** instrument the broker exposes,
walked from IG's own navigation tree and stored in the ``market_catalog`` table.

The crawl is **resumable**. The tree is far larger than any IG per-minute
allowance, so every discovered node is persisted before it is visited
(``catalog_node``): a run stopped by a quota block, a network drop or Ctrl-C
leaves its nodes ``pending``, and the next run starts exactly there. Nothing
already fetched is fetched again. Practically: launch it in the evening, re-launch
it until it prints ``complete``.

Calls go through the :class:`~src.core.api_queue.APIQueue` at ``Priority.LOW``,
like every other IG call in the project — the queue's worker waits for the guard
cooldown and re-queues on a quota block instead of failing the call.

**This opens an IG session.** IG allows a limited number of concurrent sessions,
so prefer running it while the bot is stopped, or expect the bot's session to be
re-authenticated. The same crawl is also available without a second session from
the dashboard: Actions → "Crawl Market Catalog".

Usage:
    python -m src.scripts.crawl_catalog                    # crawl / resume
    python -m src.scripts.crawl_catalog --status           # report, no API call
    python -m src.scripts.crawl_catalog --max-nodes 200    # bounded run
    python -m src.scripts.crawl_catalog --reset            # re-walk the whole tree
    python -m src.scripts.crawl_catalog --exclude Options --exclude Bonds
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from src.core.api.client import IGClient
from src.core.api_guard import APIGuard
from src.core.api_queue import APIQueue
from src.core.config import get_settings
from src.markets.catalog_crawler import CatalogCrawler
from src.models.database import create_session_factory

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s — %(message)s")


def _report(progress) -> None:  # noqa: ANN001 - CatalogProgress
    """Print the frontier state and what the next run would do."""
    print(f"\n{progress.summary()}")
    if progress.nodes_visited:
        print(
            f"this run: {progress.nodes_visited} node(s) visited, "
            f"{progress.markets_added} new market(s)"
        )
    if progress.complete:
        print("Catalog complete — every navigation node has been visited.")
    elif progress.nodes_pending:
        print(
            f"{progress.nodes_pending} node(s) still pending — re-run the same "
            "command to resume where it stopped."
        )
    if progress.last_error:
        print(f"stopped on: {progress.last_error}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status",
        action="store_true",
        help="Report the catalog/frontier state and exit (no IG session).",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop the frontier and re-walk the whole tree (markets are kept).",
    )
    parser.add_argument(
        "--max-nodes",
        type=int,
        default=None,
        help="Stop after this many nodes (default: until drained or blocked).",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="TERM",
        help="Skip nodes whose name contains TERM, and their subtree (repeatable).",
    )
    args = parser.parse_args()

    settings = get_settings()
    session_factory = create_session_factory(settings)

    if args.status:
        crawler = CatalogCrawler(
            client=None,  # type: ignore[arg-type] - no API call in status mode
            session_factory=session_factory,
        )
        _report(await crawler.progress())
        return

    async with IGClient(settings) as client:
        # Same plumbing as the bot (src/main.py): the guard tracks IG's limits and
        # the queue's worker waits-then-resumes instead of burning the call.
        queue = APIQueue(
            client,
            APIGuard(),
            max_attempts=settings.queue_max_attempts,
            retry_margin_seconds=settings.queue_retry_margin_seconds,
        )
        await queue.start()
        crawler = CatalogCrawler(
            client=queue,
            session_factory=session_factory,
            exclude_names=tuple(args.exclude),
        )
        try:
            progress = await crawler.crawl(reset=args.reset, max_nodes=args.max_nodes)
        finally:
            await queue.stop()

    _report(progress)


if __name__ == "__main__":
    asyncio.run(main())
