"""
Deterministic, isolated benchmark fixtures and runner for Music Vault Manager.
- Never imports into or mutates the user's live musicvault.db!
- Preserves exact 10-track v1.3 control routing regression (8 eligible, 2 held, 0 authorized).
- Honestly reports unavailable expected sheet rows on the 50-track v0.6 checkpoint rather than
  fabricating fake row 999 records or misleading classification accuracy percentages.
"""
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import Config
from ..core.organizer import build_move_plan
from ..core.rules import build_track_proposal, planned_row
from ..db import Database
from ..models import Track, TrackMetadata


# Verified 10-track v1.3 control fixture (from 02_Latest_Test_Report_v1.3.txt & v07_local_checkpoint.json)
BENCHMARK_10_FIXTURE: List[Dict[str, Any]] = [
    {
        "row_num": 2,
        "file_id": "1heVoqsB5HpgGHCNAU0liJDrhrIFCWytX",
        "filename": "Believe - Jesse Bloch, Coby Watts.wav",
        "checkpoint": {
            "version": "bc546f8c2d4576fa9dd83949cbeb88f5",
            "artist": "Believe",
            "title": "Jesse Bloch, Coby Watts",
            "duration": 173.07,
            "isrc": "",
            "embedded_bpm": "",
            "embedded_key": "",
            "embedded_genre": "",
            "identity_origin": "filename fallback",
        },
        "sheet_row": ["", "", "", "", "147.0", "", "", "", "", "", "", "", "", "", "1heVoqsB5HpgGHCNAU0liJDrhrIFCWytX"],
        "expected": {
            "destination": "",
            "reason": "UNKNOWN_OR_UNSUPPORTED_GENRE",
            "routing_confidence": "HOLD",
            "eligible": False,
            "move_authorized": False,
        },
    },
    {
        "row_num": 2169,
        "file_id": "1ehnUyjzwrwjeEeUv1nLymSk5LXUUDYmW",
        "filename": "Atmozfears & David Spekter - Release (10 Year Anniversary Remix).aiff",
        "checkpoint": {
            "version": "8d69c96dd1ce04d7713744163fe0d32d",
            "artist": "Atmozfears & David Spekter",
            "title": "Release (10 Year Anniversary Remix)",
            "duration": 246.0,
            "isrc": "",
            "embedded_bpm": "",
            "embedded_key": "",
            "embedded_genre": "",
            "identity_origin": "filename fallback",
        },
        "sheet_row": ["", "", "Hardstyle", "Euphoric", "", "", "", "", "", "", "", "", "", "", "1ehnUyjzwrwjeEeUv1nLymSk5LXUUDYmW"],
        "expected": {
            "destination": "Hardstyle",
            "reason": "PROVISIONAL_EXISTING_GENRE",
            "routing_confidence": "PROVISIONAL_REVIEW",
            "eligible": True,
            "move_authorized": False,
        },
    },
    {
        "row_num": 7,
        "file_id": "1v3jdlhhx3hqfY6ivYBut3DcJxKsu6gSL",
        "filename": "Act of Rage - Living Fast.flac",
        "checkpoint": {
            "version": "ec6ec53fa1eb062fd5d840d6d7efce63",
            "artist": "Act of Rage",
            "title": "Living Fast",
            "duration": 240.0,
            "isrc": "",
            "embedded_bpm": "157.9",
            "embedded_key": "11A",
            "embedded_genre": "Dance",
            "identity_origin": "embedded",
        },
        "sheet_row": ["", "", "Hardstyle", "Rawstyle", "158.0", "11A", "", "", "", "", "", "", "", "", "1v3jdlhhx3hqfY6ivYBut3DcJxKsu6gSL"],
        "expected": {
            "destination": "Hardstyle",
            "reason": "PROVISIONAL_EXISTING_GENRE",
            "routing_confidence": "PROVISIONAL_REVIEW",
            "eligible": True,
            "move_authorized": False,
        },
    },
    {
        "row_num": 23,
        "file_id": "1NJxM8N3WyJMS7-KEhDriERqlJYwk8YLf",
        "filename": "Big City Life - Luude, Mattafix.wav",
        "checkpoint": {
            "version": "9d9c0a5a49a2d269cf3c02d700fc8131",
            "artist": "Big City Life",
            "title": "Luude, Mattafix",
            "duration": 149.58,
            "isrc": "",
            "embedded_bpm": "",
            "embedded_key": "",
            "embedded_genre": "",
            "identity_origin": "filename fallback",
        },
        "sheet_row": ["", "", "Drum & Bass", "", "174.0", "5B", "", "", "", "", "", "", "", "", "1NJxM8N3WyJMS7-KEhDriERqlJYwk8YLf"],
        "expected": {
            "destination": "Drum & Bass",
            "reason": "PROVISIONAL_EXISTING_GENRE",
            "routing_confidence": "PROVISIONAL_REVIEW",
            "eligible": True,
            "move_authorized": False,
        },
    },
    {
        "row_num": 159,
        "file_id": "1N74Lus5IHsDXslNA3iB3eC9EbuoDt-zB",
        "filename": "Amen - Dexperia.flac",
        "checkpoint": {
            "version": "8e1e6f071eb4d8c083b4dc1186714bdc",
            "artist": "Dexperia",
            "title": "Amen",
            "duration": 169.45,
            "isrc": "QT6HK2614417",
            "embedded_bpm": "60",
            "embedded_key": "7B",
            "embedded_genre": "Trance",
            "identity_origin": "embedded",
        },
        "sheet_row": ["", "", "Drum & Bass", "", "180.0", "", "", "", "", "", "", "", "", "", "1N74Lus5IHsDXslNA3iB3eC9EbuoDt-zB"],
        "expected": {
            "destination": "Drum & Bass",
            "reason": "PROVISIONAL_EXISTING_GENRE",
            "routing_confidence": "PROVISIONAL_REVIEW",
            "eligible": True,
            "move_authorized": False,
        },
    },
    {
        "row_num": 355,
        "file_id": "1E50C7UI78bE6hwRpIDOuHciWqP-fYBiK",
        "filename": "Another High - Wasted Penguinz.wav",
        "checkpoint": {
            "version": "4728e82204bbd264734337b95c98ac2c",
            "artist": "Another High",
            "title": "Wasted Penguinz",
            "duration": 232.06,
            "isrc": "",
            "embedded_bpm": "",
            "embedded_key": "",
            "embedded_genre": "",
            "identity_origin": "filename fallback",
        },
        "sheet_row": ["", "", "Hardstyle", "Euphoric", "146.0", "5B", "", "", "", "", "", "", "", "", "1E50C7UI78bE6hwRpIDOuHciWqP-fYBiK"],
        "expected": {
            "destination": "Hardstyle",
            "reason": "PROVISIONAL_EXISTING_GENRE",
            "routing_confidence": "PROVISIONAL_REVIEW",
            "eligible": True,
            "move_authorized": False,
        },
    },
    {
        "row_num": 1914,
        "file_id": "1VpA4yDXwkykR7YzN9hWFZZS-TMK3ps18",
        "filename": "The Loudest City (Decibel outdoor 2026 Anthem) - Rebellion.flac",
        "checkpoint": {
            "version": "62aba7992af548e87a5766f9a557ffa4",
            "artist": "Rebelion; Micah Martin",
            "title": "The Loudest City (Decibel outdoor 2026 Anthem)",
            "duration": 292.65,
            "isrc": "NLH2L2600182",
            "embedded_bpm": "154",
            "embedded_key": "",
            "embedded_genre": "",
            "identity_origin": "embedded",
        },
        "sheet_row": ["", "", "Hardstyle", "Rawstyle", "154.0", "2A", "", "", "", "", "", "", "", "", "1VpA4yDXwkykR7YzN9hWFZZS-TMK3ps18"],
        "expected": {
            "destination": "Hardstyle",
            "reason": "PROVISIONAL_EXISTING_GENRE",
            "routing_confidence": "PROVISIONAL_REVIEW",
            "eligible": True,
            "move_authorized": False,
        },
    },
    {
        "row_num": 1040,
        "file_id": "1IikiHEWOKy_Su_9NHSx5EKdKQyoTOrt-",
        "filename": "Young, Wild & Free (feat. Bruno Mars) - Snoop Dogg, Wiz Khalifa, Bruno Mars.wav",
        "checkpoint": {
            "version": "026c7cddb12c55852ca5c105c46cc97c",
            "artist": "Young, Wild & Free (feat. Bruno Mars)",
            "title": "Snoop Dogg, Wiz Khalifa, Bruno Mars",
            "duration": 257.49,
            "isrc": "",
            "embedded_bpm": "",
            "embedded_key": "",
            "embedded_genre": "",
            "identity_origin": "filename fallback",
        },
        "sheet_row": ["", "", "", "", "", "", "", "", "", "", "", "", "", "", "1IikiHEWOKy_Su_9NHSx5EKdKQyoTOrt-"],
        "expected": {
            "destination": "",
            "reason": "UNKNOWN_OR_UNSUPPORTED_GENRE",
            "routing_confidence": "HOLD",
            "eligible": False,
            "move_authorized": False,
        },
    },
    {
        "row_num": 2181,
        "file_id": "1QuI-Y-pNGDxob7Ez_q4mN6aNtDCPOiks",
        "filename": "Hard Driver - You're Not Alone.flac",
        "checkpoint": {
            "version": "ba2e37584ebc89cdce604f1fbc3feafe",
            "artist": "Hard Driver",
            "title": "You're Not Alone",
            "duration": 199.02,
            "isrc": "BEB682600269",
            "embedded_bpm": "",
            "embedded_key": "",
            "embedded_genre": "Dance",
            "identity_origin": "embedded",
        },
        "sheet_row": ["", "", "Hardstyle", "", "", "", "", "", "", "", "", "", "", "", "1QuI-Y-pNGDxob7Ez_q4mN6aNtDCPOiks"],
        "expected": {
            "destination": "Hardstyle",
            "reason": "PROVISIONAL_EXISTING_GENRE",
            "routing_confidence": "PROVISIONAL_REVIEW",
            "eligible": True,
            "move_authorized": False,
        },
    },
    {
        "row_num": 1204,
        "file_id": "1GSmBaNJj9SR80Kw_9XtByPKyUKUg5F1C",
        "filename": "YoU'rE nOt OriGiNaL - Adjuzt.flac",
        "checkpoint": {
            "version": "47368fe801d57107c7b69ccda89d5690",
            "artist": "Adjuzt",
            "title": "You're Not Original",
            "duration": 146.99,
            "isrc": "GBLV62409764",
            "embedded_bpm": "80",
            "embedded_key": "8A",
            "embedded_genre": "Dance",
            "identity_origin": "embedded",
        },
        "sheet_row": ["", "", "Hardstyle", "Xtra Raw", "", "", "", "", "", "", "", "", "", "", "1GSmBaNJj9SR80Kw_9XtByPKyUKUg5F1C"],
        "expected": {
            "destination": "Hardstyle",
            "reason": "PROVISIONAL_EXISTING_GENRE",
            "routing_confidence": "PROVISIONAL_REVIEW",
            "eligible": True,
            "move_authorized": False,
        },
    },
]

REFERENCE_ROWS_BY_ID: Dict[str, Tuple[int, List[str], Dict[str, Any]]] = {
    item["file_id"]: (item["row_num"], item["sheet_row"], item["expected"])
    for item in BENCHMARK_10_FIXTURE
}


@dataclass
class BenchmarkTrackOutcome:
    row_num: Optional[int]
    file_id: str
    track_name: str
    expected_available: bool
    passed: Optional[bool]
    expected_destination: str
    actual_destination: str
    expected_reason: str
    actual_reason: str
    eligible: bool
    move_authorized: bool
    failure_detail: str = ""


@dataclass
class BenchmarkSuiteResult:
    sample_size: int
    status: str  # 'PASS', 'FAIL', 'INCOMPLETE_BENCHMARK'
    loaded_tracks: int = 0
    evaluated_with_ground_truth: int = 0
    missing_expected_rows: int = 0
    passed_count: int = 0
    failed_count: int = 0
    eligible_count: int = 0
    held_count: int = 0
    authorized_count: int = 0
    outcomes: List[BenchmarkTrackOutcome] = field(default_factory=list)
    notes: str = ""


def _load_external_expected_rows(path: Optional[Path]) -> Dict[str, Tuple[int, List[str], Dict[str, Any]]]:
    if not path or not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: Dict[str, Tuple[int, List[str], Dict[str, Any]]] = {}
    if isinstance(raw, dict):
        for fid, entry in raw.items():
            if isinstance(entry, dict) and "row_num" in entry and "sheet_row" in entry:
                out[str(fid)] = (
                    int(entry["row_num"]),
                    [str(x) for x in entry["sheet_row"]],
                    dict(entry.get("expected") or {}),
                )
    return out


def execute_isolated_benchmark(
    config: Config,
    sample_size: int = 10,
    checkpoint_path: Optional[Path] = None,
    expected_rows_path: Optional[Path] = None,
) -> BenchmarkSuiteResult:
    """
    Run offline fixture regression inside a temporary isolated SQLite DB.
    Never imports into or modifies `config.db_path`.
    """
    with tempfile.TemporaryDirectory(prefix="musicvault_bm_") as tmp_dir:
        bm_db = Database(Path(tmp_dir) / "benchmark_isolated.db")
        expected_map = dict(REFERENCE_ROWS_BY_ID)
        expected_map.update(_load_external_expected_rows(expected_rows_path))

        if sample_size == 10 and checkpoint_path is None:
            tracks_to_run: List[Track] = []
            for item in BENCHMARK_10_FIXTURE:
                cp = item["checkpoint"]
                t = Track(
                    file_id=item["file_id"],
                    name=item["filename"],
                    metadata=TrackMetadata(
                        version=cp["version"],
                        artist=cp["artist"],
                        title=cp["title"],
                        duration=float(cp["duration"]),
                        isrc=cp["isrc"],
                        embedded_bpm=cp["embedded_bpm"],
                        embedded_key=cp["embedded_key"],
                        embedded_genre=cp["embedded_genre"],
                        identity_origin=cp["identity_origin"],
                    ),
                )
                bm_db.save_track(t)
                tracks_to_run.append(t)
        else:
            # Resolve checkpoint file for >=50 or custom benchmark without mutating live DB
            resolved_cp = checkpoint_path
            if resolved_cp is None:
                for cand in (
                    Path("/storage/emulated/0/Download/v06_local_checkpoint.json"),
                    Path("/mnt/sdcard/Download/v06_local_checkpoint.json"),
                    Path("reference/v06_local_checkpoint.json"),
                ):
                    if cand.exists():
                        resolved_cp = cand
                        break

            if resolved_cp and resolved_cp.exists():
                bm_db.import_json_checkpoint(resolved_cp)
                tracks_to_run = bm_db.get_all_tracks()[:sample_size]
            else:
                # Seed the 10 bundled control tracks and report missing remaining checkpoint/expected rows honestly
                tracks_to_run = []
                for item in BENCHMARK_10_FIXTURE[:sample_size]:
                    cp = item["checkpoint"]
                    tracks_to_run.append(
                        Track(
                            file_id=item["file_id"],
                            name=item["filename"],
                            metadata=TrackMetadata(
                                version=cp["version"],
                                artist=cp["artist"],
                                title=cp["title"],
                                duration=float(cp["duration"]),
                            ),
                        )
                    )

        res = BenchmarkSuiteResult(sample_size=sample_size, status="PASS", loaded_tracks=len(tracks_to_run))
        if not tracks_to_run:
            res.status = "INCOMPLETE_BENCHMARK"
            res.notes = "No benchmark checkpoint fixture available."
            return res

        for t in tracks_to_run:
            if t.file_id not in expected_map:
                # NEVER fabricate fake row 999! Report unavailable expected row honestly.
                res.missing_expected_rows += 1
                res.outcomes.append(
                    BenchmarkTrackOutcome(
                        row_num=None,
                        file_id=t.file_id,
                        track_name=t.name,
                        expected_available=False,
                        passed=None,
                        expected_destination="<UNAVAILABLE_IN_FIXTURE>",
                        actual_destination="",
                        expected_reason="<UNAVAILABLE_IN_FIXTURE>",
                        actual_reason="UNAVAILABLE_EXPECTED_SHEET_ROW",
                        eligible=False,
                        move_authorized=False,
                        failure_detail="No ground-truth sheet row in supplied reference fixtures (mock row 999 disabled)",
                    )
                )
                continue

            res.evaluated_with_ground_truth += 1
            row_num, sheet_row, exp = expected_map[t.file_id]
            cands = bm_db.get_candidates(t.file_id)
            prop = build_track_proposal(t.metadata, cands)
            planned_row(
                candidates=cands,
                proposal_row=prop,
                old_row=sheet_row,
                save_medium=True,
                provider_terms_cleared=False,
            )
            plan = build_move_plan(
                file_id=t.file_id,
                track_name=t.name,
                row_num=row_num,
                sheet_row=sheet_row,
                allow_provisional=False,
                dry_run=True,
                read_only_build=True,
            )

            if plan.eligible:
                res.eligible_count += 1
            else:
                res.held_count += 1
            if plan.move_authorized:
                res.authorized_count += 1

            mismatches: List[str] = []
            if "eligible" in exp and plan.eligible != exp["eligible"]:
                mismatches.append(f"eligible expected {exp['eligible']} got {plan.eligible}")
            if "destination" in exp and plan.destination != exp["destination"]:
                mismatches.append(f"destination expected '{exp['destination']}' got '{plan.destination}'")
            if "reason" in exp and plan.reason != exp["reason"]:
                mismatches.append(f"reason expected '{exp['reason']}' got '{plan.reason}'")
            if "routing_confidence" in exp and plan.routing_confidence.value != exp["routing_confidence"]:
                mismatches.append(f"confidence expected '{exp['routing_confidence']}' got '{plan.routing_confidence.value}'")
            if plan.move_authorized is not False:
                mismatches.append("move_authorized must be False in dry-run benchmark")

            ok = len(mismatches) == 0
            if ok:
                res.passed_count += 1
            else:
                res.failed_count += 1

            res.outcomes.append(
                BenchmarkTrackOutcome(
                    row_num=row_num,
                    file_id=t.file_id,
                    track_name=t.name,
                    expected_available=True,
                    passed=ok,
                    expected_destination=str(exp.get("destination", "")),
                    actual_destination=plan.destination,
                    expected_reason=str(exp.get("reason", "")),
                    actual_reason=plan.reason,
                    eligible=plan.eligible,
                    move_authorized=plan.move_authorized,
                    failure_detail="; ".join(mismatches),
                )
            )

        if res.failed_count > 0:
            res.status = "FAIL"
        elif res.missing_expected_rows > 0 or res.loaded_tracks < sample_size:
            res.status = "INCOMPLETE_BENCHMARK"
            res.notes = (
                f"Fixture coverage incomplete: {res.evaluated_with_ground_truth}/{sample_size} tracks have supplied "
                f"expected sheet rows ({res.missing_expected_rows} lack expected rows; mock row 999 fallback disabled)."
            )
        else:
            res.status = "PASS"
            res.notes = f"Exact fixture regression match ({res.passed_count}/{sample_size}; {res.eligible_count} eligible, {res.held_count} held, 0 authorized)."

        return res
