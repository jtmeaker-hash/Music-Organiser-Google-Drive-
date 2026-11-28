"""
Controlled Google Sheets Write Preview, Approval-Gated `Tracks` Execution,
`Artist_Rules` Tab Synchronization, and Deterministic Rollback.

Safety Invariants:
1. Physical Google Drive operations (moves, renames, deletions, folder creation) are NEVER touched
   or enabled here — forced dry-run protection holds unconditionally.
2. Only validated `HIGH` and `MEDIUM` confidence metadata may populate `Tracks` (`Genre`, `Subgenre`,
   `BPM`, `Key`). `LOW` confidence results, artist-derived uncorroborated tags, `TERMS_HOLD` items,
   and unresolved conflicts remain strictly in the review pipeline.
3. Manually verified / locked fields (`USER_LOCKED`, `HIGH_LOCKED`, `MOVED`/`ANALYSED`, verified `M`/`P`
   notes, `REVIEW_HOLD`) and concrete existing metadata values are strictly protected; lower-quality
   or contradictory evidence NEVER overwrites existing data.
4. Live `Tracks` updates require an explicit `preview_sha256` confirmation matching the generated
   before-and-after preview AND optimistic pre-flight verification that target rows have not drifted.
5. Every controlled write records a 0600 JSON pre-write backup + SQLite `rollback_manifests` entry
   and uses `valueInputOption="RAW"` so spreadsheet formulas can never execute.
"""
import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ..cli.formatting import sanitize_csv_cell
from ..config import Config
from ..db import Database
from ..models import Candidate, Track
from ..providers.base import canonical_provider_family
from ..services.sheets import (
    ARTIST_RULES_HEADERS,
    ARTIST_RULES_TAB_NAME,
    VerifiedSheetIndex,
    _col_letter,
)
from .artist_rules import ArtistRule, artist_rule_to_sheet_row, load_artist_rules
from .provider_policy import _families
from .rules import (
    COL,
    EXTRA_HEADERS,
    FIELDS,
    STATE_COL,
    build_track_proposal,
    candidate_field_values,
    field_decision,
    parse_lock_states,
    planned_row,
)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _disabled_provider_families(config: Config) -> Set[str]:
    raw = (config.get("DISABLED_RESEARCH_PROVIDERS", "") or "").strip()
    out = set(_families(raw.split(","), allow_wikidata=True)) if raw else set()
    if not config.get_bool("HDRR_ENABLED", True):
        out.add("hdrr")
    return out


def _compute_preview_sha256(
    spreadsheet_id: str,
    row_updates: List[Dict[str, Any]],
    header_writes: Optional[List[Dict[str, Any]]] = None,
) -> str:
    canonical_items = [
        {
            "row_num": item["row_num"],
            "file_id": item["file_id"],
            "cell_writes": item["cell_writes"],
        }
        for item in sorted(row_updates, key=lambda x: int(x["row_num"]))
    ]
    canonical_headers = [
        {"range": hw["range"], "before": hw["before"], "after": hw["after"]}
        for hw in sorted(header_writes or [], key=lambda x: int(x["col_index_0"]))
    ]
    blob = json.dumps(
        {"spreadsheet_id": spreadsheet_id, "headers": canonical_headers, "updates": canonical_items},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _field_provenance_sources(
    track: Track,
    candidates: List[Candidate],
    field: str,
    proposal: Dict[str, Any],
) -> List[str]:
    override_fields = set(proposal.get("artist_rule_override_fields") or [])
    if field in override_fields:
        return ["artist_rule"]
    parent_g = str(proposal.get("genre", "") or "")
    matched = candidate_field_values(
        candidates, field, parent_genre=parent_g if field == "subgenre" else ""
    )
    fams = sorted({x[1] for x in matched if x[1]})
    if fams:
        if field == "subgenre" and any(
            canonical_provider_family(c.source_family or c.provider) == "hdrr"
            and any(g == proposal.get("subgenre") for g in (c.genre or []))
            for c in candidates
        ):
            fams = sorted(set(fams) | {"hdrr_corroborated"})
        return fams
    if field == "bpm" and track.metadata.embedded_bpm:
        return ["embedded"]
    if field == "key" and track.metadata.embedded_key:
        return ["embedded"]
    return []


def build_tracks_write_preview(
    db: Database,
    config: Config,
    sheet_index: VerifiedSheetIndex,
    artist_rules: Optional[Iterable[ArtistRule]] = None,
    raw_rows_by_num: Optional[Dict[int, List[str]]] = None,
) -> Dict[str, Any]:
    """
    Generate a complete, non-destructive before-and-after write preview for `Tracks` and `Artist_Rules`.
    Performs ZERO cloud writes.
    """
    active_rules = (
        [r for r in artist_rules if r.is_active]
        if artist_rules is not None
        else load_artist_rules(db=db, config=config, active_only=True)
    )
    all_configured_rules = (
        list(artist_rules)
        if artist_rules is not None
        else load_artist_rules(db=db, config=config, active_only=False)
    )
    disabled = _disabled_provider_families(config)
    save_medium = config.save_medium
    terms_cleared = config.provider_terms_cleared

    if not sheet_index.schema_valid:
        return {
            "schema_valid": False,
            "schema_error": sheet_index.schema_error,
            "spreadsheet_id": config.sheet_id,
            "generated_utc": _utc_iso(),
            "forced_drive_dry_run": True,
            "preview_sha256": _compute_preview_sha256(config.sheet_id, []),
            "summary": {"eligible_row_updates": 0, "eligible_cell_writes": 0},
            "proposed_row_updates": [],
            "terms_held_candidates": [],
            "discrepancies_held_for_review": [],
            "low_or_conflict_held_sample": [],
            "artist_rules_preview": [artist_rule_to_sheet_row(r) for r in all_configured_rules if r.approval_status == "approved"],
        }

    # Map canonical field indices -> physical 0-based sheet column indices
    phys_col_map: Dict[str, int] = {
        f_name: sheet_index.columns.get(canon_idx, canon_idx)
        for f_name, canon_idx in COL.items()
    }
    phys_drive_col = sheet_index.drive_id_col

    # Check whether review columns Q:AA (indices 16..26) are safe to populate on updated rows
    padded_headers = sheet_index.headers + [""] * max(0, 27 - len(sheet_index.headers))
    review_cols_compatible = not any(
        padded_headers[16 + off] and padded_headers[16 + off] != expected
        for off, expected in enumerate(EXTRA_HEADERS)
    )
    review_headers_missing = review_cols_compatible and not all(
        padded_headers[16 + off] == expected for off, expected in enumerate(EXTRA_HEADERS)
    )

    tracks_by_id = {t.file_id: t for t in db.get_all_tracks(live_only=False)}

    proposed_row_updates: List[Dict[str, Any]] = []
    terms_held_candidates: List[Dict[str, Any]] = []
    discrepancies_held: List[Dict[str, Any]] = []
    low_or_conflict_held: List[Dict[str, Any]] = []

    counts = {
        "total_indexed_unique_rows": len(sheet_index.unique_rows_by_id),
        "duplicate_id_rows_skipped": sum(len(v) for v in sheet_index.duplicate_ids.values()),
        "missing_id_rows_skipped": len(sheet_index.missing_id_rows),
        "quarantined_or_stale_skipped": 0,
        "malformed_lock_rows_held": 0,
        "locked_fields_protected": 0,
        "existing_values_protected": 0,
        "terms_hold_fields_held": 0,
        "low_or_conflict_fields_held": 0,
        "discrepancy_flags_held": 0,
        "eligible_row_updates": 0,
        "eligible_field_updates": 0,
        "eligible_cell_writes": 0,
    }

    for fid, (row_num, canon_row) in sorted(sheet_index.unique_rows_by_id.items(), key=lambda kv: kv[1][0]):
        tr = tracks_by_id.get(fid)
        if tr is None or tr.quarantined or tr.content_stale:
            if tr is not None and (tr.quarantined or tr.content_stale):
                counts["quarantined_or_stale_skipped"] += 1
            continue

        states_check, lock_err = parse_lock_states(canon_row)
        if lock_err is not None:
            counts["malformed_lock_rows_held"] += 1
            continue

        cands = [
            c
            for c in db.get_candidates(fid)
            if not c.is_stale
            and (not c.content_version or not tr.metadata.version or c.content_version == tr.metadata.version)
            and canonical_provider_family(c.source_family or c.provider) not in disabled
        ]
        proposal = build_track_proposal(
            tr.metadata, cands, artist_rules=active_rules, file_id=fid
        )
        new_canon_row, f_decisions, changed_fields = planned_row(
            candidates=cands,
            proposal_row=proposal,
            old_row=canon_row,
            save_medium=save_medium,
            provider_terms_cleared=terms_cleared,
            artist_rules=active_rules,
        )

        # Also inspect what WOULD qualify if provider retention terms were cleared (for transparent reporting)
        _, f_decisions_if_terms_ok, changed_if_terms_ok = planned_row(
            candidates=cands,
            proposal_row=proposal,
            old_row=canon_row,
            save_medium=save_medium,
            provider_terms_cleared=True,
            artist_rules=active_rules,
        )

        for f_name in FIELDS:
            dec = f_decisions.get(f_name, "")
            if dec == "LOCKED":
                counts["locked_fields_protected"] += 1
            elif dec in ("EXISTING_UNVERIFIED", "MEDIUM_PROVISIONAL", "HIGH_LOCKED") and f_name not in changed_fields:
                counts["existing_values_protected"] += 1
            elif dec == "EXISTING_CONFLICT_REVIEW":
                counts["existing_values_protected"] += 1
                counts["discrepancy_flags_held"] += 1
                lvl_d, rsn_d = field_decision(cands, f_name, proposal)
                discrepancies_held.append({
                    "row_num": row_num,
                    "file_id": fid,
                    "track_name": tr.name,
                    "field": f_name,
                    "existing_value_preserved": canon_row[COL[f_name]].strip(),
                    "conflicting_proposal_not_written": str(proposal.get(f_name, "") or ""),
                    "confidence": lvl_d,
                    "provenance": _field_provenance_sources(tr, cands, f_name, proposal),
                    "reason": rsn_d,
                })
            elif dec == "TERMS_HOLD":
                counts["terms_hold_fields_held"] += 1
                if f_name in changed_if_terms_ok:
                    lvl_t, rsn_t = field_decision(cands, f_name, proposal)
                    terms_held_candidates.append({
                        "row_num": row_num,
                        "file_id": fid,
                        "track_name": tr.name,
                        "field": f_name,
                        "before": canon_row[COL[f_name]].strip(),
                        "would_write_if_terms_cleared": str(proposal.get(f_name, "") or ""),
                        "confidence": lvl_t,
                        "provenance": _field_provenance_sources(tr, cands, f_name, proposal),
                        "reason": rsn_t,
                    })
            elif dec in ("REVIEW", "REVIEW_HOLD"):
                counts["low_or_conflict_fields_held"] += 1
                prop_v = str(proposal.get(f_name, "") or "")
                r_ev = proposal.get("artist_rule_eval") or {}
                if prop_v or r_ev.get("hdrr_rule_conflict") or proposal.get("hdrr_raw_genres"):
                    if len(low_or_conflict_held) < 50:
                        lvl_l, rsn_l = field_decision(cands, f_name, proposal)
                        low_or_conflict_held.append({
                            "row_num": row_num,
                            "file_id": fid,
                            "track_name": tr.name,
                            "field": f_name,
                            "existing_preserved": canon_row[COL[f_name]].strip(),
                            "unwritten_candidate_value": prop_v or (proposal.get("subgenre_hint", "") if f_name == "subgenre" else ""),
                            "hdrr_raw_genres": proposal.get("hdrr_raw_genres", []),
                            "confidence": lvl_l,
                            "reason": rsn_l,
                        })

        # Double-verify that EVERY field in `changed_fields` is genuinely HIGH or MEDIUM (defense-in-depth!)
        verified_field_changes: List[Dict[str, Any]] = []
        cell_writes: List[Dict[str, Any]] = []
        raw_orig = (raw_rows_by_num or {}).get(row_num)

        for f_name in changed_fields:
            lvl_f, rsn_f = field_decision(cands, f_name, proposal)
            if lvl_f not in ("HIGH", "MEDIUM"):
                continue
            if lvl_f == "MEDIUM" and not save_medium:
                continue
            phys_idx = phys_col_map[f_name]
            col_a1 = _col_letter(phys_idx + 1)
            before_val = (
                str(raw_orig[phys_idx]) if (raw_orig is not None and phys_idx < len(raw_orig))
                else canon_row[COL[f_name]]
            )
            after_val = sanitize_csv_cell(new_canon_row[COL[f_name]])
            if before_val == after_val:
                continue
            prov_f = _field_provenance_sources(tr, cands, f_name, proposal)
            verified_field_changes.append({
                "field": f_name,
                "column": col_a1,
                "range": f"Tracks!{col_a1}{row_num}",
                "before": before_val,
                "after": after_val,
                "confidence": lvl_f,
                "decision_state": f_decisions.get(f_name, ""),
                "provenance": prov_f,
                "reason": rsn_f,
            })
            cell_writes.append({
                "range": f"Tracks!{col_a1}{row_num}",
                "col_index_0": phys_idx,
                "before": before_val,
                "after": after_val,
            })

        if not verified_field_changes:
            continue

        # Also include provenance/lock/review audit columns Q:Z (indices 16..25) on updated rows when compatible
        if review_cols_compatible:
            for rev_idx in range(16, 27):
                before_rev = (
                    str(raw_orig[rev_idx]) if (raw_orig is not None and rev_idx < len(raw_orig))
                    else (canon_row[rev_idx] if rev_idx < len(canon_row) else "")
                )
                after_rev = sanitize_csv_cell(new_canon_row[rev_idx] if rev_idx < len(new_canon_row) else "")
                if before_rev != after_rev:
                    col_a1 = _col_letter(rev_idx + 1)
                    cell_writes.append({
                        "range": f"Tracks!{col_a1}{row_num}",
                        "col_index_0": rev_idx,
                        "before": before_rev,
                        "after": after_rev,
                    })

        counts["eligible_row_updates"] += 1
        counts["eligible_field_updates"] += len(verified_field_changes)
        counts["eligible_cell_writes"] += len(cell_writes)

        proposed_row_updates.append({
            "row_num": row_num,
            "file_id": fid,
            "drive_id_col_0": phys_drive_col,
            "track_name": tr.name,
            "field_changes": verified_field_changes,
            "cell_writes": cell_writes,
            "review_star": new_canon_row[16],
            "review_reasons": new_canon_row[17],
        })

    header_writes: List[Dict[str, Any]] = []
    if proposed_row_updates and review_headers_missing:
        for off, h_name in enumerate(EXTRA_HEADERS):
            col_idx = 16 + off
            cur_h = padded_headers[col_idx]
            if cur_h != h_name:
                col_a1 = _col_letter(col_idx + 1)
                header_writes.append({
                    "range": f"Tracks!{col_a1}1",
                    "col_index_0": col_idx,
                    "before": cur_h,
                    "after": h_name,
                })

    approved_ar_rows = [
        artist_rule_to_sheet_row(r)
        for r in all_configured_rules
        if r.approval_status == "approved"
    ]
    preview_sha = _compute_preview_sha256(config.sheet_id, proposed_row_updates, header_writes)

    return {
        "schema_valid": True,
        "schema_error": "",
        "spreadsheet_id": config.sheet_id,
        "generated_utc": _utc_iso(),
        "provider_terms_cleared": terms_cleared,
        "save_medium": save_medium,
        "forced_drive_dry_run": True,
        "tracks_live_write_requires_approval": True,
        "preview_sha256": preview_sha,
        "summary": {
            **counts,
            "terms_held_field_candidates_count": len(terms_held_candidates),
            "approved_artist_rules_count": len(approved_ar_rows),
            "review_header_writes_needed": len(header_writes),
        },
        "header_writes": header_writes,
        "proposed_row_updates": proposed_row_updates,
        "terms_held_candidates": terms_held_candidates,
        "discrepancies_held_for_review": discrepancies_held,
        "low_or_conflict_held_sample": low_or_conflict_held,
        "artist_rules_tab_preview": {
            "tab_name": ARTIST_RULES_TAB_NAME,
            "headers": list(ARTIST_RULES_HEADERS),
            "rows": approved_ar_rows,
        },
    }


def sync_artist_rules_to_sheet(
    config: Config,
    db: Database,
    controlled_sheets: Any,
    rules: Optional[List[ArtistRule]] = None,
    backup_dir: Optional[Path] = None,
    tab_name: str = ARTIST_RULES_TAB_NAME,
) -> Dict[str, Any]:
    """
    Controlled, backed-up write of approved `ArtistRule`s to the Google Sheets `Artist_Rules` tab.
    Never writes unapproved/rejected suggestions and never touches the `Tracks` tab.
    """
    target_rules = (
        [r for r in rules if r.approval_status == "approved"]
        if rules is not None
        else [r for r in load_artist_rules(db=db, config=config, active_only=False) if r.approval_status == "approved"]
    )
    grid = [list(ARTIST_RULES_HEADERS)] + [artist_rule_to_sheet_row(r) for r in target_rules]

    spreadsheet_id = config.sheet_id
    meta = controlled_sheets.get_spreadsheet_metadata(spreadsheet_id)
    existing_tabs = {
        s.get("properties", {}).get("title", ""): s.get("properties", {})
        for s in meta.get("sheets", [])
    }
    tab_existed = tab_name in existing_tabs
    prior_values: List[List[str]] = []
    if tab_existed:
        prior_values = controlled_sheets.read_values(spreadsheet_id, f"'{tab_name}'!A1:R500")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest_id = f"sheets_artist_rules_{ts}"
    b_dir = backup_dir or (config.config_dir / "sheets_write_backups")
    b_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(b_dir, stat.S_IRWXU)
    except OSError:
        pass
    backup_file = b_dir / f"{manifest_id}.json"
    manifest_payload = {
        "manifest_id": manifest_id,
        "kind": "artist_rules_tab_sync",
        "spreadsheet_id": spreadsheet_id,
        "tab_name": tab_name,
        "tab_existed_before": tab_existed,
        "before_values": prior_values,
        "after_values": grid,
        "created_utc": _utc_iso(),
    }
    backup_file.write_text(json.dumps(manifest_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(backup_file, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    db.save_rollback_manifest(manifest_id, "pre_write_ready", {**manifest_payload, "backup_file": str(backup_file)})

    if not tab_existed:
        controlled_sheets.ensure_tab_exists(spreadsheet_id, tab_name, rows=max(50, len(grid) + 10), cols=len(ARTIST_RULES_HEADERS))

    # Clear trailing rows if previous tab had more rows than `grid`, else overwrite `A1:R<len(grid)>`
    write_rows = list(grid)
    if len(prior_values) > len(grid):
        blank_row = [""] * len(ARTIST_RULES_HEADERS)
        write_rows.extend(blank_row for _ in range(len(prior_values) - len(grid)))

    end_col = _col_letter(len(ARTIST_RULES_HEADERS))
    target_range = f"'{tab_name}'!A1:{end_col}{len(write_rows)}"
    controlled_sheets.write_values_raw(spreadsheet_id, target_range, write_rows)
    db.save_rollback_manifest(manifest_id, "applied", {**manifest_payload, "backup_file": str(backup_file)})

    return {
        "manifest_id": manifest_id,
        "tab_name": tab_name,
        "tab_created": not tab_existed,
        "rules_written": len(target_rules),
        "backup_file": str(backup_file),
    }


def execute_approved_tracks_writes(
    config: Config,
    db: Database,
    controlled_sheets: Any,
    preview_payload: Dict[str, Any],
    confirm_preview_sha256: str,
    backup_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Execute controlled `Tracks` cell updates ONLY after explicit user approval of `preview_sha256`
    and live optimistic concurrency verification against current `Tracks` row values.
    """
    expected_sha = str(preview_payload.get("preview_sha256") or "").strip()
    given_sha = str(confirm_preview_sha256 or "").strip()
    if not expected_sha or given_sha != expected_sha:
        raise RuntimeError(
            "Refusing live Tracks write: --confirm-sha256 does not match the approved write preview SHA-256."
        )
    if not preview_payload.get("schema_valid"):
        raise RuntimeError("Refusing live Tracks write: preview schema_valid is False.")
    spreadsheet_id = str(preview_payload.get("spreadsheet_id") or "")
    if spreadsheet_id != config.sheet_id:
        raise RuntimeError("Refusing live Tracks write: preview spreadsheet_id does not match configured SHEET_ID.")

    row_updates = list(preview_payload.get("proposed_row_updates") or [])
    header_writes = list(preview_payload.get("header_writes") or [])
    recomputed_sha = _compute_preview_sha256(spreadsheet_id, row_updates, header_writes)
    if recomputed_sha != expected_sha:
        raise RuntimeError("Refusing live Tracks write: preview payload failed integrity SHA-256 verification.")

    if not row_updates:
        return {"manifest_id": "", "rows_updated": 0, "cells_updated": 0, "status": "noop_empty_preview"}

    # Pre-flight optimistic concurrency check: re-fetch every target row live and verify Drive ID + before values!
    live_before_rows: List[Dict[str, Any]] = []
    batch_data: List[Dict[str, Any]] = []

    if header_writes:
        live_h1 = controlled_sheets.read_values(spreadsheet_id, "Tracks!A1:AA1")
        h1_row = live_h1[0] if live_h1 else []
        for hw in header_writes:
            c_idx = int(hw["col_index_0"])
            cur_v = str(h1_row[c_idx]) if c_idx < len(h1_row) else ""
            if cur_v and cur_v != hw["before"] and cur_v != hw["after"]:
                raise RuntimeError(f"Pre-flight Drift Detected at {hw['range']}: expected '{hw['before']}', found '{cur_v}'")
            batch_data.append({"range": hw["range"], "values": [[str(hw["after"])]]})

    for item in row_updates:
        r_num = int(item["row_num"])
        fid = str(item["file_id"])
        drv_col = int(item.get("drive_id_col_0", 14))
        last_col = _col_letter(max([drv_col + 1, 27] + [int(cw["col_index_0"]) + 1 for cw in item["cell_writes"]]))
        fetched = controlled_sheets.read_values(spreadsheet_id, f"Tracks!A{r_num}:{last_col}{r_num}")
        live_r = fetched[0] if fetched else []
        live_fid = str(live_r[drv_col]).strip() if drv_col < len(live_r) else ""
        if live_fid != fid:
            raise RuntimeError(
                f"Pre-flight Drift Detected on Tracks row {r_num}: expected Drive ID '{fid}', found '{live_fid}'. Aborting."
            )
        for cw in item["cell_writes"]:
            c_idx = int(cw["col_index_0"])
            cur_cell = str(live_r[c_idx]) if c_idx < len(live_r) else ""
            if cur_cell != str(cw["before"]):
                raise RuntimeError(
                    f"Pre-flight Drift Detected at {cw['range']}: expected '{cw['before']}', found '{cur_cell}'. Aborting."
                )
            batch_data.append({"range": cw["range"], "values": [[str(cw["after"])]]})
        live_before_rows.append({"row_num": r_num, "file_id": fid, "values": live_r})

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest_id = f"sheets_tracks_{ts}"
    b_dir = backup_dir or (config.config_dir / "sheets_write_backups")
    b_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(b_dir, stat.S_IRWXU)
    except OSError:
        pass
    backup_file = b_dir / f"{manifest_id}.json"
    manifest_payload = {
        "manifest_id": manifest_id,
        "kind": "tracks_approved_write",
        "spreadsheet_id": spreadsheet_id,
        "preview_sha256": expected_sha,
        "header_writes": header_writes,
        "row_updates": row_updates,
        "live_before_rows": live_before_rows,
        "created_utc": _utc_iso(),
    }
    backup_file.write_text(json.dumps(manifest_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(backup_file, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    db.save_rollback_manifest(manifest_id, "pre_write_ready", {**manifest_payload, "backup_file": str(backup_file)})

    controlled_sheets.batch_write_values_raw(spreadsheet_id, batch_data)
    db.save_rollback_manifest(manifest_id, "applied", {**manifest_payload, "backup_file": str(backup_file)})

    return {
        "manifest_id": manifest_id,
        "rows_updated": len(row_updates),
        "cells_updated": len(batch_data),
        "backup_file": str(backup_file),
        "status": "applied",
    }


def rollback_sheets_manifest(
    config: Config,
    db: Database,
    controlled_sheets: Any,
    manifest_id: str,
) -> Dict[str, Any]:
    """Deterministically restore pre-write Google Sheets cells from a saved `rollback_manifests` entry."""
    manifest = db.get_rollback_manifest(manifest_id)
    if not manifest:
        raise RuntimeError(f"Rollback manifest '{manifest_id}' not found in database.")
    spreadsheet_id = manifest["spreadsheet_id"]
    kind = manifest.get("kind", "")

    if kind == "artist_rules_tab_sync":
        tab_name = manifest["tab_name"]
        before_vals = manifest.get("before_values") or []
        after_vals = manifest.get("after_values") or []
        max_rows = max(len(before_vals), len(after_vals), 1)
        max_cols = len(ARTIST_RULES_HEADERS)
        restored: List[List[str]] = []
        for r_i in range(max_rows):
            if r_i < len(before_vals):
                row = list(before_vals[r_i]) + [""] * max(0, max_cols - len(before_vals[r_i]))
                restored.append(row[:max_cols])
            else:
                restored.append([""] * max_cols)
        end_col = _col_letter(max_cols)
        controlled_sheets.write_values_raw(spreadsheet_id, f"'{tab_name}'!A1:{end_col}{max_rows}", restored)
        db.save_rollback_manifest(manifest_id, "rolled_back", manifest)
        return {"manifest_id": manifest_id, "kind": kind, "status": "rolled_back", "rows_restored": max_rows}

    if kind == "tracks_approved_write":
        batch_restore: List[Dict[str, Any]] = []
        for hw in (manifest.get("header_writes") or []):
            batch_restore.append({"range": hw["range"], "values": [[str(hw.get("before", ""))]]})
        for item in (manifest.get("row_updates") or []):
            for cw in (item.get("cell_writes") or []):
                batch_restore.append({"range": cw["range"], "values": [[str(cw.get("before", ""))]]})
        if batch_restore:
            controlled_sheets.batch_write_values_raw(spreadsheet_id, batch_restore)
        db.save_rollback_manifest(manifest_id, "rolled_back", manifest)
        return {"manifest_id": manifest_id, "kind": kind, "status": "rolled_back", "cells_restored": len(batch_restore)}

    raise RuntimeError(f"Unsupported rollback manifest kind: {kind}")
