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
    """

    __tablename__ = "job_preference"

    action: Mapped[str] = mapped_column(String(80), primary_key=True)
    auto: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
