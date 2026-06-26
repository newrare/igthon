"""Utility functions — ported from Tools.php.

General-purpose helpers for number formatting, data conversion, etc.
"""

import re
from datetime import datetime, time


def _parse_ig_utc_time(value: object) -> time | None:
    """Parse an IG UTC datetime string into a naive UTC :class:`~datetime.time`.

    IG's ``/history/transactions`` exposes execution timestamps in UTC
    (``openDateUtc`` / ``dateUtc``) as ISO 8601 strings, with or without
    milliseconds and using either ``T`` or a space as the date/time separator
    (e.g. ``"2026-06-23T09:23:30"`` or ``"2026-06-23 09:23:30.123"``). Returns
    the time-of-day component, or ``None`` when the value is missing or cannot
    be parsed (so callers leave the field untouched rather than crash).
    """
    if not value:
        return None
    text = str(value).strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


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


def margin_factor_pct(instrument: dict) -> float | None:
    """Extract the margin factor (as a percentage) from a ``/markets`` instrument.

    IG exposes it either as a flat ``marginFactor`` (with ``marginFactorUnit``) or,
    for tiered instruments, as the first entry of ``marginDepositBands``. Returns
    ``None`` when neither is present so the caller can flag the figure as unknown.
    """
    factor = instrument.get("marginFactor")
    if factor is not None:
        value = _to_float(factor)
        if value > 0:
            # A PERCENTAGE unit (or no unit) means the value already is a percent;
            # anything else is treated as a raw fraction and scaled up.
            unit = instrument.get("marginFactorUnit")
            return value if unit in (None, "PERCENTAGE") else value * 100
    bands = instrument.get("marginDepositBands") or []
    if bands:
        value = _to_float(bands[0].get("margin"))
        if value > 0:
            return value
    return None


def funds_needed_for_one_buy(market_data: dict) -> float | None:
    """Estimate the margin (EUR) to open one minimum-size BUY.

    Built from a single ``/markets`` payload, combining the instrument's margin
    factor, contract size and quote->EUR rate with the minimum deal size and the
    current offer (BUY) price:

        funds = euro_per_point(size) * offer_price * margin_factor_pct / 100

    Returns ``None`` when the margin factor, contract size or a usable price is missing,
    so callers can render the figure as unknown rather than a misleading ``0``.
    """
    instrument = market_data.get("instrument", {})
    snapshot = market_data.get("snapshot", {})
    dealing_rules = market_data.get("dealingRules", {})

    price = _to_float(snapshot.get("offer"), default=0.0)
    if price <= 0:
        return None

    currency = (instrument.get("currencies") or [{}])[0].get("code")
    min_deal = _to_float(
        dealing_rules.get("minDealSize", {}).get("value", 1), default=1.0
    )
    quantity = max(int(min_deal), 1)

    epp = euro_per_point(market_data, quantity, currency)
    margin_pct = margin_factor_pct(instrument)
    if not epp or margin_pct is None:
        return None
    return epp * price * margin_pct / 100


def stop_loss_eur_for_one_buy(market_data: dict) -> float | None:
    """Estimate the EUR loss if a minimum-size BUY is stopped out.

    Mirrors the manual-open path (``open_position_manual``): one minimum deal
    size opened with a stop at IG's ``minNormalStopOrLimitDistance``. The loss is
    that stop distance, expressed as a *price* distance, times the currency-aware
    value of one full point of price movement for the whole position:

        loss = euro_per_point(size) * stop_price_distance

    IG quotes the dealing-rule distance in *points* (1 point = 1 / scalingFactor
    in price terms), so it must be scaled back to a price distance before being
    multiplied by ``euro_per_point`` — exactly as ``open_position_manual`` does.
    A PERCENTAGE stop rule is already a fraction of the price. Returns ``None``
    when the contract size, price or stop rule is missing, so the caller can
    render the figure as unknown rather than a misleading ``0``.
    """
    instrument = market_data.get("instrument", {})
    snapshot = market_data.get("snapshot", {})
    dealing_rules = market_data.get("dealingRules", {})

    price = _to_float(snapshot.get("offer"), default=0.0)
    if price <= 0:
        return None

    stop_rule = dealing_rules.get("minNormalStopOrLimitDistance") or {}
    if stop_rule.get("value") is None:
        return None
    value = _to_float(stop_rule.get("value"), default=0.0)
    if stop_rule.get("unit") == "PERCENTAGE":
        # A PERCENTAGE rule is already a fraction of the price.
        stop_distance = value * price / 100
    else:
        # POINTS (the IG default): scale points back to a price distance.
        scaling_factor = _to_float(snapshot.get("scalingFactor"), default=1.0) or 1.0
        stop_distance = value / scaling_factor
    if stop_distance <= 0:
        return None

    currency = (instrument.get("currencies") or [{}])[0].get("code")
    min_deal = _to_float(
        dealing_rules.get("minDealSize", {}).get("value", 1), default=1.0
    )
    quantity = max(int(min_deal), 1)

    epp = euro_per_point(market_data, quantity, currency)
    if not epp:
        return None
    return epp * stop_distance
