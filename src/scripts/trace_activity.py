"""Trace IG account activity to explain unexpected opens (e.g. ``adopted``).

A position recorded with ``reason_open="adopted"`` was *not* decided by the bot:
the reconciliation job found it already live at IG and created a DB row to
manage it. This script answers the real question — **who opened it at IG?** — by
querying ``GET /history/activity`` (v3, detailed), whose per-deal ``channel``
field records the origin server-side:

    PUBLIC_WEB_API  REST API (this bot, or any other API key)
    PUBLIC_FIX_API  FIX API
    WEB             IG web platform (manual)
    MOBILE          IG mobile app (manual)
    SYSTEM          automatic IG action (stop/limit hit, rollover, expiry)
    DEALER          IG dealer intervention

It also cross-references the ``dealId`` of every ``adopted`` position in the DB
against the activity feed, so each mystery open is tagged with its true origin.

Usage:
    python -m src.scripts.trace_activity                 # last 24h
    python -m src.scripts.trace_activity --days 3        # last 3 days
    python -m src.scripts.trace_activity --from 2026-06-30T00:00:00 \
                                          --to 2026-06-30T23:59:59
    python -m src.scripts.trace_activity --epic CS.D.SGDJPY.CFD.IP
    python -m src.scripts.trace_activity --no-db         # skip DB cross-ref
"""

import argparse
import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from src.core.api.client import IGClient
from src.core.config import get_settings
from src.models.database import create_session_factory
from src.models.position import Position

logging.basicConfig(
    level=logging.WARNING, format="%(levelname)s %(name)s — %(message)s"
)

# IG v3 activity datetime format (no timezone — account-local).
_IG_DT = "%Y-%m-%dT%H:%M:%S"

CHANNEL_LEGEND = {
    "PUBLIC_WEB_API": "REST API (this bot / another API key)",
    "PUBLIC_FIX_API": "FIX API",
    "WEB": "IG web platform (manual)",
    "MOBILE": "IG mobile app (manual)",
    "SYSTEM": "automatic IG action (stop/limit/rollover/expiry)",
    "DEALER": "IG dealer intervention",
}


def _parse_range(args: argparse.Namespace) -> tuple[str, str]:
    """Resolve the from/to window as IG-formatted strings."""
    if args.from_date and args.to_date:
        return args.from_date, args.to_date
    now = datetime.now()
    start = now - timedelta(days=args.days)
    return start.strftime(_IG_DT), now.strftime(_IG_DT)


async def _fetch_activities(
    client: IGClient, from_date: str, to_date: str
) -> list[dict]:
    """Fetch detailed activity records for the window (v3, detailed=true)."""
    endpoint = f"/history/activity?from={from_date}&to={to_date}&detailed=true"
    data = await client.get(endpoint, version=3)
    return data.get("activities", [])


async def _adopted_deal_ids(session_factory) -> dict[str, Position]:
    """Map dealId -> Position for every ``adopted`` row in the DB."""
    async with session_factory() as session:
        result = await session.execute(
            select(Position).where(Position.reason_open == "adopted")
        )
        rows = list(result.scalars().all())
    return {p.deal_id: p for p in rows if p.deal_id}


def _opened_deal_ids(activity: dict) -> set[str]:
    """Collect the dealIds this activity actually OPENED (POSITION_OPENED action).

    The origin of a position is the activity that opened it — never the later one
    that closed it. IG records a stop-loss execution as a separate SYSTEM-channel
    POSITION_CLOSED activity on the same dealId, so matching on "any activity that
    touches this dealId" wrongly reports the close (SYSTEM) as the origin.
    """
    ids: set[str] = set()
    details = activity.get("details") or {}
    for action in details.get("actions") or []:
        if action.get("actionType") == "POSITION_OPENED" and action.get(
            "affectedDealId"
        ):
            ids.add(action["affectedDealId"])
    return ids


def _print_activities(activities: list[dict], epic_filter: str | None) -> None:
    """Print one line per activity, newest first."""
    print(
        f"\n{'TIME':<20} {'CHANNEL':<16} {'TYPE':<22} {'STATUS':<9} "
        f"{'DIR':<4} {'EPIC':<22} DEALID"
    )
    print("-" * 120)
    for act in activities:
        epic = act.get("epic") or ""
        if epic_filter and epic_filter not in epic:
            continue
        details = act.get("details") or {}
        direction = details.get("direction") or ""
        print(
            f"{(act.get('date') or ''):<20} "
            f"{(act.get('channel') or '?'):<16} "
            f"{(act.get('type') or ''):<22} "
            f"{(act.get('status') or ''):<9} "
            f"{direction:<4} "
            f"{epic:<22} "
            f"{act.get('dealId') or ''}"
        )
        desc = act.get("description")
        if desc:
            print(f"{'':<20} ↳ {desc}")


def _print_channel_summary(activities: list[dict]) -> None:
    """Count activities per channel with a plain-language legend."""
    counts: dict[str, int] = {}
    for act in activities:
        ch = act.get("channel") or "?"
        counts[ch] = counts.get(ch, 0) + 1
    print("\n=== Activity by channel ===")
    for ch, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>4}  {ch:<16} {CHANNEL_LEGEND.get(ch, '')}")


def _print_adopted_origin(activities: list[dict], adopted: dict[str, Position]) -> None:
    """For each adopted DB position, report the channel that opened it at IG."""
    print("\n=== Origin of DB 'adopted' positions ===")
    if not adopted:
        print("  (no positions with reason_open='adopted' in the DB)")
        return
    # Index only the OPENING activity per dealId — that is the true origin.
    open_by_deal: dict[str, dict] = {}
    for act in activities:
        for did in _opened_deal_ids(act):
            open_by_deal.setdefault(did, act)
    for deal_id, pos in adopted.items():
        act = open_by_deal.get(deal_id)
        if act is None:
            print(
                f"  {deal_id}  {pos.epic:<22} → no OPEN activity in window "
                f"(widen --days; older than IG's history retention)"
            )
            continue
        ch = act.get("channel") or "?"
        print(
            f"  {deal_id}  {pos.epic:<22} → opened via channel={ch} "
            f"({CHANNEL_LEGEND.get(ch, 'unknown')}) at {act.get('date')}"
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=int, default=1, help="Look back this many days (default 1)."
    )
    parser.add_argument("--from", dest="from_date", help="Start (YYYY-MM-DDTHH:MM:SS).")
    parser.add_argument("--to", dest="to_date", help="End (YYYY-MM-DDTHH:MM:SS).")
    parser.add_argument("--epic", help="Only show activities whose epic contains this.")
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="Skip the DB cross-reference of adopted positions.",
    )
    args = parser.parse_args()

    settings = get_settings()
    from_date, to_date = _parse_range(args)
    print(f"Fetching IG activity from {from_date} to {to_date} …")

    async with IGClient(settings) as client:
        activities = await _fetch_activities(client, from_date, to_date)

    # Newest first for readability.
    activities.sort(key=lambda a: a.get("date") or "", reverse=True)

    _print_activities(activities, args.epic)
    _print_channel_summary(activities)

    if not args.no_db:
        session_factory = create_session_factory(settings)
        adopted = await _adopted_deal_ids(session_factory)
        _print_adopted_origin(activities, adopted)

    print(f"\nTotal activities in window: {len(activities)}\n")


if __name__ == "__main__":
    asyncio.run(main())
