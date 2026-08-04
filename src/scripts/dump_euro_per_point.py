"""Capture each epic's € / point from IG into the backtest contract table.

The candle archive holds prices only, so an offline backtest cannot know that one
DAX point is worth several euros while one EUR/USD "point" is 0.0001. This tool
resolves that missing dimension **once** from ``/markets/{epic}`` and writes it to
the JSON table the backtester reads (``BACKTEST_CONTRACT_FILE``, default
``./config/euro_per_point.json``) — after which every backtest is offline again.

The value stored per epic is ``euro_per_point``: the euro P&L of one full point of
price movement for the position the bot would actually open, i.e.
``min_deal_size × contractSize × quote→EUR rate`` — exactly what
:func:`src.utils.tools.euro_per_point` resolves at open, so a backtest euro figure
is directly comparable with a live one. The deal size and currency inputs are
stored next to it so a suspect figure can be audited with
``python -m src.scripts.inspect_market <epic>``.

By default the epic list comes from the candle archive itself, so the table covers
exactly what can be backtested. Existing entries are kept unless ``--refresh`` is
passed; epics IG cannot price (unknown contract size) are reported and skipped
rather than written with a guessed value.

**Every call goes through the :class:`~src.core.api_queue.APIQueue`**, like every
other IG call in the project. Pricing a whole archive is over a hundred
``/markets`` reads, far beyond IG's per-minute allowance: hitting the client
directly gets ``exceeded-api-key-allowance`` / ``exceeded-account-allowance`` after
a couple of dozen epics and loses the rest. The queue's worker serialises the
calls, and on a quota block it **waits for the guard cooldown and re-queues the
call** instead of failing it — so a full run simply takes as long as IG's limits
require and comes back complete. The table is also written **incrementally**
(after every epic), so an interrupted run keeps what it already captured and a
re-run resumes where it stopped.

**This opens an IG session.** IG allows a limited number of concurrent sessions,
so prefer running it while the bot is stopped, or expect the bot's session to be
re-authenticated.

Usage:
    python -m src.scripts.dump_euro_per_point                  # every archived epic
    python -m src.scripts.dump_euro_per_point --refresh        # re-price known epics
    python -m src.scripts.dump_euro_per_point IX.D.DAX.IFMM.IP # explicit epics
    python -m src.scripts.dump_euro_per_point --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from src.backtest.backtest_archive import BacktestArchive
from src.backtest.contract_values import ContractTable
from src.core.api.client import IGClient
from src.core.api_guard import APIGuard
from src.core.api_queue import APIQueue, Priority
from src.core.config import get_settings
from src.utils.tools import _to_float, conversion_rate, euro_per_point

logging.basicConfig(
    level=logging.WARNING, format="%(levelname)s %(name)s — %(message)s"
)


def _archived_epics(settings) -> list[str]:
    """Every epic present in the candle archive, sorted."""
    archive = BacktestArchive(settings.candle_dump_dir)
    epics = {e.epic for dataset in archive.datasets() for e in dataset.epics}
    return sorted(epics)


def _resolve(epic: str, market_data: dict) -> dict | None:
    """Build the table entry for one ``/markets`` payload.

    Mirrors the open path (``TradingService.open_position``): the deal size is
    IG's ``minDealSize`` and the quote currency is the **first** entry of
    ``instrument.currencies``. Returns ``None`` when the contract size is unknown,
    since ``euro_per_point`` would then be a guess.
    """
    instrument = market_data.get("instrument", {})
    rules = market_data.get("dealingRules", {})
    currencies = instrument.get("currencies") or []
    code = currencies[0].get("code", "EUR") if currencies else "EUR"
    quantity = max(_to_float(rules.get("minDealSize", {}).get("value"), 1.0), 1.0)
    contract = _to_float(instrument.get("contractSize"), default=0.0) or _to_float(
        instrument.get("lotSize"), default=0.0
    )
    if contract <= 0:
        return None
    epp = euro_per_point(market_data, quantity, code)
    if epp <= 0:
        return None
    return {
        "euro_per_point": round(epp, 6),
        "quantity": quantity,
        "currency": code,
        "contract_size": contract,
        "conversion_rate": round(conversion_rate(instrument, code), 6),
        "name": instrument.get("name"),
    }


def _existing_entries(table: ContractTable) -> dict[str, dict]:
    """Everything already in the table, as plain JSON-ready dicts.

    Seeded from the **whole** file, not just the epics in scope, so pricing a
    single epic never drops the others.
    """
    return {
        epic: {
            "euro_per_point": value.euro_per_point,
            "quantity": value.quantity,
            "currency": value.currency,
            "contract_size": value.contract_size,
            "conversion_rate": value.conversion_rate,
            "name": value.name,
        }
        for epic, value in table.entries().items()
    }


def _write(path: Path, entries: dict[str, dict]) -> None:
    """Persist the table, sorted by epic. Called after every epic (see module doc)."""
    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "epics": dict(sorted(entries.items())),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "epics",
        nargs="*",
        help="Epics to price (default: every epic in the candle archive).",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-price epics already in the table (default: keep existing entries).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without touching the file.",
    )
    args = parser.parse_args()

    settings = get_settings()
    path = Path(settings.backtest_contract_file)
    table = ContractTable.load(path)
    epics = args.epics or _archived_epics(settings)
    if not epics:
        print("No epic to price (empty archive and no epic given).")
        return

    entries = _existing_entries(table)
    todo = [e for e in epics if args.refresh or e not in table]
    print(
        f"{len(epics)} epic(s) in scope, {len(epics) - len(todo)} already priced, "
        f"{len(todo)} to fetch -> {path}"
    )
    if args.dry_run:
        print("[dry-run] nothing will be written")
    if todo:
        print(
            "Calls go through the API queue: on an IG quota block the worker waits "
            "for the cooldown and resumes, so this can take a while."
        )

    failed: list[str] = []
    async with IGClient(settings) as client:
        # Same plumbing as the bot (src/main.py): the guard tracks IG's limits and
        # the queue's worker waits-then-resumes instead of burning the call.
        queue = APIQueue(
            client,
            APIGuard(),
            max_attempts=settings.queue_max_attempts,
            retry_margin_seconds=settings.queue_retry_margin_seconds,
        )
        await queue.start()
        try:
            for index, epic in enumerate(todo, 1):
                try:
                    market_data = await queue.get(
                        f"/markets/{epic}",
                        version=3,
                        priority=Priority.NORMAL,
                        label=f"euro-per-point {epic}",
                    )
                except Exception as exc:  # noqa: BLE001 — tool: report and carry on
                    print(f"  [{index}/{len(todo)}] {epic}: fetch failed ({exc})")
                    failed.append(epic)
                    continue
                entry = _resolve(epic, market_data)
                if entry is None:
                    print(
                        f"  [{index}/{len(todo)}] {epic}: unknown contract size "
                        "— skipped"
                    )
                    failed.append(epic)
                    continue
                entries[epic] = entry
                print(
                    f"  [{index}/{len(todo)}] {epic}: {entry['euro_per_point']} "
                    f"€/point (size={entry['quantity']} × "
                    f"contract={entry['contract_size']} × "
                    f"rate={entry['conversion_rate']} {entry['currency']})"
                )
                # Write as we go: a run interrupted by Ctrl-C or a hard IG block
                # keeps everything captured so far, and a re-run resumes from there.
                if not args.dry_run:
                    _write(path, entries)
        finally:
            await queue.stop()

    if args.dry_run:
        print(json.dumps({"epics": dict(sorted(entries.items()))}, indent=2))
        print(f"\n[dry-run] {len(entries)} epic(s) would be written to {path}")
        return

    _write(path, entries)
    print(f"\nWrote {len(entries)} epic(s) to {path}")
    if failed:
        print(f"Unpriced ({len(failed)}): {', '.join(failed)}")
        print("Re-run the same command to retry only those.")


if __name__ == "__main__":
    asyncio.run(main())
