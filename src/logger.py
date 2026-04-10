"""
RTIE Logging Module.

Provides centralized, rotating file loggers for all RTIE components.
Each concern (app, oracle, cache, validator, commands, errors) gets its
own dedicated log file with consistent formatting and automatic rotation.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Optional

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(correlation_id)s | %(name)s | %(message)s"
MAX_BYTES = 10 * 1024 * 1024  # 10 MB
BACKUP_COUNT = 5

# Map of concern -> log filename
LOG_FILES = {
    "app": "app.log",
    "oracle": "oracle.log",
    "cache": "cache.log",
    "validator": "validator.log",
    "commands": "commands.log",
    "errors": "errors.log",
}


class CorrelationFilter(logging.Filter):
    """Injects correlation_id into every log record.

    If no correlation_id is set on the record, defaults to 'N/A'.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Add correlation_id attribute to the log record.

        Args:
            record: The log record to enrich.

        Returns:
            True always — this filter enriches but never suppresses.
        """
        if not hasattr(record, "correlation_id"):
            record.correlation_id = "N/A"
        return True


def _ensure_log_dir() -> None:
    """Create the logs directory if it does not already exist."""
    os.makedirs(LOG_DIR, exist_ok=True)


def _create_rotating_handler(filename: str, level: int = logging.DEBUG) -> RotatingFileHandler:
    """Create a RotatingFileHandler for the given log filename.

    Args:
        filename: Name of the log file (e.g. 'app.log').
        level: Minimum logging level for this handler.

    Returns:
        Configured RotatingFileHandler instance.
    """
    _ensure_log_dir()
    filepath = os.path.join(LOG_DIR, filename)
    handler = RotatingFileHandler(
        filepath,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(level)
    formatter = logging.Formatter(LOG_FORMAT)
    handler.setFormatter(formatter)
    handler.addFilter(CorrelationFilter())
    return handler


def get_logger(name: str, concern: Optional[str] = None) -> logging.Logger:
    """Get a configured logger for the given module name.

    Args:
        name: The module name (typically __name__).
        concern: Optional log concern key ('app', 'oracle', 'cache',
                 'validator', 'commands'). If None, defaults to 'app'.

    Returns:
        A logging.Logger configured with rotating file handlers for the
        specified concern and the shared errors log.
    """
    concern = concern or "app"
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.addFilter(CorrelationFilter())

    # Primary concern handler
    if concern in LOG_FILES:
        logger.addHandler(_create_rotating_handler(LOG_FILES[concern]))

    # Errors handler — captures ERROR and above from all loggers
    error_handler = _create_rotating_handler(LOG_FILES["errors"], level=logging.ERROR)
    logger.addHandler(error_handler)

    # Prevent propagation to root logger
    logger.propagate = False

    return logger
