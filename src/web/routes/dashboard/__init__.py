"""Dashboard route package (decomposed from the former dashboard.py)."""

from src.web.routes.dashboard.fragments import _build_fragments
from src.web.routes.dashboard.router import router
from src.web.routes.dashboard.shell import _render_dashboard

__all__ = ["router", "_build_fragments", "_render_dashboard"]
