"""
Stage 3 tests: seekable DriveRangeReader (HTTP 206, 200 Range-ignored abort, 416, short reads,
retries, mid-read version change, per-file & global byte budget), multi-format Mutagen tag extraction
(WAV trailing ID3 + RIFF INFO, AIFF ID3, FLAC Vorbis, MP3 ID3, tagless, unsupported),
safe filename stem parsing across all real observed names, opt-in temp download cleanup,
and field preservation.
"""
import io
import struct
import wave
from pathlib import Path
from typing import Dict, List, Optional
import pytest

from mutagen.aiff import AIFF
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TBPM, TCON, TIT2, TKEY, TPE1, TSRC, TXXX
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.wave import WAVE

from musicvault.core.audio import extract_embedded_tags_from_stream, extract_metadata_from_file
from musicvault.core.hydrator import hydrate_live_inventory
from musicvault.core.matcher import (
    normalize_bpm_str,
    normalize_key_to_camelot,
    parse_filename_identity,
    verify_recording_candidate,
)
from musicvault.db import Database
from musicvault.models import TrackMetadata
from musicvault.services.range_reader import (
    BudgetExceededError,
    ByteBudgetTracker,
    DriveRangeReader,
    PerFileRangeCapError,
    RangeFetchResult,
    RangeIgnoredError,
    RemoteVersionChangedError,
)


def _make_wav_with_trailing_id3(pcm_seconds: int = 2, rate: int = 8000) -> bytes:
    """Create a real WAV file with PCM audio followed by trailing ID3v2 tags at the end of the file."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * (rate * pcm_seconds))
    buf.seek(0)
    wf = WAVE(buf)
    wf.add_tags()
    wf.tags.add(TPE1(encoding=3, text=["Act of Rage"]))
    wf.tags.add(TIT2(encoding=3, text=["Living Fast"]))
    wf.tags.add(TBPM(encoding=3, text=["157.9"]))
    wf.tags.add(TKEY(encoding=3, text=["A minor"]))
    wf.tags.add(TSRC(encoding=3, text=["NL-FL7-24-01034"]))
    wf.tags.add(TCON(encoding=3, text=["Hardstyle"]))
    wf.save(buf)
    return buf.getvalue()


def _make_wav_with_riff_info_only() -> bytes:
    """Create a WAV file carrying RIFF LIST/INFO metadata (IART, INAM, IGNR) after PCM data."""
    pcm = b"\x00\x00" * 8000  # 1s mono 8kHz 16-bit
    fmt_chunk = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16)
    data_chunk = b"data" + struct.pack("<I", len(pcm)) + pcm

    def _info_sub(code: bytes, text: str) -> bytes:
        raw = text.encode("utf-8") + b"\x00"
        pad = b"\x00" if (len(raw) % 2) else b""
        return code + struct.pack("<I", len(raw)) + raw + pad

    info_body = b"INFO" + _info_sub(b"IART", "Wasted Penguinz") + _info_sub(b"INAM", "Another High") + _info_sub(b"IGNR", "Hardstyle")
    list_chunk = b"LIST" + struct.pack("<I", len(info_body)) + info_body
    riff_body = b"WAVE" + fmt_chunk + data_chunk + list_chunk
    return b"RIFF" + struct.pack("<I", len(riff_body)) + riff_body


def _make_aiff_with_id3() -> bytes:
    """Create a real FORM/AIFF file with ID3 tags (compatible with Python 3.13 where stdlib aifc is removed)."""
    # 80-bit IEEE 754 extended float for 8000.0 Hz: exponent 0x400B, mantissa 0xFA00000000000000
    sr_80bit = b"\x40\x0b\xfa\x00\x00\x00\x00\x00\x00\x00"
    num_frames = 8000
    pcm = b"\x00\x00" * num_frames
    comm_data = struct.pack(">hIh", 1, num_frames, 16) + sr_80bit
    comm_chunk = b"COMM" + struct.pack(">I", len(comm_data)) + comm_data
    ssnd_data = struct.pack(">II", 0, 0) + pcm
    ssnd_chunk = b"SSND" + struct.pack(">I", len(ssnd_data)) + ssnd_data
    form_body = b"AIFF" + comm_chunk + ssnd_chunk
    raw_aiff = b"FORM" + struct.pack(">I", len(form_body)) + form_body

    buf = io.BytesIO(raw_aiff)
    aiff_file = AIFF(buf)
    aiff_file.add_tags()
    aiff_file.tags.add(TPE1(encoding=3, text=["Atmozfears & David Spekter"]))
    aiff_file.tags.add(TIT2(encoding=3, text=["Release (10 Year Anniversary Remix)"]))
    aiff_file.tags.add(TXXX(encoding=3, desc="INITIALKEY", text=["5B"]))
    aiff_file.save(buf)
    return buf.getvalue()


def _make_minimal_flac_with_vorbis(tmp_path: Path) -> bytes:
    """Build a minimal valid FLAC byte payload with STREAMINFO + VORBIS_COMMENT."""
    # STREAMINFO block (type 0, last=1 initially, length 34): 44100 Hz, 1ch, 16bps, 44100 samples (1.0s)
    sr = 44100
    channels_minus_1 = 0
    bps_minus_1 = 15
    total_samples = 44100
    packed_64 = (
        (sr << 44)
        | (channels_minus_1 << 41)
        | (bps_minus_1 << 36)
        | total_samples
    )
    streaminfo_body = struct.pack(">HH3s3sQ16s", 4096, 4096, b"\x00\x00\x00", b"\x00\x00\x00", packed_64, b"\x00" * 16)
    raw_flac = b"fLaC" + bytes([0x80 | 0]) + struct.pack(">I", 34)[1:] + streaminfo_body
    p = tmp_path / "sample.flac"
    p.write_bytes(raw_flac)
    fl = FLAC(p)
    fl["artist"] = "Rebelion"
    fl["title"] = "The Loudest City"
    fl["isrc"] = "NLH2L2600182"
    fl["bpm"] = "154"
    fl["initialkey"] = "2A"
    fl["genre"] = "Hardstyle"
    fl.save()
    return p.read_bytes()


def _make_minimal_mp3_with_id3(tmp_path: Path) -> bytes:
    """Create a valid MPEG1 Layer3 frame sequence with ID3v2 tags."""
    # Valid MPEG1 Layer3 128kbps 44100Hz stereo frame header: 0xFFFB9064, frame size = 417 bytes
    frame = b"\xff\xfb\x90\x64" + (b"\x00" * 413)
    p = tmp_path / "sample.mp3"
    p.write_bytes(frame * 80)
    tags = ID3()
    tags.add(TPE1(encoding=3, text=["Dexperia"]))
    tags.add(TIT2(encoding=3, text=["Amen"]))
    tags.add(TBPM(encoding=3, text=["180"]))
    tags.add(TKEY(encoding=3, text=["F# major"]))
    tags.add(TSRC(encoding=3, text=["QT6HK2614417"]))
    tags.save(p)
    return p.read_bytes()


class MockRemoteStore:
    """Simulates Google Drive HTTP Range endpoint without any real network calls."""

    def __init__(self, payload: bytes, etag: str = '"v1"'):
        self.payload = payload
        self.etag = etag
        self.calls: List[tuple] = []
        self.ignore_range = False
        self.fail_first_n = 0
        self.short_read_first = False
        self.mutate_etag_on_call: Optional[int] = None

    def fetch(self, start: int, end: int, max_allowed_bytes: int) -> RangeFetchResult:
        self.calls.append((start, end))
        call_num = len(self.calls)
        if self.mutate_etag_on_call and call_num >= self.mutate_etag_on_call:
            self.etag = '"v2_changed"'

        if self.ignore_range:
            raise RangeIgnoredError("Server returned HTTP 200 OK instead of 206 Partial Content; aborted stream")

        if self.fail_first_n > 0:
            self.fail_first_n -= 1
            raise OSError("Simulated transient mobile socket reset")

        total = len(self.payload)
        if start >= total:
            return RangeFetchResult(
                status_code=416,
                headers={"content-range": f"bytes */{total}", "etag": self.etag},
                data=b"",
                bytes_transferred=0,
            )

        actual_end = min(end, total - 1)
        chunk = self.payload[start : actual_end + 1]
        if self.short_read_first:
            self.short_read_first = False
            short = chunk[: max(1, len(chunk) // 2)]
            return RangeFetchResult(
                status_code=206,
                headers={"content-range": f"bytes {start}-{actual_end}/{total}", "etag": self.etag},
                data=short,
                bytes_transferred=len(short),
            )

        if len(chunk) > max_allowed_bytes:
            raise PerFileRangeCapError("Per-file cap exceeded")

        return RangeFetchResult(
            status_code=206,
            headers={"content-range": f"bytes {start}-{actual_end}/{total}", "etag": self.etag},
            data=chunk,
            bytes_transferred=len(chunk),
        )


def test_wav_trailing_id3_over_ranged_reader_skips_middle_pcm():
    # Create a ~5 MB WAV where ID3v2 tags sit AFTER the 5 MB PCM chunk!
    wav_bytes = _make_wav_with_trailing_id3(pcm_seconds=320, rate=8000)  # ~5.12 MB
    assert len(wav_bytes) > 5_000_000

    store = MockRemoteStore(wav_bytes)
    reader = DriveRangeReader(store.fetch, known_size=len(wav_bytes), block_size=65536, max_file_bytes=512 * 1024)
    buf_reader = io.BufferedReader(reader, buffer_size=65536)

    meta, ok = extract_embedded_tags_from_stream(buf_reader, filename="Living Fast.wav", version_id="md5_wav")
    assert ok is True
    assert meta.artist == "Act of Rage"
    assert meta.title == "Living Fast"
    assert meta.raw_key == "A minor"
    assert meta.embedded_key == "8A"  # Normalized Camelot while raw_key preserved!
    assert meta.embedded_bpm == "157.9"
    assert meta.isrc == "NLFL72401034"
    assert meta.raw_isrc == "NL-FL7-24-01034"
    assert meta.embedded_genre == "Hardstyle"
    assert meta.duration == pytest.approx(320.0, abs=0.5)
    # Transferred ONLY head + tail blocks (< 200 KiB out of > 5 MB)!
    assert reader.bytes_transferred < 200_000


def test_wav_riff_info_aiff_flac_and_mp3_tag_mappings(tmp_path: Path):
    # 1. WAV RIFF LIST-INFO
    riff_bytes = _make_wav_with_riff_info_only()
    m_riff, ok_riff = extract_embedded_tags_from_stream(io.BytesIO(riff_bytes), "Another High.wav")
    assert ok_riff is True
    assert m_riff.artist == "Wasted Penguinz"
    assert m_riff.title == "Another High"
    assert m_riff.embedded_genre == "Hardstyle"

    # 2. AIFF ID3 + TXXX:INITIALKEY
    aiff_bytes = _make_aiff_with_id3()
    m_aiff, ok_aiff = extract_embedded_tags_from_stream(io.BytesIO(aiff_bytes), "Release.aiff")
    assert ok_aiff is True
    assert m_aiff.artist == "Atmozfears & David Spekter"
    assert m_aiff.title == "Release (10 Year Anniversary Remix)"
    assert m_aiff.embedded_key == "5B"

    # 3. FLAC VorbisComments
    flac_bytes = _make_minimal_flac_with_vorbis(tmp_path)
    m_flac, ok_flac = extract_embedded_tags_from_stream(io.BytesIO(flac_bytes), "Loudest.flac")
    assert ok_flac is True
    assert m_flac.artist == "Rebelion"
    assert m_flac.title == "The Loudest City"
    assert m_flac.isrc == "NLH2L2600182"
    assert m_flac.embedded_bpm == "154"
    assert m_flac.embedded_key == "2A"

    # 4. MP3 ID3 + musical key normalization ('F# major' -> '2B')
    mp3_bytes = _make_minimal_mp3_with_id3(tmp_path)
    m_mp3, ok_mp3 = extract_embedded_tags_from_stream(io.BytesIO(mp3_bytes), "Amen.mp3")
    assert ok_mp3 is True
    assert m_mp3.artist == "Dexperia"
    assert m_mp3.title == "Amen"
    assert m_mp3.raw_key == "F# major"
    assert m_mp3.embedded_key == "2B"
    assert m_mp3.isrc == "QT6HK2614417"


def test_parse_filename_identity_handles_all_real_user_examples():
    # Malformed / empty names
    assert parse_filename_identity("-").confidence == "unresolved"
    assert parse_filename_identity("-.wav").confidence == "unresolved"

    # Track-numbered title-only names
    for raw_name, expected_title in [
        ("01 #YEET.wav", "#YEET"),
        ("01 4AM.wav", "4AM"),
        ("01 Alien.wav", "Alien"),
        ("01 Back To You.wav", "Back To You"),
        ("01 Burning (feat. Drean).wav", "Burning (feat. Drean)"),
        ("01 Clarity.wav", "Clarity"),
    ]:
        parsed = parse_filename_identity(raw_name)
        assert parsed.artist == "", f"Must not invent artist for {raw_name}"
        assert parsed.title_hint == expected_title
        assert parsed.confidence == "title_only"

    # Remix-qualifier names MUST NEVER assign remixer as original primary artist!
    ds = parse_filename_identity("01 Beneath The Lights - Darren Styles Remix.wav")
    assert ds.artist == ""
    assert ds.remixer_hint == "Darren Styles"
    assert "Beneath The Lights" in ds.title_hint
    assert ds.confidence == "title_only"

    mr = parse_filename_identity("01 Can You Hear Me - Mike Reverie Remix.wav")
    assert mr.artist == ""
    assert mr.remixer_hint == "Mike Reverie"
    assert "Can You Hear Me" in mr.title_hint
    assert mr.confidence == "title_only"

    # Ambiguous two-part filename preserves both orientations
    amb = parse_filename_identity("Believe - Jesse Bloch, Coby Watts.wav")
    assert amb.confidence == "filename_ambiguous"
    assert amb.artist == "Believe" and amb.title == "Jesse Bloch, Coby Watts"
    assert amb.alt_artist == "Jesse Bloch, Coby Watts" and amb.alt_title == "Believe"


def test_range_reader_retries_short_reads_416_200_abort_and_version_change():
    payload = b"ABCD" * 25000  # 100 KB

    # 1. Transient failure + short read recovers cleanly via retry
    store_retry = MockRemoteStore(payload)
    store_retry.fail_first_n = 1
    store_retry.short_read_first = True
    r1 = DriveRangeReader(store_retry.fetch, known_size=len(payload), block_size=16384, max_retries=2, retry_delay=0.0)
    assert r1.read(16) == payload[:16]
    # Seek beyond EOF returns b"" without error
    r1.seek(len(payload) + 500)
    assert r1.read(32) == b""

    # 2. HTTP 200 Range ignored aborts immediately
    store_200 = MockRemoteStore(payload)
    store_200.ignore_range = True
    r2 = DriveRangeReader(store_200.fetch, known_size=len(payload), block_size=16384)
    with pytest.raises(RangeIgnoredError):
        r2.read(64)

    # 3. Remote ETag/version changes between block 0 and block 1 -> raises RemoteVersionChangedError
    store_ver = MockRemoteStore(payload)
    store_ver.mutate_etag_on_call = 2
    r3 = DriveRangeReader(store_ver.fetch, known_size=len(payload), block_size=16384)
    assert len(r3.read(16)) == 16
    r3.seek(32768)
    with pytest.raises(RemoteVersionChangedError):
        r3.read(16)


def test_hydrator_budget_exhaustion_sheet_provenance_and_opt_in_temp_cleanup(tmp_path: Path):
    db = Database(tmp_path / "hydrate.db")
    s1 = db.begin_scan_run("SRC_1")
    wav_tagged = _make_wav_with_trailing_id3(pcm_seconds=2, rate=8000)
    # Create a tagless WAV
    buf_tagless = io.BytesIO()
    with wave.open(buf_tagless, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(b"\x00\x00" * 8000)
    wav_tagless = buf_tagless.getvalue()

    files_by_id: Dict[str, bytes] = {
        "F_TAGGED": wav_tagged,
        "F_SHEET": wav_tagless,
        "F_TITLE_ONLY": wav_tagless,
        "F_CORRUPT": b"NOT_AN_AUDIO_FILE_AT_ALL" * 100,
        "F_ZZZ_OVER_BUDGET": wav_tagged * 20,
    }
    names_by_id = {
        "F_TAGGED": "01 Unknown Name.wav",
        "F_SHEET": "01 Track In Sheet.wav",
        "F_TITLE_ONLY": "01 Clarity.wav",
        "F_CORRUPT": "broken.wav",
        "F_ZZZ_OVER_BUDGET": "huge.wav",
    }
    for fid, data in files_by_id.items():
        db.reconcile_scanned_file(
            s1, "SRC_1",
            {"id": fid, "name": names_by_id[fid], "size": str(len(data)), "md5Checksum": f"md5_{fid}", "scanned_parent": "SRC_1"},
        )
    db.complete_scan_run(s1, "SRC_1", len(files_by_id))

    class FakeDriveForHydration:
        def open_range_reader(self, file_id: str, budget: ByteBudgetTracker, known_size: int = 0, expected_md5: str = "", max_file_bytes: int = 2097152):
            payload = files_by_id[file_id]
            store = MockRemoteStore(payload)

            def _fetch(start: int, end: int, max_cap: int) -> RangeFetchResult:
                res = store.fetch(start, end, max_cap)
                budget.record(res.bytes_transferred)
                return res

            return io.BufferedReader(
                DriveRangeReader(_fetch, known_size=known_size, block_size=16384, max_file_bytes=max_file_bytes),
                buffer_size=16384,
            )

    summary = hydrate_live_inventory(
        db=db,
        drive_service=FakeDriveForHydration(),
        limit=10,
        max_mib=0.10,  # Tight 102.4 KiB budget so 5th file triggers budget exhaustion pause!
        allow_full_download=False,
        sheet_identities={"F_SHEET": ("Verified Sheet Artist", "Verified Sheet Title")},
    )

    t_tagged = db.get_track("F_TAGGED")
    assert t_tagged is not None
    assert t_tagged.metadata.artist == "Act of Rage"
    assert t_tagged.metadata.identity_origin == "embedded"
    assert t_tagged.metadata.identity_confidence == "verified_embedded"
    assert t_tagged.hydration_status == "success"

    # Sheet-derived identity must NEVER be labeled 'embedded'!
    t_sheet = db.get_track("F_SHEET")
    assert t_sheet is not None
    assert t_sheet.metadata.artist == "Verified Sheet Artist"
    assert t_sheet.metadata.identity_origin == "sheet_exact_id"
    assert t_sheet.metadata.identity_origin != "embedded"
    assert t_sheet.hydration_status == "partial"

    # Title-only tagless file stays unresolved without inventing an artist
    t_title = db.get_track("F_TITLE_ONLY")
    assert t_title is not None
    assert t_title.metadata.artist == ""
    assert t_title.metadata.filename_title_hint == "Clarity"
    assert t_title.metadata.identity_confidence == "title_only"
    assert t_title.hydration_status == "unresolved"

    # Unsupported data marked permanent_unsupported
    t_corrupt = db.get_track("F_CORRUPT")
    assert t_corrupt is not None
    assert t_corrupt.hydration_status == "permanent_unsupported"
    assert summary.paused_budget_exhausted is True

    # Opt-in full download recovers when Range is ignored, cleans up temp files, and preserves old fields
    db_opt = Database(tmp_path / "opt_in_dl.db")
    s_opt = db_opt.begin_scan_run("SRC_1")
    db_opt.reconcile_scanned_file(
        s_opt, "SRC_1",
        {"id": "F_200_ONLY", "name": "Song.wav", "size": str(len(wav_tagged)), "md5Checksum": "v_opt", "scanned_parent": "SRC_1"},
    )
    db_opt.complete_scan_run(s_opt, "SRC_1", 1)

    created_temp_paths: List[Path] = []

    class FakeDrive200Fallback:
        def open_range_reader(self, file_id: str, budget: ByteBudgetTracker, **kwargs):
            raise RangeIgnoredError("Server returned 200 instead of 206")

        def download_bounded_temp_file(self, file_id: str, dest_path: Path, budget: ByteBudgetTracker, **kwargs) -> int:
            created_temp_paths.append(dest_path)
            budget.record(len(wav_tagged))
            dest_path.write_bytes(wav_tagged)
            return len(wav_tagged)

    sum_opt = hydrate_live_inventory(
        db=db_opt,
        drive_service=FakeDrive200Fallback(),
        limit=5,
        max_mib=5.0,
        allow_full_download=True,
    )
    assert sum_opt.hydrated_embedded == 1
    assert len(created_temp_paths) == 1
    assert not created_temp_paths[0].exists()  # Temporary media file cleaned up!
    t_opt = db_opt.get_track("F_200_ONLY")
    assert t_opt is not None and t_opt.metadata.artist == "Act of Rage"

