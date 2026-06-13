"""Application configuration loaded from environment variables."""

from enum import StrEnum
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent.parent / ".env"


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
    streaming_bootstrap_points: int = 50  # /prices fallback seed size (>= sma_slow)
    streaming_max_epics: int = 40  # IG hard cap: 40 subscriptions per connection
    # Recent window (minutes) read from the candle table to rehydrate the buffer
    # on startup — wide enough to hold >= strategy_sma_slow one-minute candles.
    streaming_rehydrate_window_minutes: int = 90
    streaming_reconnect_max_backoff_seconds: int = 60

    # Strategy selection — name of the pluggable strategy driving entries.
    # Registered names live in src/strategies/__init__.py; each strategy is
    # documented in docs/strategies/<name>.md. The rest of the pipeline
    # (gates, orders, trailing stop, simulator, dashboard) is shared.
    strategy_name: str = "donchian_er"

    # Donchian breakout (strategy_name = "donchian_er")
    strategy_donchian_channel: int = 20  # channel lookback (candles)
    strategy_donchian_stop_atr_k: float = 2.5  # stop distance in ATR multiples
    # Regime gate: only trade epics whose Kaufman Efficiency Ratio over the
    # window reaches the threshold — i.e. skip sideways chop, keep clean trends.
    strategy_efficiency_period: int = 30
    strategy_min_efficiency: float = 0.45

    # Trend follower (strategy_name = "trend_follower") — Trend Volume Intraday
    strategy_min_r2: float = 0.70
    strategy_min_score: float = 0.75
    strategy_lookback_points: int = 20
    strategy_sma_fast: int = 5
    strategy_sma_slow: int = 20
    strategy_roc_period: int = 10
    strategy_max_spread_ratio: float = 0.0015
    strategy_stop_multiplier: float = 2.5
    strategy_target_multiplier: float = 4.0
    strategy_tactic: str = "spread"

    # Trailing stop (ATR-based adaptive follower)
    # Distance = k x ATR(period). k widens before break-even to let the trade
    # breathe, then tightens once price clears level_zero to lock in the gain.
    strategy_atr_period: int = 14
    strategy_atr_k_pre: float = 2.5  # multiplier before break-even
    strategy_atr_k_post: float = 1.5  # tighter multiplier once past level_zero
    strategy_trailing_step_ratio: float = 0.3  # min gain (xATR) before a PUT

    # Position / Risk management
    strategy_max_positions: int = 6
    strategy_max_trades_day: int = 50
    strategy_daily_loss_limit: float = -500.0
    strategy_daily_win_target: float = 300.0
    strategy_min_win_rate: float = 0.40
    strategy_hour_start: int = 9
    strategy_hour_end: int = 16
    strategy_hour_close: int = 17
    strategy_close_target: str = "follower"
    strategy_compensate_loose: bool = False
    strategy_euro_loss: float = 4000.0
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
