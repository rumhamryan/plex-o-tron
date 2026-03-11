"""PTB environment bootstrap.

This module sets environment flags before importing python-telegram-bot elsewhere.
Import this module as the first import in any entrypoint that needs PTB.
"""

from __future__ import annotations

import os

# Opt-in to timedelta for RetryAfter.retry_after to avoid deprecation warnings
os.environ.setdefault("PTB_TIMEDELTA", "1")
