"""Minimal logging setup for EpiDash (keeps epidash self-contained)."""

import logging
import os
import sys

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _level() -> str:
    return os.environ.get("EPIDASH_LOG_LEVEL", "INFO").upper()


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logging.getLogger("epidash").handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(_FORMAT))
        root = logging.getLogger("epidash")
        root.addHandler(handler)
        root.setLevel(_level())
    return logger
