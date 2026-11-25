"""
Local audio metadata extraction and analysis via Mutagen.
"""
import re
from pathlib import Path
from typing import Optional, Dict, Any
import mutagen
from mutagen.easyid3 import EasyID3

from ..models import TrackMetadata
from .matcher import clean_filename, parse_artist_title_from_stem


def extract_metadata_from_file(file_path: Path, version_id: str = "") -> TrackMetadata:
    """
    Extract tags and duration from local audio file using Mutagen.
    Supports MP3, FLAC, WAV, AIFF, M4A, OGG, OPUS, etc.
    """
    if not file_path.exists():
        raise FileNotFoundError(f"Audio file does not exist: {file_path}")

    stem = file_path.stem
    audio = None
    try:
        audio = mutagen.File(file_path, easy=True)
    except Exception:
        pass

    # If easy=True fails, try default mutagen
    if audio is None:
        try:
            audio = mutagen.File(file_path)
        except Exception:
            pass

    duration = 0.0
    if audio is not None and getattr(audio, 'info', None):
        duration = round(float(getattr(audio.info, 'length', 0.0) or 0.0), 2)

    def get_tag(tag_name: str) -> str:
        if audio is None:
            return ""
        val = audio.get(tag_name)
        if isinstance(val, (list, tuple)) and val:
            return str(val[0]).strip()
        elif isinstance(val, str):
            return val.strip()
        return ""

    artist = get_tag('artist')
    title = get_tag('title')
    isrc = get_tag('isrc')
    bpm = get_tag('bpm')
    key = get_tag('initialkey')
    genre = get_tag('genre')
    origin = "embedded"

    # Fallback to filename if tags are absent
    if not artist or not title:
        f_artist, f_title = parse_artist_title_from_stem(stem)
        artist = artist or f_artist
        title = title or f_title
        origin = "filename fallback"

    return TrackMetadata(
        version=version_id,
        artist=artist,
        title=title,
        duration=duration,
        isrc=isrc,
        embedded_bpm=bpm,
        embedded_key=key,
        embedded_genre=genre,
        identity_origin=origin
    )
