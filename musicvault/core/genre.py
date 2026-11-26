"""
Parent genre classification, alias normalization, and taxonomy management.
"""
import re
from typing import Dict, List, Optional, Set, Tuple, Any

from .matcher import norm, parse_artists
from ..models import Candidate, TrackMetadata

# Canonical parent genres and common aliases
ALIASES_PARENT: Dict[str, str] = {
    'hardstyle': 'Hardstyle',
    'rawstyle': 'Hardstyle',
    'euphorichardstyle': 'Hardstyle',
    'xtraraw': 'Hardstyle',
    'frenchcore': 'Hardstyle',
    'uptempo': 'Hardstyle',
    'reversebass': 'Hardstyle',
    'hardcore': 'Hardcore',
    'techno': 'Techno',
    'hypertechno': 'Techno',
    'hardtechno': 'Techno',
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
    'house': 'House',
    'ukgarage': 'UK Garage',
    'garage': 'UK Garage',
    'dubstep': 'Dubstep',
    'pop': 'Pop',
    'rock': 'Rock',
    'reggae': 'Reggae',
    'jungle': 'Jungle',
    'bassline': 'Bassline',
    'happyhardcore': 'Happy Hardcore',
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
    'rhythmandblues': 'R&B'
}

# Generic non-informative tags to ignore during parent classification
GENERIC_TAGS: Set[str] = {
    'electronic', 'edm', 'dance', 'music', 'unknown', 'other',
    'various', 'harddance', 'club', 'all'
}

# Subgenre to parent mapping (preserved for future re-enabling)
SUBGENRE_PARENT: Dict[str, str] = {
    'Euphoric': 'Hardstyle',
    'Rawstyle': 'Hardstyle',
    'Xtra Raw': 'Hardstyle',
    'Reverse Bass': 'Hardstyle',
    'Frenchcore': 'Hardcore',
    'Uptempo': 'Hardcore',
    'Hypertechno': 'Techno',
    'Hard Techno': 'Techno',
    'Jump Up': 'Drum & Bass',
    'Liquid': 'Drum & Bass',
    'Neurofunk': 'Drum & Bass',
    'Dancefloor': 'Drum & Bass'
}

_SUBGENRE_ALIASES: Dict[str, str] = {
    **{norm(k): k for k in SUBGENRE_PARENT},
    'euphorichardstyle': 'Euphoric',
    'hardtechno': 'Hard Techno',
    'liquiddnb': 'Liquid',
    'liquidfunk': 'Liquid',
    'uptempohardcore': 'Uptempo',
    'jumpupdnb': 'Jump Up',
}


def canonical_subgenre(style_name: Optional[str]) -> str:
    """Resolve raw style/tag text case-insensitively to canonical SUBGENRE_PARENT name."""
    if not style_name:
        return ""
    return _SUBGENRE_ALIASES.get(norm(style_name), "")

# Known artist sound profiles (artist -> (Parent Genre, Subgenre))
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
    norm('Sefa'): ('Hardstyle', 'Frenchcore'),
    norm('Dr. Peacock'): ('Hardstyle', 'Frenchcore'),
    norm('Da Tweekaz'): ('Hardstyle', 'Euphoric'),
    norm('Luude'): ('Drum & Bass', ''),
    norm('Basstripper'): ('Drum & Bass', 'Jump Up')
}

# Known label profiles
LABEL_PROFILES: Dict[str, Tuple[str, str]] = {
    norm('Minus is More'): ('Hardstyle', 'Rawstyle'),
    norm('Scantraxx'): ('Hardstyle', ''),
    norm('Dirty Workz'): ('Hardstyle', '')
}


def canonical_genre(genre_name: Optional[str]) -> str:
    """Resolve raw genre text to canonical parent genre name."""
    if not genre_name:
        return ""
    return ALIASES_PARENT.get(norm(genre_name), "")


def decide_genre(existing_genre: str, evidence_genres: List[str], locked: bool = False) -> Tuple[str, str]:
    """
    Decide parent genre based on existing spreadsheet value vs fresh catalogue evidence.
    Returns: (genre, status_reason)
    """
    g = canonical_genre(existing_genre)
    votes = set(evidence_genres) - {''}

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


def classify_track_genres(meta: TrackMetadata, candidates: List[Candidate],
                          enable_subgenres: bool = False) -> Dict[str, Any]:
    """
    Perform weighted evidence classification across candidates, artist, and label profiles.
    Parent genre classification is prioritized; subgenre classification can be toggled.
    """
    votes: Dict[str, Dict[str, int]] = {}
    subs: Dict[str, Dict[str, int]] = {}
    unmapped_tags: Set[str] = set()

    def add_vote(raw_g: str, category: str, weight: int) -> None:
        raw_clean = raw_g.strip()
        canon = canonical_genre(raw_clean)
        if not canon:
            if norm(raw_clean) not in GENERIC_TAGS:
                unmapped_tags.add(raw_clean)
            return
        votes.setdefault(canon, {})[category] = weight
        if raw_clean in SUBGENRE_PARENT:
            subs.setdefault(raw_clean, {})[category] = weight

    for c in candidates:
        v = (c.verification or "").lower()
        if 'conflict' in v or 'mismatch' in v:
            continue
        p = c.provider
        if 'wikidata' in p.lower() or 'inferred' in v:
            # Knowledge-graph hints are surfaced separately for human review, not weighted as catalogue votes
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

    # Artist profile influence
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
            'unmapped_tags': sorted(unmapped_tags)
        }

    score, top_genre = ranked[0]
    conflict = len(ranked) > 1 and (
        score - ranked[1][0] < 15 or
        ('catalogue' in votes[ranked[1][1]] and 'artist' in votes[top_genre])
    )

    chosen_sub = ""
    sub_confidence = "missing"
    if enable_subgenres:
        eligible = sorted(
            ((sum(v.values()), s) for s, v in subs.items() if SUBGENRE_PARENT.get(s) == top_genre),
            reverse=True
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
        'unmapped_tags': sorted(unmapped_tags)
    }
