# Ruff Error Resolution Plan

This document outlines the steps required to fix the errors reported by the `ruff` linter during the `pre-commit` run. The errors are categorized for clarity, with specific file paths and code examples for each fix.

---

### 1. Fix: `F811 - Redefinition of unused Application`

This error is caused by a duplicate import in a single line.

-   **File**: `telegram_bot/services/download_manager.py`
-   **Line**: 10
-   **Action**: Remove the redundant `Application` import.

**Change this:**
'''python
from telegram.ext import Application, ContextTypes, Application
'''

**To this:**
'''python
from telegram.ext import Application, ContextTypes
'''

---

### 2. Fix: `F821 - Undefined Name in Type Hint`

This error occurs because the type hint in `delete_workflow.py` uses class names (`'Movie'`, `'Show'`, etc.) that aren't imported, so `ruff` cannot validate them. We can fix this by importing them inside a `TYPE_CHECKING` block, which is ignored at runtime but visible to linters.

-   **File**: `telegram_bot/workflows/delete_workflow.py`
-   **Action**: Add the `TYPE_CHECKING` block and import the necessary `plexapi` classes.

**Add the following code near the top of the file, after the other imports:**

'''python
from typing import List, Optional, Union, Tuple, TYPE_CHECKING # Add TYPE_CHECKING

# ... other imports

# Add this block
if TYPE_CHECKING:
    from plexapi.video import Movie, Episode, Show, Season
'''

**Then, update the function signature to use the real (non-string) types:**

'''python
# Change this line:
def _find_media_in_plex_by_path(plex: PlexServer, path_to_delete: str) -> Optional[Union['Movie', 'Episode', 'Show', 'Season']]: # type: ignore

# To this (remove the quotes and the #type: ignore comment):
def _find_media_in_plex_by_path(plex: PlexServer, path_to_delete: str) -> Optional[Union[Movie, Episode, Show, Season]]:'''

---

### 3. Fix: `E402 - Module Level Import Not at Top of File`

This error appears in all test files because the `sys.path` modification occurs after project modules are imported. The fix is to move the `sys.path` logic to the very top.

-   **Files**: All files in the `tests/` directory (e.g., `tests/handlers/test_callback_handlers.py`, `tests/services/test_media_manager.py`, etc.).
-   **Action**: For each test file, ensure the import block is ordered correctly.

**Change this structure:**
'''python
import pytest
from telegram import Update
# ... other library imports

import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from telegram_bot.handlers.callback_handlers import button_handler # Project import
'''

**To this structure:**
'''python
import sys
from pathlib import Path
from unittest.mock import AsyncMock

# This block MUST come before any other project imports
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

import pytest
from telegram import Update, CallbackQuery
from telegram_bot.handlers.callback_handlers import button_handler
'''

---

### 4. Fix: `E501 - Line Too Long`

This is the most common error. The standard is to keep lines under 88 characters. The solution is to wrap long lines in parentheses `()`.

-   **Files**: Multiple files as listed in the `ruff` output.
-   **Action**: Identify and reformat all lines that exceed the character limit.

#### General Strategy:

Enclose the entire long statement in parentheses and break it into multiple lines. `black` will often handle the rest of the formatting for you.

#### Example 1: Long f-string (`telegram_bot/handlers/error_handler.py`)

**Change this:**
'''python
context_message = (
    f"An exception was raised while handling an update\n"
    f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
    "</pre>\n\n"
    # ...
)
'''

**To this:**
'''python
context_message = (
    "An exception was raised while handling an update\n"
    f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
    "</pre>\n\n"
    # ...
)
'''
*(Note: Sometimes just changing how f-strings are combined is enough)*. A more robust way is to build the string piece by piece if it's very complex, or break the line inside the parentheses.

#### Example 2: Long String with Escaped Markdown (`telegram_bot/workflows/search_workflow.py`)

**Change this:**
'''python
error_text = f"That doesn't look like a valid 4\\-digit year\\. Please try again for *{escape_markdown(title, version=2)}* or cancel\\."
'''

**To this:**
'''python
error_text = (
    "That doesn't look like a valid 4\\-digit year\\. "
    f"Please try again for *{escape_markdown(title, version=2)}* or cancel\\."
)
'''

#### Example 3: Long Function Call (`telegram_bot/services/scraping_service.py`)

**Change this:**
'''python
results = await scrape_1337x("Sample Movie 2023", "movie", "https://1337x.to/search/{query}/1/", Ctx(), base_query_for_filter="Sample Movie")
'''

**To this:**
'''python
results = await scrape_1337x(
    "Sample Movie 2023",
    "movie",
    "https://1337x.to/search/{query}/1/",
    Ctx(),
    base_query_for_filter="Sample Movie",
)
'''

---

### Next Steps

1.  Apply all the fixes described above to the corresponding files.
2.  Stage all the changes: `git add .`
3.  Run the pre-commit hooks again: `pre-commit run --all-files`
4.  The hooks should now pass, allowing you to commit your changes.