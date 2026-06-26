"""Logging and notification service — ported from Record.php.

Provides structured logging and optional email notifications.
"""

import collections
import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.core.config import Settings

# Default rotating-log location — a predictable temp path so the file can be
# tailed/read while debugging (e.g. why the rolling ranker isn't opening).
DEFAULT_LOG_FILE = Path("/tmp/ig_bot/ig_bot.log")

logger = logging.getLogger("ig_bot")


class LogBuffer(logging.Handler):
    """Rolling in-memory log handler — keeps the last N records *per level*.

    Each severity (INFO, WARNING, ERROR, …) has its own bounded deque, so a
    flood of INFO records can never evict the most recent WARNING/ERROR ones:
    the last ``max_per_level`` of each level are always retained. A monotonic
    sequence number stamped on every record lets get_all() merge the buckets
    back into chronological order.

    Thread-safe: emit() is called within the handler's own lock (via handle()),
    and get_all() also acquires it before reading the buckets.
    """

    def __init__(self, max_per_level: int = 30) -> None:
        super().__init__(level=logging.INFO)
        self._max_per_level = max_per_level
        self._buckets: dict[str, collections.deque] = {}
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[0]:
                import traceback

                tb = traceback.format_exception_only(
                    record.exc_info[0], record.exc_info[1]
                )
                msg += " | " + "".join(tb).strip()
        except Exception:
            msg = str(record.msg)
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        bucket = self._buckets.get(record.levelname)
        if bucket is None:
            bucket = collections.deque(maxlen=self._max_per_level)
            self._buckets[record.levelname] = bucket
        self._seq += 1
        bucket.append(
            {
                "seq": self._seq,
                "ts": ts,
                "level": record.levelname,
                "name": record.name,
                "msg": msg,
            }
        )

    def get_all(self) -> list[dict]:
        """Return all retained entries, oldest first, merged across levels."""
        self.acquire()
        try:
            merged = [entry for bucket in self._buckets.values() for entry in bucket]
        finally:
            self.release()
        merged.sort(key=lambda e: e["seq"])
        return [{k: v for k, v in e.items() if k != "seq"} for e in merged]


def setup_logging(
    level: str = "INFO",
    *,
    log_file: str | Path | None = DEFAULT_LOG_FILE,
    file_level: str = "DEBUG",
    max_bytes: int = 5_000_000,
    backup_count: int = 5,
) -> LogBuffer:
    """Configure application-wide logging and return the in-memory log buffer.

    Two sinks are wired onto the root logger:

    * a **console** handler at ``level`` (the operator-facing stream);
    * a **rotating file** handler at ``file_level`` (default ``DEBUG``) so the
      full detailed trace — including the ``Rolling select …`` decisions that are
      DEBUG-only — is always persisted to ``log_file`` for post-mortem reading,
      independently of how noisy the console is set to be.

    The root level is lowered to the most verbose of the two so DEBUG records
    reach the file even when the console stays at INFO. The file rotates at
    ``max_bytes`` keeping ``backup_count`` backups (``ig_bot.log.1`` …). Pass
    ``log_file=None`` to disable the file sink (e.g. in tests).

    Args:
        level: Console log level (DEBUG, INFO, WARNING, ERROR).
        log_file: Destination path for the rotating file sink, or None.
        file_level: Minimum level written to the file sink.
        max_bytes: Rotate the file once it reaches this size.
        backup_count: Number of rotated backups to keep.

    Returns:
        LogBuffer handler attached to the root logger.
    """
    console_level = getattr(logging, level.upper(), logging.INFO)
    file_lvl = getattr(logging, file_level.upper(), logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    # Drop our own previously-attached handlers so repeated calls don't stack
    # duplicates; foreign handlers (e.g. pytest's caplog) are left untouched.
    for handler in list(root.handlers):
        if isinstance(handler, RotatingFileHandler | LogBuffer) or (
            type(handler) is logging.StreamHandler
        ):
            root.removeHandler(handler)

    root.setLevel(min(console_level, file_lvl) if log_file else console_level)

    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_file is not None:
        path = Path(log_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setLevel(file_lvl)
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
            logger.info(
                "Logging to %s (file level=%s, rotate %d×%dB)",
                path,
                logging.getLevelName(file_lvl),
                backup_count,
                max_bytes,
            )
        except OSError as exc:  # pragma: no cover - defensive (perms/full disk)
            logger.warning("Could not open log file %s: %s", log_file, exc)

    # Reduce noise from third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    buf = LogBuffer(max_per_level=30)
    root.addHandler(buf)
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
