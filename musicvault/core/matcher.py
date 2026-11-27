"""
Normalization, artist/title matching, safe filename stem parsing, musical key conversion,
and conservative recording verification utilities.
"""
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Known artist aliases
ARTIST_ALIASES = {
    "rebellion": "rebelion",
}

_AUDIO_EXTS = {
    ".mp3", ".wav", ".flac", ".aiff", ".aif",
    ".m4a", ".ogg", ".opus", ".aac",
}

# Standard musical key names -> (pitch_class 0..11, mode 0=minor / 1=major)
_MUSICAL_NOTE_TO_PC: Dict[str, int] = {
    "c": 0, "b#": 0,
    "c#": 1, "db": 1,
    "d": 2,
    "d#": 3, "eb": 3,
    "e": 4, "fb": 4,
    "f": 5, "e#": 5,
    "f#": 6, "gb": 6,
    "g": 7,
    "g#": 8, "ab": 8,
    "a": 9,
    "a#": 10, "bb": 10,
    "b": 11, "cb": 11,
}

# Qualifier patterns for remixes, edits, VIPs, mixes, versions
_REMIXER_SUFFIX_RE = re.compile(
    r"^\s*(?P<remixer>.+?)\s+(?P<kind>remix|bootleg|rework|flip|refix|mashup)\s*$",
    re.IGNORECASE,
)
_PURE_VERSION_RE = re.compile(
    r"^\s*(?:(?:extended|radio|original|club|festival|vocal|instrumental|acoustic|vip|dub|"
    r"hardstyle|rawstyle|techno|hypertechno|dnb|sped\s+up|slowed(?:\s*\+\s*reverb)?|anniversary|live)"
    r"(?:\s+|/|-)*)+(?:mix|edit|version|vip|cut|remaster(?:ed)?)?\s*$",
    re.IGNORECASE,
)
_FEAT_EXTRACT_RE = re.compile(
    r"(?:\(|\[|\s)(?:feat\.?|ft\.?|featuring)\s+([^\)\]\-]+)(?:\)|\])?",
    re.IGNORECASE,
)
_PAREN_QUALIFIER_RE = re.compile(r"[\(\[]([^\)\]]+)[\)\]]")


@dataclass
class ParsedFilenameIdentity:
    """Structured result of safe filename parsing with explicit ambiguity and qualifier hints."""
    artist: str = ""
    title: str = ""
    alt_artist: str = ""
    alt_title: str = ""
    title_hint: str = ""
    remixer_hint: str = ""
    version_hint: str = ""
    featured_artists: List[str] = field(default_factory=list)
    confidence: str = "unresolved"  # 'filename_ambiguous', 'title_only', 'unresolved'


def norm(s: Any) -> str:
    """Normalize a string: NFKD decomposition, casefolded, alphanumeric only."""
    if s is None:
        return ""
    text = str(s).replace("official decibel", "decibel")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed.casefold() if c.isalnum())


def artist_alias(a: str) -> str:
    """Resolve known artist aliases."""
    normalized = norm(a)
    return ARTIST_ALIASES.get(normalized, normalized)


def parse_artists(artist_str: str) -> List[str]:
    """Split artist string into normalized individual artist tokens."""
    if not artist_str:
        return []
    tokens = re.split(
        r"\s*(?:,|;|/|\s&\s|\band\b|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\bvs\.?\b|\bx\b|\bwith\b)\s*",
        artist_str,
        flags=re.I,
    )
    return [artist_alias(x) for x in tokens if norm(x)]


def extract_version_tokens(title: str) -> Tuple[str, Set[str], str]:
    """
    Separate a track title into (normalized_base_title, normalized_version_tokens, normalized_remixer).
    Used to prevent matching a remix/VIP/edit against an unrelated edition or original mix.
    """
    if not title:
        return "", set(), ""

    work = str(title)
    version_tokens: Set[str] = set()
    remixer_norm = ""

    def _inspect_qualifier(q_text: str) -> bool:
        nonlocal remixer_norm
        q_clean = q_text.strip()
        # Ignore feat./ft. blocks when stripping version qualifiers
        if re.match(r"^(?:feat\.?|ft\.?|featuring)\b", q_clean, flags=re.I):
            return False
        m_rem = _REMIXER_SUFFIX_RE.match(q_clean)
        if m_rem:
            rem_raw = m_rem.group("remixer").strip()
            # Strip trailing length/style modifiers on remixer name (e.g. 'Darren Styles Extended Remix')
            m_mod = re.match(r"^(?P<base>.+?)\s+(?P<mod>extended|radio|club)$", rem_raw, flags=re.I)
            if m_mod and norm(m_mod.group("base")):
                mod_low = m_mod.group("mod").casefold()
                if mod_low == "extended":
                    version_tokens.add("extendedmix")
                elif mod_low in ("radio", "club"):
                    version_tokens.add("radioedit")
                rem_raw = m_mod.group("base").strip()
            if norm(rem_raw) not in ("original", "extended", "radio", "club", "vocal", "instrumental"):
                remixer_norm = artist_alias(rem_raw)
                version_tokens.add(f"remix:{remixer_norm}")
                return True
        lower_q = q_clean.casefold()
        matched_any = False
        for kw in ("remix", "vip", "bootleg", "rework", "flip", "radio edit", "extended mix",
                   "acoustic", "instrumental", "live", "sped up", "slowed"):
            if kw in lower_q:
                version_tokens.add(norm(kw))
                matched_any = True
        if re.search(r"\bextended(?:\s+(?:mix|version|cut|edit))?\b", lower_q):
            version_tokens.add("extendedmix")
            matched_any = True
        m_named_edit = re.match(r"^(?P<edit_qual>.+?)\s+edit$", q_clean, flags=re.I)
        if m_named_edit:
            eq_norm = artist_alias(m_named_edit.group("edit_qual").strip())
            if eq_norm in ("radio", "short", "club", "clean", "vocal", "original"):
                version_tokens.add("radioedit")
            elif eq_norm == "extended":
                version_tokens.add("extendedmix")
            elif eq_norm:
                version_tokens.add(f"edit:{eq_norm}")
            matched_any = True
        elif re.search(r"\b(?:radio|short|club|clean|vocal|original)\s+edit\b", lower_q) or lower_q == "edit":
            version_tokens.add("radioedit")
            matched_any = True
        if re.search(r"\boriginal(?:\s+(?:mix|version))?\b", lower_q):
            version_tokens.add("originalmix")
            matched_any = True
        return matched_any

    # Inspect parenthesized/bracketed blocks
    def _strip_if_version(match: re.Match) -> str:
        inner = match.group(1)
        if _inspect_qualifier(inner):
            return " "
        # Strip feat. from base title comparison so "Song (feat. X)" matches "Song" if X is in artists
        if re.match(r"^(?:feat\.?|ft\.?|featuring)\b", inner.strip(), flags=re.I):
            return " "
        return match.group(0)

    stripped = _PAREN_QUALIFIER_RE.sub(_strip_if_version, work)

    # Also check trailing ' - Something Remix / Edit / VIP'
    if " - " in stripped:
        left, right = stripped.rsplit(" - ", 1)
        if _inspect_qualifier(right):
            stripped = left

    return norm(stripped), version_tokens, remixer_norm


def versions_compatible(query_title: str, candidate_title: str) -> bool:
    """
    Check whether two titles represent compatible editions/mixes.
    Blocks matching an Original against a Remix/VIP/Acoustic/Sped-Up or two different remixers.
    """
    _, q_vers, q_rem = extract_version_tokens(query_title)
    _, c_vers, c_rem = extract_version_tokens(candidate_title)

    # Remixer-specific mismatch (e.g. Darren Styles Remix vs Original or Mike Reverie Remix)
    if q_rem != c_rem:
        return False

    # Hard incompatibilities (remix, named edit, vip, acoustic, instrumental, live, spedup, slowed, bootleg)
    strict_kinds = {"remix", "vip", "bootleg", "rework", "flip", "acoustic", "instrumental", "live", "spedup", "slowed"}
    q_strict = {v for v in q_vers if v in strict_kinds or v.startswith("remix:") or v.startswith("edit:")}
    c_strict = {v for v in c_vers if v in strict_kinds or v.startswith("remix:") or v.startswith("edit:")}
    if q_strict != c_strict:
        return False

    # Radio Edit vs Extended Mix conflict when both explicitly specify opposing lengths
    if ("radioedit" in q_vers and "extendedmix" in c_vers) or ("extendedmix" in q_vers and "radioedit" in c_vers):
        return False

    # Explicit Original Mix vs explicit Radio Edit or Extended Mix
    if ("originalmix" in q_vers and ("extendedmix" in c_vers or "radioedit" in c_vers)) or (
        "originalmix" in c_vers and ("extendedmix" in q_vers or "radioedit" in q_vers)
    ):
        return False

    return True


def match_track(
    query_artist: str,
    query_title: str,
    candidate_title: str,
    candidate_artists: List[str],
) -> bool:
    """
    Match query track with candidate recording.
    Requires compatible edition/remix, matching normalized title (exact or feat-stripped base),
    and at least one overlapping primary artist.
    """
    if not query_artist or not query_title or not candidate_title:
        return False

    if not versions_compatible(query_title, candidate_title):
        return False

    q_base, _, q_rem = extract_version_tokens(query_title)
    c_base, _, c_rem = extract_version_tokens(candidate_title)
    if norm(query_title) != norm(candidate_title):
        if not q_base or q_base != c_base:
            return False

    q_artists = set(parse_artists(query_artist))
    if q_rem:
        q_artists.add(q_rem)
    c_artists: Set[str] = set()
    for item in candidate_artists:
        c_artists.update(parse_artists(item))
    if c_rem:
        c_artists.add(c_rem)

    return bool(q_artists & c_artists)


def clean_filename(filename: str) -> str:
    """Clean filename by removing [Free DL], etc., and stripping forbidden characters."""
    s = re.sub(r"(?i)\s*\[(?:free dl|free download|official audio|official video)\]", "", filename)
    s = re.sub(r"(?i)\s*\((?:free dl|free download|official audio|official video)\)", "", s)
    s = re.sub(r'[:*?"<>|\\]', "", s)
    return s.strip()


def parse_artist_title_from_stem(stem: str) -> Tuple[str, str]:
    """Legacy helper: parse 'Artist - Title' from filename stem (retained for compatibility)."""
    cleaned = clean_filename(stem)
    parts = cleaned.split(" - ", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", cleaned.strip()


def _strip_leading_track_number(stem: str) -> str:
    """
    Safely strip leading track/disc numbers ('01 ', '01. ', '1-02 ', '03 - ')
    without stripping real numeric titles like '4AM' or '24K Magic'.
    """
    s = stem.strip()
    # Case 1: Disc-track or zero-padded track number (e.g. '01 4AM', '01 #YEET', '1-01 Clarity', '02 - Song')
    s_new = re.sub(r"^(?:\d{1,2}-)?0\d(?:\s*[\.\)\-_]\s*|\s+)+", "", s)
    if s_new != s and s_new.strip():
        return s_new.strip()

    # Case 2: Explicit delimiter after 1-2 digit track number (e.g. '1. Song', '12) Song', '7 - Artist - Song')
    s_new = re.sub(r"^(?:\d{1,2}-)?[1-9]\d?(?:\s*[\.\)]\s*|\s+-\s+)", "", s)
    if s_new != s and s_new.strip():
        return s_new.strip()

    # Case 3: Two-digit unpadded track number 10..39 followed by space and non-digit title
    s_new = re.sub(r"^[1-3]\d\s+(?=[A-Za-z#\[])", "", s)
    if s_new != s and s_new.strip():
        return s_new.strip()

    return s


def _is_version_or_remix_segment(seg: str) -> Tuple[bool, str]:
    """Return (is_qualifier_only, remixer_name_if_any) for a hyphen-separated segment."""
    clean = seg.strip(" ()[]")
    if not clean:
        return False, ""
    if _PURE_VERSION_RE.match(clean):
        return True, ""
    m = _REMIXER_SUFFIX_RE.match(clean)
    if m:
        remixer = m.group("remixer").strip()
        if norm(remixer) not in ("original", "extended", "radio", "club", "vocal", "instrumental"):
            return True, remixer
        return True, ""
    return False, ""


def parse_filename_identity(filename_or_stem: str) -> ParsedFilenameIdentity:
    """
    Safely parse untrusted audio filename into structured hints and explicit confidence.
    - Never treats a remixer ('01 Beneath The Lights - Darren Styles Remix.wav') as original artist.
    - Never treats a single title ('01 Clarity.wav', '01 4AM.wav') as verified artist+title.
    - Never invents identity from punctuation-only names ('-', '-.wav').
    - Preserves both orientations on 'A - B' ambiguous stems.
    """
    raw = str(filename_or_stem or "").strip()
    if not raw:
        return ParsedFilenameIdentity()

    p = Path(raw)
    stem = p.stem if p.suffix.lower() in _AUDIO_EXTS else raw
    cleaned = clean_filename(stem)
    # Strip trailing OS duplicate suffix like ' (1)' or ' - Copy' only if preceded by text
    cleaned = re.sub(r"(?i)\s*(?:-\s*copy|\(\d\))\s*$", "", cleaned).strip()

    # Check if punctuation-only (e.g. '-', '--', '___')
    if not norm(cleaned):
        return ParsedFilenameIdentity(confidence="unresolved")

    cleaned = _strip_leading_track_number(cleaned)
    if not norm(cleaned):
        return ParsedFilenameIdentity(confidence="unresolved")

    featured: List[str] = []
    for fm in _FEAT_EXTRACT_RE.finditer(cleaned):
        featured.extend(x.strip() for x in re.split(r",|&|\band\b", fm.group(1)) if norm(x))

    # Split on ' - ' (surrounded by whitespace so hyphenated artist names like 'A-RIZE' stay intact!)
    parts = [seg.strip() for seg in re.split(r"\s+-\s+", cleaned) if seg.strip()]
    parts = [seg for seg in parts if norm(seg)]

    if not parts:
        return ParsedFilenameIdentity(confidence="unresolved")

    if len(parts) == 1:
        only = parts[0]
        _, rem = _is_version_or_remix_segment(only)
        return ParsedFilenameIdentity(
            title_hint=only,
            remixer_hint=rem,
            featured_artists=featured,
            confidence="title_only",
        )

    if len(parts) == 2:
        left, right = parts
        right_is_qual, right_remixer = _is_version_or_remix_segment(right)
        if right_is_qual:
            # e.g. '01 Beneath The Lights - Darren Styles Remix.wav' -> title + remix qualifier, artist unknown!
            combined_title = f"{left} ({right})" if f"({right.lower()})" not in left.lower() else left
            return ParsedFilenameIdentity(
                title_hint=combined_title,
                remixer_hint=right_remixer,
                version_hint=right,
                featured_artists=featured,
                confidence="title_only",
            )

        left_is_qual, left_remixer = _is_version_or_remix_segment(left)
        if left_is_qual:
            combined_title = f"{right} ({left})"
            return ParsedFilenameIdentity(
                title_hint=combined_title,
                remixer_hint=left_remixer,
                version_hint=left,
                featured_artists=featured,
                confidence="title_only",
            )

        # Two substantive sides ('Artist - Title' OR 'Title - Artist') -> both recorded, marked ambiguous
        return ParsedFilenameIdentity(
            artist=left,
            title=right,
            alt_artist=right,
            alt_title=left,
            title_hint=right,
            featured_artists=featured,
            confidence="filename_ambiguous",
        )

    # 3+ segments (e.g. 'PART OF ME - HARDSTYLE SPED UP - SICK LEGEND' or 'Artist - Title - Artist Remix')
    qualifiers: List[str] = []
    remixer_found = ""
    substantive: List[str] = []
    for seg in parts:
        is_q, rem = _is_version_or_remix_segment(seg)
        if is_q:
            qualifiers.append(seg)
            if rem and not remixer_found:
                remixer_found = rem
        else:
            substantive.append(seg)

    qual_suffix = f" ({', '.join(qualifiers)})" if qualifiers else ""
    if len(substantive) == 1:
        return ParsedFilenameIdentity(
            title_hint=f"{substantive[0]}{qual_suffix}",
            remixer_hint=remixer_found,
            version_hint=", ".join(qualifiers),
            featured_artists=featured,
            confidence="title_only",
        )
    if len(substantive) >= 2:
        first = substantive[0]
        second = " - ".join(substantive[1:])
        return ParsedFilenameIdentity(
            artist=first,
            title=f"{second}{qual_suffix}",
            alt_artist=second,
            alt_title=f"{first}{qual_suffix}",
            title_hint=f"{second}{qual_suffix}",
            remixer_hint=remixer_found,
            version_hint=", ".join(qualifiers),
            featured_artists=featured,
            confidence="filename_ambiguous",
        )

    return ParsedFilenameIdentity(confidence="unresolved")


def seconds_from_val(val: Any) -> Optional[float]:
    """Convert duration value in ms, seconds, or MM:SS string to seconds."""
    if val is None:
        return None
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        if ":" in s:
            parts = s.split(":")
            try:
                if len(parts) == 2:
                    return round(int(parts[0]) * 60 + float(parts[1]), 2)
                if len(parts) == 3:
                    return round(int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2]), 2)
            except ValueError:
                return None
    try:
        x = float(val)
        if x <= 0:
            return None
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
    if type(pitch_class) is not int or pitch_class not in range(12) or mode not in (0, 1):
        return None
    major_wheel = [8, 3, 10, 5, 12, 7, 2, 9, 4, 11, 6, 1]
    minor_wheel = [5, 12, 7, 2, 9, 4, 11, 6, 1, 8, 3, 10]
    wheel = major_wheel if mode == 1 else minor_wheel
    return f"{wheel[pitch_class]}{'B' if mode == 1 else 'A'}"


def is_valid_camelot(key_str: Any) -> bool:
    """Validate that key string conforms to Camelot notation (1A-12B)."""
    return bool(re.fullmatch(r"(?:[1-9]|1[0-2])[AB]", str(key_str or "").strip()))


def normalize_key_to_camelot(raw_key: Any) -> str:
    """
    Normalize Camelot ('8a' -> '8A'), OpenKey ('1m' -> '8A', '1d' -> '8B'),
    or standard musical key strings ('A minor', 'Am', 'F# major', 'Eb') to Camelot (1A-12B).
    Returns '' if empty or unrecognized (never invents keys).
    """
    if raw_key is None:
        return ""
    s = str(raw_key).strip()
    if not s:
        return ""

    # 1. Already Camelot (e.g. '8A', '11b', '08A')
    m_cam = re.fullmatch(r"0?([1-9]|1[0-2])\s*([ABab])", s)
    if m_cam:
        return f"{int(m_cam.group(1))}{m_cam.group(2).upper()}"

    # 2. OpenKey notation (1m..12m -> minor A, 1d..12d -> major B)
    m_open = re.fullmatch(r"0?([1-9]|1[0-2])\s*([mdMD])", s)
    if m_open:
        num = int(m_open.group(1))
        kind = m_open.group(2).lower()
        cam_num = ((num + 7 - 1) % 12) + 1
        return f"{cam_num}{'A' if kind == 'm' else 'B'}"

    # 3. Standard musical key (e.g. 'Am', 'A minor', 'F# major', 'Dbm', 'C# min', 'G')
    s_clean = s.replace("♯", "#").replace("♭", "b")
    m_mus = re.fullmatch(
        r"([A-Ga-g][#b]?)\s*(maj(?:or)?|min(?:or)?|m)?",
        s_clean,
        flags=re.IGNORECASE,
    )
    if m_mus:
        note = m_mus.group(1).lower()
        qual = (m_mus.group(2) or "").lower()
        pc = _MUSICAL_NOTE_TO_PC.get(note)
        if pc is not None:
            mode = 0 if qual in ("m", "min", "minor") else 1
            return camelot_key(pc, mode) or ""

    return ""


def is_valid_bpm(bpm_val: Any) -> bool:
    """Validate that BPM is a realistic musical tempo (40 to 350)."""
    try:
        v = float(bpm_val)
        return 40.0 <= v <= 350.0
    except (ValueError, TypeError):
        return False


def normalize_bpm_str(raw_bpm: Any) -> str:
    """Normalize BPM tag string if valid (40..350), else return ''."""
    if raw_bpm is None:
        return ""
    s = re.sub(r"(?i)\s*bpm\s*$", "", str(raw_bpm).strip())
    if not s:
        return ""
    try:
        val = round(float(s), 1)
    except ValueError:
        return ""
    if not (40.0 <= val <= 350.0):
        return ""
    return f"{val:g}"


def format_display_bpm(raw_bpm: Any) -> str:
    """Format verified/proposed BPM for spreadsheet display as rounded integer string (40..350)."""
    if raw_bpm is None or raw_bpm in ("", "CONFLICT"):
        return ""
    s = re.sub(r"(?i)\s*bpm\s*$", "", str(raw_bpm).strip())
    if not s:
        return ""
    try:
        val = float(s)
    except ValueError:
        return ""
    if not (40.0 <= val <= 350.0):
        return ""
    return str(int(round(val)))


def verify_candidate(
    candidate_isrc: Optional[str],
    candidate_duration: Optional[float],
    local_isrc: Optional[str],
    local_duration: Optional[float],
) -> str:
    """
    Determine acoustic/identifier verification status against local audio characteristics.
    (Legacy 4-arg signature preserved for backward compatibility.)
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


def verify_recording_candidate(
    query_artist: str,
    query_title: str,
    identity_confidence: str,
    local_isrc: Optional[str],
    local_duration: Optional[float],
    candidate_title: str,
    candidate_artists: List[str],
    candidate_isrc: Optional[str],
    candidate_duration: Optional[float],
    alt_artist: str = "",
    alt_title: str = "",
) -> str:
    """
    Conservative recording-level verifier:
    1. Title-only queries NEVER auto-verify via duration/ISRC ('Title-only review').
    2. Checks normalized title, primary artist overlap, and remix/edit/version compatibility
       (testing alternate orientation if filename order was ambiguous).
    3. Unrelated tracks with matching duration are rejected ('Identity mismatch').
    4. Checks ISRC and duration conflicts before granting 'ISRC supported' or 'Duration supported'.
    """
    if identity_confidence == "title_only" or not query_artist.strip():
        return "Title-only review"

    primary_ok = match_track(query_artist, query_title, candidate_title, candidate_artists)
    alt_ok = False
    if not primary_ok and alt_artist and alt_title:
        alt_ok = match_track(alt_artist, alt_title, candidate_title, candidate_artists)

    if not primary_ok and not alt_ok:
        # Distinguish version conflict (same artist + base title, incompatible remix/VIP) from unrelated track
        q_base, _, _ = extract_version_tokens(query_title)
        c_base, _, _ = extract_version_tokens(candidate_title)
        q_art = set(parse_artists(query_artist))
        c_art = {a for item in candidate_artists for a in parse_artists(item)}
        if q_base and q_base == c_base and (q_art & c_art) and not versions_compatible(query_title, candidate_title):
            return "Version conflict"
        return "Identity mismatch"

    return verify_candidate(
        candidate_isrc=candidate_isrc,
        candidate_duration=candidate_duration,
        local_isrc=local_isrc,
        local_duration=local_duration,
    )
