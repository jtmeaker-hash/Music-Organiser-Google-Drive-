"""
Command implementations for Music Vault Manager v2.0.3 (Read-only library processing).
Implements local status diagnostics, verified DB backups, transactional inventory scanning,
bandwidth-bounded audio hydration, resumable online research, selective dry-run-first recovery,
real read-only Sheets/Drive preview, and isolated offline benchmarks.
"""
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import __version__
from ..auth import GoogleAuthManager
from ..config import Config
from ..core.hydrator import hydrate_live_inventory
from ..core.inventory import scan_root
from ..core.organizer import evaluate_live_preview
from ..core.researcher import execute_research_batch
from ..db import Database, DatabaseMigrationError
from ..fixtures.benchmarks import execute_isolated_benchmark
from ..models import Track
from ..services.drive import DriveService
from ..services.sheets import SheetsService
from ..termux import detect_environment, termux_wake_lock
from .formatting import (
    export_safe_csv,
    export_safe_json,
    format_mib,
    get_terminal_width,
    render_responsive_records,
    short_file_id,
)

logger = logging.getLogger("musicvault.commands")


def run_status(
    config: Config,
    db: Database,
    verbose: bool = False,
    export_json: Optional[Path] = None,
    export_csv: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Read-only local status & diagnostic report (ZERO network calls).
    """
    env_info = detect_environment()
    db_ok, db_msg = db.verify_integrity()
    inv = db.get_inventory_summary()

    inv_for_report = dict(inv)
    if isinstance(inv_for_report.get("last_scan"), dict):
        ls_copy = dict(inv_for_report["last_scan"])
        ls_copy["source_folder_id"] = short_file_id(ls_copy.get("source_folder_id", ""), verbose=verbose)
        inv_for_report["last_scan"] = ls_copy

    report: Dict[str, Any] = {
        "version": __version__,
        "build_mode": f"v{__version__}_read_only",
        "environment": {
            "runtime": env_info.runtime,
            "distro": env_info.distro_name,
            "in_tmux": env_info.in_tmux,
            "tmux_available": env_info.tmux_available,
            "wake_lock_binary_detected": env_info.wake_lock_detected,
            "wake_lock_acquired": env_info.wake_lock_acquired,
            "wake_lock_detail": env_info.wake_lock_detail,
        },
        "safety_gates": {
            "configured_dry_run": config.dry_run,
            "effective_dry_run": True,
            "allow_provisional_moves": config.allow_provisional_moves,
            "provider_terms_cleared": config.provider_terms_cleared,
            "cloud_writes_enabled": False,
        },
        "credentials_configured": {
            "sonovault": bool(config.sonovault_api_key),
            "audiometa": bool(config.audiometa_api_key),
            "discogs": bool(config.discogs_token),
            "google_token_file_present": config.token_path.exists(),
            "google_client_secrets_present": config.client_secrets_path.exists(),
        },
        "database": {
            "integrity_ok": db_ok,
            "integrity_detail": db_msg,
            **inv_for_report,
        },
    }

    width = min(get_terminal_width(), 72)
    print("\n" + "=" * width)
    print(f"  MUSIC VAULT MANAGER v{__version__} — LOCAL DIAGNOSTIC STATUS")
    print("=" * width)
    print(f"  Runtime:        {env_info.distro_name} [{env_info.runtime}]")
    print(f"  Tmux session:   {'Active' if env_info.in_tmux else ('Installed (not active)' if env_info.tmux_available else 'Not installed')}")
    wl_acq = "Yes" if env_info.wake_lock_acquired is True else ("Failed" if env_info.wake_lock_acquired is False else "Not attempted in this process")
    print(f"  Wake-lock:      binary={'Detected' if env_info.wake_lock_detected else 'Absent'} | acquired={wl_acq}")
    print(f"  Safety gates:   DRY_RUN=True (enforced; .env={config.dry_run}) | Writes=DISABLED")
    print(f"                  PROVISIONAL_MOVES={'HELD' if not config.allow_provisional_moves else 'CONFIG_TRUE_OVERRIDDEN_BY_READONLY'}")
    print(f"                  PROVIDER_TERMS_CLEARED={config.provider_terms_cleared}")
    print("-" * width)
    print(f"  DB Integrity:   {'OK' if db_ok else 'FAILED (' + db_msg + ')'} (schema v{inv['schema_version']})")
    if inv.get("last_migration_backup"):
        print(f"  Migration bak:  {inv['last_migration_backup']}")
    print(f"  Total records:  {inv['total_records']}")
    print(f"    Live source:  {inv['live_source_tracks']} (ready artist+title: {inv['live_ready_identity']}, missing: {inv['live_missing_identity']})")
    print(f"    Quarantined:  {inv['quarantined_records']} (checkpoint-only: {inv['checkpoint_only_records']})")
    print(f"    Stale files:  {inv['stale_live_tracks']} | Unresolved conflicts: {inv['unresolved_conflicts']}")
    print(f"  Hydration:      {inv['hydration_status_counts'] or {'none': 0}}")
    print(f"  Bytes read:     {format_mib(inv['total_hydration_bytes'])}")
    print(
        f"  Research:       {inv['research_status_counts'] or {'none': 0}} "
        f"(eligible: {inv['eligible_for_research']}, incomplete: {inv.get('incomplete_research_tracks', 0)})"
    )
    if inv.get("active_provider_cooldowns"):
        cd_parts = [
            f"{p}:{info.get('remaining_cooldown_seconds', 0)}s"
            for p, info in inv["active_provider_cooldowns"].items()
        ]
        print(f"  Cooldowns:      {', '.join(cd_parts)}")
    if inv.get("last_scan"):
        ls = inv["last_scan"]
        src_disp = short_file_id(ls.get("source_folder_id", ""), verbose=verbose)
        print(f"  Last scan:      #{ls['scan_id']} [{ls['status']}] seen={ls['seen_count']} source={src_disp}")
    print("=" * width + "\n")

    secrets = config.known_secrets()
    if export_json:
        p = export_safe_json(export_json, report, secrets)
        print(f"[✓] Exported JSON status report to {p}")
    if export_csv:
        flat_row = {
            "version": __version__,
            "runtime": env_info.runtime,
            "db_integrity_ok": db_ok,
            "schema_version": inv["schema_version"],
            "total_records": inv["total_records"],
            "live_source_tracks": inv["live_source_tracks"],
            "quarantined_records": inv["quarantined_records"],
            "live_ready_identity": inv["live_ready_identity"],
            "live_missing_identity": inv["live_missing_identity"],
            "eligible_for_research": inv["eligible_for_research"],
            "total_hydration_bytes": inv["total_hydration_bytes"],
        }
        p = export_safe_csv(export_csv, list(flat_row.keys()), [flat_row], secrets)
        print(f"[✓] Exported CSV status summary to {p}")

    return report


def run_backup(config: Config, db: Database, label: str = "manual") -> Optional[Path]:
    """Create a verified timestamped SQLite online backup without overwriting prior backups."""
    try:
        bak = db.create_verified_backup(label=label)
        print(f"[✓] Created and verified SQLite online backup: {bak.name}")
        print(f"    Location: {bak}")
        return bak
    except DatabaseMigrationError as exc:
        print(f"[✗] Backup failed: {config.redact(exc)}")
        return None


def run_scan(
    config: Config,
    db: Database,
    hydrate_after: bool = False,
    hydrate_limit: int = 10,
    hydrate_max_mib: float = 32.0,
    allow_full_download: bool = False,
    verbose: bool = False,
    drive_override: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Scan configured Google Drive source folder (read-only metadata list).
    Refreshes Drive parent/name/size independently of tags, repairs unchanged incomplete rows,
    and marks membership ONLY after full scan completion.
    """
    if drive_override is not None:
        drive = drive_override
    else:
        auth_mgr = GoogleAuthManager(config)
        creds = auth_mgr.get_credentials()
        if not creds:
            print("[✗] Google authentication is required to scan Drive.")
            return {"status": "auth_failed", "seen": 0}
        drive = DriveService(creds)

    source_id = config.source_folder_id
    src_disp = short_file_id(source_id, verbose=verbose)
    print(f"\nStarting read-only Drive inventory scan on source folder ({src_disp})...")

    with termux_wake_lock():
        result = scan_root(db, drive, source_id, "source", config.redact)
    if result["status"] != "completed":
        print(f"[!] Source scan {result['status']}: {result['error']}")
        return result
    inventory = db.get_inventory_summary()
    result["live_source_tracks"] = inventory["live_source_tracks"]
    result["quarantined_tracks"] = inventory["quarantined_records"]
    print(f"[✓] Source inventory completed: {result['seen']} distinct files")
    if hydrate_after:
        print(
            f"\n[--hydrate] Running bounded post-scan hydration "
            f"(limit={hydrate_limit}, budget={hydrate_max_mib:g} MiB)..."
        )
        hyd_sum = run_hydrate(
            config,
            db,
            limit=hydrate_limit,
            max_mib=hydrate_max_mib,
            allow_full_download=allow_full_download,
            verbose=verbose,
            drive_override=drive,
        )
        result["hydration"] = hyd_sum
    return result


def run_hydrate(
    config: Config,
    db: Database,
    limit: int = 10,
    max_mib: float = 32.0,
    max_file_mib: float = 2.0,
    allow_full_download: bool = False,
    use_sheet: bool = False,
    force_rehydrate: bool = False,
    verbose: bool = False,
    export_json: Optional[Path] = None,
    export_csv: Optional[Path] = None,
    drive_override: Optional[Any] = None,
    sheets_override: Optional[Any] = None,
) -> Any:
    """
    Bandwidth-aware audio tag & duration hydration for pending live tracks.
    Defaults: --limit 10, --max-mib 32.0, no full-file downloads unless opted in.
    """
    inv = db.get_inventory_summary()
    if inv["live_source_tracks"] == 0:
        print("\n[!] NO WORK PERFORMED: 0 live source tracks in database. Run 'musicvault scan' first.")
        return None

    queue_preview = db.get_live_tracks_for_hydration(
        limit=max(1, int(limit)),
        force_rehydrate=force_rehydrate,
        use_sheet_uninspected=use_sheet,
    )
    if not queue_preview:
        print(
            f"\n[i] NO WORK NEEDED: All {inv['live_source_tracks']} live tracks have completed or "
            "permanently unsupported hydration status."
        )
        return None

    if drive_override is not None:
        drive = drive_override
        creds = None
    else:
        auth_mgr = GoogleAuthManager(config)
        creds = auth_mgr.get_credentials()
        if not creds:
            print("[✗] Google authentication is required to read ranged Drive audio headers.")
            return None
        drive = DriveService(creds)

    sheet_identities = None
    if use_sheet:
        sh_svc = sheets_override or (SheetsService(creds) if creds else None)
        if sh_svc is not None:
            try:
                s_idx = sh_svc.load_verified_index(config.sheet_id)
                if s_idx.schema_valid and s_idx.verified_identities:
                    sheet_identities = s_idx.verified_identities
                    print(f"  [sheet] Loaded {len(sheet_identities)} exact-ID verified sheet identities.")
                elif not s_idx.schema_valid:
                    print(f"  [!] Sheet identity fallback skipped ({s_idx.schema_error}).")
            except Exception as exc:
                print(f"  [!] Sheet identity lookup skipped: {config.redact(exc)}")

    est_bytes = min(int(max_mib * 1024 * 1024), len(queue_preview) * 131072)
    print(
        f"\nStarting bounded metadata hydration: up to {len(queue_preview)} live tracks "
        f"(cap={max_mib:g} MiB, est. ranged transfer ~{format_mib(est_bytes)}, "
        f"full-download={'OPTED-IN' if allow_full_download else 'OFF'})..."
    )

    started = time.monotonic()

    def _on_progress(i: int, total: int, tr: Track, status: str, used_b: int) -> None:
        if i == 1 or i == total or i % 5 == 0:
            elapsed = time.monotonic() - started
            origin = tr.metadata.identity_origin or "-"
            print(
                f"  [{i}/{total}] {tr.name[:42]} -> {status} ({origin}) | "
                f"transferred={format_mib(used_b)} ({elapsed:.1f}s)"
            )

    with termux_wake_lock():
        summary = hydrate_live_inventory(
            db=db,
            drive_service=drive,
            limit=limit,
            max_mib=max_mib,
            max_file_ranged_mib=max_file_mib,
            allow_full_download=allow_full_download,
            sheet_identities=sheet_identities,
            force_rehydrate=force_rehydrate,
            secret_values=config.known_secrets(),
            progress_callback=_on_progress,
        )

    print("\n--- Hydration Batch Summary ---")
    print(f"  Attempted:                 {summary.attempted} (of {summary.eligible_pending} pending/incomplete live)")
    print(f"  Verified embedded tags:    {summary.hydrated_embedded} (+ {summary.preserved_same_version} preserved same-version)")
    print(f"  Exact-ID sheet identity:   {summary.hydrated_sheet_identity}")
    print(f"  Ambiguous filename hints:  {summary.hydrated_filename_ambiguous}")
    print(f"  Title-only (held):         {summary.title_only_unresolved}")
    print(f"  Tagless/unresolved:        {summary.unresolved_tagless}")
    print(f"  Failures (retryable/perm): {summary.retryable_failures} retryable / {summary.permanent_unsupported} unsupported")
    print(f"  Actual bytes transferred:  {format_mib(summary.bytes_transferred)} / {format_mib(summary.byte_budget_cap)}")
    if summary.paused_budget_exhausted:
        print("  [!] Paused cleanly upon reaching --max-mib byte budget. Re-run 'musicvault hydrate' to continue.")
    if summary.interrupted:
        print("  [!] Interrupted by user (Ctrl+C). Completed track checkpoints saved.")
    for err_line in summary.errors[:5]:
        print(f"  [issue] {config.redact(err_line)}")
    print()

    secrets = config.known_secrets()
    if export_json:
        export_safe_json(export_json, summary.__dict__, secrets)
        print(f"[✓] Exported hydration JSON summary to {export_json}")
    if export_csv:
        live_rows = [
            {
                "file_id": short_file_id(t.file_id, verbose=verbose),
                "name": t.name,
                "hydration_status": t.hydration_status,
                "hydration_method": t.hydration_method,
                "identity_origin": t.metadata.identity_origin,
                "identity_confidence": t.metadata.identity_confidence,
                "artist": t.metadata.artist,
                "title": t.metadata.title,
                "duration": t.metadata.duration,
                "isrc": t.metadata.isrc,
                "bpm": t.metadata.embedded_bpm,
                "key": t.metadata.embedded_key,
                "bytes": t.hydration_bytes,
            }
            for t in db.get_all_tracks(live_only=True)
        ]
        if live_rows:
            export_safe_csv(export_csv, list(live_rows[0].keys()), live_rows, secrets)
            print(f"[✓] Exported hydration CSV report to {export_csv}")

    return summary


def run_research(
    config: Config,
    db: Database,
    limit: int = 10,
    force_refresh: bool = False,
    include_title_only: bool = False,
    enable_wikidata: bool = True,
    verbose: bool = False,
    export_json: Optional[Path] = None,
    export_csv: Optional[Path] = None,
    providers_override: Optional[Dict[str, Any]] = None,
) -> Any:
    """
    Run resumable online metadata research for eligible live tracks with known identity.
    Never prints fake completion when 0 tracks were attempted.
    """
    started = time.monotonic()

    def _on_progress(i: int, total: int, tr: Track, status: str, cand_cnt: int) -> None:
        elapsed = time.monotonic() - started
        print(f"  [{i}/{total}] {tr.name[:44]} -> {status} ({cand_cnt} candidates, {elapsed:.1f}s)")

    with termux_wake_lock():
        summary = execute_research_batch(
            config=config,
            db=db,
            limit=limit,
            force_refresh=force_refresh,
            include_title_only=include_title_only,
            enable_wikidata_fallback=enable_wikidata,
            providers_override=providers_override,
            progress_callback=_on_progress,
        )

    print("\n--- Online Metadata Research Summary ---")
    print(
        f"  Live source inventory: {summary.live_total} "
        f"(quarantined excluded: {summary.quarantined_excluded}, stale excluded: {summary.stale_excluded})"
    )
    print(
        f"  Identity & queue:      researched_complete={summary.already_completed} | "
        f"deferred_retry={summary.deferred_retry_not_due} | "
        f"exhausted_cap={summary.exhausted_retry_limit}"
    )
    print(
        f"                         missing_identity={summary.deferred_missing_identity} | "
        f"title_only={summary.deferred_title_only}"
    )

    secrets = config.known_secrets()

    def _write_research_exports() -> None:
        if export_json:
            export_safe_json(export_json, summary.__dict__, secrets)
            print(f"[✓] Exported research JSON summary to {export_json}")
        if export_csv:
            rows_out = [
                {
                    "file_id": short_file_id(t.file_id, verbose=verbose),
                    "name": t.name,
                    "artist": t.metadata.artist,
                    "title": t.metadata.title,
                    "research_status": t.research_status,
                    "research_incomplete": t.research_incomplete,
                    "attempts": t.research_attempts,
                    "deferrals": t.research_deferrals,
                    "next_retry_utc": t.research_next_retry_utc,
                    "candidates_count": len(db.get_candidates(t.file_id)),
                }
                for t in db.get_all_tracks(live_only=True)
            ]
            if rows_out:
                export_safe_csv(export_csv, list(rows_out[0].keys()), rows_out, secrets)
                print(f"[✓] Exported research CSV report to {export_csv}")

    if summary.attempted == 0:
        if summary.paused_all_providers_unavailable:
            stop_reasons = []
            if summary.paused_provider_cooldown:
                stop_reasons.append("provider cooldown active")
            if summary.paused_provider_budget:
                stop_reasons.append("run request budget reached")
            reason_str = ", ".join(stop_reasons) or "all configured providers unavailable"
            print(
                f"  [!] PAUSED ({reason_str}): 0 tracks consumed attempts "
                f"(deferred without penalty={summary.deferred_without_attempt})."
            )
            if summary.next_retry_utc:
                print(f"  [i] Next retry window: {summary.next_retry_utc}")
        else:
            print(f"  [!] NO WORK PERFORMED: {config.redact(summary.no_work_reason)}")
        if summary.provider_cooldowns:
            for p_name, cd_utc in sorted(summary.provider_cooldowns.items()):
                print(f"  [cooldown] {p_name}: until {cd_utc}")
        for issue in summary.actionable_issues:
            print(f"  [!] Provider issue: {config.redact(issue)}")
        print()
        _write_research_exports()
        return summary

    print(
        f"  Attempted this batch:  {summary.attempted} (limit={limit}; "
        f"completed={summary.completed_tracks}, incomplete_retained={summary.incomplete_tracks})"
    )
    print(
        f"  Recording-verified:    {summary.matched_verified} "
        f"(with_genre={summary.matched_verified_with_genre}, identity_only_no_genre={summary.matched_verified_no_genre})"
    )
    print(f"  Review-only / partial: {summary.matched_review_only}")
    print(f"  Definitive no match:   {summary.no_match}")
    print(f"  Retryable failures:    {summary.failed_retryable}")
    if summary.unresolved_blocked:
        print(f"  Blocked / unresolved:  {summary.unresolved_blocked}")
    print(
        f"  Traffic diagnostics:   actual_http_requests={summary.total_actual_http_requests} | "
        f"cache_hits={summary.total_cache_hits}"
    )

    if summary.provider_telemetry:
        print("  Provider telemetry:")
        for p_name, stats in summary.provider_telemetry.items():
            skipped_tot = (
                stats.get("skipped_cooldown", 0)
                + stats.get("skipped_budget", 0)
                + stats.get("skipped_blocked", 0)
            )
            if stats.get("calls", 0) or stats.get("cache_hits", 0) or skipped_tot:
                print(
                    f"    - {p_name}: http_calls={stats.get('calls', 0)}, cache_hits={stats.get('cache_hits', 0)}, "
                    f"skipped={skipped_tot} (cd={stats.get('skipped_cooldown', 0)}/cap={stats.get('skipped_budget', 0)}/blk={stats.get('skipped_blocked', 0)}), "
                    f"ok={stats.get('ok', 0)}, rate_lim={stats.get('rate_limited', 0)}, "
                    f"auth_err={stats.get('auth_errors', 0)}, quota_err={stats.get('quota_exceeded', 0)}, "
                    f"err={stats.get('server_errors', 0) + stats.get('network_errors', 0) + stats.get('timeouts', 0)}"
                )

    if summary.provider_cooldowns:
        print("  Active provider cooldowns:")
        for p_name, cd_utc in sorted(summary.provider_cooldowns.items()):
            print(f"    - {p_name}: retry after {cd_utc}")
    if summary.paused_provider_budget:
        print("  [!] Stopped early: provider per-run request cap reached (unqueried tracks preserved).")
    if summary.paused_provider_cooldown:
        print("  [!] Stopped early: eligible providers entered cooldown (unqueried tracks preserved).")
    if summary.next_retry_utc and (summary.incomplete_tracks > 0 or summary.paused_provider_cooldown):
        print(f"  [i] Earliest scheduled retry UTC: {summary.next_retry_utc}")
    for issue in summary.actionable_issues:
        print(f"  [!] Provider issue: {config.redact(issue)}")
    if summary.interrupted:
        print("  [!] Interrupted by user (Ctrl+C). Completed per-track checkpoints saved.")
    print()

    _write_research_exports()

    return summary


def run_recover_research(
    config: Config,
    db: Database,
    apply: bool = False,
    create_backup: bool = False,
    backup_file: Optional[Path] = None,
    verbose: bool = False,
    wide: bool = False,
    export_json: Optional[Path] = None,
    export_csv: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Selective dry-run-first research recovery for live, non-quarantined, current-content tracks
    stranded by transient/cooldown/budget provider failures.
    Never runs automatically; requires --apply plus a verified SQLite backup to mutate.
    """
    configured_provs = {"musicbrainz"}
    if config.sonovault_api_key:
        configured_provs.add("sonovault")
    if config.audiometa_api_key:
        configured_provs.add("audiometa")
    if config.discogs_token:
        configured_provs.add("discogs")

    try:
        report = db.inspect_or_apply_research_recovery(
            configured_providers=configured_provs,
            apply=apply,
            verified_backup_path=backup_file,
            create_backup=create_backup,
        )
    except DatabaseMigrationError as exc:
        err_clean = config.redact(exc)
        print(f"\n[✗] Selective recovery aborted safely: {err_clean}\n")
        return {"mode": "error", "error": err_clean}

    counts = report["counts"]
    mode_banner = (
        "APPLIED TRANSACTIONALLY" if report["mode"] == "applied" else "DRY-RUN PREVIEW (0 DB mutations)"
    )
    print(f"\n=== SELECTIVE RESEARCH RECOVERY — {mode_banner} ===")
    if report.get("verified_backup"):
        print(f"  Verified backup:       {report['verified_backup']}")
    print(f"  Total records checked: {counts['total_records_inspected']}")
    print(
        f"  Excluded safely:       quarantined/non-live={counts['excluded_quarantined_or_nonlive']} | "
        f"permanent_unsupported={counts['excluded_permanent_unsupported']} | "
        f"stale={counts['excluded_stale_or_version_mismatch']} | "
        f"missing_identity={counts['excluded_missing_identity']}"
    )
    print(f"  Already in queue:      {counts['already_queued_or_recovered']}")
    print(f"  Cleanly completed:     {counts['clean_completed_no_action']}")
    print(f"  Recoverable tracks:    {counts['recoverable_total']} (applied={counts['applied_count']})")
    for r_key, r_cnt in sorted(report["recoverable_reasons"].items()):
        print(f"    - {r_key}: {r_cnt}")

    print(f"  Ambiguous (NOT reset): {counts['ambiguous_total']}")
    for a_key, a_cnt in sorted(report["ambiguous_reasons"].items()):
        print(f"    - {a_key}: {a_cnt}")

    if report["active_provider_cooldowns"]:
        print("  Active provider cooldowns respected:")
        for p_fam, p_info in sorted(report["active_provider_cooldowns"].items()):
            print(f"    - {p_fam}: until {p_info.get('cooldown_until_utc', '')} ({p_info.get('remaining_cooldown_seconds', 0)}s)")

    sample_rows = [
        {
            "file_id": short_file_id(it["file_id"], verbose=verbose),
            "name": it["name"],
            "prior": f"{it['prior_status']} (att={it['prior_attempts']})",
            "cands": str(it["preserved_candidates"]),
            "failed": ",".join(it["failed_providers"]),
            "reason": it["reason"],
        }
        for it in report["recoverable_tracks"][:20]
    ]
    if sample_rows:
        print("\nRecoverable Track Sample (up to 20):")
        cols = [
            ("file_id", "File ID", 15 if not verbose else 35),
            ("name", "Track", 32),
            ("prior", "Prior State", 18),
            ("cands", "Cands", 6),
            ("failed", "Failed Provs", 20),
            ("reason", "Reason", 34),
        ]
        print(render_responsive_records(sample_rows, cols, force_wide=wide))

    if not apply and counts["recoverable_total"] > 0:
        print(
            "\n  [i] Dry-run complete. To apply selectively after creating a fresh verified backup:\n"
            "      musicvault recover-research --apply --create-backup\n"
        )
    elif apply:
        print(f"\n  [✓] Recovery committed ({counts['applied_count']} track(s) re-queued; candidates & hydration preserved).\n")
    else:
        print("\n  [i] No demonstrably incomplete tracks require recovery.\n")

    # Mask Drive IDs by default in exported artifacts unless --verbose is explicitly passed
    masked_report = {
        **report,
        "recoverable_tracks": [
            {**it, "file_id": short_file_id(it["file_id"], verbose=verbose)}
            for it in report["recoverable_tracks"]
        ],
        "ambiguous_tracks": [
            {**it, "file_id": short_file_id(it["file_id"], verbose=verbose)}
            for it in report["ambiguous_tracks"]
        ],
    }
    secrets = config.known_secrets()
    if export_json:
        export_safe_json(export_json, masked_report, secrets)
        print(f"[✓] Exported recovery JSON report to {export_json}")
    if export_csv:
        csv_items: List[Dict[str, Any]] = []
        for it in masked_report["recoverable_tracks"]:
            csv_items.append(
                {
                    "category": "recoverable",
                    "file_id": it["file_id"],
                    "name": it["name"],
                    "status": it["prior_status"],
                    "attempts": it["prior_attempts"],
                    "preserved_candidates": it["preserved_candidates"],
                    "providers": ",".join(it["failed_providers"]),
                    "next_retry_utc": it["next_retry_utc"],
                    "reason": it["reason"],
                }
            )
        for it in masked_report["ambiguous_tracks"]:
            csv_items.append(
                {
                    "category": "ambiguous_not_reset",
                    "file_id": it["file_id"],
                    "name": it["name"],
                    "status": it["research_status"],
                    "attempts": it["attempts"],
                    "preserved_candidates": "",
                    "providers": ",".join(it.get("blocked_providers") or it.get("unqueried_configured_providers") or []),
                    "next_retry_utc": "",
                    "reason": it["reason"],
                }
            )
        if csv_items:
            export_safe_csv(export_csv, list(csv_items[0].keys()), csv_items, secrets)
            print(f"[✓] Exported recovery CSV report to {export_csv}")

    return masked_report


def run_preview(
    config: Config,
    db: Database,
    limit: int = 25,
    wide: bool = False,
    verbose: bool = False,
    export_json: Optional[Path] = None,
    export_csv: Optional[Path] = None,
    drive_override: Optional[Any] = None,
    sheets_override: Optional[Any] = None,
) -> Any:
    """
    Real read-only Google Sheets + Google Drive organisation preview.
    Forces dry-run (0 authorized, 0 executed, 0 cloud writes) regardless of config.
    """
    inv = db.get_inventory_summary()
    live_tracks = db.get_all_tracks(live_only=True)
    if not live_tracks:
        print("\n[!] No live source tracks in database to preview. Run 'musicvault scan' first.\n")
        return None

    selected_tracks = live_tracks[:limit] if (limit and limit > 0) else live_tracks

    if sheets_override is not None and drive_override is not None:
        sh_svc = sheets_override
        dr_svc = drive_override
    else:
        auth_mgr = GoogleAuthManager(config)
        creds = auth_mgr.get_credentials()
        if not creds:
            print("[✗] Google authentication is required for live Sheets/Drive preview.")
            return None
        sh_svc = sheets_override or SheetsService(creds)
        dr_svc = drive_override or DriveService(creds)

    try:
        sheet_index = sh_svc.load_verified_index(config.sheet_id)
    except Exception as exc:
        print(f"[✗] Unable to read Google Sheet 'Tracks' tab (read-only): {config.redact(exc)}")
        return None

    vault_folders: Optional[Dict[str, List[str]]] = None
    try:
        vault_folders = dr_svc.get_children_folders(config.vault_folder_id)
    except Exception as exc:
        print(f"[!] Warning: Could not inspect Vault destination folders ({config.redact(exc)}); folder checks marked provisional.")

    cands_by_id = {t.file_id: db.get_candidates(t.file_id) for t in selected_tracks}
    preview = evaluate_live_preview(
        live_tracks=selected_tracks,
        candidates_by_id=cands_by_id,
        sheet_index=sheet_index,
        source_folder_id=config.source_folder_id,
        vault_folder_id=config.vault_folder_id,
        vault_folders=vault_folders,
        save_medium=config.save_medium,
        provider_terms_cleared=config.provider_terms_cleared,
        quarantined_count=inv["quarantined_records"],
    )

    print(f"\n=== LIVE READ-ONLY ORGANISATION PREVIEW (v{__version__}) ===")
    if not preview.sheet_schema_valid:
        print(f"[✗] SHEET SCHEMA INVALID — ALL TRACKS HELD: {preview.sheet_schema_error}")

    display_records = [
        {
            "row": str(r.row_num) if r.row_num is not None else "-",
            "file_id": short_file_id(r.file_id, verbose=verbose),
            "track": r.track_name,
            "existing_g": r.existing_values.get("genre", ""),
            "destination": r.destination,
            "folder_action": r.folder_action,
            "reason": r.reason,
            "confidence": r.routing_confidence,
            "eligible": str(r.eligible),
            "provisional": str(r.provisional),
            "authorized": str(r.move_authorized),
            "star": r.star,
        }
        for r in preview.rows
    ]
    cols = [
        ("row", "Row", 6),
        ("file_id", "File ID", 15 if not verbose else 35),
        ("track", "Track", 32),
        ("existing_g", "Sheet Genre", 13),
        ("destination", "Parent Dest", 16),
        ("reason", "Reason", 28),
        ("confidence", "Confidence", 18),
        ("eligible", "Elig", 6),
        ("provisional", "Prov", 6),
        ("authorized", "Auth", 6),
    ]
    print(render_responsive_records(display_records, cols, force_wide=wide))
    print(
        f"\nEvaluated: {preview.evaluated_count} of {preview.total_live_tracks} live tracks "
        f"(quarantined excluded: {preview.quarantined_excluded}) | "
        f"Sheet matched: {preview.matched_sheet_rows}, unindexed draft: {preview.unindexed_draft_only}, "
        f"dup-ID holds: {preview.duplicate_sheet_id_holds}, lock-error holds: {preview.malformed_lock_holds}"
    )
    print(
        f"Eligible: {preview.eligible_count} (provisional: {preview.provisional_count}) | "
        f"Move-Authorized: {preview.authorized_count} | Executable: {preview.executable_count} | "
        f"FORCED_DRY_RUN: True (zero Drive/Sheet writes in v{__version__})\n"
    )

    secrets = config.known_secrets()
    if export_json:
        payload = {
            "summary": {k: v for k, v in preview.__dict__.items() if k != "rows"},
            "rows": [
                {
                    **r.__dict__,
                    "file_id": r.file_id if verbose else short_file_id(r.file_id, verbose=False),
                }
                for r in preview.rows
            ],
        }
        export_safe_json(export_json, payload, secrets)
        print(f"[✓] Exported live preview JSON report to {export_json}")
    if export_csv:
        csv_rows = [
            {
                "row": r.row_num if r.row_num is not None else "",
                "file_id": r.file_id if verbose else short_file_id(r.file_id, verbose=False),
                "track_name": r.track_name,
                "existing_genre": r.existing_values.get("genre", ""),
                "existing_subgenre": r.existing_values.get("subgenre", ""),
                "existing_bpm": r.existing_values.get("bpm", ""),
                "existing_key": r.existing_values.get("key", ""),
                "proposed_genre": r.proposed_values.get("genre", ""),
                "proposed_bpm": r.proposed_values.get("bpm", ""),
                "proposed_key": r.proposed_values.get("key", ""),
                "destination": r.destination,
                "folder_action": r.folder_action,
                "reason": r.reason,
                "routing_confidence": r.routing_confidence,
                "eligible": r.eligible,
                "provisional": r.provisional,
                "move_authorized": r.move_authorized,
                "executable": r.executable,
                "star": r.star,
                "evidence_summary": r.evidence_summary,
            }
            for r in preview.rows
        ]
        if csv_rows:
            export_safe_csv(export_csv, list(csv_rows[0].keys()), csv_rows, secrets)
            print(f"[✓] Exported live preview CSV report to {export_csv}")

    return preview


def run_benchmark(
    config: Config,
    db: Optional[Database] = None,
    sample_size: int = 10,
    checkpoint_path: Optional[Path] = None,
    expected_rows_path: Optional[Path] = None,
    wide: bool = False,
    verbose: bool = False,
) -> Any:
    """
    Execute isolated offline fixture regression benchmark (never touches or imports into live DB).
    """
    print(f"\n=== OFFLINE FIXTURE BENCHMARK ({sample_size} TRACKS — ISOLATED DB) ===")
    res = execute_isolated_benchmark(
        config=config,
        sample_size=sample_size,
        checkpoint_path=checkpoint_path,
        expected_rows_path=expected_rows_path,
    )

    records = [
        {
            "row": str(o.row_num) if o.row_num is not None else "-",
            "file_id": short_file_id(o.file_id, verbose=verbose),
            "track": o.track_name,
            "expected": o.expected_destination or "(HOLD)",
            "actual": o.actual_destination or "(HOLD)",
            "reason": o.actual_reason,
            "eligible": str(o.eligible),
            "auth": str(o.move_authorized),
            "check": "PASS" if o.passed is True else ("FAIL" if o.passed is False else "NO_FIXTURE"),
        }
        for o in res.outcomes
    ]
    cols = [
        ("row", "Row", 6),
        ("file_id", "File ID", 15 if not verbose else 35),
        ("track", "Track", 34),
        ("expected", "Expected", 18),
        ("actual", "Actual", 14),
        ("reason", "Reason", 28),
        ("eligible", "Elig", 6),
        ("auth", "Auth", 6),
        ("check", "Check", 10),
    ]
    print(render_responsive_records(records, cols, force_wide=wide))
    print(f"\nBenchmark Status: {res.status}")
    print(f"  {res.notes}")
    print(
        f"  Ground-truth rows evaluated: {res.evaluated_with_ground_truth}/{res.loaded_tracks} | "
        f"Passed: {res.passed_count} | Failed: {res.failed_count} | Missing fixture rows: {res.missing_expected_rows}"
    )
    print(
        f"  Eligible: {res.eligible_count} | Held: {res.held_count} | "
        f"Move-Authorized: {res.authorized_count} | Live DB untouched.\n"
    )
    for o in res.outcomes:
        if o.passed is False:
            print(f"  [FAIL] {o.track_name}: {o.failure_detail}")
    return res
