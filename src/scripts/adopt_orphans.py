"""One-off: adopt live IG positions the DB is not tracking.

Runs the real ``TradingService.sync_open_positions`` reconciliation (which now
adopts untracked IG positions) against the live broker and the real database,
via a thin adapter that drops the APIQueue-only kwargs (priority/label) so a
plain IGClient can be used outside the running app.

Usage:
    python -m src.scripts.adopt_orphans
"""

import asyncio
import logging

from src.core.api.client import IGClient
from src.core.config import get_settings
from src.execution.trading import TradeConfig, TradingService
from src.models.database import create_session_factory

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s — %(message)s")


class _ClientAdapter:
    """Wrap IGClient so it tolerates the APIQueue-only kwargs the service passes."""

    def __init__(self, client: IGClient) -> None:
        self._c = client

    async def get(self, endpoint, *, version=1, suppress_error_logging=False, **_):
        return await self._c.get(
            endpoint, version=version, suppress_error_logging=suppress_error_logging
        )

    async def post(self, endpoint, payload, *, version=1, **_):
        return await self._c.post(endpoint, payload, version=version)

    async def put(self, endpoint, payload, *, version=1, **_):
        return await self._c.put(endpoint, payload, version=version)

    async def delete(self, endpoint, payload, *, version=1, **_):
        return await self._c.delete(endpoint, payload, version=version)


async def main() -> None:
    settings = get_settings()
    session_factory = create_session_factory(settings)

    async with IGClient(settings) as client:
        async with session_factory() as session:
            svc = TradingService(
                client=_ClientAdapter(client),
                db_session=session,
                config=TradeConfig.from_settings(settings),
            )
            live = await svc.sync_open_positions()
            print(
                f"\nReconciled. IG reports {len(live)} epic(s) live: "
                f"{sorted(live)}\n"
            )


if __name__ == "__main__":
    asyncio.run(main())
