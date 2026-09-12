"""Command-line export and restore utilities for the personal inbox."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.core.config import get_settings
from app.db import get_session_factory
from app.services.backup import export_inbox, restore_inbox


def main() -> None:
    parser = argparse.ArgumentParser(description="Export or restore the Personal AI Inbox")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("export", help="write a portable ZIP export")
    restore_parser = subparsers.add_parser("restore", help="restore a ZIP export")
    restore_parser.add_argument("archive", type=Path)
    args = parser.parse_args()

    settings = get_settings()
    if args.command == "export":
        path = export_inbox(get_session_factory(), settings.storage_dir)
        print(path)
        return
    result = restore_inbox(
        get_session_factory(),
        args.archive,
        storage_dir=settings.storage_dir,
    )
    print(
        "Restored: "
        f"{result.captures_created} new captures, "
        f"{result.captures_updated} existing captures checked, "
        f"{result.corrections_restored} corrections, "
        f"{result.relationships_restored} relationships, "
        f"{result.media_restored} media files."
    )


if __name__ == "__main__":
    main()
