import json

from musicvault.config import Config
from musicvault.core.coverage import attribution, decisions, library_report
from musicvault.core.inventory import scan_root
from musicvault.db import Database
from musicvault.models import Candidate, Track, TrackMetadata
from musicvault.services.sheets import validate_and_index_tracks_sheet
from tests.test_v203_process import arguments


def test_attribution_reuses_stored_agreement():
    track = Track("one", "Artist - Track.flac", metadata=TrackMetadata(artist="Artist", title="Track"))
    candidates = [
        Candidate(provider, "Track", ["Artist"], bpm=120, key="8A", verification="Duration supported")
        for provider in ("MusicBrainz", "SonoVault")
    ]
    baseline, _ = decisions(track, candidates, {})
    assert baseline["bpm"]["state"] == "verified"
    assert baseline["genre"]["state"] == "missing"
    credits = attribution(track, candidates, {}, baseline)
    assert all("bpm" in shared and "bpm" in unique for shared, unique in credits.values())
    manual, _ = decisions(track, candidates, {"bpm": ("124", "USER_LOCKED")})
    assert manual["bpm"]["value"] == "124"
    assert all("bpm" not in unique for _, unique in attribution(
        track, candidates, {"bpm": ("124", "USER_LOCKED")}, manual,
    ).values())


def test_bpm_full_alias_and_duplicate_aliases():
    header = ["Title", "Artist", "Genre", "Subgenre", "Musical Key", "BPM (full)"] + [""] * 8 + ["Drive ID"]
    row = ["One", "A", "House", "", "8A", "123"] + [""] * 8 + ["drive_exact_one"]
    index = validate_and_index_tracks_sheet(header, [row])
    assert index.schema_valid
    assert index.unique_rows_by_id["drive_exact_one"][1][4:6] == ["123", "8A"]
    header[6] = "Tempo"
    assert not validate_and_index_tracks_sheet(header, [row]).schema_valid


def test_whole_library_denominator_and_reports_without_provider_client(tmp_path):
    config = Config(tmp_path)
    db = Database(config.db_path)

    class Drive:
        def scan_inventory(self, root):
            yield {"id": "drive_exact_one", "name": "One.flac", "parents": [root]}

        def get_children_folders(self, root):
            return {}

    class Sheet:
        def fetch_header_row(self, *args):
            return ["Title", "Artist", "Genre", "Subgenre", "BPM (full)", "Key"] + [""] * 8 + ["Drive ID"]

        def iter_tracks_rows(self, *args):
            row = ["One", "Artist"] + [""] * 12 + ["drive_exact_one"]
            yield 2, row
            yield 3, row

    drive = Drive()
    scan_root(db, drive, config.source_folder_id, "source")
    scan_root(db, drive, config.vault_folder_id, "destination")
    export = tmp_path / "preview.json"
    with db._get_connection() as conn:
        report = library_report(conn, db, config, arguments(
            export_json=str(export), export_csv="", verbose=True,
        ), Sheet(), drive)
    assert all(report["tracks"][scope]["denominator"] == 1 for scope in report["tracks"])
    assert report["sheets"]["duplicate_ids"] == 1
    saved = json.loads(export.read_text())
    assert saved["rows"][0]["routing"] == "ambiguous_location"
    assert saved["rows"][0]["move_authorized"] is False
    assert saved["rows"][0]["fields"]["bpm"]["state"] == "missing"
