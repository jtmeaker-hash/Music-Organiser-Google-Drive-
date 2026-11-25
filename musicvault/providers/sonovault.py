"""
SonoVault metadata provider client.
"""
from typing import List, Optional
from ..models import Candidate
from ..core.matcher import match_track, seconds_from_val, norm
from .base import BaseProvider


class SonoVaultProvider(BaseProvider):
    """Client for SonoVault API (BPM, Key, Genre, ISRC)."""

    def __init__(self, api_key: Optional[str] = None, contact: str = "", max_requests: int = 25):
        super().__init__("SonoVault", default_delay=3.2, max_requests=max_requests, contact=contact)
        self.api_key = api_key

    def search(self, artist: str, title: str) -> List[Candidate]:
        if not self.api_key or not self.can_request():
            return []

        # Artist alias fix
        query_artist = 'Rebelion' if norm(artist) == 'rebellion' else artist
        url = "https://api.sonovault.now/v1/tracks/search"
        headers = {"x-api-key": self.api_key}
        params = {"artist": query_artist, "title": title}

        data = self.request(url, params=params, headers=headers)
        if not data or "results" not in data:
            return []

        candidates = []
        for item in data.get("results", []):
            artists = [x.get("name", "") if isinstance(x, dict) else str(x) for x in item.get("artists", [])]
            t_name = item.get("title", "")
            if match_track(artist, title, t_name, artists):
                genres = (item.get("genre") or []) + (item.get("subgenre") or [])
                candidates.append(Candidate(
                    provider="SonoVault",
                    title=t_name,
                    artists=artists,
                    duration=seconds_from_val(item.get("duration_ms") or item.get("duration")),
                    isrc=item.get("isrc"),
                    bpm=float(item.get("bpm")) if item.get("bpm") is not None else None,
                    key=item.get("key_alphanumeric"),
                    genre=genres,
                    url=item.get("url")
                ))
        return candidates
