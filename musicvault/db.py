"""
Persistent SQLite database, transactional schema migrations, verified online backups,
inventory reconciliation, and checkpoint management for Music Vault Manager.
"""
import json
import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .models import Candidate, Track, TrackMetadata

SCHEMA_VERSION = 2

_AUDIO_EXTENSIONS = (
    ".mp3", ".wav", ".flac", ".aiff", ".aif",
    ".m4a", ".ogg", ".opus", ".aac",
)


class DatabaseMigrationError(RuntimeError):
    """Raised when database integrity verification, backup, or migration fails."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: Optional[datetime] = None) -> str:
    return (dt or _utc_now()).isoformat()


def _has_audio_extension(name: str) -> bool:
    return Path(name or "").suffix.lower() in _AUDIO_EXTENSIONS


_TRACKS_V2_COLUMNS: List[Tuple[str, str]] = [
    ("mime_type", "TEXT DEFAULT ''"),
    ("record_origin", "TEXT DEFAULT 'unknown'"),
    ("is_live_source", "INTEGER DEFAULT 0"),
    ("quarantined", "INTEGER DEFAULT 0"),
    ("quarantine_reason", "TEXT DEFAULT ''"),
    ("configured_source_id", "TEXT DEFAULT ''"),
    ("observed_parent_id", "TEXT DEFAULT ''"),
    ("scan_generation", "INTEGER DEFAULT 0"),
    ("content_stale", "INTEGER DEFAULT 0"),
    ("raw_artist", "TEXT DEFAULT ''"),
    ("raw_title", "TEXT DEFAULT ''"),
    ("raw_bpm", "TEXT DEFAULT ''"),
    ("raw_key", "TEXT DEFAULT ''"),
    ("raw_genre", "TEXT DEFAULT ''"),
    ("raw_isrc", "TEXT DEFAULT ''"),
    ("identity_confidence", "TEXT DEFAULT ''"),
    ("sheet_artist", "TEXT DEFAULT ''"),
    ("sheet_title", "TEXT DEFAULT ''"),
    ("filename_artist_hint", "TEXT DEFAULT ''"),
    ("filename_title_hint", "TEXT DEFAULT ''"),
    ("filename_remixer_hint", "TEXT DEFAULT ''"),
    ("audio_format", "TEXT DEFAULT ''"),
    ("hydration_status", "TEXT DEFAULT 'pending'"),
    ("hydration_version", "TEXT DEFAULT ''"),
    ("hydration_method", "TEXT DEFAULT ''"),
    ("hydration_error", "TEXT DEFAULT ''"),
    ("hydration_bytes", "INTEGER DEFAULT 0"),
    ("hydration_attempts", "INTEGER DEFAULT 0"),
    ("last_hydrated_utc", "TEXT DEFAULT ''"),
    ("next_hydrate_retry_utc", "TEXT DEFAULT ''"),
    ("research_status", "TEXT DEFAULT 'pending'"),
    ("research_version", "TEXT DEFAULT ''"),
    ("research_attempts", "INTEGER DEFAULT 0"),
    ("research_last_utc", "TEXT DEFAULT ''"),
    ("research_next_retry_utc", "TEXT DEFAULT ''"),
    ("research_error", "TEXT DEFAULT ''"),
    ("provider_calls_json", "TEXT DEFAULT '{}'"),
]

_CANDIDATES_V2_COLUMNS: List[Tuple[str, str]] = [
    ("content_version", "TEXT DEFAULT ''"),
    ("is_stale", "INTEGER DEFAULT 0"),
    ("match_confidence", "TEXT DEFAULT ''"),
    ("source_family", "TEXT DEFAULT ''"),
]


class Database:
    """Manages persistent SQLite operations, verified migrations, caching, and checkpoints."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.last_backup_path: Optional[Path] = None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_or_migrate()

    def _get_connection(self, isolation_level: Optional[str] = "") -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=isolation_level)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _check_connection_integrity(conn: sqlite3.Connection) -> Tuple[bool, str]:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        msgs = [str(r[0]) for r in rows]
        ok = len(msgs) == 1 and msgs[0].lower() == "ok"
        return ok, "; ".join(msgs)

    def verify_integrity(self) -> Tuple[bool, str]:
        """Run PRAGMA integrity_check on the active database."""
        if not self.db_path.exists():
            return True, "not_created_yet"
        with self._get_connection() as conn:
            return self._check_connection_integrity(conn)

    def create_verified_backup(self, label: str = "backup") -> Path:
        """
        Create a unique timestamped SQLite online backup (including WAL snapshot)
        and verify PRAGMA integrity_check + table row counts on both original and backup.
        Never overwrites any existing backup file.
        """
        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            raise DatabaseMigrationError(f"Database file does not exist or is empty: {self.db_path}")

        safe_label = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in label)
        ts = _utc_now().strftime("%Y%m%dT%H%M%S_%fZ")
        base_name = f"{self.db_path.name}.{safe_label}_{ts}.sqlite"
        backup_path = self.db_path.parent / base_name
        suffix_idx = 1
        while backup_path.exists():
            backup_path = self.db_path.parent / f"{self.db_path.name}.{safe_label}_{ts}_{suffix_idx}.sqlite"
            suffix_idx += 1

        src_conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        dst_conn: Optional[sqlite3.Connection] = None
        try:
            src_ok, src_msg = self._check_connection_integrity(src_conn)
            if not src_ok:
                raise DatabaseMigrationError(
                    f"Original database failed PRAGMA integrity_check ({src_msg}); aborting before migration."
                )

            src_tables = [
                r[0]
                for r in src_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            src_counts = {
                tbl: src_conn.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]
                for tbl in src_tables
            }

            dst_conn = sqlite3.connect(str(backup_path), timeout=30.0)
            src_conn.backup(dst_conn)
            dst_conn.commit()

            try:
                os.chmod(backup_path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass

            dst_ok, dst_msg = self._check_connection_integrity(dst_conn)
            if not dst_ok:
                raise DatabaseMigrationError(
                    f"Backup database failed PRAGMA integrity_check ({dst_msg}); aborting."
                )

            for tbl, expected_cnt in src_counts.items():
                actual_cnt = dst_conn.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]
                if actual_cnt != expected_cnt:
                    raise DatabaseMigrationError(
                        f"Backup verification mismatch on table '{tbl}': expected {expected_cnt}, got {actual_cnt}"
                    )
        except Exception:
            if dst_conn is not None:
                dst_conn.close()
                dst_conn = None
            if backup_path.exists():
                try:
                    backup_path.unlink()
                except OSError:
                    pass
            raise
        finally:
            if dst_conn is not None:
                dst_conn.close()
            src_conn.close()

        self.last_backup_path = backup_path
        return backup_path

    @staticmethod
    def restore_verified_backup(backup_path: Path, target_db_path: Path) -> Path:
        """
        Safely restore a verified backup after moving the current DB and any WAL/SHM/journal
        sidecars aside for forensic preservation. Never deletes backups or overwrites active files.
        Returns the forensic archive path of the displaced database.
        """
        backup_path = Path(backup_path)
        target_db_path = Path(target_db_path)
        if not backup_path.exists() or backup_path.stat().st_size == 0:
            raise DatabaseMigrationError(f"Backup file does not exist or is empty: {backup_path}")

        with sqlite3.connect(str(backup_path), timeout=30.0) as bconn:
            ok, msg = Database._check_connection_integrity(bconn)
            if not ok:
                raise DatabaseMigrationError(f"Refusing to restore corrupt backup ({msg}): {backup_path}")

        ts = _utc_now().strftime("%Y%m%dT%H%M%S_%fZ")
        forensic_base = target_db_path.parent / f"{target_db_path.name}.forensic_{ts}.sqlite"
        idx = 1
        while forensic_base.exists():
            forensic_base = target_db_path.parent / f"{target_db_path.name}.forensic_{ts}_{idx}.sqlite"
            idx += 1

        if target_db_path.exists():
            target_db_path.rename(forensic_base)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(target_db_path) + suffix)
            if sidecar.exists():
                sidecar.rename(Path(str(forensic_base) + suffix))

        with sqlite3.connect(str(backup_path), timeout=30.0) as src, sqlite3.connect(str(target_db_path), timeout=30.0) as dst:
            src.backup(dst)
            dst.commit()
            ok, msg = Database._check_connection_integrity(dst)
            if not ok:
                raise DatabaseMigrationError(f"Restored database failed integrity check ({msg})")

        try:
            os.chmod(target_db_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return forensic_base

    def get_schema_version(self) -> int:
        """Return applied schema version (0 if uninitialized, 1 if legacy v2.0.0, 2+ if migrated)."""
        if not self.db_path.exists() or self.db_path.stat().st_size == 0:
            return 0
        with self._get_connection() as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "schema_migrations" in tables:
                row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
                if row and row[0] is not None:
                    return int(row[0])
            if "tracks" in tables:
                return 1
            return 0

    def _init_or_migrate(self) -> None:
        """Initialize fresh v2 database or transactionally migrate v2.0.0 database after backup."""
        current_ver = self.get_schema_version()
        if current_ver >= SCHEMA_VERSION:
            # Ensure auxiliary indexes/tables exist idempotently without re-backing-up
            with self._get_connection() as conn:
                self._ensure_auxiliary_tables(conn)
            return

        if current_ver == 0:
            self._create_fresh_v2_schema()
            try:
                os.chmod(self.db_path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            return

        # Existing legacy v1 (v2.0.0) database -> create verified online backup BEFORE touching DB!
        backup_path = self.create_verified_backup(label="pre_v201_migration")
        self._migrate_v1_to_v2(backup_path)

    def _create_fresh_v2_schema(self) -> None:
        now = _utc_iso()
        extra_track_cols = ",\n                ".join(f"{col} {ddl}" for col, ddl in _TRACKS_V2_COLUMNS)
        extra_cand_cols = ",\n                ".join(f"{col} {ddl}" for col, ddl in _CANDIDATES_V2_COLUMNS)
        conn = self._get_connection(isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"""
            CREATE TABLE IF NOT EXISTS tracks (
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
                last_scanned_utc TEXT DEFAULT '',
                {extra_track_cols}
            )
            """)
            conn.execute(f"""
            CREATE TABLE IF NOT EXISTS candidates (
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
                {extra_cand_cols},
                FOREIGN KEY (file_id) REFERENCES tracks(file_id)
            )
            """)
            self._ensure_auxiliary_tables(conn)
            conn.execute(
                "INSERT OR REPLACE INTO schema_migrations (version, applied_utc, backup_path, notes) VALUES (?, ?, ?, ?)",
                (SCHEMA_VERSION, now, "", "initial_v2_schema"),
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _ensure_auxiliary_tables(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS field_decisions (
            file_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            value TEXT DEFAULT '',
            level TEXT DEFAULT 'LOW',
            state TEXT DEFAULT 'REVIEW',
            reason TEXT DEFAULT '',
            updated_utc TEXT NOT NULL,
            PRIMARY KEY (file_id, field_name),
            FOREIGN KEY (file_id) REFERENCES tracks(file_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS provider_cache (
            provider TEXT NOT NULL,
            cache_key TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_utc TEXT NOT NULL,
            ttl_seconds INTEGER DEFAULT 86400,
            PRIMARY KEY (provider, cache_key)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS rollback_manifests (
            manifest_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            created_utc TEXT NOT NULL,
            manifest_json TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            file_id TEXT,
            details_json TEXT NOT NULL,
            created_utc TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_utc TEXT NOT NULL,
            backup_path TEXT DEFAULT '',
            notes TEXT DEFAULT ''
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_runs (
            scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_folder_id TEXT NOT NULL,
            started_utc TEXT NOT NULL,
            completed_utc TEXT DEFAULT '',
            status TEXT NOT NULL,
            seen_count INTEGER DEFAULT 0,
            error_text TEXT DEFAULT ''
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS metadata_conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            existing_value TEXT DEFAULT '',
            incoming_value TEXT DEFAULT '',
            existing_source TEXT DEFAULT '',
            incoming_source TEXT DEFAULT '',
            version TEXT DEFAULT '',
            created_utc TEXT NOT NULL,
            resolved INTEGER DEFAULT 0
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS evidence_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT NOT NULL,
            old_version TEXT DEFAULT '',
            archived_utc TEXT NOT NULL,
            reason TEXT NOT NULL,
            track_snapshot_json TEXT NOT NULL,
            candidates_json TEXT NOT NULL
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_file ON candidates(file_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_live_hydration ON tracks(is_live_source, quarantined, hydration_status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_live_research ON tracks(is_live_source, quarantined, research_status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conflicts_file ON metadata_conflicts(file_id)")

    def _migrate_v1_to_v2(self, backup_path: Path) -> None:
        """Transactionally migrate a v2.0.0 database to schema v2 without dropping or losing records."""
        now = _utc_iso()
        conn = self._get_connection(isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")

            existing_track_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(tracks)").fetchall()
            }
            for col, ddl in _TRACKS_V2_COLUMNS:
                if col not in existing_track_cols:
                    conn.execute(f"ALTER TABLE tracks ADD COLUMN {col} {ddl}")

            existing_cand_cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(candidates)").fetchall()
            }
            for col, ddl in _CANDIDATES_V2_COLUMNS:
                if col not in existing_cand_cols:
                    conn.execute(f"ALTER TABLE candidates ADD COLUMN {col} {ddl}")

            self._ensure_auxiliary_tables(conn)

            # 1. Quarantine blank/invalid file_ids without deleting rows
            conn.execute("""
            UPDATE tracks
            SET record_origin = 'invalid',
                is_live_source = 0,
                quarantined = 1,
                quarantine_reason = 'blank_file_id',
                hydration_status = 'permanent_unsupported',
                research_status = 'permanent_unsupported'
            WHERE TRIM(COALESCE(file_id, '')) = ''
            """)

            # 2. Quarantine legacy checkpoint-only rows (blank parent_id and zero size from import_json_checkpoint)
            conn.execute("""
            UPDATE tracks
            SET record_origin = 'checkpoint_import',
                is_live_source = 0,
                quarantined = 1,
                quarantine_reason = 'legacy_checkpoint_unconfirmed_live'
            WHERE TRIM(COALESCE(file_id, '')) != ''
              AND TRIM(COALESCE(parent_id, '')) = ''
              AND COALESCE(size, 0) = 0
            """)

            # 3. Mark rows observed with non-empty parent_id from v2.0.0 scan as live_scan
            conn.execute("""
            UPDATE tracks
            SET record_origin = 'live_scan',
                observed_parent_id = parent_id,
                is_live_source = 1,
                quarantined = 0,
                quarantine_reason = ''
            WHERE TRIM(COALESCE(file_id, '')) != ''
              AND TRIM(COALESCE(parent_id, '')) != ''
            """)

            # 4. Remaining non-empty IDs with size > 0 but blank parent: keep unquarantined non-live until scan confirms
            conn.execute("""
            UPDATE tracks
            SET record_origin = CASE WHEN record_origin = 'unknown' THEN 'legacy_cached' ELSE record_origin END
            WHERE TRIM(COALESCE(file_id, '')) != ''
              AND record_origin = 'unknown'
            """)

            # 5. Backfill raw_* fields and hydration/identity status
            conn.execute("""
            UPDATE tracks
            SET raw_artist = CASE WHEN raw_artist = '' THEN COALESCE(artist, '') ELSE raw_artist END,
                raw_title = CASE WHEN raw_title = '' THEN COALESCE(title, '') ELSE raw_title END,
                raw_bpm = CASE WHEN raw_bpm = '' THEN COALESCE(embedded_bpm, '') ELSE raw_bpm END,
                raw_key = CASE WHEN raw_key = '' THEN COALESCE(embedded_key, '') ELSE raw_key END,
                raw_genre = CASE WHEN raw_genre = '' THEN COALESCE(embedded_genre, '') ELSE raw_genre END,
                raw_isrc = CASE WHEN raw_isrc = '' THEN COALESCE(isrc, '') ELSE raw_isrc END
            """)

            conn.execute("""
            UPDATE tracks
            SET hydration_status = 'success',
                hydration_version = COALESCE(version, ''),
                hydration_method = 'legacy_embedded',
                identity_confidence = 'verified_embedded'
            WHERE quarantined = 0
              AND TRIM(COALESCE(artist, '')) != ''
              AND TRIM(COALESCE(title, '')) != ''
              AND COALESCE(identity_origin, 'embedded') = 'embedded'
            """)

            # Checkpoint-imported rows also get accurate metadata status preserved while quarantined from routing
            conn.execute("""
            UPDATE tracks
            SET hydration_status = CASE
                    WHEN TRIM(COALESCE(artist, '')) != '' AND TRIM(COALESCE(title, '')) != '' AND COALESCE(identity_origin, 'embedded') = 'embedded'
                    THEN 'success'
                    WHEN TRIM(COALESCE(artist, '')) != '' AND TRIM(COALESCE(title, '')) != ''
                    THEN 'partial'
                    ELSE 'pending'
                END,
                hydration_version = CASE
                    WHEN TRIM(COALESCE(artist, '')) != '' AND TRIM(COALESCE(title, '')) != ''
                    THEN COALESCE(version, '')
                    ELSE ''
                END,
                identity_confidence = CASE
                    WHEN TRIM(COALESCE(artist, '')) != '' AND TRIM(COALESCE(title, '')) != '' AND COALESCE(identity_origin, 'embedded') = 'embedded'
                    THEN 'verified_embedded'
                    WHEN TRIM(COALESCE(artist, '')) != '' AND TRIM(COALESCE(title, '')) != ''
                    THEN 'filename_parsed'
                    ELSE 'unresolved'
                END
            WHERE record_origin = 'checkpoint_import'
            """)

            conn.execute("""
            UPDATE tracks
            SET hydration_status = 'partial',
                hydration_version = COALESCE(version, ''),
                hydration_method = 'legacy_filename_fallback',
                identity_confidence = 'filename_parsed'
            WHERE quarantined = 0
              AND TRIM(COALESCE(artist, '')) != ''
              AND TRIM(COALESCE(title, '')) != ''
              AND COALESCE(identity_origin, '') != 'embedded'
            """)

            conn.execute("""
            UPDATE tracks
            SET hydration_status = 'pending',
                identity_confidence = 'unresolved'
            WHERE TRIM(COALESCE(file_id, '')) != ''
              AND (TRIM(COALESCE(artist, '')) = '' OR TRIM(COALESCE(title, '')) = '')
              AND hydration_status != 'permanent_unsupported'
            """)

            # 6. Backfill candidate content_version and track research_status
            conn.execute("""
            UPDATE candidates
            SET content_version = COALESCE(
                (SELECT t.version FROM tracks t WHERE t.file_id = candidates.file_id),
                ''
            )
            WHERE COALESCE(content_version, '') = ''
            """)

            conn.execute("""
            UPDATE tracks
            SET research_status = 'success',
                research_version = COALESCE(version, '')
            WHERE EXISTS (
                SELECT 1 FROM candidates c WHERE c.file_id = tracks.file_id AND COALESCE(c.is_stale, 0) = 0
            )
            """)

            conn.execute(
                "INSERT INTO audit_log (event_type, file_id, details_json, created_utc) VALUES (?, ?, ?, ?)",
                (
                    "SCHEMA_MIGRATION",
                    None,
                    json.dumps({"from_version": 1, "to_version": SCHEMA_VERSION, "backup_file": backup_path.name}),
                    now,
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_migrations (version, applied_utc, backup_path, notes) VALUES (?, ?, ?, ?)",
                (SCHEMA_VERSION, now, backup_path.name, "migrated_from_v2_0_0"),
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except Exception as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise DatabaseMigrationError(f"Migration to schema v{SCHEMA_VERSION} failed and rolled back: {exc}") from exc
        finally:
            conn.close()

    # --- Row Deserialization ---

    @staticmethod
    def _row_to_track(row: sqlite3.Row) -> Track:
        keys = set(row.keys())
        meta = TrackMetadata(
            version=row["version"] or "",
            artist=row["artist"] or "",
            title=row["title"] or "",
            duration=float(row["duration"] or 0.0),
            isrc=row["isrc"] or "",
            embedded_bpm=row["embedded_bpm"] or "",
            embedded_key=row["embedded_key"] or "",
            embedded_genre=row["embedded_genre"] or "",
            identity_origin=row["identity_origin"] or "embedded",
            raw_artist=row["raw_artist"] if "raw_artist" in keys else "",
            raw_title=row["raw_title"] if "raw_title" in keys else "",
            raw_bpm=row["raw_bpm"] if "raw_bpm" in keys else "",
            raw_key=row["raw_key"] if "raw_key" in keys else "",
            raw_genre=row["raw_genre"] if "raw_genre" in keys else "",
            raw_isrc=row["raw_isrc"] if "raw_isrc" in keys else "",
            identity_confidence=row["identity_confidence"] if "identity_confidence" in keys else "",
            sheet_artist=row["sheet_artist"] if "sheet_artist" in keys else "",
            sheet_title=row["sheet_title"] if "sheet_title" in keys else "",
            filename_artist_hint=row["filename_artist_hint"] if "filename_artist_hint" in keys else "",
            filename_title_hint=row["filename_title_hint"] if "filename_title_hint" in keys else "",
            filename_remixer_hint=row["filename_remixer_hint"] if "filename_remixer_hint" in keys else "",
            audio_format=row["audio_format"] if "audio_format" in keys else "",
        )
        return Track(
            file_id=row["file_id"],
            name=row["name"] or "",
            size=int(row["size"] or 0),
            md5=row["md5"] or "",
            modified_time=row["modified_time"] or "",
            parent_id=row["parent_id"] or "",
            metadata=meta,
            mime_type=row["mime_type"] if "mime_type" in keys else "",
            record_origin=row["record_origin"] if "record_origin" in keys else "unknown",
            is_live_source=bool(row["is_live_source"]) if "is_live_source" in keys else False,
            quarantined=bool(row["quarantined"]) if "quarantined" in keys else False,
            quarantine_reason=row["quarantine_reason"] if "quarantine_reason" in keys else "",
            configured_source_id=row["configured_source_id"] if "configured_source_id" in keys else "",
            observed_parent_id=row["observed_parent_id"] if "observed_parent_id" in keys else "",
            scan_generation=int(row["scan_generation"] or 0) if "scan_generation" in keys else 0,
            content_stale=bool(row["content_stale"]) if "content_stale" in keys else False,
            hydration_status=row["hydration_status"] if "hydration_status" in keys else "pending",
            hydration_version=row["hydration_version"] if "hydration_version" in keys else "",
            hydration_method=row["hydration_method"] if "hydration_method" in keys else "",
            hydration_error=row["hydration_error"] if "hydration_error" in keys else "",
            hydration_bytes=int(row["hydration_bytes"] or 0) if "hydration_bytes" in keys else 0,
            hydration_attempts=int(row["hydration_attempts"] or 0) if "hydration_attempts" in keys else 0,
            last_hydrated_utc=row["last_hydrated_utc"] if "last_hydrated_utc" in keys else "",
            next_hydrate_retry_utc=row["next_hydrate_retry_utc"] if "next_hydrate_retry_utc" in keys else "",
            research_status=row["research_status"] if "research_status" in keys else "pending",
            research_version=row["research_version"] if "research_version" in keys else "",
            research_attempts=int(row["research_attempts"] or 0) if "research_attempts" in keys else 0,
            research_last_utc=row["research_last_utc"] if "research_last_utc" in keys else "",
            research_next_retry_utc=row["research_next_retry_utc"] if "research_next_retry_utc" in keys else "",
            research_error=row["research_error"] if "research_error" in keys else "",
            provider_calls_json=row["provider_calls_json"] if "provider_calls_json" in keys else "{}",
        )

    # --- Conflict & Evidence Archival Helpers ---

    def _record_conflict_conn(
        self,
        conn: sqlite3.Connection,
        file_id: str,
        field_name: str,
        existing_value: Any,
        incoming_value: Any,
        existing_source: str,
        incoming_source: str,
        version: str,
    ) -> None:
        ex_str = str(existing_value or "").strip()
        in_str = str(incoming_value or "").strip()
        if not ex_str or not in_str or ex_str == in_str:
            return
        conn.execute(
            """
            INSERT INTO metadata_conflicts (
                file_id, field_name, existing_value, incoming_value,
                existing_source, incoming_source, version, created_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (file_id, field_name, ex_str, in_str, existing_source, incoming_source, version, _utc_iso()),
        )

    def record_conflict(
        self,
        file_id: str,
        field_name: str,
        existing_value: Any,
        incoming_value: Any,
        existing_source: str,
        incoming_source: str,
        version: str = "",
    ) -> None:
        with self._get_connection() as conn:
            self._record_conflict_conn(
                conn, file_id, field_name, existing_value, incoming_value,
                existing_source, incoming_source, version
            )

    def get_conflicts(self, file_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            if file_id:
                rows = conn.execute(
                    "SELECT * FROM metadata_conflicts WHERE file_id = ? ORDER BY id ASC", (file_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM metadata_conflicts ORDER BY id ASC").fetchall()
            return [dict(r) for r in rows]

    def _archive_stale_version_conn(
        self, conn: sqlite3.Connection, existing_row: sqlite3.Row, reason: str
    ) -> None:
        fid = existing_row["file_id"]
        old_ver = existing_row["version"] or ""
        cands = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM candidates WHERE file_id = ? AND COALESCE(is_stale, 0) = 0", (fid,)
            ).fetchall()
        ]
        conn.execute(
            """
            INSERT INTO evidence_history (
                file_id, old_version, archived_utc, reason, track_snapshot_json, candidates_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (fid, old_ver, _utc_iso(), reason, json.dumps(dict(existing_row)), json.dumps(cands)),
        )
        conn.execute("UPDATE candidates SET is_stale = 1 WHERE file_id = ?", (fid,))

    # --- Checkpoints & Safe Track Persistence ---

    def save_track(self, track: Track) -> None:
        """
        Safely upsert a track record without overwriting existing non-blank metadata with blank values.
        Conflicts between two non-blank values on the same version are logged for review.
        """
        fid = str(track.file_id or "").strip()
        if not fid:
            return

        now = _utc_iso()
        with self._get_connection() as conn:
            existing = conn.execute("SELECT * FROM tracks WHERE file_id = ?", (fid,)).fetchone()
            meta = track.metadata
            in_ver = meta.version or track.md5 or track.modified_time or ""

            if existing is None:
                has_identity = bool(meta.artist.strip() and meta.title.strip())
                hyd_status = track.hydration_status
                if hyd_status == "pending" and has_identity:
                    hyd_status = "success" if meta.identity_origin == "embedded" else "partial"
                id_conf = meta.identity_confidence
                if not id_conf:
                    if has_identity and meta.identity_origin == "embedded":
                        id_conf = "verified_embedded"
                    elif has_identity:
                        id_conf = "filename_parsed"
                    else:
                        id_conf = "unresolved"

                conn.execute(
                    """
                    INSERT INTO tracks (
                        file_id, name, size, md5, modified_time, parent_id,
                        artist, title, duration, isrc, embedded_bpm, embedded_key,
                        embedded_genre, identity_origin, version, last_scanned_utc,
                        mime_type, record_origin, is_live_source, quarantined, quarantine_reason,
                        configured_source_id, observed_parent_id, scan_generation, content_stale,
                        raw_artist, raw_title, raw_bpm, raw_key, raw_genre, raw_isrc,
                        identity_confidence, sheet_artist, sheet_title,
                        filename_artist_hint, filename_title_hint, filename_remixer_hint, audio_format,
                        hydration_status, hydration_version, hydration_method, hydration_error,
                        hydration_bytes, hydration_attempts, last_hydrated_utc, next_hydrate_retry_utc,
                        research_status, research_version, research_attempts, research_last_utc,
                        research_next_retry_utc, research_error, provider_calls_json
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?
                    )
                    """,
                    (
                        fid, track.name, track.size, track.md5, track.modified_time, track.parent_id,
                        meta.artist, meta.title, meta.duration, meta.isrc, meta.embedded_bpm, meta.embedded_key,
                        meta.embedded_genre, meta.identity_origin, in_ver, now,
                        track.mime_type, track.record_origin, int(track.is_live_source), int(track.quarantined),
                        track.quarantine_reason, track.configured_source_id,
                        track.observed_parent_id or track.parent_id, track.scan_generation, int(track.content_stale),
                        meta.raw_artist or meta.artist, meta.raw_title or meta.title,
                        meta.raw_bpm or meta.embedded_bpm, meta.raw_key or meta.embedded_key,
                        meta.raw_genre or meta.embedded_genre, meta.raw_isrc or meta.isrc,
                        id_conf, meta.sheet_artist, meta.sheet_title,
                        meta.filename_artist_hint, meta.filename_title_hint, meta.filename_remixer_hint, meta.audio_format,
                        hyd_status, track.hydration_version or (in_ver if has_identity else ""),
                        track.hydration_method, track.hydration_error,
                        track.hydration_bytes, track.hydration_attempts, track.last_hydrated_utc, track.next_hydrate_retry_utc,
                        track.research_status, track.research_version, track.research_attempts, track.research_last_utc,
                        track.research_next_retry_utc, track.research_error, track.provider_calls_json,
                    ),
                )
                return

            # Existing record: preserve good non-blank values unless content version changed
            ex_ver = existing["version"] or ""
            version_changed = bool(ex_ver and in_ver and ex_ver != in_ver)
            if version_changed:
                self._archive_stale_version_conn(conn, existing, reason="save_track_version_changed")

            def _pick_str(field_col: str, new_val: str, allow_replace_on_new_ver: bool = True) -> str:
                old_val = str(existing[field_col] or "")
                new_clean = str(new_val or "")
                if version_changed and allow_replace_on_new_ver:
                    return new_clean
                if not new_clean:
                    return old_val
                if not old_val:
                    return new_clean
                if old_val != new_clean:
                    # Allow upgrading from filename fallback/hint to embedded without losing conflict audit
                    old_origin = existing["identity_origin"] or ""
                    new_origin = meta.identity_origin or ""
                    self._record_conflict_conn(
                        conn, fid, field_col, old_val, new_clean, old_origin, new_origin, in_ver or ex_ver
                    )
                    if new_origin == "embedded" and old_origin != "embedded":
                        return new_clean
                    return old_val
                return old_val

            merged_name = track.name if (track.name and ( _has_audio_extension(track.name) or not existing["name"] )) else existing["name"]
            merged_size = track.size if track.size > 0 else int(existing["size"] or 0)
            merged_md5 = track.md5 or existing["md5"] or ""
            merged_mod = track.modified_time or existing["modified_time"] or ""
            merged_parent = track.parent_id or existing["parent_id"] or ""

            merged_artist = _pick_str("artist", meta.artist)
            merged_title = _pick_str("title", meta.title)
            merged_dur = (
                meta.duration
                if (version_changed or (meta.duration > 0 and float(existing["duration"] or 0.0) <= 0))
                else (float(existing["duration"] or 0.0) or meta.duration)
            )
            merged_isrc = _pick_str("isrc", meta.isrc)
            merged_bpm = _pick_str("embedded_bpm", meta.embedded_bpm)
            merged_key = _pick_str("embedded_key", meta.embedded_key)
            merged_genre = _pick_str("embedded_genre", meta.embedded_genre)

            merged_origin = meta.identity_origin
            if not version_changed and existing["identity_origin"] == "embedded" and merged_origin != "embedded":
                merged_origin = "embedded"

            conn.execute(
                """
                UPDATE tracks SET
                    name = ?,
                    size = ?,
                    md5 = ?,
                    modified_time = ?,
                    parent_id = ?,
                    artist = ?,
                    title = ?,
                    duration = ?,
                    isrc = ?,
                    embedded_bpm = ?,
                    embedded_key = ?,
                    embedded_genre = ?,
                    identity_origin = ?,
                    version = ?,
                    last_scanned_utc = ?,
                    mime_type = ?,
                    raw_artist = ?,
                    raw_title = ?,
                    raw_bpm = ?,
                    raw_key = ?,
                    raw_genre = ?,
                    raw_isrc = ?,
                    identity_confidence = ?,
                    sheet_artist = ?,
                    sheet_title = ?,
                    filename_artist_hint = ?,
                    filename_title_hint = ?,
                    filename_remixer_hint = ?,
                    audio_format = ?,
                    hydration_status = ?,
                    hydration_version = ?,
                    hydration_method = ?,
                    hydration_error = ?,
                    hydration_bytes = ?,
                    hydration_attempts = ?,
                    last_hydrated_utc = ?,
                    next_hydrate_retry_utc = ?,
                    content_stale = ?
                WHERE file_id = ?
                """,
                (
                    merged_name,
                    merged_size,
                    merged_md5,
                    merged_mod,
                    merged_parent,
                    merged_artist,
                    merged_title,
                    merged_dur,
                    merged_isrc,
                    merged_bpm,
                    merged_key,
                    merged_genre,
                    merged_origin,
                    in_ver or ex_ver,
                    now,
                    track.mime_type or existing["mime_type"] or "",
                    meta.raw_artist or existing["raw_artist"] or merged_artist,
                    meta.raw_title or existing["raw_title"] or merged_title,
                    meta.raw_bpm or existing["raw_bpm"] or merged_bpm,
                    meta.raw_key or existing["raw_key"] or merged_key,
                    meta.raw_genre or existing["raw_genre"] or merged_genre,
                    meta.raw_isrc or existing["raw_isrc"] or merged_isrc,
                    meta.identity_confidence or existing["identity_confidence"] or "",
                    meta.sheet_artist or existing["sheet_artist"] or "",
                    meta.sheet_title or existing["sheet_title"] or "",
                    meta.filename_artist_hint or existing["filename_artist_hint"] or "",
                    meta.filename_title_hint or existing["filename_title_hint"] or "",
                    meta.filename_remixer_hint or existing["filename_remixer_hint"] or "",
                    meta.audio_format or existing["audio_format"] or "",
                    track.hydration_status if track.hydration_status != "pending" or version_changed else existing["hydration_status"],
                    track.hydration_version or existing["hydration_version"] or "",
                    track.hydration_method or existing["hydration_method"] or "",
                    track.hydration_error,
                    track.hydration_bytes if track.hydration_bytes > 0 else int(existing["hydration_bytes"] or 0),
                    track.hydration_attempts if track.hydration_attempts > 0 else int(existing["hydration_attempts"] or 0),
                    track.last_hydrated_utc or existing["last_hydrated_utc"] or "",
                    track.next_hydrate_retry_utc,
                    int(track.content_stale if not version_changed else True),
                    fid,
                ),
            )

    # --- Scan Generation & Live Inventory Reconciliation ---

    def begin_scan_run(self, source_folder_id: str) -> int:
        """Start a tracked inventory scan pass. Membership is finalized only on completion."""
        now = _utc_iso()
        with self._get_connection() as conn:
            cur = conn.execute(
                "INSERT INTO scan_runs (source_folder_id, started_utc, status, seen_count) VALUES (?, ?, 'in_progress', 0)",
                (source_folder_id, now),
            )
            return int(cur.lastrowid)

    def reconcile_scanned_file(
        self, scan_id: int, source_folder_id: str, item: Dict[str, Any]
    ) -> str:
        """
        Reconcile a Drive file item by exact file_id without touching tags on unchanged files,
        repairing incomplete unchanged records, and archiving stale evidence on version change.
        """
        fid = str(item.get("id") or "").strip()
        now = _utc_iso()
        if not fid:
            with self._get_connection() as conn:
                conn.execute(
                    "INSERT INTO audit_log (event_type, file_id, details_json, created_utc) VALUES (?, ?, ?, ?)",
                    ("SCAN_BLANK_FILE_ID", None, json.dumps({"name": item.get("name", "")}), now),
                )
            return "skipped_blank_id"

        name = str(item.get("name") or "")
        size = int(item.get("size") or 0)
        md5 = str(item.get("md5Checksum") or "")
        mod = str(item.get("modifiedTime") or "")
        mime = str(item.get("mimeType") or "")
        parents = item.get("parents") or []
        parent = str(item.get("scanned_parent") or (parents[0] if parents else "") or "")
        new_ver = md5 or mod

        with self._get_connection() as conn:
            existing = conn.execute("SELECT * FROM tracks WHERE file_id = ?", (fid,)).fetchone()

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO tracks (
                        file_id, name, size, md5, modified_time, parent_id,
                        version, last_scanned_utc, mime_type,
                        record_origin, is_live_source, quarantined, quarantine_reason,
                        configured_source_id, observed_parent_id, scan_generation,
                        content_stale, identity_origin, identity_confidence,
                        hydration_status, research_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'live_scan', 1, 0, '', ?, ?, ?, 0, 'unresolved', 'unresolved', 'pending', 'pending')
                    """,
                    (fid, name, size, md5, mod, parent, new_ver, now, mime, source_folder_id, parent, scan_id),
                )
                return "inserted"

            if int(existing["scan_generation"] or 0) == scan_id:
                conn.execute(
                    "INSERT INTO audit_log (event_type, file_id, details_json, created_utc) VALUES (?, ?, ?, ?)",
                    ("SCAN_DUPLICATE_FILE_ID", fid, json.dumps({"parent": parent}), now),
                )
                return "duplicate_in_scan"

            old_origin = existing["record_origin"] or "unknown"
            new_origin = (
                "live_and_checkpoint"
                if old_origin in ("checkpoint_import", "live_and_checkpoint")
                else "live_scan"
            )
            old_ver = existing["version"] or ""
            version_changed = bool(old_ver and new_ver and old_ver != new_ver)

            if version_changed:
                self._archive_stale_version_conn(conn, existing, reason="scan_content_version_changed")
                conn.execute(
                    """
                    UPDATE tracks SET
                        name = ?, size = ?, md5 = ?, modified_time = ?, parent_id = ?,
                        mime_type = ?, version = ?, last_scanned_utc = ?,
                        record_origin = ?, is_live_source = 1, quarantined = 0, quarantine_reason = '',
                        configured_source_id = ?, observed_parent_id = ?, scan_generation = ?,
                        content_stale = 1,
                        artist = '', title = '', duration = 0.0, isrc = '',
                        embedded_bpm = '', embedded_key = '', embedded_genre = '',
                        raw_artist = '', raw_title = '', raw_bpm = '', raw_key = '', raw_genre = '', raw_isrc = '',
                        identity_origin = 'unresolved', identity_confidence = 'unresolved',
                        hydration_status = 'pending', hydration_version = '', hydration_method = '', hydration_error = '',
                        research_status = 'pending', research_version = '', research_error = ''
                    WHERE file_id = ?
                    """,
                    (name, size, md5, mod, parent, mime, new_ver, now, new_origin, source_folder_id, parent, scan_id, fid),
                )
                return "version_changed_stale"

            # Same version: refresh inventory attributes independently of tags and repair incomplete rows
            target_ver = new_ver or old_ver
            has_complete_embedded = bool(
                str(existing["artist"] or "").strip()
                and str(existing["title"] or "").strip()
                and float(existing["duration"] or 0.0) > 0
                and (existing["identity_origin"] or "") == "embedded"
            )
            curr_hyd = existing["hydration_status"] or "pending"
            curr_hyd_ver = existing["hydration_version"] or ""
            curr_method = existing["hydration_method"] or ""
            already_stream_inspected = (
                (not target_ver or curr_hyd_ver == target_ver)
                and curr_method.startswith(("embedded_ranged", "embedded_full", "full_download", "preserved_same_version"))
            )
            if has_complete_embedded:
                next_hyd = "success"
                id_conf = existing["identity_confidence"] or "verified_embedded"
            elif curr_hyd == "permanent_unsupported" and (not target_ver or curr_hyd_ver == target_ver):
                next_hyd = "permanent_unsupported"
                id_conf = existing["identity_confidence"] or "unresolved"
            elif curr_hyd in ("partial", "unresolved") and already_stream_inspected:
                # Stream was already inspected at this exact version (e.g. tagless or filename-only WAV) -> do not reset!
                next_hyd = curr_hyd
                id_conf = existing["identity_confidence"] or (
                    "filename_ambiguous" if (existing["artist"] and existing["title"]) else "unresolved"
                )
            else:
                # Schedule hydration repair for unchanged uninspected or legacy fallback-only rows
                next_hyd = "pending" if curr_hyd in ("pending", "partial", "unresolved", "") else curr_hyd
                id_conf = existing["identity_confidence"] or (
                    "filename_parsed" if (existing["artist"] and existing["title"]) else "unresolved"
                )

            conn.execute(
                """
                UPDATE tracks SET
                    name = ?,
                    size = ?,
                    md5 = ?,
                    modified_time = ?,
                    parent_id = ?,
                    mime_type = ?,
                    version = ?,
                    last_scanned_utc = ?,
                    record_origin = ?,
                    is_live_source = 1,
                    quarantined = 0,
                    quarantine_reason = '',
                    configured_source_id = ?,
                    observed_parent_id = ?,
                    scan_generation = ?,
                    hydration_status = ?,
                    hydration_version = CASE WHEN ? = 'success' THEN ? ELSE hydration_version END,
                    identity_confidence = ?
                WHERE file_id = ?
                """,
                (
                    name if name else existing["name"],
                    size if size > 0 else int(existing["size"] or 0),
                    md5 or existing["md5"] or "",
                    mod or existing["modified_time"] or "",
                    parent or existing["parent_id"] or "",
                    mime or existing["mime_type"] or "",
                    new_ver or old_ver,
                    now,
                    new_origin,
                    source_folder_id,
                    parent or existing["observed_parent_id"] or "",
                    scan_id,
                    next_hyd,
                    next_hyd,
                    new_ver or old_ver,
                    id_conf,
                    fid,
                ),
            )
            return (
                "updated_preserved"
                if (has_complete_embedded or (curr_hyd in ("partial", "unresolved") and already_stream_inspected))
                else "updated_needs_hydration"
            )

    def complete_scan_run(
        self, scan_id: int, source_folder_id: str, seen_count: int
    ) -> Dict[str, int]:
        """
        Finalize scan generation transactionally AFTER full inventory walk succeeds.
        Marks previously live records not seen in scan_id as non-live without deleting them,
        and keeps checkpoint-only records quarantined from live routing.
        """
        now = _utc_iso()
        conn = self._get_connection(isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE scan_runs SET status = 'completed', completed_utc = ?, seen_count = ? WHERE scan_id = ?",
                (now, seen_count, scan_id),
            )

            # Unseen tracks that were previously marked live become non-live (e.g. moved out of source)
            cur_absent = conn.execute(
                """
                UPDATE tracks
                SET is_live_source = 0
                WHERE is_live_source = 1 AND scan_generation != ?
                """,
                (scan_id,),
            )
            newly_absent = cur_absent.rowcount

            # Ensure checkpoint-only records not seen in live source remain quarantined
            conn.execute(
                """
                UPDATE tracks
                SET is_live_source = 0,
                    quarantined = 1,
                    quarantine_reason = CASE
                        WHEN quarantine_reason = '' THEN 'checkpoint_only_not_in_source'
                        ELSE quarantine_reason
                    END
                WHERE record_origin = 'checkpoint_import' AND scan_generation != ?
                """,
                (scan_id,),
            )

            live_cnt = conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE is_live_source = 1 AND quarantined = 0"
            ).fetchone()[0]
            quar_cnt = conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE quarantined = 1"
            ).fetchone()[0]

            conn.execute("COMMIT")
            return {
                "live_source_tracks": int(live_cnt),
                "newly_absent_tracks": int(newly_absent),
                "quarantined_tracks": int(quar_cnt),
            }
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def abort_scan_run(self, scan_id: int, seen_count: int, reason: str, interrupted: bool = False) -> None:
        """Record interrupted/failed scan WITHOUT marking unseen tracks absent."""
        status = "interrupted" if interrupted else "failed"
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE scan_runs SET status = ?, completed_utc = ?, seen_count = ?, error_text = ? WHERE scan_id = ?",
                (status, _utc_iso(), seen_count, reason[:500], scan_id),
            )

    # --- Hydration & Research State Updates ---

    def update_hydration_result(
        self,
        file_id: str,
        status: str,
        version: str,
        method: str,
        bytes_read: int = 0,
        error_msg: str = "",
        retry_delay_seconds: int = 0,
        metadata: Optional[TrackMetadata] = None,
    ) -> None:
        """Transactionally persist hydration status, provenance, byte usage, and extracted metadata."""
        now_dt = _utc_now()
        now = _utc_iso(now_dt)
        next_retry = _utc_iso(now_dt + timedelta(seconds=retry_delay_seconds)) if retry_delay_seconds > 0 else ""

        conn = self._get_connection(isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM tracks WHERE file_id = ?", (file_id,)).fetchone()
            if existing is None:
                conn.execute("ROLLBACK")
                return

            attempts = int(existing["hydration_attempts"] or 0) + 1
            total_bytes = int(existing["hydration_bytes"] or 0) + max(0, int(bytes_read))

            if metadata is not None:
                # Never overwrite good non-blank values with blank values on same version
                def _merge(col: str, new_v: str, incoming_src: str) -> str:
                    old_v = str(existing[col] or "")
                    new_s = str(new_v or "")
                    if not new_s:
                        return old_v
                    if not old_v:
                        return new_s
                    if old_v != new_s:
                        old_src = existing["identity_origin"] or "existing"
                        self._record_conflict_conn(conn, file_id, col, old_v, new_s, old_src, incoming_src, version)
                        # Embedded tags outrank prior filename hints
                        if incoming_src == "embedded" and old_src != "embedded":
                            return new_s
                        return old_v
                    return old_v

                in_src = metadata.identity_origin or method
                m_artist = _merge("artist", metadata.artist, in_src)
                m_title = _merge("title", metadata.title, in_src)
                m_dur = metadata.duration if metadata.duration > 0 else float(existing["duration"] or 0.0)
                m_isrc = _merge("isrc", metadata.isrc, in_src)
                m_bpm = _merge("embedded_bpm", metadata.embedded_bpm, in_src)
                m_key = _merge("embedded_key", metadata.embedded_key, in_src)
                m_genre = _merge("embedded_genre", metadata.embedded_genre, in_src)
                m_origin = (
                    "embedded"
                    if (in_src == "embedded" or existing["identity_origin"] == "embedded") and (m_artist or m_title)
                    else (metadata.identity_origin or existing["identity_origin"] or "unresolved")
                )
                m_conf = metadata.identity_confidence or existing["identity_confidence"] or "unresolved"

                conn.execute(
                    """
                    UPDATE tracks SET
                        artist = ?, title = ?, duration = ?, isrc = ?,
                        embedded_bpm = ?, embedded_key = ?, embedded_genre = ?,
                        identity_origin = ?, identity_confidence = ?,
                        raw_artist = ?, raw_title = ?, raw_bpm = ?, raw_key = ?, raw_genre = ?, raw_isrc = ?,
                        sheet_artist = ?, sheet_title = ?,
                        filename_artist_hint = ?, filename_title_hint = ?, filename_remixer_hint = ?,
                        audio_format = ?,
                        content_stale = CASE WHEN ? IN ('success', 'partial') THEN 0 ELSE content_stale END,
                        hydration_status = ?, hydration_version = ?, hydration_method = ?,
                        hydration_error = ?, hydration_bytes = ?, hydration_attempts = ?,
                        last_hydrated_utc = ?, next_hydrate_retry_utc = ?
                    WHERE file_id = ?
                    """,
                    (
                        m_artist, m_title, m_dur, m_isrc,
                        m_bpm, m_key, m_genre,
                        m_origin, m_conf,
                        metadata.raw_artist or existing["raw_artist"] or "",
                        metadata.raw_title or existing["raw_title"] or "",
                        metadata.raw_bpm or existing["raw_bpm"] or "",
                        metadata.raw_key or existing["raw_key"] or "",
                        metadata.raw_genre or existing["raw_genre"] or "",
                        metadata.raw_isrc or existing["raw_isrc"] or "",
                        metadata.sheet_artist or existing["sheet_artist"] or "",
                        metadata.sheet_title or existing["sheet_title"] or "",
                        metadata.filename_artist_hint or existing["filename_artist_hint"] or "",
                        metadata.filename_title_hint or existing["filename_title_hint"] or "",
                        metadata.filename_remixer_hint or existing["filename_remixer_hint"] or "",
                        metadata.audio_format or existing["audio_format"] or "",
                        status,
                        status, version, method,
                        error_msg[:500], total_bytes, attempts,
                        now, next_retry,
                        file_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE tracks SET
                        hydration_status = ?, hydration_version = ?, hydration_method = ?,
                        hydration_error = ?, hydration_bytes = ?, hydration_attempts = ?,
                        last_hydrated_utc = ?, next_hydrate_retry_utc = ?
                    WHERE file_id = ?
                    """,
                    (status, version, method, error_msg[:500], total_bytes, attempts, now, next_retry, file_id),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def update_research_result(
        self,
        file_id: str,
        status: str,
        version: str,
        candidates: Optional[List[Candidate]],
        provider_calls: Dict[str, Any],
        error_msg: str = "",
        retry_delay_seconds: int = 0,
        preserve_existing_candidates_on_failure: bool = True,
    ) -> None:
        """
        Transactionally persist per-track research status, provider call audit, and candidates.
        Preserves prior valid candidates on transient/retryable failures.
        """
        now_dt = _utc_now()
        now = _utc_iso(now_dt)
        next_retry = _utc_iso(now_dt + timedelta(seconds=retry_delay_seconds)) if retry_delay_seconds > 0 else ""

        conn = self._get_connection(isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT research_attempts FROM tracks WHERE file_id = ?", (file_id,)).fetchone()
            if existing is None:
                conn.execute("ROLLBACK")
                return

            attempts = int(existing["research_attempts"] or 0) + 1
            should_replace_candidates = candidates is not None and not (
                preserve_existing_candidates_on_failure
                and status == "retryable_failure"
                and len(candidates) == 0
            )

            if should_replace_candidates and candidates is not None:
                conn.execute(
                    "DELETE FROM candidates WHERE file_id = ? AND COALESCE(is_stale, 0) = 0",
                    (file_id,),
                )
                for c in candidates:
                    conn.execute(
                        """
                        INSERT INTO candidates (
                            file_id, provider, title, artists_json, duration,
                            isrc, bpm, key, genre_json, labels_json, url, verification,
                            created_utc, content_version, is_stale, match_confidence, source_family
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                        """,
                        (
                            file_id, c.provider, c.title, json.dumps(c.artists),
                            c.duration, c.isrc, c.bpm, c.key,
                            json.dumps(c.genre), json.dumps(c.labels),
                            c.url, c.verification,
                            now, c.content_version or version, c.match_confidence, c.source_family,
                        ),
                    )

            conn.execute(
                """
                UPDATE tracks SET
                    research_status = ?,
                    research_version = ?,
                    research_attempts = ?,
                    research_last_utc = ?,
                    research_next_retry_utc = ?,
                    research_error = ?,
                    provider_calls_json = ?
                WHERE file_id = ?
                """,
                (
                    status,
                    version,
                    attempts,
                    now,
                    next_retry,
                    error_msg[:500],
                    json.dumps(provider_calls, sort_keys=True),
                    file_id,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # --- Queries ---

    def get_track(self, file_id: str) -> Optional[Track]:
        """Fetch a track by its exact Google Drive file ID."""
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM tracks WHERE file_id = ?", (file_id,)).fetchone()
            return self._row_to_track(row) if row else None

    def get_all_tracks(self, live_only: bool = False) -> List[Track]:
        """Fetch stored tracks (optionally restricted to live non-quarantined source tracks)."""
        with self._get_connection() as conn:
            if live_only:
                rows = conn.execute(
                    "SELECT * FROM tracks WHERE is_live_source = 1 AND quarantined = 0 ORDER BY name ASC, file_id ASC"
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM tracks ORDER BY name ASC, file_id ASC").fetchall()
            return [self._row_to_track(r) for r in rows]

    def get_live_tracks_for_hydration(
        self,
        limit: int,
        include_retryable: bool = True,
        max_attempts: int = 4,
        force_rehydrate: bool = False,
        use_sheet_uninspected: bool = False,
    ) -> List[Track]:
        """
        Select live source tracks needing metadata hydration or retry.
        Excludes tagless/partial tracks whose audio stream was already inspected at the current
        file version unless force_rehydrate=True or sheet identity lookup is newly requested.
        """
        now = _utc_iso()
        with self._get_connection() as conn:
            if force_rehydrate:
                rows = conn.execute(
                    """
                    SELECT * FROM tracks
                    WHERE is_live_source = 1
                      AND quarantined = 0
                      AND hydration_status != 'permanent_unsupported'
                    ORDER BY content_stale DESC, hydration_attempts ASC, file_id ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                return [self._row_to_track(r) for r in rows]

            retry_clause = (
                """
                OR (
                    hydration_status = 'retryable_failure'
                    AND hydration_attempts < ?
                    AND (next_hydrate_retry_utc = '' OR next_hydrate_retry_utc <= ?)
                )
                """
                if include_retryable
                else ""
            )
            params: List[Any] = []
            if include_retryable:
                params.extend([max_attempts, now])
            params.extend([1 if use_sheet_uninspected else 0, limit])

            rows = conn.execute(
                f"""
                SELECT * FROM tracks
                WHERE is_live_source = 1
                  AND quarantined = 0
                  AND hydration_status != 'permanent_unsupported'
                  AND (
                      content_stale = 1
                      OR hydration_status = 'pending'
                      {retry_clause}
                      OR (
                          hydration_status IN ('partial', 'unresolved')
                          AND (
                              (version != '' AND hydration_version != version)
                              OR NOT (
                                  hydration_method LIKE 'embedded_ranged%'
                                  OR hydration_method LIKE 'embedded_full%'
                                  OR hydration_method LIKE 'full_download%'
                                  OR hydration_method = 'preserved_same_version'
                              )
                              OR (? = 1 AND TRIM(COALESCE(sheet_artist, '')) = '' AND TRIM(COALESCE(artist, '')) = '')
                          )
                      )
                  )
                ORDER BY content_stale DESC, hydration_attempts ASC, file_id ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            return [self._row_to_track(r) for r in rows]

    def get_live_tracks_for_research(
        self,
        limit: int,
        force_refresh: bool = False,
        include_title_only: bool = False,
        max_attempts: int = 3,
    ) -> List[Track]:
        """
        Select next batch of live, non-stale tracks with searchable identity for online research.
        Advances cleanly across batches rather than repeating alphabetical top tracks.
        """
        now = _utc_iso()
        with self._get_connection() as conn:
            if include_title_only:
                identity_clause = """
                (
                    (TRIM(COALESCE(artist, '')) != '' AND TRIM(COALESCE(title, '')) != '')
                    OR (TRIM(COALESCE(filename_title_hint, '')) != '' OR TRIM(COALESCE(title, '')) != '')
                )
                """
            else:
                identity_clause = "(TRIM(COALESCE(artist, '')) != '' AND TRIM(COALESCE(title, '')) != '')"

            if force_refresh:
                status_clause = "research_status != 'permanent_unsupported'"
                params: Tuple[Any, ...] = (limit,)
            else:
                status_clause = """
                (
                    research_status = 'pending'
                    OR (research_version != '' AND version != '' AND research_version != version)
                    OR (
                        research_status = 'retryable_failure'
                        AND research_attempts < ?
                        AND (research_next_retry_utc = '' OR research_next_retry_utc <= ?)
                    )
                )
                """
                params = (max_attempts, now, limit)

            rows = conn.execute(
                f"""
                SELECT * FROM tracks
                WHERE is_live_source = 1
                  AND quarantined = 0
                  AND content_stale = 0
                  AND {identity_clause}
                  AND {status_clause}
                ORDER BY research_attempts ASC, file_id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [self._row_to_track(r) for r in rows]

    def import_json_checkpoint(self, json_path: Path) -> int:
        """
        Safely import tracks from a legacy JSON checkpoint file WITHOUT overwriting live inventory records.
        Checkpoint-only IDs are quarantined from live routing until confirmed by a live Drive scan.
        """
        if not json_path.exists():
            return 0
        with json_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return 0

        now = _utc_iso()
        count = 0
        with self._get_connection() as conn:
            for raw_id, item in data.items():
                if not isinstance(item, dict):
                    continue
                fid = str(raw_id or "").strip()
                if not fid:
                    conn.execute(
                        "INSERT INTO audit_log (event_type, file_id, details_json, created_utc) VALUES (?, ?, ?, ?)",
                        ("CHECKPOINT_BLANK_ID_SKIPPED", None, json.dumps({"raw_id": str(raw_id)}), now),
                    )
                    continue

                cp_ver = str(item.get("version") or "")
                cp_artist = str(item.get("artist") or "").strip()
                cp_title = str(item.get("title") or "").strip()
                cp_dur = float(item.get("duration") or 0.0)
                cp_isrc = str(item.get("isrc") or "").strip()
                cp_bpm = str(item.get("embedded_bpm") or "").strip()
                cp_key = str(item.get("embedded_key") or "").strip()
                cp_genre = str(item.get("embedded_genre") or "").strip()
                cp_origin = str(item.get("identity_origin") or "embedded")

                existing = conn.execute("SELECT * FROM tracks WHERE file_id = ?", (fid,)).fetchone()
                if existing is not None:
                    # NEVER overwrite live Drive name/parent/size/membership or non-blank fields!
                    ex_ver = existing["version"] or ""
                    same_ver = (not ex_ver) or (not cp_ver) or (ex_ver == cp_ver)
                    if not same_ver:
                        self._record_conflict_conn(
                            conn, fid, "version", ex_ver, cp_ver,
                            existing["record_origin"] or "live", "checkpoint_import", ex_ver
                        )
                        continue

                    updates: Dict[str, Any] = {}
                    for col, val in (
                        ("artist", cp_artist),
                        ("title", cp_title),
                        ("isrc", cp_isrc),
                        ("embedded_bpm", cp_bpm),
                        ("embedded_key", cp_key),
                        ("embedded_genre", cp_genre),
                    ):
                        old_v = str(existing[col] or "").strip()
                        if val and not old_v:
                            updates[col] = val
                        elif val and old_v and val != old_v:
                            self._record_conflict_conn(
                                conn, fid, col, old_v, val,
                                existing["identity_origin"] or "live", "checkpoint_import", ex_ver or cp_ver
                            )

                    if cp_dur > 0 and float(existing["duration"] or 0.0) <= 0:
                        updates["duration"] = cp_dur

                    if updates:
                        new_a = updates.get("artist", existing["artist"] or "")
                        new_t = updates.get("title", existing["title"] or "")
                        if new_a and new_t and cp_origin == "embedded":
                            updates["identity_origin"] = "embedded"
                            updates["identity_confidence"] = "verified_embedded"
                            updates["hydration_status"] = "success"
                            updates["hydration_version"] = ex_ver or cp_ver
                            updates["hydration_method"] = "checkpoint_same_version"
                        if existing["record_origin"] == "live_scan":
                            updates["record_origin"] = "live_and_checkpoint"

                        set_sql = ", ".join(f"{k} = ?" for k in updates.keys())
                        conn.execute(
                            f"UPDATE tracks SET {set_sql} WHERE file_id = ?",
                            (*updates.values(), fid),
                        )
                    count += 1
                    continue

                synth_name = f"{cp_artist} - {cp_title}" if (cp_artist or cp_title) else f"[checkpoint:{fid[:8]}]"
                has_id = bool(cp_artist and cp_title)
                hyd_st = "success" if (has_id and cp_origin == "embedded") else ("partial" if has_id else "pending")
                id_conf = "verified_embedded" if (has_id and cp_origin == "embedded") else ("filename_parsed" if has_id else "unresolved")

                conn.execute(
                    """
                    INSERT INTO tracks (
                        file_id, name, size, md5, modified_time, parent_id,
                        artist, title, duration, isrc, embedded_bpm, embedded_key,
                        embedded_genre, identity_origin, version, last_scanned_utc,
                        record_origin, is_live_source, quarantined, quarantine_reason,
                        raw_artist, raw_title, raw_bpm, raw_key, raw_genre, raw_isrc,
                        identity_confidence, hydration_status, hydration_version, hydration_method
                    ) VALUES (
                        ?, ?, 0, ?, '', '',
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        'checkpoint_import', 0, 1, 'checkpoint_import_unconfirmed_live',
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, 'checkpoint_import'
                    )
                    """,
                    (
                        fid, synth_name, cp_ver,
                        cp_artist, cp_title, cp_dur, cp_isrc, cp_bpm, cp_key,
                        cp_genre, cp_origin, cp_ver, now,
                        cp_artist, cp_title, cp_bpm, cp_key, cp_genre, cp_isrc,
                        id_conf, hyd_st, cp_ver if has_id else "",
                    ),
                )
                count += 1
        return count

    # --- Candidate Management ---

    def save_candidates(self, file_id: str, candidates: List[Candidate], content_version: str = "") -> None:
        """Replace active non-stale candidates for a track while preserving archived stale rows."""
        now = _utc_iso()
        with self._get_connection() as conn:
            ver = content_version
            if not ver:
                tr = conn.execute("SELECT version FROM tracks WHERE file_id = ?", (file_id,)).fetchone()
                ver = (tr["version"] or "") if tr else ""
            conn.execute("DELETE FROM candidates WHERE file_id = ? AND COALESCE(is_stale, 0) = 0", (file_id,))
            for c in candidates:
                conn.execute(
                    """
                    INSERT INTO candidates (
                        file_id, provider, title, artists_json, duration,
                        isrc, bpm, key, genre_json, labels_json, url, verification,
                        created_utc, content_version, is_stale, match_confidence, source_family
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        file_id, c.provider, c.title, json.dumps(c.artists),
                        c.duration, c.isrc, c.bpm, c.key,
                        json.dumps(c.genre), json.dumps(c.labels),
                        c.url, c.verification,
                        now, c.content_version or ver, c.match_confidence, c.source_family,
                    ),
                )

    def get_candidates(self, file_id: str, include_stale: bool = False) -> List[Candidate]:
        """Fetch stored active candidates for a track (excluding stale candidates by default)."""
        with self._get_connection() as conn:
            if include_stale:
                cur = conn.execute("SELECT * FROM candidates WHERE file_id = ? ORDER BY id ASC", (file_id,))
            else:
                cur = conn.execute(
                    "SELECT * FROM candidates WHERE file_id = ? AND COALESCE(is_stale, 0) = 0 ORDER BY id ASC",
                    (file_id,),
                )
            results = []
            for row in cur.fetchall():
                keys = set(row.keys())
                results.append(
                    Candidate(
                        provider=row["provider"],
                        title=row["title"],
                        artists=json.loads(row["artists_json"] or "[]"),
                        duration=row["duration"],
                        isrc=row["isrc"],
                        bpm=row["bpm"],
                        key=row["key"],
                        genre=json.loads(row["genre_json"] or "[]"),
                        labels=json.loads(row["labels_json"] or "[]"),
                        url=row["url"],
                        verification=row["verification"],
                        content_version=row["content_version"] if "content_version" in keys else "",
                        is_stale=bool(row["is_stale"]) if "is_stale" in keys else False,
                        match_confidence=row["match_confidence"] if "match_confidence" in keys else "",
                        source_family=row["source_family"] if "source_family" in keys else "",
                    )
                )
            return results

    # --- Provider Cache (with strict UTC TTL enforcement) ---

    def get_cache(self, provider: str, cache_key: str) -> Optional[Any]:
        """Retrieve cached provider response only if within UTC TTL window."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT response_json, created_utc, ttl_seconds FROM provider_cache WHERE provider = ? AND cache_key = ?",
                (provider, cache_key),
            ).fetchone()
            if not row:
                return None
            ttl = int(row["ttl_seconds"] or 0)
            if ttl <= 0:
                return None
            try:
                created_raw = str(row["created_utc"]).replace("Z", "+00:00")
                created_dt = datetime.fromisoformat(created_raw)
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                if (_utc_now() - created_dt).total_seconds() > ttl:
                    return None
            except ValueError:
                return None
            return json.loads(row["response_json"])

    def set_cache(self, provider: str, cache_key: str, data: Any, ttl_seconds: int = 86400) -> None:
        """Cache provider response with UTC timestamp and explicit TTL."""
        if ttl_seconds <= 0:
            return
        now = _utc_iso()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO provider_cache (provider, cache_key, response_json, created_utc, ttl_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                (provider, cache_key, json.dumps(data), now, int(ttl_seconds)),
            )

    # --- Local Read-Only Diagnostic Summary (Zero Network Calls) ---

    def get_inventory_summary(self) -> Dict[str, Any]:
        """Compute local database diagnostic counts with zero network calls."""
        with self._get_connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            live = conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE is_live_source = 1 AND quarantined = 0"
            ).fetchone()[0]
            quarantined = conn.execute("SELECT COUNT(*) FROM tracks WHERE quarantined = 1").fetchone()[0]
            checkpoint_only = conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE record_origin = 'checkpoint_import' AND quarantined = 1"
            ).fetchone()[0]
            stale = conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE is_live_source = 1 AND quarantined = 0 AND content_stale = 1"
            ).fetchone()[0]

            live_complete = conn.execute(
                """
                SELECT COUNT(*) FROM tracks
                WHERE is_live_source = 1 AND quarantined = 0
                  AND TRIM(COALESCE(artist, '')) != '' AND TRIM(COALESCE(title, '')) != ''
                """
            ).fetchone()[0]
            live_incomplete = live - live_complete

            hyd_counts = {
                r["hydration_status"]: int(r["cnt"])
                for r in conn.execute(
                    """
                    SELECT hydration_status, COUNT(*) AS cnt FROM tracks
                    WHERE is_live_source = 1 AND quarantined = 0
                    GROUP BY hydration_status
                    """
                ).fetchall()
            }
            res_counts = {
                r["research_status"]: int(r["cnt"])
                for r in conn.execute(
                    """
                    SELECT research_status, COUNT(*) AS cnt FROM tracks
                    WHERE is_live_source = 1 AND quarantined = 0
                    GROUP BY research_status
                    """
                ).fetchall()
            }
            conf_counts = {
                (r["identity_confidence"] or "unresolved"): int(r["cnt"])
                for r in conn.execute(
                    """
                    SELECT identity_confidence, COUNT(*) AS cnt FROM tracks
                    WHERE is_live_source = 1 AND quarantined = 0
                    GROUP BY identity_confidence
                    """
                ).fetchall()
            }
            total_bytes = conn.execute(
                "SELECT COALESCE(SUM(hydration_bytes), 0) FROM tracks"
            ).fetchone()[0]
            eligible_hydration = conn.execute(
                """
                SELECT COUNT(*) FROM tracks
                WHERE is_live_source = 1 AND quarantined = 0
                  AND hydration_status != 'permanent_unsupported'
                  AND (
                      content_stale = 1
                      OR hydration_status = 'pending'
                      OR (hydration_status = 'retryable_failure' AND hydration_attempts < 4)
                      OR (
                          hydration_status IN ('partial', 'unresolved')
                          AND (
                              (version != '' AND hydration_version != version)
                              OR NOT (
                                  hydration_method LIKE 'embedded_ranged%'
                                  OR hydration_method LIKE 'embedded_full%'
                                  OR hydration_method LIKE 'full_download%'
                                  OR hydration_method = 'preserved_same_version'
                              )
                          )
                      )
                  )
                """
            ).fetchone()[0]
            eligible_research = conn.execute(
                """
                SELECT COUNT(*) FROM tracks
                WHERE is_live_source = 1 AND quarantined = 0 AND content_stale = 0
                  AND TRIM(COALESCE(artist, '')) != '' AND TRIM(COALESCE(title, '')) != ''
                  AND (
                      research_status = 'pending'
                      OR (research_status = 'retryable_failure' AND research_attempts < 3)
                  )
                """
            ).fetchone()[0]
            conflicts_cnt = conn.execute(
                "SELECT COUNT(*) FROM metadata_conflicts WHERE resolved = 0"
            ).fetchone()[0]
            stale_cands_cnt = conn.execute(
                "SELECT COUNT(*) FROM candidates WHERE COALESCE(is_stale, 0) = 1"
            ).fetchone()[0]
            last_scan_row = conn.execute(
                "SELECT * FROM scan_runs ORDER BY scan_id DESC LIMIT 1"
            ).fetchone()
            mig_row = conn.execute(
                "SELECT * FROM schema_migrations ORDER BY version DESC LIMIT 1"
            ).fetchone()

            return {
                "schema_version": int(mig_row["version"]) if mig_row else 1,
                "last_migration_backup": (mig_row["backup_path"] or "") if mig_row else "",
                "total_records": int(total),
                "live_source_tracks": int(live),
                "quarantined_records": int(quarantined),
                "checkpoint_only_records": int(checkpoint_only),
                "stale_live_tracks": int(stale),
                "live_ready_identity": int(live_complete),
                "live_missing_identity": int(live_incomplete),
                "eligible_for_hydration": int(eligible_hydration),
                "eligible_for_research": int(eligible_research),
                "hydration_status_counts": hyd_counts,
                "research_status_counts": res_counts,
                "identity_confidence_counts": conf_counts,
                "total_hydration_bytes": int(total_bytes or 0),
                "unresolved_conflicts": int(conflicts_cnt),
                "archived_stale_candidates": int(stale_cands_cnt),
                "last_scan": dict(last_scan_row) if last_scan_row else None,
            }

    # --- Rollback Manifests ---

    def save_rollback_manifest(self, manifest_id: str, status: str, data: Dict[str, Any]) -> None:
        now = _utc_iso()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO rollback_manifests (manifest_id, status, created_utc, manifest_json)
                VALUES (?, ?, ?, ?)
                """,
                (manifest_id, status, now, json.dumps(data)),
            )

    def get_rollback_manifest(self, manifest_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT manifest_json FROM rollback_manifests WHERE manifest_id = ?", (manifest_id,)
            ).fetchone()
            return json.loads(row["manifest_json"]) if row else None

    def list_rollback_manifests(self) -> List[Tuple[str, str, str]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT manifest_id, status, created_utc FROM rollback_manifests ORDER BY created_utc DESC"
            ).fetchall()
            return [(r["manifest_id"], r["status"], r["created_utc"]) for r in rows]
