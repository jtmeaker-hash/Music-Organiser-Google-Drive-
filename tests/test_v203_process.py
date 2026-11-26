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
