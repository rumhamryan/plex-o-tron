#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="/tank/movies"
REMOVE_PREFIXES=0

usage() {
  cat <<'EOF'
Usage: find_two_digit_dash_files.sh [ROOT_DIR] [--remove-prefixes]

Defaults to listing files whose basename starts with:
  two digits + space + dash + space
Example: "00 - Prey (2022).mkv"

Options:
  --remove-prefixes    Rename matching files and remove the leading "NN - "
  -h, --help           Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --remove-prefixes)
      REMOVE_PREFIXES=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "Error: unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      ROOT_DIR="$1"
      shift
      ;;
  esac
done

if [[ ! -d "$ROOT_DIR" ]]; then
  echo "Error: directory not found: $ROOT_DIR" >&2
  exit 1
fi

find "$ROOT_DIR" -path '*/.*' -prune -o -type f -print0 |
while IFS= read -r -d '' file_path; do
  file_name="$(basename "$file_path")"
  if [[ "$file_name" =~ ^[0-9]{2}\ -\  ]]; then
    if [[ "$REMOVE_PREFIXES" -eq 1 ]]; then
      new_name="${file_name#[0-9][0-9] - }"
      parent_dir="$(dirname "$file_path")"
      new_path="$parent_dir/$new_name"

      if [[ -e "$new_path" ]]; then
        printf 'Skip (target exists): %s -> %s\n' "$file_path" "$new_path" >&2
        continue
      fi

      mv -- "$file_path" "$new_path"
      printf 'Renamed: %s -> %s\n' "$file_path" "$new_path"
    else
      printf '%s\n' "$file_path"
    fi
  fi
done
