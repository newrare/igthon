"""FastAPI web application for the trading dashboard."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import Settings
from src.services.price_buffer import PriceBuffer

if TYPE_CHECKING:
    from src.services.api_error_log import APIErrorLog
    from src.services.api_guard import APIGuard
    from src.services.api_queue import APIQueue
    from src.services.candle_store import CandleStore
    from src.services.recorder import LogBuffer

templates = Jinja2Templates(directory="src/web/templates")


def create_app(
    settings: Settings,
    buffer: PriceBuffer,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    scheduler=None,
    error_log: APIErrorLog | None = None,
    guard: APIGuard | None = None,
    api_queue: APIQueue | None = None,
    candle_store: CandleStore | None = None,
    log_buffer: LogBuffer | None = None,
) -> FastAPI:
    """Create the FastAPI application with injected dependencies.

    Args:
        settings: Application settings.
        buffer: Shared price buffer instance.
        session_factory: Async DB session factory.
        scheduler: BotScheduler instance for pause/resume control.
        error_log: Shared APIErrorLog for the error history section.
        guard: Shared APIGuard for availability indicator and reset action.
        api_queue: Shared APIQueue for the queue status section.

    Returns:
        Configured FastAPI application.
    """
    app = FastAPI(title="IG Trading Bot", version="0.1.0")

    app.mount("/static", StaticFiles(directory="src/web/static"), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse("src/web/static/favicon.ico")

    # Store shared state
    app.state.settings = settings
    app.state.buffer = buffer
    app.state.session_factory = session_factory
    app.state.scheduler = scheduler
    app.state.error_log = error_log
    app.state.guard = guard
    app.state.api_queue = api_queue
    app.state.candle_store = candle_store
    app.state.log_buffer = log_buffer

    # Include routes
    from src.web.routes.backtest import router as backtest_router
    from src.web.routes.charts import router as charts_router
    from src.web.routes.dashboard import router as dashboard_router
    from src.web.routes.positions import router as positions_router
    from src.web.routes.simulator import router as simulator_router

    app.include_router(dashboard_router)
    app.include_router(positions_router, prefix="/positions")
    app.include_router(charts_router)
    app.include_router(simulator_router)
    app.include_router(backtest_router)

    return app
