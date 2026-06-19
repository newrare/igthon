"""History endpoint — GET /history."""

from src.core.api.client import IGClient


async def get_activity_history(
    client: IGClient,
    from_date: str = "",
    to_date: str = "",
) -> list[dict]:
    """Fetch activity history.

    Args:
        client: Authenticated IG client.
        from_date: Start date (format: 2023-01-01T00:00:00).
        to_date: End date.

    Returns:
        List of activity records.
    """
    params = "/history/activity"
    if from_date and to_date:
        params += f"?from={from_date}&to={to_date}"
    data = await client.get(params, version=3)
    return data.get("activities", [])


async def get_transaction_history(
    client: IGClient,
    from_date: str = "",
    to_date: str = "",
) -> list[dict]:
    """Fetch transaction history.

    Args:
        client: Authenticated IG client.
        from_date: Start date.
        to_date: End date.

    Returns:
        List of transaction records.
    """
    params = "/history/transactions"
    if from_date and to_date:
        params += f"?from={from_date}&to={to_date}"
    data = await client.get(params, version=2)
    return data.get("transactions", [])
