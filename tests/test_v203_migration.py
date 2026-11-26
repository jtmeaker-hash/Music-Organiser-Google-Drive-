import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from musicvault.db import Database, DatabaseMigrationError
from musicvault.db_lock import database_lock

FIXTURE = Path(__file__).parent / "fixtures" / "musicvault_v202_schema.sql"


def seed_database(path):
    conn = sqlite3.connect(path)
    conn.executescript(FIXTURE.read_text())
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        INSERT INTO tracks(file_id,name,version,is_live_source,configured_source_id,observed_parent_id,
            hydration_status,research_status,provider_calls_json,research_attempts)
        VALUES ('fake_drive_id','Track.flac','version-1',1,'fake_source','fake_source',
            'success','partial','{"test":"kept"}',2)
    """)
    conn.execute("""
        INSERT INTO candidates(file_id,provider,created_utc,content_version,bpm)
        VALUES ('fake_drive_id','MusicBrainz','2026-01-01','version-1',117)
    """)
    conn.execute("""
        INSERT INTO field_decisions(file_id,field_name,value,state,updated_utc)
        VALUES ('fake_drive_id','bpm','117','USER_LOCKED','2026-01-01')
    """)
    conn.commit()
    return conn


def test_v202_wal_migration_preserves_rows_and_restores(tmp_path):
    path = tmp_path / "musicvault.db"
    with closing(seed_database(path)):
        assert path.with_name(path.name + "-wal").is_file()
        db = Database(path)
    assert db.get_schema_version() == 3
    assert db.last_backup_path is not None
    assert Database(path).last_backup_path is None
    restored = tmp_path / "rehearsal.db"
    Database.restore_verified_backup(db.last_backup_path, restored)
    legacy = Database(restored, initialize=False)
    assert legacy.get_schema_version() == 2
    assert legacy.verify_integrity()[0]
    for table in ("tracks", "candidates", "field_decisions"):
        with closing(db._get_connection()) as new, closing(legacy._get_connection()) as old:
            assert [tuple(row) for row in old.execute(f"SELECT * FROM {table}")] == [
                tuple(row) for row in new.execute(f"SELECT * FROM {table}")
            ]


def test_migration_rollback_and_lock_contention(tmp_path, monkeypatch):
    path = tmp_path / "musicvault.db"
    with closing(seed_database(path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(DatabaseMigrationError):
            Database(path)
        writer.rollback()
    with database_lock(path):
        with pytest.raises(RuntimeError, match="holds the database lock"):
            with database_lock(path):
                pytest.fail("second lock opened")

    def fail(self, conn):
        conn.execute("CREATE TABLE injected_failure(id INTEGER)")
        raise RuntimeError("interrupted transaction")

    monkeypatch.setattr(Database, "_ensure_v3_tables", fail)
    with pytest.raises(DatabaseMigrationError, match="aborted"):
        Database(path)
    db = Database(path, initialize=False)
    assert db.get_schema_version() == 2
    with closing(db._get_connection()) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='injected_failure'").fetchone() is None
        assert conn.execute("SELECT count(*) FROM candidates").fetchone()[0] == 1
