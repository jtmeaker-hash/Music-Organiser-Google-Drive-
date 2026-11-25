"""
Discogs metadata provider client.
"""
import re
from typing import List, Optional
from ..models import Candidate
from ..core.matcher import match_track
from .base import BaseProvider


class DiscogsProvider(BaseProvider):
    """Client for Discogs API (releases, styles, and labels)."""

    def __init__(self, token: Optional[str] = None, contact: str = "", max_requests: int = 40):
        super().__init__("Discogs", default_delay=1.2, max_requests=max_requests, contact=contact)
        self.token = token

    def search(self, artist: str, title: str) -> List[Candidate]:
        if not self.token or not self.can_request():
            return []

        headers = {"Authorization": f"Discogs token={self.token}"}
        url = "https://api.discogs.com/database/search"
        params = {"artist": artist, "track": title, "type": "release", "per_page": 10}

        data = self.request(url, params=params, headers=headers)
        if not data or "results" not in data:
            return []

        candidates = []
        for hit in data.get("results", [])[:3]:
            if not self.can_request():
                break
            release_id = hit.get("id")
            if not release_id:
                continue

            rel_url = f"https://api.discogs.com/releases/{release_id}"
            rel_data = self.request(rel_url, headers=headers)
            if not rel_data:
                continue

            labels = [x.get("name", "") if isinstance(x, dict) else str(x) for x in rel_data.get("labels", [])]
            styles = rel_data.get("styles", []) or []

            for track_item in rel_data.get("tracklist", []):
                t_title = track_item.get("title", "")
                raw_artists = track_item.get("artists") or rel_data.get("artists", [])
                artists = [
                    re.sub(r' \(\d+\)$', '', x.get("name", "") if isinstance(x, dict) else str(x))
                    for x in raw_artists
                ]

                if match_track(artist, title, t_title, artists):
                    candidates.append(Candidate(
                        provider="Discogs release",
                        title=t_title,
                        artists=artists,
                        genre=styles,
                        labels=labels,
                        url=rel_data.get("uri")
                    ))

        return candidates
