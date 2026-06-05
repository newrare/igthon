"""Accounts endpoint — GET /accounts."""

from src.api.client import IGClient


async def get_accounts(client: IGClient) -> list[dict]:
    """Fetch all accounts for the authenticated user."""
    data = await client.get("/accounts", version=1)
    return data.get("accounts", [])


async def get_account_balance(client: IGClient, account_id: str) -> dict | None:
    """Get the balance of a specific account."""
    accounts = await get_accounts(client)
    for account in accounts:
        if account["accountId"] == account_id:
            return account.get("balance", {})
    return None
