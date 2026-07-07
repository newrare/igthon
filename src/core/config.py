"""Application configuration loaded from environment variables."""

from enum import StrEnum
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# config.py lives at src/core/config.py, so the project root (where .env sits)
# is three parents up. Resolve first so it works regardless of the CWD.
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


class IGEnvironment(StrEnum):
    """IG API environment selector."""

    DEMO = "demo"
    LIVE = "live"


class Settings(BaseSettings):
    """Application settings loaded from .env file."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # IG API
    ig_env: IGEnvironment = IGEnvironment.DEMO
    ig_api_key: str
    ig_username: str
    ig_password: str
    ig_account_id: str

    # Database
    database_url: str = "sqlite:///./ig_trading.db"

    # Web
    web_host: str = "0.0.0.0"
    web_port: int = 8000

    # Market scanner — search-term based discovery.
    # Each term is passed to GET /markets?searchTerm=X (v1). Broad terms are used
    # deliberately: a currency code like "USD" returns every pair containing it,
    # so a handful of codes cover all FX majors/minors/crosses. Off-class results
    # (SHARES/RATES/…) that broad terms also return are dropped by the
    # instrument-type filter (``scanner_allowed_instrument_types``), so widening
    # the terms improves coverage without polluting the universe.
    # Override via SCANNER_SEARCH_TERMS='["USD","Gold",...]' in .env
    scanner_search_terms: list[str] = [
        # Forex — broad currency-code searches (each returns all pairs with it).
        "USD",
        "EUR",
        "GBP",
        "JPY",
        "AUD",
        "CAD",
        "CHF",
        "NZD",
        # Indices — broad term plus regional names not always surfaced by "Index".
        "Index",
        "Germany 40",
        "UK 100",
        "Wall Street",
        "US Tech 100",
        "US 500",
        "France 40",
        "Japan 225",
        "Australia 200",
        "EU Stocks 50",
        "Netherlands 25",
        "Spain 35",
        "Hong Kong",
        # Commodities — broad terms plus named contracts.
        "Oil",
        "Gold",
        "Silver",
        "Brent Crude",
        "Natural Gas",
        "Copper",
        "Wheat",
        "Sugar",
        "Coffee",
        "Cocoa",
        "Corn",
        "Platinum",
    ]

    # Asset classes to keep in the tradable universe. IG search returns
    # instruments of every kind (including SHARES whose name matches a commodity
    # or index term — e.g. gold miners for "Gold", with epics ending in
    # ``.CASH``). Only these ``instrumentType`` values are retained, applied both
    # at search time and on the fetched market details; everything else (SHARES,
    # RATES, SECTORS, BINARY, …) is dropped. Markets whose type is unknown/blank
    # are kept (we can't prove they're out of scope).
    # Override via SCANNER_ALLOWED_INSTRUMENT_TYPES='["CURRENCIES",...]' in .env
    scanner_allowed_instrument_types: list[str] = [
        "CURRENCIES",
        "INDICES",
        "COMMODITIES",
    ]

    # Candle persistence — durable price history for charts + backtesting.
    # Populated passively from the existing collect-and-analyze fetch (no extra
    # API calls). Candles older than the retention window are dumped to CSV
    # (for offline simulation) then deleted from the live table.
    candle_retention_days: int = 7
    candle_dump_dir: str = "./dumps"

    # API queue — serialises and throttles all IG API calls
    queue_max_attempts: int = 3  # 3-strike budget for transient errors
    queue_retry_margin_seconds: int = 5  # margin added on top of the guard cooldown
    queue_recent_size: int = 50  # dashboard recent-tasks ring buffer size

    # Streaming — live candles via Lightstreamer (replaces /prices polling).
    # The historical /prices endpoint is only used as a fallback to seed the
    # indicator buffer when the candle table has no recent history for an epic.
    streaming_enabled: bool = True  # master switch; False = legacy polling path
    streaming_resolution: str = "1MINUTE"  # Lightstreamer CHART scale
    streaming_max_epics: int = 40  # IG hard cap: 40 subscriptions per connection
    streaming_reconnect_max_backoff_seconds: int = 60
    # Watchdog: an open position's epic must always have a live feed. If its most
    # recent streamed candle is older than this (or it has none), the scheduler
    # force re-subscribes it — covers an individual Lightstreamer subscription that
    # silently stalls/expires without a full disconnect. Keep comfortably above
    # the candle resolution (3x a 1-minute bar) to avoid churn on slow markets.
    streaming_stale_seconds: int = 180

    # Open / stop / close selection — the trading decisions are decoupled and
    # chosen independently, and the ``.env`` file is the SINGLE source of truth:
    # there is no code default (empty string here), no database persistence and no
    # dashboard switching. The entry strategy (src/entry/) decides only the
    # direction; the stop policy (src/stops/) places the initial protective stop;
    # the close profile (src/exit/) owns the break-even/margin references and
    # every per-tick stop update. The rest of the pipeline (gates, orders, sizing,
    # simulator, dashboard) is shared. Registered names: src/entry/__init__.py,
    # src/stops/__init__.py and src/exit/zones/__init__.py.
    #
    # The close side is split into THREE independent zones (the single composer
    # profile ``close_zoneprofit`` wires one updater per zone), each selected on
    # its own so its behaviour can be tuned without influencing the other two:
    #   CLOSE_ZONESTART  — open → break-even   (ZONESTART_UPDATERS)
    #   CLOSE_ZONEMARGE  — break-even → margin (ZONEMARGE_UPDATERS)
    #   CLOSE_ZONEPROFIT — above the margin    (ZONEPROFIT_UPDATERS)
    #
    # Each field is REQUIRED: a missing/empty or unknown value makes startup fail
    # with a clear "configure your .env" message (see
    # ``validate_strategy_selection`` in src/core/scheduler.py).
    open_strategy: str = ""  # e.g. open_donchian / open_projection / open_ranking
    stop_strategy: str = ""  # e.g. stop_support / stop_atr
    close_zonestart: str = ""  # zone open→break-even   (e.g. hold)
    close_zonemarge: str = ""  # zone break-even→margin (e.g. hold)
    close_zoneprofit: str = ""  # zone above margin      (e.g. trailing_ratchet)

    # Open strategies (open_donchian, open_projection, open_ranking), stop policies
    # (stop_support, stop_atr) and the close profile (close_zoneprofit) keep their
    # parameters as constants on their own classes under src/entry/, src/stops/ and
    # src/exit/ — including the Donchian channel / ATR / Efficiency-Ratio knobs and
    # the market-scanner spread gate (MarketScanner.DEFAULT_MAX_SPREAD_RATIO). Tune
    # them there; only the selection names above live in the environment.

    # Trailing stop (ATR-based chandelier follower)
    # Distance = k x ATR(period); the stop trails k×ATR below the running high
    # and only ever ratchets up. pre/post are the multipliers before/after
    # break-even. They are kept EQUAL (no post-break-even tightening) on purpose:
    # the strategies are trend-followers, and tightening the stop once a trade
    # turns green cut winners off at ~1.5 ATR while losers still ran the full
    # 2.5 ATR — winners ended up smaller than losers. A single consistent width
    # lets winners run, which is the whole point of a Donchian breakout.
    strategy_atr_period: int = 14
    strategy_atr_k_pre: float = 2.5  # multiplier before break-even
    strategy_atr_k_post: float = 2.5  # kept equal: do not tighten after break-even
    strategy_trailing_step_ratio: float = 0.3  # min gain (xATR) before a PUT

    # The "close_zoneprofit" close profile composes a stop policy (src/stops/) at
    # open with three per-zone stop updaters (src/exit/zones/) — their shaping
    # parameters are constants in the matching modules (not .env): tune them there.
    # The "stop_support" distance anchors the initial stop below a recency-weighted
    # last-hour support (see src/stops/stop_support.py).

    # Position management
    # Minutes before an epic's own market close to force-close a position on it.
    # Applied to the per-epic Epic.market_close_utc when known (IG openingHours).
    # When the close time is unknown there is no time-based force-close at all
    # (no hard global fallback) — the position rides its broker-side stop.
    strategy_close_margin_minutes: int = 5
    # Do not OPEN a new position when the epic's own market closes within this
    # many minutes (on top of ``strategy_close_margin_minutes``). Prevents opening
    # a trade the per-epic close rule would force-close almost immediately —
    # paying the spread for nothing. Only applies to epics whose close time is
    # known; a 24h market (unknown close) is never blocked.
    strategy_open_close_buffer_minutes: int = 60
    # Safety margin added on top of IG's minimum stop distance when placing an
    # order. IG rejects a stop that sits at/inside its minimum-distance rule, and
    # the price drifts between the market snapshot and the order landing, so a
    # stop clamped exactly to the minimum is frequently rejected ("Stop trop
    # près"). Padding the floor by this fraction (0.15 = 15%) absorbs that drift.
    strategy_stop_min_distance_margin: float = 0.15
    # Loss-recovery — master switch (single boolean, the only .env knob here).
    # When True, a long that closes on the "trend-reversal at open" pattern (a
    # quick stop-out that never crossed break-even) immediately triggers a
    # double-size SELL on the same epic, managed by a mirrored trailing_ratchet
    # short exit, to try and recoup the loss on the ensuing decline. The detection
    # thresholds and the size multiplier are constants in src/execution/recovery.py
    # (convention: only selectors/switches live in .env). Anti-loop: a recovery
    # short that itself loses never spawns another recovery. The recovery short
    # counts as the single open position, so the ranking strategy's one-position
    # budget still holds. See src/execution/recovery.py and src/exit/recovery_short.py.
    recovery_enabled: bool = False

    # Max margin (EUR) to open one minimum-size BUY; epics above this are dropped
    # from the tradable/streaming set — pointless to analyze (and subscribe to)
    # markets we could never afford to open. Set to 0 to disable the filter.
    max_funds_per_position: float = 5000.0

    @property
    def ig_base_url(self) -> str:
        """Return the IG API base URL based on environment."""
        if self.ig_env == IGEnvironment.LIVE:
            return "https://api.ig.com/gateway/deal"
        return "https://demo-api.ig.com/gateway/deal"


def get_settings() -> Settings:
    """Create and return application settings."""
    return Settings()
