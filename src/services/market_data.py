"""Market data service — fetches prices from IG and feeds the PriceBuffer.

This replaces the old approach of storing every tick in the DB.
Prices are fetched on demand from the IG API and kept in memory only.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from src.services.price_buffer import Candle, PriceBuffer

if TYPE_CHECKING:
    from src.api.client import IGClient
    from src.services.api_queue import APIQueue

logger = logging.getLogger(__name__)


def _parse_ig_timestamp(snapshot_time: str) -> datetime:
    """Parse IG timestamp format (e.g. '2026/06/01 09:30:00').

    IG returns timestamps in various formats depending on the endpoint.
    """
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y:%m:%d-%H:%M:%S"):
        try:
            return datetime.strptime(snapshot_time, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    # Fallback: use current time
    return datetime.now(UTC)


def _parse_candle(price_point: dict) -> Candle:
    """Convert an IG price point to a Candle object.

    IG price structure:
    {
        "snapshotTime": "2026/06/01 09:30:00",
        "openPrice": {"bid": ..., "ask": ..., "lastTraded": null},
        "closePrice": {"bid": ..., "ask": ..., "lastTraded": null},
        "highPrice": {"bid": ..., "ask": ..., "lastTraded": null},
        "lowPrice": {"bid": ..., "ask": ..., "lastTraded": null},
        "lastTradedVolume": 123
    }
    """
    return Candle(
        timestamp=_parse_ig_timestamp(price_point.get("snapshotTime", "")),
        bid_open=float(price_point["openPrice"]["bid"]),
        bid_close=float(price_point["closePrice"]["bid"]),
        bid_high=float(price_point["highPrice"]["bid"]),
        bid_low=float(price_point["lowPrice"]["bid"]),
        offer_open=float(price_point["openPrice"]["ask"]),
        offer_close=float(price_point["closePrice"]["ask"]),
        offer_high=float(price_point["highPrice"]["ask"]),
        offer_low=float(price_point["lowPrice"]["ask"]),
        volume=int(price_point.get("lastTradedVolume", 0) or 0),
    )


class MarketDataService:
    """Service to fetch and manage market price data.

    Fetches candles from the IG /prices endpoint and maintains
    an in-memory PriceBuffer for real-time indicator calculations.
    """

    def __init__(self, client: IGClient | APIQueue, buffer: PriceBuffer) -> None:
        self._client = client
        self._buffer = buffer

    @property
    def buffer(self) -> PriceBuffer:
        """Access the underlying price buffer."""
        return self._buffer

    async def fetch_candles(
        self,
        epic: str,
        resolution: str = "MINUTE",
        num_points: int = 50,
    ) -> list[Candle]:
        """Fetch candles from IG API and update the buffer.

        Args:
            epic: Market identifier.
            resolution: Candle resolution (MINUTE, MINUTE_5, etc.).
            num_points: Number of candles to fetch (max 1000).

        Returns:
            List of parsed Candle objects.
        """
        data = await self._client.get(
            f"/prices/{epic}/{resolution}/{num_points}", version=2
        )

        prices = data.get("prices", [])
        if not prices:
            logger.warning("No price data returned for %s", epic)
            return []

        candles = [_parse_candle(p) for p in prices]
        self._buffer.update(epic, candles)

        logger.info(
            "Fetched %d candles for %s (%s)",
            len(candles),
            epic,
            resolution,
        )
        return candles

    async def refresh_all(
        self,
        epics: list[str],
        resolution: str = "MINUTE",
        num_points: int = 50,
    ) -> None:
        """Refresh candle data for all tracked epics.

        All fetches are enqueued at once via ``asyncio.gather`` so the APIQueue
        receives every request immediately and drains them under its own
        rate-limit control — the queue counter then reflects the real backlog.
        A single epic failing does not abort the others.

        Args:
            epics: List of epic identifiers to refresh.
            resolution: Candle resolution.
            num_points: Number of candles per epic.
        """
        results = await asyncio.gather(
            *[self.fetch_candles(epic, resolution, num_points) for epic in epics],
            return_exceptions=True,
        )
        for epic, result in zip(epics, results):
            if isinstance(result, BaseException):
                logger.warning("Failed to refresh candles for %s: %s", epic, result)

    async def fetch_latest_candles(
        self,
        epic: str,
        resolution: str = "MINUTE",
        num_points: int = 2,
    ) -> list[Candle]:
        """Fetch a small number of recent candles and append to the buffer.

        Unlike fetch_candles, this does NOT clear the existing buffer — it only
        appends candles newer than the most recent one already stored. Use this
        for incremental 30-second updates to avoid exhausting the IG historical
        data allowance.
        """
        data = await self._client.get(
            f"/prices/{epic}/{resolution}/{num_points}", version=2
        )
        prices = data.get("prices", [])
        if not prices:
            return []

        candles = [_parse_candle(p) for p in prices]
        self._buffer.append_candles(epic, candles)
        logger.debug("Incremental update: %s (%d points fetched)", epic, len(candles))
        return candles

    async def get_current_price(self, epic: str) -> dict | None:
        """Get the latest price snapshot for an epic (single API call).

        Returns the market snapshot without storing in the buffer.
        Useful for quick checks (e.g. before opening a position).
        """
        data = await self._client.get(f"/markets/{epic}", version=3)
        return data.get("snapshot")
