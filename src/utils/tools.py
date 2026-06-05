"""Utility functions — ported from Tools.php.

General-purpose helpers for number formatting, data conversion, etc.
"""


def num(value: float, decimals: int = 3) -> float:
    """Round a number to N decimal places.

    Equivalent to Tools::num() in PHP.
    """
    return round(value, decimals)


def scaling(level: float, stop_level: float) -> str:
    """Format stop level for the IG API.

    Some IG instruments require stop levels in specific formats.
    """
    return str(round(stop_level, 1))


def spread_ratio(bid: float, offer: float) -> float:
    """Calculate the spread ratio (spread / bid).

    Useful for filtering high-spread epics.
    """
    if bid <= 0:
        return 1.0
    return (offer - bid) / bid


def pip_value(scaling_factor: float) -> float:
    """Calculate the euro value of 1 pip.

    Args:
        scaling_factor: Market scaling factor from IG.

    Returns:
        Euro value per pip movement.
    """
    if scaling_factor <= 0:
        return 1.0
    return 1.0 / scaling_factor


def format_pnl(euro: float) -> str:
    """Format P&L for display with color indicator."""
    sign = "+" if euro >= 0 else ""
    return f"{sign}{euro:.2f}€"


def is_market_open(hour: int, *, start: int = 9, end: int = 17) -> bool:
    """Check if the current hour is within market hours."""
    return start <= hour < end
