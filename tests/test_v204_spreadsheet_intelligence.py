"""
Comprehensive regression suite for MusicVault v2.0.4 Spreadsheet Intelligence & Full-Vault Reconciliation.
Built from sanitized production reconciliation fixtures (zero credentials/tokens).
Covers:
1. Columns L/M/P human locks, deliberate key/genre withholds, [Vault audit] footer stripping, and Q:AA locks.
2. 'Unclassified' / 'Needs Review' / '*Subgenre pending' placeholder upgrades (HIGH & MEDIUM) vs preservation on LOW/MISSING/TERMS_HOLD.
3. Blank rows and strict idempotence of planned_row().
4. Evidence-backed 'Dance / Club' & 'Dance & Club' -> 'Hardstyle' upgrades vs keeping 124-126 BPM club tracks intact,
   and blocking artist-only or BPM-only genre/subgenre inference.
5. Granular subgenres (Euphoric, Rawstyle, Rawphoric, Slowtempo, Uptempo, Hard Techno, Dancefloor) and conflicting subgenres.
6. Genuine slow-tempo Hardstyle preservation (no forced 150+ BPM doubling or genre rejection).
7. Integer BPM display + raw measurement retention + <=0.5 BPM same-integer clustering + track-corroborated 0.5x/2x octave
   resolution vs never manufacturing BPM/key.
8. Key normalization (Camelot, OpenKey, standard notation), invalid key rejection, and conflict preservation.
9. Version/edition distinction across original, remix, edit, and extended versions.
10. Cross-root / within-root identical-MD5 distinct file_id preservation (never merging/deleting), SonoVault disabled guard,
    and bounded interruption recovery.
"""
import json
from argparse import Namespace
from contextlib import closing

import pytest

from musicvault.config import Config
from musicvault.core.coverage import _edition, decisions, library_report
from musicvault.core.genre import (
    canonical_genre,
    canonical_subgenre,
    decide_genre,
    is_genre_placeholder,
    is_subgenre_placeholder,
    is_umbrella_club_genre,
)
from musicvault.core.inventory import scan_root
from musicvault.core.matcher import (
    format_display_bpm,
    is_valid_camelot,
    match_track,
    normalize_key_to_camelot,
    versions_compatible,
)
from musicvault.core.organizer import evaluate_live_preview
from musicvault.core.provider_policy import ProviderPolicy
from musicvault.core.rules import (
    build_track_proposal,
    field_decision,
    inspect_sheet_row_semantics,
    parse_lock_states,
    planned_row,
)
from musicvault.db import Database
from musicvault.models import Candidate, Track, TrackMetadata
from musicvault.services.sheets import validate_and_index_tracks_sheet


def _make_tracks_row(
    filename="Artist - Track.wav",
    src_folder="Unsorted Import",
    genre="Unclassified",
    subgenre="Needs Review",
    bpm="",
    key="",
    proc_status="PENDING",
    anal_conf="",
    file_id="fid_test_0001",
    notes="",
):
    return [
        filename, src_folder, genre, subgenre, bpm, key,
        "", "", "WAV", "30.0", "", proc_status, anal_conf,
        f"https://drive.google.com/file/d/{file_id}/view", file_id, notes,
    ]


def test_v204_lmp_locks_key_withholds_and_vault_audit_footer_stripping():
    # Sanitized Row 2 ('Believe') fixture: BPM high (147), key withheld due to conflict, genre House vs Techno inconsistent,
    # plus trailing automated [Vault audit ...] lines that must NOT confuse human note parsing.
    row_believe = _make_tracks_row(
        filename="Believe - Jesse Bloch, Coby Watts.wav",
        genre="Dance & Club",
        subgenre="Needs Review",
        bpm="147",
        key="",
        proc_status="REVIEW",
        anal_conf="BPM high: audio 147.2, embedded 146.3, published 147; key conflicts",
        file_id="fid_believe_row2",
        notes=(
            "Audio checked: source tag 146.3/3A, signal 147.2, published 147/2B; key withheld. "
            "Genre House versus Techno inconsistent; keep source in import until resolved.\n"
            "[Vault audit hash1] BPM estimate 143.55; key candidate 3B; see Vault Audit v0.3 tab. Not verified."
        ),
    )
    sem = inspect_sheet_row_semantics(row_believe)
    assert "[Vault audit" not in sem["human_notes"]
    assert sem["field_locks"]["bpm"]["state"] == "USER_LOCKED"
    assert sem["field_holds"]["key"]["state"] == "REVIEW_HOLD"
    assert sem["field_holds"]["genre"]["state"] == "REVIEW_HOLD"
    assert "keep_in_source_hold" in sem["row_flags"]

    # Even if HIGH 2-provider candidates propose BPM 146, Key 2B, and Genre Hardstyle, NONE may overwrite Row 2!
    cands = [
        Candidate("MusicBrainz", "Believe", ["Jesse Bloch", "Coby Watts"], duration=173.0, bpm=146.0, key="2B", genre=["Hardstyle"], verification="ISRC supported", source_family="musicbrainz"),
        Candidate("Discogs release", "Believe", ["Jesse Bloch", "Coby Watts"], duration=173.0, bpm=146.0, key="2B", genre=["Hardstyle"], verification="Duration supported", source_family="discogs"),
    ]
    prop = build_track_proposal(TrackMetadata(artist="Jesse Bloch & Coby Watts", title="Believe", duration=173.07), cands)
    new_row, dec, chg = planned_row(cands, prop, row_believe, provider_terms_cleared=True)
    assert new_row[4] == "147" and dec["bpm"] == "LOCKED"
    assert new_row[5] == "" and dec["key"] == "REVIEW_HOLD"
    assert new_row[2] == "Dance & Club" and dec["genre"] == "REVIEW_HOLD"
    assert new_row[16] == "★"

    # Sanitized Row 4 ('Bassdrum'): 'existing key retained' + 'BPM high' locks both BPM (126) and Key (5A)
    row_bassdrum = _make_tracks_row(
        filename="Bassdrum - Nora & Chris.wav",
        genre="Dance & Club",
        subgenre="Nightclub - Subgenre Pending",
        bpm="126",
        key="5A",
        proc_status="SORTED – AUDIO PENDING",
        anal_conf="BPM high: CQT/chroma and external track data agree 126 BPM/5A",
        file_id="fid_bassdrum_row4",
        notes="Supports BPM only, existing key retained.",
    )
    locks_bd, err_bd = parse_lock_states(row_bassdrum)
    assert err_bd is None
    assert locks_bd["bpm"]["state"] == "USER_LOCKED"
    assert locks_bd["key"]["state"] == "USER_LOCKED"


def test_v204_placeholders_blank_rows_terms_hold_and_idempotence():
    assert is_genre_placeholder("Unclassified") is True
    assert is_genre_placeholder("Needs Review") is True
    assert is_genre_placeholder("Hardstyle") is False
    assert is_subgenre_placeholder("Needs Review") is True
    assert is_subgenre_placeholder("Nightclub - Subgenre Pending") is True
    assert is_subgenre_placeholder("Hard Techno (review)") is True
    assert is_subgenre_placeholder("Euphoric") is False

    # 1. When evidence is LOW / absent, 'Unclassified' and 'Needs Review' MUST be preserved (never wiped to '')!
    row_unres = _make_tracks_row(genre="Unclassified", subgenre="Needs Review", file_id="fid_unres_1")
    meta_unres = TrackMetadata(artist="Rebelion", title="Unknown ID", duration=200.0, embedded_bpm="155", embedded_genre="Dance")
    prop_unres = build_track_proposal(meta_unres, [])  # Zero catalogue candidates -> artist profile alone stays LOW!
    assert field_decision([], "genre", prop_unres)[0] == "LOW"
    out_unres, dec_unres, chg_unres = planned_row([], prop_unres, row_unres, provider_terms_cleared=True)
    assert out_unres[2] == "Unclassified"
    assert out_unres[3] == "Needs Review"
    assert out_unres[4] == ""  # Unverified embedded BPM 155 NOT written to Tracks!
    assert chg_unres == []

    # 2. When HIGH 2-provider evidence exists:
    cands_hs = [
        Candidate("MusicBrainz", "Living Fast", ["Act of Rage"], duration=240.0, genre=["hardstyle", "rawstyle"], verification="ISRC supported", source_family="musicbrainz"),
        Candidate("Discogs release", "Living Fast", ["Act of Rage"], duration=240.0, genre=["Hardstyle", "Rawstyle"], bpm=158.0, key="11A", verification="Duration supported", source_family="discogs"),
    ]
    meta_lf = TrackMetadata(artist="Act of Rage", title="Living Fast", duration=240.0)
    prop_lf = build_track_proposal(meta_lf, cands_hs)
    assert prop_lf["genre"] == "Hardstyle"
    assert prop_lf["subgenre"] == "Rawstyle"
    assert field_decision(cands_hs, "genre", prop_lf)[0] == "HIGH"
    assert field_decision(cands_hs, "subgenre", prop_lf)[0] == "HIGH"

    # Under PROVIDER_TERMS_CLEARED=False, restricted Discogs provenance holds write under TERMS_HOLD while keeping 'Unclassified' intact:
    out_hold, dec_hold, chg_hold = planned_row(cands_hs, prop_lf, row_unres, provider_terms_cleared=False)
    assert dec_hold["genre"] == "TERMS_HOLD"
    assert out_hold[2] == "Unclassified"
    assert chg_hold == []

    # Under PROVIDER_TERMS_CLEARED=True, upgrades placeholders cleanly AND is strictly idempotent on second pass!
    out_1, dec_1, chg_1 = planned_row(cands_hs, prop_lf, row_unres, provider_terms_cleared=True)
    assert out_1[2] == "Hardstyle"
    assert out_1[3] == "Rawstyle"
    assert out_1[4] == "158"
    assert out_1[5] == "11A"
    assert set(chg_1) == {"genre", "subgenre", "bpm", "key"}

    out_2, dec_2, chg_2 = planned_row(cands_hs, prop_lf, out_1, provider_terms_cleared=True)
    assert out_2[:22] == out_1[:22]
    assert chg_2 == []


def test_v204_evidence_backed_dance_club_to_hardstyle_and_no_artist_or_bpm_only_inference():
    assert is_umbrella_club_genre("Dance / Club") is True
    assert is_umbrella_club_genre("Dance & Club") is True

    # 1. Genuine 124 BPM club track ('Blank Space - Wasback') with House/Dance candidates stays 'Dance & Club'!
    row_club = _make_tracks_row(
        filename="Blank Space - Wasback.wav",
        genre="Dance & Club",
        subgenre="Nightclub - Subgenre Pending",
        bpm="124",
        proc_status="SORTED – AUDIO PENDING",
        anal_conf="BPM high: audio 124.3; stored 61.9 is half time",
        file_id="fid_wasback",
    )
    cands_house = [
        Candidate("MusicBrainz", "Blank Space", ["Wasback"], duration=150.0, genre=["House", "Slap House"], verification="Duration supported", source_family="musicbrainz"),
    ]
    prop_club = build_track_proposal(TrackMetadata(artist="Wasback", title="Blank Space", duration=150.0), cands_house)
    out_club, dec_club, chg_club = planned_row(cands_house, prop_club, row_club, provider_terms_cleared=True)
    assert out_club[2] == "Dance & Club"
    assert "genre" not in chg_club

    # 2. Known Hardstyle artist ('Rebelion') at 160 BPM with ZERO recording-verified catalogue genre must NOT upgrade 'Dance / Club'!
    row_artist_only = _make_tracks_row(
        filename="Rebelion - Unreleased Tool.wav",
        genre="Dance / Club",
        subgenre="Subgenre pending",
        file_id="fid_rebelion_tool",
    )
    cands_bpm_only = [
        Candidate("Audiometa", "Unreleased Tool", ["Rebelion"], duration=180.0, bpm=160.0, key="8A", genre=[], verification="Duration supported", source_family="audiometa"),
    ]
    prop_ao = build_track_proposal(TrackMetadata(artist="Rebelion", title="Unreleased Tool", duration=180.0, embedded_bpm="160"), cands_bpm_only)
    assert field_decision(cands_bpm_only, "genre", prop_ao)[0] == "LOW"
    out_ao, dec_ao, _ = planned_row(cands_bpm_only, prop_ao, row_artist_only, provider_terms_cleared=True)
    assert out_ao[2] == "Dance / Club"  # Not upgraded from artist/BPM alone!

    # 3. Recording-verified Hardstyle evidence upgrades 'Dance / Club' -> 'Hardstyle' with provenance & double-check star!
    cands_rooler = [
        Candidate("MusicBrainz", "MOVE 2 DA BEAT", ["Rooler"], duration=190.0, genre=["Hardstyle", "Rawstyle"], verification="ISRC supported", source_family="musicbrainz"),
    ]
    row_rooler = _make_tracks_row(
        filename="MOVE 2 DA BEAT - Rooler.wav",
        genre="Dance / Club",
        subgenre="Subgenre pending",
        file_id="fid_rooler_m2db",
    )
    prop_rooler = build_track_proposal(TrackMetadata(artist="Rooler", title="MOVE 2 DA BEAT", duration=190.0), cands_rooler)
    out_rooler, dec_rooler, chg_rooler = planned_row(cands_rooler, prop_rooler, row_rooler, provider_terms_cleared=True)
    assert out_rooler[2] == "Hardstyle"
    assert out_rooler[3] == "Rawstyle"
    assert dec_rooler["genre"] == "MEDIUM_PROVISIONAL"
    assert out_rooler[16] == "★"
    assert decide_genre("Dance / Club", ["Hardstyle"]) == ("Hardstyle", "UMBRELLA_DANCE_CLUB_TO_HARDSTYLE")


def test_v204_slowtempo_hardstyle_bpm_clustering_and_corroborated_octave_resolution():
    # 1. Genuine slow-tempo Hardstyle (105 BPM, Slowtempo subgenre or uncorroborated 112 BPM) stays intact — NEVER forced to 210/224!
    cands_slow = [
        Candidate("MusicBrainz", "Slow Hardstyle Interlude", ["Atmozfears"], duration=180.0, bpm=105.0, genre=["Hardstyle", "Slowtempo"], verification="Duration supported", source_family="musicbrainz"),
        Candidate("Discogs release", "Slow Hardstyle Interlude", ["Atmozfears"], duration=180.0, bpm=105.2, genre=["Hardstyle", "Slowtempo"], verification="Duration supported", source_family="discogs"),
    ]
    prop_slow = build_track_proposal(TrackMetadata(artist="Atmozfears", title="Slow Hardstyle Interlude", duration=180.0, embedded_bpm="105"), cands_slow)
    assert prop_slow["genre"] == "Hardstyle"
    assert prop_slow["subgenre"] == "Slowtempo"
    assert prop_slow["bpm"] == 105
    assert prop_slow["bpm_display"] == "105"
    assert prop_slow["tempo_hint"] == ""
    assert field_decision(cands_slow, "bpm", prop_slow)[0] == "HIGH"

    # 2. Credible same-integer BPM clustering (151.9 + 152.0 -> 152 HIGH, raw measurements retained) vs 150.0 vs 151.0 -> CONFLICT!
    cands_arcana = [
        Candidate("Audiometa", "Arcana", ["Hyperion"], duration=195.0, bpm=151.9, key="C minor", verification="Duration supported", source_family="audiometa"),
        Candidate("SonoVault", "Arcana", ["Hyperion"], duration=195.0, bpm=152.0, key="5A", genre=["Hardstyle", "Euphoric"], verification="Duration supported", source_family="sonovault"),
    ]
    prop_arc = build_track_proposal(TrackMetadata(artist="Hyperion", title="Arcana", duration=195.0, embedded_bpm="101.7", embedded_key="5B"), cands_arcana)
    assert prop_arc["bpm"] == 152
    assert prop_arc["bpm_display"] == "152"
    assert prop_arc["key"] == "5A"
    assert len(prop_arc["bpm_raw_measurements"]) == 2
    assert field_decision(cands_arcana, "bpm", prop_arc)[0] == "HIGH"
    assert field_decision(cands_arcana, "key", prop_arc)[0] == "HIGH"

    # 3. Corroborated 0.5x <-> 2x octave resolution across providers (87.0 + 174.0 on Drum & Bass -> 174 MEDIUM + octave flag!)
    cands_dnb_oct = [
        Candidate("Audiometa", "Big City Life", ["Luude", "Mattafix"], duration=149.6, bpm=87.0, key="5B", verification="Duration supported", source_family="audiometa"),
        Candidate("SonoVault", "Big City Life", ["Luude", "Mattafix"], duration=149.6, bpm=174.0, key="5B", genre=["Drum & Bass", "Dancefloor"], verification="Duration supported", source_family="sonovault"),
    ]
    prop_oct = build_track_proposal(TrackMetadata(artist="Luude & Mattafix", title="Big City Life", duration=149.58, embedded_bpm="87"), cands_dnb_oct)
    assert prop_oct["bpm"] == 174
    assert prop_oct["bpm_octave_adjusted"] is True
    assert field_decision(cands_dnb_oct, "bpm", prop_oct)[0] == "MEDIUM"

    # 4. Uncorroborated single-source half-time BPM (87.0 only, embedded 87) is NOT auto-doubled; emits Drum & Bass tempo_hint!
    prop_uncorr = build_track_proposal(
        TrackMetadata(artist="Luude & Mattafix", title="Big City Life", duration=149.58, embedded_bpm="87"),
        [cands_dnb_oct[0], Candidate("MusicBrainz", "Big City Life", ["Luude", "Mattafix"], duration=149.6, genre=["Drum & Bass"], verification="Duration supported", source_family="musicbrainz")],
    )
    assert prop_uncorr["bpm"] == 87
    assert "174" in prop_uncorr["tempo_hint"]


def test_v204_key_normalization_subgenre_conflicts_and_edition_distinction():
    assert normalize_key_to_camelot("C minor") == "5A"
    assert normalize_key_to_camelot("08a") == "8A"
    assert normalize_key_to_camelot("1m") == "8A"
    assert normalize_key_to_camelot("H# superlocrian") == ""
    assert is_valid_camelot("12B") is True
    assert is_valid_camelot("13A") is False
    assert format_display_bpm("154.6") == "155"
    assert format_display_bpm("") == ""

    # Subgenre conflict (Euphoric vs Rawstyle on same track) stays CONFLICT (LOW)
    cands_sub_conf = [
        Candidate("MusicBrainz", "Crossover", ["Artist"], duration=200.0, genre=["Hardstyle", "Euphoric"], verification="ISRC supported", source_family="musicbrainz"),
        Candidate("Discogs release", "Crossover", ["Artist"], duration=200.0, genre=["Hardstyle", "Rawstyle"], verification="Duration supported", source_family="discogs"),
    ]
    prop_sc = build_track_proposal(TrackMetadata(artist="Artist", title="Crossover", duration=200.0), cands_sub_conf)
    assert prop_sc["genre"] == "Hardstyle"
    assert prop_sc["subgenre"] == "CONFLICT"
    assert field_decision(cands_sub_conf, "subgenre", prop_sc)[0] == "LOW"

    # Edition distinction: original, remix, edit, extended
    assert _edition("Living Fast (Original Mix)") == "original"
    assert _edition("Loud (Radio Edit)") == "edit"
    assert _edition("Big City Life (Extended)") == "extended"
    assert _edition("Big City Life (Odd Mob Remix)") == "remix (oddmob)"

    assert versions_compatible("Living Fast", "Living Fast (Original Mix)") is True
    assert versions_compatible("Living Fast (Original Mix)", "Living Fast (Extended Mix)") is False
    assert versions_compatible("Loud (Radio Edit)", "Loud (Extended)") is False
    assert versions_compatible("Big City Life (Extended)", "Big City Life (Odd Mob Remix)") is False
    assert match_track("Luude", "Big City Life (Radio Edit)", "Big City Life (Extended)", ["Luude", "Mattafix"]) is False


def test_v204_distinct_file_id_md5_duplicates_preserved_and_sonovault_disabled_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("DISABLED_RESEARCH_PROVIDERS", "sonovault")
    cfg = Config(tmp_path)
    cfg.set("SONOVAULT_API_KEY", "sv_should_not_be_used")
    db = Database(cfg.db_path)

    # Cross-root identical-MD5 files with distinct Drive file IDs (sanitized 'Release 10Y Remix' fixture)
    class CrossRootDupDrive:
        def scan_inventory(self, root):
            if root == cfg.source_folder_id:
                yield {
                    "id": "src_dup_fid_1",
                    "name": "Atmozfears, David Spekter - Release (10 Year Anniversary Remix).aiff",
                    "parents": [root],
                    "scanned_parent": root,
                    "md5Checksum": "same_md5_8d69c9",
                }
            else:
                yield {
                    "id": "dst_dup_fid_2",
                    "name": "Atmozfears & David Spekter - Release (10 Year Anniversary Remix).aiff",
                    "parents": ["VAULT_EUPHORIC_ID"],
                    "scanned_parent": "VAULT_EUPHORIC_ID",
                    "scanned_path": ["Hardstyle", "Euphoric - Classic", "Atmozfears & David Spekter - Release (10 Year Anniversary Remix).aiff"],
                    "md5Checksum": "same_md5_8d69c9",
                }
        def get_children_folders(self, root):
            return {"Hardstyle": ["VAULT_HS_ID"]}

    class DupHoldSheet:
        def fetch_header_row(self, *a):
            return _make_tracks_row()[:0] + [
                "Original Filename", "Source Folder", "Genre", "Subgenre", "BPM (full)", "Camelot Key",
                "Energy /10", "Filth /10", "Format", "Size (MB)", "Proposed Filename", "Processing Status",
                "Analysis Confidence", "Drive File URL", "Drive File ID", "Notes / Override",
            ]
        def iter_tracks_rows(self, *a):
            yield 2, _make_tracks_row(
                filename="Atmozfears, David Spekter - Release (10 Year Anniversary Remix).aiff",
                genre="Hardstyle", subgenre="Euphoric", bpm="155", proc_status="REVIEW",
                anal_conf="BPM high; key conflicting",
                file_id="src_dup_fid_1",
                notes="Potential duplicate: Music Vault/Euphoric - Classic already has same named AIFF.",
            )
            yield 3, _make_tracks_row(
                filename="Atmozfears & David Spekter - Release (10 Year Anniversary Remix).aiff",
                src_folder="Hardstyle / Euphoric - Classic",
                genre="Hardstyle", subgenre="Euphoric", bpm="155", proc_status="SORTED – AUDIO PENDING",
                file_id="dst_dup_fid_2",
            )

    drive = CrossRootDupDrive()
    scan_root(db, drive, cfg.source_folder_id, "source")
    scan_root(db, drive, cfg.vault_folder_id, "destination")

    # Both distinct file_ids MUST be preserved without merging or deleting!
    inv = db.get_inventory_summary()
    assert inv["total_records"] == 2
    assert inv["source_membership_tracks"] == 1
    assert inv["destination_tracks"] == 1
    assert inv["unique_library_tracks"] == 2

    # SonoVault disabled guard verified
    pol = ProviderPolicy(cfg)
    assert pol.permits("sonovault") is False
    assert pol.reason_to_skip("sonovault", {"identity", "genre", "bpm", "key"}) == "disabled_by_policy"

    # Human duplicate note on src_dup_fid_1 holds routing in review_or_incomplete even if 2-provider Hardstyle verified!
    for fid in ("src_dup_fid_1", "dst_dup_fid_2"):
        db.update_hydration_result(
            fid, "success", "same_md5_8d69c9", method="embedded_ranged",
            metadata=TrackMetadata(version="same_md5_8d69c9", artist="Atmozfears & David Spekter", title="Release (10 Year Anniversary Remix)", duration=246.0, identity_origin="embedded"),
        )
        db.update_research_result(
            fid, "success", "same_md5_8d69c9",
            candidates=[
                Candidate("MusicBrainz", "Release (10 Year Anniversary Remix)", ["Atmozfears", "David Spekter"], duration=246.0, genre=["Hardstyle", "Euphoric"], verification="Duration supported", source_family="musicbrainz"),
                Candidate("Discogs release", "Release (10 Year Anniversary Remix)", ["Atmozfears", "David Spekter"], duration=246.0, genre=["Hardstyle", "Euphoric"], verification="Duration supported", source_family="discogs"),
            ],
            provider_calls={"_meta": {"lookup_complete": True}, "MusicBrainz": {"request_made": True}},
            research_incomplete=False,
        )

    exp = tmp_path / "dup_cov.json"
    with closing(db._get_connection()) as conn:
        rep = library_report(
            conn, db, cfg,
            Namespace(scope="all", file_id=[], verbose=True, export_json=str(exp), export_csv=""),
            DupHoldSheet(), drive,
        )
    rows_by_id = {r["file_id"]: r for r in json.loads(exp.read_text())["rows"]}
    assert rows_by_id["src_dup_fid_1"]["routing"] == "review_or_incomplete"
    assert "potential_duplicate_hold" in rows_by_id["src_dup_fid_1"]["double_check_flags"]
    assert rows_by_id["dst_dup_fid_2"]["routing"] == "already_correctly_located"
    assert rep["providers"]["sonovault"]["availability"] == "DISABLED"


def test_v204_audit_fixes_hardcore_subgenres_sefa_and_schranz_parent_resolution():
    # Audit Fix 1: Subgenre-only tags resolve to their canonical SUBGENRE_PARENT; Uptempo & Frenchcore stay Hardcore!
    assert canonical_genre("Uptempo") == "Hardcore"
    assert canonical_genre("Frenchcore") == "Hardcore"
    assert canonical_genre("Schranz") == "Techno"
    assert canonical_genre("Melodic Hardstyle") == "Hardstyle"

    cands_uptempo = [
        Candidate("MusicBrainz", "Bassline", ["N-Vitral"], duration=180.0, genre=["Hardcore", "Uptempo"], verification="ISRC supported", source_family="musicbrainz"),
        Candidate("Discogs release", "Bassline", ["N-Vitral"], duration=180.0, genre=["Uptempo"], verification="Duration supported", source_family="discogs"),
    ]
    prop_up = build_track_proposal(TrackMetadata(artist="N-Vitral", title="Bassline", duration=180.0), cands_uptempo)
    assert prop_up["genre"] == "Hardcore"
    assert prop_up["subgenre"] == "Uptempo"
    assert field_decision(cands_uptempo, "genre", prop_up)[0] == "HIGH"
    assert field_decision(cands_uptempo, "subgenre", prop_up)[0] == "HIGH"

    cands_sefa = [
        Candidate("MusicBrainz", "Lovely Day to Die", ["Sefa"], duration=210.0, genre=["Hardcore", "Frenchcore"], verification="ISRC supported", source_family="musicbrainz"),
    ]
    prop_sefa = build_track_proposal(TrackMetadata(artist="Sefa", title="Lovely Day to Die", duration=210.0), cands_sefa)
    assert prop_sefa["genre"] == "Hardcore"
    assert prop_sefa["subgenre"] == "Frenchcore"
    assert field_decision(cands_sefa, "genre", prop_sefa)[0] == "MEDIUM"

    cands_schranz = [
        Candidate("MusicBrainz", "Warehouse", ["Klangkuenstler"], duration=300.0, genre=["Schranz"], verification="Duration supported", source_family="musicbrainz"),
    ]
    prop_sch = build_track_proposal(TrackMetadata(artist="Klangkuenstler", title="Warehouse", duration=300.0), cands_schranz)
    assert prop_sch["genre"] == "Techno"
    assert prop_sch["subgenre"] == "Hard Techno"
    assert field_decision(cands_schranz, "genre", prop_sch)[0] == "MEDIUM"
    assert field_decision(cands_schranz, "subgenre", prop_sch)[0] == "MEDIUM"


def test_v204_audit_fixes_moved_review_locks_text_only_guard_and_reordered_headers(tmp_path):
    # Audit Fix 2a & 2b: MOVED locks Dance / Club; REVIEW blocks umbrella upgrade AND holds evaluate_live_preview
    cands_hs = [
        Candidate("MusicBrainz", "Anthem", ["Artist"], duration=200.0, genre=["Hardstyle"], verification="ISRC supported", source_family="musicbrainz"),
        Candidate("Discogs release", "Anthem", ["Artist"], duration=200.0, genre=["Hardstyle"], verification="Duration supported", source_family="discogs"),
    ]
    prop_hs = build_track_proposal(TrackMetadata(artist="Artist", title="Anthem", duration=200.0), cands_hs)

    row_moved = _make_tracks_row(genre="Dance / Club", subgenre="Club", proc_status="MOVED", file_id="fid_moved_club")
    out_mv, dec_mv, chg_mv = planned_row(cands_hs, prop_hs, row_moved, provider_terms_cleared=True)
    assert out_mv[2] == "Dance / Club"
    assert dec_mv["genre"] == "LOCKED"
    assert chg_mv == []

    row_review = _make_tracks_row(genre="Dance & Club", subgenre="Needs Review", proc_status="REVIEW", file_id="fid_review_club")
    out_rv, dec_rv, chg_rv = planned_row(cands_hs, prop_hs, row_review, provider_terms_cleared=True)
    assert out_rv[2] == "Dance & Club"
    assert dec_rv["genre"] == "EXISTING_CONFLICT_REVIEW"
    assert "genre" not in chg_rv

    # Audit Fix 2c: Col M duplicate & key-unconfirmed phrasings
    row_colm_dup = _make_tracks_row(
        genre="Hardstyle", subgenre="Euphoric", bpm="150", key="",
        proc_status="SORTED – AUDIO PENDING",
        anal_conf="Potential duplicate across formats; no reliable key; key/genre conflict",
        file_id="fid_colm_dup",
    )
    sem_m = inspect_sheet_row_semantics(row_colm_dup)
    assert "potential_duplicate_hold" in sem_m["row_flags"]
    assert sem_m["field_holds"]["key"]["state"] == "REVIEW_HOLD"
    assert sem_m["field_holds"]["genre"]["state"] == "REVIEW_HOLD"

    # Audit Fix 4: Text-only Discogs candidate must NOT trigger umbrella upgrade in evaluate_live_preview
    tr_text_only = Track(
        file_id="fid_text_only",
        name="Artist - ClubTrack.flac",
        is_live_source=True,
        observed_parent_id="SRC_ID",
        hydration_status="success",
        hydration_version="v1",
        research_status="success",
        research_version="v1",
        provider_calls_json=json.dumps({"_meta": {"lookup_complete": True}}),
        metadata=TrackMetadata(version="v1", artist="Artist", title="ClubTrack", duration=180.0, identity_origin="embedded"),
    )
    cands_text_only = [
        Candidate("Discogs release", "ClubTrack", ["Artist"], duration=0.0, genre=["Hardstyle"], verification="Text only", source_family="discogs"),
    ]
    tr_rev = Track(
        file_id="fid_review_live",
        name="Artist - Anthem.flac",
        is_live_source=True,
        observed_parent_id="SRC_ID",
        hydration_status="success",
        hydration_version="v1",
        research_status="success",
        research_version="v1",
        provider_calls_json=json.dumps({"_meta": {"lookup_complete": True}}),
        metadata=TrackMetadata(version="v1", artist="Artist", title="Anthem", duration=200.0, identity_origin="embedded"),
    )
    sheet_idx = validate_and_index_tracks_sheet(
        [
            "Original Filename", "Source Folder", "Genre", "Subgenre", "BPM (full)", "Camelot Key",
            "Energy /10", "Filth /10", "Format", "Size (MB)", "Proposed Filename", "Processing Status",
            "Analysis Confidence", "Drive File URL", "Drive File ID", "Notes / Override",
        ],
        [
            _make_tracks_row(genre="Dance / Club", proc_status="PENDING", file_id="fid_text_only"),
            _make_tracks_row(genre="Dance / Club", proc_status="REVIEW", file_id="fid_review_live"),
        ],
    )
    live_summary = evaluate_live_preview(
        [tr_text_only, tr_rev],
        {"fid_text_only": cands_text_only, "fid_review_live": cands_hs},
        sheet_idx,
        "SRC_ID",
        "VAULT_ID",
        vault_folders={"Hardstyle": ["HS_ID"], "Dance & Club": ["DC_ID"]},
        provider_terms_cleared=True,
    )
    rows_live = {r.file_id: r for r in live_summary.rows}
    assert rows_live["fid_text_only"].eligible is False
    assert rows_live["fid_text_only"].destination != "Hardstyle"
    assert rows_live["fid_review_live"].eligible is False
    assert rows_live["fid_review_live"].reason == "METADATA_OR_HUMAN_REVIEW_HOLD"

    # Audit Fix 6a: Custom reordered sheet with unmapped column at index 15 must NOT leak into Notes (col 15)
    custom_hdr = ["Title", "Artist", "Genre", "Subgenre", "BPM", "Key"] + [f"Extra{i}" for i in range(8)] + ["Drive ID", "ISRC"]
    custom_row = ["Song", "Act", "Hardstyle", "Rawstyle", "155", "8A"] + [""] * 8 + ["fid_custom_15", "potential duplicate key withheld"]
    c_idx = validate_and_index_tracks_sheet(custom_hdr, [custom_row])
    assert c_idx.schema_valid
    norm_r = c_idx.unique_rows_by_id["fid_custom_15"][1]
    assert norm_r[15] == ""
    assert inspect_sheet_row_semantics(norm_r)["row_flags"] == []


def test_v204_audit_fixes_genre_gated_octave_bpm_named_edits_and_coverage_holds(tmp_path):
    # Audit Fix 3: Pop / non-uptempo genre at 70 BPM + 140 BPM must NOT octave-double!
    cands_pop = [
        Candidate("MusicBrainz", "Ballad", ["Indie Singer"], duration=210.0, genre=["Pop"], verification="ISRC supported", source_family="musicbrainz"),
        Candidate("Audiometa", "Ballad", ["Indie Singer"], duration=210.0, bpm=70.0, verification="Duration supported", source_family="audiometa"),
    ]
    prop_pop = build_track_proposal(TrackMetadata(artist="Indie Singer", title="Ballad", duration=210.0, embedded_bpm="140"), cands_pop)
    assert prop_pop["genre"] == "Pop"
    assert prop_pop["bpm"] == 70
    assert prop_pop["bpm_octave_adjusted"] is False

    # Sheet Subgenre='Slowtempo' blocks 2x octave promotion even if Hardstyle candidates lack subgenre tags!
    cands_hs_oct = [
        Candidate("MusicBrainz", "Slow Rave", ["DJ"], duration=200.0, genre=["Hardstyle"], bpm=80.0, verification="Duration supported", source_family="musicbrainz"),
        Candidate("SonoVault", "Slow Rave", ["DJ"], duration=200.0, genre=["Hardstyle"], bpm=160.0, verification="Duration supported", source_family="sonovault"),
    ]
    prop_hs_oct = build_track_proposal(TrackMetadata(artist="DJ", title="Slow Rave", duration=200.0), cands_hs_oct)
    assert prop_hs_oct["bpm_octave_adjusted"] is True
    row_sheet_slow = _make_tracks_row(genre="Hardstyle", subgenre="Slowtempo", bpm="", file_id="fid_sheet_slow")
    out_sl, dec_sl, chg_sl = planned_row(cands_hs_oct, prop_hs_oct, row_sheet_slow, provider_terms_cleared=True)
    assert out_sl[4] == ""
    assert dec_sl["bpm"] == "REVIEW"
    assert "bpm" not in chg_sl

    # Audit Fix 5: Named DJ edits & Extended Remixes
    assert versions_compatible("Live Fast (Act of Rage Edit)", "Live Fast (Radio Edit)") is False
    assert versions_compatible("Live Fast (Act of Rage Edit)", "Live Fast (Rebelion Edit)") is False
    assert versions_compatible("Live Fast (Act of Rage Edit)", "Live Fast") is False
    assert versions_compatible("Live Fast (Act of Rage Edit)", "Live Fast (Act of Rage Edit)") is True
    assert _edition("Live Fast (Act of Rage Edit)") == "edit (actofrage)"
    assert versions_compatible("Song (Darren Styles Remix)", "Song (Darren Styles Extended Remix)") is True
    assert versions_compatible("Song (Darren Styles Radio Remix)", "Song (Darren Styles Extended Remix)") is False
    assert _edition("Song (Darren Styles Extended Remix)") == "remix (darrenstyles)"

    # Audit Fix 2d: Coverage REVIEW_HOLD downgrades field from verified -> conflicting/candidate_only and sets review_required=True;
    # MALFORMED_LOCK_JSON holds proposed!
    cfg = Config(tmp_path)
    cfg.set("PROVIDER_TERMS_CLEARED", "true")
    db = Database(cfg.db_path)

    class HoldDrive:
        def scan_inventory(self, root):
            if root == cfg.source_folder_id:
                yield {"id": "fid_key_hold", "name": "Artist - KeyHold.flac", "parents": [root], "scanned_parent": root, "md5Checksum": "v1"}
                yield {"id": "fid_bad_lock", "name": "Artist - BadLock.flac", "parents": [root], "scanned_parent": root, "md5Checksum": "v2"}
            else:
                return
                yield
        def get_children_folders(self, root):
            return {"Hardstyle": ["HS_ID"]}

    class HoldSheet:
        def fetch_header_row(self, *a):
            return [
                "Original Filename", "Source Folder", "Genre", "Subgenre", "BPM (full)", "Camelot Key",
                "Energy /10", "Filth /10", "Format", "Size (MB)", "Proposed Filename", "Processing Status",
                "Analysis Confidence", "Drive File URL", "Drive File ID", "Notes / Override",
                "Review Star", "Review Reasons", "BPM State", "Key State", "Genre State", "Subgenre State", "Metadata Lock JSON",
            ]
        def iter_tracks_rows(self, *a):
            yield 2, _make_tracks_row(
                genre="Hardstyle", subgenre="Rawstyle", bpm="155", key="",
                anal_conf="BPM high; key conflicts", file_id="fid_key_hold",
            )
            bad = _make_tracks_row(genre="", subgenre="", bpm="", key="", file_id="fid_bad_lock") + [""] * 6 + ["{corrupt_json"]
            yield 3, bad

    drive = HoldDrive()
    scan_root(db, drive, cfg.source_folder_id, "source")
    scan_root(db, drive, cfg.vault_folder_id, "destination")
    for fid, ver, title in (("fid_key_hold", "v1", "KeyHold"), ("fid_bad_lock", "v2", "BadLock")):
        db.update_hydration_result(
            fid, "success", ver, method="embedded_ranged",
            metadata=TrackMetadata(version=ver, artist="Artist", title=title, duration=200.0, identity_origin="embedded"),
        )
        db.update_research_result(
            fid, "success", ver,
            candidates=[
                Candidate("MusicBrainz", title, ["Artist"], duration=200.0, bpm=155.0, key="8A", genre=["Hardstyle", "Rawstyle"], verification="ISRC supported", source_family="musicbrainz"),
                Candidate("Discogs release", title, ["Artist"], duration=200.0, bpm=155.0, key="8A", genre=["Hardstyle", "Rawstyle"], verification="Duration supported", source_family="discogs"),
            ],
            provider_calls={"_meta": {"lookup_complete": True}, "MusicBrainz": {"request_made": True}},
            research_incomplete=False,
        )

    exp = tmp_path / "hold_cov.json"
    with closing(db._get_connection()) as conn:
        library_report(
            conn, db, cfg,
            Namespace(scope="all", file_id=[], verbose=True, export_json=str(exp), export_csv=""),
            HoldSheet(), drive,
        )
    by_id = {r["file_id"]: r for r in json.loads(exp.read_text())["rows"]}
    kh = by_id["fid_key_hold"]
    assert kh["fields"]["key"]["proposal_action"] == "human_review_hold"
    assert kh["fields"]["key"]["state"] == "conflicting"
    assert kh["states"]["metadata_complete"] is False
    assert kh["states"]["review_required"] is True

    bl = by_id["fid_bad_lock"]
    assert bl["fields"]["genre"]["proposal_action"] == "lock_error_hold"
    assert bl["fields"]["genre"]["proposed"] == ""
    assert bl["routing"] == "sheet_mapping_hold"
