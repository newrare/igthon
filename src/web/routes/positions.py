"""Positions route — view and manage trading positions."""

from datetime import date

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from src.models.position import Position, PositionState

router = APIRouter()


@router.get("/")
async def list_positions(
    request: Request,
    state: str | None = Query(None, description="Filter: open or close"),
    epic: str | None = Query(None, description="Filter by epic"),
    limit: int = Query(50, ge=1, le=200),
) -> JSONResponse:
    """List positions with optional filters."""
    session_factory = request.app.state.session_factory
    if not session_factory:
        return JSONResponse({"error": "Database not configured"}, status_code=503)

    async with session_factory() as session:
        query = select(Position).order_by(Position.id.desc()).limit(limit)

        if state:
            query = query.where(Position.state == state)
        if epic:
            query = query.where(Position.epic == epic)

        result = await session.execute(query)
        positions = result.scalars().all()

    return JSONResponse(
        {
            "count": len(positions),
            "positions": [
                {
                    "id": p.id,
                    "epic": p.epic,
                    "epic_name": p.epic_name,
                    "date": p.date.isoformat() if p.date else None,
                    "state": p.state.value if p.state else None,
                    "direction": p.direction,
                    "level_open": float(p.level_open) if p.level_open else None,
                    "level_close": float(p.level_close) if p.level_close else None,
                    "euro": float(p.euro) if p.euro is not None else None,
                    "reason_close": p.reason_close,
                    "time_open": p.time_open.isoformat() if p.time_open else None,
                    "time_close": p.time_close.isoformat() if p.time_close else None,
                }
                for p in positions
            ],
        }
    )


@router.get("/summary")
async def daily_summary(request: Request) -> JSONResponse:
    """Get today's trading summary."""
    session_factory = request.app.state.session_factory
    if not session_factory:
        return JSONResponse({"error": "Database not configured"}, status_code=503)

    today = date.today()

    async with session_factory() as session:
        result = await session.execute(select(Position).where(Position.date == today))
        positions = result.scalars().all()

    open_positions = [p for p in positions if p.state == PositionState.OPEN]
    closed_positions = [p for p in positions if p.state == PositionState.CLOSE]
    total_pnl = sum(float(p.euro or 0) for p in closed_positions)
    wins = sum(1 for p in closed_positions if (p.win or 0) > 0)
    win_rate = wins / len(closed_positions) if closed_positions else 0.0

    return JSONResponse(
        {
            "date": today.isoformat(),
            "open_count": len(open_positions),
            "closed_count": len(closed_positions),
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(win_rate, 3),
            "open_positions": [
                {"epic": p.epic, "level_open": float(p.level_open or 0)}
                for p in open_positions
            ],
        }
    )
