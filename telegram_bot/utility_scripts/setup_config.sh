#!/bin/bash

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'


echo -e "${CYAN}--- Plex-o-Tron Bot Configuration Setup ---${NC}"
echo "This script will generate a config.ini file for you."

# Helper function to get input
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
    local PIN_CODE=$(echo "$RESPONSE" | grep -o '"code":"[^"]*"' | cut -d\" -f4)

    if [ -z "$PIN_ID" ] || [ -z "$PIN_CODE" ]; then
        echo -e "${RED} Failed to get PIN from Plex.${NC}" >&2
        return 1
    fi
    echo -e "${GREEN} Done.${NC}" >&2

    local AUTH_URL="https://app.plex.tv/auth#?context%5Bdevice%5D%5Bproduct%5D=$(echo "$PRODUCT" | sed 's/ /%20/g')&clientID=$CLIENT_ID&code=$PIN_CODE"

    echo -e "\nPlease open the following URL in your web browser to authorize the app:" >&2
    echo -e "\n    ${YELLOW}$AUTH_URL${NC}\n" >&2

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

        local TOKEN=$(echo "$CHECK_RESPONSE" | grep -o '"authToken":"[^"]*"' | cut -d\" -f4)

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

# 1. Telegram Configuration
echo -e "\n${YELLOW}[Telegram Settings]${NC}"
BOT_TOKEN=$(get_input "Enter your Telegram Bot Token")
ALLOWED_IDS=$(get_input "Enter allowed User IDs (comma separated)")

# 2. Plex Configuration
echo -e "\n${YELLOW}[Plex Settings]${NC}"

PLEX_URL=""
PLEX_TOKEN=""

read -p "Do you want to automatically retrieve your Plex Token and Server URL? (y/n) [y]: " AUTO_PLEX
if [ "$AUTO_PLEX" != "n" ]; then
    PLEX_TOKEN=$(get_plex_token)
    if [ $? -eq 0 ]; then
        PLEX_URL=$(discover_plex_url "$PLEX_TOKEN")
        if [ $? -ne 0 ]; then
            PLEX_URL=$(get_input "Enter your Plex Server URL" "http://localhost:32400")
        fi
    else
        echo -e "${YELLOW}Falling back to manual entry.${NC}"
        PLEX_TOKEN=$(get_input "Enter your Plex Authentication Token" "")
        PLEX_URL=$(get_input "Enter your Plex Server URL" "http://localhost:32400")
    fi
else
    PLEX_TOKEN=$(get_input "Enter your Plex Authentication Token" "")
    PLEX_URL=$(get_input "Enter your Plex Server URL" "http://localhost:32400")
fi

# 3. Host/Path Configuration
echo -e "\n${YELLOW}[Host Paths]${NC}"
DEFAULT_DL="$HOME/Downloads/Unsorted"
DEFAULT_MOV="$HOME/Media/Movies"
DEFAULT_TV="$HOME/Media/TV"

DEFAULT_SAVE_PATH=$(get_input "Download Path (Unsorted)" "$DEFAULT_DL")
MOVIES_SAVE_PATH=$(get_input "Movies Library Path" "$DEFAULT_MOV")
TV_SHOWS_SAVE_PATH=$(get_input "TV Shows Library Path" "$DEFAULT_TV")

# 4. Generate Content
cat > config.ini.tmp <<EOF
# file: config.ini

[telegram]
# Get your bot token from @BotFather on Telegram
bot_token = $BOT_TOKEN
# Get your numeric User ID from @userinfobot on Telegram
allowed_user_ids = $ALLOWED_IDS

[plex]
# (Optional) Your Plex server URL and API token
plex_url = $PLEX_URL
plex_token = $PLEX_TOKEN

[host]
# Define absolute paths for your media. Use forward slashes for both OSes.
default_save_path = $DEFAULT_SAVE_PATH
movies_save_path = $MOVIES_SAVE_PATH
tv_shows_save_path = $TV_SHOWS_SAVE_PATH

[search]
websites = {
    "movies": [
        {"name": "YTS.lt", "search_url": "https://yts.lt/browse-movies/{query}/{quality}/all/0/latest/{year}/all"},
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
            "4k": 5,
            "2160p": 5,
            "1080p": 3,
            "720p": 1
        },
        "codecs": {
            "x265": 5,
            "hevc": 5,
            "x264": 1,
            "h264": 1
        },
        "uploaders": {
            "mazemaze16": 5,
            "QxR": 5
        }
    },
    "tv": {
        "resolutions": {
            "1080p": 5,
            "720p": 2
        },
        "codecs": {
            "x265": 5,
            "hevc": 5,
            "x264": 1
        },
        "uploaders": {
            "EZTV": 5,
            "MeGusta": 5
        }
    }
}
EOF

# 5. Write File
OUTPUT_PATH="./config.ini"

if [ -f "$OUTPUT_PATH" ]; then
    read -p "File '$OUTPUT_PATH' already exists. Overwrite? (y/n) [n]: " OVERWRITE
    if [ "$OVERWRITE" != "y" ]; then
        echo -e "${YELLOW}Operation cancelled. No changes made.${NC}"
        rm config.ini.tmp
        exit 0
    fi
fi

mv config.ini.tmp "$OUTPUT_PATH"
echo -e "\n${GREEN}Success! Configuration written to: $(realpath "$OUTPUT_PATH")${NC}"
echo "You can now run the bot using: uv run __main__.py"
