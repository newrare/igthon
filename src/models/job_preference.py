"""JobPreference model — persists scheduler job auto/manual state across restarts."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models.database import Base


class JobPreference(Base):
    """Stores the last user-chosen mode (automatic vs manual) for each scheduler job.

    Keyed by the action name (e.g. ``collect_and_analyze``).  Loaded on scheduler
    startup so the bot resumes with the same job configuration it had before the
    server was stopped.

    ``last_run_at`` records the last time a catch-up-eligible fixed-time job ran
    successfully (whether scheduled or manually triggered). On startup the
    scheduler compares it against the most recent scheduled fire time to detect
    runs missed while the server was down, and replays them once.
    """

    __tablename__ = "job_preference"

    action: Mapped[str] = mapped_column(String(80), primary_key=True)
    auto: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
