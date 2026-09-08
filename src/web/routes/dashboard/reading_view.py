"""Market-reading panel — presentation of :mod:`src.core.reading` for the UI.

Turns a :class:`~src.core.reading.CurveReading` (and, when the epic holds an open
trade, a :class:`~src.core.reading.PositionReading`) into the flat
``{sections: [{title, note, rows: [{label, value, detail, tone}]}]}`` structure the
dashboard's reading modal renders. All the interpretation lives here — labels,
number formatting, and which values deserve a colour — so the browser side stays a
dumb row renderer and the meaning of each measure is written once, in Python.

No measure is computed in this module: it only reads the reading layer. Adding a
field to the panel is adding a row here, never maths.
"""

from __future__ import annotations

import math

from src.core.reading import CurveReading, PositionReading, Regime, Trend

#: Tones the client maps to colours: neutral, good, bad, warn, muted.
_GOOD, _BAD, _WARN, _MUTED, _NEUTRAL = "good", "bad", "warn", "muted", ""

_REGIME_TONE = {
    Regime.TREND_UP: _GOOD,
    Regime.TREND_DOWN: _BAD,
    Regime.RANGE: _WARN,
    Regime.VOLATILE: _BAD,
    Regime.UNKNOWN: _MUTED,
}

_REGIME_NOTE = {
    Regime.TREND_UP: "directional path, pointing up",
    Regime.TREND_DOWN: "directional path, pointing down",
    Regime.RANGE: "choppy and contained — mean-reverting",
    Regime.VOLATILE: "choppy and agitated — swings going nowhere",
    Regime.UNKNOWN: "not enough history to judge",
}

_TREND_TONE = {Trend.UP: _GOOD, Trend.DOWN: _BAD, Trend.FLAT: _MUTED}

# How a round level's 0-1 roundness score reads in words (see
# ``src.core.levels.psychological_levels``): the rounder the figure, the more
# orders sit on it.
_ROUNDNESS = {
    1.0: "major round figure",
    0.8: "big round figure",
    0.6: "strong round number",
    0.4: "round number (half step)",
    0.2: "round number",
}


def _decimals(price: float) -> int:
    """Decimals to print a price of this magnitude with (DAX 18 042 vs EUR/USD 1.08)."""
    if price >= 1000:
        return 1
    if price >= 100:
        return 2
    if price >= 10:
        return 3
    return 5


def _price(value: float | None, scale: float) -> str:
    """Format a price/distance at the precision the epic's own scale deserves."""
    if value is None:
        return "—"
    if value == math.inf:
        return "∞"
    return f"{value:.{_decimals(scale)}f}"


def _signed(value: float, unit: str = "", decimals: int = 2) -> str:
    """Format a signed figure, so a positive number always shows its ``+``."""
    return f"{value:+.{decimals}f}{unit}"


def _tone_of(value: float, *, good_above: float = 0.0) -> str:
    """Green above the threshold, red below, muted on it."""
    if value > good_above:
        return _GOOD
    if value < good_above:
        return _BAD
    return _MUTED


def _row(label: str, value: str, detail: str = "", tone: str = _NEUTRAL) -> dict:
    """One label/value line of the panel."""
    return {"label": label, "value": value, "detail": detail, "tone": tone}


def _trend_row(reading: CurveReading, minutes: int) -> dict:
    """One horizon of the trend section: size, steepness, linearity, direction."""
    trend = reading.trend(minutes)
    if trend.minutes < 2:
        return _row(f"{minutes} min", "—", "no history", _MUTED)
    detail = (
        f"{trend.degrees:+.0f}° · R² {trend.r_squared:.2f} · "
        f"{trend.direction.value.lower()}"
        + ("" if trend.minutes == minutes else f" · only {trend.minutes} min held")
    )
    return _row(
        f"{minutes} min", _signed(trend.pct, " %"), detail, _TREND_TONE[trend.direction]
    )


def _market_sections(reading: CurveReading, spread: dict | None) -> list[dict]:
    """The market half of the panel: regime, trend, pressure, noise, momentum."""
    price = reading.price
    atr = reading.atr

    regime_row = _row(
        "Regime",
        reading.regime.value.replace("_", " ").title(),
        _REGIME_NOTE[reading.regime],
        _REGIME_TONE[reading.regime],
    )
    alignment = reading.alignment
    align_detail = (
        "every horizon agrees"
        if reading.is_aligned
        else (
            "horizons contradict each other"
            if abs(alignment) < 0.4
            else "mostly agreed"
        )
    )
    ticks = reading.ticks_15m
    cross = reading.cross(9, 21)
    if cross.fast == 0.0:
        cross_row = _row("EMA 9/21", "—", "needs 22 candles", _MUTED)
    else:
        age = (
            f"{cross.bars_since_cross} min ago"
            if cross.crossed_in_history
            else f"no turn in {cross.bars_since_cross} min"
        )
        cross_row = _row(
            "EMA 9/21",
            cross.state.value.lower(),
            f"{age} · gap {reading.in_atr(abs(cross.gap)):.2f} ATR"
            + (" · fresh turn" if cross.just_crossed else ""),
            _TREND_TONE[cross.state],
        )

    amplitude_5 = reading.amplitude(5)
    amplitude_15 = reading.amplitude(15)
    volatility = reading.volatility_ratio
    rsi = reading.rsi()
    position_in_range = reading.position_in_range

    spread_points = reading.spread
    spread_euro = (spread or {}).get("euro")
    spread_detail = (
        f"{reading.spread_in_atr:.2f} ATR · "
        f"×{reading.spread_ratio:.2f} of its 30 min mean"
    )
    if spread_euro:
        spread_detail = f"{spread_euro:.2f} € round trip · " + spread_detail

    return [
        {
            "title": "State",
            "note": "What kind of market this is right now.",
            "rows": [
                regime_row,
                _row(
                    "Horizon alignment",
                    _signed(alignment, "", 2),
                    align_detail,
                    _tone_of(alignment) if abs(alignment) > 0.4 else _MUTED,
                ),
                _row(
                    "Acceleration",
                    _signed(reading.acceleration, " %/min", 4),
                    "5 min speed vs 30 min"
                    + (" · trend is fading" if reading.is_fading else ""),
                    _WARN if reading.is_fading else _MUTED,
                ),
                _row(
                    "History held",
                    f"{reading.minutes} min",
                    f"last candle {max(0.0, reading.age_minutes):.0f} min ago",
                    _WARN if reading.age_minutes > 3 else _MUTED,
                ),
            ],
        },
        {
            "title": "Trend per horizon",
            "note": "Implied move across the window, its angle and its linearity.",
            "rows": [_trend_row(reading, minutes) for minutes in (5, 15, 30, 60)],
        },
        {
            "title": "Candle pressure (15 min)",
            "note": "Who won the count, ignoring how big the moves were.",
            "rows": [
                _row(
                    "Up / down / flat",
                    f"{ticks.up} ↑ / {ticks.down} ↓ / {ticks.flat} =",
                    f"{ticks.flat_ratio:.0%} undecided",
                ),
                _row(
                    "Bias",
                    _signed(ticks.bias),
                    f"dominant {ticks.dominant.value.lower()}",
                    _TREND_TONE[ticks.dominant],
                ),
                _row(
                    "Streak",
                    f"{reading.streak:+d} candles",
                    "current one-sided run",
                    _tone_of(reading.streak),
                ),
            ],
        },
        {
            "title": "Amplitude & noise",
            "note": "Four different sizes of movement — a stop needs more than one.",
            "rows": [
                _row(
                    "ATR (14)",
                    _price(atr, price),
                    f"{reading.atr_pct:.3f} % of price",
                ),
                _row(
                    "Agitation",
                    f"×{volatility:.2f}" if volatility else "—",
                    "recent candles vs this epic's own baseline",
                    _WARN if volatility >= 1.5 else _MUTED,
                ),
                _row(
                    "Band noise 5 / 15 min",
                    f"{_price(amplitude_5.band_noise, price)} / "
                    f"{_price(amplitude_15.band_noise, price)}",
                    "thickness of the band price wanders in (detrended)",
                ),
                _row(
                    "Adverse step / retracement (15 min)",
                    f"{_price(amplitude_15.adverse_noise, price)} / "
                    f"{_price(amplitude_15.retracement, price)}",
                    "what one bad candle vs a sequence of them gives back",
                ),
                _row(
                    "Efficiency (30 min)",
                    f"{reading.efficiency(30):.2f}",
                    "1 = a straight line, 0 = pure chop",
                    _GOOD if reading.efficiency(30) >= 0.35 else _WARN,
                ),
            ],
        },
        {
            "title": "Momentum",
            "note": "Where the price sits and whether a turn is being signalled.",
            "rows": [
                _row(
                    "RSI (14)",
                    f"{rsi:.0f}",
                    (
                        "stretched up"
                        if rsi >= 70
                        else "stretched down" if rsi <= 30 else "balanced"
                    ),
                    _WARN if rsi >= 70 or rsi <= 30 else _MUTED,
                ),
                _row(
                    "Position in the hour's range",
                    f"{position_in_range:.0%}",
                    "0 % = the low, 100 % = the high",
                ),
                cross_row,
                _row(
                    "Spread",
                    _price(spread_points, price),
                    spread_detail,
                    _WARN if reading.spread_ratio > 1.5 else _MUTED,
                ),
            ],
        },
        _levels_section(reading),
    ]


def _level_row(label: str, level, reading: CurveReading) -> dict:
    """One support/resistance line: price, how it was earned, how far away it is."""
    if level is None:
        return _row(label, "—", "none found in the buffer", _MUTED)
    price = reading.price
    distance = level.distance(price)
    if level.kind == "round":
        origin = _ROUNDNESS[min(_ROUNDNESS, key=lambda s: abs(s - level.strength))]
    else:
        plural = "es" if level.touches > 1 else ""
        origin = f"{level.touches} touch{plural} ({level.kind})"
    return _row(
        label,
        _price(level.price, price),
        f"{_price(distance, price)} away · "
        f"{reading.in_atr(distance):.2f} ATR · {origin}",
    )


def _levels_section(reading: CurveReading) -> dict:
    """Readable levels: what the market already respected, and the round numbers."""
    price = reading.price
    return {
        "title": "Readable levels",
        "note": "Prices the curve turned at, and the round numbers orders cluster on.",
        "rows": [
            _level_row("Nearest support", reading.nearest_support, reading),
            _level_row("Nearest resistance", reading.nearest_resistance, reading),
            _row(
                "Last swing low / high",
                f"{_price(reading.swing_low, price)} / "
                f"{_price(reading.swing_high, price)}",
                "most recent confirmed turns",
            ),
            _level_row("Round number below", reading.round_below, reading),
            _level_row("Round number above", reading.round_above, reading),
        ],
    }


def _position_section(position_reading: PositionReading) -> dict:
    """The since-open half of the panel — only built when a trade is open."""
    p = position_reading
    price_scale = p.current_price or 1.0
    stop_room = p.stop_room
    stop_detail = (
        "no software stop known"
        if stop_room == math.inf
        else (
            f"{p.stop_room_in_atr:.2f} ATR · "
            f"{p.stop_room_in_noise:.2f} × the noise band"
        )
    )
    breakeven = (
        f"reached after {p.minutes_to_breakeven} min"
        if p.minutes_to_breakeven is not None
        else "never reached"
    )
    return {
        "title": f"Open position ({p.direction})",
        "note": (
            "Measured from the open, in profit terms — identical for a long or a short."
        ),
        "rows": [
            _row(
                "Gain",
                f"{_signed(p.gain, '', _decimals(price_scale))} pts",
                f"{p.gain_euro:+.2f} € · {p.gain_in_atr:+.2f} ATR · "
                f"{p.minutes_open} min open",
                _tone_of(p.gain),
            ),
            _row(
                "Best / worst excursion",
                f"{_price(p.mfe, price_scale)} / {_price(p.mae, price_scale)}",
                (
                    f"{p.mfe_euro:+.2f} € offered · {p.mae_euro:.2f} € suffered · "
                    f"edge ×{p.edge_ratio:.2f}"
                    if p.edge_ratio != math.inf
                    else f"{p.mfe_euro:+.2f} € offered · never went adverse"
                ),
            ),
            _row(
                "Given back",
                f"{p.giveback_ratio:.0%}",
                f"{_price(p.giveback, price_scale)} off the peak · "
                f"peak {p.minutes_since_peak} min ago",
                _BAD if p.giveback_ratio > 0.5 else _MUTED,
            ),
            _row(
                "Break-even",
                "crossed" if p.crossed_breakeven else "never crossed",
                f"{breakeven} · {p.time_in_profit:.0%} of candles in profit",
                _GOOD if p.crossed_breakeven else _BAD,
            ),
            _row(
                "Since open",
                _signed(p.trend_since_open.pct, " %"),
                f"efficiency {p.efficiency_since_open:.2f} · "
                f"streak {p.favourable_streak:+d}"
                + (" · stalling" if p.is_stalling(5) else ""),
                _WARN if p.is_stalling(5) else _tone_of(p.trend_since_open.pct),
            ),
            _row(
                "Stop room",
                _price(stop_room, price_scale),
                stop_detail,
                _WARN if stop_room != math.inf and p.stop_room_in_atr < 1 else _MUTED,
            ),
            _row(
                "Locked at the stop",
                (
                    f"{p.locked_gain * p.euro_per_point:+.2f} €"
                    if p.euro_per_point
                    else _signed(p.locked_gain)
                ),
                (
                    "the gain is secured"
                    if p.is_secured
                    else "the stop is still under water"
                ),
                _GOOD if p.is_secured else _BAD,
            ),
            _row(
                "Progress to the margin line",
                f"{p.progress_to_margin:.2f}×" if p.progress_to_margin else "—",
                "1× = the move has cleared the bid/offer churn band",
                _GOOD if p.progress_to_margin >= 1 else _MUTED,
            ),
        ],
    }


def build_reading_view(
    buf,
    *,
    epic: str,
    name: str = "",
    position=None,
    spread: dict | None = None,
) -> dict:
    """Build the reading panel payload for one epic.

    Args:
        buf: The epic's :class:`~src.feed.price_buffer.EpicBuffer`.
        epic: Epic identifier (echoed back for the client).
        name: Human-readable market name, when the caller knows it.
        position: Open ``Position`` row for this epic, or ``None``. When given, a
            since-open section is added and the whole reading switches to the
            trade's close-out side.
        spread: Cached ``{"points", "euro"}`` dealing spread, when available.

    Returns:
        ``{epic, name, price, minutes, headline: [...], sections: [...]}``.
    """
    reading = CurveReading.of(buf)
    sections = _market_sections(reading, spread)

    if position is not None:
        try:
            position_reading = PositionReading.from_position(position, buf)
            sections.insert(0, _position_section(position_reading))
        except ValueError:
            # A row without any reference level cannot be measured; the market
            # half of the panel is still worth showing.
            pass

    headline = [
        {
            "label": "Regime",
            "value": reading.regime.value.replace("_", " ").title(),
            "tone": _REGIME_TONE[reading.regime],
        },
        {
            "label": "Price",
            "value": _price(reading.price, reading.price),
            "tone": _NEUTRAL,
        },
        {
            "label": "ATR",
            "value": f"{reading.atr_pct:.3f} %",
            "tone": _NEUTRAL,
        },
        {
            "label": "Alignment",
            "value": _signed(reading.alignment, "", 2),
            "tone": (
                _tone_of(reading.alignment) if abs(reading.alignment) > 0.4 else _MUTED
            ),
        },
        {
            "label": "History",
            "value": f"{reading.minutes} min",
            "tone": _MUTED if reading.minutes >= 30 else _WARN,
        },
    ]

    return {
        "epic": epic,
        "name": name,
        "price": reading.price,
        "minutes": reading.minutes,
        "headline": headline,
        "sections": sections,
    }
