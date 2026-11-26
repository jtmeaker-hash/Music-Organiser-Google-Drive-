"""Publish a root's inventory only after Drive finishes enumerating it."""
import json
from collections import Counter
from contextlib import closing

from ..db import _utc_iso
from ..services.drive import DriveService


def scan_root(db, drive, root_id, role, redact=str):
    scan_id = db.begin_scan_run(root_id)
    counts = Counter()
    try:
        items = (
            drive.scan_inventory(root_id, counts)
            if isinstance(drive, DriveService)
            else drive.scan_inventory(root_id)
        )
        with closing(db._get_connection()) as conn:
            for item in items:
                file_id = str(item.get("id") or "").strip()
                if not file_id:
                    raise ValueError("Drive returned an audio record without a file ID.")
                data = json.dumps(item, sort_keys=True)
                with conn:
                    cursor = conn.execute("""
                        INSERT OR IGNORE INTO root_observation(scan_id,file_id,item_json)
                        VALUES (?, ?, ?)
                    """, (scan_id, file_id, data))
                    if cursor.rowcount:
                        counts["seen"] += 1
                    else:
                        counts["duplicates"] += 1
                        conn.execute("""
                            UPDATE root_observation SET ambiguous = 1
                            WHERE scan_id = ? AND file_id = ? AND item_json != ?
                        """, (scan_id, file_id, data))
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                counts["newly_absent_tracks"] = conn.execute("""
                    DELETE FROM root_membership WHERE role=? AND root_id=?
                    AND NOT EXISTS (SELECT 1 FROM root_observation o
                                    WHERE o.scan_id=? AND o.file_id=root_membership.file_id)
                """, (role, root_id, scan_id)).rowcount
                if role == "source":
                    conn.execute("""
                        UPDATE tracks SET is_live_source=0 WHERE configured_source_id=?
                        AND NOT EXISTS (SELECT 1 FROM root_observation o
                                        WHERE o.scan_id=? AND o.file_id=tracks.file_id)
                    """, (root_id, scan_id))
                observations = conn.execute("""
                    SELECT file_id, item_json, ambiguous FROM root_observation WHERE scan_id=?
                """, (scan_id,))
                for file_id, data, ambiguous in observations:
                    item = json.loads(data)
                    ambiguous = bool(ambiguous or len(item.get("parents") or []) > 1)
                    prior = conn.execute("""
                        SELECT is_live_source, configured_source_id, scan_generation,
                               quarantined, quarantine_reason
                        FROM tracks WHERE file_id=?
                    """, (file_id,)).fetchone()
                    if not ambiguous or prior is None:
                        counts[db.reconcile_scanned_file(scan_id, root_id, item, conn)] += 1
                    if role == "destination":
                        conn.execute("""
                            UPDATE tracks SET is_live_source=?, configured_source_id=?,
                            scan_generation=? WHERE file_id=?
                        """, (*(tuple(prior)[:3] if prior else (0, "", 0)), file_id))
                    if prior and prior["quarantined"] and "checkpoint" not in prior["quarantine_reason"]:
                        conn.execute("""
                            UPDATE tracks SET quarantined=1, quarantine_reason=? WHERE file_id=?
                        """, (prior["quarantine_reason"], file_id))
                    parents = item.get("parents") or []
                    parent = item.get("scanned_parent") or (parents[0] if parents else "")
                    conn.execute("""
                        INSERT INTO root_membership VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(role,root_id,file_id) DO UPDATE SET
                            parent_id=excluded.parent_id, path_json=excluded.path_json,
                            scan_id=excluded.scan_id, ambiguous=excluded.ambiguous
                    """, (role, root_id, file_id, parent,
                          json.dumps(item.get("scanned_path", [item.get("name", "")])),
                          scan_id, int(ambiguous)))
                conn.execute("""
                    UPDATE scan_runs SET status='completed',completed_utc=?,seen_count=?
                    WHERE scan_id=?
                """, (_utc_iso(), counts["seen"], scan_id))
                conn.execute("DELETE FROM root_observation WHERE scan_id=?", (scan_id,))
        return {"status": "completed", "scan_id": scan_id, "seen": counts["seen"], **counts}
    except (Exception, KeyboardInterrupt) as exc:
        interrupted = isinstance(exc, KeyboardInterrupt)
        error = "Interrupted by user" if interrupted else redact(exc)
        db.abort_scan_run(scan_id, counts["seen"], error, interrupted=interrupted)
        with closing(db._get_connection()) as conn, conn:
            conn.execute("DELETE FROM root_observation WHERE scan_id=?", (scan_id,))
        return {"status": "interrupted" if interrupted else "failed", "error": error,
                "scan_id": scan_id, "seen": counts["seen"], **counts}
