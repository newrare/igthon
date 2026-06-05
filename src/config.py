"""Application configuration loaded from environment variables."""

from enum import Enum
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent.parent / ".env"


class IGEnvironment(str, Enum):
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

    # Market scanner — search-term based discovery
    # Each term is passed to GET /markets?searchTerm=X (v1).
    # Override via SCANNER_SEARCH_TERMS='["EUR/USD","Gold",...]' in .env
    scanner_search_terms: list[str] = [
        # Forex — major pairs
        "EUR/USD",
        "GBP/USD",
        "USD/JPY",
        "AUD/USD",
        "USD/CHF",
        "USD/CAD",
        "NZD/USD",
        "EUR/GBP",
        "EUR/JPY",
        "EUR/CHF",
        "GBP/JPY",
        "AUD/JPY",
        # Indices
        "Germany 40",
        "UK 100",
        "Wall Street",
        "US Tech 100",
        "US 500",
        "France 40",
        "Japan 225",
        "Australia 200",
        "EU Stocks 50",
        # Commodities
        "Gold",
        "Silver",
        "Brent Crude",
        "US Crude",
        "Natural Gas",
        "Copper",
        "Wheat",
        "Sugar",
        "Coffee",
        "Cocoa",
        "Corn",
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

    # Strategy — Trend Volume Intraday
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

    @property
    def ig_base_url(self) -> str:
        """Return the IG API base URL based on environment."""
        if self.ig_env == IGEnvironment.LIVE:
            return "https://api.ig.com/gateway/deal"
        return "https://demo-api.ig.com/gateway/deal"


def get_settings() -> Settings:
    """Create and return application settings."""
    return Settings()
