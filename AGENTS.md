# AGENTS.md

> A concise, agent-oriented guide to hacking on this project.
> Format reference: AGENTS.md open format.

SITUATION: You are an expert Python developer maintaining a Telegram bot that searches, downloads, and organizes media for Plex.
CHALLENGE: Implement based on INSTRUCTIONS.md if it exists, otherwise from the user prompt, with maintainability and readability first.
AUDIENCE: Developers who will maintain this code in the future, including those who were not involved in initial development.
FORMAT:
- Use clear, descriptive naming conventions.
- Include meaningful comments explaining the "why" behind complex logic.
- Follow the principle of least surprise.
- Implement appropriate error handling with informative messages.
- Organize code with logical separation of concerns.
FOUNDATIONS:
- Prioritize readability over clever optimizations.
- Include comprehensive input validation.
- Implement proper exception handling.
- Use consistent patterns throughout.
- Create modular components with clear responsibilities.
- Include unit tests that document expected behavior.

## Project snapshot

- Name: **plex-o-tron-bot** (Telegram bot that searches, downloads, organizes media, and integrates with Plex).
- Primary entrypoint: `__main__.py` at the repository root (builds app, loads config, registers handlers, starts polling).
- Python: **3.12+** (`requires-python = ">=3.12"`).
- Key libs: `python-telegram-bot`, `libtorrent`, `beautifulsoup4`, `httpx`, `plexapi`, `thefuzz`, `PyYAML`, `wikipedia`.

## Setup commands

```bash
# 1) Create venv with uv & activate (POSIX)
uv venv && source .venv/bin/activate
# (Windows)
uv venv && .\.venv\Scripts\activate

# 2) System deps (Linux, for libtorrent)
sudo apt-get update && sudo apt-get install -y libtorrent-rasterbar-dev

# 3) Sync dependencies from lockfile/project metadata
uv pip sync pyproject.toml --extra dev

# 4) Install pre-commit hooks
pre-commit install
pre-commit install --hook-type commit-msg --hook-type post-commit
```

Linux libtorrent headers are required for the Python package to function.

## Run / Dev

```bash
# Start the bot
uv run __main__.py
```

- The bot reads **`config.ini`** from the repo root and exits if required fields are missing.
- App startup wires `post_init`/`post_shutdown` for download resume + persistence.

## Tests

```bash
uv run pre-commit run --all-files
```

- This runs lint/format/type/test hooks (`ruff`, `ruff-format`, `mypy`, `pytest`, etc.).
- You can run `uv run pytest` directly for faster feedback during iteration.

## Code style & tooling

- **Ruff** for lint + format (`line-length = 100`).
- **Mypy** is configured in `pyproject.toml` and enforced by pre-commit.
- **pre-commit** is the quality gate and also runs `uv-lock`.
- `pyright` exists as a dev dependency, but the enforced type-check hook is mypy.

## Project structure (agent-relevant)

- `__main__.py` — Application bootstrap and handler registration.
- `telegram_bot/config.py` — Loads/validates config, creates save paths, logging, constants. **Do not** hardcode secrets.
- `telegram_bot/state.py` — Persists/resumes active and queued downloads to `persistence.json`.
- `telegram_bot/handlers/` — Command/callback/message/error handlers.
- `telegram_bot/workflows/` — User interaction flows (`search_workflow`, `delete_workflow`) and parsing/session state.
- `telegram_bot/ui/` — Bot-facing messages/views/confirmation prompts.
- `telegram_bot/services/` — Auth, torrent/download/media/plex/search/scraping logic (mostly package-based modules).
- `telegram_bot/services/scrapers/` — Site scrapers and shared generic scraping strategies.
- `telegram_bot/domain/types.py` — Shared domain-level types.
- `telegram_bot/utils.py` — Common helpers (formatting, message edit/send safety, torrent parsing).
- `telegram_bot/utility_scripts/` — Setup/restart/config-generation scripts.

## Configuration

Create **`config.ini`** (values shown here are placeholders):

```ini
[telegram]
bot_token = TELEGRAM_BOT_TOKEN
allowed_user_ids = 12345, 67890

[plex]
plex_url = http://192.168.0.121:32400
plex_token = PLEX_TOKEN

[host]
default_save_path = /path/to/downloads
movies_save_path = /path/to/movies
tv_shows_save_path = /path/to/tv

[search]
# JSON blobs (websites, preferences); see config.ini.template
```

Notes for agents:
- The `[search]` section embeds multi-line JSON for `websites` and `preferences`; **do not break JSON formatting**.
- Parsing is custom in `config.py::_parse_search_section`.
- `get_configuration()` creates missing save directories automatically.
- If `[search]` is missing/empty, search features are disabled.

## Commands & UX (what the bot exposes)

Commands are case-insensitive and work with or without a leading slash:
- `help` / `start` — Help and command list.
- `search` — Interactive Movie/TV flow (includes movie collection/franchise paths).
- `delete` — Guided deletion flow with confirmation.
- `status` — Plex connectivity check.
- `restart` — Plex service restart attempt.
- `links` — Shows known torrent/tracker source links.

Additional behavior:
- Sending a magnet link or URL triggers link ingestion, parsing/validation, and confirmation prompts.
- Workflow routing depends on `context.user_data["active_workflow"]`.

## Scraping & search (important behaviors)

- Search sources and preferences are configured in `config.ini` under `[search]`.
- The project uses both site-specific scrapers (`yts`, `tpb`, `1337x`, YAML-configured sites) and generic HTML strategies.
- Preserve consistent result shape/scoring behavior when adding or changing scrapers.

## Persistence & shutdown

- Active downloads and queues persist to **`persistence.json`** on shutdown and resume on startup via `post_init`/`post_shutdown`.
- Avoid storing non-JSON objects in persisted structures; state helpers strip known non-serializable runtime fields.

## Security checklist (please follow)

- **Do not commit secrets** (tokens, IDs, local paths that expose private infrastructure).
- Only allowlisted Telegram User IDs may interact (see `ALLOWED_USER_IDS` in bot data).
- Keep logs free of tokens/PII; logging is INFO-level with noisy `httpx` logs suppressed.
- If any credential-bearing file is discovered, remove it from VCS and rotate affected credentials.

## Conventions & gotchas

- Respect MarkdownV2/HTML parse modes; escaping errors break bot output.
- Clear or reset workflow state when canceling/completing multi-step flows.
- Size/format constraints are centralized:
  - `MAX_TORRENT_SIZE_GIB = 21` (`MAX_TORRENT_SIZE_GB` remains as compatibility alias).
  - `ALLOWED_EXTENSIONS = [".mkv", ".mp4"]`.

## What to do when adding features

1. Add or update tests (unit + workflow integration where relevant).
2. Keep new user-facing behavior inside workflows/handlers and business logic inside services.
3. Extend `config.ini` parsing only when necessary; preserve backward compatibility for existing keys.
4. Reuse utilities/UI helpers instead of duplicating parsing/formatting/retry logic.
5. Ensure `uv run pre-commit run --all-files` passes before submitting changes.

---
