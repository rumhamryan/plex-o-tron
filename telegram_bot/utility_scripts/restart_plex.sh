#!/bin/bash
# This script's sole purpose is to restart the Plex Media Server.

echo "Wrapper script initiated. Attempting to restart Plex..."
/bin/systemctl restart plexmediaserver.service
echo "Plex restart command sent."
