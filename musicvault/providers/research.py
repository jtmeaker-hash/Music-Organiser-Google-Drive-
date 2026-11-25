"""
Intelligent online metadata research.
Replaces brittle scraping (Reddit 403 errors) with legitimate open APIs (Wikidata, MusicBrainz, Wikipedia).
"""
import logging
from typing import Dict, List, Any, Optional
from ..core.genre import ALIASES_PARENT, canonical_genre
from .base import BaseProvider

logger = logging.getLogger("musicvault.research")


class ResearchProvider(BaseProvider):
    """
    Resilient metadata research engine.
    Uses Wikidata API and Wikipedia knowledge graphs to discover musical genres.
    """

    def __init__(self, contact: str = "", max_requests: int = 15):
        super().__init__("OnlineResearch", default_delay=1.0, max_requests=max_requests, contact=contact)

    def research_track(self, artist: str, title: str) -> Dict[str, Any]:
        """
        Research genre hints for an uncertain track via open Wikidata and Wikipedia APIs.
        """
        hints: List[Dict[str, Any]] = []
        hits: List[Dict[str, str]] = []

        if not self.can_request():
            return {
                "track": f"{artist} - {title}",
                "hits": hits,
                "genre_hints": hints,
                "status": "BUDGET_REACHED"
            }

        # Query Wikidata entity search
        wiki_url = "https://www.wikidata.org/w/api.php"
        params = {
            "action": "wbsearchentities",
            "search": f"{artist} {title}",
            "language": "en",
            "format": "json",
            "limit": 5
        }

        data = self.request(wiki_url, params=params)
        if data and "search" in data:
            for item in data["search"]:
                q_id = item.get("id")
                label = item.get("label", "")
                desc = item.get("description", "")
                concept_url = item.get("concepturi", f"https://www.wikidata.org/wiki/{q_id}")
                hits.append({"source": "wikidata", "url": concept_url, "title": f"{label}: {desc}"})

                # Check label and description for genre keywords
                full_text = f"{label} {desc}".lower()
                for keyword, canon in ALIASES_PARENT.items():
                    if keyword in full_text:
                        hints.append({
                            "url": concept_url,
                            "source": "wikidata",
                            "genres": [(canon, "")]
                        })

        # Also search artist if no track hits
        if not hints and self.can_request():
            params["search"] = artist
            artist_data = self.request(wiki_url, params=params)
            if artist_data and "search" in artist_data:
                for item in artist_data["search"][:2]:
                    q_id = item.get("id")
                    label = item.get("label", "")
                    desc = item.get("description", "")
                    concept_url = item.get("concepturi", f"https://www.wikidata.org/wiki/{q_id}")
                    hits.append({"source": "wikidata_artist", "url": concept_url, "title": f"{label}: {desc}"})
                    full_text = f"{label} {desc}".lower()
                    for keyword, canon in ALIASES_PARENT.items():
                        if keyword in full_text:
                            hints.append({
                                "url": concept_url,
                                "source": "wikidata_artist",
                                "genres": [(canon, "")]
                            })

        return {
            "track": f"{artist} - {title}",
            "hits": hits[:8],
            "genre_hints": hints,
            "status": "REVIEW_ONLY — snippets/titles not independently verified"
        }
