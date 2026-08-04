"""Main entry point for the IG Trading Bot.

Starts the scheduler and optional web interface.

Usage:
    cd python/
    python -m src.main                  # Start bot (scheduler only)
    python -m src.main --web            # Start bot + web dashboard
"""

import argparse
import asyncio
import logging
import signal

from src.core.api.client import IGClient
from src.core.api_error_log import APIErrorLog
from src.core.api_guard import APIGuard
from src.core.api_queue import APIQueue
from src.core.config import get_settings
from src.core.recorder import DEFAULT_LOG_FILE, LogBuffer, Recorder, setup_logging
from src.core.scheduler import BotScheduler, validate_strategy_selection
from src.feed.candle_store import CandleStore
from src.feed.market_data import MarketDataService
from src.feed.price_buffer import PriceBuffer
from src.feed.streaming import IGStreamingClient
from src.models.database import create_session_factory

logger = logging.getLogger(__name__)


class _SuppressEndpointAccessLog(logging.Filter):
    """Drop uvicorn access-log lines for a high-frequency polled endpoint.

    The dashboard polls ``/api/dashboard-fragments`` once a second, which would
    otherwise emit one ``200 OK`` access line per second and drown the log. The
    request still serves normally; only its access-log record is filtered.
    """

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = path

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access record args: (client, method, full_path, http_ver, status)
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            return self._path not in str(args[2])
        return True


# Default epics to track
DEFAULT_EPICS = [
    "IX.D.DAX.IFMM.IP",  # DAX 40 (1€)
    "IX.D.FTSE.DAILY.IP",  # FTSE 100
    "IX.D.CAC.IDF.IP",  # CAC 40
    "CS.D.EURUSD.TODAY.IP",  # EUR/USD
    "CS.D.GBPUSD.TODAY.IP",  # GBP/USD
]


#: Delay between IG reconnection attempts while the dashboard stays up (web mode).
RECONNECT_DELAY_SECONDS = 30


async def _run_connected(
    client: IGClient,
    *,
    settings,
    buffer: PriceBuffer,
    recorder: Recorder,
    session_factory,
    guard: APIGuard,
    stop_event: asyncio.Event,
    app=None,
) -> None:
    """Build the live bot stack on an authenticated client and run until shutdown.

    Wires the API queue, market-data feed, candle store, Lightstreamer stream and
    scheduler, then (when ``app`` is given) publishes them into the web app's
    state so the already-running dashboard goes live. Returns only when
    ``stop_event`` is set; all stack components are torn down on the way out.
    """
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

    # Restore the persisted epic list before starting so the dashboard
    # shows the day's discovered epics immediately after a restart.
    await scheduler.load_persisted_state()

    # Start scheduler (all jobs start in manual mode by default).
    scheduler.start()

    # The open / stop / close selection comes straight from .env (validated at
    # startup); there is no persisted selection to restore.

    # Restore the last-saved auto/manual mode for each job so the bot
    # resumes exactly as the user left it before the server was stopped.
    await scheduler.load_job_preferences()

    # Replay any fixed-time job whose scheduled slot was missed while the
    # server was down (e.g. started after the 07:30 epic refresh). Run it in
    # the background: a missed job (the epic refresh in particular) can be
    # slow and may stall behind the rate-limited APIQueue, and it must not
    # block startup. run_catch_up handles its own errors internally.
    catch_up_task = asyncio.create_task(scheduler.run_catch_up())

    # Publish the live stack to the (already-serving) web app so the dashboard
    # switches from the degraded "connecting" state to fully operational.
    if app is not None:
        app.state.api_queue = api_queue
        app.state.scheduler = scheduler
        app.state.candle_store = candle_store
        app.state.startup_error = None
        app.state.connecting = False
    logger.info("IG connection established — bot is live.")

    try:
        # Wait for shutdown
        await stop_event.wait()
    finally:
        # Stop the catch-up replay if it is still draining the queue.
        catch_up_task.cancel()
        scheduler.stop()
        if streaming is not None:
            await streaming.stop()
        await api_queue.stop()


async def run_bot(
    *, with_web: bool = False, log_buffer: LogBuffer | None = None
) -> None:
    """Start the full trading bot with scheduler.

    In ``--web`` mode the dashboard is started **first and unconditionally**, so a
    failure to reach IG (expired API key, broker outage, network error) never
    crashes the process: the dashboard comes up in a degraded state showing the
    error, and the IG connection is retried in the background until it succeeds.

    Args:
        with_web: Also start the FastAPI web interface.
    """
    settings = get_settings()
    # The buffer window bounds every strategy's usable lookback, so it is a
    # setting rather than a literal (see docs/DATAFLOW.md §3).
    buffer = PriceBuffer(max_candles=settings.buffer_max_candles)
    recorder = Recorder(settings)
    session_factory = create_session_factory(settings)
    error_log = APIErrorLog(max_entries=20)
    guard = APIGuard(max_per_minute=30, max_per_second=20)

    # The .env file is the single source of truth for the open / stop / close
    # selection. Validate it up front: a missing or unknown name must not start
    # trading. In --web mode we still bring the dashboard up (degraded) so it can
    # display the "configure your .env" message; otherwise we log and exit.
    config_error: str | None = None
    try:
        validate_strategy_selection(settings)
    except ValueError as exc:
        config_error = str(exc)
        logger.error("%s", config_error)

    logger.info("Starting IG Trading Bot (%s environment)", settings.ig_env.value)

    # Graceful shutdown — installed once, shared by the connection loop.
    stop_event = asyncio.Event()

    def handle_signal(*_: object) -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Start the web server immediately, in a degraded state. The IG connection is
    # established afterwards (and retried on failure); the dashboard reads the
    # live objects from app.state at request time, so it goes live automatically
    # once connected — and shows app.state.startup_error meanwhile.
    app = None
    server = None
    if with_web:
        import uvicorn

        from src.web.app import create_app

        app = create_app(
            settings,
            buffer,
            session_factory,
            scheduler=None,
            error_log=error_log,
            guard=guard,
            api_queue=None,
            candle_store=None,
            log_buffer=log_buffer,
        )
        app.state.startup_error = config_error
        app.state.connecting = config_error is None
        config = uvicorn.Config(
            app,
            host=settings.web_host,
            port=settings.web_port,
            log_level="info",
        )
        server = uvicorn.Server(config)
        # Silence the once-a-second dashboard poll in the access log (keeps
        # every other request visible). Attaching to the logger (not a handler)
        # means it survives uvicorn's own logging setup in serve().
        logging.getLogger("uvicorn.access").addFilter(
            _SuppressEndpointAccessLog("/api/dashboard-fragments")
        )
        asyncio.create_task(server.serve())
        logger.info(
            "Web interface started on %s:%d (connecting to IG…)",
            settings.web_host,
            settings.web_port,
        )

    # Invalid .env strategy selection: never connect or trade. In web mode keep
    # the (degraded) dashboard up until shutdown so the error stays visible.
    if config_error is not None:
        if with_web:
            await stop_event.wait()
        else:
            logger.error("Exiting — fix the strategy selection in .env and restart.")

    # Connection loop: (re)connect to IG until a clean shutdown. A connection
    # failure no longer aborts the process — in web mode we surface it and retry.
    while not stop_event.is_set() and config_error is None:
        try:
            async with IGClient(settings, error_log=error_log, guard=guard) as client:
                await _run_connected(
                    client,
                    settings=settings,
                    buffer=buffer,
                    recorder=recorder,
                    session_factory=session_factory,
                    guard=guard,
                    stop_event=stop_event,
                    app=app,
                )
            break  # _run_connected returns only when shutdown was requested
        except Exception as exc:
            logger.error("IG connection failed: %s", exc)
            if app is not None:
                # Degrade the dashboard: clear any stale live objects and surface
                # the reason so the page shows an explanation instead of 500s.
                app.state.startup_error = str(exc)
                app.state.connecting = False
                app.state.scheduler = None
                app.state.api_queue = None
                app.state.candle_store = None
            if not with_web:
                logger.error(
                    "Cannot start without an IG connection — exiting. "
                    "Check IG_API_KEY and credentials in .env."
                )
                break
            logger.info(
                "Retrying IG connection in %ds (dashboard stays up).",
                RECONNECT_DELAY_SECONDS,
            )
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=RECONNECT_DELAY_SECONDS
                )
            except TimeoutError:
                continue  # delay elapsed → try connecting again
            break  # shutdown requested during the wait

    if server is not None:
        server.should_exit = True
    logger.info("Bot stopped cleanly")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="IG Trading Bot")
    parser.add_argument("--web", action="store_true", help="Start web dashboard")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--log-file",
        default=str(DEFAULT_LOG_FILE),
        help="Rotating log file path (empty string disables the file sink)",
    )
    parser.add_argument(
        "--log-file-level",
        default="DEBUG",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Minimum level written to the rotating log file",
    )
    args = parser.parse_args()

    log_buffer = setup_logging(
        args.log_level,
        log_file=args.log_file or None,
        file_level=args.log_file_level,
    )

    asyncio.run(run_bot(with_web=args.web, log_buffer=log_buffer))


if __name__ == "__main__":
    main()
