"""
Intelligent open-knowledge-graph metadata research via Wikidata API.
Validates response schema strictly, restricts hints to musical entities with word-boundary matching,
and labels all suggestions explicitly as review-only inferred hints (never catalogue verification).
"""
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from ..core.genre import SUBGENRE_PARENT
from .base import BaseProvider, ProviderCallOutcome, ProviderStatus

logger = logging.getLogger("musicvault.research")

# Explicit word-bounded genre phrase dictionary -> (Canonical Parent Genre, Optional Subgenre)
_WIKIDATA_GENRE_PHRASES = [
    ("euphoric hardstyle", ("Hardstyle", "Euphoric")),
    ("rawstyle", ("Hardstyle", "Rawstyle")),
    ("xtra raw", ("Hardstyle", "Xtra Raw")),
    ("reverse bass", ("Hardstyle", "Reverse Bass")),
    ("hardstyle", ("Hardstyle", "")),
    ("frenchcore", ("Hardcore", "Frenchcore")),
    ("uptempo hardcore", ("Hardcore", "Uptempo")),
    ("happy hardcore", ("Happy Hardcore", "")),
    ("hardcore techno", ("Hardcore", "")),
    ("hypertechno", ("Techno", "Hypertechno")),
    ("hard techno", ("Techno", "Hard Techno")),
    ("techno", ("Techno", "")),
    ("liquid funk", ("Drum & Bass", "Liquid")),
    ("neurofunk", ("Drum & Bass", "Neurofunk")),
    ("jump up", ("Drum & Bass", "Jump Up")),
    ("drum and bass", ("Drum & Bass", "")),
    ("drum & bass", ("Drum & Bass", "")),
    ("uk garage", ("UK Garage", "")),
    ("trance", ("Trance", "")),
    ("dubstep", ("Dubstep", "")),
    ("hip-hop", ("Hip-Hop", "")),
    ("hip hop", ("Hip-Hop", "")),
    ("house music", ("House", "")),
    ("breakbeat", ("Breakbeat", "")),
    ("jungle music", ("Jungle", "")),
]

_MUSIC_CONTEXT_RE = re.compile(
    r"\b(?:song|single|track|recording|album|ep|remix|anthem|dj|producer|musician|"
    r"musical|duo|trio|band|group|singer|rapper|composer|electronic|record\s+label)\b",
    re.IGNORECASE,
)


class ResearchProvider(BaseProvider):
    """
    Resilient open knowledge-graph research engine (Wikidata CC0).
    Produces review-only inferred genre hints; never treated as recording verification.
    """

    ALLOWS_OPEN_PERSISTENT_CACHE = True
    DEFAULT_CACHE_TTL_SECONDS = 7 * 86400

    def __init__(
        self,
        contact: str = "",
        max_requests: int = 15,
        db: Optional[Any] = None,
        provider_terms_cleared: bool = False,
        default_delay: float = 1.0,
        **kwargs: Any,
    ):
        super().__init__(
            "OnlineResearch",
            default_delay=default_delay,
            max_requests=max_requests,
            contact=contact,
            db=db,
            provider_terms_cleared=provider_terms_cleared,
            **kwargs,
        )

    @staticmethod
    def _extract_validated_hints(data: Any, source_label: str) -> tuple[List[Dict[str, str]], List[Dict[str, Any]]]:
        """Validate Wikidata wbsearchentities schema and extract word-bounded music genre hints."""
        hits: List[Dict[str, str]] = []
        hints: List[Dict[str, Any]] = []

        if not isinstance(data, dict):
            return hits, hints
        search_items = data.get("search")
        if not isinstance(search_items, list):
            return hits, hints

        for item in search_items[:5]:
            if not isinstance(item, dict):
                continue
            q_id = str(item.get("id") or "").strip()
            label = str(item.get("label") or "").strip()
            desc = str(item.get("description") or "").strip()
            if not q_id or not (label or desc):
                continue

            concept_url = str(item.get("concepturi") or f"https://www.wikidata.org/wiki/{q_id}")
            hits.append({"source": source_label, "url": concept_url, "title": f"{label}: {desc}"})

            combined = f"{label} {desc}"
            # Require musical context in entity description/label so non-music entities aren't misread
            if not _MUSIC_CONTEXT_RE.search(combined):
                continue

            matched_pairs: List[Dict[str, str]] = []
            seen_parents = set()
            for phrase, (parent_g, sub_g) in _WIKIDATA_GENRE_PHRASES:
                if re.search(r"(?<![A-Za-z0-9])" + re.escape(phrase) + r"(?![A-Za-z0-9])", combined, flags=re.I):
                    if parent_g not in seen_parents:
                        seen_parents.add(parent_g)
                        matched_pairs.append({"parent": parent_g, "subgenre": sub_g})

            if matched_pairs:
                hints.append(
                    {
                        "url": concept_url,
                        "source": source_label,
                        "evidence_type": "inferred",
                        "genre_mappings": matched_pairs,
                        # Structured list of canonical parent genre strings (safe against string indexing bugs)
                        "parent_genres": [m["parent"] for m in matched_pairs],
                    }
                )
        return hits, hints

    def research_track_detailed(self, artist: str, title: str) -> Tuple[Dict[str, Any], ProviderCallOutcome]:
        """Query Wikidata entities with structured ProviderCallOutcome diagnostics."""
        wiki_url = "https://www.wikidata.org/w/api.php"
        params = {
            "action": "wbsearchentities",
            "search": f"{artist} {title}".strip(),
            "language": "en",
            "format": "json",
            "limit": 5,
        }

        out = self.request_detailed(wiki_url, params=params)
        if out.status != ProviderStatus.OK:
            return {
                "track": f"{artist} - {title}",
                "hits": [],
                "genre_hints": [],
                "status": "BUDGET_REACHED" if out.status == ProviderStatus.BUDGET_EXHAUSTED else out.status.value,
            }, out

        hits, hints = self._extract_validated_hints(out.data, "wikidata_track")
        final_out = out

        if not hints and artist.strip():
            artist_params = dict(params, search=artist.strip())
            art_out = self.request_detailed(wiki_url, params=artist_params)
            if art_out.status == ProviderStatus.OK:
                a_hits, a_hints = self._extract_validated_hints(art_out.data, "wikidata_artist")
                hits.extend(a_hits)
                hints.extend(a_hints)
            elif art_out.status != ProviderStatus.EMPTY:
                final_out = art_out

        if final_out.status == ProviderStatus.OK and not hints:
            final_out = ProviderCallOutcome(
                provider=self.name,
                status=ProviderStatus.EMPTY,
                http_status=final_out.http_status,
                from_cache=final_out.from_cache,
                request_made=final_out.request_made,
            )

        return {
            "track": f"{artist} - {title}",
            "hits": hits[:8],
            "genre_hints": hints,
            "status": "REVIEW_ONLY — inferred knowledge-graph hints, not recording verification",
        }, final_out

    def research_track(self, artist: str, title: str) -> Dict[str, Any]:
        """Query Wikidata entities for track/artist genre hints with strict schema validation."""
        res, _ = self.research_track_detailed(artist, title)
        return res
