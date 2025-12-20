# AGENTS.md

> A concise, agent-oriented guide to hacking on this project.
> Format reference: AGENTS.md open format.

SITUATION: You are an expert Python developer tasked with optimizing and maintaining a generic web scraper
CHALLENGE: Implement based on INSTRUCTIONS.md if it exists, otherwise from the user prompt that prioritizes maintainability and readability
AUDIENCE: Developers who will maintain this code in the future, including those who were not involved in initial development
FORMAT:
- Use clear, descriptive naming conventions
- Include meaningful comments explaining the "why" behind complex logic
- Follow the principle of least surprise
- Implement appropriate error handling with informative messages
- Organize code with logical separation of concerns
FOUNDATIONS:
- Prioritize readability over clever optimizations
- Include comprehensive input validation
- Implement proper exception handling
- Use consistent patterns throughout
- Create modular components with clear responsibilities
- Include unit tests that document expected behavior

## Project snapshot

- Name: **plex-o-tron-bot** (Telegram bot that searches, downloads, organizes media for Plex).
- Primary entrypoint: 'telegram_bot/__main__.py' (registers handlers, loads config, starts polling).
- Python: **3.12**.
- Key libs: 'python-telegram-bot', 'libtorrent', 'beautifulsoup4', 'httpx', 'plexapi', 'thefuzz'.

## Setup commands

```bash
# 1) Create venv with uv & activate (POSIX)
uv venv && source .venv/bin/activate
# (Windows)
uv venv && .\.venv\Scripts\activate

# 2) System deps (Linux, for libtorrent)
sudo apt-get update && sudo apt-get install -y libtorrent-rasterbar-dev

# 3) Sync dependencies from lockfile
uv pip sync pyproject.toml --extra dev

# 4) Install pre-commit
pre-commit install
```

Linux libtorrent headers are required for the Python package to function.

## Run / Dev

```bash
# Start the bot
uv run __main__.py
```

- The bot reads **'config.ini'** (see 'Configuration'). It will exit if required fields are missing.
- Handlers are registered in 'register_handlers' and polling begins.

## Tests

```bash
uv run pre-commit run --all-files
```
- Code quality hooks and unit tests.
- Unit tests cover handlers, services (download, scraping, torrent, Plex), workflows, and utilities.

## Code style & tooling

- **Black** + **Ruff** (line length 88).
- **Pyright** configured for Python 3.12.
- **pre-commit** is used to enforce style and quality. Hooks are configured for tools like black, ruff, and pytest.
- All pre-commit hooks must pass before code is submitted.

## Project structure (agent-relevant)

- 'telegram_bot/config.py' — Loads/validates config, creates save paths, logging, constants. **Do not** hardcode secrets.
- 'telegram_bot/state.py' — Persists/resumes active & queued downloads to 'persistence.json'. Avoid serializing non-JSON objects; helper does this already.
- 'telegram_bot/handlers/' — Commands ('help', 'search', 'status', 'restart', 'delete'), callbacks, errors, message routing. Keep UI text minimal and use MarkdownV2/HTML as coded.
- 'telegram_bot/services/' — Auth, torrent/download/media/plex/scraping/search logic. Maintain separation: parsing/scraping vs. orchestration vs. UI.
- 'telegram_bot/utils.py' — Helpers (byte formatting, safe message editing, torrent name parsing). Reuse instead of duplicating.

## Configuration

Create **'config.ini'** (values shown here are placeholders):

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
# JSON blobs (websites, preferences); see template
```

Notes for agents:
- The '[search]' section embeds multi-line JSON for 'websites' and 'preferences'; **do not break the JSON**. Parsing is custom in 'config.py::_parse_search_section'.
- 'get_configuration()' will **mkdir** any missing save paths.

## Commands & UX (what the bot exposes)

- '/search' → interactive flow: choose Movie/TV → collect fields → present scored results.
- Sending a **magnet/URL** triggers link ingestion → parse/validate torrent → confirmation prompt.
- '/status' checks Plex connectivity. '/delete' opens guided deletion flow (movie / tv / season / episode) with confirmation.

## Scraping & search (important behaviors)

- Sites & preferences are configured in 'config.ini' under '[search]':
  - Example sites: YTS (movies JSON API), 1337x/EZTV (HTML).
- The codebase includes a **generic HTML scraping path** (strategies for tables/contextual search) and dedicated scrapers (e.g., YTS via JSON). Preserve consistent result shape and scoring.

## Persistence & shutdown

- Active downloads and queues persist to **'persistence.json'** on shutdown and resume on startup via 'post_init'. Avoid storing unserializable objects; helpers strip them.

## Security checklist (please follow)

- **Do not commit secrets.** This repo currently includes a 'secrets.txt' with real tokens — **remove from VCS and rotate** all exposed tokens immediately.
- Only allowlisted Telegram User IDs may interact (see 'ALLOWED_USER_IDS').
- Keep logs free of tokens/PII; current logging is INFO-level and suppresses 'httpx' noise.

## Conventions & gotchas

- Prefer **MarkdownV2** or **HTML** exactly as implemented for messages; escaping matters.
- Handler routing depends on 'context.user_data["active_workflow"]' being set ('search', 'delete'). Don’t forget to clear/cancel flows appropriately.
- Large downloads: 'MAX_TORRENT_SIZE_GB = 10'. Respect 'ALLOWED_EXTENSIONS'.

## What to do when adding features

1. Add tests (look at existing test modules for patterns).
2. Keep configurations in 'config.ini' (extend parser if you add new JSON blobs).
3. Reuse utilities and UI helpers; don’t duplicate parsing or formatting.
4. Ensure all pre-commit hooks pass before submitting your changes.

---
