"""Convert a legacy MariaDB ``historyDay`` SQL dump into candle archive CSVs.

The old PHP bot stored one snapshot row per epic per polling tick in the
``historyDay`` table (columns: ``epic, date, updateTime, bid, offer, high, low,
euro``). This script reconstructs the OHLC ``Candle`` archive format used by
:mod:`src.feed.candle_store` so old data can be replayed by the backtester.

Reconstruction rules (each snapshot row becomes one candle):
  - ``timestamp``  = ``<date>T<updateTime>`` in UTC (the dump runs at TZ +00:00).
  - ``*_close``    = the snapshot ``bid`` / ``offer``.
  - ``*_open``     = the previous snapshot's close for the same epic, chained
                     oldest-to-newest; the first candle of an epic opens flat.
  - ``high``/``low`` are the period range: applied to the bid directly and to
                     the offer shifted by the spread (``offer - bid``), then
                     clamped so the wick always covers open/close.
  - ``volume``     = 0 (the legacy schema recorded none).
Prices are kept exactly as stored in the dump (no unscaling — the per-epic
scaling factor is not present in ``historyDay``).

Rows are bucketed by ISO week exactly like ``candle_store``, so a dump that
straddles a week boundary produces one ``candles_<year>-W<week>.csv`` per week.

By default the same underlying market quoted under several contract types
(``CS.D.EURGBP.CFD.IP`` vs ``...MINI.IP``, ``CC.D.NG.UNC.IP`` vs ``...UME.IP``,
crypto ``CFD`` vs ``CFE`` …) is collapsed to a single epic, since both carry an
identical price series and the doubled epic count overflows the backtester. The
kept contract type follows what the live bot trades (CFD / UNC preferred), with
candle count then epic name as deterministic tie-breakers. Pass
``--keep-duplicates`` to skip this and emit every epic.

Usage:
    python -m src.scripts.convert_legacy_dump dumps/dumpSql2021-41.sql
    python -m src.scripts.convert_legacy_dump dumps/dumpSql2021-41.sql --out-dir dumps
    python -m src.scripts.convert_legacy_dump dumps/dumpSql2021-41.sql --keep-duplicates
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from src.feed.candle_store import _DUMP_FIELDS, iso_week_label

# Matches one VALUES tuple of the historyDay table:
# (id,'epic','date','updateTime',bid,offer,high,low,euro)
_ROW_RE = re.compile(
    r"\(\d+,"
    r"'([^']*)',"  # epic
    r"'([^']*)',"  # date (YYYY-MM-DD)
    r"'([^']*)',"  # updateTime (HH:MM:SS)
    r"(-?[\d.]+),"  # bid
    r"(-?[\d.]+),"  # offer
    r"(-?[\d.]+),"  # high
    r"(-?[\d.]+),"  # low
    r"(-?[\d.]+)\)"  # euro (€/point, unused for OHLC)
)


def parse_dump(text: str) -> list[tuple]:
    """Return parsed snapshot rows, sorted by (epic, timestamp).

    Each element is ``(epic, timestamp, bid, offer, high, low)``. Sorting by
    epic then time is what lets us chain each candle's open from the previous
    close within the same epic.
    """
    rows: list[tuple] = []
    for m in _ROW_RE.finditer(text):
        epic, date_s, time_s, bid, offer, high, low, _euro = m.groups()
        ts = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=UTC
        )
        rows.append((epic, ts, float(bid), float(offer), float(high), float(low)))
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


# Contract types the live bot actually trades, best first. An epic whose type
# token ranks higher (lower index) wins its underlying group; unranked types
# fall back to candle count, then epic name, for a deterministic choice.
_TYPE_PRIORITY = ["CFD", "UNC"]


def _underlying(epic: str) -> str:
    """Return an epic's underlying key by dropping its contract-type token.

    IG epics look like ``CS.D.EURGBP.CFD.IP``; the second-to-last token is the
    contract type, so ``CS.D.EURGBP.CFD.IP`` and ``CS.D.EURGBP.MINI.IP`` share
    the underlying key ``CS.D.EURGBP.IP``.
    """
    parts = epic.split(".")
    if len(parts) >= 5:
        return ".".join(parts[:-2] + parts[-1:])
    return epic


def dedupe_epics(rows: list[tuple]) -> tuple[list[tuple], dict[str, list[str]]]:
    """Keep one epic per underlying market; return (filtered_rows, dropped map).

    ``dropped`` maps each kept epic to the sibling epics removed from its group,
    purely so the caller can report what was collapsed.
    """
    counts: dict[str, int] = {}
    for epic, *_ in rows:
        counts[epic] = counts.get(epic, 0) + 1

    groups: dict[str, list[str]] = {}
    for epic in counts:
        groups.setdefault(_underlying(epic), []).append(epic)

    def rank(epic: str) -> tuple:
        contract_type = epic.split(".")[-2] if len(epic.split(".")) >= 5 else ""
        priority = (
            _TYPE_PRIORITY.index(contract_type)
            if contract_type in _TYPE_PRIORITY
            else len(_TYPE_PRIORITY)
        )
        # priority asc, then most candles, then name asc — all deterministic.
        return (priority, -counts[epic], epic)

    kept: set[str] = set()
    dropped: dict[str, list[str]] = {}
    for siblings in groups.values():
        winner = min(siblings, key=rank)
        kept.add(winner)
        losers = sorted(e for e in siblings if e != winner)
        if losers:
            dropped[winner] = losers

    return [r for r in rows if r[0] in kept], dropped


def build_candles(rows: list[tuple]) -> list[list]:
    """Turn snapshot rows into candle CSV rows matching ``_DUMP_FIELDS``."""
    out: list[list] = []
    prev_epic: str | None = None
    prev_bid = prev_offer = 0.0

    for epic, ts, bid, offer, high, low in rows:
        if epic != prev_epic:
            # First candle of this epic opens flat (no prior close to chain to).
            bid_open, offer_open = bid, offer
        else:
            bid_open, offer_open = prev_bid, prev_offer

        spread = offer - bid
        # high/low are the period range recorded against the (bid-side) price.
        bid_high = max(high, bid_open, bid)
        bid_low = min(low, bid_open, bid)
        offer_high = max(high + spread, offer_open, offer)
        offer_low = min(low + spread, offer_open, offer)

        out.append(
            [
                epic,
                ts.isoformat(),
                bid_open,
                bid,
                bid_high,
                bid_low,
                offer_open,
                offer,
                offer_high,
                offer_low,
                0,
            ]
        )
        prev_epic, prev_bid, prev_offer = epic, bid, offer
    return out


def write_archives(candles: list[list], out_dir: Path) -> list[Path]:
    """Write candle rows to per-ISO-week ``candles_<year>-W<week>.csv`` files."""
    out_dir.mkdir(parents=True, exist_ok=True)

    buckets: dict[str, list[list]] = {}
    for row in candles:
        ts = datetime.fromisoformat(row[1])
        buckets.setdefault(iso_week_label(ts), []).append(row)

    paths: list[Path] = []
    for week, week_rows in sorted(buckets.items()):
        path = out_dir / f"candles_{week}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(_DUMP_FIELDS)
            writer.writerows(week_rows)
        paths.append(path)
        print(f"  {path}  ({len(week_rows)} candles)")
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dump", type=Path, help="Path to the historyDay SQL dump")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory (default: the dump's own directory)",
    )
    parser.add_argument(
        "--keep-duplicates",
        action="store_true",
        help="Keep every contract type instead of one epic per underlying market",
    )
    args = parser.parse_args(argv)

    if not args.dump.is_file():
        print(f"error: dump not found: {args.dump}", file=sys.stderr)
        return 1

    out_dir = args.out_dir or args.dump.parent
    text = args.dump.read_text(encoding="utf-8", errors="replace")

    rows = parse_dump(text)
    print(f"Parsed {len(rows)} snapshot rows from {args.dump}")

    if not args.keep_duplicates:
        before = len({r[0] for r in rows})
        rows, dropped = dedupe_epics(rows)
        after = len({r[0] for r in rows})
        removed = sum(len(v) for v in dropped.values())
        print(
            f"Deduplicated epics: {before} -> {after} "
            f"({removed} duplicate contract types dropped)"
        )

    candles = build_candles(rows)
    print(f"Built {len(candles)} candles; writing archives to {out_dir}/")
    write_archives(candles, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
