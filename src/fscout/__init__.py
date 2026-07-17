"""Faculty Scout – AI-driven async CLI tool for scraping university faculty information."""

from __future__ import annotations

import sys

__version__ = "0.1.0"

# ---------------------------------------------------------------------------
# Windows ProactorEventLoop subprocess pipe cleanup workaround
# ---------------------------------------------------------------------------
# When Playwright (or any asyncio subprocess) creates pipe transports on
# Windows, those transports can be garbage-collected *after* the event loop
# is closed.  At that point ``self._sock`` still exists but the underlying OS
# handle is already invalid, so ``fileno()`` raises ``ValueError`` during the
# ``__repr__`` call inside ``__del__``.  This hook suppresses those noisy
# "Exception ignored" messages that are harmless post-shutdown noise.
_orig_unraisable = sys.unraisablehook


def _suppress_closed_pipe(hook_args):
    exc_value = hook_args.exc_value
    if isinstance(exc_value, ValueError) and "closed pipe" in str(exc_value):
        return
    _orig_unraisable(hook_args)


sys.unraisablehook = _suppress_closed_pipe

from .config import AppConfig, load_config, mask_secrets
from .database import Database
from .schema import Schema, load_schema

__all__ = [
    "AppConfig",
    "Database",
    "Schema",
    "__version__",
    "load_config",
    "load_schema",
    "mask_secrets",
]
