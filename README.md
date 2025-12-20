# Plex-o-Tron Bot

This Telegram bot that automates the process of downloading torrents and organizing them for a Plex media server. It intelligently parses torrent names, enriches metadata from Wikipedia, renames files, and triggers Plex library scans automatically. It also provides tools for managing your library after downloads are complete.

## Core Features

*   **User Authorization**: Restricts bot access to a whitelist of Telegram User IDs.
*   **Interactive Search**: Find movies by title and year, or TV shows by title, season, and episode number, with guided prompts.
*   **Smart Content Parsing**: Automatically detects whether a download is a movie or a TV show when a link is provided.
*   **TV Show Metadata**: For TV shows, it scrapes Wikipedia to find the exact episode title.
*   **Plex-Friendly Naming**: Renames files to a clean, Plex-compatible format (e.g., `Show Name/Season 01/s01e01 - Episode Title.mkv`).
*   **Automated File Organization**: Moves completed movie and TV show downloads to their respective library folders.
*   **Plex Integration**: Automatically triggers a library-specific scan on the Plex Media Server after a download completes.
*   **Interactive Media Deletion**: Safely delete entire movies, TV shows, specific seasons, or individual episodes directly from the chat.
*   **Download Persistence**: Resumes any active downloads if the bot is restarted.
*   **Clean UI**: Deletes user commands and edits status messages in place to keep the chat tidy.

## Installation

This project is designed to run on Windows and Linux (Ubuntu/Debian).

### Prerequisites

*   **Python 3.12+**: Ensure Python is installed and in your PATH.
*   **uv**: We highly recommend using [uv](https://github.com/astral-sh/uv) for fast, reliable dependency management.
    *   *To install uv:* `curl -LsSf https://astral.sh/uv/install.sh | sh` (Linux/Mac) or via PowerShell (Windows).
*   **Git**: To clone the repository.

#### Telegram Bot Setup
1.  **Create a Bot**: Message [@BotFather](https://t.me/botfather) on Telegram and use the `/newbot` command. Follow the prompts to get your **Bot Token**.
2.  **Get Your User ID**: Message [@userinfobot](https://t.me/userinfobot) to get your numeric **User ID**. You will need this to authorize yourself as an admin of the bot.
3.  **Privacy Settings**: By default, bots cannot see messages in groups. If you plan to use this in a group, use `/setprivacy` in @BotFather to disable privacy mode (though direct messages are recommended for this bot).

### Linux Installation (Ubuntu/Debian)

We provide a comprehensive setup script that handles system dependencies, virtual environment creation, interactive configuration (with automated Plex discovery), and systemd service installation.

#### 1. Clone the Repository
```bash
git clone https://github.com/yourusername/plex-o-tron.git
cd plex-o-tron
```

#### 2. Run the Setup Script
```bash
# Make the script executable
chmod +x telegram_bot/utility_scripts/setup_bot.sh

# Run the full setup
./telegram_bot/utility_scripts/setup_bot.sh
```

The script will guide you through:
*   Installing system dependencies (`libtorrent`, `curl`, etc.).
*   Setting up permissions for automated Plex restarts.
*   Creating a Python virtual environment.
*   **Interactive Configuration**: Retrieving your Plex Token and Server URL automatically.
*   **Service Setup**: Installing the bot as a `systemd` service so it starts on boot.

#### 3. Manage the Service
If you chose to install the systemd service, you can manage it with:
```bash
sudo systemctl status telegram-bot
sudo systemctl restart telegram-bot
journalctl -u telegram-bot -f  # View live logs
```

---

### Windows Installation

We provide a PowerShell setup script that handles virtual environment creation, dependency installation, and interactive configuration (with automated Plex discovery).

#### 1. System Prerequisites
*   **Microsoft Visual C++ Redistributable**: The `libtorrent` library requires this. [Download the x64 version here](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170).

#### 2. Run the Setup Script
Open PowerShell in the project directory:

```powershell
.\telegram_bot\utility_scripts\setup_bot.ps1
```

The script will guide you through:
*   Creating a Python virtual environment.
*   Installing all project dependencies.
*   **Interactive Configuration**: Retrieving your Plex Token and Server URL automatically.

#### 3. Run the Bot
```powershell
uv run __main__.py
```

## Development Setup

Prepare your local environment before contributing by installing development dependencies and setting up pre-commit hooks:

```bash
uv pip sync pyproject.toml --extra dev
pre-commit install
pre-commit install --hook-type commit-msg --hook-type post-commit
```

Run the commands above prior to executing `pre-commit run --all-files` to ensure your changes meet the project's linting standards.

### Running Tests

After installing the `dev` extra, execute the suite locally via:

```bash
uv run pre-commit run --all-files
```

### Bot Commands

The bot supports the following commands (with or without a leading slash):

*   **help / start**: Displays a welcome message and lists all available commands.
*   **search**: Initiates an interactive workflow to find media.
    *   Prompts for "Movie" or "TV Show".
    *   **Movies**: Search by title and year. Supports **Collections** to download entire franchises (auto-detected via Wikipedia).
    *   **TV Shows**: Search by title, season, and episode.
    *   Presents the best matching torrents based on your preferences (resolution, codec, uploader).
*   **delete**: Initiates an interactive workflow to safely remove media from your library.
    *   Choose between Movie or TV Show.
    *   For TV Shows, you can delete an entire show, a specific season, or a single episode.
    *   Requires final confirmation before files are removed.
*   **status**: Checks and reports the connection status to your Plex Media Server.
*   **restart**: Restarts the Plex Media Server service using the configured OS-specific script.
*   **links**: Provides a list of popular torrent and tracker websites.

**Pro-tip**: You can also just paste a **magnet link** directly into the chat, and the bot will attempt to parse and download it automatically!
