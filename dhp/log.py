"""DHP structured logging. stdlib logging, one logger, handoff_id as
correlation key. No prints in library code."""
from __future__ import annotations

import logging
import os
import sys

_LOGGER_NAME = "dhp"
_configured = False


def get_logger(name: str = _LOGGER_NAME) -> logging.Logger:
    return logging.getLogger(name)


def configure(level: str | None = None) -> logging.Logger:
    global _configured
    logger = get_logger()
    if _configured:
        return logger
    lvl = (level or os.environ.get("DHP_LOG_LEVEL", "INFO")).upper()
    logger.setLevel(getattr(logging, lvl, logging.INFO))
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    _configured = True
    return logger


def hid12(hid: str) -> str:
    return hid[:12] if isinstance(hid, str) else "?"
