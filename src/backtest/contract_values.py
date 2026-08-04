"""Per-epic contract values — the € per point a backtest needs to price a trade.

The candle archive holds **prices only**: no contract size, no quote currency, no
deal size. A euro P&L therefore cannot be derived from it, and a single global
"euro per point" is not a fix — one DAX point is worth several euros while one
EUR/USD "point" is 0.0001, so a shared factor silently flattens every forex trade
to ``0.00 €`` (which is exactly why the page used to report percentages only).

This module supplies the missing dimension as a **file** the backtester reads
next to the archive: ``epic → € per point``, captured once from IG's
``/markets/{epic}`` payload by ``python -m src.scripts.dump_euro_per_point`` and
then version-controlled. That keeps the backtest strictly offline (no DB, no IG
API) while making its euro figures instrument-aware.

``euro_per_point`` is the euro value of **one full point of price movement for
the whole position the bot would open** — deal size × contract size × quote→EUR
rate, i.e. exactly what :func:`src.utils.tools.euro_per_point` resolves at open —
so a trade's P&L is simply ``(level_close - level_open) × euro_per_point``.

An epic missing from the table is **not** priced at a guessed value: its trades
are excluded from the euro totals and reported as such, so a partial table can
never masquerade as a complete euro result.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ContractValue:
    """One epic's resolved euro-per-point, with the inputs it was built from.

    The extra fields are informational — they let a suspect figure be audited
    against ``python -m src.scripts.inspect_market <epic>`` without re-reading
    IG. Only :attr:`euro_per_point` is used to price a trade.
    """

    epic: str
    euro_per_point: float
    quantity: float = 1.0  # deal size the value is priced for (IG min deal size)
    currency: str | None = None  # quote currency of the instrument
    contract_size: float | None = None
    conversion_rate: float | None = None
    name: str | None = None  # IG instrument name, to read the file by eye


class ContractTable:
    """``epic → € per point``, read from a JSON file (no DB, no IG API).

    File shape::

        {
          "generated_at": "2026-08-03T07:12:00+00:00",
          "epics": {
            "IX.D.DAX.IFMM.IP": {
              "euro_per_point": 1.0, "quantity": 1, "currency": "EUR",
              "contract_size": 1.0, "conversion_rate": 1.0
            }
          }
        }

    A missing file, unreadable JSON or a malformed entry never raises: the table
    is simply empty (or that entry absent) and the caller reports the epics it
    could not price. A backtest must still run — and still report its counts and
    percentage figures — on a machine where the table has not been generated yet.
    """

    def __init__(
        self,
        values: dict[str, ContractValue] | None = None,
        *,
        generated_at: str | None = None,
        path: Path | None = None,
    ) -> None:
        self._values = dict(values or {})
        self.generated_at = generated_at
        self.path = path

    def __len__(self) -> int:
        return len(self._values)

    def __contains__(self, epic: object) -> bool:
        return epic in self._values

    @classmethod
    def load(cls, path: str | Path | None) -> ContractTable:
        """Read the table at ``path``; an absent or invalid file yields an empty one."""
        if not path:
            return cls()
        file = Path(path)
        if not file.is_file():
            logger.info("No euro-per-point table at %s — euro P&L unavailable", file)
            return cls(path=file)
        try:
            raw = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Unreadable euro-per-point table %s: %s", file, exc)
            return cls(path=file)
        return cls(
            values=_parse_entries(raw.get("epics") or {}),
            generated_at=raw.get("generated_at"),
            path=file,
        )

    def value(self, epic: str) -> ContractValue | None:
        """The full entry for ``epic``, or ``None`` when it is not in the table."""
        return self._values.get(epic)

    def entries(self) -> dict[str, ContractValue]:
        """A copy of every entry, keyed by epic — used to rewrite the file."""
        return dict(self._values)

    def euro_per_point(self, epic: str) -> float | None:
        """Euro value of one point for ``epic``, or ``None`` when unknown."""
        entry = self._values.get(epic)
        return entry.euro_per_point if entry else None

    def missing(self, epics) -> list[str]:
        """The subset of ``epics`` this table cannot price, sorted."""
        return sorted({e for e in epics if e not in self._values})


def _parse_entries(raw: dict) -> dict[str, ContractValue]:
    """Build ContractValue objects, skipping entries without a usable value."""
    values: dict[str, ContractValue] = {}
    for epic, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        try:
            epp = float(entry.get("euro_per_point"))
        except (TypeError, ValueError):
            continue
        if epp <= 0:
            continue
        values[str(epic)] = ContractValue(
            epic=str(epic),
            euro_per_point=epp,
            quantity=_opt_float(entry.get("quantity")) or 1.0,
            currency=entry.get("currency"),
            contract_size=_opt_float(entry.get("contract_size")),
            conversion_rate=_opt_float(entry.get("conversion_rate")),
            name=entry.get("name"),
        )
    return values


def _opt_float(value) -> float | None:
    """Best-effort float, ``None`` when absent or not numeric."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
