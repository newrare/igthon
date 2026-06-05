"""Prices endpoint — GET /prices."""

from src.api.client import IGClient


async def get_prices(
    client: IGClient,
    epic: str,
    resolution: str = "MINUTE",
    num_points: int = 20,
) -> dict:
    """Fetch historical prices for an epic.

    Args:
        client: Authenticated IG client.
        epic: Market identifier.
        resolution: Candle resolution (SECOND, MINUTE, MINUTE_2, MINUTE_3,
                    MINUTE_5, MINUTE_10, MINUTE_15, MINUTE_30, HOUR, HOUR_2,
                    HOUR_3, HOUR_4, DAY, WEEK, MONTH).
        num_points: Number of data points.

    Returns:
        Price data including allowance info and price list.
    """
    return await client.get(f"/prices/{epic}/{resolution}/{num_points}", version=2)


async def get_prices_between(
    client: IGClient,
    epic: str,
    resolution: str,
    start: str,
    end: str,
) -> dict:
    """Fetch prices between two dates.

    Args:
        client: Authenticated IG client.
        epic: Market identifier.
        resolution: Candle resolution.
        start: Start datetime (format: 2023-01-01T09:00:00).
        end: End datetime.

    Returns:
        Price data for the period.
    """
    return await client.get(f"/prices/{epic}/{resolution}/{start}/{end}", version=2)
