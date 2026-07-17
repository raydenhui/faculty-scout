"""Centralised logging configuration for fscout.

Usage:
    from ffscout.logging_config import get_logger
    log = get_logger(__name__)
    log.debug("detail"), log.info("milestone"), log.warning("issue"), log.error("fail")
"""

from __future__ import annotations

import logging
import sys

# Module-level flag – set by CLI before pipeline runs.
_verbose: bool = False
_debug: bool = False

LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)-25s %(message)s"
LOG_FORMAT_VERBOSE = "%(asctime)s [%(levelname)-7s] %(name)-25s %(funcName)s:%(lineno)d  %(message)s"


def configure(verbose: bool = False, debug: bool = False) -> None:
    """Call once at startup to configure global logging."""
    global _verbose, _debug
    _verbose = verbose
    _debug = debug

    level = logging.DEBUG if debug else (logging.INFO if verbose else logging.WARNING)
    fmt = LOG_FORMAT_VERBOSE if debug else LOG_FORMAT

    root = logging.getLogger("fscout")
    root.setLevel(level)
    root.handlers.clear()
    root.propagate = False

    # Console handler (stderr)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
    handler.setLevel(level)
    root.addHandler(handler)

    # File handler for debug logs (debug.log)
    if debug:
        from pathlib import Path
        fh = logging.FileHandler(Path("debug.log"), mode="w", encoding="utf-8")
        fh.setFormatter(logging.Formatter(LOG_FORMAT_VERBOSE, datefmt="%Y-%m-%d %H:%M:%S"))
        fh.setLevel(logging.DEBUG)
        root.addHandler(fh)

    # Separate file for full LLM conversations (always when debug)
    if debug:
        from pathlib import Path
        llm_log = logging.getLogger("fscout.llm_conversation")
        llm_log.handlers.clear()
        llm_log.propagate = False
        llm_log.setLevel(logging.DEBUG)
        fh2 = logging.FileHandler(Path("llm_conversation.log"), mode="w", encoding="utf-8")
        fh2.setFormatter(logging.Formatter("%(message)s"))
        llm_log.addHandler(fh2)


def get_llm_logger() -> logging.Logger:
    """Return a logger for LLM conversation content (prompts + responses)."""
    return logging.getLogger("fscout.llm_conversation")


def get_logger(name: str) -> logging.Logger:
    """Return a logger child of the fscout namespace."""
    return logging.getLogger(f"fscout.{name}")


def is_verbose() -> bool:
    return _verbose


def is_debug() -> bool:
    return _debug
