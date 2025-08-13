#!/bin/bash

# This script automates the full environment setup and optional service
# deployment for the Plex Torrent Telegram Bot on a Debian-based Linux system.
#
# IT ASSUMES:
# 1. You are running this script from the root of the project directory.
# 2. Python 3.12 and the 'venv' module are already installed.
# 3. 'restart_plex.sh' is present in the project directory.

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

echo -e "${GREEN}--- Starting Bot Environment Setup ---${NC}"

# --- Step 1: Install System Dependencies ---
echo -e "\n${YELLOW}Step 1: Installing required system packages (libtorrent)...${NC}"
sudo apt-get update
sudo apt-get install -y libtorrent-rasterbar-dev
echo -e "${GREEN}System packages installed successfully.${NC}"


# --- Step 2: Set Permissions for the Plex Restart Script ---
echo -e "\n${YELLOW}Step 2: Locating and setting permissions for 'restart_plex.sh'...${NC}"
WRAPPER_SCRIPT_PATH="$PROJECT_DIR/restart_plex.sh"
if [ -f "$WRAPPER_SCRIPT_PATH" ]; then
    chmod +x "$WRAPPER_SCRIPT_PATH"
    echo "Made 'restart_plex.sh' executable."
else
    echo -e "${RED}ERROR: The 'restart_plex.sh' script was not found.${NC}"
    exit 1
fi


# --- Step 3: Configure Sudoers for Passwordless Restart ---
echo -e "\n${YELLOW}Step 3: Configuring sudoers for passwordless Plex restart...${NC}"
SUDOERS_FILE_PATH="/etc/sudoers.d/99-plex-restart"
SUDOERS_RULE="$CURRENT_USER ALL=(ALL) NOPASSWD: $WRAPPER_SCRIPT_PATH"
echo "This will create a sudoers rule for user '$CURRENT_USER'."
echo "$SUDOERS_RULE" | sudo tee "$SUDOERS_FILE_PATH" > /dev/null
sudo chmod 0440 "$SUDOERS_FILE_PATH"
echo -e "${GREEN}Sudoers rule created successfully.${NC}"


# --- Step 4: Create Template config.ini (if it doesn't exist) ---
echo -e "\n${YELLOW}Step 4: Checking for 'config.ini'...${NC}"
if [ ! -f "config.ini" ]; then
    echo "No 'config.ini' found. Creating a template..."
    cat << 'EOF' > config.ini
[telegram]
bot_token = PLACE_YOUR_TELEGRAM_BOT_TOKEN_HERE
allowed_user_ids = PLACE_YOUR_NUMERIC_TELEGRAM_USER_ID_HERE
[plex]
plex_url = http://127.0.0.1:32400
plex_token = PLACE_YOUR_PLEX_TOKEN_HERE
[host]
default_save_path = /path/to/your/telegram_downloads
movies_save_path = /path/to/your/plex_movies
tv_shows_save_path = /path/to/your/plex_tv_shows
EOF
    echo -e "${GREEN}'config.ini' template created.${NC}"
else
    echo -e "${GREEN}'config.ini' already exists. Skipping creation.${NC}"
fi


# --- Step 5: Setup Python Virtual Environment and Install Dependencies ---
echo -e "\n${YELLOW}Step 5: Setting up Python virtual environment...${NC}"
# --- UPDATED TO PYTHON 3.12 ---
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
deactivate
echo -e "${GREEN}Virtual environment created and dependencies installed.${NC}"


# --- Step 6: Optional Systemd Service Setup ---
echo -e "\n${YELLOW}Step 6: Set up the bot as a systemd service?${NC}"
read -p "This will make the bot start on boot and restart if it fails. (y/n): " wants_service

if [[ "$wants_service" =~ ^[Yy]$ ]]; then
    echo -e "\n${CYAN}Creating systemd service file...${NC}"
    SERVICE_FILE_PATH="/etc/systemd/system/telegram-bot.service"

    # --- UPDATED TO PYTHON 3.12 ---
    # Note: Using "EOF" without quotes allows shell variables like $PROJECT_DIR to be expanded.
    sudo tee "$SERVICE_FILE_PATH" > /dev/null <<EOF
[Unit]
Description=Telegram Bot for Plex Torrent Automation
After=network.target

[Service]
User=$CURRENT_USER
Group=$CURRENT_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/venv/bin/python3.12 $PROJECT_DIR/__main__.py
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
    echo -e "Use ${YELLOW}'journalctl -u telegram-bot.service -f'${NC} to view live logs."

else
    echo -e "\n${GREEN}--- Setup Complete! ---${NC}"
    echo "You chose not to set up the systemd service."
    echo -e "\n${YELLOW}To run the bot manually, use these commands:${NC}"
    echo -e "1. Activate the environment: ${CYAN}source ${PROJECT_DIR}/venv/bin/activate${NC}"
    echo -e "2. Run the bot: ${CYAN}python3.12 ${PROJECT_DIR}/__main__.py${NC}"
fi
