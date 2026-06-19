"""Test the PriceBuffer + MarketDataService pipeline.

Usage:
    cd python/
    python -m src.test_buffer IX.D.DAX.IFMM.IP
"""

import asyncio
import logging
import sys

from src.core.api.client import IGClient
from src.core.config import get_settings
from src.feed.market_data import MarketDataService
from src.feed.price_buffer import PriceBuffer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    """Fetch candles for an epic and display buffer stats."""
    settings = get_settings()
    epic = sys.argv[1] if len(sys.argv) > 1 else "IX.D.DAX.IFMM.IP"

    buffer = PriceBuffer(max_candles=100)

    async with IGClient(settings) as client:
        service = MarketDataService(client, buffer)

        # Fetch 30 candles (MINUTE resolution)
        candles = await service.fetch_candles(epic, "MINUTE", 30)

        if not candles:
            print("No data returned.")
            return

        epic_buf = buffer.get(epic)
        last = epic_buf.last

        print(f"\n--- PriceBuffer: {epic} ---")
        print(f"  Candles in buffer : {len(epic_buf)}")
        print(f"  Last timestamp    : {last.timestamp}")
        print(f"  Last bid close    : {last.bid_close}")
        print(f"  Last offer close  : {last.offer_close}")
        print(f"  Last spread       : {last.spread:.2f}")
        print(f"  Avg spread        : {sum(epic_buf.spreads) / len(epic_buf):.2f}")

        bids = epic_buf.bid_closes
        print(f"  Bid high (buffer) : {max(bids):.1f}")
        print(f"  Bid low (buffer)  : {min(bids):.1f}")
        print(f"  Bid range         : {max(bids) - min(bids):.1f}")
        print("---")
        print(f"\n  Last 5 bid closes : {bids[-5:]}")
        print()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.error("Error: %s", exc)
        sys.exit(1)
