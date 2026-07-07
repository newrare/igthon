"""Inspect the ``/markets/{epic}`` payload that drives per-point risk sizing.

The euros-of-risk on every open are ``stop_distance × euro_per_point`` where
``euro_per_point = deal_size × contractSize × conversion_rate`` (see
``src/utils/tools.py``). When the conversion rate is silently read as ``1.0``
for a non-EUR quote currency, the euro risk (``euro_stop``) is under-stated by
the currency factor — e.g. a GBP market "converted at 1.16" is sized as if
1.00, ~14 % light.

This tool prints, for one or more epics, the raw ``instrument.currencies``
block, the contract size, the scaling factor, and the ``conversion_rate`` /
``euro_per_point`` this code actually resolves — so a mismatch with the
broker's real "converted at <rate>" figure is visible at a glance.

Usage:
    python -m src.scripts.inspect_market CC.D.LCC.UNC.IP
    python -m src.scripts.inspect_market CC.D.LCC.UNC.IP CS.D.GBPUSD.CFD.IP
    python -m src.scripts.inspect_market --size 1 CC.D.LCC.UNC.IP
"""

import argparse
import asyncio
import json
import logging

from src.core.api.client import IGClient
from src.core.config import get_settings
from src.utils.tools import _to_float, conversion_rate, euro_per_point

logging.basicConfig(
    level=logging.WARNING, format="%(levelname)s %(name)s — %(message)s"
)


def _report(epic: str, market_data: dict, size: float) -> None:
    """Print the currency/contract fields and the resolved per-point value."""
    instrument = market_data.get("instrument", {})
    snapshot = market_data.get("snapshot", {})
    currencies = instrument.get("currencies") or []
    # This mirrors the exact selection made at open (trading.py): the first
    # currency's code is passed to conversion_rate / euro_per_point.
    quote_code = currencies[0].get("code", "EUR") if currencies else "EUR"
    contract = _to_float(instrument.get("contractSize"), default=0.0) or _to_float(
        instrument.get("lotSize"), default=0.0
    )
    rate = conversion_rate(instrument, quote_code)
    epp = euro_per_point(market_data, size, quote_code)

    print(f"\n=== {epic} — {instrument.get('name', '?')} ===")
    print(f"  marketStatus   : {snapshot.get('marketStatus')}")
    print(f"  scalingFactor  : {snapshot.get('scalingFactor')}")
    print(f"  contractSize   : {instrument.get('contractSize')} "
          f"(lotSize={instrument.get('lotSize')}) -> {contract}")
    print(f"  currencies     : {json.dumps(currencies)}")
    print(f"  selected code  : {quote_code}  (first entry — as open_position does)")
    print(f"  conversion_rate: {rate}")
    print(f"  euro_per_point : {epp}   (size={size} × contractSize={contract} × "
          f"rate={rate})")
    if quote_code != "EUR" and abs(rate - 1.0) < 1e-9:
        print("  ⚠️  quote currency is NOT EUR but conversion_rate resolved to 1.0 "
              "— euro risk is UNDER-STATED. This is the bug.")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("epics", nargs="+", help="One or more IG epics.")
    parser.add_argument(
        "--size",
        type=float,
        default=1.0,
        help="Deal size to price the per-point value for (default 1).",
    )
    args = parser.parse_args()

    settings = get_settings()
    async with IGClient(settings) as client:
        for epic in args.epics:
            try:
                market_data = await client.get(f"/markets/{epic}", version=3)
            except Exception as exc:  # noqa: BLE001 — diagnostic tool, report and continue
                print(f"\n=== {epic} === failed: {exc}")
                continue
            _report(epic, market_data, args.size)
    print()


if __name__ == "__main__":
    asyncio.run(main())
