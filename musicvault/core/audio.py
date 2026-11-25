"""
Multi-format audio tag and duration extraction via Mutagen over seekable streams or local files.
Supports ID3v2/ID3v1 (MP3, WAV, AIFF), RIFF WAV LIST-INFO chunks, FLAC/Vorbis, Ogg/Opus, and MP4/M4A.
Preserves raw tag strings separately from normalized values and never fabricates missing fields.
"""
import io
import re
import struct
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterable, List, Optional, Tuple
import mutagen

from ..models import TrackMetadata
from ..services.range_reader import HydrationTransportError
from .matcher import (
    normalize_bpm_str,
    normalize_key_to_camelot,
    parse_artist_title_from_stem,
    parse_filename_identity,
)


def _clean_tag_str(val: Any) -> str:
    """Convert a Mutagen tag value (list, ID3 frame, MP4FreeForm, bytes, int) into clean string."""
    if val is None:
        return ""
    if hasattr(val, "genres"):
        # ID3 TCON frame resolves numeric ID3v1 genre codes cleanly via .genres
        genres = [str(g).strip() for g in (val.genres or []) if str(g).strip()]
        if genres:
            return ", ".join(genres)
    if hasattr(val, "text"):
        texts = [str(x).strip() for x in (val.text or []) if str(x).strip()]
        return texts[0] if texts else ""
    if isinstance(val, (bytes, bytearray)):
        return bytes(val).decode("utf-8", errors="ignore").strip("\x00").strip()
    if isinstance(val, (list, tuple)):
        for item in val:
            s = _clean_tag_str(item)
            if s:
                return s
        return ""
    return str(val).strip("\x00").strip()


def _lookup_tag_keys(tags: Any, candidate_keys: Iterable[str]) -> str:
    """Case-insensitive / raw-frame lookup across ID3, VorbisComments, and MP4 tags."""
    if tags is None:
        return ""
    # Direct exact lookup first
    for k in candidate_keys:
        try:
            if k in tags:
                v = _clean_tag_str(tags[k])
                if v:
                    return v
        except Exception:
            pass

    # Case-insensitive fallback (for VorbisComments, TXXX:<desc>, MP4 freeform atoms)
    try:
        lower_map: Dict[str, Any] = {str(k).casefold(): k for k in tags.keys()}
    except Exception:
        return ""

    for want in candidate_keys:
        actual_key = lower_map.get(want.casefold())
        if actual_key is not None:
            try:
                v = _clean_tag_str(tags[actual_key])
                if v:
                    return v
            except Exception:
                pass
    return ""


def _parse_riff_wav_info_chunks(stream: BinaryIO) -> Dict[str, str]:
    """
    Walk RIFF WAVE top-level chunks via bounded seeks to read LIST/INFO metadata
    ('IART', 'INAM', 'IGNR', 'ISRC') without reading audio PCM data.
    """
    info_tags: Dict[str, str] = {}
    try:
        stream.seek(0, io.SEEK_SET)
        header = stream.read(12)
        if len(header) < 12 or header[:4] not in (b"RIFF", b"RF64") or header[8:12] != b"WAVE":
            return info_tags

        riff_size = struct.unpack("<I", header[4:8])[0]
        max_end = riff_size + 8 if riff_size > 12 and riff_size != 0xFFFFFFFF else None
        offset = 12
        hops = 0

        while hops < 64:
            hops += 1
            if max_end is not None and offset + 8 > max_end:
                break
            stream.seek(offset, io.SEEK_SET)
            ch_hdr = stream.read(8)
            if len(ch_hdr) < 8:
                break
            chunk_id = ch_hdr[:4]
            chunk_size = struct.unpack("<I", ch_hdr[4:8])[0]

            if chunk_id == b"LIST" and 4 <= chunk_size <= 262144:
                list_payload = stream.read(chunk_size)
                if len(list_payload) >= 4 and list_payload[:4] == b"INFO":
                    pos = 4
                    while pos + 8 <= len(list_payload):
                        sub_id = list_payload[pos : pos + 4].decode("ascii", errors="ignore")
                        sub_sz = struct.unpack("<I", list_payload[pos + 4 : pos + 8])[0]
                        val_bytes = list_payload[pos + 8 : pos + 8 + sub_sz]
                        val_str = val_bytes.decode("utf-8", errors="replace").strip("\x00").strip()
                        if val_str and sub_id:
                            info_tags[sub_id.upper()] = val_str
                        pos += 8 + sub_sz + (sub_sz % 2)

            # Advance to next 2-byte word-aligned RIFF chunk (skipping multi-MB 'data' PCM without reading!)
            step = 8 + chunk_size + (chunk_size % 2)
            if step <= 0:
                break
            offset += step
    except HydrationTransportError:
        raise
    except Exception:
        return info_tags
    return info_tags


def _normalize_isrc(raw_isrc: str) -> str:
    """Normalize ISRC (strip hyphens/spaces, validate 12-char alphanumeric code)."""
    if not raw_isrc:
        return ""
    cleaned = re.sub(r"[\s\-]", "", raw_isrc).upper()
    if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}\d{7}", cleaned):
        return cleaned
    return raw_isrc.strip()


def extract_embedded_tags_from_stream(
    stream: BinaryIO, filename: str, version_id: str = ""
) -> Tuple[TrackMetadata, bool]:
    """
    Inspect an open seekable binary stream using Mutagen (raw + easy tags + RIFF INFO).
    Returns (TrackMetadata_with_embedded_only, container_recognized).
    Does NOT apply filename fallback so caller can layer exact-ID sheet and filename hints cleanly.
    """
    ext = Path(filename or "").suffix.lower().lstrip(".")
    stream.seek(0, io.SEEK_SET)
    audio = None
    try:
        audio = mutagen.File(stream, filename=filename, easy=False)
    except HydrationTransportError:
        raise
    except Exception:
        audio = None

    if audio is None:
        try:
            stream.seek(0, io.SEEK_SET)
            audio = mutagen.File(stream, filename=filename, easy=True)
        except HydrationTransportError:
            raise
        except Exception:
            audio = None

    if audio is None:
        return TrackMetadata(version=version_id, identity_origin="unresolved", audio_format=ext), False

    duration = 0.0
    if getattr(audio, "info", None) is not None:
        duration = round(float(getattr(audio.info, "length", 0.0) or 0.0), 2)

    tags = getattr(audio, "tags", None)
    raw_artist = _lookup_tag_keys(
        tags,
        ("artist", "TPE1", "\xa9ART", "Author", "TPE2", "albumartist", "aART"),
    )
    raw_title = _lookup_tag_keys(
        tags,
        ("title", "TIT2", "\xa9nam", "Title"),
    )
    raw_isrc = _lookup_tag_keys(
        tags,
        ("isrc", "TSRC", "TXXX:ISRC", "TXXX:isrc", "----:com.apple.iTunes:ISRC", "----:com.apple.iTunes:isrc"),
    )
    raw_bpm = _lookup_tag_keys(
        tags,
        ("bpm", "tempo", "TBPM", "TXXX:BPM", "TXXX:bpm", "TXXX:TEMPO", "tmpo", "----:com.apple.iTunes:BPM"),
    )
    raw_key = _lookup_tag_keys(
        tags,
        (
            "initialkey", "key", "TKEY", "TXXX:INITIALKEY", "TXXX:initialkey",
            "TXXX:KEY", "TXXX:key", "----:com.apple.iTunes:initialkey", "----:com.apple.iTunes:KEY",
        ),
    )
    raw_genre = _lookup_tag_keys(
        tags,
        ("genre", "TCON", "\xa9gen"),
    )

    # Check RIFF WAV LIST/INFO chunks when WAV lacks ID3 artist/title
    if ext == "wav" and (not raw_artist or not raw_title):
        riff_info = _parse_riff_wav_info_chunks(stream)
        if riff_info:
            raw_artist = raw_artist or riff_info.get("IART", "")
            raw_title = raw_title or riff_info.get("INAM", "")
            raw_genre = raw_genre or riff_info.get("IGNR", "")
            raw_isrc = raw_isrc or riff_info.get("ISRC", "")

    norm_bpm = normalize_bpm_str(raw_bpm)
    norm_key = normalize_key_to_camelot(raw_key)
    norm_isrc = _normalize_isrc(raw_isrc)

    has_full_embedded = bool(raw_artist and raw_title)
    origin = "embedded" if (raw_artist or raw_title or norm_isrc or norm_bpm or norm_key or raw_genre) else "unresolved"
    conf = "verified_embedded" if has_full_embedded else "unresolved"

    meta = TrackMetadata(
        version=version_id,
        artist=raw_artist,
        title=raw_title,
        duration=duration,
        isrc=norm_isrc,
        embedded_bpm=norm_bpm or raw_bpm,
        embedded_key=norm_key or raw_key,
        embedded_genre=raw_genre,
        identity_origin=origin,
        raw_artist=raw_artist,
        raw_title=raw_title,
        raw_bpm=raw_bpm,
        raw_key=raw_key,
        raw_genre=raw_genre,
        raw_isrc=raw_isrc,
        identity_confidence=conf,
        audio_format=type(audio).__name__,
    )
    return meta, True


def extract_metadata_from_file(file_path: Path, version_id: str = "") -> TrackMetadata:
    """
    Extract tags and duration from a local audio file using Mutagen (raw + easy tags + RIFF INFO),
    falling back to filename stem parsing when embedded artist/title tags are absent.
    """
    if not file_path.exists():
        raise FileNotFoundError(f"Audio file does not exist: {file_path}")

    with file_path.open("rb") as fh:
        meta, _ = extract_embedded_tags_from_stream(fh, filename=file_path.name, version_id=version_id)

    parsed_fn = parse_filename_identity(file_path.name)
    meta.filename_artist_hint = parsed_fn.artist
    meta.filename_title_hint = parsed_fn.title_hint or parsed_fn.title
    meta.filename_remixer_hint = parsed_fn.remixer_hint

    if not meta.artist or not meta.title:
        if parsed_fn.confidence == "filename_ambiguous" and parsed_fn.artist and parsed_fn.title:
            meta.artist = meta.artist or parsed_fn.artist
            meta.title = meta.title or parsed_fn.title
            meta.identity_origin = "filename fallback"
            meta.identity_confidence = "filename_ambiguous"
        else:
            # Preserve legacy stem fallback only when ' - ' existed and was non-empty
            f_artist, f_title = parse_artist_title_from_stem(file_path.stem)
            if parsed_fn.confidence == "title_only":
                # Do NOT assign remixer or track number as artist!
                meta.title = meta.title or parsed_fn.title_hint
                meta.identity_origin = "filename fallback"
                meta.identity_confidence = "title_only"
            elif f_artist and f_title:
                meta.artist = meta.artist or f_artist
                meta.title = meta.title or f_title
                meta.identity_origin = "filename fallback"
                meta.identity_confidence = "filename_parsed"

    return meta
