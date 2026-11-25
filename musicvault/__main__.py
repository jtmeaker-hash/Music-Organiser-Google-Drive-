"""
Package entrypoint for Music Vault Manager.
"""
import argparse
import sys
from pathlib import Path

from .config import Config
from .db import Database
from .cli.menu import main_menu
from .cli.wizard import run_setup_wizard
from .cli.commands import run_benchmark, run_scan, run_research


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        prog="musicvault",
        description="Music Vault Manager: Standalone Audio Library & Metadata Organizer for Termux"
    )
    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    # benchmark
    bm_parser = subparsers.add_parser("benchmark", help="Run benchmark regression test")
    bm_parser.add_argument("--size", type=int, default=10, help="Track count (10 or 50)")
    bm_parser.add_argument("--checkpoint", type=str, default="", help="Path to JSON checkpoint file")

    # setup
    subparsers.add_parser("setup", help="Run credential setup wizard")

    # scan
    subparsers.add_parser("scan", help="Scan Google Drive music library")

    # research
    res_parser = subparsers.add_parser("research", help="Run metadata research")
    res_parser.add_argument("--limit", type=int, default=10, help="Maximum tracks to query")

    args = parser.parse_args()
    config = Config()
    db = Database(config.db_path)

    if not args.command:
        # Launch interactive terminal interface
        main_menu(config, db)
    elif args.command == "benchmark":
        cp_path = Path(args.checkpoint) if args.checkpoint else None
        run_benchmark(config, db, sample_size=args.size, checkpoint_path=cp_path)
    elif args.command == "setup":
        run_setup_wizard(config)
    elif args.command == "scan":
        run_scan(config, db)
    elif args.command == "research":
        run_research(config, db, limit=args.limit)


if __name__ == "__main__":
    main()
