"""Logging and notification service — ported from Record.php.

Provides structured logging and optional email notifications.
"""

import collections
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path

from src.core.config import Settings

# Default rotating-log location — a predictable temp path so the file can be
# tailed/read while debugging (e.g. why the rolling ranker isn't opening).
DEFAULT_LOG_FILE = Path("/tmp/ig_bot/ig_bot.log")

# Dedicated audit log for every IG API call we send. Rotated by *day* (not size)
# so the retention window is guaranteed in days regardless of call volume — the
# size-based main log can churn through a noisy week in minutes.
DEFAULT_API_LOG_FILE = Path("/tmp/ig_bot/ig_api_calls.log")

# Dedicated log for IG API calls we *abandoned* (errors). Kept separate from the
# audit log so failures survive a restart and the main log's size rotation —
# the gap that lost the root cause of the never_opened phantoms (the confirm
# error scrolled out of the size-rotated files before it could be read). Also
# day-rotated for a guaranteed retention window.
DEFAULT_API_ERROR_LOG_FILE = Path("/tmp/ig_bot/ig_api_errors.log")

# Logger name of the IG HTTP client (src/core/api/client.py uses __name__). Its
# per-call DEBUG records ("GET …", "POST … payload=…") are the audit trail.
_API_CLIENT_LOGGER = "src.core.api.client"

# Dedicated logger for abandoned API calls (errors). Its records go ONLY to the
# error-log file (propagate disabled in setup) so it never duplicates the
# APIQueue's own ``logger.error`` in the main log/console. A NullHandler is
# attached at import time so writing to it is harmless before setup_logging runs
# (e.g. in tests) — no "No handlers could be found" last-resort spew.
API_ERROR_LOGGER = "ig_bot.api_errors"
logging.getLogger(API_ERROR_LOGGER).addHandler(logging.NullHandler())

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
    api_log_file: str | Path | None = DEFAULT_API_LOG_FILE,
    api_backup_days: int = 14,
    api_error_log_file: str | Path | None = DEFAULT_API_ERROR_LOG_FILE,
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

    A third sink — the **API audit log** — is attached to the IG client logger
    only: every ``GET/POST/PUT/DELETE`` we send is written to ``api_log_file``,
    rotated once per day and kept ``api_backup_days`` days. This is the durable
    record used to answer "what calls did we make?" (e.g. tracing an unexpected
    ``adopted`` open back to the POST that created it), independently of the
    noisy size-rotated main file.

    Args:
        level: Console log level (DEBUG, INFO, WARNING, ERROR).
        log_file: Destination path for the rotating file sink, or None.
        file_level: Minimum level written to the file sink.
        max_bytes: Rotate the file once it reaches this size.
        backup_count: Number of rotated backups to keep.
        api_log_file: Destination for the per-call API audit log, or None.
        api_backup_days: Days of API audit logs to retain (one file per day).

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

    # Reduce noise from third-party libraries. aiosqlite dumps every SQL
    # statement (with the full parameter list) at DEBUG — it alone accounted for
    # ~99% of the file volume and churned the size-rotated log every few minutes,
    # evicting the records that actually matter. apscheduler's executor logs a
    # "Running job"/"executed successfully" pair for the 20s sync job (~8.6k
    # lines/day of pure heartbeat). Both are silenced to WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)

    # API audit log — a per-call trail kept for `api_backup_days` days. Attached
    # to the client logger (not root) so it captures only IG calls, and pinned to
    # DEBUG so the records survive even if the console/file level is raised.
    _setup_api_audit_log(formatter, api_log_file, api_backup_days)

    # API error log — abandoned calls only, on a dedicated durable file so a
    # failure (HTTP status + IG errorCode) survives restarts and log rotation.
    _setup_api_error_log(formatter, api_error_log_file, api_backup_days)

    buf = LogBuffer(max_per_level=30)
    root.addHandler(buf)
    return buf


def _setup_api_audit_log(
    formatter: logging.Formatter,
    api_log_file: str | Path | None,
    api_backup_days: int,
) -> None:
    """Attach (or re-attach) the day-rotated API audit handler to the client logger."""
    api_logger = logging.getLogger(_API_CLIENT_LOGGER)
    # Drop any audit handler from a previous setup_logging call so repeated calls
    # (e.g. in tests or a re-init) don't stack duplicates.
    for handler in list(api_logger.handlers):
        if isinstance(handler, TimedRotatingFileHandler):
            api_logger.removeHandler(handler)
            handler.close()

    if api_log_file is None:
        return

    path = Path(api_log_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        api_handler = TimedRotatingFileHandler(
            path,
            when="midnight",
            backupCount=api_backup_days,
            encoding="utf-8",
        )
        api_handler.setLevel(logging.DEBUG)
        api_handler.setFormatter(formatter)
        # Pin the client logger to DEBUG so its per-call records always reach this
        # handler, regardless of how quiet the root/console level is set to be.
        api_logger.setLevel(logging.DEBUG)
        api_logger.addHandler(api_handler)
        logger.info(
            "API audit log to %s (rotate daily, keep %d days)",
            path,
            api_backup_days,
        )
    except OSError as exc:  # pragma: no cover - defensive (perms/full disk)
        logger.warning("Could not open API audit log %s: %s", api_log_file, exc)


def _setup_api_error_log(
    formatter: logging.Formatter,
    error_log_file: str | Path | None,
    backup_days: int,
) -> None:
    """Attach (or re-attach) the day-rotated API *error* handler.

    Records go only to this file (propagate disabled), so the file is a clean,
    durable record of every abandoned IG call — distinct from the per-call audit
    log and unaffected by the main log's size rotation.
    """
    api_error_logger = logging.getLogger(API_ERROR_LOGGER)
    api_error_logger.propagate = False
    # Drop any file handler from a previous setup_logging call (keep the
    # import-time NullHandler) so repeated calls don't stack duplicates.
    for handler in list(api_error_logger.handlers):
        if isinstance(handler, TimedRotatingFileHandler):
            api_error_logger.removeHandler(handler)
            handler.close()

    if error_log_file is None:
        return

    path = Path(error_log_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = TimedRotatingFileHandler(
            path,
            when="midnight",
            backupCount=backup_days,
            encoding="utf-8",
        )
        handler.setLevel(logging.ERROR)
        handler.setFormatter(formatter)
        api_error_logger.setLevel(logging.ERROR)
        api_error_logger.addHandler(handler)
        logger.info(
            "API error log to %s (rotate daily, keep %d days)", path, backup_days
        )
    except OSError as exc:  # pragma: no cover - defensive (perms/full disk)
        logger.warning("Could not open API error log %s: %s", error_log_file, exc)


def log_api_error(
    *,
    method: str,
    endpoint: str,
    version: int,
    http_status: int | None,
    ig_error_code: str,
    attempts: int,
    error: str,
    label: str | None = None,
) -> None:
    """Append one abandoned IG API call to the dedicated, durable error log.

    Called by :class:`APIQueue` when a call is given up on. Mirrors the
    in-memory queue error buffer (shown on the dashboard) onto a day-rotated
    file so the failure — its HTTP status and IG ``errorCode`` — survives a
    restart and the main log's churn.
    """
    logging.getLogger(API_ERROR_LOGGER).error(
        "%s %s (v%d) -> HTTP %s IG=%s after %d attempt(s)%s: %s",
        method,
        endpoint,
        version,
        http_status if http_status is not None else "—",
        ig_error_code or "—",
        attempts,
        f" [{label}]" if label else "",
        error,
    )


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
