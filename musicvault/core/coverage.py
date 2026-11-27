"""Local metadata reconciliation, spreadsheet intelligence preview, and attribution over stored evidence only."""
import csv
import json
import os
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..cli.formatting import _redact_json_tree, sanitize_csv_cell, short_file_id
from ..providers.base import canonical_provider_family
from ..services.sheets import _DRIVE_ID_TOKEN_RE, normalize_tracks_row, resolve_tracks_headers
from .artist_rules import load_artist_rules
from .genre import (
    SUBGENRE_PARENT,
    canonical_genre,
    canonical_subgenre,
    is_genre_placeholder,
    is_subgenre_placeholder,
    is_umbrella_club_genre,
)
from .matcher import (
    extract_version_tokens,
    format_display_bpm,
    is_valid_bpm,
    norm,
    normalize_key_to_camelot,
    parse_artists,
    versions_compatible,
)
from .organizer import _vault_parent_folder_key
from .provider_policy import MAX_ENRICHMENT_ATTEMPTS, _families, provider_report
from .rules import (
    COL,
    build_track_proposal,
    candidate_field_values,
    field_decision,
    inspect_sheet_row_semantics,
    parse_lock_states,
)

FIELDS = ("artist", "title", "edition", "duration", "bpm", "key", "genre", "subgenre", "label", "isrc")
ATTRIBUTED = ("identity", "bpm", "key", "genre", "subgenre")
MANUAL_STATES = {"USER_LOCKED", "HIGH_LOCKED", "ACCEPTED", "VERIFIED"}
RECORDING_STATES = {"Duration supported", "ISRC supported"}
OPEN_PROVENANCE = {"musicbrainz", "embedded", "manual", "hdrr"}


def _family(candidate):
    return canonical_provider_family(candidate.source_family or candidate.provider)


def _edition(title):
    if not title:
        return ""
    _, tokens, remixer = extract_version_tokens(title)
    if remixer or "remix" in tokens:
        return f"remix ({remixer})" if remixer else "remix"
    named_edits = sorted(t.split(":", 1)[1] for t in tokens if t.startswith("edit:"))
    if named_edits:
        return f"edit ({' / '.join(named_edits)})"
    if "radioedit" in tokens:
        return "edit"
    if "extendedmix" in tokens:
        return "extended"
    if "originalmix" in tokens:
        return "original"
    other = sorted(tokens - {"originalmix"})
    return " / ".join(other) or "original"


def _candidate_values(candidate, field):
    if field == "artist":
        return [", ".join(candidate.artists)] if candidate.artists else []
    if field == "edition":
        return [_edition(candidate.title)] if candidate.title else []
    if field == "subgenre":
        return list(dict.fromkeys(sub for value in (candidate.genre or []) if (sub := canonical_subgenre(value))))
    if field == "label":
        return list(dict.fromkeys(str(lbl).strip() for lbl in (candidate.labels or []) if str(lbl or "").strip()))
    value = getattr(candidate, field, None)
    return value if isinstance(value, list) else [value]


def _group_recording_values(verified_candidates, field):
    """
    Group recording-verified candidate values by normalized semantic equivalence so formatting
    or sub-second duration jitter does not fracture corroborating families or fabricate conflicts.
    Returns dict: normalized_key -> (display_value, set_of_families).
    """
    grouped = {}
    if field == "duration":
        clusters = []
        for item in verified_candidates:
            fam = _family(item)
            for val in _candidate_values(item, field):
                if not val or float(val) <= 0:
                    continue
                d_val = round(float(val), 2)
                placed = False
                for cluster in clusters:
                    ref = cluster["display"]
                    tol = max(3.0, max(ref, d_val) * 0.02)
                    if abs(d_val - ref) <= tol:
                        cluster["families"].add(fam)
                        placed = True
                        break
                if not placed:
                    clusters.append({"display": d_val, "families": {fam}})
        for idx, cl in enumerate(clusters):
            grouped[f"dur_{idx}"] = (str(cl["display"]), cl["families"])
        return grouped

    for item in verified_candidates:
        fam = _family(item)
        for raw_val in _candidate_values(item, field):
            if not raw_val:
                continue
            disp = str(raw_val).strip()
            if not disp:
                continue
            if field == "artist":
                key = ",".join(sorted(set(parse_artists(disp)))) or norm(disp)
            elif field in {"isrc", "label", "title"}:
                key = norm(disp)
            else:
                key = disp
            if not key:
                continue
            if key not in grouped:
                grouped[key] = [disp, {fam}]
            else:
                grouped[key][1].add(fam)
    return {k: (v[0], v[1]) for k, v in grouped.items()}


def decisions(track, candidates, manual, artist_rules=None):
    """Return a field view from a materialized track and permitted candidates."""
    proposal = build_track_proposal(track.metadata, candidates, artist_rules=artist_rules, file_id=track.file_id)
    verified = [c for c in candidates if c.verification in RECORDING_STATES and _family(c) != "wikidata"]
    meta = track.metadata
    local = {
        "artist": meta.artist,
        "title": meta.title,
        "edition": _edition(meta.title),
        "duration": meta.duration,
        "bpm": format_display_bpm(meta.embedded_bpm) if meta.embedded_bpm else "",
        "key": normalize_key_to_camelot(meta.embedded_key) if meta.embedded_key else "",
        "genre": meta.embedded_genre,
        "isrc": meta.isrc,
    }
    parent_g = canonical_genre(str(proposal.get("genre", "")))
    results = {}
    for field in FIELDS:
        hits = {}
        for item in candidates:
            for value in _candidate_values(item, field):
                if value:
                    hits.setdefault(str(value), set()).add(_family(item))
        evidence = {value: sorted(sources) for value, sources in hits.items()}
        state, value, confidence, sources, reason = "missing", "", "LOW", [], "No evidence"
        if hits or local.get(field):
            state, reason = "candidate_only", "Evidence requires verification"
        if field in COL:
            raw_prop_val = proposal[field]
            value = format_display_bpm(raw_prop_val) if field == "bpm" else str(raw_prop_val or "")
            confidence, reason = field_decision(candidates, field, proposal)
            if raw_prop_val == "CONFLICT" or "disagreement" in reason or "contradictory" in reason or "conflicting" in reason and raw_prop_val not in ("", None):
                state = "conflicting"
                value = ""
            elif confidence == "HIGH" and value:
                state = "verified"
                sources = sorted({
                    item[1]
                    for item in candidate_field_values(
                        candidates, field, parent_genre=parent_g if field == "subgenre" else ""
                    )
                })
        else:
            matched = _group_recording_values(verified, field)
            embedded = local.get(field) and meta.identity_origin == "embedded"
            if len(matched) > 1 and field == "edition":
                state, reason = "conflicting", "Recording-level values disagree"
            elif len(matched) > 1 and field == "isrc" and not embedded:
                state, reason = "conflicting", "Recording-level values disagree"
            elif field == "edition" and meta.title and any(
                not versions_compatible(meta.title, item.title) for item in verified
            ):
                state, reason = "conflicting", "Recording editions disagree"
            elif embedded:
                state, value, confidence, sources, reason = (
                    "accepted",
                    local[field],
                    "MEDIUM",
                    ["embedded"],
                    "Local embedded value",
                )
            elif matched:
                _k, (best_val, families) = max(
                    matched.items(), key=lambda entry: (len(entry[1][1]), entry[1][0])
                )
                value = best_val
                if len(families) >= 2 or (
                    field in {"artist", "title", "edition", "isrc"}
                    and any(c.verification == "ISRC supported" for c in verified)
                ):
                    state, confidence, sources, reason = (
                        "verified",
                        "HIGH",
                        sorted(families),
                        "Recording-level evidence",
                    )
        disputed = state == "conflicting"
        if field in manual and manual[field][0] and manual[field][1] in MANUAL_STATES:
            m_val, lock = manual[field]
            if field == "bpm" and is_valid_bpm(m_val):
                m_val = format_display_bpm(m_val)
            elif field == "key":
                m_val = normalize_key_to_camelot(m_val) or m_val
            state, value, confidence, sources, reason = (
                "verified" if lock in {"VERIFIED", "HIGH_LOCKED"} else "accepted",
                m_val,
                "HIGH",
                ["manual"],
                "Preserved accepted value",
            )
        entry_out = {
            "state": state,
            "value": str(value or ""),
            "confidence": confidence,
            "provenance": sources,
            "reason": reason,
            "candidates": evidence,
            "conflict": disputed,
        }
        if field == "bpm":
            entry_out["raw_measurements"] = proposal.get("bpm_raw_measurements", [])
            entry_out["octave_adjusted"] = bool(proposal.get("bpm_octave_adjusted", False))
        elif field in ("genre", "subgenre"):
            if proposal.get("hdrr_raw_genres"):
                entry_out["hdrr_raw_source_genres"] = proposal["hdrr_raw_genres"]
            r_ev = proposal.get("artist_rule_eval") or {}
            if r_ev.get("matched_rules"):
                entry_out["artist_rules_matched"] = r_ev["matched_rules"]
            if r_ev.get("conflict_reasons"):
                entry_out["artist_rule_conflicts"] = r_ev["conflict_reasons"]
            if field == "subgenre" and r_ev.get("investigation_cues"):
                entry_out["investigation_cues"] = r_ev["investigation_cues"]
        results[field] = entry_out
    return results, proposal.get("tempo_hint", "")


def _signature(result, field):
    if field == "identity":
        return tuple(_signature(result, item) for item in ("artist", "title"))
    value = result[field]
    return (value["state"], value["value"]) if value["state"] in {"accepted", "verified"} else ()


def attribution(track, candidates, manual, baseline, artist_rules=None):
    """Counterfactual attribution. This function has no access to provider clients."""
    result = {}
    families = {_family(item) for item in candidates}
    for family in families:
        reduced, _ = decisions(
            track,
            [item for item in candidates if _family(item) != family],
            manual,
            artist_rules=artist_rules,
        )
        accepted = [
            field
            for field in ATTRIBUTED
            if family
            in (
                baseline["artist"]["provenance"] + baseline["title"]["provenance"]
                if field == "identity"
                else baseline[field]["provenance"]
            )
        ]
        unique = [
            field
            for field in ATTRIBUTED
            if _signature(baseline, field) and _signature(baseline, field) != _signature(reduced, field)
        ]
        result[family] = accepted, unique
    return result


def _column_name(index):
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def read_sheet(conn, sheets, sheet_id):
    index = resolve_tracks_headers(sheets.fetch_header_row(sheet_id, "Tracks!1:1"))
    if not index.schema_valid:
        return {"schema_error": index.schema_error}
    conn.execute(
        """CREATE TEMP TABLE IF NOT EXISTS preview_sheet (
        row_number INTEGER PRIMARY KEY, file_id TEXT NOT NULL, row_json TEXT NOT NULL
    )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS preview_sheet_ids ON preview_sheet(file_id)")
    conn.execute("DELETE FROM preview_sheet")
    last_column = _column_name(max(27, len(index.headers)))

    def rows():
        for number, raw in sheets.iter_tracks_rows(sheet_id, last_column):
            if not any(str(value or "").strip() for value in raw):
                continue
            canonical = normalize_tracks_row(raw, index)
            file_id = canonical[14].strip()
            if (len(index.headers) <= index.drive_id_col or not index.headers[index.drive_id_col]) and file_id:
                if not _DRIVE_ID_TOKEN_RE.fullmatch(file_id):
                    raise ValueError("Invalid ID in the blank legacy Drive ID column.")
            yield number, file_id, json.dumps(canonical)

    with conn:
        conn.executemany("INSERT INTO preview_sheet VALUES (?, ?, ?)", rows())
    return {
        "missing_ids": conn.execute("SELECT COUNT(*) FROM preview_sheet WHERE file_id=''").fetchone()[0],
        "duplicate_ids": conn.execute(
            """SELECT COUNT(*) FROM (
            SELECT file_id FROM preview_sheet WHERE file_id!='' GROUP BY file_id HAVING COUNT(*)>1
        )"""
        ).fetchone()[0],
        "columns": {name: _column_name(index.columns[col] + 1) for name, col in COL.items()},
    }


def _disabled_families(config):
    raw = (config.get("DISABLED_RESEARCH_PROVIDERS", "") or "").strip()
    return set(_families(raw.split(","), allow_wikidata=True)) if raw else set()


def _track_report(conn, db, row, config, sheet_info, folders, location_verified, disabled, artist_rules=None):
    track = db._row_to_track(row)
    terms = config.provider_terms_cleared
    candidates = [
        c
        for c in db.get_candidates(track.file_id)
        if not c.is_stale
        and c.content_version in ("", track.metadata.version)
        and _family(c) not in disabled
    ]
    manual = {
        field: (value, state)
        for field, value, state in conn.execute(
            "SELECT field_name,value,state FROM field_decisions WHERE file_id=?",
            (track.file_id,),
        )
    }
    sheet_rows = (
        []
        if "schema_error" in sheet_info
        else conn.execute(
            "SELECT row_number,row_json FROM preview_sheet WHERE file_id=? LIMIT 2",
            (track.file_id,),
        ).fetchall()
    )
    old = json.loads(sheet_rows[0][1]) if len(sheet_rows) == 1 else []
    locks, lock_error = parse_lock_states(old) if old else ({}, None)
    sem = inspect_sheet_row_semantics(old) if old else {"field_locks": {}, "field_holds": {}, "row_flags": []}
    for field, col in COL.items():
        lock = locks.get(field, {}).get("state")
        if old and lock in MANUAL_STATES and old[col].strip():
            manual.setdefault(field, (old[col].strip(), lock))

    field_data, tempo_hint = decisions(track, candidates, manual, artist_rules=artist_rules)
    parent_g = canonical_genre(field_data["genre"]["value"])
    double_check_flags = list(sem["row_flags"])
    if tempo_hint:
        double_check_flags.append(f"tempo_hint:{tempo_hint}")
    if field_data["bpm"].get("octave_adjusted"):
        double_check_flags.append("bpm_octave_adjusted_2x")
    if field_data["subgenre"].get("artist_rule_conflicts") or field_data["genre"].get("artist_rule_conflicts"):
        double_check_flags.append("artist_rule_conflict_detected")
    if field_data["subgenre"].get("investigation_cues"):
        double_check_flags.append("investigate:" + ",".join(field_data["subgenre"]["investigation_cues"]))

    try:
        audit = json.loads(track.provider_calls_json)
        audit_meta = audit.get("_meta", {})
    except (ValueError, AttributeError):
        audit, audit_meta = {}, {}
    pending = bool(
        conn.execute(
            """
            SELECT 1 FROM enrichment_work
            WHERE file_id=? AND version=? AND COALESCE(attempts, 0) < ? LIMIT 1
            """,
            (track.file_id, track.metadata.version, MAX_ENRICHMENT_ATTEMPTS),
        ).fetchone()
    )
    exhausted_enrichment = bool(
        conn.execute(
            """
            SELECT 1 FROM enrichment_work
            WHERE file_id=? AND version=? AND COALESCE(attempts, 0) >= ? LIMIT 1
            """,
            (track.file_id, track.metadata.version, MAX_ENRICHMENT_ATTEMPTS),
        ).fetchone()
    )
    research_complete = bool(
        audit_meta.get("lookup_complete")
        and not track.research_incomplete
        and track.research_version == track.metadata.version
        and not track.content_stale
    )
    no_match = research_complete and track.research_status == "no_match"

    sheet_unmapped_or_ambiguous = bool("schema_error" in sheet_info or len(sheet_rows) != 1)
    if lock_error:
        double_check_flags.append(f"lock_error:{lock_error}")

    for field, detail in field_data.items():
        old_val = old[COL[field]].strip() if old and field in COL else ""
        detail["old"] = old_val
        lock_state = locks.get(field, {}).get("state", "") if field in COL else ""
        is_locked = lock_state in MANUAL_STATES
        is_review_hold = lock_state == "REVIEW_HOLD"
        is_placeholder = (
            (field == "genre" and is_genre_placeholder(old_val))
            or (field == "subgenre" and is_subgenre_placeholder(old_val))
        )
        is_umbrella_upgrade = (
            field == "genre"
            and is_umbrella_club_genre(old_val)
            and "sheet_status_review_hold" not in sem["row_flags"]
            and detail["value"] == "Hardstyle"
            and detail["confidence"] in {"HIGH", "MEDIUM"}
            and not detail["conflict"]
            and not is_locked
            and not is_review_hold
        )

        if lock_error:
            detail["proposed"] = old_val
            detail["would_propose"] = ""
            detail["proposal_action"] = "lock_error_hold"
            detail["terms_hold"] = False
        elif field in COL and sheet_unmapped_or_ambiguous:
            detail["proposed"] = old_val
            detail["would_propose"] = (
                detail["value"]
                if (detail["value"] and not detail["conflict"] and detail["confidence"] in {"HIGH", "MEDIUM"})
                else ""
            )
            detail["proposal_action"] = "sheet_mapping_hold"
            detail["terms_hold"] = False
        elif is_locked:
            detail["proposed"] = old_val
            detail["would_propose"] = old_val
            detail["proposal_action"] = "locked_preserved"
            detail["terms_hold"] = False
        elif is_review_hold:
            hold_reason = locks[field].get("reason", "human review hold")
            detail["state"] = "conflicting" if "conflict" in hold_reason.lower() else "candidate_only"
            detail["confidence"] = "LOW"
            detail["provenance"] = []
            detail["reason"] = hold_reason
            detail["conflict"] = detail["state"] == "conflicting"
            detail["proposed"] = old_val
            detail["would_propose"] = ""
            detail["proposal_action"] = "human_review_hold"
            detail["terms_hold"] = False
        elif (
            field == "bpm"
            and detail.get("octave_adjusted")
            and old
            and canonical_subgenre(old[COL["subgenre"]]) == "Slowtempo"
        ):
            detail["proposed"] = old_val
            detail["would_propose"] = ""
            detail["proposal_action"] = "slowtempo_octave_blocked"
            detail["terms_hold"] = False
            double_check_flags.append("slowtempo_octave_blocked")
        elif old_val and not is_placeholder and not is_umbrella_upgrade:
            detail["proposed"] = old_val
            detail["would_propose"] = old_val
            detail["terms_hold"] = False
            # Flag discrepancy if HIGH/MEDIUM catalogue disagrees with unlocked concrete Sheet value
            contradicts = False
            if field in COL and detail["confidence"] in {"HIGH", "MEDIUM"} and detail["value"] and not detail["conflict"]:
                if field == "genre":
                    ex_cg = canonical_genre(old_val)
                    contradicts = bool(
                        (ex_cg and ex_cg != canonical_genre(detail["value"]))
                        or (is_umbrella_club_genre(old_val) and canonical_genre(detail["value"]) == "Hardstyle")
                    )
                elif field == "bpm" and is_valid_bpm(old_val):
                    contradicts = abs(int(round(float(old_val))) - int(round(float(detail["value"])))) >= 2
                elif field == "key":
                    ex_k = normalize_key_to_camelot(old_val)
                    contradicts = bool(ex_k and ex_k != normalize_key_to_camelot(detail["value"]))
            if contradicts:
                detail["proposal_action"] = "preserved_existing_conflict_flagged"
                double_check_flags.append(f"{field}_existing_vs_catalogue_discrepancy")
            else:
                detail["proposal_action"] = "existing_preserved"
        elif (
            detail["value"]
            and not detail["conflict"]
            and (detail["state"] in {"accepted", "verified"} or (field in COL and detail["confidence"] in {"HIGH", "MEDIUM"}))
        ):
            cand_fams = set(detail["provenance"]) or {
                x[1]
                for x in candidate_field_values(
                    candidates, field, parent_genre=parent_g if field == "subgenre" else ""
                )
            }
            restricted_prov = bool(
                field in COL
                and not terms
                and cand_fams
                and not (cand_fams <= OPEN_PROVENANCE)
            )
            detail["would_propose"] = detail["value"]
            if is_umbrella_upgrade:
                double_check_flags.append(f"umbrella_dance_club_to_hardstyle_{detail['confidence'].lower()}")
            elif detail["confidence"] == "MEDIUM" and field in COL:
                double_check_flags.append(f"{field}_medium_provisional")
            if restricted_prov:
                # Preserve existing placeholder text (or '' if blank) while holding live Sheet cell under TERMS_HOLD
                detail["proposed"] = old_val
                detail["proposal_action"] = "terms_hold_upgrade" if (is_placeholder or is_umbrella_upgrade) else "terms_hold"
                detail["terms_hold"] = True
            else:
                detail["proposed"] = detail["value"]
                detail["proposal_action"] = (
                    "umbrella_upgrade" if is_umbrella_upgrade
                    else "placeholder_upgrade" if is_placeholder
                    else "fill_blank"
                )
                detail["terms_hold"] = False
        else:
            # Preserve existing cell value when evidence is LOW, MISSING, or CONFLICT!
            detail["proposed"] = old_val
            detail["would_propose"] = ""
            detail["proposal_action"] = "conflict_hold" if detail["conflict"] else "insufficient_evidence_preserved"
            detail["terms_hold"] = False

    metadata_complete = all(value["state"] in {"accepted", "verified"} for value in field_data.values())
    identity_ready = all(field_data[f]["state"] in {"accepted", "verified"} for f in ("artist", "title"))
    conflict = any(value["conflict"] for value in field_data.values())
    any_field_review_hold = any(locks.get(f, {}).get("state") == "REVIEW_HOLD" for f in COL)
    human_routing_hold = any(
        fl in sem["row_flags"]
        for fl in ("potential_duplicate_hold", "keep_in_source_hold", "human_genre_conflict_hold", "sheet_status_review_hold")
    )
    review = bool(
        track.quarantined
        or lock_error
        or conflict
        or any_field_review_hold
        or human_routing_hold
        or bool(tempo_hint)
        or exhausted_enrichment
        or (research_complete and track.research_status == "partial")
    )
    genre = field_data["genre"]
    canon_g = canonical_genre(genre["value"])
    genre_key = _vault_parent_folder_key(canon_g) if canon_g else ""
    targets = folders.get(genre_key, []) if genre_key else []
    try:
        dest_path = json.loads(row["destination_path_json"] or "[]")
    except (ValueError, TypeError):
        dest_path = []
    in_correct_vault_folder = bool(
        genre_key
        and (
            (len(targets) == 1 and row["destination_parent"] == targets[0])
            or (
                len(dest_path) >= 2
                and _vault_parent_folder_key(dest_path[0]) == genre_key
                and len(targets) <= 1
            )
        )
    )
    if (
        row["destination_member"]
        and genre["state"] == "verified"
        and in_correct_vault_folder
        and not row["source_member"]
        and not row["ambiguous"]
    ):
        route = "already_correctly_located"
    elif (row["source_member"] and row["destination_member"]) or row["ambiguous"]:
        route = "ambiguous_location"
    elif row["destination_member"]:
        route = "destination_only"
    elif not location_verified:
        route = "unverified_root_membership"
    elif len(sheet_rows) != 1 or lock_error:
        route = "sheet_mapping_hold"
    elif (
        track.quarantined
        or conflict
        or human_routing_hold
        or exhausted_enrichment
        or (research_complete and track.research_status == "partial")
        or no_match
        or pending
        or not research_complete
        or not identity_ready
        or genre["state"] != "verified"
    ):
        route = "review_or_incomplete"
    elif len(targets) != 1 or targets[0] == config.source_folder_id:
        route = "unknown_or_ambiguous_destination"
    else:
        route = "preview_ready"

    states = {
        "inventory_discovered": True,
        "hydration_complete": (
            not track.content_stale
            and track.hydration_version == track.metadata.version
            and track.hydration_status in {"success", "partial", "unresolved"}
        ),
        "research_attempted": bool(
            track.research_attempts
            or any(
                isinstance(entry, dict) and (entry.get("request_made") or entry.get("cache_hits"))
                for entry in audit.values()
            )
        ),
        "research_complete": research_complete,
        "metadata_complete": metadata_complete,
        "enrichment_pending": pending,
        "review_required": review,
        "terminal_no_match": no_match,
        "workflow_complete": research_complete
        or (review and not pending and (track.research_status == "unresolved" or exhausted_enrichment)),
        "routing_ready": route == "preview_ready",
        "provider_complete": research_complete,
    }
    detail = {
        "file_id": short_file_id(track.file_id, verbose=False),
        "name": track.name,
        "states": states,
        "fields": field_data,
        "tempo_hint": tempo_hint,
        "double_check_flags": list(dict.fromkeys(double_check_flags)),
        "routing": route,
        "destination": canon_g if route == "preview_ready" else "",
        "move_authorized": False,
        "executable": False,
        "sheet_row": sheet_rows[0][0] if len(sheet_rows) == 1 else None,
    }
    return detail, attribution(track, candidates, manual, field_data, artist_rules=artist_rules)


def library_report(conn, db, config, args, sheets, drive):
    """Stream optional detail files and retain only the summary in memory."""
    conn.execute("PRAGMA temp_store=FILE")
    disabled = _disabled_families(config)
    try:
        sheet_info = read_sheet(conn, sheets, config.sheet_id)
    except Exception as exc:
        sheet_info = {"schema_error": config.redact(exc)}
    sheet_artist_rules = []
    if hasattr(sheets, "load_artist_rules_tab"):
        try:
            sheet_artist_rules, ar_warnings = sheets.load_artist_rules_tab(config.sheet_id)
            if ar_warnings:
                sheet_info["artist_rules_warnings"] = ar_warnings
        except Exception:
            sheet_artist_rules = []
    active_artist_rules = load_artist_rules(db=db, config=config, sheet_rules=sheet_artist_rules)
    try:
        folders = {}
        for name, ids in drive.get_children_folders(config.vault_folder_id).items():
            folders.setdefault(_vault_parent_folder_key(name), []).extend(ids)
    except Exception:
        folders = {}
    location_verified = (
        config.source_folder_id != config.vault_folder_id
        and conn.execute(
            """SELECT COUNT(DISTINCT source_folder_id) FROM scan_runs
               WHERE source_folder_id IN (?,?) AND status='completed'""",
            (config.source_folder_id, config.vault_folder_id),
        ).fetchone()[0]
        == 2
    )
    totals = {scope: Counter() for scope in ("source", "destination", "unique")}
    field_counts = {scope: {field: Counter() for field in FIELDS} for scope in totals}
    accepted, unique = {}, {}
    columns = (
        "file_id",
        "name",
        "states",
        "fields",
        "tempo_hint",
        "double_check_flags",
        "routing",
        "destination",
        "move_authorized",
        "executable",
        "sheet_row",
    )
    rows = conn.execute(
        """
        WITH membership AS (
            SELECT file_id, MAX(role='source') AS source_member,
                   MAX(role='destination') AS destination_member,
                   MAX(CASE WHEN role='destination' THEN parent_id ELSE '' END) AS destination_parent,
                   MAX(CASE WHEN role='destination' THEN path_json ELSE '[]' END) AS destination_path_json,
                   MAX(ambiguous) AS ambiguous
            FROM root_membership
            WHERE (role='source' AND root_id=?) OR (role='destination' AND root_id=?)
            GROUP BY file_id
        )
        SELECT t.*,m.source_member,m.destination_member,m.destination_parent,m.destination_path_json,m.ambiguous
        FROM membership m JOIN tracks t USING(file_id)
        """,
        (config.source_folder_id, config.vault_folder_id),
    )
    outputs = []
    secrets = config.known_secrets()
    try:
        with ExitStack() as stack:
            for kind, path in (("csv", args.export_csv), ("json", args.export_json)):
                if path:
                    target = Path(path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    stream = stack.enter_context(
                        NamedTemporaryFile(
                            mode="w",
                            encoding="utf-8",
                            newline="",
                            dir=target.parent,
                            delete=False,
                        )
                    )
                    outputs.append((kind, target, stream))
            csv_writer = next((csv.DictWriter(stream, columns) for kind, _, stream in outputs if kind == "csv"), None)
            json_stream = next((stream for kind, _, stream in outputs if kind == "json"), None)
            if csv_writer:
                csv_writer.writeheader()
            if json_stream:
                json_stream.write('{"rows":[')
            first = True
            for row in rows:
                detail, providers = _track_report(
                    conn, db, row, config, sheet_info, folders, location_verified, disabled,
                    artist_rules=active_artist_rules,
                )
                if args.verbose:
                    detail["file_id"] = row["file_id"]
                for family, (fields, unique_fields) in providers.items():
                    accepted.setdefault(family, Counter()).update(fields)
                    unique.setdefault(family, Counter()).update(unique_fields)
                scopes = ["unique"]
                if row["source_member"]:
                    scopes.append("source")
                if row["destination_member"]:
                    scopes.append("destination")
                for scope in scopes:
                    totals[scope]["denominator"] += 1
                    totals[scope].update(key for key, value in detail["states"].items() if value)
                    for field in FIELDS:
                        item = detail["fields"][field]
                        field_counts[scope][field][item["state"]] += 1
                        if item.get("terms_hold"):
                            field_counts[scope][field]["terms_hold"] += 1
                        if item.get("proposal_action"):
                            field_counts[scope][field][f"action:{item['proposal_action']}"] += 1
                        if item["conflict"] and item["state"] != "conflicting":
                            field_counts[scope][field]["conflict_with_preserved_value"] += 1
                if args.file_id and row["file_id"] not in args.file_id:
                    continue
                if csv_writer:
                    csv_writer.writerow(
                        {
                            key: sanitize_csv_cell(
                                json.dumps(value) if isinstance(value, (dict, list)) else value,
                                secrets,
                            )
                            for key, value in detail.items()
                        }
                    )
                if json_stream:
                    if not first:
                        json_stream.write(",")
                    json_stream.write(json.dumps(_redact_json_tree(detail, secrets), ensure_ascii=False))
                    first = False
            telemetry = provider_report(conn, config)
            for family, values in telemetry.items():
                values.update(
                    accepted_contribution=dict(accepted.get(family, {})),
                    unique_contribution=dict(unique.get(family, {})),
                )
            summary = {"tracks": totals, "fields": field_counts, "providers": telemetry, "sheets": sheet_info}
            if json_stream:
                json_stream.write('],"coverage":' + json.dumps(summary) + "}\n")
        for _, path, stream in outputs:
            os.replace(stream.name, path)
        return summary
    finally:
        for _, _, stream in outputs:
            Path(stream.name).unlink(missing_ok=True)
