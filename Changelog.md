## Example
Commit: c841859d0ba579df0ecd8433f528f5e64874ebe6
- /tests/interactive_scripts/test_1337x_scrapper.py
  - First detailed explanation of update
  - Second detailed explanation of update
  - Third detailed explanation of update
- /tests/interactive_scripts/test_generic_scraper.py
  - First detailed explanation of update
  - Second detailed explanation of update
  - Third detailed explanation of update
- etc...
---
Commit: <pending>
- /telegram_bot/workflows/delete_workflow.py
  - Added helper utilities to detect duplicate “name twin” files, format result buttons with concise size info, and remove Plex collections after disk cleanup.
  - Reworked delete-target tracking and success messaging so Plex skips, manual deletions, and collection removals surface `{filename}{extension} | {size_in_GB}` without breaking Markdown.
  - Returned structured metadata from the Plex deletion routine to handle skip/not-found states, guard against `None` Plex connections, and decide when filesystem deletes or collection cleanup should occur.
- /tests/workflows/test_delete_workflow.py
  - Updated the happy-path delete test for the structured Plex result contract and verified list buttons now include size labels.
  - Added new cases covering name-twin skips, manual deletion messaging, collection cleanup, and the `_has_name_twin` helper's edge conditions.
  - Normalized assertions to compare against Markdown-escaped filenames so expectations match Telegram's final output.
- /.pre-commit-config.yaml
  - Added local commit-msg and post-commit hooks so the changelog entry populates the commit message and records the resulting hash automatically.
- /scripts/changelog_hooks.py
  - Implemented the helper CLI that extracts the top changelog entry, writes commit messages, and replaces `<pending>` with the resolved commit hash.
- /README.md
  - Documented the changelog-driven workflow plus the extra `pre-commit install` commands required to enable the commit/post-commit hooks locally, and clarified that developers should sync with `--extra dev` so `uv run pytest` has access to fixtures like `mocker`.
- /AGENTS.md
  - Updated the setup instructions to sync dependencies with the `dev` extra so future contributors automatically install `pytest-mock` and the rest of the tooling required for the test suite.
