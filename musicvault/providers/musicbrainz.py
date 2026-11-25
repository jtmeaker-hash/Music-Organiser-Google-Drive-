"""
MusicBrainz provider client for open music metadata, genres, tags, and ISRCs.
"""
from typing import List, Optional
from ..models import Candidate
from ..core.matcher import match_track, seconds_from_val
from .base import BaseProvider


class MusicBrainzProvider(BaseProvider):
    """Client for MusicBrainz Web Service (requires respectful rate limit and contact header)."""

    def __init__(self, contact: str = "", max_requests: int = 30):
        super().__init__("MusicBrainz", default_delay=2.0, max_requests=max_requests, contact=contact)

    def search(self, artist: str, title: str, isrc: Optional[str] = None) -> List[Candidate]:
        if not self.can_request():
            return []

        base = "https://musicbrainz.org/ws/2/recording"
        hits = {}

        queries = []
        if isrc:
            queries.append(f"isrc:{isrc}")
        clean_title = title.replace('"', ' ')
        clean_artist = artist.replace('"', ' ')
        queries.append(f'recording:"{clean_title}" AND artist:"{clean_artist}"')

        for q in queries:
            if not self.can_request():
                break
            params = {"query": q, "fmt": "json", "limit": 15}
            data = self.request(base, params=params)
            if not data:
                continue
            for rec in data.get("recordings", []):
                artists = [
                    v.get("artist", {}).get("name", v.get("name", ""))
                    for v in rec.get("artist-credit", [])
                    if isinstance(v, dict)
                ]
                rec_title = rec.get("title", "")
                if match_track(artist, title, rec_title, artists):
                    hits[rec["id"]] = (rec, artists)

        candidates = []
        for rec_id, (rec, artists) in list(hits.items())[:3]:
            if not self.can_request():
                break
            detail_url = f"{base}/{rec_id}"
            params = {"fmt": "json", "inc": "isrcs+genres+tags"}
            detail = self.request(detail_url, params=params)
            if not detail:
                continue

            tags = []
            for tag_list in (detail.get("genres", []), detail.get("tags", [])):
                for item in tag_list:
                    if item.get("count", 1) > 0 and item.get("name"):
                        tags.append(item["name"])

            matched_isrc = isrc if (isrc and isrc in detail.get("isrcs", [])) else None
            if not matched_isrc and detail.get("isrcs"):
                matched_isrc = detail["isrcs"][0]

            candidates.append(Candidate(
                provider="MusicBrainz",
                title=rec.get("title", ""),
                artists=artists,
                duration=seconds_from_val(rec.get("length")),
                isrc=matched_isrc,
                genre=tags,
                url=f"https://musicbrainz.org/recording/{rec_id}"
            ))

        return candidates
