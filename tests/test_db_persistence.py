"""
Unit tests for SQLite database schema, checkpoints, and cache.
"""
import pytest
from pathlib import Path
import tempfile
from musicvault.db import Database
from musicvault.models import Track, TrackMetadata, Candidate


def test_sqlite_db_roundtrip():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        db = Database(db_path)

        track = Track(
            file_id="FILE123",
            name="Artist - Track.flac",
            size=10240,
            md5="checksum123",
            metadata=TrackMetadata(
                artist="Artist",
                title="Track",
                duration=180.5,
                isrc="US1234567890"
            )
        )

        db.save_track(track)
        fetched = db.get_track("FILE123")
        assert fetched is not None
        assert fetched.name == "Artist - Track.flac"
        assert fetched.metadata.artist == "Artist"
        assert fetched.metadata.duration == 180.5

        # Test caching
        db.set_cache("test_prov", "key1", {"status": "ok"})
        cached = db.get_cache("test_prov", "key1")
        assert cached == {"status": "ok"}
