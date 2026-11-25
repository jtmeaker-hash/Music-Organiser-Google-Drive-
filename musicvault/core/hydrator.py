"""
Bandwidth-aware metadata hydration engine for Music Vault Manager.
Combines preserved same-version tags, bounded ranged Drive reads (with optional opt-in temp download),
verified exact-file-ID master sheet identity, and safe filename hints with explicit provenance.
"""
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config import redact_secrets
from ..db import Database
from ..models import Track, TrackMetadata
from ..services.range_reader import (
    BudgetExceededError,
    ByteBudgetTracker,
    HydrationTransportError,
    InsufficientDiskSpaceError,
    PerFileRangeCapError,
    RangeIgnoredError,
    RemoteVersionChangedError,
)
from .audio import extract_embedded_tags_from_stream
from .matcher import parse_filename_identity


@dataclass
class HydrationSummary:
    """Detailed honest summary of a hydration batch."""
    eligible_pending: int = 0
    attempted: int = 0
    preserved_same_version: int = 0
    hydrated_embedded: int = 0
    hydrated_sheet_identity: int = 0
    hydrated_filename_ambiguous: int = 0
    title_only_unresolved: int = 0
    unresolved_tagless: int = 0
    retryable_failures: int = 0
    permanent_unsupported: int = 0
    paused_budget_exhausted: bool = False
    interrupted: bool = False
    bytes_transferred: int = 0
    byte_budget_cap: int = 0
    errors: List[str] = field(default_factory=list)


def apply_fallback_identity(
    embedded_meta: TrackMetadata,
    track_name: str,
    sheet_identity: Optional[Tuple[str, str]] = None,
) -> Tuple[TrackMetadata, str, str]:
    """
    Combine embedded tag extraction result with verified exact-ID sheet identity and filename hints.
    Never labels sheet-derived or filename-derived fields as 'embedded'.
    Returns (final_metadata, hydration_status, hydration_method_suffix).
    """
    parsed_fn = parse_filename_identity(track_name)
    embedded_meta.filename_artist_hint = parsed_fn.artist
    embedded_meta.filename_title_hint = parsed_fn.title_hint or parsed_fn.title
    embedded_meta.filename_remixer_hint = parsed_fn.remixer_hint

    if sheet_identity:
        s_artist, s_title = sheet_identity
        embedded_meta.sheet_artist = (s_artist or "").strip()
        embedded_meta.sheet_title = (s_title or "").strip()

    # 1. Complete embedded artist + title
    if embedded_meta.artist.strip() and embedded_meta.title.strip():
        embedded_meta.identity_origin = "embedded"
        embedded_meta.identity_confidence = "verified_embedded"
        return embedded_meta, "success", "embedded"

    # 2. Exact-ID-linked master sheet Artist + Title (only when schema-verified)
    if embedded_meta.sheet_artist and embedded_meta.sheet_title:
        embedded_meta.artist = embedded_meta.artist.strip() or embedded_meta.sheet_artist
        embedded_meta.title = embedded_meta.title.strip() or embedded_meta.sheet_title
        embedded_meta.identity_origin = "sheet_exact_id"
        embedded_meta.identity_confidence = "sheet_exact_id"
        return embedded_meta, "partial", "sheet_exact_id"

    # 3. Ambiguous two-part filename ('Artist - Title' or 'Title - Artist')
    if parsed_fn.confidence == "filename_ambiguous" and parsed_fn.artist and parsed_fn.title:
        embedded_meta.artist = embedded_meta.artist.strip() or parsed_fn.artist
        embedded_meta.title = embedded_meta.title.strip() or parsed_fn.title
        embedded_meta.identity_origin = "filename_hint"
        embedded_meta.identity_confidence = "filename_ambiguous"
        return embedded_meta, "partial", "filename_hint"

    # 4. Title-only or remix-qualifier-only filename ('01 Clarity.wav', '01 Beneath The Lights - Darren Styles Remix.wav')
    # Never fabricate artist from remixer or track number!
    if parsed_fn.confidence == "title_only" and parsed_fn.title_hint:
        embedded_meta.identity_origin = "filename_hint"
        embedded_meta.identity_confidence = "title_only"
        return embedded_meta, "unresolved", "title_only_hint"

    # 5. Truly tagless and malformed filename ('-', '-.wav')
    embedded_meta.identity_origin = "unresolved"
    embedded_meta.identity_confidence = "unresolved"
    return embedded_meta, "unresolved", "unresolved"


def hydrate_live_inventory(
    db: Database,
    drive_service: Any,
    limit: int = 10,
    max_mib: float = 32.0,
    max_file_ranged_mib: float = 2.0,
    allow_full_download: bool = False,
    max_full_download_mib: float = 50.0,
    sheet_identities: Optional[Dict[str, Tuple[str, str]]] = None,
    force_rehydrate: bool = False,
    secret_values: Optional[List[str]] = None,
    progress_callback: Optional[Callable[[int, int, Track, str, int], None]] = None,
) -> HydrationSummary:
    """
    Hydrate up to `limit` live pending/incomplete tracks within `max_mib` byte budget.
    Default conservative settings: 10 tracks, 32 MiB total budget, no full-file downloads.
    """
    budget_bytes = max(1024, int(float(max_mib) * 1024 * 1024))
    per_file_ranged_bytes = max(16384, int(float(max_file_ranged_mib) * 1024 * 1024))
    max_full_bytes = max(65536, int(float(max_full_download_mib) * 1024 * 1024))

    budget = ByteBudgetTracker(max_bytes=budget_bytes)
    summary = HydrationSummary(byte_budget_cap=budget_bytes)
    secrets = secret_values or []
    use_sheet_flag = bool(sheet_identities)

    # Count total live candidates genuinely needing hydration, then select up to `limit`
    inv_stats = db.get_inventory_summary()
    summary.eligible_pending = (
        inv_stats["live_source_tracks"]
        if force_rehydrate
        else inv_stats.get("eligible_for_hydration", 0)
    )

    queue = db.get_live_tracks_for_hydration(
        limit=max(1, int(limit)),
        force_rehydrate=force_rehydrate,
        use_sheet_uninspected=use_sheet_flag,
    )
    if not queue:
        return summary

    try:
        for idx, track in enumerate(queue, 1):
            if not track.is_live_source or track.quarantined:
                continue

            ver = track.metadata.version or track.md5 or track.modified_time or ""

            # Step 1: Already-known trustworthy embedded metadata for same version (0 network bytes)
            if (
                not track.content_stale
                and track.metadata.identity_origin == "embedded"
                and track.metadata.artist.strip()
                and track.metadata.title.strip()
                and track.metadata.duration > 0
                and (not ver or track.hydration_version == ver or track.metadata.version == ver)
            ):
                db.update_hydration_result(
                    track.file_id,
                    status="success",
                    version=ver,
                    method="preserved_same_version",
                    bytes_read=0,
                    metadata=track.metadata,
                )
                summary.attempted += 1
                summary.preserved_same_version += 1
                if progress_callback:
                    progress_callback(idx, len(queue), track, "preserved_same_version", budget.bytes_used)
                continue

            if budget.exhausted:
                summary.paused_budget_exhausted = True
                break

            before_bytes = budget.bytes_used
            summary.attempted += 1
            sheet_pair = (sheet_identities or {}).get(track.file_id)

            try:
                stream = drive_service.open_range_reader(
                    file_id=track.file_id,
                    budget=budget,
                    known_size=track.size,
                    expected_md5=track.md5,
                    max_file_bytes=per_file_ranged_bytes,
                )
                try:
                    extracted_meta, recognized = extract_embedded_tags_from_stream(
                        stream, filename=track.name, version_id=ver
                    )
                finally:
                    stream.close()

                spent = budget.bytes_used - before_bytes

                if not recognized:
                    if allow_full_download:
                        extracted_meta, recognized = _attempt_opt_in_full_download(
                            drive_service, track, ver, budget, max_full_bytes
                        )
                        spent = budget.bytes_used - before_bytes
                        method_base = "embedded_full"
                    else:
                        final_meta, _, _ = apply_fallback_identity(
                            TrackMetadata(version=ver), track.name, sheet_pair
                        )
                        db.update_hydration_result(
                            track.file_id,
                            status="permanent_unsupported",
                            version=ver,
                            method="ranged_unsupported_container",
                            bytes_read=spent,
                            error_msg="Unrecognized or unsupported audio container via ranged read",
                            metadata=final_meta,
                        )
                        summary.permanent_unsupported += 1
                        summary.bytes_transferred = budget.bytes_used
                        if progress_callback:
                            progress_callback(idx, len(queue), track, "permanent_unsupported", budget.bytes_used)
                        continue
                else:
                    method_base = "embedded_ranged"

                final_meta, hyd_status, method_suffix = apply_fallback_identity(
                    extracted_meta, track.name, sheet_pair
                )
                method = method_base if method_suffix == "embedded" else f"{method_base}+{method_suffix}"
                db.update_hydration_result(
                    track.file_id,
                    status=hyd_status,
                    version=ver,
                    method=method,
                    bytes_read=spent,
                    metadata=final_meta,
                )
                _tally_outcome(summary, final_meta, hyd_status)

            except (RangeIgnoredError, PerFileRangeCapError) as range_exc:
                spent = budget.bytes_used - before_bytes
                if allow_full_download and not budget.exhausted:
                    try:
                        extracted_meta, recognized = _attempt_opt_in_full_download(
                            drive_service, track, ver, budget, max_full_bytes
                        )
                        spent = budget.bytes_used - before_bytes
                        if recognized:
                            final_meta, hyd_status, method_suffix = apply_fallback_identity(
                                extracted_meta, track.name, sheet_pair
                            )
                            db.update_hydration_result(
                                track.file_id,
                                status=hyd_status,
                                version=ver,
                                method=f"full_download+{method_suffix}",
                                bytes_read=spent,
                                metadata=final_meta,
                            )
                            _tally_outcome(summary, final_meta, hyd_status)
                        else:
                            db.update_hydration_result(
                                track.file_id,
                                status="permanent_unsupported",
                                version=ver,
                                method="full_download_unsupported",
                                bytes_read=spent,
                                error_msg="Unsupported audio data after opt-in full download",
                            )
                            summary.permanent_unsupported += 1
                    except BudgetExceededError:
                        summary.paused_budget_exhausted = True
                        err_clean = redact_secrets(range_exc, secrets)
                        db.update_hydration_result(
                            track.file_id,
                            status="retryable_failure",
                            version=ver,
                            method="paused_budget",
                            bytes_read=budget.bytes_used - before_bytes,
                            error_msg=err_clean,
                            retry_delay_seconds=60,
                        )
                        summary.retryable_failures += 1
                        break
                    except (InsufficientDiskSpaceError, HydrationTransportError) as dl_exc:
                        err_clean = redact_secrets(dl_exc, secrets)
                        db.update_hydration_result(
                            track.file_id,
                            status="retryable_failure",
                            version=ver,
                            method="full_download_failed",
                            bytes_read=budget.bytes_used - before_bytes,
                            error_msg=err_clean,
                            retry_delay_seconds=300,
                        )
                        summary.retryable_failures += 1
                        summary.errors.append(f"{track.name}: {err_clean}")
                else:
                    # Record safe sheet/filename fallback hints while keeping retryable_failure so backoff & max_attempts apply
                    fallback_meta, _, _ = apply_fallback_identity(
                        TrackMetadata(version=ver), track.name, sheet_pair
                    )
                    err_clean = redact_secrets(range_exc, secrets)
                    db.update_hydration_result(
                        track.file_id,
                        status="retryable_failure",
                        version=ver,
                        method="range_capped_fallback",
                        bytes_read=spent,
                        error_msg=err_clean,
                        retry_delay_seconds=300,
                        metadata=fallback_meta,
                    )
                    summary.retryable_failures += 1
                    summary.errors.append(f"{track.name}: {err_clean}")

            except RemoteVersionChangedError as ver_exc:
                spent = budget.bytes_used - before_bytes
                err_clean = redact_secrets(ver_exc, secrets)
                db.update_hydration_result(
                    track.file_id,
                    status="retryable_failure",
                    version=ver,
                    method="version_changed_mid_read",
                    bytes_read=spent,
                    error_msg=err_clean,
                    retry_delay_seconds=60,
                )
                summary.retryable_failures += 1
                summary.errors.append(f"{track.name}: {err_clean}")

            except BudgetExceededError as bud_exc:
                spent = budget.bytes_used - before_bytes
                err_clean = redact_secrets(bud_exc, secrets)
                db.update_hydration_result(
                    track.file_id,
                    status="retryable_failure",
                    version=ver,
                    method="paused_budget_exhausted",
                    bytes_read=spent,
                    error_msg=err_clean,
                    retry_delay_seconds=60,
                )
                summary.paused_budget_exhausted = True
                summary.bytes_transferred = budget.bytes_used
                break

            except Exception as exc:
                spent = budget.bytes_used - before_bytes
                err_clean = redact_secrets(exc, secrets)
                db.update_hydration_result(
                    track.file_id,
                    status="retryable_failure",
                    version=ver,
                    method="transport_error",
                    bytes_read=spent,
                    error_msg=err_clean,
                    retry_delay_seconds=180,
                )
                summary.retryable_failures += 1
                summary.errors.append(f"{track.name}: {err_clean}")

            summary.bytes_transferred = budget.bytes_used
            if progress_callback:
                updated_t = db.get_track(track.file_id) or track
                progress_callback(idx, len(queue), updated_t, updated_t.hydration_status, budget.bytes_used)

    except KeyboardInterrupt:
        summary.interrupted = True
        summary.bytes_transferred = budget.bytes_used

    summary.bytes_transferred = budget.bytes_used
    return summary


def _attempt_opt_in_full_download(
    drive_service: Any,
    track: Track,
    version_id: str,
    budget: ByteBudgetTracker,
    max_full_bytes: int,
) -> Tuple[TrackMetadata, bool]:
    """Download to an isolated temporary directory and guarantee cleanup on exit/failure."""
    suffix = Path(track.name or "audio.bin").suffix or ".bin"
    with tempfile.TemporaryDirectory(prefix="musicvault_hyd_") as tmp_dir:
        tmp_path = Path(tmp_dir) / f"payload{suffix}"
        try:
            drive_service.download_bounded_temp_file(
                file_id=track.file_id,
                dest_path=tmp_path,
                budget=budget,
                expected_size=track.size,
                max_full_bytes=max_full_bytes,
            )
            with tmp_path.open("rb") as fh:
                return extract_embedded_tags_from_stream(fh, filename=track.name, version_id=version_id)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


def _tally_outcome(summary: HydrationSummary, meta: TrackMetadata, status: str) -> None:
    if meta.identity_confidence == "verified_embedded" and status == "success":
        summary.hydrated_embedded += 1
    elif meta.identity_confidence == "sheet_exact_id":
        summary.hydrated_sheet_identity += 1
    elif meta.identity_confidence == "filename_ambiguous":
        summary.hydrated_filename_ambiguous += 1
    elif meta.identity_confidence == "title_only":
        summary.title_only_unresolved += 1
    else:
        summary.unresolved_tagless += 1
