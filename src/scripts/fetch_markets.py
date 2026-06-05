"""Fetch market info for a given epic — validates the full API pipeline.

Usage:
    cd python/
    python -m src.fetch_markets DAX
    python -m src.fetch_markets "EUR/USD"
    python -m src.fetch_markets              # lists open positions
"""

import asyncio
import logging
import sys

from src.api.client import IGClient
from src.api.endpoints.accounts import get_accounts
from src.api.endpoints.markets import get_market, search_markets
from src.api.endpoints.positions import get_positions
from src.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


async def show_positions(client: IGClient) -> None:
    """Display all open positions."""
    positions = await get_positions(client)
    if not positions:
        print("\nNo open positions.")
        return

    print(f"\n--- Open Positions ({len(positions)}) ---")
    for pos in positions:
        market = pos.get("market", {})
        position = pos.get("position", {})
        print(
            f"  • {market.get('epic', '?')} | "
            f"{position.get('direction', '?')} | "
            f"Size: {position.get('size', '?')} | "
            f"Open: {position.get('openLevel', '?')} | "
            f"P&L: {position.get('profit', '?')}"
        )
    print()


async def show_market_search(client: IGClient, term: str) -> None:
    """Search and display markets matching a term."""
    results = await search_markets(client, term)
    if not results:
        print(f"\nNo markets found for '{term}'.")
        return

    print(f"\n--- Markets matching '{term}' ({len(results)}) ---")
    for market in results[:10]:
        print(
            f"  • {market.get('epic', '?'):40s} | "
            f"{market.get('instrumentName', '?')}"
        )
    if len(results) > 10:
        print(f"  ... and {len(results) - 10} more.")
    print()


async def show_market_detail(client: IGClient, epic: str) -> None:
    """Display detailed info for a specific epic."""
    data = await get_market(client, epic)
    instrument = data.get("instrument", {})
    snapshot = data.get("snapshot", {})
    dealing = data.get("dealingRules", {})

    print(f"\n--- Market Detail: {epic} ---")
    print(f"  Name        : {instrument.get('name', '?')}")
    print(f"  Type        : {instrument.get('type', '?')}")
    print(f"  Status      : {snapshot.get('marketStatus', '?')}")
    print(f"  Bid         : {snapshot.get('bid', '?')}")
    print(f"  Offer       : {snapshot.get('offer', '?')}")
    print(f"  High        : {snapshot.get('high', '?')}")
    print(f"  Low         : {snapshot.get('low', '?')}")
    print(f"  Spread      : {snapshot.get('offer', 0) - snapshot.get('bid', 0):.3f}")
    min_stop = dealing.get("minNormalStopOrLimitDistance", {})
    print(f"  Min stop    : {min_stop.get('value', '?')} ({min_stop.get('unit', '')})")
    print()


async def main() -> None:
    """Main entry point."""
    settings = get_settings()
    args = sys.argv[1:]

    async with IGClient(settings) as client:
        # Show account balance
        accounts = await get_accounts(client)
        for acc in accounts:
            if acc["accountId"] == settings.ig_account_id:
                balance = acc.get("balance", {})
                print(
                    f"\nAccount: {acc['accountId']} | "
                    f"Balance: {balance.get('balance', '?')} {acc.get('currency', '')}"
                )
                break

        if not args:
            # No argument: show open positions
            await show_positions(client)
        else:
            term = args[0]
            # If it looks like a full epic (contains dots), get details
            if "." in term:
                await show_market_detail(client, term)
            else:
                # Otherwise search
                await show_market_search(client, term)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.error("Error: %s", exc)
        sys.exit(1)
