# Services Dependency Map

Purpose: Make service boundaries explicit so future refactors stay clean and predictable.

## Layers
1. Foundation: `config.py`, `utils.py`, `state.py`, `ui/messages.py`
2. Services: domain logic and integrations (see Service Map)
3. Workflows: user-facing orchestration (`telegram_bot/workflows/*`)
4. Handlers/UI: Telegram entry points (`telegram_bot/handlers/*`, `telegram_bot/ui/*`)

## Boundary Rules
- Handlers and UI may call workflows and services.
- Workflows may call services and helpers, but should not be imported by services.
- Services should not import handlers or UI. If UI formatting is needed, expose a helper in `telegram_bot/ui/messages.py`.
- Metadata scrapers should only be used by scraping/search services, never by handlers or workflows directly.

## Service Map
- `auth_service`: allowlist checks. Depends on `config` and Telegram types.
- `plex_service`: Plex API operations. Depends on `plexapi` and `config`.
- `scraping_service`: shared metadata lookup entry points. Depends on Wikipedia scraper helpers.
- `services/scrapers/wikipedia/*`: Wikipedia metadata scraping for movie/episode/collection details.
- `services/discovery/*`: provider-backed torrent discovery. Depends on Torznab/Prowlarr/Jackett-style APIs.
- `search_logic/*`: search orchestration and local media discovery helpers. Torrent search delegates to `services/discovery`.
- `torrent_service/*`: magnet/torrent intake. Depends on `media_manager` for parsing helpers.
- `media_manager/*`: naming, validation, file moves, Plex scan trigger. Depends on `scraping_service` (episode titles) and `plex_service` helpers.
- `download_manager/*`: queueing and progress for torrents. Depends on `media_manager`, `plex_service`, `services/types`, and `state`.

## Known Exceptions
- `download_manager` currently imports `telegram_bot.workflows.finalize_movie_collection`. This is a workflow dependency from a service and should be removed during a future refactor.

## Cycles to Avoid
- `workflows` importing `handlers` or `ui/views`.
- `services` importing `workflows` (except the known exception above).
- `scrapers` importing `handlers` or `workflows`.
