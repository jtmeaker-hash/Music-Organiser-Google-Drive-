"""Publish a root's inventory only after Drive finishes enumerating it, with folder-level resume checkpoints."""
import json
from collections import Counter
from contextlib import closing

from ..db import _utc_iso
from ..services.drive import DriveService


def scan_root(
    db,
    drive,
    root_id,
    role,
    redact=str,
    on_complete=None,
    resume_scan_id=None,
    keep_partial_on_abort=False,
):
    counts = Counter()
    scan_id = None
    pre_staged = {}
    if resume_scan_id is not None:
        with closing(db._get_connection()) as conn, conn:
            prev_run = conn.execute(
                "SELECT status FROM scan_runs WHERE scan_id=? AND source_folder_id=?",
                (int(resume_scan_id), root_id),
            ).fetchone()
            if prev_run is not None and prev_run["status"] != "completed":
                scan_id = int(resume_scan_id)
                conn.execute(
                    "UPDATE scan_runs SET status='in_progress', error_text='' WHERE scan_id=?",
                    (scan_id,),
                )
                pre_staged = {
                    r[0]: r[1]
                    for r in conn.execute(
                        "SELECT file_id, item_json FROM root_observation WHERE scan_id=?",
                        (scan_id,),
                    ).fetchall()
                }
                counts["seen"] = len(pre_staged)
    if scan_id is None:
        scan_id = db.begin_scan_run(root_id)

    try:
        if isinstance(drive, DriveService) and keep_partial_on_abort:
            items = drive.scan_inventory(root_id, counts, db=db, scan_id=scan_id)
        elif isinstance(drive, DriveService):
            items = drive.scan_inventory(root_id, counts)
        else:
            items = drive.scan_inventory(root_id)

        with closing(db._get_connection()) as conn:
            for item in items:
                file_id = str(item.get("id") or "").strip()
                if not file_id:
                    raise ValueError("Drive returned an audio record without a file ID.")
                data = json.dumps(item, sort_keys=True)
                with conn:
                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO root_observation(scan_id,file_id,item_json)
                        VALUES (?, ?, ?)
                        """,
                        (scan_id, file_id, data),
                    )
                    if cursor.rowcount:
                        counts["seen"] += 1
                    elif pre_staged.get(file_id) == data:
                        # Re-observed an item already staged before an interrupted folder finished; not a duplicate
                        pre_staged.pop(file_id, None)
                    else:
                        counts["duplicates"] += 1
                        conn.execute(
                            """
                            UPDATE root_observation SET ambiguous = 1
                            WHERE scan_id = ? AND file_id = ? AND item_json != ?
                            """,
                            (scan_id, file_id, data),
                        )

            with conn:
                conn.execute("BEGIN IMMEDIATE")
                counts["newly_absent_tracks"] = conn.execute(
                    """
                    DELETE FROM root_membership WHERE role=?
                    AND (root_id!=? OR NOT EXISTS (
                        SELECT 1 FROM root_observation o
                        WHERE o.scan_id=? AND o.file_id=root_membership.file_id
                    ))
                    """,
                    (role, root_id, scan_id),
                ).rowcount
                if role == "source":
                    conn.execute(
                        """
                        UPDATE tracks SET is_live_source=0 WHERE is_live_source=1
                        AND NOT EXISTS (
                            SELECT 1 FROM root_observation o
                            WHERE o.scan_id=? AND o.file_id=tracks.file_id
                        )
                        """,
                        (scan_id,),
                    )
                source_member_ids = {
                    r[0]
                    for r in conn.execute(
                        "SELECT file_id FROM root_membership WHERE role='source'"
                    ).fetchall()
                }
                observations = conn.execute(
                    """
                    SELECT file_id, item_json, ambiguous FROM root_observation WHERE scan_id=?
                    """,
                    (scan_id,),
                ).fetchall()
                for file_id, data, ambiguous in observations:
                    item = json.loads(data)
                    ambiguous = bool(ambiguous or len(item.get("parents") or []) > 1)
                    prior = conn.execute(
                        """
                        SELECT is_live_source, configured_source_id, scan_generation,
                               quarantined, quarantine_reason, parent_id, observed_parent_id
                        FROM tracks WHERE file_id=?
                        """,
                        (file_id,),
                    ).fetchone()
                    counts[db.reconcile_scanned_file(scan_id, root_id, item, conn)] += 1
                    if role == "destination":
                        keep_source_parent = bool(
                            (prior and prior["is_live_source"]) or (file_id in source_member_ids)
                        )
                        if prior and keep_source_parent:
                            conn.execute(
                                """
                                UPDATE tracks SET
                                    is_live_source=?,
                                    configured_source_id=?,
                                    scan_generation=?,
                                    parent_id=?,
                                    observed_parent_id=?
                                WHERE file_id=?
                                """,
                                (
                                    prior["is_live_source"],
                                    prior["configured_source_id"],
                                    prior["scan_generation"],
                                    prior["parent_id"],
                                    prior["observed_parent_id"],
                                    file_id,
                                ),
                            )
                        else:
                            conn.execute(
                                """
                                UPDATE tracks SET is_live_source=0, configured_source_id='',
                                scan_generation=0 WHERE file_id=?
                                """,
                                (file_id,),
                            )
                    if prior and prior["quarantined"] and (
                        ambiguous or "checkpoint" not in (prior["quarantine_reason"] or "")
                    ):
                        conn.execute(
                            """
                            UPDATE tracks SET quarantined=1, quarantine_reason=? WHERE file_id=?
                            """,
                            (prior["quarantine_reason"], file_id),
                        )
                    parents = item.get("parents") or []
                    parent = item.get("scanned_parent") or (parents[0] if parents else "")
                    conn.execute(
                        """
                        INSERT INTO root_membership VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(role,root_id,file_id) DO UPDATE SET
                            parent_id=excluded.parent_id, path_json=excluded.path_json,
                            scan_id=excluded.scan_id, ambiguous=excluded.ambiguous
                        """,
                        (
                            role,
                            root_id,
                            file_id,
                            parent,
                            json.dumps(item.get("scanned_path", [item.get("name", "")])),
                            scan_id,
                            int(ambiguous),
                        ),
                    )
                conn.execute(
                    """
                    UPDATE scan_runs SET status='completed',completed_utc=?,seen_count=?
                    WHERE scan_id=?
                    """,
                    (_utc_iso(), counts["seen"], scan_id),
                )
                conn.execute("DELETE FROM root_observation WHERE scan_id=?", (scan_id,))
                conn.execute("DELETE FROM scan_folders WHERE scan_id=?", (scan_id,))
                result = {"status": "completed", "scan_id": scan_id, "seen": counts["seen"], **counts}
                if on_complete:
                    on_complete(conn, result)
        return result
    except (Exception, KeyboardInterrupt) as exc:
        interrupted = isinstance(exc, KeyboardInterrupt)
        error = "Interrupted by user" if interrupted else redact(exc)
        with closing(db._get_connection()) as conn:
            status = conn.execute("SELECT status FROM scan_runs WHERE scan_id=?", (scan_id,)).fetchone()[0]
        if status == "completed":
            return {"status": "completed", "scan_id": scan_id, "seen": counts["seen"], **counts}
        db.abort_scan_run(scan_id, counts["seen"], error, interrupted=interrupted)
        if not keep_partial_on_abort:
            with closing(db._get_connection()) as conn, conn:
                conn.execute("DELETE FROM root_observation WHERE scan_id=?", (scan_id,))
                conn.execute("DELETE FROM scan_folders WHERE scan_id=?", (scan_id,))
        return {
            "status": "interrupted" if interrupted else "failed",
            "error": error,
            "scan_id": scan_id,
            "seen": counts["seen"],
            **counts,
        }
