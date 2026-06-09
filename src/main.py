"""Main entry point for the IG Trading Bot.

Starts the scheduler and optional web interface.

Usage:
    cd python/
    python -m src.main                  # Start bot (scheduler only)
    python -m src.main --web            # Start bot + web dashboard
    python -m src.main --analyze-only   # Single analysis pass (no trading)
"""

import argparse
import asyncio
import logging
import signal

from src.api.client import IGClient
from src.api.streaming import IGStreamingClient
from src.config import get_settings
from src.models.database import create_session_factory
from src.services.api_error_log import APIErrorLog
from src.services.api_guard import APIGuard
from src.services.api_queue import APIQueue
from src.services.candle_store import CandleStore
from src.services.compute import compute_signal
from src.services.market_data import MarketDataService
from src.services.price_buffer import PriceBuffer
from src.services.recorder import LogBuffer, Recorder, setup_logging
from src.services.scheduler import BotScheduler

logger = logging.getLogger(__name__)

# Default epics to track
DEFAULT_EPICS = [
    "IX.D.DAX.IFMM.IP",  # DAX 40 (1€)
    "IX.D.FTSE.DAILY.IP",  # FTSE 100
    "IX.D.CAC.IDF.IP",  # CAC 40
    "CS.D.EURUSD.TODAY.IP",  # EUR/USD
    "CS.D.GBPUSD.TODAY.IP",  # GBP/USD
]


async def analyze_once(epics: list[str] | None = None) -> None:
    """Run a single analysis pass and display results.

    Useful for testing the indicator pipeline without trading.
    """
    settings = get_settings()
    target_epics = epics or DEFAULT_EPICS
    buffer = PriceBuffer(max_candles=100)

    async with IGClient(settings) as client:
        service = MarketDataService(client, buffer)

        print(
            f"\n{'Epic':<35} {'Score':>6} {'Dir':>7} {'R²':>5} {'ROC':>6} {'Spread':>7}"
        )
        print("-" * 75)

        for epic in target_epics:
            try:
                await service.fetch_candles(epic, "MINUTE", 50)
                buf = buffer.get(epic)
                if buf and len(buf) >= 20:
                    sig = compute_signal(epic, buf)
                    if sig:
                        print(
                            f"  {epic:<33} {sig.score:>6.2f} "
                            f"{sig.direction:>7} {sig.regression.r_squared:>5.2f} "
                            f"{sig.roc:>6.2f} {sig.spread:>7.2f}"
                        )
                    else:
                        print(f"  {epic:<33} {'N/A':>6} {'—':>7}")
                else:
                    print(f"  {epic:<33} {'NO DATA':>6}")
            except Exception as exc:
                print(f"  {epic:<33} {'ERROR':>6} — {exc}")

        print()


async def run_bot(*, with_web: bool = False, log_buffer: LogBuffer | None = None) -> None:
    """Start the full trading bot with scheduler.

    Args:
        with_web: Also start the FastAPI web interface.
    """
    settings = get_settings()
    buffer = PriceBuffer(max_candles=200)
    recorder = Recorder(settings)
    session_factory = create_session_factory(settings)
    error_log = APIErrorLog(max_entries=20)
    guard = APIGuard(max_per_minute=50, max_per_second=25)

    logger.info("Starting IG Trading Bot (%s environment)", settings.ig_env.value)

    async with IGClient(settings, error_log=error_log, guard=guard) as client:
        # All IG calls are funnelled through the queue: a single worker drains it
        # while respecting the guard's rate limits, retries transient failures and
        # waits-then-resumes on quota blocks.
        api_queue = APIQueue(
            client,
            guard,
            max_attempts=settings.queue_max_attempts,
            retry_margin_seconds=settings.queue_retry_margin_seconds,
            recent_size=settings.queue_recent_size,
        )
        await api_queue.start()

        market_data = MarketDataService(api_queue, buffer)
        candle_store = CandleStore(
            session_factory,
            dump_dir=settings.candle_dump_dir,
            retention_days=settings.candle_retention_days,
        )

        # Live price data comes from the Lightstreamer feed (no historical-data
        # allowance consumed). /prices is only used to seed the buffer when the DB
        # lacks recent history. The feed persists completed candles back to the
        # candle table, which in turn rehydrates the buffer on the next restart.
        streaming: IGStreamingClient | None = None
        if settings.streaming_enabled:
            streaming = IGStreamingClient(
                client,
                buffer,
                settings,
                on_candle_persist=candle_store.save,
            )
            await streaming.start()

        scheduler = BotScheduler(
            settings=settings,
            client=api_queue,
            buffer=buffer,
            market_data=market_data,
            recorder=recorder,
            epics=DEFAULT_EPICS,
            session_factory=session_factory,
            candle_store=candle_store,
            streaming=streaming,
        )

        # Handle graceful shutdown
        stop_event = asyncio.Event()

        def handle_signal(*_: object) -> None:
            logger.info("Shutdown signal received")
            stop_event.set()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        # Restore the persisted epic list before starting so the dashboard
        # shows the day's discovered epics immediately after a restart.
        await scheduler.load_persisted_state()

        # Start scheduler (all jobs start in manual mode by default).
        scheduler.start()

        # Restore the last-saved auto/manual mode for each job so the bot
        # resumes exactly as the user left it before the server was stopped.
        await scheduler.load_job_preferences()

        # Optionally start web server
        if with_web:
            import uvicorn

            from src.web.app import create_app

            app = create_app(
                settings,
                buffer,
                session_factory,
                scheduler=scheduler,
                error_log=error_log,
                guard=guard,
                api_queue=api_queue,
                candle_store=candle_store,
                log_buffer=log_buffer,
            )
            config = uvicorn.Config(
                app,
                host=settings.web_host,
                port=settings.web_port,
                log_level="info",
            )
            server = uvicorn.Server(config)
            asyncio.create_task(server.serve())
            logger.info(
                "Web interface started on %s:%d", settings.web_host, settings.web_port
            )

        # Wait for shutdown
        await stop_event.wait()

        scheduler.stop()
        if streaming is not None:
            await streaming.stop()
        await api_queue.stop()
        logger.info("Bot stopped cleanly")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="IG Trading Bot")
    parser.add_argument("--web", action="store_true", help="Start web dashboard")
    parser.add_argument(
        "--analyze-only",
        action="store_true",
        help="Run a single analysis pass and exit",
    )
    parser.add_argument("--epics", nargs="*", help="Override default epics list")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    log_buffer = setup_logging(args.log_level)

    if args.analyze_only:
        asyncio.run(analyze_once(args.epics))
    else:
        asyncio.run(run_bot(with_web=args.web, log_buffer=log_buffer))


if __name__ == "__main__":
    main()
