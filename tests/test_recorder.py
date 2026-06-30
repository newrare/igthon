"""Tests for the recorder service — in-memory log buffer retention."""

import logging
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

import pytest

from src.core.recorder import API_ERROR_LOGGER, LogBuffer, log_api_error, setup_logging


def _make_logger(buf: LogBuffer) -> logging.Logger:
    """Return an isolated logger wired to the given buffer."""
    logger = logging.getLogger(f"test_recorder.{id(buf)}")
    logger.handlers.clear()
    logger.addHandler(buf)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def test_per_level_retention_keeps_recent_errors_through_info_flood() -> None:
    """A flood of INFO must not evict the most recent WARNING/ERROR records."""
    buf = LogBuffer(max_per_level=3)
    logger = _make_logger(buf)

    logger.error("err-old")
    logger.warning("warn-1")
    for i in range(50):
        logger.info(f"info-{i}")
    logger.error("err-new")

    entries = buf.get_all()
    by_level = lambda lvl: [e for e in entries if e["level"] == lvl]  # noqa: E731

    # Both errors survive despite 50 interleaved INFO records.
    assert [e["msg"] for e in by_level("ERROR")] == ["err-old", "err-new"]
    assert [e["msg"] for e in by_level("WARNING")] == ["warn-1"]
    # INFO is capped at max_per_level (the 3 most recent).
    assert [e["msg"] for e in by_level("INFO")] == ["info-47", "info-48", "info-49"]


def test_get_all_is_chronological_and_hides_seq() -> None:
    """Merged output is ordered oldest-first and exposes no internal seq key."""
    buf = LogBuffer(max_per_level=5)
    logger = _make_logger(buf)

    logger.info("first")
    logger.error("second")
    logger.warning("third")

    entries = buf.get_all()
    assert [e["msg"] for e in entries] == ["first", "second", "third"]
    assert all("seq" not in e for e in entries)
    assert all(set(e) == {"ts", "level", "name", "msg"} for e in entries)


@pytest.fixture
def restore_root_logging():
    """Snapshot and restore the root logger so setup_logging can't leak handlers
    into other tests (it mutates the root logger by design)."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)


def test_setup_logging_writes_debug_to_rotating_file(tmp_path, restore_root_logging):
    """The file sink captures DEBUG even when the console stays at INFO."""
    log_file = tmp_path / "ig_bot.log"
    setup_logging("INFO", log_file=log_file, file_level="DEBUG")

    logging.getLogger("ig_bot.test").debug("rolling select diagnostic line")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert log_file.exists()
    assert "rolling select diagnostic line" in log_file.read_text()


def test_setup_logging_rotates_when_exceeding_max_bytes(tmp_path, restore_root_logging):
    """Crossing max_bytes spills the old content into a numbered backup file."""
    log_file = tmp_path / "ig_bot.log"
    setup_logging(
        "DEBUG", log_file=log_file, file_level="DEBUG", max_bytes=500, backup_count=1
    )

    logger = logging.getLogger("ig_bot.rotate")
    for i in range(200):
        logger.info("padding line number %d with some trailing text", i)
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert log_file.exists()
    assert (tmp_path / "ig_bot.log.1").exists()


def test_log_api_error_writes_to_dedicated_file(tmp_path, restore_root_logging):
    """log_api_error appends abandoned calls to a dedicated, durable file, and
    those records never leak into the main log (propagate is disabled)."""
    main_log = tmp_path / "ig_bot.log"
    err_log = tmp_path / "ig_api_errors.log"
    api_logger = logging.getLogger(API_ERROR_LOGGER)
    try:
        setup_logging(
            "INFO",
            log_file=main_log,
            api_log_file=None,
            api_error_log_file=err_log,
        )

        log_api_error(
            method="POST",
            endpoint="/positions/otc",
            version=2,
            http_status=400,
            ig_error_code="validation.failed",
            attempts=3,
            error="boom",
            label="open X",
        )
        for handler in api_logger.handlers:
            handler.flush()

        content = err_log.read_text()
        assert "POST /positions/otc (v2)" in content
        assert "HTTP 400" in content
        assert "IG=validation.failed" in content
        assert "open X" in content
        # Propagate is disabled, so it must NOT appear in the main log.
        assert "/positions/otc" not in main_log.read_text()
    finally:
        # Detach the tmp_path file handler so a later log_api_error never writes
        # into the deleted temp dir.
        for handler in list(api_logger.handlers):
            if isinstance(handler, TimedRotatingFileHandler):
                api_logger.removeHandler(handler)
                handler.close()


def test_setup_logging_none_disables_file_sink(restore_root_logging):
    """Passing log_file=None attaches no rotating file handler."""
    setup_logging("INFO", log_file=None)

    handlers = logging.getLogger().handlers
    assert not any(isinstance(h, RotatingFileHandler) for h in handlers)
