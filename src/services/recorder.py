"""Logging and notification service — ported from Record.php.

Provides structured logging and optional email notifications.
"""

import collections
import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage

from src.config import Settings

logger = logging.getLogger("ig_bot")


class LogBuffer(logging.Handler):
    """Rolling in-memory log handler — keeps the last N INFO+ records.

    Thread-safe: emit() is called within the handler's own lock (via handle()),
    and get_all() also acquires it before reading the deque.
    """

    def __init__(self, max_entries: int = 30) -> None:
        super().__init__(level=logging.INFO)
        self._entries: collections.deque = collections.deque(maxlen=max_entries)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[0]:
                import traceback

                tb = traceback.format_exception_only(record.exc_info[0], record.exc_info[1])
                msg += " | " + "".join(tb).strip()
        except Exception:
            msg = str(record.msg)
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        self._entries.append(
            {
                "ts": ts,
                "level": record.levelname,
                "name": record.name,
                "msg": msg,
            }
        )

    def get_all(self) -> list[dict]:
        self.acquire()
        try:
            return list(self._entries)
        finally:
            self.release()


def setup_logging(level: str = "INFO") -> LogBuffer:
    """Configure application-wide logging and return the in-memory log buffer.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR).

    Returns:
        LogBuffer handler attached to the root logger.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Reduce noise from third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    buf = LogBuffer(max_entries=30)
    logging.getLogger().addHandler(buf)
    return buf


def send_email(
    settings: Settings,
    subject: str,
    body: str,
) -> bool:
    """Send an email notification.

    Args:
        settings: Application settings with email config.
        subject: Email subject.
        body: Email body text.

    Returns:
        True if sent successfully, False otherwise.
    """
    if not hasattr(settings, "email_enabled") or not settings.email_enabled:
        return False

    try:
        msg = EmailMessage()
        msg["Subject"] = f"[IG Bot] {subject}"
        msg["From"] = settings.email_from
        msg["To"] = settings.email_to
        msg.set_content(body)

        with smtplib.SMTP("localhost", 25) as server:
            server.send_message(msg)

        logger.info("Email sent: %s", subject)
        return True
    except Exception as exc:
        logger.error("Failed to send email: %s", exc)
        return False


class Recorder:
    """Structured recorder for trading events.

    Wraps the logging module with trading-specific context.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    def info(self, message: str, **kwargs: object) -> None:
        """Log an informational trading event."""
        extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
        logger.info("%s %s", message, extra)

    def warn(self, message: str, **kwargs: object) -> None:
        """Log a warning (recoverable issue)."""
        extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
        logger.warning("%s %s", message, extra)

    def error(self, message: str, **kwargs: object) -> None:
        """Log an error (failure requiring attention)."""
        extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
        logger.error("%s %s", message, extra)

    def trade_open(self, epic: str, **kwargs: object) -> None:
        """Log a position opening."""
        self.info(f"OPEN {epic}", **kwargs)
        if self._settings:
            send_email(
                self._settings,
                f"Position opened: {epic}",
                f"Opened position on {epic}\n"
                + "\n".join(f"  {k}: {v}" for k, v in kwargs.items()),
            )

    def trade_close(self, epic: str, reason: str, **kwargs: object) -> None:
        """Log a position closing."""
        self.info(f"CLOSE {epic} ({reason})", **kwargs)
        if self._settings:
            send_email(
                self._settings,
                f"Position closed: {epic} ({reason})",
                f"Closed position on {epic}\nReason: {reason}\n"
                + "\n".join(f"  {k}: {v}" for k, v in kwargs.items()),
            )
