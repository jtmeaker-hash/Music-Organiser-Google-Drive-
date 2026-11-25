"""
Normalization, artist/title matching, and tag verification utilities.
"""
import re
import unicodedata
from typing import List, Optional, Set, Any, Dict

# Known artist aliases
ARTIST_ALIASES = {
    'rebellion': 'rebelion',
}


def norm(s: Any) -> str:
    """Normalize a string: NFKD decomposition, casefolded, alphanumeric only."""
    if s is None:
        return ""
    text = str(s).replace('official decibel', 'decibel')
    decomposed = unicodedata.normalize('NFKD', text)
    return ''.join(c for c in decomposed.casefold() if c.isalnum())


def artist_alias(a: str) -> str:
    """Resolve known artist aliases."""
    normalized = norm(a)
    return ARTIST_ALIASES.get(normalized, normalized)


def parse_artists(artist_str: str) -> List[str]:
    """Split artist string into normalized individual artist tokens."""
    if not artist_str:
        return []
    # Split on commas, ampersands, feat/ft/featuring, x, vs, with
    tokens = re.split(r'\s*(?:,|&|\band\b|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\bvs\.?\b|\bx\b|\bwith\b)\s*', artist_str, flags=re.I)
    return [artist_alias(x) for x in tokens if norm(x)]


def match_track(query_artist: str, query_title: str, candidate_title: str, candidate_artists: List[str]) -> bool:
    """
    Match query track with candidate.
    Must match normalized title and have at least one overlapping artist.
    """
    if not query_artist or not query_title:
        return False
    if norm(query_title) != norm(candidate_title):
        return False
    
    q_artists = set(parse_artists(query_artist))
    c_artists = set()
    for item in candidate_artists:
        c_artists.update(parse_artists(item))
        
    return bool(q_artists & c_artists)


def clean_filename(filename: str) -> str:
    """Clean filename by removing [Free DL], etc., and stripping forbidden characters."""
    # Remove free download tags
    s = re.sub(r'(?i)\s*\[(?:free dl|free download)\]', '', filename)
    s = re.sub(r'(?i)\s*\((?:free dl|free download)\)', '', s)
    # Remove illegal characters for Android/FAT32 filesystems (: * ? " < > | \ /)
    s = re.sub(r'[:*?"<>|\\]', '', s)
    return s.strip()


def parse_artist_title_from_stem(stem: str) -> tuple[str, str]:
    """Parse 'Artist - Title' from filename stem."""
    cleaned = clean_filename(stem)
    parts = cleaned.split(' - ', 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", cleaned.strip()


def seconds_from_val(val: Any) -> Optional[float]:
    """Convert duration value in ms or s to seconds."""
    if val is None:
        return None
    try:
        x = float(val)
        if x > 10000:  # millisecond value
            return round(x / 1000.0, 2)
        return round(x, 2)
    except (ValueError, TypeError):
        return None


def camelot_key(pitch_class: Any, mode: Any) -> Optional[str]:
    """
    Convert Spotify/AudioMeta pitch class (0-11) and mode (0=minor, 1=major)
    into Camelot alphanumeric musical key notation (e.g. 5A, 8B).
    """
    if not isinstance(pitch_class, int) or pitch_class not in range(12) or mode not in (0, 1):
        return None
    major_wheel = [8, 3, 10, 5, 12, 7, 2, 9, 4, 11, 6, 1]
    minor_wheel = [5, 12, 7, 2, 9, 4, 11, 6, 1, 8, 3, 10]
    wheel = major_wheel if mode == 1 else minor_wheel
    return f"{wheel[pitch_class]}{'B' if mode == 1 else 'A'}"


def is_valid_camelot(key_str: Any) -> bool:
    """Validate that key string conforms to Camelot notation (1A-12B)."""
    return bool(re.fullmatch(r'(?:[1-9]|1[0-2])[AB]', str(key_str or '').strip()))


def is_valid_bpm(bpm_val: Any) -> bool:
    """Validate that BPM is a realistic musical tempo (40 to 350)."""
    try:
        v = float(bpm_val)
        return 40.0 <= v <= 350.0
    except (ValueError, TypeError):
        return False


def verify_candidate(candidate_isrc: Optional[str], candidate_duration: Optional[float],
                     local_isrc: Optional[str], local_duration: Optional[float]) -> str:
    """
    Determine verification confidence against local audio file characteristics.
    """
    l_isrc = norm(local_isrc) if local_isrc else ""
    c_isrc = norm(candidate_isrc) if candidate_isrc else ""

    if l_isrc and c_isrc and l_isrc != c_isrc:
        return "ISRC conflict"

    l_dur = float(local_duration or 0.0)
    c_dur = float(candidate_duration or 0.0)

    if l_dur > 0 and c_dur > 0:
        max_allowed_delta = max(3.0, l_dur * 0.02)
        if abs(c_dur - l_dur) > max_allowed_delta:
            return "Duration conflict"

    if l_isrc and c_isrc and l_isrc == c_isrc:
        return "ISRC supported"

    if l_dur > 0 and c_dur > 0:
        return "Duration supported"

    return "Text only"
