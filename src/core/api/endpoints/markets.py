"""Markets endpoint — GET /markets."""

from src.core.api.client import IGClient


async def get_market(client: IGClient, epic: str) -> dict:
    """Fetch market details for a single epic.

    Args:
        client: Authenticated IG client.
        epic: Market identifier (e.g. "IX.D.DAX.DAILY.IP").

    Returns:
        Full market details including instrument, dealing rules, snapshot.
    """
    return await client.get(f"/markets/{epic}", version=3)


async def search_markets(client: IGClient, search_term: str) -> list[dict]:
    """Search for markets by keyword.

    Args:
        client: Authenticated IG client.
        search_term: Keyword to search (e.g. "DAX", "EUR/USD").

    Returns:
        List of matching markets.
    """
    data = await client.get(f"/markets?searchTerm={search_term}", version=1)
    return data.get("markets", [])


async def get_markets(client: IGClient, epics: list[str]) -> list[dict]:
    """Fetch multiple markets at once (max 50 per IG docs).

    Args:
        client: Authenticated IG client.
        epics: List of epic identifiers.

    Returns:
        List of market node details.
    """
    epics_str = ",".join(epics)
    data = await client.get(f"/markets?epics={epics_str}", version=2)
    return data.get("marketDetails", [])
