from argparse import Namespace

from musicvault.config import Config
from musicvault.core.process import run_process
from musicvault.db import Database


def arguments(**changes):
    return Namespace(**{
        "scope": "all", "limit": 0, "batch_size": 10, "cooldown": 0, "concurrency": 1,
        "resume": False, "inventory_only": False, "dry_run": False, "file_id": [],
        "provider": "", **changes,
    })


def test_resume_reuses_completed_root_and_restarts_failed_root(tmp_path):
    config = Config(tmp_path)
    db = Database(config.db_path)
    called = []

    class Drive:
        def scan_inventory(self, root):
            called.append(root)
            if root == config.vault_folder_id and called.count(root) == 1:
                raise OSError("inaccessible")
            yield {"id": root + "_audio", "name": "Audio.flac", "parents": [root]}

    drive = Drive()
    first = run_process(config, db, drive, arguments(inventory_only=True))
    assert first["status"] == "failed"
    second = run_process(config, db, drive, arguments(resume=True, inventory_only=True))
    assert second["run_id"] == first["run_id"]
    assert second["inventory"] == {"source": 1, "destination": 1, "unique": 2, "overlaps": 0}
    assert called == [config.source_folder_id, config.vault_folder_id, config.vault_folder_id]


def test_scoped_hydration_selector_includes_destination(tmp_path):
    config = Config(tmp_path)
    db = Database(config.db_path)

    class Drive:
        def scan_inventory(self, root):
            yield {"id": "audio", "name": "Audio.flac", "parents": [root]}

    result = run_process(config, db, Drive(), arguments(scope="destination", dry_run=True))
    assert result["eligible_hydration"]
    assert not db.get_track("audio").is_live_source


def test_unified_research_fairness_across_small_resumptions(tmp_path, monkeypatch):
    from musicvault.core.provider_policy import ProviderPolicy
    from musicvault.models import TrackMetadata
    from musicvault.providers.base import ProviderCallOutcome, ProviderStatus

    config = Config(tmp_path)
    db = Database(config.db_path)
    monkeypatch.setenv("REQUIRED_RESEARCH_PROVIDERS", "musicbrainz")
    ids = [f"new_{i:02}" for i in range(7)] + ["retry"]

    class Drive:
        def scan_inventory(self, root):
            for file_id in ids:
                yield {"id": file_id, "name": file_id + ".flac", "parents": [root], "md5Checksum": "version"}

    class Provider:
        def can_request(self):
            return True

        def search_detailed(self, artist, title, isrc=""):
            called.append(title)
            return [], ProviderCallOutcome("MusicBrainz", ProviderStatus.EMPTY, request_made=True)

    called = []
    drive = Drive()
    run_process(config, db, drive, arguments(inventory_only=True, scope="source"))
    for file_id in ids:
        db.update_hydration_result(
            file_id, "success", "version", method="embedded_ranged",
            metadata=TrackMetadata(artist="Artist", title=file_id, version="version", duration=180,
                                   identity_origin="embedded"),
        )
    with db._get_connection() as conn:
        conn.execute("""
            UPDATE tracks SET research_status='retryable_failure', research_attempts=1,
            research_version='version' WHERE file_id='retry'
        """)
    options = arguments(limit=1, scope="source")
    for index in range(5):
        options.resume = index > 0
        result = run_process(config, db, drive, options, policy=ProviderPolicy(config),
                             providers_override={"mb": Provider()})
        assert result["status"] == "paused"
    assert called == ["new_00", "new_01", "new_02", "new_03", "retry"]
