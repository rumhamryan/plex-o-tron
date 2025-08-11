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

## System Configuration & Installation

This project is designed to run on both Windows or Linux (Ubuntu).
The setup for Windows is a manual process, follow these steps carefully.
For Linux, there is a setup script.

### Step 1: System Prerequisites

Before setting up the Python environment, ensure the necessary system-level dependencies are installed.

#### Python
*   **Python 3.12 or later** is required. It is assumed that you have Python installed and available in your system's PATH. You can verify this by running `python --version` or `python3 --version`.

#### C++ Dependencies (Crucial for `libtorrent`)
The `libtorrent` package is a Python wrapper around a powerful C++ library. For it to work, the underlying C++ components must be available on your system.

*   **On Windows**:
    *   The `libtorrent` Python package often relies on the **Microsoft Visual C++ Redistributable**.
    *   Many systems already have this installed. If you encounter errors during the `pip install` step related to missing DLLs (like `VCRUNTIME140.dll`), you will need to install it.
    *   You can download the latest version directly from Microsoft's website: [Latest supported Visual C++ Redistributable downloads](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170). Be sure to install the **x64** version.

*   **On Ubuntu / Debian**:
    *   The equivalent of the C++ dependency is provided by the `libtorrent-rasterbar` package. You must install this from the system's package manager **before** installing the Python packages.
    *   This command installs both the required runtime library and the development files needed by `pip`.
    *   Run the following in your terminal:
        ```bash
        # Update your package list
        sudo apt-get update

        # Install the libtorrent-rasterbar library and its development headers
        sudo apt-get install -y libtorrent-rasterbar-dev
        ```

### Step 2: Create and Activate a Virtual Environment

Using a virtual environment is highly recommended to isolate project dependencies.

1.  **Navigate to the project directory** in your terminal or command prompt.
2.  **Create the virtual environment**: `python3 -m venv venv`
3.  **Activate the virtual environment**:
    *   **Windows**: `.\venv\Scripts\activate`
    *   **Ubuntu / Debian**: `source venv/bin/activate`

### Step 3: Install Python Dependencies

With your virtual environment activated (and after completing Step 1), you can install all required Python packages with a single command.

```bash
pip install -r requirements.txt
```

### Step 4: Configure the Bot

Configuration is handled in the config.ini file.

1.  Create config.ini: If it doesn't exist, create it.
2.  Edit the file with your details:
```ini
[telegram]
# Get your bot token from @BotFather on Telegram
token = PLACE_TOKEN_HERE
# Get your numeric User ID from @userinfobot on Telegram
allowed_user_ids = 123456789

[plex]
# (Optional) Your Plex server URL and API token
plex_url = http://192.168.1.100:32400
plex_token = YOUR_PLEX_TOKEN_HERE

[host]
# Define absolute paths for your media. Use forward slashes for both OSes.
default_save_path = ~/Downloads
movies_save_path = /mnt/movies
tv_shows_save_path = /mnt/tv
```

### Step 5: Run the Bot

With your virtual environment active and configuration complete, start the bot:
```bash  
python __main__.py
```

To stop the bot, press `Ctrl+C`. Remember to reactivate the virtual environment `(source venv/bin/activate` or `.\venv\Scripts\activate`) every time you want to run the bot in a new terminal session.

### Bot Commands

    start - Displays a welcome message and links to torrent sites.
    help - Shows a brief help message with available commands.
    search - Initiates an interactive workflow to find media.
        Prompts for "Movie" or "TV Show".
        For movies, it asks for a title and year.
        For TV shows, it asks for a title, season number, and episode number.
        Presents the best matching torrents for you to download.
    plexstatus - Checks the connection to your Plex server.
    plexrestart - (Linux-only by default) Restarts the Plex Media Server service.
    delete - Initiates an interactive workflow to delete media from your library.
        Prompts for "Movie" or "TV Show".
        Asks for the title to search for.
        If a TV show is selected, it provides options to delete the whole show, a specific season, or a single episode.
        Requires final confirmation before any files are removed.