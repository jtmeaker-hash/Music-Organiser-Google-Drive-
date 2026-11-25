"""
Command implementations for Music Vault Manager.
"""
import sys
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any

from ..config import Config
from ..db import Database
from ..termux import termux_wake_lock
from ..models import Track, TrackMetadata, Candidate
from ..core.rules import planned_row, EXTRA_HEADERS, FIELDS, COL
from ..core.organizer import build_move_plan, create_rollback_manifest
from ..core.genre import classify_track_genres, decide_genre, canonical_genre
from ..core.matcher import norm, verify_candidate
from ..providers.sonovault import SonoVaultProvider
from ..providers.audiometa import AudioMetaProvider
from ..providers.discogs import DiscogsProvider
from ..providers.musicbrainz import MusicBrainzProvider
from ..providers.research import ResearchProvider
from ..services.drive import DriveService
from ..services.sheets import SheetsService
from ..auth import GoogleAuthManager

logger = logging.getLogger("musicvault.commands")


def run_benchmark(config: Config, db: Database, sample_size: int = 10,
                  checkpoint_path: Optional[Path] = None) -> None:
    """
    Execute benchmark regression test.
    Reproduces deterministic v1.3 / v1.4 decision rules against benchmark tracks.
    """
    print(f"\n=== RUNNING BENCHMARK REGRESSION TEST ({sample_size} TRACKS) ===")

    # Import checkpoint if provided or available
    if checkpoint_path and checkpoint_path.exists():
        imported = db.import_json_checkpoint(checkpoint_path)
        print(f"Imported {imported} tracks from {checkpoint_path.name}")
    elif sample_size == 10:
        cand_paths = [
            Path("/mnt/sdcard/Download/v07_local_checkpoint.json"),
            Path("reference/v07_local_checkpoint.json"),
            Path("/root/music_vault_source_zip/v07_local_checkpoint.json")
        ]
        for p in cand_paths:
            if p.exists():
                db.import_json_checkpoint(p)
                break
    elif sample_size >= 50:
        cand_paths = [
            Path("/mnt/sdcard/Download/v06_local_checkpoint.json"),
            Path("reference/v06_local_checkpoint.json")
        ]
        for p in cand_paths:
            if p.exists():
                db.import_json_checkpoint(p)
                break

    tracks = db.get_all_tracks()
    if not tracks:
        print("[!] No tracks found in database or local checkpoint. Run a scan or specify checkpoint path.")
        return

    # Benchmark control ordering
    CONTROL_IDS = [
        '1heVoqsB5HpgGHCNAU0liJDrhrIFCWytX',
        '1ehnUyjzwrwjeEeUv1nLymSk5LXUUDYmW',
        '1v3jdlhhx3hqfY6ivYBut3DcJxKsu6gSL',
        '1NJxM8N3WyJMS7-KEhDriERqlJYwk8YLf',
        '1N74Lus5IHsDXslNA3iB3eC9EbuoDt-zB',
        '1E50C7UI78bE6hwRpIDOuHciWqP-fYBiK',
        '1VpA4yDXwkykR7YzN9hWFZZS-TMK3ps18',
        '1IikiHEWOKy_Su_9NHSx5EKdKQyoTOrt-',
        '1QuI-Y-pNGDxob7Ez_q4mN6aNtDCPOiks',
        '1GSmBaNJj9SR80Kw_9XtByPKyUKUg5F1C'
    ]

    selected: List[Track] = []
    # If 10-track benchmark, prioritize exact control IDs
    if sample_size == 10:
        for cid in CONTROL_IDS:
            hit = next((t for t in tracks if t.file_id == cid), None)
            if hit and hit not in selected:
                selected.append(hit)

    if len(selected) < sample_size:
        controls = [
            'believe', 'release (10 year anniversary remix)', 'living fast',
            'big city life', 'amen', 'another high', 'the loudest city',
            'young, wild & free', "you're not alone", 'adjuzt'
        ]
        for term in controls:
            hits = [t for t in tracks if norm(term) in norm(t.name) and t not in selected]
            if hits:
                selected.append(hits[0])

        selected = (selected + [t for t in tracks if t not in selected])[:sample_size]
    print(f"Loaded {len(selected)} tracks for benchmark.")

    # Setup mock / offline reference rows (representing existing sheet)
    # Row mapping: row_num, track, BPM, Key, Genre, Subgenre
    mock_sheet_rows: Dict[str, Tuple[int, List[str]]] = {
        '1heVoqsB5HpgGHCNAU0liJDrhrIFCWytX': (2, ['', '', '', '', '147.0', '', '', '', '', '', '', '', '', '', '1heVoqsB5HpgGHCNAU0liJDrhrIFCWytX']),
        '1ehnUyjzwrwjeEeUv1nLymSk5LXUUDYmW': (2169, ['', '', 'Hardstyle', 'Euphoric', '', '', '', '', '', '', '', '', '', '', '1ehnUyjzwrwjeEeUv1nLymSk5LXUUDYmW']),
        '1v3jdlhhx3hqfY6ivYBut3DcJxKsu6gSL': (7, ['', '', 'Hardstyle', 'Rawstyle', '158.0', '11A', '', '', '', '', '', '', '', '', '1v3jdlhhx3hqfY6ivYBut3DcJxKsu6gSL']),
        '1NJxM8N3WyJMS7-KEhDriERqlJYwk8YLf': (23, ['', '', 'Drum & Bass', '', '174.0', '5B', '', '', '', '', '', '', '', '', '1NJxM8N3WyJMS7-KEhDriERqlJYwk8YLf']),
        '1N74Lus5IHsDXslNA3iB3eC9EbuoDt-zB': (159, ['', '', 'Drum & Bass', '', '180.0', '', '', '', '', '', '', '', '', '', '1N74Lus5IHsDXslNA3iB3eC9EbuoDt-zB']),
        '1E50C7UI78bE6hwRpIDOuHciWqP-fYBiK': (355, ['', '', 'Hardstyle', 'Euphoric', '146.0', '5B', '', '', '', '', '', '', '', '', '1E50C7UI78bE6hwRpIDOuHciWqP-fYBiK']),
        '1VpA4yDXwkykR7YzN9hWFZZS-TMK3ps18': (1914, ['', '', 'Hardstyle', 'Rawstyle', '154.0', '2A', '', '', '', '', '', '', '', '', '1VpA4yDXwkykR7YzN9hWFZZS-TMK3ps18']),
        '1IikiHEWOKy_Su_9NHSx5EKdKQyoTOrt-': (1040, ['', '', '', '', '', '', '', '', '', '', '', '', '', '', '1IikiHEWOKy_Su_9NHSx5EKdKQyoTOrt-']),
        '1QuI-Y-pNGDxob7Ez_q4mN6aNtDCPOiks': (2181, ['', '', 'Hardstyle', '', '', '', '', '', '', '', '', '', '', '', '1QuI-Y-pNGDxob7Ez_q4mN6aNtDCPOiks']),
        '1GSmBaNJj9SR80Kw_9XtByPKyUKUg5F1C': (1204, ['', '', 'Hardstyle', 'Xtra Raw', '', '', '', '', '', '', '', '', '', '', '1GSmBaNJj9SR80Kw_9XtByPKyUKUg5F1C'])
    }

    report_rows = []
    move_preview_rows = []

    for t in selected:
        cs = db.get_candidates(t.file_id)
        # Classify
        g_info = classify_track_genres(t.metadata, cs)
        proposal_dict = {
            'bpm': t.metadata.embedded_bpm,
            'key': t.metadata.embedded_key,
            'genre': g_info['genre'],
            'subgenre': g_info['subgenre']
        }

        row_num, old_vals = mock_sheet_rows.get(t.file_id, (999, ['', '', '', '', '', '', '', '', '', '', '', '', '', '', t.file_id]))
        new_row, decisions, changed = planned_row(
            candidates=cs,
            proposal_row=proposal_dict,
            old_row=old_vals,
            save_medium=config.save_medium,
            provider_terms_cleared=config.provider_terms_cleared
        )

        report_rows.append({
            'row': row_num,
            'track': t.name,
            'BPM': decisions.get('bpm', ''),
            'Key': decisions.get('key', ''),
            'Genre': decisions.get('genre', ''),
            'Subgenre': decisions.get('subgenre', ''),
            'star': new_row[16],
            'would_fill': ','.join(changed)
        })

        move_item = build_move_plan(
            file_id=t.file_id,
            track_name=t.name,
            row_num=row_num,
            sheet_row=old_vals,
            allow_provisional=config.allow_provisional_moves
        )
        move_preview_rows.append(move_item)

    # Print Report
    print("\n=== COPY THIS REPORT — v1.3-provisional-routing-10 ===")
    print(f"{'row':<6} {'track':<40} {'BPM':<15} {'Key':<15} {'Genre':<20} {'Subgenre':<20} {'star':<5} {'would_fill'}")
    print("-" * 135)
    for r in report_rows:
        t_display = r['track'][:38]
        print(f"{r['row']:<6} {t_display:<40} {r['BPM']:<15} {r['Key']:<15} {r['Genre']:<20} {r['Subgenre']:<20} {r['star']:<5} {r['would_fill']}")

    print(f"\nDry-run: {config.dry_run} Provider terms cleared: {config.provider_terms_cleared} Proposed row updates: {len(report_rows)}")
    print("No audio modifications are supported by this application.")

    print("\n=== MOVE PREVIEW — v1.3 ===")
    print(f"{'row':<6} {'file_id':<35} {'track':<35} {'destination':<15} {'reason':<25} {'routing_confidence':<20} {'eligible':<10} {'move_authorized'}")
    print("-" * 165)
    for m in move_preview_rows:
        t_disp = m.track_name[:33]
        print(f"{m.row_num or 0:<6} {m.file_id:<35} {t_disp:<35} {m.destination:<15} {m.reason:<25} {m.routing_confidence.value:<20} {str(m.eligible):<10} {str(m.move_authorized)}")

    eligible_count = sum(1 for m in move_preview_rows if m.eligible)
    auth_count = sum(1 for m in move_preview_rows if m.move_authorized)
    print(f"Eligible: {eligible_count} of {len(move_preview_rows)} | move-authorized: {auth_count} | DRY_RUN: {config.dry_run}\n")


def run_scan(config: Config, db: Database) -> None:
    """Scan Google Drive source folder, extract audio tags, and save checkpoints."""
    auth_mgr = GoogleAuthManager(config)
    creds = auth_mgr.get_credentials()
    if not creds:
        print("[✗] Google authentication is required to scan Drive.")
        return

    drive = DriveService(creds)
    source_id = config.source_folder_id

    print(f"\nStarting Drive inventory scan on folder: {source_id}...")
    with termux_wake_lock():
        count = 0
        for item in drive.scan_inventory(source_id):
            file_id = item['id']
            name = item.get('name', '')
            size = int(item.get('size', 0) or 0)
            md5 = item.get('md5Checksum', '')
            mod = item.get('modifiedTime', '')
            parent = item.get('scanned_parent', '')

            # Check if already up-to-date in SQLite
            existing = db.get_track(file_id)
            if existing and existing.metadata.version == (md5 or mod):
                count += 1
                continue

            track = Track(
                file_id=file_id,
                name=name,
                size=size,
                md5=md5,
                modified_time=mod,
                parent_id=parent,
                metadata=TrackMetadata(version=md5 or mod)
            )
            db.save_track(track)
            count += 1
            if count % 10 == 0:
                print(f"  Scanned {count} tracks...")

    print(f"[✓] Inventory scan completed. {count} tracks registered in local database.")


def run_research(config: Config, db: Database, limit: int = 10) -> None:
    """Run metadata research across active providers for unverified tracks."""
    tracks = db.get_all_tracks()[:limit]
    if not tracks:
        print("[!] No tracks in database to research. Run scan first.")
        return

    sono = SonoVaultProvider(api_key=config.sonovault_api_key, contact=config.contact)
    am = AudioMetaProvider(api_key=config.audiometa_api_key, contact=config.contact)
    dc = DiscogsProvider(token=config.discogs_token, contact=config.contact)
    mb = MusicBrainzProvider(contact=config.contact)
    researcher = ResearchProvider(contact=config.contact)

    print(f"\nStarting metadata research for {len(tracks)} tracks...")
    with termux_wake_lock():
        for t in tracks:
            print(f"Researching: {t.name}...")
            artist = t.metadata.artist
            title = t.metadata.title
            if not artist or not title:
                continue

            candidates: List[Candidate] = []

            # SonoVault
            if config.sonovault_api_key:
                try:
                    candidates.extend(sono.search(artist, title))
                except Exception as e:
                    logger.debug(f"SonoVault error: {e}")

            # AudioMeta
            if config.audiometa_api_key:
                try:
                    candidates.extend(am.search(artist, title))
                except Exception as e:
                    logger.debug(f"AudioMeta error: {e}")

            # Discogs
            if config.discogs_token:
                try:
                    candidates.extend(dc.search(artist, title))
                except Exception as e:
                    logger.debug(f"Discogs error: {e}")

            # MusicBrainz
            try:
                candidates.extend(mb.search(artist, title, t.metadata.isrc))
            except Exception as e:
                logger.debug(f"MusicBrainz error: {e}")

            # Verify each candidate against local audio file metrics
            for c in candidates:
                c.verification = verify_candidate(
                    candidate_isrc=c.isrc,
                    candidate_duration=c.duration,
                    local_isrc=t.metadata.isrc,
                    local_duration=t.metadata.duration
                )

            # Online Research fallback if uncertain
            if not candidates or all('conflict' in c.verification.lower() for c in candidates):
                hints = researcher.research_track(artist, title)
                for h in hints.get("genre_hints", []):
                    candidates.append(Candidate(
                        provider="WikidataResearch",
                        title=title,
                        artists=[artist],
                        genre=[g[0] for g in h.get("genres", [])],
                        verification="Text only"
                    ))

            db.save_candidates(t.file_id, candidates)
            print(f"  Found {len(candidates)} candidates.")

    print("[✓] Research phase complete. Candidates persisted to database.")
