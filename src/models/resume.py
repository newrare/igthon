"""Resume model — ported from Resume.php."""

from datetime import date

from sqlalchemy import Date, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models.database import Base


class Resume(Base):
    """Per-epic direction summary (day/week)."""

    __tablename__ = "resume"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    epic: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    day: Mapped[date | None] = mapped_column(Date)
    week: Mapped[str | None] = mapped_column(String(10))
    direction: Mapped[str | None] = mapped_column(String(10))
