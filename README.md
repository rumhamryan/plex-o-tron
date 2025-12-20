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

---

### Linux Installation (Ubuntu/Debian)

This guide covers setting up the bot on a Linux server, including system dependencies, virtual environment, and configuring it as a systemd service for auto-restart.

#### 1. Install System Dependencies
Update your package list and install the required build tools and the `libtorrent` C++ library headers.

```bash
sudo apt update
sudo apt install -y git python3-venv libtorrent-rasterbar-dev
```

#### 2. Clone the Repository
Navigate to your desired install directory (e.g., `/opt` or your home folder) and clone the project.

```bash
git clone https://github.com/yourusername/plex-o-tron.git
cd plex-o-tron
```

#### 3. Set Up Python Environment
Create a virtual environment and install the dependencies using `uv`.

```bash
# Create the virtual environment
uv venv

# Activate the virtual environment
source .venv/bin/activate

# Install project dependencies
uv pip sync pyproject.toml
```

#### 4. Configure the Bot
You can use the provided helper script to interactively generate your `config.ini` file.

**Option A: Use the Setup Script (Recommended)**
```bash
# Make the script executable first
chmod +x telegram_bot/utility_scripts/setup_config.sh

# Run the interactive setup
./telegram_bot/utility_scripts/setup_config.sh
```

**Option B: Manual Configuration**
Create the configuration file from the template and edit it with your details.
```bash
cp config.ini.template config.ini
nano config.ini
```
*   **[telegram]**: Add your Bot Token and allowed User IDs.
*   **[plex]**: (Optional) Add your Plex URL (e.g., `http://localhost:32400`) and Token.
*   **[host]**: Set the absolute paths where downloads should go (e.g., `/mnt/media/downloads` and `/mnt/media/movies`).

#### 5. Set Up Plex Restart (Optional)
The bot can restart the Plex Media Server service if you grant it permission.

1.  **Make the script executable**:
    ```bash
    chmod +x telegram_bot/utility_scripts/restart_plex.sh
    ```
2.  **Configure sudoers**:
    Allow your user (e.g., `ubuntu`) to run this specific script as root without a password.
    ```bash
    sudo visudo -f /etc/sudoers.d/99-plex-bot-restart
    ```
    Add the following line (replace `ubuntu` with your username and fix the path):
    ```text
    ubuntu ALL=(ALL) NOPASSWD: /home/ubuntu/plex-o-tron/telegram_bot/utility_scripts/restart_plex.sh
    ```
3.  **Save and exit**.

#### 6. Run as a System Service (systemd)
To keep the bot running in the background and start on boot, create a systemd service.

1.  **Create the service file**:
    ```bash
    sudo nano /etc/systemd/system/plex-bot.service
    ```
2.  **Paste the following configuration** (update `User`, `WorkingDirectory`, and `ExecStart` paths):
    ```ini
    [Unit]
    Description=Plex-o-Tron Telegram Bot
    After=network.target

    [Service]
    Type=simple
    User=ubuntu
    WorkingDirectory=/home/ubuntu/plex-o-tron
    # Point to the python executable inside your venv
    ExecStart=/home/ubuntu/plex-o-tron/.venv/bin/python __main__.py
    Restart=on-failure
    RestartSec=10

    [Install]
    WantedBy=multi-user.target
    ```
3.  **Enable and Start**:
    ```bash
    sudo systemctl daemon-reload
    sudo systemctl enable plex-bot
    sudo systemctl start plex-bot
    ```
4.  **Check Status**:
    ```bash
    sudo systemctl status plex-bot
    ```

---

### Windows Installation

#### 1. System Prerequisites
*   **Microsoft Visual C++ Redistributable**: The `libtorrent` library requires this. [Download the x64 version here](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170).

#### 2. Create Virtual Environment
Open PowerShell in the project directory:

```powershell
uv venv
.\.venv\Scripts\activate
```

#### 3. Install Dependencies

```powershell
uv pip sync pyproject.toml
```

#### 4. Configure
Run the setup script to interactively generate your configuration:

```powershell
.\telegram_bot\utility_scripts\setup_config.ps1
```

Alternatively, copy `config.ini.template` to `config.ini` and edit it manually. Ensure paths use strictly forward slashes `/` or escaped backslashes `\\`.

#### 5. Run
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
uv run pytest
```

The `dev` group bundles tooling such as `pytest-mock`, which provides the ubiquitous `mocker` fixture. If you previously synced without `--extra dev`, rerun the sync command above so local pytest matches the configuration used by pre-commit.

## Changelog-Driven Commits

The repository keeps a running `Changelog.md`. The top-most entry (right after the example block) should describe the pending work and uses the format shown in the file with a `Commit: <pending>` header.

To streamline this workflow:

1. Install the existing pre-commit hooks (`pre-commit install && pre-commit install --hook-type commit-msg --hook-type post-commit`) so Git executes the custom `commit-msg` and `post-commit` stages.
2. Edit `Changelog.md` before committing. Leave the header as `Commit: <pending>` and describe every touched path underneath.
3. Run `git commit` **without** `-m`. The `commit-msg` hook copies that changelog entry into the commit message automatically.
4. After the commit finishes, the `post-commit` hook swaps `<pending>` with the real commit hash. That change is left unstaged so you can review and include it as part of your next commit.

If you prefer a custom commit message (e.g., `git commit -m "Hotfix"`), the hook detects the pre-populated text and leaves it untouched.




### Bot Commands

    start - Displays a welcome message and links to torrent sites.
    help - Shows a brief help message with available commands.
    search - Initiates an interactive workflow to find media.
        Prompts for "Movie" or "TV Show".
        For movies, it asks for a title and year.
        A dedicated "Collection" path lets you download an entire franchise in one run.
        Collection runs auto-detect the franchise via Wikipedia, let you deselect titles, and queue matching torrents into `<movies>/<Franchise>/<Movie Title (Year)>/`.
        For TV shows, it asks for a title, season number, and episode number.
        Presents the best matching torrents for you to download.
    plexstatus - Checks the connection to your Plex server.
    plexrestart - (Linux-only by default) Restarts the Plex Media Server service.
    delete - Initiates an interactive workflow to delete media from your library.
        Prompts for "Movie" or "TV Show".
        Asks for the title to search for.
        If a TV show is selected, it provides options to delete the whole show, a specific season, or a single episode.
        Requires final confirmation before any files are removed.
