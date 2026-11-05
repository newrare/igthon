"""Instrument exposure — what an epic is a bet on, read from the epic itself.

Correlating two curves over one morning is a noisy test of whether two markets
are the same bet: EUR/JPY and EUR/GBP, both bought, both carry the euro, yet two
hours of one-minute returns can correlate below any sensible threshold because
the yen and the pound add their own noise. Some relations are structural and
known in advance, so they are read from the instrument instead:

- a **currency pair** is a bet on both of its currencies — two pairs sharing a
  currency share an exposure, whatever their curves did this morning;
- an **equity index** is a bet on its region's equities — the DAX, the CAC and
  the EuroStoxx are one bet on Europe;
- a **commodity** is a bet on its underlying — spot silver and a silver future
  are one bet.

:func:`exposure_of` returns the instrument's **asset class** (one of
:data:`ASSET_CLASSES`, or the epic's own two-letter prefix for an unknown kind)
and its **exposure tags**. Two instruments sharing a tag are the same bet; two
of the same class are merely alike. Purely a reading of the epic string: an epic
this module does not recognise gets its own unique tag, so it is never wrongly
grouped with another one.
"""

from __future__ import annotations

from dataclasses import dataclass

#: ISO codes of the currencies IG quotes in pairs (``CS.D.EURUSD.CFD.IP``).
CURRENCIES: frozenset[str] = frozenset(
    {
        "AUD", "CAD", "CHF", "CNH", "CZK", "DKK", "EUR", "GBP", "HKD", "HUF",
        "ILS", "INR", "JPY", "MXN", "NOK", "NZD", "PLN", "SEK", "SGD", "TRY",
        "USD", "ZAR",
    }
)  # fmt: skip

#: Equity-index codes (third epic field) → region.
INDEX_REGIONS: dict[str, str] = {
    "DAX": "europe",
    "CAC": "europe",
    "STXE": "europe",
    "StoxxBank": "europe",
    "FTSE": "europe",
    "IBEX": "europe",
    "AEX": "europe",
    "SMI": "europe",
    "MIB": "europe",
    "DOW": "us",
    "SPTRD": "us",
    "NASDAQ": "us",
    "RUSSELL": "us",
    "NIKFUT": "asia",
    "NIKKEI": "asia",
    "TOPIX": "asia",
    "HANGSENG": "asia",
    "HSTECH": "asia",
    "ASX": "asia",
    "CHINAA": "asia",
    "EMGMKT": "emerging",
}

#: Commodity codes → (asset class, underlying).
COMMODITIES: dict[str, tuple[str, str]] = {
    "CL": ("energy", "crude"),
    "LCO": ("energy", "crude"),
    "NG": ("energy", "natgas"),
    "ICENG": ("energy", "natgas"),
    "HO": ("energy", "heating_oil"),
    "RB": ("energy", "gasoline"),
    "BO": ("agri", "soybean_oil"),
    "S": ("agri", "soybeans"),
    "SM": ("agri", "soybean_meal"),
    "W": ("agri", "wheat"),
    "C": ("agri", "corn"),
    "SB": ("agri", "sugar"),
    "KC": ("agri", "coffee"),
    "CC": ("agri", "cocoa"),
    "CT": ("agri", "cotton"),
    "SI": ("metal", "silver"),
    "CFDSILVER": ("metal", "silver"),
    "GC": ("metal", "gold"),
    "CFDGOLD": ("metal", "gold"),
    "CFEGOLD": ("metal", "gold"),
    "HG": ("metal", "copper"),
    "PL": ("metal", "platinum"),
    "PA": ("metal", "palladium"),
}

ASSET_CLASSES: tuple[str, ...] = ("forex", "index", "energy", "agri", "metal")


@dataclass(frozen=True, slots=True)
class Exposure:
    """What one epic is a bet on.

    Attributes:
        asset_class: ``forex`` / ``index`` / ``energy`` / ``agri`` / ``metal``,
            or the epic's two-field prefix (``"XX.D"``) when unknown.
        tags: Exposures that make two instruments the same bet when shared —
            ``ccy:EUR``, ``region:europe``, ``underlying:silver``…
    """

    asset_class: str
    tags: frozenset[str]

    def shares_exposure(self, other: Exposure) -> bool:
        """True when the two instruments carry a common exposure."""
        return bool(self.tags & other.tags)


def exposure_of(epic: str) -> Exposure:
    """The asset class and exposure tags of ``epic`` (see the module docstring)."""
    parts = epic.split(".")
    code = parts[2] if len(parts) >= 3 else epic
    prefix = ".".join(parts[:2])

    if len(code) == 6 and code[:3] in CURRENCIES and code[3:] in CURRENCIES:
        return Exposure("forex", frozenset({f"ccy:{code[:3]}", f"ccy:{code[3:]}"}))
    if code in COMMODITIES:
        asset_class, underlying = COMMODITIES[code]
        return Exposure(asset_class, frozenset({f"underlying:{underlying}"}))
    if prefix == "IX.D":
        region = INDEX_REGIONS.get(code)
        tag = f"region:{region}" if region else f"index:{code}"
        return Exposure("index", frozenset({tag}))
    return Exposure(prefix, frozenset({f"instrument:{code}"}))
