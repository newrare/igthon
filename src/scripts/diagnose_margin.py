"""Diagnose a 'margin in use' vs 'no open position' mismatch.

Compares what IG actually reports as open (GET /positions, the source of truth
for tied-up margin) against what the local DB believes is OPEN, and against what
the dashboard would show (DB rows with state=OPEN AND date=today).

Usage:
    python -m src.scripts.diagnose_margin
"""

import asyncio
import logging
from datetime import date

from sqlalchemy import select

from src.core.api.client import IGClient
from src.core.config import get_settings
from src.models.database import create_session_factory
from src.models.position import Position, PositionState

logging.basicConfig(level=logging.WARNING)


async def main() -> None:
    settings = get_settings()
    session_factory = create_session_factory(settings)

    async with IGClient(settings) as client:
        accounts = (await client.get("/accounts", version=1)).get("accounts", [])
        positions = (await client.get("/positions", version=2)).get("positions", [])

    print("\n=== IG ACCOUNT BALANCE ===")
    for acc in accounts:
        bal = acc.get("balance", {}) or {}
        marker = (
            " <-- configured" if acc.get("accountId") == settings.ig_account_id else ""
        )
        print(
            f"  {acc.get('accountId')}{marker} | balance={bal.get('balance')} "
            f"available={bal.get('available')} deposit/in-use={bal.get('deposit')} "
            f"pnl={bal.get('profitLoss')}"
        )

    print(f"\n=== IG LIVE POSITIONS ({len(positions)}) ===")
    ig_epics = []
    for entry in positions:
        pos = entry.get("position", {}) or {}
        mkt = entry.get("market", {}) or {}
        epic = mkt.get("epic")
        ig_epics.append(epic)
        print(
            f"  epic={epic} dealId={pos.get('dealId')} dir={pos.get('direction')} "
            f"size={pos.get('size')} open={pos.get('level') or pos.get('openLevel')} "
            f"createdUTC={pos.get('createdDateUTC')}"
        )
    if not positions:
        print("  (none) — IG reports NO open positions")

    today = date.today()
    async with session_factory() as session:
        db_open = list(
            await session.scalars(
                select(Position).where(Position.state == PositionState.OPEN)
            )
        )
    print(f"\n=== DB OPEN POSITIONS, any date ({len(db_open)}) ===")
    for p in db_open:
        shown = (
            " (shown on dashboard)" if p.date == today else " (HIDDEN: date != today)"
        )
        print(
            f"  id={p.id} epic={p.epic} date={p.date} dealId={p.deal_id} "
            f"open={p.level_open}{shown}"
        )
    if not db_open:
        print("  (none) — DB has NO position in OPEN state")

    ig_set = {e for e in ig_epics if e}
    db_set = {p.epic for p in db_open}
    print("\n=== MISMATCH ANALYSIS ===")
    untracked = ig_set - db_set
    stale = db_set - ig_set
    if untracked:
        print(
            f"  ! Open at IG but NOT OPEN in DB (eats margin, invisible): {untracked}"
        )
    if stale:
        print(f"  ! OPEN in DB but gone at IG (should be reconciled closed): {stale}")
    hidden = [p for p in db_open if p.epic in ig_set and p.date != today]
    if hidden:
        print(
            f"  ! OPEN at IG + in DB but dated earlier (hidden from dashboard): "
            f"{[p.epic for p in hidden]}"
        )
    if not (untracked or stale or hidden):
        print("  No mismatch between IG and DB open sets.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
