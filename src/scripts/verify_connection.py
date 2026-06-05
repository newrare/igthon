"""Quick script to verify the IG API connection.

Usage:
    cd python/
    python -m src.verify_connection

Requires a .env file with valid IG credentials.
"""

import asyncio
import logging
import sys

from src.api.client import IGClient
from src.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    """Connect to IG API, fetch account info, and print a summary."""
    settings = get_settings()

    logger.info("Environment: %s", settings.ig_env.value)
    logger.info("Base URL: %s", settings.ig_base_url)

    async with IGClient(settings) as client:
        logger.info("Authentication successful!")

        # Fetch accounts to confirm the connection works end-to-end
        data = await client.get("/accounts", version=1)
        accounts = data.get("accounts", [])

        print("\n--- IG API Connection OK ---")
        print(f"Environment : {settings.ig_env.value}")
        print(f"Accounts    : {len(accounts)}")
        for acc in accounts:
            balance = acc.get("balance", {})
            print(
                f"  • {acc['accountId']} | "
                f"{acc['accountName']} | "
                f"Balance: {balance.get('balance', '?')} {acc.get('currency', '')}"
            )
        print("----------------------------\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.error("Connection failed: %s", exc)
        sys.exit(1)
