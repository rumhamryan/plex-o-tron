"""PTB environment bootstrap for tests.

Sets environment flags before importing python-telegram-bot in tests.
"""

from __future__ import annotations

import os

os.environ.setdefault("PTB_TIMEDELTA", "1")
