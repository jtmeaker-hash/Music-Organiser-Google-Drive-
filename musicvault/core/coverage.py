"""Local metadata reconciliation and attribution over stored evidence only."""
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
from .genre import SUBGENRE_PARENT, canonical_genre
from .matcher import extract_version_tokens, norm, versions_compatible
from .organizer import _vault_parent_folder_key
from .provider_policy import provider_report
from .rules import COL, build_track_proposal, candidate_field_values, field_decision, parse_lock_states

FIELDS = ("artist", "title", "edition", "duration", "bpm", "key", "genre", "subgenre", "label", "isrc")
ATTRIBUTED = ("identity", "bpm", "key", "genre", "subgenre")
MANUAL_STATES = {"USER_LOCKED", "HIGH_LOCKED", "ACCEPTED", "VERIFIED"}
RECORDING_STATES = {"Duration supported", "ISRC supported"}


def _family(candidate):
    return canonical_provider_family(candidate.source_family or candidate.provider)


def _edition(title):
    if not title:
        return ""
    _, tokens, remixer = extract_version_tokens(title)
    return " / ".join([*sorted(tokens), *([remixer] if remixer else [])]) or "original"


def _candidate_values(candidate, field):
    if field == "artist":
        return [", ".join(candidate.artists)]
    if field == "edition":
        return [_edition(candidate.title)]
    if field == "subgenre":
        return [value for value in candidate.genre if value in SUBGENRE_PARENT]
    if field == "label":
        return candidate.labels
    value = getattr(candidate, field, None)
    return value if isinstance(value, list) else [value]


def decisions(track, candidates, manual):
    """Return a field view from a materialized track and permitted candidates."""
    proposal = build_track_proposal(track.metadata, candidates)
    verified = [c for c in candidates if c.verification in RECORDING_STATES and _family(c) != "wikidata"]
    meta = track.metadata
    local = {
        "artist": meta.artist, "title": meta.title, "edition": _edition(meta.title),
        "duration": meta.duration, "bpm": meta.embedded_bpm, "key": meta.embedded_key,
        "genre": meta.embedded_genre, "isrc": meta.isrc,
    }
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
            value = proposal[field]
            confidence, reason = field_decision(candidates, field, proposal)
            if value == "CONFLICT" or "disagreement" in reason or "contradictory" in reason:
                state = "conflicting"
            elif confidence == "HIGH" and value:
                state = "verified"
                sources = sorted({item[1] for item in candidate_field_values(candidates, field)})
            elif field == "subgenre":
                corroborated = {}
                for item in verified:
                    for style in _candidate_values(item, field):
                        if SUBGENRE_PARENT[style] == proposal["genre"]:
                            corroborated.setdefault(style, set()).add(_family(item))
                if len(corroborated) > 1:
                    state, reason = "conflicting", "Recording-level styles disagree"
                elif corroborated:
                    value, families = next(iter(corroborated.items()))
                    if len(families) >= 2:
                        state, confidence, sources, reason = (
                            "verified", "HIGH", sorted(families), "Independent recording-level styles agree",
                        )
        else:
            matched = {}
            for item in verified:
                for candidate_value in _candidate_values(item, field):
                    if candidate_value:
                        matched.setdefault(str(candidate_value), set()).add(_family(item))
            embedded = local.get(field) and meta.identity_origin == "embedded"
            if len(matched) > 1 and field in {"edition", "isrc", "label"}:
                state, reason = "conflicting", "Recording-level values disagree"
            elif field == "edition" and meta.title and any(
                not versions_compatible(meta.title, item.title) for item in verified
            ):
                state, reason = "conflicting", "Recording editions disagree"
            elif embedded:
                state, value, confidence, sources, reason = (
                    "accepted", local[field], "MEDIUM", ["embedded"], "Local embedded value",
                )
            elif matched:
                value, families = max(matched.items(), key=lambda entry: (len(entry[1]), entry[0]))
                if len(families) >= 2 or (field in {"artist", "title", "edition", "isrc"}
                                         and any(c.verification == "ISRC supported" for c in verified)):
                    state, confidence, sources, reason = (
                        "verified", "HIGH", sorted(families), "Recording-level evidence",
                    )
        disputed = state == "conflicting"
        if field in manual and manual[field][0] and manual[field][1] in MANUAL_STATES:
            value, lock = manual[field]
            state, confidence, sources, reason = (
                "verified" if lock in {"VERIFIED", "HIGH_LOCKED"} else "accepted",
                "HIGH", ["manual"], "Preserved accepted value",
            )
        results[field] = {"state": state, "value": str(value or ""), "confidence": confidence,
                          "provenance": sources, "reason": reason, "candidates": evidence,
                          "conflict": disputed}
    return results, proposal.get("tempo_hint", "")


def _signature(result, field):
    if field == "identity":
        return tuple(_signature(result, item) for item in ("artist", "title"))
    value = result[field]
    return (value["state"], value["value"]) if value["state"] in {"accepted", "verified"} else ()


def attribution(track, candidates, manual, baseline):
    """Counterfactual attribution. This function has no access to provider clients."""
    result = {}
    families = {_family(item) for item in candidates}
    for family in families:
        reduced, _ = decisions(track, [item for item in candidates if _family(item) != family], manual)
        accepted = [field for field in ATTRIBUTED
                    if family in (baseline["artist"]["provenance"] + baseline["title"]["provenance"]
                                  if field == "identity" else baseline[field]["provenance"])]
        unique = [field for field in ATTRIBUTED if _signature(baseline, field)
                  and _signature(baseline, field) != _signature(reduced, field)]
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
    conn.execute("""CREATE TEMP TABLE IF NOT EXISTS preview_sheet (
        row_number INTEGER PRIMARY KEY, file_id TEXT NOT NULL, row_json TEXT NOT NULL
    )""")
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
        "duplicate_ids": conn.execute("""SELECT COUNT(*) FROM (
            SELECT file_id FROM preview_sheet WHERE file_id!='' GROUP BY file_id HAVING COUNT(*)>1
        )""").fetchone()[0],
        "columns": {name: _column_name(index.columns[col] + 1) for name, col in COL.items()},
    }


def _track_report(conn, db, row, config, sheet_info, folders, location_verified):
    track = db._row_to_track(row)
    terms = config.provider_terms_cleared
    candidates = [c for c in db.get_candidates(track.file_id)
                  if not c.is_stale and c.content_version in ("", track.metadata.version)
                  and (terms or _family(c) in {"musicbrainz", "wikidata"})]
    manual = {field: (value, state) for field, value, state in conn.execute(
        "SELECT field_name,value,state FROM field_decisions WHERE file_id=?", (track.file_id,),
    )}
    sheet_rows = [] if "schema_error" in sheet_info else conn.execute(
        "SELECT row_number,row_json FROM preview_sheet WHERE file_id=? LIMIT 2", (track.file_id,),
    ).fetchall()
    old = json.loads(sheet_rows[0][1]) if len(sheet_rows) == 1 else []
    locks, lock_error = parse_lock_states(old)
    for field, col in COL.items():
        lock = locks.get(field, {}).get("state")
        if old and lock in MANUAL_STATES:
            manual.setdefault(field, (old[col], lock))
    field_data, tempo_hint = decisions(track, candidates, manual)
    try:
        audit = json.loads(track.provider_calls_json)
        audit_meta = audit.get("_meta", {})
    except (ValueError, AttributeError):
        audit, audit_meta = {}, {}
    pending = bool(conn.execute("""
        SELECT 1 FROM enrichment_work WHERE file_id=? AND version=? LIMIT 1
    """, (track.file_id, track.metadata.version)).fetchone())
    research_complete = bool(audit_meta.get("lookup_complete") and not track.research_incomplete
                             and track.research_version == track.metadata.version and not track.content_stale)
    no_match = research_complete and track.research_status == "no_match"
    for field, detail in field_data.items():
        detail["old"] = old[COL[field]] if old and field in COL else ""
        detail["proposed"] = (detail["old"] or detail["value"]) if (
            detail["old"] or detail["state"] in {"accepted", "verified"}
        ) else ""
    metadata_complete = all(value["state"] in {"accepted", "verified"} for value in field_data.values())
    conflict = any(value["conflict"] for value in field_data.values()) or bool(tempo_hint)
    review = bool(track.quarantined or lock_error or conflict or (research_complete and track.research_status == "partial"))
    genre = field_data["genre"]
    targets = folders.get(_vault_parent_folder_key(canonical_genre(genre["value"])), [])
    if row["destination_member"] and genre["state"] == "verified" and len(targets) == 1 and row["destination_parent"] == targets[0] and not row["source_member"] and not row["ambiguous"]:
        route = "already_correctly_located"
    elif row["source_member"] and row["destination_member"] or row["ambiguous"]:
        route = "ambiguous_location"
    elif row["destination_member"]:
        route = "destination_only"
    elif not location_verified:
        route = "unverified_root_membership"
    elif len(sheet_rows) != 1 or lock_error:
        route = "sheet_mapping_hold"
    elif review or no_match or pending or not research_complete or not metadata_complete or genre["state"] != "verified":
        route = "review_or_incomplete"
    elif len(targets) != 1:
        route = "unknown_or_ambiguous_destination"
    else:
        route = "preview_ready"
    states = {
        "inventory_discovered": True,
        "hydration_complete": (not track.content_stale and track.hydration_version == track.metadata.version
                               and track.hydration_status in {"success", "partial", "unresolved"}),
        "research_attempted": bool(track.research_attempts or any(
            isinstance(entry, dict) and (entry.get("request_made") or entry.get("cache_hits"))
            for entry in audit.values()
        )),
        "research_complete": research_complete,
        "metadata_complete": metadata_complete,
        "enrichment_pending": pending,
        "review_required": review,
        "terminal_no_match": no_match,
        "workflow_complete": research_complete or (review and not pending and track.research_status == "unresolved"),
        "routing_ready": route == "preview_ready",
        "provider_complete": research_complete,
    }
    detail = {"file_id": short_file_id(track.file_id, verbose=False), "name": track.name,
              "states": states, "fields": field_data, "tempo_hint": tempo_hint,
              "routing": route, "destination": genre["value"] if route == "preview_ready" else "",
              "move_authorized": False, "executable": False,
              "sheet_row": sheet_rows[0][0] if len(sheet_rows) == 1 else None}
    return detail, attribution(track, candidates, manual, field_data)


def library_report(conn, db, config, args, sheets, drive):
    """Stream optional detail files and retain only the summary in memory."""
    conn.execute("PRAGMA temp_store=FILE")
    try:
        sheet_info = read_sheet(conn, sheets, config.sheet_id)
    except Exception as exc:
        sheet_info = {"schema_error": config.redact(exc)}
    try:
        folders = {}
        for name, ids in drive.get_children_folders(config.vault_folder_id).items():
            folders.setdefault(_vault_parent_folder_key(name), []).extend(ids)
    except Exception:
        folders = {}
    location_verified = (
        config.source_folder_id != config.vault_folder_id
        and conn.execute("""SELECT COUNT(DISTINCT source_folder_id) FROM scan_runs
                            WHERE source_folder_id IN (?,?) AND status='completed'""",
                         (config.source_folder_id, config.vault_folder_id)).fetchone()[0] == 2
    )
    totals = {scope: Counter() for scope in ("source", "destination", "unique")}
    field_counts = {scope: {field: Counter() for field in FIELDS} for scope in totals}
    accepted, unique = {}, {}
    columns = ("file_id", "name", "states", "fields", "tempo_hint", "routing", "destination",
               "move_authorized", "executable", "sheet_row")
    rows = conn.execute("""
        WITH membership AS (
            SELECT file_id, MAX(role='source') AS source_member,
                   MAX(role='destination') AS destination_member,
                   MAX(CASE WHEN role='destination' THEN parent_id ELSE '' END) AS destination_parent,
                   MAX(ambiguous) AS ambiguous
            FROM root_membership
            WHERE (role='source' AND root_id=?) OR (role='destination' AND root_id=?)
            GROUP BY file_id
        )
        SELECT t.*,m.source_member,m.destination_member,m.destination_parent,m.ambiguous
        FROM membership m JOIN tracks t USING(file_id)
    """, (config.source_folder_id, config.vault_folder_id))
    outputs = []
    secrets = config.known_secrets()
    try:
        with ExitStack() as stack:
            for kind, path in (("csv", args.export_csv), ("json", args.export_json)):
                if path:
                    target = Path(path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    stream = stack.enter_context(NamedTemporaryFile(
                        mode="w", encoding="utf-8", newline="", dir=target.parent, delete=False,
                    ))
                    outputs.append((kind, target, stream))
            csv_writer = next((csv.DictWriter(stream, columns) for kind, _, stream in outputs if kind == "csv"), None)
            json_stream = next((stream for kind, _, stream in outputs if kind == "json"), None)
            if csv_writer:
                csv_writer.writeheader()
            if json_stream:
                json_stream.write('{"rows":[')
            first = True
            for row in rows:
                detail, providers = _track_report(conn, db, row, config, sheet_info, folders, location_verified)
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
                        if item["conflict"] and item["state"] != "conflicting":
                            field_counts[scope][field]["conflict_with_preserved_value"] += 1
                if args.file_id and row["file_id"] not in args.file_id:
                    continue
                if csv_writer:
                    csv_writer.writerow({
                        key: sanitize_csv_cell(json.dumps(value) if isinstance(value, dict) else value,
                                               secrets)
                        for key, value in detail.items()
                    })
                if json_stream:
                    if not first:
                        json_stream.write(",")
                    json_stream.write(json.dumps(_redact_json_tree(detail, secrets), ensure_ascii=False))
                    first = False
            telemetry = provider_report(conn, config)
            for family, values in telemetry.items():
                values.update(accepted_contribution=dict(accepted.get(family, {})),
                              unique_contribution=dict(unique.get(family, {})))
            summary = {"tracks": totals, "fields": field_counts, "providers": telemetry, "sheets": sheet_info}
            if json_stream:
                json_stream.write('],"coverage":' + json.dumps(summary) + '}\n')
        for _, path, stream in outputs:
            os.replace(stream.name, path)
        return summary
    finally:
        for _, _, stream in outputs:
            Path(stream.name).unlink(missing_ok=True)
