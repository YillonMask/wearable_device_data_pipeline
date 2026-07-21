"""Stdlib logging configuration.

Stream handler to stderr at INFO; rotating file handler under
``data/logs/wearable.log`` capturing DEBUG-and-above. Configured once at CLI
startup; safe to call multiple times.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_LOGGER_NAME = "wearable_pipeline"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_TIMEFMT = "%Y-%m-%dT%H:%M:%S"


def configure_logging(
    *,
    log_dir: Path = Path("data/logs"),
    level: int = logging.INFO,
) -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(_FORMAT, _TIMEFMT)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    stream.setLevel(level)
    logger.addHandler(stream)

    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "wearable.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    logger.propagate = False
    return logger
