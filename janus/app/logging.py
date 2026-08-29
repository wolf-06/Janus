"""Structured (JSON) logging.

The plan requires structured logs (§3 Observability). ``structlog`` renders one JSON
object per line so logs can be shipped and queried without a parser.
"""

from __future__ import annotations

import logging

import structlog

_LEVELS: dict[str, int] = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}


def configure_logging(level: str = "INFO") -> None:
    """Configure stdlib + structlog to emit JSON lines at ``level``."""
    logging_level = _LEVELS.get(level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", level=logging_level, force=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
