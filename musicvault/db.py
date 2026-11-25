"""
Persistent SQLite database and checkpoint management for Music Vault Manager.
"""
import sqlite3
import json
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timezone

from .models import Track, TrackMetadata, Candidate


class Database:
    """Manages persistent SQLite operations, caching, and checkpoints."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_schema()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        """Create tables and indexes if they do not exist."""
        with self._get_connection() as conn:
            conn.executescript("""
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
                last_scanned_utc TEXT DEFAULT ''
            );

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
                FOREIGN KEY (file_id) REFERENCES tracks(file_id)
            );

            CREATE INDEX IF NOT EXISTS idx_candidates_file ON candidates(file_id);

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
            );

            CREATE TABLE IF NOT EXISTS provider_cache (
                provider TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_utc TEXT NOT NULL,
                ttl_seconds INTEGER DEFAULT 86400,
                PRIMARY KEY (provider, cache_key)
            );

            CREATE TABLE IF NOT EXISTS rollback_manifests (
                manifest_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_utc TEXT NOT NULL,
                manifest_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                file_id TEXT,
                details_json TEXT NOT NULL,
                created_utc TEXT NOT NULL
            );
            """)

    # --- Checkpoints & Track Persistence ---

    def save_track(self, track: Track) -> None:
        """Upsert a track record with audio metadata checkpoint."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute("""
            INSERT INTO tracks (
                file_id, name, size, md5, modified_time, parent_id,
                artist, title, duration, isrc, embedded_bpm, embedded_key,
                embedded_genre, identity_origin, version, last_scanned_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
                name=excluded.name,
                size=excluded.size,
                md5=excluded.md5,
                modified_time=excluded.modified_time,
                parent_id=excluded.parent_id,
                artist=excluded.artist,
                title=excluded.title,
                duration=excluded.duration,
                isrc=excluded.isrc,
                embedded_bpm=excluded.embedded_bpm,
                embedded_key=excluded.embedded_key,
                embedded_genre=excluded.embedded_genre,
                identity_origin=excluded.identity_origin,
                version=excluded.version,
                last_scanned_utc=excluded.last_scanned_utc
            """, (
                track.file_id, track.name, track.size, track.md5,
                track.modified_time, track.parent_id,
                track.metadata.artist, track.metadata.title,
                track.metadata.duration, track.metadata.isrc,
                track.metadata.embedded_bpm, track.metadata.embedded_key,
                track.metadata.embedded_genre, track.metadata.identity_origin,
                track.metadata.version, now
            ))

    def get_track(self, file_id: str) -> Optional[Track]:
        """Fetch a track by its Google Drive file ID."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM tracks WHERE file_id = ?", (file_id,))
            row = cur.fetchone()
            if not row:
                return None
            meta = TrackMetadata(
                version=row["version"],
                artist=row["artist"],
                title=row["title"],
                duration=row["duration"],
                isrc=row["isrc"],
                embedded_bpm=row["embedded_bpm"],
                embedded_key=row["embedded_key"],
                embedded_genre=row["embedded_genre"],
                identity_origin=row["identity_origin"]
            )
            return Track(
                file_id=row["file_id"],
                name=row["name"],
                size=row["size"],
                md5=row["md5"],
                modified_time=row["modified_time"],
                parent_id=row["parent_id"],
                metadata=meta
            )

    def get_all_tracks(self) -> List[Track]:
        """Fetch all stored tracks."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM tracks ORDER BY name ASC")
            tracks = []
            for row in cur.fetchall():
                meta = TrackMetadata(
                    version=row["version"],
                    artist=row["artist"],
                    title=row["title"],
                    duration=row["duration"],
                    isrc=row["isrc"],
                    embedded_bpm=row["embedded_bpm"],
                    embedded_key=row["embedded_key"],
                    embedded_genre=row["embedded_genre"],
                    identity_origin=row["identity_origin"]
                )
                tracks.append(Track(
                    file_id=row["file_id"],
                    name=row["name"],
                    size=row["size"],
                    md5=row["md5"],
                    modified_time=row["modified_time"],
                    parent_id=row["parent_id"],
                    metadata=meta
                ))
            return tracks

    def import_json_checkpoint(self, json_path: Path) -> int:
        """Import tracks from an existing JSON checkpoint file."""
        if not json_path.exists():
            return 0
        with json_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        count = 0
        for file_id, item in data.items():
            meta = TrackMetadata(
                version=item.get("version", ""),
                artist=item.get("artist", ""),
                title=item.get("title", ""),
                duration=float(item.get("duration", 0.0) or 0.0),
                isrc=item.get("isrc", ""),
                embedded_bpm=str(item.get("embedded_bpm", "") or ""),
                embedded_key=str(item.get("embedded_key", "") or ""),
                embedded_genre=str(item.get("embedded_genre", "") or ""),
                identity_origin=item.get("identity_origin", "embedded")
            )
            track = Track(
                file_id=file_id,
                name=f"{meta.artist} - {meta.title}",
                md5=meta.version,
                metadata=meta
            )
            self.save_track(track)
            count += 1
        return count

    # --- Candidate Management ---

    def save_candidates(self, file_id: str, candidates: List[Candidate]) -> None:
        """Replace candidates for a track."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute("DELETE FROM candidates WHERE file_id = ?", (file_id,))
            for c in candidates:
                conn.execute("""
                INSERT INTO candidates (
                    file_id, provider, title, artists_json, duration,
                    isrc, bpm, key, genre_json, labels_json, url, verification, created_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    file_id, c.provider, c.title, json.dumps(c.artists),
                    c.duration, c.isrc, c.bpm, c.key,
                    json.dumps(c.genre), json.dumps(c.labels),
                    c.url, c.verification, now
                ))

    def get_candidates(self, file_id: str) -> List[Candidate]:
        """Fetch all stored candidates for a track."""
        with self._get_connection() as conn:
            cur = conn.execute("SELECT * FROM candidates WHERE file_id = ?", (file_id,))
            results = []
            for row in cur.fetchall():
                results.append(Candidate(
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
                    verification=row["verification"]
                ))
            return results

    # --- Provider Cache ---

    def get_cache(self, provider: str, cache_key: str) -> Optional[Any]:
        """Retrieve cached provider response if valid."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT response_json, created_utc, ttl_seconds FROM provider_cache WHERE provider = ? AND cache_key = ?",
                (provider, cache_key)
            )
            row = cur.fetchone()
            if not row:
                return None
            return json.loads(row["response_json"])

    def set_cache(self, provider: str, cache_key: str, data: Any, ttl_seconds: int = 86400) -> None:
        """Cache provider response."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO provider_cache (provider, cache_key, response_json, created_utc, ttl_seconds)
            VALUES (?, ?, ?, ?, ?)
            """, (provider, cache_key, json.dumps(data), now, ttl_seconds))

    # --- Rollback Manifests ---

    def save_rollback_manifest(self, manifest_id: str, status: str, data: Dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO rollback_manifests (manifest_id, status, created_utc, manifest_json)
            VALUES (?, ?, ?, ?)
            """, (manifest_id, status, now, json.dumps(data)))

    def get_rollback_manifest(self, manifest_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT manifest_json FROM rollback_manifests WHERE manifest_id = ?", (manifest_id,))
            row = cur.fetchone()
            if row:
                return json.loads(row["manifest_json"])
            return None

    def list_rollback_manifests(self) -> List[Tuple[str, str, str]]:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT manifest_id, status, created_utc FROM rollback_manifests ORDER BY created_utc DESC")
            return [(r["manifest_id"], r["status"], r["created_utc"]) for r in cur.fetchall()]
