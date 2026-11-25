"""
Stage 2 tests: synthetic v2.0.0 908-record migration, verified SQLite online backup,
live vs checkpoint quarantine/reconciliation, unchanged incomplete repair, content version change
stale history archival, interrupted scan safety, checkpoint import protection, cache TTL, and rollback.
"""
import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from musicvault.db import Database, DatabaseMigrationError, SCHEMA_VERSION
from musicvault.models import Candidate, Track, TrackMetadata


def _build_legacy_v200_db(db_path: Path) -> None:
    """
    Construct a synthetic v2.0.0 database mirroring the real observed device state:
    - 50 checkpoint records imported first (23 overlap with live source, 27 checkpoint-only; 49 have artist+title, 1 blank '-')
    - 858 additional live source tracks scanned with TrackMetadata(version=...) and blank artist/title
    Total = 908 rows (881 in live source, 27 checkpoint-only; 49 complete artist+title, 859 missing).
    """
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
    CREATE TABLE tracks (
        file_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        size INTEGER DEFAULT 0,
        md5 TEXT DEFAULT '',
        modified_time TEXT DEFAULT '',
        parent_id TEXT DEFAULT '',
        artist TEXT DEFAULT '',
        title TEXT DEFAULT '',
        duration REAL DEFAULT 0.0,
        isrc TEXT DEFAULT '',
        embedded_bpm TEXT DEFAULT '',
        embedded_key TEXT DEFAULT '',
        embedded_genre TEXT DEFAULT '',
        identity_origin TEXT DEFAULT 'embedded',
        version TEXT DEFAULT '',
        last_scanned_utc TEXT DEFAULT ''
    );
    CREATE TABLE candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        title TEXT DEFAULT '',
        artists_json TEXT DEFAULT '[]',
        duration REAL,
        isrc TEXT,
        bpm REAL,
        key TEXT,
        genre_json TEXT DEFAULT '[]',
        labels_json TEXT DEFAULT '[]',
        url TEXT,
        verification TEXT DEFAULT 'Text only',
        created_utc TEXT NOT NULL,
        FOREIGN KEY (file_id) REFERENCES tracks(file_id)
    );
    CREATE TABLE field_decisions (
        file_id TEXT NOT NULL,
        field_name TEXT NOT NULL,
        value TEXT DEFAULT '',
        level TEXT DEFAULT 'LOW',
        state TEXT DEFAULT 'REVIEW',
        reason TEXT DEFAULT '',
        updated_utc TEXT NOT NULL,
        PRIMARY KEY (file_id, field_name)
    );
    CREATE TABLE provider_cache (
        provider TEXT NOT NULL,
        cache_key TEXT NOT NULL,
        response_json TEXT NOT NULL,
        created_utc TEXT NOT NULL,
        ttl_seconds INTEGER DEFAULT 86400,
        PRIMARY KEY (provider, cache_key)
    );
    CREATE TABLE rollback_manifests (
        manifest_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        created_utc TEXT NOT NULL,
        manifest_json TEXT NOT NULL
    );
    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        file_id TEXT,
        details_json TEXT NOT NULL,
        created_utc TEXT NOT NULL
    );
    """)

    now = "2026-11-25T08:00:00+00:00"
    # 50 checkpoint rows (synthetic names, blank parent_id, size=0 because v2.0.0 scan skipped matching version!)
    for idx in range(50):
        fid = f"CP_OVERLAP_{idx:03d}" if idx < 23 else f"CP_VAULT_ONLY_{idx:03d}"
        if idx == 49:
            artist, title, name = "", "", " - "
        else:
            artist, title = f"Artist {idx}", f"Title {idx}"
            name = f"{artist} - {title}"
        ver = f"md5_{idx:04d}"
        conn.execute(
            """
            INSERT INTO tracks VALUES (?, ?, 0, ?, '', '', ?, ?, 210.5, ?, '150', '8A', 'Dance', 'embedded', ?, ?)
            """,
            (fid, name, ver, artist, title, f"ISRC{idx:08d}", ver, now),
        )

    # Seed candidates on one overlapping track and one vault-only track
    conn.execute(
        """
        INSERT INTO candidates (file_id, provider, title, artists_json, duration, isrc, bpm, key, genre_json, labels_json, url, verification, created_utc)
        VALUES ('CP_OVERLAP_000', 'SonoVault', 'Title 0', '["Artist 0"]', 210.5, 'ISRC00000000', 150.0, '8A', '["Hardstyle"]', '[]', 'https://example.test/0', 'ISRC supported', ?)
        """,
        (now,),
    )

    # 858 scanned-only rows in SOURCE with empty artist/title
    for idx in range(858):
        fid = f"LIVE_SCAN_{idx:04d}"
        ver = f"scan_md5_{idx:04d}"
        conn.execute(
            """
            INSERT INTO tracks VALUES (?, ?, 35000000, ?, '2026-11-20T00:00:00Z', 'SRC_ROOT', '', '', 0.0, '', '', '', '', 'embedded', ?, ?)
            """,
            (fid, f"01 Track_{idx:04d}.wav", ver, ver, now),
        )

    conn.commit()
    conn.close()


def test_migration_preserves_908_records_creates_verified_backup_and_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "musicvault.db"
    prior_bak = tmp_path / "musicvault.db.bak"
    prior_bak.write_text("do_not_overwrite_prior_bak", encoding="utf-8")

    _build_legacy_v200_db(db_path)

    db = Database(db_path)
    assert db.get_schema_version() == SCHEMA_VERSION
    assert db.last_backup_path is not None
    assert db.last_backup_path.exists()
    assert prior_bak.read_text(encoding="utf-8") == "do_not_overwrite_prior_bak"

    # Verify backup integrity and row count == 908
    with sqlite3.connect(str(db.last_backup_path)) as bconn:
        assert bconn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert bconn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 908

    summary = db.get_inventory_summary()
    assert summary["total_records"] == 908
    # Before a v2.0.1 scan runs: 858 had parent_id='SRC_ROOT', 50 had blank parent_id (quarantined)
    assert summary["live_source_tracks"] == 858
    assert summary["checkpoint_only_records"] == 50
    assert summary["quarantined_records"] == 50

    # Existing candidates preserved
    cands = db.get_candidates("CP_OVERLAP_000")
    assert len(cands) == 1
    assert cands[0].provider == "SonoVault"

    # Re-opening migrated DB is idempotent and does NOT create redundant migration backups
    backups_before = sorted(tmp_path.glob("musicvault.db.pre_v201_migration_*.sqlite"))
    assert len(backups_before) == 1
    db2 = Database(db_path)
    backups_after = sorted(tmp_path.glob("musicvault.db.pre_v201_migration_*.sqlite"))
    assert len(backups_after) == 1
    assert db2.get_inventory_summary()["total_records"] == 908


def test_v201_scan_reconciles_881_live_vs_27_checkpoint_and_repairs_unchanged_incomplete(tmp_path: Path):
    db_path = tmp_path / "musicvault.db"
    _build_legacy_v200_db(db_path)
    db = Database(db_path)

    # Simulate live scan seeing the 23 overlapping checkpoint IDs + 858 live IDs = 881 live tracks
    scan_id = db.begin_scan_run("SRC_ROOT")
    seen = 0
    for idx in range(23):
        res = db.reconcile_scanned_file(
            scan_id,
            "SRC_ROOT",
            {
                "id": f"CP_OVERLAP_{idx:03d}",
                "name": f"Artist {idx} - Title {idx}.flac",
                "size": "24000000",
                "md5Checksum": f"md5_{idx:04d}",
                "modifiedTime": "2026-11-24T10:00:00Z",
                "scanned_parent": "SRC_ROOT",
            },
        )
        assert res == "updated_preserved"
        seen += 1

    for idx in range(858):
        res = db.reconcile_scanned_file(
            scan_id,
            "SRC_ROOT",
            {
                "id": f"LIVE_SCAN_{idx:04d}",
                "name": f"01 Track_{idx:04d}.wav",
                "size": "35000000",
                "md5Checksum": f"scan_md5_{idx:04d}",
                "modifiedTime": "2026-11-20T00:00:00Z",
                "scanned_parent": "SRC_ROOT",
            },
        )
        assert res == "updated_needs_hydration"
        seen += 1

    scan_summary = db.complete_scan_run(scan_id, "SRC_ROOT", seen)
    assert seen == 881
    assert scan_summary["live_source_tracks"] == 881
    assert scan_summary["quarantined_tracks"] == 27

    # Verify overlapping track got real Drive filename & size while preserving good metadata & candidates!
    reconciled = db.get_track("CP_OVERLAP_000")
    assert reconciled is not None
    assert reconciled.is_live_source is True
    assert reconciled.quarantined is False
    assert reconciled.name == "Artist 0 - Title 0.flac"
    assert reconciled.size == 24000000
    assert reconciled.metadata.artist == "Artist 0"
    assert reconciled.metadata.title == "Title 0"
    assert reconciled.hydration_status == "success"
    assert len(db.get_candidates("CP_OVERLAP_000")) == 1

    # Verify vault-only checkpoint record remains preserved in DB (908 total) but quarantined from live routing
    vault_only = db.get_track("CP_VAULT_ONLY_030")
    assert vault_only is not None
    assert vault_only.is_live_source is False
    assert vault_only.quarantined is True
    assert vault_only.metadata.artist == "Artist 30"

    # Unchanged incomplete tracks are scheduled for hydration without rescan or file edits
    pending_hyd = db.get_live_tracks_for_hydration(limit=10)
    assert len(pending_hyd) == 10
    assert all(t.is_live_source and not t.quarantined for t in pending_hyd)


def test_interrupted_scan_does_not_mark_unseen_files_absent(tmp_path: Path):
    db = Database(tmp_path / "scan_interrupt.db")
    s1 = db.begin_scan_run("SRC_1")
    for i in range(5):
        db.reconcile_scanned_file(s1, "SRC_1", {"id": f"F_{i}", "name": f"t{i}.mp3", "md5Checksum": f"m{i}", "scanned_parent": "SRC_1"})
    db.complete_scan_run(s1, "SRC_1", 5)
    assert db.get_inventory_summary()["live_source_tracks"] == 5

    # Second scan sees only 2 files then gets interrupted -> all 5 remain live!
    s2 = db.begin_scan_run("SRC_1")
    for i in range(2):
        db.reconcile_scanned_file(s2, "SRC_1", {"id": f"F_{i}", "name": f"t{i}.mp3", "md5Checksum": f"m{i}", "scanned_parent": "SRC_1"})
    db.abort_scan_run(s2, seen_count=2, reason="KeyboardInterrupt", interrupted=True)

    summary = db.get_inventory_summary()
    assert summary["live_source_tracks"] == 5
    assert summary["last_scan"]["status"] == "interrupted"


def test_changed_content_version_marks_old_evidence_stale_and_preserves_history(tmp_path: Path):
    db = Database(tmp_path / "version_change.db")
    s1 = db.begin_scan_run("SRC_1")
    db.reconcile_scanned_file(s1, "SRC_1", {"id": "F_VER", "name": "Song.flac", "md5Checksum": "v1_hash", "scanned_parent": "SRC_1"})
    db.complete_scan_run(s1, "SRC_1", 1)

    db.update_hydration_result(
        "F_VER",
        status="success",
        version="v1_hash",
        method="embedded_ranged",
        bytes_read=65536,
        metadata=TrackMetadata(version="v1_hash", artist="Old Artist", title="Old Title", duration=200.0, identity_origin="embedded", identity_confidence="verified_embedded"),
    )
    db.save_candidates(
        "F_VER",
        [Candidate(provider="MusicBrainz", title="Old Title", artists=["Old Artist"], verification="Duration supported")],
        content_version="v1_hash",
    )
    assert len(db.get_candidates("F_VER")) == 1

    # Rescan detects new MD5 checksum -> old evidence archived & marked stale, track queued for rehydration
    s2 = db.begin_scan_run("SRC_1")
    outcome = db.reconcile_scanned_file(s2, "SRC_1", {"id": "F_VER", "name": "Song.flac", "md5Checksum": "v2_new_hash", "scanned_parent": "SRC_1"})
    db.complete_scan_run(s2, "SRC_1", 1)
    assert outcome == "version_changed_stale"

    t = db.get_track("F_VER")
    assert t is not None
    assert t.content_stale is True
    assert t.hydration_status == "pending"
    assert t.research_status == "pending"
    # Active candidates cleared for new version, but stale candidates & evidence_history retained!
    assert len(db.get_candidates("F_VER", include_stale=False)) == 0
    assert len(db.get_candidates("F_VER", include_stale=True)) == 1
    assert db.get_inventory_summary()["archived_stale_candidates"] == 1


def test_checkpoint_import_cannot_overwrite_live_records_and_handles_blank_and_duplicate_ids(tmp_path: Path):
    db = Database(tmp_path / "cp_guard.db")
    s1 = db.begin_scan_run("SRC_1")
    db.reconcile_scanned_file(
        s1,
        "SRC_1",
        {"id": "LIVE_1", "name": "Real Drive Name.wav", "size": "48000000", "md5Checksum": "ver_1", "scanned_parent": "SRC_1"},
    )
    # Duplicate ID in same scan pass
    dup_res = db.reconcile_scanned_file(
        s1,
        "SRC_1",
        {"id": "LIVE_1", "name": "Real Drive Name.wav", "size": "48000000", "md5Checksum": "ver_1", "scanned_parent": "SRC_SUB"},
    )
    blank_res = db.reconcile_scanned_file(s1, "SRC_1", {"id": "   ", "name": "bad.wav"})
    db.complete_scan_run(s1, "SRC_1", 1)
    assert dup_res == "duplicate_in_scan"
    assert blank_res == "skipped_blank_id"

    db.update_hydration_result(
        "LIVE_1",
        status="success",
        version="ver_1",
        method="embedded_ranged",
        metadata=TrackMetadata(version="ver_1", artist="True Artist", title="True Title", duration=180.0, identity_origin="embedded", identity_confidence="verified_embedded"),
    )

    cp_file = tmp_path / "cp.json"
    cp_file.write_text(
        json.dumps(
            {
                "LIVE_1": {"version": "ver_1", "artist": "Wrong Checkpoint Artist", "title": "True Title", "duration": 180.0},
                "ONLY_CP": {"version": "ver_cp", "artist": "CP Artist", "title": "CP Title", "duration": 150.0},
                "": {"version": "bad", "artist": "X", "title": "Y"},
            }
        ),
        encoding="utf-8",
    )
    db.import_json_checkpoint(cp_file)

    live_t = db.get_track("LIVE_1")
    assert live_t is not None
    assert live_t.name == "Real Drive Name.wav"
    assert live_t.size == 48000000
    assert live_t.parent_id == "SRC_1"
    assert live_t.is_live_source is True
    assert live_t.quarantined is False
    assert live_t.metadata.artist == "True Artist"  # Never overwritten!

    conflicts = db.get_conflicts("LIVE_1")
    assert any(c["field_name"] == "artist" and c["incoming_value"] == "Wrong Checkpoint Artist" for c in conflicts)

    cp_only = db.get_track("ONLY_CP")
    assert cp_only is not None
    assert cp_only.quarantined is True
    assert cp_only.is_live_source is False


def test_forensic_rollback_from_verified_backup_and_cache_ttl(tmp_path: Path):
    db_path = tmp_path / "rollback_test.db"
    _build_legacy_v200_db(db_path)

    db = Database(db_path)
    backup_path = db.last_backup_path
    assert backup_path is not None and backup_path.exists()

    # Test cache TTL expiry in UTC
    db.set_cache("MusicBrainz", "k_valid", {"hits": [1]}, ttl_seconds=3600)
    assert db.get_cache("MusicBrainz", "k_valid") == {"hits": [1]}
    with sqlite3.connect(str(db_path)) as conn:
        old_utc = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        conn.execute("UPDATE provider_cache SET created_utc = ?, ttl_seconds = 60 WHERE cache_key = 'k_valid'", (old_utc,))
        conn.commit()
    assert db.get_cache("MusicBrainz", "k_valid") is None

    # Simulate forensic rollback (including WAL/SHM sidecar handling): preserves newer DB + backup
    forensic_path = Database.restore_verified_backup(backup_path, db_path)

    with sqlite3.connect(str(db_path)) as rconn:
        assert rconn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert rconn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 908
        tables = {r[0] for r in rconn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "schema_migrations" not in tables  # Restored cleanly to exact v2.0.0 state!
        assert forensic_path.exists() and backup_path.exists()
