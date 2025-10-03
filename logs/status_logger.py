"""Simple status logger used across the data pipeline."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict

_LOG_NAME = "grantwatch"
_LOG_LEVELS: Dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

_log_file = Path(__file__).resolve().parent / "grantwatch.log"


def _configure_logger() -> logging.Logger:
    logger = logging.getLogger(_LOG_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    try:
        _log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(_log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        logger.warning("Failed to set up file logging at %s", _log_file)

    return logger


_logger = _configure_logger()


def logger(level: str, message: str) -> None:
    """Log a message at the requested level.

    Parameters
    ----------
    level: str
        Logging level name such as ``info`` or ``error``.
    message: str
        The message to log.
    """
    log_level = _LOG_LEVELS.get(level.lower())
    if log_level is None:
        _logger.warning("Unknown log level '%s'; defaulting to INFO", level)
        log_level = logging.INFO

    _logger.log(log_level, message)
