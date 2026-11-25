"""
Drive routing, folder hierarchy organization, read-only live preview planning, and rollback manifests.
Separates eligible, provisional, move_authorized, and executable states and enforces parent-genre-only
routing at the v2.0.1 live preview boundary with zero cloud writes.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from ..models import Candidate, MovePlanItem, RoutingConfidence, Track
from ..providers.base import canonical_provider_family
from .genre import GENERIC_TAGS, SUBGENRE_PARENT, canonical_genre, classify_track_genres, decide_genre
from .matcher import norm
from .rules import build_track_proposal, load_existing_states, parse_lock_states, planned_row


def safe_segment(s: Any) -> Optional[str]:
    """Validate that folder path segment is safe for Drive and Android."""
    seg = str(s or "").strip()
    if not seg or seg in (".", "..") or "/" in seg or "\\" in seg or len(seg) > 80:
        return None
    return seg


def routing_choice(old_row: List[Any]) -> Tuple[Optional[List[str]], str, str]:
    """
    Pure helper: determine destination folder path, reason, and routing confidence from a sheet row.
    Preserves v1.3 nested/broad unit-test semantics; live preview enforces parent-only at boundary.
    """
    row = list(old_row) + [""] * max(0, 27 - len(old_row))
    states, lock_err = parse_lock_states(row)
    if lock_err is not None:
        return None, "MALFORMED_LOCK_JSON", "HOLD"

    raw_genre = str(row[2]).strip()
    genre = canonical_genre(raw_genre)
    sub = str(row[3]).strip()

    if not genre:
        return None, "UNKNOWN_OR_UNSUPPORTED_GENRE", "HOLD"

    gs = states.get("genre", {}).get("state", "")
    ss = states.get("subgenre", {}).get("state", "")

    if gs in ("CONFLICT", "LOW_CONFLICT", "REJECTED") or ss in ("CONFLICT", "LOW_CONFLICT", "REJECTED"):
        return None, "METADATA_CONFLICT", "HOLD"

    if gs in ("HIGH_LOCKED", "USER_LOCKED"):
        if sub and ss in ("HIGH_LOCKED", "USER_LOCKED") and SUBGENRE_PARENT.get(sub) == genre:
            return [genre, sub], "GENRE_AND_SUBGENRE_VERIFIED", "VERIFIED_NESTED"
        return [genre], "BROAD_GENRE_ONLY", "VERIFIED_BROAD"

    if gs == "MEDIUM_PROVISIONAL":
        return [genre], "PROVISIONAL_MEDIUM_GENRE", "PROVISIONAL_REVIEW"

    return [genre], "PROVISIONAL_EXISTING_GENRE", "PROVISIONAL_REVIEW"


def build_move_plan(
    file_id: str,
    track_name: str,
    row_num: Optional[int],
    sheet_row: List[Any],
    allow_provisional: bool = False,
    dry_run: bool = True,
    read_only_build: bool = True,
) -> MovePlanItem:
    """
    Construct move plan item for a single track.
    Enforces zero authorization whenever dry_run=True or read_only_build=True,
    even for verified rows or if legacy config had ALLOW_PROVISIONAL_MOVES=True.
    """
    parts, reason, confidence = routing_choice(sheet_row)
    dest_str = "/".join(parts) if parts else ""
    eligible = bool(parts) and row_num is not None
    provisional = eligible and (confidence == "PROVISIONAL_REVIEW")

    if dry_run or read_only_build:
        authorized = False
        executable = False
    else:
        authorized = eligible and (not provisional or allow_provisional)
        executable = authorized

    return MovePlanItem(
        row_num=row_num,
        file_id=file_id,
        track_name=track_name,
        destination=dest_str,
        destination_segments=parts or [],
        reason=reason,
        routing_confidence=RoutingConfidence(confidence),
        eligible=eligible,
        move_authorized=authorized,
        provisional=provisional,
        executable=executable,
        existing_genre=str(sheet_row[2]).strip() if len(sheet_row) > 2 else "",
    )


@dataclass
class LivePreviewRowReport:
    """Full auditable preview record joining live Drive track, real Sheet row, and candidates."""
    row_num: Optional[int]
    file_id: str
    track_name: str
    existing_values: Dict[str, str]
    proposed_values: Dict[str, Any]
    field_decisions: Dict[str, str]
    would_fill: List[str]
    star: str
    destination: str
    folder_action: str
    reason: str
    routing_confidence: str
    eligible: bool
    provisional: bool
    move_authorized: bool
    executable: bool
    evidence_summary: str


@dataclass
class LivePreviewSummary:
    """Summary of read-only live Sheets + Drive organisation preview."""
    forced_dry_run: bool = True
    sheet_schema_valid: bool = True
    sheet_schema_error: str = ""
    total_live_tracks: int = 0
    quarantined_excluded: int = 0
    evaluated_count: int = 0
    matched_sheet_rows: int = 0
    unindexed_draft_only: int = 0
    duplicate_sheet_id_holds: int = 0
    malformed_lock_holds: int = 0
    stale_evidence_holds: int = 0
    conflict_holds: int = 0
    eligible_count: int = 0
    provisional_count: int = 0
    authorized_count: int = 0
    executable_count: int = 0
    rows: List[LivePreviewRowReport] = field(default_factory=list)


def evaluate_live_preview(
    live_tracks: List[Track],
    candidates_by_id: Dict[str, List[Candidate]],
    sheet_index: Any,
    source_folder_id: str,
    vault_folder_id: str,
    vault_folders: Optional[Dict[str, List[str]]] = None,
    save_medium: bool = True,
    provider_terms_cleared: bool = False,
    quarantined_count: int = 0,
) -> LivePreviewSummary:
    """
    Build real read-only organisation & spreadsheet preview from live source inventory.
    - Forces dry-run (move_authorized=False, executable=False) unconditionally in v2.0.1.
    - Enforces parent-genre-only destinations (never descends into subgenres, preserves subgenre cells).
    - Never fabricates row 999 or matches rows by title.
    - Fails closed on incompatible sheet headers, duplicate IDs, malformed lock JSON, stale evidence,
      missing parents, source==vault misconfiguration, or metadata conflicts.
    """
    summary = LivePreviewSummary(
        forced_dry_run=True,
        sheet_schema_valid=bool(sheet_index.schema_valid),
        sheet_schema_error=sheet_index.schema_error,
        total_live_tracks=len(live_tracks),
        quarantined_excluded=quarantined_count,
    )

    folder_lookup: Dict[str, List[Tuple[str, str]]] = {}
    if vault_folders:
        for fname, fids in vault_folders.items():
            key = canonical_genre(fname) or norm(fname)
            for fid in fids:
                folder_lookup.setdefault(key, []).append((fname, fid))

    source_vault_invalid = (
        not source_folder_id.strip()
        or not vault_folder_id.strip()
        or source_folder_id.strip() == vault_folder_id.strip()
    )

    for t in live_tracks:
        if not t.is_live_source or t.quarantined:
            continue
        summary.evaluated_count += 1
        cands = candidates_by_id.get(t.file_id, [])
        proposal = build_track_proposal(t.metadata, cands)

        # Build concise provenance/evidence summary
        ev_parts: List[str] = []
        if t.metadata.identity_origin and t.metadata.identity_origin != "unresolved":
            ev_parts.append(f"id={t.metadata.identity_origin}")
        for c in cands:
            ev_parts.append(f"{c.provider}:{c.verification}")
        ev_summary = "; ".join(dict.fromkeys(ev_parts)) or "no_provider_candidates"

        def _make_hold_row(
            r_num: Optional[int],
            existing_map: Dict[str, str],
            f_decisions: Dict[str, str],
            hold_reason: str,
            draft_dest: str = "",
            f_action: str = "HOLD",
            star_val: str = "★",
            is_prov: bool = False,
        ) -> LivePreviewRowReport:
            return LivePreviewRowReport(
                row_num=r_num,
                file_id=t.file_id,
                track_name=t.name,
                existing_values=existing_map,
                proposed_values=proposal,
                field_decisions=f_decisions,
                would_fill=[],
                star=star_val,
                destination=draft_dest,
                folder_action=f_action,
                reason=hold_reason,
                routing_confidence=RoutingConfidence.HOLD.value,
                eligible=False,
                provisional=is_prov,
                move_authorized=False,
                executable=False,
                evidence_summary=ev_summary,
            )

        empty_existing = {"bpm": "", "key": "", "genre": "", "subgenre": ""}

        # Global pre-flight gates
        if not sheet_index.schema_valid:
            summary.rows.append(
                _make_hold_row(None, empty_existing, {}, sheet_index.schema_error or "INCOMPATIBLE_HEADERS")
            )
            continue

        if source_vault_invalid:
            summary.rows.append(
                _make_hold_row(None, empty_existing, {}, "INVALID_SOURCE_VAULT_SEPARATION")
            )
            continue

        obs_parent = (t.observed_parent_id or t.parent_id or "").strip()
        if not obs_parent:
            summary.rows.append(_make_hold_row(None, empty_existing, {}, "MISSING_DRIVE_PARENT"))
            continue
        if obs_parent == vault_folder_id.strip():
            summary.rows.append(_make_hold_row(None, empty_existing, {}, "ALREADY_IN_VAULT_ROOT"))
            continue

        if t.content_stale:
            summary.stale_evidence_holds += 1
            summary.rows.append(
                _make_hold_row(None, empty_existing, {}, "STALE_EVIDENCE_REHYDRATE_REQUIRED")
            )
            continue

        if t.file_id in sheet_index.duplicate_ids:
            summary.duplicate_sheet_id_holds += 1
            dup_rows = ",".join(str(r) for r in sheet_index.duplicate_ids[t.file_id])
            summary.rows.append(
                _make_hold_row(None, empty_existing, {}, f"DUPLICATE_SHEET_FILE_ID(rows:{dup_rows})")
            )
            continue

        # Unindexed file (not in Tracks sheet): never use row 999 or create rows; draft proposal only!
        if t.file_id not in sheet_index.unique_rows_by_id:
            summary.unindexed_draft_only += 1
            draft_g = canonical_genre(proposal.get("genre", ""))
            summary.rows.append(
                _make_hold_row(
                    None,
                    empty_existing,
                    {f: "DRAFT_UNINDEXED" for f in ("bpm", "key", "genre", "subgenre")},
                    "NO_MATCHING_SHEET_ROW_DRAFT_ONLY",
                    draft_dest=f"[DRAFT] {draft_g}" if draft_g else "",
                    f_action="HOLD_UNINDEXED",
                    is_prov=bool(draft_g),
                )
            )
            continue

        # Matched exact file_id in verified Tracks sheet!
        summary.matched_sheet_rows += 1
        row_num, old_row = sheet_index.unique_rows_by_id[t.file_id]
        existing_vals = {
            "genre": old_row[2].strip(),
            "subgenre": old_row[3].strip(),
            "bpm": old_row[4].strip(),
            "key": old_row[5].strip(),
        }

        states, lock_err = parse_lock_states(old_row)
        if lock_err is not None:
            summary.malformed_lock_holds += 1
            summary.rows.append(
                _make_hold_row(
                    row_num,
                    existing_vals,
                    {f: "LOCK_ERROR_HOLD" for f in ("bpm", "key", "genre", "subgenre")},
                    "MALFORMED_LOCK_JSON",
                )
            )
            continue

        new_row, f_decisions, changed = planned_row(
            candidates=cands,
            proposal_row=proposal,
            old_row=old_row,
            save_medium=save_medium,
            provider_terms_cleared=provider_terms_cleared,
        )

        # Collect recording-supported catalogue genres and check novel 2-provider proposals
        known_cat_genres: List[str] = []
        novel_by_tag: Dict[str, Set[str]] = {}
        for c in cands:
            v_low = (c.verification or "").lower()
            if "conflict" in v_low or "mismatch" in v_low or "inferred" in v_low or "title-only" in v_low:
                continue
            fam = c.source_family or canonical_provider_family(c.provider)
            if fam == "wikidata":
                continue
            is_rec_verified = c.verification in ("ISRC supported", "Duration supported")
            for raw_tag in (c.genre or []):
                if not isinstance(raw_tag, str) or not raw_tag.strip():
                    continue
                cg = canonical_genre(raw_tag)
                if cg and (is_rec_verified or c.provider == "Discogs release"):
                    known_cat_genres.append(cg)
                elif (
                    not cg
                    and is_rec_verified
                    and norm(raw_tag) not in GENERIC_TAGS
                    and re.fullmatch(r"[A-Za-z][A-Za-z &-]{1,45}", raw_tag.strip())
                ):
                    novel_by_tag.setdefault(raw_tag.strip().title(), set()).add(fam)

        gs = states.get("genre", {}).get("state", "")
        ss = states.get("subgenre", {}).get("state", "")
        if gs in ("CONFLICT", "LOW_CONFLICT", "REJECTED") or ss in ("CONFLICT", "LOW_CONFLICT", "REJECTED"):
            summary.conflict_holds += 1
            summary.rows.append(
                _make_hold_row(row_num, existing_vals, f_decisions, "METADATA_LOCK_CONFLICT", star_val=new_row[16] or "★")
            )
            continue

        is_genre_locked = gs in ("HIGH_LOCKED", "USER_LOCKED")
        decided_parent, genre_reason = decide_genre(existing_vals["genre"], known_cat_genres, locked=is_genre_locked)

        # Cross-check artist-profile inference conflict on unverified existing sheet genres (v1.4 policy)
        inferred_hint = canonical_genre(proposal.get("genre", ""))
        if genre_reason == "EXISTING_ONLY_REVIEW" and inferred_hint and inferred_hint != decided_parent:
            summary.conflict_holds += 1
            summary.rows.append(
                _make_hold_row(row_num, existing_vals, f_decisions, "INFERENCE_CONFLICT_REVIEW", star_val="★")
            )
            continue

        if genre_reason in ("CONFLICT", "USER_LOCKED_DISCREPANCY"):
            summary.conflict_holds += 1
            summary.rows.append(
                _make_hold_row(row_num, existing_vals, f_decisions, genre_reason, star_val="★")
            )
            continue

        # Check novel genre proposal (requires >= 2 independent recording-supported catalogue sources; never auto-creates folder!)
        if not decided_parent:
            credible_novels = {tag: fams for tag, fams in novel_by_tag.items() if len(fams) >= 2}
            if len(credible_novels) == 1:
                novel_name = next(iter(credible_novels))
                summary.rows.append(
                    _make_hold_row(
                        row_num,
                        existing_vals,
                        f_decisions,
                        "NOVEL_GENRE_REVIEW_PROPOSAL",
                        draft_dest=f"[PROPOSE_NEW] {novel_name}",
                        f_action="PROPOSE_CREATE_REVIEW",
                        star_val="★",
                        is_prov=True,
                    )
                )
                continue

            summary.rows.append(
                _make_hold_row(row_num, existing_vals, f_decisions, "UNKNOWN_OR_UNSUPPORTED_GENRE", star_val=new_row[16] or "★")
            )
            continue

        # Parent-genre-only enforcement at live preview boundary!
        if not safe_segment(decided_parent):
            summary.rows.append(
                _make_hold_row(row_num, existing_vals, f_decisions, "UNSAFE_DESTINATION_SEGMENT")
            )
            continue

        # Validate destination against Vault folder listing when available
        folder_action = "REUSE_EXISTING"
        if vault_folders is not None:
            matched_folders = folder_lookup.get(decided_parent, []) or folder_lookup.get(norm(decided_parent), [])
            if len(matched_folders) > 1:
                summary.rows.append(
                    _make_hold_row(row_num, existing_vals, f_decisions, "AMBIGUOUS_DUPLICATE_VAULT_FOLDER")
                )
                continue
            if len(matched_folders) == 1:
                actual_folder_name, actual_folder_id = matched_folders[0]
                if actual_folder_id == source_folder_id.strip():
                    summary.rows.append(
                        _make_hold_row(row_num, existing_vals, f_decisions, "DESTINATION_EQUALS_SOURCE")
                    )
                    continue
                decided_parent = actual_folder_name
                folder_action = "REUSE_EXISTING"
            else:
                # Folder does not yet exist in Vault -> never create folder in preview; propose creation for review
                folder_action = "PROPOSE_CREATE_PARENT_FOLDER"

        if is_genre_locked:
            r_conf = RoutingConfidence.VERIFIED_BROAD.value
            r_why = "BROAD_PARENT_GENRE_VERIFIED"
            is_prov = False
        elif gs == "MEDIUM_PROVISIONAL" or genre_reason in ("AGREEMENT", "NEW_CATALOGUE_CANDIDATE"):
            r_conf = RoutingConfidence.PROVISIONAL_REVIEW.value
            r_why = genre_reason if genre_reason != "EXISTING_ONLY_REVIEW" else "PROVISIONAL_MEDIUM_GENRE"
            is_prov = True
        else:
            r_conf = RoutingConfidence.PROVISIONAL_REVIEW.value
            r_why = "PROVISIONAL_EXISTING_GENRE"
            is_prov = True

        # Eligible only if parent genre resolved cleanly; NEVER authorized or executable in v2.0.1!
        is_eligible = True
        summary.eligible_count += 1
        if is_prov:
            summary.provisional_count += 1

        summary.rows.append(
            LivePreviewRowReport(
                row_num=row_num,
                file_id=t.file_id,
                track_name=t.name,
                existing_values=existing_vals,
                proposed_values=proposal,
                field_decisions=f_decisions,
                would_fill=changed,
                star=new_row[16],
                destination=decided_parent,
                folder_action=folder_action,
                reason=r_why,
                routing_confidence=r_conf,
                eligible=is_eligible,
                provisional=is_prov,
                move_authorized=False,
                executable=False,
                evidence_summary=ev_summary,
            )
        )

    return summary


def create_rollback_manifest(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Generate a rollback manifest before executing any future approved Drive moves."""
    return {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "PRE_MOVE",
        "entries": entries,
    }
