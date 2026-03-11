# Services Overview

This folder contains the core service layer for the bot. Services encapsulate
domain logic and integrations (torrenting, scraping, Plex, downloads) and are
designed to be called from handlers and workflows.

## Goals
- Keep services cohesive and readable.
- Minimize cross-service coupling.
- Prefer package-level imports over internal modules.

## Dependency Direction
- Handlers and workflows call services.
- Services may call other services, but avoid cycles.
- Utilities, config, and state are leaf dependencies.
- Avoid service-to-workflow imports except for existing exceptions.

## Service Map
- `scrapers/`: Site-specific scraping plus generic HTML/YAML-based scrapers.
- `search_logic/`: Search orchestration and local media discovery helpers.
- `torrent_service/`: Magnet/torrent URL handling and metadata retrieval.
- `download_manager/`: Download orchestration, queueing, progress, and cleanup.
- `media_manager/`: File parsing, naming, and post-download organization.
- `plex_service.py`: Plex connectivity and collection management.
- `auth_service.py`: Authentication and allowlist validation.

## Shared State Conventions
- `bot_data["TORRENT_SESSION"]`: Libtorrent session.
- `bot_data["active_downloads"]`: Active download task state by chat ID.
- `bot_data["download_queues"]`: Pending download queue by chat ID.
- `bot_data["DOWNLOAD_BATCHES"]`: Batch metadata for multi-episode/movie flows.
- `bot_data["SAVE_PATHS"]`: Resolved download destinations.

## Public API Convention
- Import from the package top-level (for example, `telegram_bot.services.download_manager`)
  instead of internal modules to keep refactors isolated.
