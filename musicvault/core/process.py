"""Run the read-only library pipeline with durable local checkpoints."""
import json
import sqlite3
import time
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor

from ..db import _utc_iso
from .hydrator import hydrate_live_inventory
from .inventory import scan_root
from .researcher import build_research_providers, execute_research_batch


def inventory_counts(conn, source_id, destination_id):
    active = """FROM root_membership
        WHERE (role='source' AND root_id=?) OR (role='destination' AND root_id=?)"""
    params = (source_id, destination_id)
    return {
        "source": conn.execute("SELECT COUNT(*) FROM root_membership WHERE role='source' AND root_id=?",
                               (source_id,)).fetchone()[0],
        "destination": conn.execute(
            "SELECT COUNT(*) FROM root_membership WHERE role='destination' AND root_id=?",
            (destination_id,)).fetchone()[0],
        "unique": conn.execute("SELECT COUNT(DISTINCT file_id) " + active, params).fetchone()[0],
        "overlaps": conn.execute(
            "SELECT COUNT(*) FROM (SELECT file_id " + active
            + " GROUP BY file_id HAVING COUNT(DISTINCT role)>1)", params).fetchone()[0],
    }


def _save(conn, run_id, phase, state):
    with conn:
        conn.execute(
            "UPDATE process_runs SET phase=?,state_json=?,updated_utc=? WHERE id=?",
            (phase, json.dumps(state), _utc_iso(), run_id),
        )


def _open_run(conn, roots, args):
    selection = json.dumps({"roots": roots, "file_ids": sorted(set(args.file_id)),
                            "inventory_only": args.inventory_only}, sort_keys=True)
    with conn:
        if args.resume:
            row = conn.execute("""
                SELECT * FROM process_runs WHERE selection=? AND phase!='complete'
                ORDER BY id DESC LIMIT 1
            """, (selection,)).fetchone()
            if row is None:
                raise ValueError("No resumable run matches the roots and track selection.")
            state = json.loads(row["state_json"])
            state["epoch"] += 1
            _save(conn, row["id"], row["phase"], state)
            return row["id"], state
        state = {"roots": {}, "epoch": 1}
        row = conn.execute("""
            INSERT INTO process_runs(selection,phase,state_json,updated_utc)
            VALUES (?, 'inventory', ?, ?)
        """, (selection, json.dumps(state), _utc_iso()))
        return row.lastrowid, state


def _populate_work(conn, run_id, roots, file_ids):
    root_terms = " OR ".join("(m.role=? AND m.root_id=?)" for _ in roots)
    params = [value for pair in roots.items() for value in pair]
    id_filter = f"AND t.file_id IN ({','.join('?' for _ in file_ids)})" if file_ids else ""
    conn.execute(f"""
        INSERT INTO process_work(run_id,file_id,version)
        SELECT DISTINCT ?, t.file_id, t.version
        FROM tracks t JOIN root_membership m ON m.file_id=t.file_id
        WHERE ({root_terms}) {id_filter}
        ON CONFLICT(run_id,file_id) DO UPDATE SET
            version=excluded.version,hydrated=0,researched=0
        WHERE process_work.version!=excluded.version
    """, (run_id, *params, *file_ids))


def _checkpoint_track(conn, run, stage, track):
    run_id, epoch = run
    with conn:
        conn.execute(
            f"UPDATE process_work SET {stage}=? WHERE run_id=? AND file_id=? AND version=?",
            (epoch, run_id, track.file_id, track.metadata.version),
        )


def _next_research(db, conn, run, condition="0", only_enrichment=False):
    row = conn.execute("SELECT value FROM scheduler_state WHERE name='first_pass'").fetchone()
    streak = row[0] if row else 0
    order = ("retry", "first") if streak >= 4 else ("first", "retry")
    for category in order:
        tracks = db.get_live_tracks_for_research(
            1, process_run=run, queue_class=category, enrichment_condition=condition, only_enrichment=only_enrichment,
        )
        if tracks:
            return tracks[0], min(streak + 1, 4) if category == "first" else 0
    return None, streak


def _hydrate_batch(db, drive, drive_factory, tracks, concurrency, secrets, done):
    if concurrency == 1 or len(tracks) == 1:
        return [hydrate_live_inventory(
            db, drive, limit=len(tracks), selected_tracks=tracks, secret_values=secrets,
            progress_callback=lambda _i, _n, t, _s, _b: done(t),
        )]
    workers = min(concurrency, len(tracks))

    def hydrate_one(track):
        # Partition the shared batch cap so in-flight reads cannot overspend it.
        service = drive_factory() if drive_factory else drive
        return hydrate_live_inventory(
            db, service, limit=1, max_mib=32.0 / len(tracks),
            selected_tracks=[track], secret_values=secrets,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = []
        for track, result in zip(tracks, executor.map(hydrate_one, tracks)):
            if result.attempted and not result.interrupted:
                done(track)
            results.append(result)
        return results


def run_process(config, db, drive, args, *, drive_factory=None, policy=None,
                preview=None, providers_override=None):
    started = time.monotonic()
    roots = {"source": config.source_folder_id, "destination": config.vault_folder_id}
    roots = {role: root for role, root in roots.items() if args.scope in ("all", role)}
    with closing(db._get_connection()) as conn:
        run_id, state = _open_run(conn, roots, args)
        result = {"run_id": run_id, "roots": state["roots"], "status": "completed",
                  "dry_run": args.dry_run}
        for role, root in roots.items():
            if state["roots"].get(role, {}).get("status") == "completed":
                continue
            report = scan_root(db, drive, root, role, config.redact)
            state["roots"][role] = report
            _save(conn, run_id, "inventory", state)
            if report["status"] != "completed":
                result.update(status=report["status"], error=report["error"])
                return result

        result["inventory"] = inventory_counts(conn, config.source_folder_id, config.vault_folder_id)
        if args.inventory_only:
            _save(conn, run_id, "complete", state)
            return result

        run = (run_id, state["epoch"])
        with conn:
            _populate_work(conn, run_id, roots, args.file_id)
        if args.dry_run:
            result["eligible_hydration"] = len(db.get_live_tracks_for_hydration(1, process_run=run)) > 0
            result["eligible_research"] = len(db.get_live_tracks_for_research(1, process_run=run)) > 0
            if preview:
                result["coverage"] = preview(conn, run_id)
            return result

        _save(conn, run_id, "hydration", state)
        hydrated = 0
        while not args.provider and (not args.limit or hydrated < args.limit):
            amount = min(args.batch_size, args.limit - hydrated) if args.limit else args.batch_size
            tracks = db.get_live_tracks_for_hydration(amount, process_run=run)
            if not tracks:
                break
            summaries = _hydrate_batch(
                db, drive, drive_factory, tracks, args.concurrency, config.known_secrets(),
                lambda track: _checkpoint_track(conn, run, "hydrated", track),
            )
            attempted = sum(s.attempted for s in summaries)
            hydrated += attempted
            print(f"Hydration attempted {hydrated} tracks ({time.monotonic() - started:.1f}s elapsed)")
            if any(s.interrupted for s in summaries):
                result["status"] = "interrupted"
                return result
            if not attempted:
                break
            if args.cooldown and len(tracks) == amount:
                time.sleep(args.cooldown)

        _save(conn, run_id, "research", state)
        providers = providers_override if providers_override is not None else build_research_providers(
            config, db, args.limit or 100
        )
        if policy is not None:
            policy.bind(db, providers, args.provider)
        researched = 0
        last = None
        while not args.limit or researched < args.limit:
            if policy and researched % args.batch_size == 0:
                policy.new_batch()
            condition = policy.enrichment_condition() if policy else "0"
            track, streak = _next_research(db, conn, run, condition, bool(args.provider))
            if track is None:
                break
            summary = execute_research_batch(
                config, db, limit=1, providers_override=providers,
                selected_tracks=[track], policy=policy,
            )
            last = summary
            if summary.interrupted:
                result["status"] = "interrupted"
                return result
            _checkpoint_track(conn, run, "researched", track)
            researched += 1
            with conn:
                conn.execute("""
                    INSERT INTO scheduler_state VALUES ('first_pass', ?)
                    ON CONFLICT(name) DO UPDATE SET value=excluded.value
                """, (streak,))
            if summary.paused_all_providers_unavailable:
                break
            if researched % args.batch_size == 0:
                print(f"Research selected {researched} tracks ({time.monotonic() - started:.1f}s elapsed)")
                if args.cooldown:
                    time.sleep(args.cooldown)

        _save(conn, run_id, "preview", state)
        if preview:
            result["coverage"] = preview(conn, run_id)
        result.update(hydration_attempted=hydrated, research_selected=researched,
                      elapsed_seconds=round(time.monotonic() - started, 2))
        next_research, _ = _next_research(db, conn, run, policy.enrichment_condition() if policy else "0")
        result["work_remaining"] = bool(
            next_research or db.get_live_tracks_for_hydration(1, process_run=run)
        )
        if last:
            result["next_retry_utc"] = last.next_retry_utc
        backlog = conn.execute("""
            SELECT COUNT(*), MIN(NULLIF(e.due_utc,''))
            FROM enrichment_work e JOIN process_work w ON w.file_id=e.file_id AND w.version=e.version
            WHERE w.run_id=?
        """, (run_id,)).fetchone()
        result["enrichment_pending"] = backlog[0]
        result["next_retry_utc"] = min(filter(None, (result.get("next_retry_utc"), backlog[1])), default="")
        if result["work_remaining"] or backlog[0] or (last and last.paused_all_providers_unavailable):
            result["status"] = "paused"
            result["reason"] = "Provider work deferred or invocation limit reached."
        _save(conn, run_id, "paused" if result["status"] == "paused" else "complete", state)
        return result


def snapshot_database(source, target):
    if source.is_file() and source.stat().st_size:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(target)) as dest:
                src.backup(dest)
