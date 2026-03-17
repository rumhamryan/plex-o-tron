#!/usr/bin/env bash
set -euo pipefail

MEDIA_GROUP="media"
BOT_USER="ryan"
PLEX_USER="plex"

# Update these if your actual library or staging paths differ.
ROOTS=(
  "/mnt/movies"
  "/mnt/tv"
  "/mnt/downloads"
)

echo "Creating shared group if needed..."
sudo groupadd -f "$MEDIA_GROUP"

echo "Adding users to shared group..."
sudo usermod -aG "$MEDIA_GROUP" "$PLEX_USER"
sudo usermod -aG "$MEDIA_GROUP" "$BOT_USER"

echo "Installing ACL tools if needed..."
if ! command -v setfacl >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y acl
fi

for root in "${ROOTS[@]}"; do
  if [[ ! -e "$root" ]]; then
    echo "Skipping missing path: $root"
    continue
  fi

  echo "Fixing ownership and permissions under: $root"
  sudo chgrp -R "$MEDIA_GROUP" "$root"
  sudo find "$root" -type d -exec chmod 2775 {} +
  sudo find "$root" -type f -exec chmod 664 {} +

  echo "Applying ACLs under: $root"
  sudo setfacl -R -m "g:${MEDIA_GROUP}:rwx" "$root"
  sudo setfacl -R -d -m "g:${MEDIA_GROUP}:rwx" "$root"
done

echo "Setting telegram-bot umask so future files stay group-writable..."
sudo mkdir -p /etc/systemd/system/telegram-bot.service.d
sudo tee /etc/systemd/system/telegram-bot.service.d/umask.conf >/dev/null <<'EOC'
[Service]
UMask=0002
EOC

echo "Reloading and restarting services..."
sudo systemctl daemon-reload
sudo systemctl restart plexmediaserver
sudo systemctl restart telegram-bot

echo
echo "Verification:"
id "$PLEX_USER" || true
id "$BOT_USER" || true
for root in "${ROOTS[@]}"; do
  [[ -e "$root" ]] || continue
  echo "--- $root ---"
  sudo -u "$PLEX_USER" test -w "$root" && echo "plex can write: $root" || echo "plex CANNOT write: $root"
done

echo
echo "Done. Log out and back in if you want your shell to pick up new group membership."
