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
    streaming_bootstrap_points: int = 50  # /prices fallback seed size (>= sma_slow)
    streaming_max_epics: int = 40  # IG hard cap: 40 subscriptions per connection
    # Recent window (minutes) read from the candle table to rehydrate the buffer
    # on startup — wide enough to hold >= strategy_sma_slow one-minute candles.
    streaming_rehydrate_window_minutes: int = 90
    streaming_reconnect_max_backoff_seconds: int = 60

    # Open/close selection — entry strategy and close profile are decoupled and
    # chosen independently. The entry strategy (src/entry/) decides only the
    # direction; the close profile (src/exit/) owns the stop/target/trailing.
    # The rest of the pipeline (gates, orders, sizing, simulator, dashboard) is
    # shared. Registered names: src/entry/__init__.py and src/exit/__init__.py.
    entry_strategy_name: str = "donchian_er"
    close_profile_name: str = "atr_trailing"

    # Deprecated alias kept for the legacy strategies/ registry still used by
    # not-yet-ported entries; new code reads entry_strategy_name.
    strategy_name: str = "donchian_er"

    # Donchian breakout (strategy_name = "donchian_er")
    strategy_donchian_channel: int = 20  # channel lookback (candles)
    strategy_donchian_stop_atr_k: float = 2.5  # stop distance in ATR multiples
    # Regime gate: only trade epics whose Kaufman Efficiency Ratio over the
    # window reaches the threshold — i.e. skip sideways chop, keep clean trends.
    # Raised to 0.60 (from 0.45): on real 1-minute data the looser gate let too
    # many marginal breakouts through, and each one bled the bid/offer spread
    # ("spread churn"). A stricter regime gate trades less but cleaner.
    strategy_efficiency_period: int = 30
    strategy_min_efficiency: float = 0.60

    # Trend follower (strategy_name = "trend_follower") — Trend Volume Intraday
    strategy_min_r2: float = 0.70
    strategy_min_score: float = 0.75
    strategy_lookback_points: int = 20
    strategy_sma_fast: int = 5
    strategy_sma_slow: int = 20
    strategy_roc_period: int = 10
    strategy_max_spread_ratio: float = 0.0010  # tightened: scalper edge is spread-sized
    strategy_stop_multiplier: float = 2.5
    strategy_target_multiplier: float = 4.0
    strategy_tactic: str = "spread"

    # Momentum scalper (strategy_name = "momentum_scalper") — high-frequency,
    # buy fresh up-ticks and grab a spread-multiple of profit immediately.
    strategy_scalper_momentum_period: int = 8  # recent-trend ROC window (candles)
    strategy_scalper_min_roc: float = 0.20  # min ROC over the window, in percent
    strategy_scalper_confirm_period: int = 1  # very-recent rising-closes to confirm
    strategy_scalper_win_ratio: float = 4.0  # take-profit in net spread multiples
    strategy_scalper_stop_lookback: int = 60  # support window (≈ last hour, candles)
    strategy_scalper_stop_buffer_atr_k: float = 0.5  # ATR buffer below the support
    strategy_scalper_max_stop_atr_k: float = 3.0  # cap on stop distance (ATR mult)

    # Trend template (strategy_name = "trend_template") — hourly cross-epic
    # selector. Every hour it ranks all tradable epics by how close their recent
    # curve is to a theoretical up-trend (R² of an upward linear regression) and
    # opens only the single best one. Quantity follows a martingale on the day's
    # trailing loss streak (win → ×1, each consecutive loss → ×base_multiplier).
    # The cross-epic ranking + sizing live in the scheduler; this block tunes the
    # per-epic eligibility/levels in src/strategies/trend_template.py.
    strategy_trend_template_regression_period: int = 30  # candles for the R² fit
    strategy_trend_template_min_r2: float = 0.80  # min R² to be a clean up-trend
    strategy_trend_template_win_ratio: float = 2.0  # take-profit in net spreads
    strategy_trend_template_projection_horizon: int = 60  # candles (~1h) to target
    strategy_trend_template_stop_lookback: int = 60  # support window — last hour
    strategy_trend_template_stop_buffer_atr_k: float = 0.5  # ATR cushion below support
    strategy_trend_template_base_multiplier: int = 3  # martingale factor per loss
    strategy_trend_template_max_multiplier: int = 27  # hard cap on martingale size

    # Dip rebound (strategy_name = "dip_rebound") — per-epic, buy a significant
    # pullback inside a globally rising market the moment the price turns back
    # up, capturing the bounce from a better entry than chasing fresh highs.
    strategy_dip_rebound_trend_period: int = 60  # candles for the up-trend fit
    strategy_dip_rebound_min_trend_r2: float = 0.55  # looser R²: a dip dents the fit
    strategy_dip_rebound_pullback_lookback: int = 30  # window for the swing high
    strategy_dip_rebound_min_pullback_atr_k: float = 1.5  # min dip depth, in ATR
    strategy_dip_rebound_rebound_period: int = 2  # rising closes confirming the bounce
    strategy_dip_rebound_win_ratio: float = 2.0  # take-profit in reward/risk multiples
    strategy_dip_rebound_stop_lookback: int = 10  # window for the dip bottom (stop)
    strategy_dip_rebound_stop_buffer_atr_k: float = 0.5  # ATR cushion below the dip

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
