#!/usr/bin/env bash
set -euo pipefail

# Resets Plex's main library database so Plex can recreate it on next startup.
# This is destructive for Plex metadata/history, so we keep a timestamped backup
# by default unless explicitly disabled.

DEFAULT_DBDIR="/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Plug-in Support/Databases"
DEFAULT_SERVICE="plexmediaserver"
DB_BASENAME="com.plexapp.plugins.library.db"

dbdir="$DEFAULT_DBDIR"
service_name="$DEFAULT_SERVICE"
skip_backup=0
assume_yes=0

usage() {
  cat <<'EOF'
Usage:
  sudo bash scripts/reset_plex_library_db.sh [options]

Options:
  --dbdir PATH       Override Plex database directory.
  --service NAME     Override systemd service name (default: plexmediaserver).
  --no-backup        Skip creating a backup copy before deletion.
  --yes              Skip interactive confirmation.
  -h, --help         Show this help message.

Examples:
  sudo bash scripts/reset_plex_library_db.sh --yes
  sudo bash scripts/reset_plex_library_db.sh --dbdir "/custom/Plex Media Server/Plug-in Support/Databases" --yes
EOF
}

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*"
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --dbdir)
      shift
      [[ $# -gt 0 ]] || fail "--dbdir requires a path."
      dbdir="$1"
      ;;
    --service)
      shift
      [[ $# -gt 0 ]] || fail "--service requires a value."
      service_name="$1"
      ;;
    --no-backup)
      skip_backup=1
      ;;
    --yes)
      assume_yes=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown option: $1"
      ;;
  esac
  shift
done

if [[ "${EUID}" -ne 0 ]]; then
  fail "Run as root (for example: sudo bash scripts/reset_plex_library_db.sh --yes)"
fi

if ! command -v systemctl >/dev/null 2>&1; then
  fail "systemctl not found. This script expects a systemd-managed Plex service."
fi

if ! systemctl list-unit-files | awk '{print $1}' | grep -Fxq "${service_name}.service"; then
  fail "Service '${service_name}.service' was not found."
fi

db_file="$dbdir/$DB_BASENAME"
db_wal="$db_file-wal"
db_shm="$db_file-shm"
backup_dir="$dbdir/backup-$(date '+%F-%H%M%S')"

if [[ ! -d "$dbdir" ]]; then
  fail "Database directory not found: $dbdir"
fi

declare -a existing_files=()
for candidate in "$db_file" "$db_wal" "$db_shm"; do
  if [[ -e "$candidate" ]]; then
    existing_files+=("$candidate")
  fi
done

if [[ ${#existing_files[@]} -eq 0 ]]; then
  fail "No active DB files found in: $dbdir"
fi

log "Target service: ${service_name}.service"
log "Target DB directory: $dbdir"
log "Files to remove:"
for file in "${existing_files[@]}"; do
  log "  - $file"
done

if [[ $skip_backup -eq 0 ]]; then
  log "Backup directory (will be created): $backup_dir"
else
  log "Backup disabled (--no-backup)."
fi

if [[ $assume_yes -eq 0 ]]; then
  read -r -p "Proceed with Plex DB reset? [y/N]: " response
  if [[ ! "$response" =~ ^[Yy]$ ]]; then
    log "Aborted."
    exit 0
  fi
fi

log "Stopping ${service_name}.service..."
systemctl stop "$service_name"

if [[ $skip_backup -eq 0 ]]; then
  log "Creating backup..."
  mkdir -p "$backup_dir"
  cp -a "${existing_files[@]}" "$backup_dir/"
fi

log "Removing DB files..."
rm -f "$db_file" "$db_wal" "$db_shm"

if id plex >/dev/null 2>&1; then
  # Ensures Plex can create replacement DB files after deletion.
  chown -R plex:plex "$dbdir"
fi

log "Starting ${service_name}.service..."
systemctl start "$service_name"

log "Current service status:"
systemctl --no-pager --full status "$service_name" || true

log "Done. Plex should recreate '$DB_BASENAME' automatically."
if [[ $skip_backup -eq 0 ]]; then
  log "Backup saved at: $backup_dir"
fi
