"""
AudioMeta provider client for acoustic features, BPM, and musical key.
"""
from typing import List, Optional
import urllib.parse
from ..models import Candidate
from ..core.matcher import match_track, seconds_from_val, camelot_key
from .base import BaseProvider


class AudioMetaProvider(BaseProvider):
    """Client for AudioMeta API."""

    def __init__(self, api_key: Optional[str] = None, contact: str = "", max_requests: int = 25):
        super().__init__("AudioMeta", default_delay=0.85, max_requests=max_requests, contact=contact)
        self.api_key = api_key

    def search(self, artist: str, title: str) -> List[Candidate]:
        if not self.api_key or not self.can_request():
            return []

        headers = {"Authorization": f"Bearer {self.api_key}"}
        hits = {}

        # Search by title and artist
        for q in dict.fromkeys([title, artist]):
            if not self.can_request():
                break
            url = "https://api.audiometa.io/v1/search"
            params = {"q": q, "type": "track", "limit": 50}
            data = self.request(url, params=params, headers=headers)
            if not data:
                continue
            tracks = data.get("tracks", {}).get("items", [])
            for item in tracks:
                track_id = item.get("id")
                if not track_id:
                    continue
                artists = [x.get("name", "") if isinstance(x, dict) else str(x) for x in item.get("artists", [])]
                if match_track(artist, title, item.get("name", ""), artists):
                    hits[track_id] = item

        candidates = []
        for track_id in list(hits.keys())[:2]:
            if not self.can_request():
                break
            detail_url = f"https://api.audiometa.io/v1/tracks/{urllib.parse.quote(track_id, safe='')}"
            track_data = self.request(detail_url, headers=headers)
            if not track_data:
                continue

            artists = [x.get("name", "") if isinstance(x, dict) else str(x) for x in track_data.get("artists", [])]
            t_name = track_data.get("name", "")
            features = track_data.get("audio_features") or {}

            if match_track(artist, title, t_name, artists):
                tempo = features.get("tempo")
                bpm_val = round(float(tempo), 1) if tempo is not None else None
                key_val = camelot_key(features.get("key"), features.get("mode"))
                candidates.append(Candidate(
                    provider="Audiometa",
                    title=t_name,
                    artists=artists,
                    duration=seconds_from_val(track_data.get("duration_ms")),
                    isrc=(track_data.get("external_ids") or {}).get("isrc"),
                    bpm=bpm_val,
                    key=key_val,
                    url=track_data.get("href")
                ))

        return candidates
