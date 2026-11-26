"""
Package CLI entrypoint for Music Vault Manager v2.0.2.
"""
import argparse
import sys
from tempfile import TemporaryDirectory
from pathlib import Path
from typing import List, Optional

from . import __version__
from .cli.commands import (
    run_backup,
    run_benchmark,
    run_hydrate,
    run_preview,
    run_recover_research,
    run_research,
    run_scan,
    run_status,
)
from .cli.menu import main_menu
from .cli.wizard import run_setup_wizard
from .auth import GoogleAuthManager
from .config import Config
from .core.process import run_process, snapshot_database
from .core.coverage import library_report
from .core.provider_policy import CAPABILITIES, ProviderPolicy
from .providers.base import canonical_provider_family
from .services.drive import DriveService
from .services.sheets import SheetsService
from .cli.formatting import export_safe_csv, export_safe_json
from .db import Database, DatabaseMigrationError, SCHEMA_VERSION
from .db_lock import database_lock


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="musicvault",
        description="Music Vault Manager v2.0.2: Read-Only Audio Library Metadata & Organisation Preview Tool",
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

    migration = subparsers.add_parser("migrate", help="Back up and migrate the local database")
    migration.add_argument("--confirm-stopped", required=True, action="store_true",
                           help="Confirm older MusicVault processes are stopped")

    reset = subparsers.add_parser("clear-quota", help="Clear an unknown quota block after verifying the reset")
    reset.add_argument("provider", choices=sorted(CAPABILITIES))
    reset.add_argument("--confirm-reset", required=True, action="store_true")

    # scan
    sc_p = subparsers.add_parser("scan", help="Scan Google Drive source folder metadata inventory (read-only)")
    sc_p.add_argument("--hydrate", action="store_true", help="Run bounded metadata hydration after scan")
    sc_p.add_argument("--limit", type=int, default=10, help="Track cap when --hydrate is enabled (default: 10)")
    sc_p.add_argument("--max-mib", type=float, default=32.0, help="Byte budget in MiB when --hydrate is enabled (default: 32)")
    sc_p.add_argument("--allow-full-download", action="store_true", help="Explicitly opt in to bounded temp file download on Range fallback")
    sc_p.add_argument("--verbose", action="store_true", help="Show full Drive IDs")

    process = subparsers.add_parser("process", help="Run the resumable read-only library pipeline")
    process.add_argument("--scope", choices=("source", "destination", "all"), default="all")
    process.add_argument("--limit", type=int, default=0)
    process.add_argument("--batch-size", type=int, default=10)
    process.add_argument("--cooldown", type=float, default=0)
    process.add_argument("--concurrency", type=int, choices=range(1, 5), default=1)
    process.add_argument("--resume", action="store_true")
    process.add_argument("--inventory-only", action="store_true")
    process.add_argument("--dry-run", action="store_true")
    process.add_argument("--file-id", action="append", default=[])
    process.add_argument("--provider", default="", help="Resume enrichment for one provider family")
    process.add_argument("--verbose", action="store_true", help="Include full file IDs in local exports")
    process.add_argument("--export-json", default="")
    process.add_argument("--export-csv", default="")

    # hydrate
    hy_p = subparsers.add_parser("hydrate", help="Hydrate audio tags & duration via bounded HTTP Range reads")
    hy_p.add_argument("--limit", type=int, default=10, help="Maximum pending live tracks to attempt (default: 10)")
    hy_p.add_argument("--max-mib", type=float, default=32.0, help="Maximum total MiB budget for this run (default: 32)")
    hy_p.add_argument("--max-file-mib", type=float, default=2.0, help="Maximum ranged MiB per track before fallback (default: 2)")
    hy_p.add_argument("--allow-full-download", action="store_true", help="Opt in to bounded temp full download if server ignores Range")
    hy_p.add_argument("--use-sheet", action="store_true", help="Also consult verified exact-ID Tracks sheet Artist/Title when tags absent")
    hy_p.add_argument("--force-rehydrate", action="store_true", help="Re-inspect already-inspected tagless/partial tracks")
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

    # recover-research (dry-run default; requires --apply + verified backup to mutate)
    rc_p = subparsers.add_parser(
        "recover-research",
        help="Preview (dry-run) or selectively re-queue tracks with incomplete/failed provider research audits",
    )
    rc_p.add_argument("--apply", action="store_true", help="Transactionally apply selective recovery (requires verified backup)")
    rc_p.add_argument("--create-backup", action="store_true", help="Create and verify a fresh SQLite backup before --apply")
    rc_p.add_argument("--backup-file", type=str, default="", help="Path to an existing verified SQLite backup file for --apply")
    rc_p.add_argument("--wide", action="store_true", help="Force wide horizontal table output")
    rc_p.add_argument("--verbose", action="store_true", help="Show full Drive file IDs")
    rc_p.add_argument("--export-json", type=str, default="", help="Export recovery audit report to UTF-8 JSON")
    rc_p.add_argument("--export-csv", type=str, default="", help="Export recovery audit rows to formula-safe UTF-8 CSV")

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
    if args.command == "process":
        if args.limit < 0 or args.batch_size < 1 or not 0 <= args.cooldown < float("inf"):
            parser.error("process requires a non-negative limit and cooldown and a positive batch size")
        if args.provider and not args.resume:
            parser.error("--provider requires --resume")
        if args.dry_run and args.resume:
            parser.error("--dry-run starts a disposable run")
    config = Config()

    if args.command == "setup":
        run_setup_wizard(config)
        return 0

    try:
        if args.command == "process" and args.dry_run:
            with TemporaryDirectory(prefix="musicvault-dry-run-") as directory:
                target = Path(directory) / "library.db"
                snapshot_database(config.db_path, target)
                return run_process_cli(args, config, Database(target))
        with database_lock(config.db_path):
            db = Database(config.db_path, initialize=False)
            version = db.get_schema_version()
            if version > SCHEMA_VERSION:
                raise DatabaseMigrationError("Database schema is newer than this build.")
            if version and version < SCHEMA_VERSION and args.command not in {"status", "backup", "migrate"}:
                raise DatabaseMigrationError("Run musicvault migrate --confirm-stopped after shutting down old processes.")
            if args.command == "status" and version == 1:
                print("Schema 1 database. Back up and migrate after verifying shutdown.")
                return 0
            if not version or args.command == "migrate":
                db = Database(config.db_path)
            if args.command == "migrate":
                print(f"Local schema version {db.get_schema_version()}")
                if db.last_backup_path:
                    print(f"Verified backup: {db.last_backup_path}")
                return 0
            return dispatch(args, config, db)
    except (DatabaseMigrationError, OSError, RuntimeError, ValueError) as exc:
        sys.stderr.write(f"[✗] {config.redact(exc)}\n")
        return 2
    except KeyboardInterrupt:
        return 130


def run_process_cli(args, config, db):
    policy = None if args.inventory_only or args.dry_run else ProviderPolicy(config)
    creds = GoogleAuthManager(config).get_credentials(
        allow_interactive=not args.dry_run, persist_refresh=not args.dry_run,
    )
    if not creds:
        print("Google authentication is required to read Drive inventory.")
        return 2
    drive = DriveService(creds)
    preview = None if args.inventory_only else lambda conn, _run_id: library_report(
        conn, db, config, args, SheetsService(creds), drive,
    )
    result = run_process(config, db, drive, args,
                         drive_factory=lambda: DriveService(creds), policy=policy, preview=preview)
    print(config.redact(result))
    if args.export_json and "coverage" not in result:
        export_safe_json(Path(args.export_json), result, config.known_secrets())
    if args.export_csv and "coverage" not in result:
        rows = [{"role": role, **info} for role, info in result["roots"].items()]
        fields = list(dict.fromkeys(key for row in rows for key in row))
        export_safe_csv(Path(args.export_csv), fields, rows, config.known_secrets())
    return {"failed": 1, "interrupted": 130}.get(result["status"], 0)


def dispatch(args: argparse.Namespace, config: Config, db: Database) -> int:
    try:
        if not args.command:
            main_menu(config, db)
        elif args.command == "clear-quota":
            family = canonical_provider_family(args.provider)
            state = db.get_provider_operational_state(family) or {}
            if state.get("last_status") != "quota_exceeded" or state.get("cooldown_until_utc"):
                print("No unknown quota block to clear for this provider.")
                return 1
            db.set_provider_operational_state(family, last_status="ok", cooldown_until_utc="")
            print(f"Cleared the confirmed reset for {family}.")
            return 0
        elif args.command == "process":
            return run_process_cli(args, config, db)
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
                force_rehydrate=args.force_rehydrate,
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
        elif args.command == "recover-research":
            rec_res = run_recover_research(
                config,
                db,
                apply=args.apply,
                create_backup=args.create_backup,
                backup_file=Path(args.backup_file) if args.backup_file else None,
                verbose=args.verbose,
                wide=args.wide,
                export_json=Path(args.export_json) if args.export_json else None,
                export_csv=Path(args.export_csv) if args.export_csv else None,
            )
            return 1 if rec_res.get("mode") == "error" else 0
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
