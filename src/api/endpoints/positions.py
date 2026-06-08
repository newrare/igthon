"""Positions endpoint — GET/POST/DELETE /positions."""

from src.api.client import IGClient


async def get_positions(client: IGClient) -> list[dict]:
    """Fetch all open positions."""
    data = await client.get("/positions", version=2)
    return data.get("positions", [])


async def get_position(client: IGClient, deal_id: str) -> dict:
    """Fetch a single position by deal ID."""
    return await client.get(f"/positions/{deal_id}", version=2)


async def open_position(client: IGClient, payload: dict) -> dict:
    """Open a new position (POST /positions/otc).

    Args:
        client: Authenticated IG client.
        payload: Order payload (epic, direction, size, etc.).

    Returns:
        Deal reference for confirmation.
    """
    return await client.post("/positions/otc", payload, version=2)


async def close_position(client: IGClient, payload: dict) -> dict:
    """Close a position (DELETE /positions/otc).

    Args:
        client: Authenticated IG client.
        payload: Close payload (dealId, direction, size, orderType, expiry, forceOpen).

    Returns:
        Deal reference for confirmation.
    """
    return await client.delete("/positions/otc", payload, version=1)


async def get_deal_confirmation(client: IGClient, deal_reference: str) -> dict:
    """Get deal confirmation after open/close.

    Args:
        client: Authenticated IG client.
        deal_reference: Reference returned from open/close.

    Returns:
        Confirmation details (dealId, status, reason, etc.).
    """
    return await client.get(f"/confirms/{deal_reference}", version=1)
