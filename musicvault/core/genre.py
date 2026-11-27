"""
Parent genre classification, granular subgenre taxonomy, placeholder detection, and alias normalization.
"""
from typing import Any, Dict, List, Optional, Set, Tuple

from ..models import Candidate, TrackMetadata
from .matcher import norm, parse_artists

# Canonical parent genres and common aliases
ALIASES_PARENT: Dict[str, str] = {
    'hardstyle': 'Hardstyle',
    'rawstyle': 'Hardstyle',
    'rawphoric': 'Hardstyle',
    'euphorichardstyle': 'Hardstyle',
    'xtraraw': 'Hardstyle',
    'xtrarawstyle': 'Hardstyle',
    'experimentalraw': 'Hardstyle',
    'experimentalrawstyle': 'Hardstyle',
    'rawphorichardstyle': 'Hardstyle',
    'mainsteamhardstyle': 'Hardstyle',
    'frenchcore': 'Hardcore',
    'uptempo': 'Hardcore',
    'deutscherkrach': 'Hardcore',
    'doomcore': 'Hardcore',
    'speedcore': 'Hardcore',
    'jcore': 'Hardcore',
    'gabba': 'Hardcore',
    'reversebass': 'Hardstyle',
    'slowtempohardstyle': 'Hardstyle',
    'mainstreamhardstyle': 'Hardstyle',
    'mainstreamfestivalhardstyle': 'Hardstyle',
    'hardcore': 'Hardcore',
    'uptempohardcore': 'Hardcore',
    'gabber': 'Hardcore',
    'mainstreamhardcore': 'Hardcore',
    'techno': 'Techno',
    'hypertechno': 'Techno',
    'hardtechno': 'Techno',
    'industrialtechno': 'Techno',
    'acidtechno': 'Techno',
    'trance': 'Trance',
    'drumandbass': 'Drum & Bass',
    'drumbass': 'Drum & Bass',
    'drumnbass': 'Drum & Bass',
    'dnb': 'Drum & Bass',
    'jumpup': 'Drum & Bass',
    'liquid': 'Drum & Bass',
    'liquiddnb': 'Drum & Bass',
    'neurofunk': 'Drum & Bass',
    'dancefloor': 'Drum & Bass',
    'hiphop': 'Hip-Hop',
    'hiphoprap': 'Hip-Hop',
    'rap': 'Hip-Hop',
    'phonk': 'Hip-Hop',
    'house': 'House',
    'progressivehouse': 'House',
    'basshouse': 'House',
    'slaphouse': 'House',
    'techhouse': 'House',
    'futurehouse': 'House',
    'tropicalhouse': 'House',
    'ukgarage': 'UK Garage',
    'garage': 'UK Garage',
    'dubstep': 'Dubstep',
    'dubstepandbass': 'Dubstep',
    'dubstepbass': 'Dubstep',
    'pop': 'Pop',
    'popandalternative': 'Pop',
    'popalternative': 'Pop',
    'rock': 'Rock',
    'poppunk': 'Rock',
    'alternativerock': 'Rock',
    'country': 'Country',
    'contemporarycountry': 'Country',
    'hardbounce': 'Hard Bounce',
    'reggae': 'Reggae',
    'jungle': 'Jungle',
    'bassline': 'Bassline',
    'happyhardcore': 'Happy Hardcore',
    'ukhardcore': 'Happy Hardcore',
    'deephappyhardcore': 'Happy Hardcore',
    'ambient': 'Ambient',
    'disco': 'Disco',
    'funk': 'Funk',
    'soul': 'Soul',
    'jazz': 'Jazz',
    'classical': 'Classical',
    'breakbeat': 'Breakbeat',
    'electro': 'Electro',
    'trap': 'Trap',
    'rnb': 'R&B',
    'rhythmandblues': 'R&B',
}

# Generic non-informative tags to ignore during parent classification
GENERIC_TAGS: Set[str] = {
    'electronic', 'edm', 'dance', 'music', 'unknown', 'other',
    'various', 'harddance', 'club', 'all', 'unclassified', 'needsreview', 'pending',
}

SHEET_GENRE_PLACEHOLDERS: Set[str] = {
    'unclassified', 'needsreview', 'pending', 'unknown', 'tbd', 'none', 'na',
}

UMBRELLA_CLUB_GENRES: Set[str] = {
    'danceclub', 'danceandclub',
}

SHEET_SUBGENRE_PLACEHOLDERS: Set[str] = {
    'needsreview', 'subgenrepending', 'pending', 'unclassified', 'unknown', 'tbd', 'none', 'na',
}

# Approved granular subgenre -> parent genre taxonomy
SUBGENRE_PARENT: Dict[str, str] = {
    'Euphoric': 'Hardstyle',
    'Rawstyle': 'Hardstyle',
    'Rawphoric': 'Hardstyle',
    'Xtra Raw': 'Hardstyle',
    'Experimental Raw': 'Hardstyle',
    'Reverse Bass': 'Hardstyle',
    'Slowtempo': 'Hardstyle',
    'Mainstream Hardstyle': 'Hardstyle',
    'Frenchcore': 'Hardcore',
    'Uptempo': 'Hardcore',
    'Gabber': 'Hardcore',
    'Mainstream Hardcore': 'Hardcore',
    'Doomcore': 'Hardcore',
    'Speedcore': 'Hardcore',
    'J-Core': 'Hardcore',
    'UK Hardcore': 'Happy Hardcore',
    'Deep Happy Hardcore': 'Happy Hardcore',
    'Hypertechno': 'Techno',
    'Hard Techno': 'Techno',
    'Industrial Techno': 'Techno',
    'Acid Techno': 'Techno',
    'Jump Up': 'Drum & Bass',
    'Liquid': 'Drum & Bass',
    'Neurofunk': 'Drum & Bass',
    'Dancefloor': 'Drum & Bass',
    'Progressive House': 'House',
    'Bass House': 'House',
    'Slap House': 'House',
    'Tech House': 'House',
    'Future House': 'House',
    'Tropical House': 'House',
    'Pop Punk': 'Rock',
    'Alternative Rock': 'Rock',
    'Phonk': 'Hip-Hop',
    'Melodic Rap': 'Hip-Hop',
}

_SUBGENRE_ALIASES: Dict[str, str] = {
    **{norm(k): k for k in SUBGENRE_PARENT},
    'euphorichardstyle': 'Euphoric',
    'euphoricclassic': 'Euphoric',
    'melodichardstyle': 'Euphoric',
    'rawstylemodern': 'Rawstyle',
    'rawstylerawphoric': 'Rawphoric',
    'rawphorichardstyle': 'Rawphoric',
    'extraraw': 'Xtra Raw',
    'xtrarawstyle': 'Xtra Raw',
    'experimentalrawstyle': 'Experimental Raw',
    'classicreversebass': 'Reverse Bass',
    'slowtempohardstyle': 'Slowtempo',
    'mainstreamfestival': 'Mainstream Hardstyle',
    'mainstreamfestivalhardstyle': 'Mainstream Hardstyle',
    'mainsteamhardstyle': 'Mainstream Hardstyle',
    'festivalhardstyle': 'Mainstream Hardstyle',
    'hardtechno': 'Hard Techno',
    'hardtechnorave': 'Hard Techno',
    'schranz': 'Hard Techno',
    'hardtechnoindustrial': 'Industrial Techno',
    'liquiddnb': 'Liquid',
    'liquidfunk': 'Liquid',
    'uptempohardcore': 'Uptempo',
    'deutscherkrach': 'Uptempo',
    'gabba': 'Gabber',
    'earlyhardcore': 'Gabber',
    'hardcoretechno': 'Mainstream Hardcore',
    'gabbermainstreamhardcore': 'Mainstream Hardcore',
    'jumpupdnb': 'Jump Up',
    'dancefloordnb': 'Dancefloor',
}


def canonical_subgenre(style_name: Optional[str]) -> str:
    """Resolve raw style/tag text case-insensitively to canonical SUBGENRE_PARENT name."""
    if not style_name:
        return ""
    return _SUBGENRE_ALIASES.get(norm(style_name), "")


def is_genre_placeholder(raw_genre: Any) -> bool:
    """Return True when a spreadsheet Genre cell contains a non-evidence placeholder (e.g. Unclassified)."""
    n = norm(raw_genre)
    return bool(n and n in SHEET_GENRE_PLACEHOLDERS)


def is_umbrella_club_genre(raw_genre: Any) -> bool:
    """Return True when a spreadsheet Genre cell holds an umbrella Dance/Club bucket."""
    return norm(raw_genre) in UMBRELLA_CLUB_GENRES


def is_subgenre_placeholder(raw_subgenre: Any) -> bool:
    """Return True when a spreadsheet Subgenre cell holds a review/pending placeholder."""
    s = str(raw_subgenre or "").strip()
    if not s:
        return False
    n = norm(s)
    if n in SHEET_SUBGENRE_PLACEHOLDERS:
        return True
    s_low = s.casefold()
    return s_low.endswith("subgenre pending") or s_low.endswith("(review)")


# Known artist sound profiles (artist -> (Parent Genre, Subgenre)) — LOW review hints only, never sole inference
ARTIST_PROFILES: Dict[str, Tuple[str, str]] = {
    norm('Act of Rage'): ('Hardstyle', 'Rawstyle'),
    norm('Wasted Penguinz'): ('Hardstyle', 'Euphoric'),
    norm('Atmozfears'): ('Hardstyle', 'Euphoric'),
    norm('Rebelion'): ('Hardstyle', 'Rawstyle'),
    norm('Adjuzt'): ('Hardstyle', 'Xtra Raw'),
    norm('Sickmode'): ('Hardstyle', 'Xtra Raw'),
    norm('Krowdexx'): ('Hardstyle', 'Xtra Raw'),
    norm('A-Rize'): ('Hardstyle', 'Euphoric'),
    norm('Headhunterz'): ('Hardstyle', 'Euphoric'),
    norm('Angerfist'): ('Hardcore', ''),
    norm('Sefa'): ('Hardcore', 'Frenchcore'),
    norm('Dr. Peacock'): ('Hardcore', 'Frenchcore'),
    norm('Da Tweekaz'): ('Hardstyle', 'Euphoric'),
    norm('Luude'): ('Drum & Bass', ''),
    norm('Basstripper'): ('Drum & Bass', 'Jump Up'),
}

# Known label profiles — LOW review hints unless recording-verified catalogue genre is present
LABEL_PROFILES: Dict[str, Tuple[str, str]] = {
    norm('Minus is More'): ('Hardstyle', 'Rawstyle'),
    norm('Scantraxx'): ('Hardstyle', ''),
    norm('Dirty Workz'): ('Hardstyle', ''),
    norm('Roughstate'): ('Hardstyle', 'Rawstyle'),
    norm('Art of Creation'): ('Hardstyle', 'Euphoric'),
}


def canonical_genre(genre_name: Optional[str]) -> str:
    """Resolve raw genre or subgenre text to canonical parent genre name."""
    if not genre_name:
        return ""
    parent = ALIASES_PARENT.get(norm(genre_name), "")
    if parent:
        return parent
    sub = canonical_subgenre(genre_name)
    return SUBGENRE_PARENT.get(sub, "") if sub else ""


def decide_genre(existing_genre: str, evidence_genres: List[str], locked: bool = False) -> Tuple[str, str]:
    """
    Decide parent genre based on existing spreadsheet value vs fresh catalogue evidence.
    - Treats placeholders ('Unclassified', 'Needs Review') as empty.
    - Allows umbrella 'Dance / Club' / 'Dance & Club' upgrade to 'Hardstyle' ONLY when
      recording-supported catalogue evidence unanimously identifies 'Hardstyle' without conflict.
    Returns: (genre, status_reason)
    """
    votes = set(evidence_genres) - {''}

    if is_umbrella_club_genre(existing_genre):
        if locked:
            return '', 'USER_LOCKED'
        if len(votes) > 1:
            return '', 'CONFLICT'
        if votes == {'Hardstyle'}:
            return 'Hardstyle', 'UMBRELLA_DANCE_CLUB_TO_HARDSTYLE'
        return '', 'UMBRELLA_DANCE_CLUB_HOLD'

    g = '' if is_genre_placeholder(existing_genre) else canonical_genre(existing_genre)

    if locked and g:
        return g, 'USER_LOCKED_DISCREPANCY' if (votes - {g}) else 'USER_LOCKED'

    if len(votes) > 1 or (g and votes and g not in votes):
        return '', 'CONFLICT'

    if votes:
        chosen = next(iter(votes))
        return chosen, 'AGREEMENT' if g else 'NEW_CATALOGUE_CANDIDATE'

    if g:
        return g, 'EXISTING_ONLY_REVIEW'

    return '', 'UNKNOWN'


def classify_track_genres(
    meta: TrackMetadata,
    candidates: List[Candidate],
    enable_subgenres: bool = False,
) -> Dict[str, Any]:
    """
    Perform weighted evidence classification across candidates, artist, and label profiles.
    Note: Artist/label profile votes alone never reach MEDIUM or HIGH in `field_decision` —
    they serve strictly as LOW-confidence review hints and conflict cross-checks.
    """
    votes: Dict[str, Dict[str, int]] = {}
    subs: Dict[str, Dict[str, int]] = {}
    unmapped_tags: Set[str] = set()

    def add_vote(raw_g: str, category: str, weight: int) -> None:
        raw_clean = raw_g.strip()
        canon_sub = canonical_subgenre(raw_clean)
        canon = canonical_genre(raw_clean) or (SUBGENRE_PARENT.get(canon_sub, "") if canon_sub else "")
        if not canon:
            if norm(raw_clean) not in GENERIC_TAGS:
                unmapped_tags.add(raw_clean)
            return
        votes.setdefault(canon, {})[category] = weight
        if canon_sub and SUBGENRE_PARENT.get(canon_sub) == canon:
            subs.setdefault(canon_sub, {})[category] = weight

    for c in candidates:
        v = (c.verification or "").lower()
        if 'conflict' in v or 'mismatch' in v:
            continue
        p = c.provider
        if 'wikidata' in p.lower() or 'inferred' in v:
            continue
        if p == 'Discogs release':
            cat, w = 'release', 25
        elif 'isrc supported' in v or 'duration supported' in v:
            cat, w = ('track', 40) if p == 'MusicBrainz' else ('catalogue', 10)
        else:
            continue

        for g in c.genre:
            if isinstance(g, str):
                add_vote(g, cat, w)

        for label in c.labels:
            n_lbl = norm(label)
            if n_lbl in LABEL_PROFILES:
                parent, sub = LABEL_PROFILES[n_lbl]
                add_vote(parent, 'label', 10)
                if sub and enable_subgenres:
                    add_vote(sub, 'label', 10)

    parsed = set(parse_artists(meta.artist))
    if meta.identity_origin == 'filename fallback' and meta.title:
        parsed.update(parse_artists(meta.title))

    for a in parsed:
        if a in ARTIST_PROFILES:
            parent, sub = ARTIST_PROFILES[a]
            add_vote(parent, 'artist', 20)
            if sub and enable_subgenres:
                add_vote(sub, 'artist', 20)

    ranked = sorted(((sum(v.values()), g) for g, v in votes.items()), reverse=True)
    if not ranked:
        return {
            'genre': '',
            'subgenre': '',
            'genre_confidence': 'missing',
            'subgenre_confidence': 'missing',
            'genre_scores': '',
            'unmapped_tags': sorted(unmapped_tags),
        }

    score, top_genre = ranked[0]
    conflict = len(ranked) > 1 and (
        score - ranked[1][0] < 15
        or ('catalogue' in votes[ranked[1][1]] and 'artist' in votes[top_genre])
    )

    chosen_sub = ""
    sub_confidence = "missing"
    if enable_subgenres:
        eligible = sorted(
            ((sum(v.values()), s) for s, v in subs.items() if SUBGENRE_PARENT.get(s) == top_genre),
            reverse=True,
        )
        if eligible and (len(eligible) == 1 or eligible[0][0] > eligible[1][0]):
            chosen_sub = eligible[0][1]
            sub_confidence = 'inference only'

    status = (
        'review conflict' if conflict
        else 'weak suggestion' if score < 35
        else 'weighted candidate'
    ) + f' ({score}/100)'

    return {
        'genre': top_genre,
        'subgenre': chosen_sub,
        'genre_confidence': status,
        'subgenre_confidence': sub_confidence,
        'genre_scores': '; '.join(f'{g}:{s}' for s, g in ranked),
        'unmapped_tags': sorted(unmapped_tags),
    }
