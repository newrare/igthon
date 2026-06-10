"""Tests for the recorder service — in-memory log buffer retention."""

import logging

from src.services.recorder import LogBuffer


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
