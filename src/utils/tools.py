"""Utility functions — ported from Tools.php.

General-purpose helpers for number formatting, data conversion, etc.
"""

import re


def _to_float(value: object, default: float = 0.0) -> float:
    """Best-effort float conversion tolerant of IG's string numbers.

    IG returns numeric fields as strings that may carry thousands separators
    (e.g. ``"100,000"``). Returns ``default`` when the value is missing or
    cannot be parsed.
    """
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return default


def parse_ig_pnl(raw: object) -> float | None:
    """Parse an IG ``profitAndLoss`` value (e.g. ``"E-2.73"``) into a float.

    IG prefixes the realized P&L with a currency symbol or letter
    (``E`` = EUR, ``$``, ``£``, ``¥``) and may use ``,`` as a thousands
    separator. Returns the signed amount in the account currency, or ``None``
    when the value cannot be parsed.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    cleaned = re.sub(r"[^0-9+\-.]", "", str(raw).replace(",", ""))
    if cleaned in ("", "+", "-", ".", "+.", "-."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def conversion_rate(instrument: dict, currency_code: str | None = None) -> float:
    """Return the rate converting the instrument's quote currency to EUR.

    IG ships the conversion rate inside ``instrument.currencies[].exchangeRate``
    (the "converted at …" figure shown on the broker's statement). Picks the
    entry matching ``currency_code``, else the default currency, else the first
    one. Falls back to ``1.0`` (no conversion) when nothing is available.
    """
    currencies = instrument.get("currencies") or []
    entry = None
    if currency_code:
        entry = next((c for c in currencies if c.get("code") == currency_code), None)
    if entry is None:
        entry = next((c for c in currencies if c.get("isDefault")), None)
    if entry is None and currencies:
        entry = currencies[0]
    if not entry:
        return 1.0
    rate = entry.get("exchangeRate", entry.get("baseExchangeRate"))
    return _to_float(rate, default=1.0) or 1.0


def euro_per_point(
    market_data: dict, size: float, currency_code: str | None = None
) -> float:
    """Euros of P&L per 1.0 of price movement for the whole position.

    ``P&L = (close - open) * euro_per_point``. Built from a single ``/markets``
    payload: the instrument's contract size (value of one full point in the
    quote currency) times the quote->EUR exchange rate times the deal size. This
    is currency-aware (e.g. JPY pairs) and instrument-aware, unlike the legacy
    ``1 / scalingFactor`` heuristic. Returns ``0.0`` when the contract size is
    unknown so callers can fall back to the legacy estimate.
    """
    instrument = market_data.get("instrument", {})
    contract = _to_float(instrument.get("contractSize"), default=0.0)
    if contract <= 0:
        contract = _to_float(instrument.get("lotSize"), default=0.0)
    if contract <= 0:
        return 0.0
    rate = conversion_rate(instrument, currency_code)
    return float(size) * contract * rate
