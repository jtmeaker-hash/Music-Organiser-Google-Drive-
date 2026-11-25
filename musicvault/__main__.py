"""
Package CLI entrypoint for Music Vault Manager v2.0.1.
"""
import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .cli.commands import (
    run_backup,
    run_benchmark,
    run_hydrate,
    run_preview,
    run_research,
    run_scan,
    run_status,
)
from .cli.menu import main_menu
from .cli.wizard import run_setup_wizard
from .config import Config
from .db import Database, DatabaseMigrationError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="musicvault",
        description="Music Vault Manager v2.0.1: Read-Only Audio Library Metadata & Organisation Preview Tool",
    )
    parser.add_argument("--version", action="version", version=f"musicvault {__version__}")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # status (local read-only, zero network calls)
    st_p = subparsers.add_parser("status", help="Show local DB, safety, and environment status (no network calls)")
    st_p.add_argument("--verbose", action="store_true", help="Show full Drive folder IDs")
    st_p.add_argument("--export-json", type=str, default="", help="Export status report to UTF-8 JSON file")
    st_p.add_argument("--export-csv", type=str, default="", help="Export status summary to safe UTF-8 CSV file")

    # backup
    bk_p = subparsers.add_parser("backup", help="Create a verified timestamped SQLite online database backup")
    bk_p.add_argument("--label", type=str, default="manual", help="Short label included in backup filename")

    # scan
    sc_p = subparsers.add_parser("scan", help="Scan Google Drive source folder metadata inventory (read-only)")
    sc_p.add_argument("--hydrate", action="store_true", help="Run bounded metadata hydration after scan")
    sc_p.add_argument("--limit", type=int, default=10, help="Track cap when --hydrate is enabled (default: 10)")
    sc_p.add_argument("--max-mib", type=float, default=32.0, help="Byte budget in MiB when --hydrate is enabled (default: 32)")
    sc_p.add_argument("--allow-full-download", action="store_true", help="Explicitly opt in to bounded temp file download on Range fallback")
    sc_p.add_argument("--verbose", action="store_true", help="Show full Drive IDs")

    # hydrate
    hy_p = subparsers.add_parser("hydrate", help="Hydrate audio tags & duration via bounded HTTP Range reads")
    hy_p.add_argument("--limit", type=int, default=10, help="Maximum pending live tracks to attempt (default: 10)")
    hy_p.add_argument("--max-mib", type=float, default=32.0, help="Maximum total MiB budget for this run (default: 32)")
    hy_p.add_argument("--max-file-mib", type=float, default=2.0, help="Maximum ranged MiB per track before fallback (default: 2)")
    hy_p.add_argument("--allow-full-download", action="store_true", help="Opt in to bounded temp full download if server ignores Range")
    hy_p.add_argument("--use-sheet", action="store_true", help="Also consult verified exact-ID Tracks sheet Artist/Title when tags absent")
    hy_p.add_argument("--verbose", action="store_true", help="Include full Drive file IDs in exports")
    hy_p.add_argument("--export-json", type=str, default="", help="Export hydration summary to JSON")
    hy_p.add_argument("--export-csv", type=str, default="", help="Export live track hydration table to safe CSV")

    # research
    rs_p = subparsers.add_parser("research", help="Run resumable online metadata research for tracks with identity")
    rs_p.add_argument("--limit", type=int, default=10, help="Maximum eligible live tracks to attempt (default: 10)")
    rs_p.add_argument("--force-refresh", action="store_true", help="Re-query already completed eligible tracks")
    rs_p.add_argument("--include-title-only", action="store_true", help="Include title-only review queue")
    rs_p.add_argument("--no-wikidata", action="store_true", help="Disable Wikidata inferred genre hints")
    rs_p.add_argument("--verbose", action="store_true", help="Include full Drive file IDs in exports")
    rs_p.add_argument("--export-json", type=str, default="", help="Export research summary to JSON")
    rs_p.add_argument("--export-csv", type=str, default="", help="Export research status table to safe CSV")

    # preview
    pv_p = subparsers.add_parser("preview", help="Run read-only live Google Sheets & Drive organisation preview")
    pv_p.add_argument("--limit", type=int, default=25, help="Maximum live tracks to preview (0 for all; default: 25)")
    pv_p.add_argument("--wide", action="store_true", help="Force wide horizontal table output")
    pv_p.add_argument("--verbose", action="store_true", help="Show full Drive file IDs")
    pv_p.add_argument("--export-json", type=str, default="", help="Export preview report to UTF-8 JSON")
    pv_p.add_argument("--export-csv", type=str, default="", help="Export preview report to formula-safe UTF-8 CSV")

    # benchmark
    bm_p = subparsers.add_parser("benchmark", help="Run isolated offline benchmark regression check")
    bm_p.add_argument("--size", type=int, default=10, help="Benchmark sample size (10 or 50; default: 10)")
    bm_p.add_argument("--checkpoint", type=str, default="", help="Optional path to local JSON checkpoint fixture")
    bm_p.add_argument("--expected-rows", type=str, default="", help="Optional path to expected sheet rows JSON fixture")
    bm_p.add_argument("--wide", action="store_true", help="Force wide horizontal table output")
    bm_p.add_argument("--verbose", action="store_true", help="Show full Drive file IDs")

    # setup
    subparsers.add_parser("setup", help="Run credential & Google OAuth setup wizard")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Config()

    if args.command == "setup":
        run_setup_wizard(config)
        return 0

    try:
        db = Database(config.db_path)
    except DatabaseMigrationError as exc:
        sys.stderr.write(f"[✗] Database initialization/migration aborted safely: {config.redact(exc)}\n")
        return 2

    try:
        if not args.command:
            main_menu(config, db)
        elif args.command == "status":
            run_status(
                config,
                db,
                verbose=args.verbose,
                export_json=Path(args.export_json) if args.export_json else None,
                export_csv=Path(args.export_csv) if args.export_csv else None,
            )
        elif args.command == "backup":
            bak = run_backup(config, db, label=args.label)
            return 0 if bak else 1
        elif args.command == "scan":
            res = run_scan(
                config,
                db,
                hydrate_after=args.hydrate,
                hydrate_limit=args.limit,
                hydrate_max_mib=args.max_mib,
                allow_full_download=args.allow_full_download,
                verbose=args.verbose,
            )
            return 0 if res.get("status") == "completed" else 1
        elif args.command == "hydrate":
            run_hydrate(
                config,
                db,
                limit=args.limit,
                max_mib=args.max_mib,
                max_file_mib=args.max_file_mib,
                allow_full_download=args.allow_full_download,
                use_sheet=args.use_sheet,
                verbose=args.verbose,
                export_json=Path(args.export_json) if args.export_json else None,
                export_csv=Path(args.export_csv) if args.export_csv else None,
            )
        elif args.command == "research":
            run_research(
                config,
                db,
                limit=args.limit,
                force_refresh=args.force_refresh,
                include_title_only=args.include_title_only,
                enable_wikidata=not args.no_wikidata,
                verbose=args.verbose,
                export_json=Path(args.export_json) if args.export_json else None,
                export_csv=Path(args.export_csv) if args.export_csv else None,
            )
        elif args.command == "preview":
            run_preview(
                config,
                db,
                limit=args.limit,
                wide=args.wide,
                verbose=args.verbose,
                export_json=Path(args.export_json) if args.export_json else None,
                export_csv=Path(args.export_csv) if args.export_csv else None,
            )
        elif args.command == "benchmark":
            cp_path = Path(args.checkpoint) if args.checkpoint else None
            exp_path = Path(args.expected_rows) if args.expected_rows else None
            run_benchmark(
                config,
                db,
                sample_size=args.size,
                checkpoint_path=cp_path,
                expected_rows_path=exp_path,
                wide=args.wide,
                verbose=args.verbose,
            )
    except KeyboardInterrupt:
        print("\n[i] Interrupted cleanly (Ctrl+C). Saved checkpoints preserved.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
