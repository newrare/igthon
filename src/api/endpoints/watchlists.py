"""Watchlists endpoint — GET /watchlists."""

from src.api.client import IGClient


async def get_watchlists(client: IGClient) -> list[dict]:
    """Fetch all watchlists."""
    data = await client.get("/watchlists", version=1)
    return data.get("watchlists", [])


async def get_watchlist(client: IGClient, watchlist_id: str) -> list[dict]:
    """Fetch markets in a specific watchlist."""
    data = await client.get(f"/watchlists/{watchlist_id}", version=1)
    return data.get("markets", [])
