#!/bin/bash

# This script automates the full environment setup, configuration, and optional
# service deployment for the Plex-o-Tron Bot on a Debian-based Linux system.
#
# IT ASSUMES:
# 1. You are running this script from the root of the project directory.
# 2. Python 3.12 is available on your system.

# --- Configuration ---
PROJECT_DIR=$(pwd)
CURRENT_USER=$(whoami)

# --- Colors for better output ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

# Exit immediately if a command exits with a non-zero status.
set -e

echo -e "${GREEN}--- Starting Plex-o-Tron Bot Full Setup ---${NC}"

# --- Helper function for configuration input ---
get_input() {
    local prompt="$1"
    local default="$2"
    local val

    if [ -n "$default" ]; then
        read -p "$(echo -e "${GREEN}$prompt${NC} [$default]: ")" val
        echo "${val:-$default}"
    else
        while true; do
            read -p "$(echo -e "${GREEN}$prompt${NC}: ")" val
            if [ -n "$val" ]; then
                echo "$val"
                break
            fi
            echo -e "${RED}This field is required.${NC}" >&2
        done
    fi
}

get_plex_token() {
    echo -e "\n${CYAN}--- Plex Authentication Flow ---${NC}" >&2
    echo "We will now obtain a Plex Token by logging you in via your browser." >&2

    if ! command -v curl &> /dev/null; then
        echo -e "${RED}Error: 'curl' is not installed. Cannot perform automatic login.${NC}" >&2
        return 1
    fi

    local CLIENT_ID
    CLIENT_ID=$(cat /proc/sys/kernel/random/uuid 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())' 2>/dev/null || echo "plex-o-tron-$(date +%s)")
    local PRODUCT="Plex Telegram Bot Setup"
    local PINS_URL="https://plex.tv/api/v2/pins"

    echo -n "Requesting login PIN..." >&2
    local RESPONSE
    RESPONSE=$(curl -s -X POST "$PINS_URL" \
        -H "X-Plex-Product: $PRODUCT" \
        -H "X-Plex-Version: 1.0" \
        -H "X-Plex-Platform: Linux" \
        -H "X-Plex-Client-Identifier: $CLIENT_ID" \
        -H "Accept: application/json" \
        -d "strong=true")

    local PIN_ID=$(echo "$RESPONSE" | grep -o '"id":[0-9]*' | cut -d: -f2)
    local PIN_CODE=$(echo "$RESPONSE" | grep -o '"code":"[^" ]*"' | cut -d\" -f4)

    if [ -z "$PIN_ID" ] || [ -z "$PIN_CODE" ]; then
        echo -e "${RED} Failed to get PIN from Plex.${NC}" >&2
        return 1
    fi
    echo -e "${GREEN} Done.${NC}" >&2

    local AUTH_URL="https://app.plex.tv/auth#?context%5Bdevice%5D%5Bproduct%5D=$(echo "$PRODUCT" | sed 's/ /%20/g')&clientID=$CLIENT_ID&code=$PIN_CODE"

    echo -e "\nPlease open the following URL in your web browser to authorize the app:" >&2
    echo -e "\n    ${YELLOW}$AUTH_URL${NC}\n" >&2

    # Try to open browser
    if command -v xdg-open &> /dev/null; then
        xdg-open "$AUTH_URL" &> /dev/null
    elif command -v open &> /dev/null; then
        open "$AUTH_URL" &> /dev/null
    fi

    echo "Waiting for authorization (timeout in 3 minutes)..." >&2

    local START_TIME=$(date +%s)
    while [ $(($(date +%s) - START_TIME)) -lt 180 ]; do
        sleep 5
        local CHECK_RESPONSE
        CHECK_RESPONSE=$(curl -s -X GET "$PINS_URL/$PIN_ID" \
            -H "X-Plex-Product: $PRODUCT" \
            -H "X-Plex-Version: 1.0" \
            -H "X-Plex-Platform: Linux" \
            -H "X-Plex-Client-Identifier: $CLIENT_ID" \
            -H "Accept: application/json")

        local TOKEN=$(echo "$CHECK_RESPONSE" | grep -o '"authToken":"[^" ]*"' | cut -d\" -f4)

        if [ -n "$TOKEN" ]; then
            echo -e "\n${GREEN}Success! Token retrieved.${NC}" >&2
            echo "$TOKEN"
            return 0
        fi
        echo -n "." >&2
    done

    echo -e "\n${RED}Timed out waiting for authentication.${NC}" >&2
    return 1
}

discover_plex_url() {
    local token="$1"
    echo -e "\n${CYAN}Searching for Plex servers associated with your account...${NC}" >&2

    local CLIENT_ID
    CLIENT_ID=$(cat /proc/sys/kernel/random/uuid 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())' 2>/dev/null || echo "plex-o-tron-$(date +%s)")
    local PRODUCT="Plex Telegram Bot Setup"

    local RESOURCES
    RESOURCES=$(curl -s -X GET "https://plex.tv/api/v2/resources?includeHttps=1" \
        -H "X-Plex-Token: $token" \
        -H "X-Plex-Client-Identifier: $CLIENT_ID" \
        -H "Accept: application/json")

    local SERVER_INFO
    SERVER_INFO=$(echo "$RESOURCES" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    servers = [r for r in data if 'server' in r.get('provides', '')]
    if not servers:
        print('NONE')
        sys.exit(0)
    for i, s in enumerate(servers):
        conns = s.get('connections', [])
        local_uri = next((c['uri'] for c in conns if c.get('local')), None)
        remote_uri = next((c['uri'] for c in conns if not c.get('local')), None)
        best_uri = local_uri if local_uri else (remote_uri if remote_uri else '')
        print(f\"{i}|{s['name']}|{best_uri}\")
except Exception:
    print('ERROR')
")

    if [ "$SERVER_INFO" = "NONE" ]; then
        echo -e "${YELLOW}No Plex servers found on your account.${NC}" >&2
        return 1
    elif [ "$SERVER_INFO" = "ERROR" ]; then
        echo -e "${RED}Error parsing server list.${NC}" >&2
        return 1
    fi

    local IFS=$'\n'
    local servers=($SERVER_INFO)
    local selected_server=""

    if [ ${#servers[@]} -eq 1 ]; then
        selected_server="${servers[0]}"
        local s_name=$(echo "$selected_server" | cut -d'|' -f2)
        echo -e "${GREEN}Found server: $s_name${NC}" >&2
    else
        echo "Multiple servers found. Please choose one:" >&2
        local i=0
        for s in "${servers[@]}"; do
            local name=$(echo "$s" | cut -d'|' -f2)
            echo "[$i] $name" >&2
            i=$((i+1))
        done
        read -p "Enter selection number [0]: " choice < /dev/tty
        choice=${choice:-0}
        selected_server="${servers[$choice]}"
    fi

    local default_url=$(echo "$selected_server" | cut -d'|' -f3)
    echo -e "\nDetected connection URL: ${CYAN}$default_url${NC}" >&2
    read -p "Confirm or enter Plex URL [$default_url]: " final_url < /dev/tty
    echo "${final_url:-$default_url}"
}

# --- Step 1: Install System Dependencies ---
echo -e "\n${YELLOW}Step 1: Installing required system packages (libtorrent)...${NC}"
sudo apt-get update
sudo apt-get install -y libtorrent-rasterbar-dev curl python3-venv git
echo -e "${GREEN}System packages installed successfully.${NC}"


# --- Step 2: Set Permissions for the Plex Restart Script ---
echo -e "\n${YELLOW}Step 2: Locating and setting permissions for 'restart_plex.sh'...${NC}"
# Use the script from its new location in the project
RESTART_SCRIPT_PATH="$PROJECT_DIR/telegram_bot/utility_scripts/restart_plex.sh"
if [ -f "$RESTART_SCRIPT_PATH" ]; then
    chmod +x "$RESTART_SCRIPT_PATH"
    echo "Made 'restart_plex.sh' executable."
else
    echo -e "${RED}ERROR: The 'restart_plex.sh' script was not found at $RESTART_SCRIPT_PATH.${NC}"
    exit 1
fi


# --- Step 3: Configure Sudoers for Passwordless Restart ---
echo -e "\n${YELLOW}Step 3: Configuring sudoers for passwordless Plex restart...${NC}"
SUDOERS_FILE_PATH="/etc/sudoers.d/99-plex-restart"
SUDOERS_RULE="$CURRENT_USER ALL=(ALL) NOPASSWD: $RESTART_SCRIPT_PATH"
echo "This will create a sudoers rule for user '$CURRENT_USER'."
echo "$SUDOERS_RULE" | sudo tee "$SUDOERS_FILE_PATH" > /dev/null
sudo chmod 0440 "$SUDOERS_FILE_PATH"
echo -e "${GREEN}Sudoers rule created successfully.${NC}"


# --- Step 4: Setup Python Virtual Environment and Install Dependencies ---
echo -e "\n${YELLOW}Step 4: Setting up Python virtual environment...${NC}"

# Ensure uv is installed (CRITICAL DEPENDENCY)
if ! command -v uv &> /dev/null; then
    echo -e "${YELLOW}uv not found. Installing uv...${NC}"
    # Try the official shell installer first
    if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
        echo -e "${YELLOW}Primary installer failed (likely server timeout). Trying pip fallback...${NC}"
        # Fallback to pip installation
        if ! pip3 install uv --user &> /dev/null && ! pip install uv --user &> /dev/null; then
            echo -e "${RED}Error: uv installation failed via all methods.${NC}"
            echo "Please install uv manually: https://github.com/astral-sh/uv"
            exit 1
        fi
    fi

    # Update PATH for the current session to include common installation locations
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

    if ! command -v uv &> /dev/null; then
        echo -e "${RED}Error: uv installed but not found in PATH.${NC}"
        echo "Please add uv to your PATH manually and restart the script."
        exit 1
    fi
    echo -e "${GREEN}uv installed successfully.${NC}"
fi

echo "Using uv for dependency management..."

# Try to find a system python 3.12 or 3.13 first to avoid external downloads if possible
SYS_PYTHON=$(command -v python3.13 || command -v python3.12 || echo "")

if [ -n "$SYS_PYTHON" ]; then
    echo -e "${CYAN}Found system Python at $SYS_PYTHON. Using it for venv...${NC}"
    uv venv --python "$SYS_PYTHON"
else
    echo "No system Python 3.12+ found. uv will attempt to download a standalone version..."
    if ! uv venv --python 3.12; then
        echo -e "${RED}Error: uv failed to create a virtual environment.${NC}"
        echo "This usually happens if the Python download from GitHub timed out (504)."
        echo -e "\n${YELLOW}To fix this, please install Python 3.12 on your system manually:${NC}"
        echo "  sudo add-apt-repository ppa:deadsnakes/ppa"
        echo "  sudo apt update"
        echo "  sudo apt install python3.12 python3.12-venv"
        echo -e "\nThen run this setup script again."
        exit 1
    fi
fi

source .venv/bin/activate
uv pip sync pyproject.toml

echo -e "${GREEN}Virtual environment created and dependencies installed.${NC}"


# --- Step 5: Interactive Configuration ---
echo -e "\n${YELLOW}Step 5: Configuring the Bot...${NC}"
if [ -f "config.ini" ]; then
    read -p "config.ini already exists. Re-configure? (y/n): " reconfig
else
    reconfig="y"
fi

if [[ "$reconfig" =~ ^[Yy]$ ]]; then
    echo -e "\n${CYAN}[Telegram Settings]${NC}"
    BOT_TOKEN=$(get_input "Enter your Telegram Bot Token")
    ALLOWED_IDS=$(get_input "Enter allowed User IDs (comma separated)")

    echo -e "\n${CYAN}[Plex Settings]${NC}"
    read -p "Do you want to automatically retrieve your Plex Token and Server URL? (y/n) [y]: " AUTO_PLEX
    if [[ "$AUTO_PLEX" != "n" ]]; then
        PLEX_TOKEN_FULL=$(get_plex_token)
        PLEX_TOKEN=$(echo "$PLEX_TOKEN_FULL" | tail -n 1)
        if [ -n "$PLEX_TOKEN" ] && [[ "$PLEX_TOKEN" != ".*" ]] && [[ "$PLEX_TOKEN" != *" "* ]]; then
            PLEX_URL_FULL=$(discover_plex_url "$PLEX_TOKEN")
            PLEX_URL=$(echo "$PLEX_URL_FULL" | tail -n 1)
        fi
    fi

    if [ -z "$PLEX_TOKEN" ]; then
        PLEX_TOKEN=$(get_input "Enter your Plex Authentication Token" "")
    fi
    if [ -z "$PLEX_URL" ]; then
        PLEX_URL=$(get_input "Enter your Plex Server URL" "http://localhost:32400")
    fi

    echo -e "\n${CYAN}[Host Paths]${NC}"
    DEFAULT_SAVE_PATH=$(get_input "Download Path (Unsorted)" "$HOME/Downloads/Unsorted")
    MOVIES_SAVE_PATH=$(get_input "Movies Library Path" "$HOME/Media/Movies")
    TV_SHOWS_SAVE_PATH=$(get_input "TV Shows Library Path" "$HOME/Media/TV")

    cat > config.ini <<EOF
# file: config.ini

[telegram]
bot_token = $BOT_TOKEN
allowed_user_ids = $ALLOWED_IDS

[plex]
plex_url = $PLEX_URL
plex_token = $PLEX_TOKEN

[host]
default_save_path = $DEFAULT_SAVE_PATH
movies_save_path = $MOVIES_SAVE_PATH
tv_shows_save_path = $TV_SHOWS_SAVE_PATH

[search]
websites = {
    "movies": [
        {"name": "YTS.lt", "search_url": "https://yts.lt/browse-movies/{query}/{quality}/all/0/{year}/all"},
        {"name": "tpb", "search_url": "https://thepiratebay.org/search.php?q={query}&cat=0"}
    ],
    "tv": [
        {"name": "eztvx.to", "search_url": "https://eztvx.to/search/{query}"},
        {"name": "tpb", "search_url": "https://thepiratebay.org/search.php?q={query}&cat=0"}
    ]
}
preferences = {
    "movies": {
        "resolutions": {
            "4k": 5, "2160p": 5, "1080p": 3, "720p": 1
        },
        "codecs": {
            "x265": 2, "hevc": 2, "x264": 1, "h264": 1
        },
        "uploaders": {
            "mazemaze16": 5, "QxR": 5
        }
    },
    "tv": {
        "resolutions": {
            "1080p": 5, "720p": 2
        },
        "codecs": {
            "x265": 2, "hevc": 2, "x264": 1
        },
        "uploaders": {
            "EZTV": 5, "MeGusta": 5
        }
    }
}
EOF
    echo -e "${GREEN}Configuration saved to config.ini.${NC}"
fi


# --- Step 6: Optional Systemd Service Setup ---
echo -e "\n${YELLOW}Step 6: Set up the bot as a systemd service?${NC}"
read -p "This will make the bot start on boot and restart if it fails. (y/n): " wants_service

if [[ "$wants_service" =~ ^[Yy]$ ]]; then
    echo -e "\n${CYAN}Creating systemd service file...${NC}"
    SERVICE_FILE_PATH="/etc/systemd/system/telegram-bot.service"

    sudo tee "$SERVICE_FILE_PATH" > /dev/null <<EOF
[Unit]
Description=Telegram Bot for Plex Torrent Automation
After=network.target

[Service]
User=$CURRENT_USER
Group=$CURRENT_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/.venv/bin/python $PROJECT_DIR/__main__.py
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF

    echo "Service file created at $SERVICE_FILE_PATH"

    echo -e "\n${CYAN}Enabling and starting the service...${NC}"
    sudo systemctl daemon-reload
    sudo systemctl enable telegram-bot.service
    sudo systemctl start telegram-bot.service

    echo -e "\n${GREEN}--- Setup Complete! ---${NC}"
    echo "The bot is now running as a background service."
    echo -e "Use ${YELLOW}'sudo systemctl status telegram-bot.service'${NC} to check its status."
else
    echo -e "\n${GREEN}--- Setup Complete! ---${NC}"
    echo "You chose not to set up the systemd service."
    echo -e "\n${YELLOW}To run the bot manually, use:${NC}"
    echo -e "${CYAN}source .venv/bin/activate && python __main__.py${NC}"
fi
