#!/usr/bin/env python3
"""Utilities for syncing commit messages with Changelog.md entries."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence


def _read_lines(changelog_path: Path) -> list[str]:
    if not changelog_path.exists():
        raise FileNotFoundError(f"Changelog file not found: {changelog_path}")
    return changelog_path.read_text(encoding="utf-8").splitlines(keepends=True)


def _write_lines(changelog_path: Path, lines: Sequence[str]) -> None:
    changelog_path.write_text("".join(lines), encoding="utf-8")


def _find_first_entry(lines: Sequence[str]) -> tuple[int, int]:
    """Return (start, end) indices for the first entry after the example block."""
    after_example = False
    start = None
    for idx, line in enumerate(lines):
        if not after_example:
            if line.strip() == "---":
                after_example = True
            continue
        if line.startswith("Commit: "):
            start = idx
            break

    if start is None:
        raise ValueError("No changelog entries found after the example block.")

    end = len(lines)
    for idx in range(start + 1, len(lines)):
        if lines[idx].startswith("Commit: "):
            end = idx
            break
    return start, end


def generate_commit_message(changelog_path: Path) -> str:
    """Build a commit message body from the first changelog entry."""
    lines = _read_lines(changelog_path)
    start, end = _find_first_entry(lines)
    entry_lines = [lines[i].rstrip("\r\n") for i in range(start + 1, end)]
    message = "\n".join(entry_lines).strip("\n")
    if not message:
        raise ValueError(
            "Changelog entry is empty. Please describe your changes before committing."
        )
    return message


def prepare_commit_message(message_file: Path, changelog_path: Path) -> None:
    """Populate the commit message file with the changelog entry if it is empty."""
    current = message_file.read_text(encoding="utf-8") if message_file.exists() else ""
    if current.strip():
        return  # Respect manually supplied messages (e.g., -m or amend)

    message = generate_commit_message(changelog_path)
    message_file.write_text(f"{message}\n", encoding="utf-8")


def replace_pending_commit(changelog_path: Path, commit_hash: str) -> bool:
    """Replace the first 'Commit: <pending>' line with the actual commit hash."""
    lines = _read_lines(changelog_path)
    try:
        start, _ = _find_first_entry(lines)
    except ValueError:
        return False

    line = lines[start]
    if "<pending>" not in line:
        return False

    stripped = line.rstrip("\r\n")
    newline = line[len(stripped) :]
    if not newline:
        newline = os.linesep

    lines[start] = f"Commit: {commit_hash}{newline}"
    _write_lines(changelog_path, lines)
    return True


def finalize_changelog(changelog_path: Path, commit_hash: str | None) -> bool:
    """Update the changelog with the latest commit hash."""
    if commit_hash is None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                check=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            print(
                f"[changelog hooks] Failed to resolve HEAD commit: {exc}",
                file=sys.stderr,
            )
            return False
        commit_hash = result.stdout.strip()

    if not commit_hash:
        return False

    updated = replace_pending_commit(changelog_path, commit_hash)
    if not updated:
        print(
            "[changelog hooks] No pending changelog entry found to update.",
            file=sys.stderr,
        )
    return updated


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--changelog",
        type=Path,
        default=Path("Changelog.md"),
        help="Path to the changelog file (default: %(default)s)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare-message", help="Populate the commit message from Changelog.md"
    )
    prepare.add_argument(
        "--message-file",
        required=True,
        type=Path,
        help="Path to .git/COMMIT_EDITMSG provided by git",
    )

    finalize = subparsers.add_parser(
        "finalize", help="Replace the pending commit placeholder with the actual hash"
    )
    finalize.add_argument(
        "--commit-hash",
        type=str,
        default=None,
        help="Override commit hash (default: detect from HEAD)",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    changelog_path = args.changelog

    try:
        if args.command == "prepare-message":
            prepare_commit_message(args.message_file, changelog_path)
        elif args.command == "finalize":
            finalize_changelog(changelog_path, args.commit_hash)
    except Exception as exc:  # pragma: no cover - hook-friendly message
        print(f"[changelog hooks] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
