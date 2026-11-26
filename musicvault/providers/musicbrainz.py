"""
MusicBrainz provider client for open music metadata, genres, tags, and ISRCs.
Adheres strictly to MusicBrainz 1 req/sec rate etiquette and descriptive User-Agent requirement.
"""
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from ..core.matcher import match_track, norm, seconds_from_val
from ..models import Candidate
from .base import BaseProvider, ProviderCallOutcome, ProviderStatus


class MusicBrainzProvider(BaseProvider):
    """Client for MusicBrainz Web Service v2 (CC0 core metadata)."""

    ALLOWS_OPEN_PERSISTENT_CACHE = True
    DEFAULT_CACHE_TTL_SECONDS = 7 * 86400
    MIN_REQUIRED_DELAY_SECONDS = 1.1

    def __init__(
        self,
        contact: str = "",
        max_requests: int = 30,
        db: Optional[Any] = None,
        provider_terms_cleared: bool = False,
        default_delay: float = 1.5,
        **kwargs: Any,
    ):
        # Enforce MusicBrainz >= 1 req/sec etiquette unconditionally (never allow zero-delay bypass)
        enforced_delay = max(self.MIN_REQUIRED_DELAY_SECONDS, float(default_delay))
        super().__init__(
            "MusicBrainz",
            default_delay=enforced_delay,
            max_requests=max_requests,
            contact=contact,
            db=db,
            provider_terms_cleared=provider_terms_cleared,
            **kwargs,
        )

    def search_detailed(
        self, artist: str, title: str, isrc: Optional[str] = None
    ) -> Tuple[List[Candidate], ProviderCallOutcome]:
        base = "https://musicbrainz.org/ws/2/recording"
        hits: Dict[str, Tuple[Dict[str, Any], List[str]]] = {}
        primary_out: Optional[ProviderCallOutcome] = None
        incomplete_out: Optional[ProviderCallOutcome] = None

        queries: List[str] = []
        if isrc and isrc.strip():
            queries.append(f"isrc:{isrc.strip()}")
        clean_title = title.replace('"', " ").strip()
        clean_artist = artist.replace('"', " ").strip()
        if clean_title and clean_artist:
            queries.append(f'recording:"{clean_title}" AND artist:"{clean_artist}"')

        for idx, q in enumerate(queries):
            out = self.request_detailed(base, params={"query": q, "fmt": "json", "limit": 15})
            if primary_out is None:
                primary_out = out
            if out.status != ProviderStatus.OK or not isinstance(out.data, dict):
                if out.status != ProviderStatus.EMPTY:
                    incomplete_out = out
                    return [], out
                continue

            for rec in out.data.get("recordings", []):
                if not isinstance(rec, dict) or not rec.get("id"):
                    continue
                artists = [
                    v.get("artist", {}).get("name", v.get("name", ""))
                    for v in (rec.get("artist-credit") or [])
                    if isinstance(v, dict)
                ]
                rec_title = str(rec.get("title") or "")
                if match_track(artist, title, rec_title, artists):
                    hits[str(rec["id"])] = (rec, artists)
            if hits:
                break

        candidates: List[Candidate] = []
        for rec_id, (rec, artists) in list(hits.items())[:3]:
            detail_url = f"{base}/{urllib.parse.quote(rec_id, safe='')}"
            det_out = self.request_detailed(
                detail_url,
                params={"fmt": "json", "inc": "isrcs+genres+tags+releases"},
                allow_404_as_empty=True,
            )
            if det_out.status == ProviderStatus.OK and isinstance(det_out.data, dict):
                detail = det_out.data
            else:
                if det_out.status != ProviderStatus.EMPTY and incomplete_out is None:
                    incomplete_out = det_out
                detail = rec

            tags: List[str] = []
            for field_name in ("genres", "tags"):
                for item in (detail.get(field_name) or []):
                    if isinstance(item, dict) and item.get("count", 1) > 0 and item.get("name"):
                        tags.append(str(item["name"]))

            isrc_list = [str(x) for x in (detail.get("isrcs") or []) if x]
            matched_isrc: Optional[str] = None
            if isrc and any(norm(isrc) == norm(x) for x in isrc_list):
                matched_isrc = isrc
            elif isrc_list:
                matched_isrc = isrc_list[0]

            candidates.append(
                Candidate(
                    provider="MusicBrainz",
                    title=str(rec.get("title") or ""),
                    artists=artists,
                    duration=seconds_from_val(detail.get("length") or rec.get("length")),
                    isrc=matched_isrc,
                    genre=tags,
                    url=f"https://musicbrainz.org/recording/{rec_id}",
                    source_family="musicbrainz",
                )
            )
            if incomplete_out is not None:
                break

        if incomplete_out is not None:
            return candidates, incomplete_out

        final_out = primary_out or ProviderCallOutcome(provider=self.name, status=ProviderStatus.EMPTY)
        if final_out.status == ProviderStatus.OK and not candidates:
            final_out = ProviderCallOutcome(
                provider=self.name,
                status=ProviderStatus.EMPTY,
                http_status=final_out.http_status,
                from_cache=final_out.from_cache,
                request_made=final_out.request_made,
            )
        return candidates, final_out

    def search_title_only(self, title_hint: str) -> Tuple[List[Candidate], ProviderCallOutcome]:
        """Optional review-only lookup for title-only tracks (never auto-confirms identity)."""
        clean_title = (title_hint or "").replace('"', " ").strip()
        if not clean_title:
            return [], ProviderCallOutcome(provider=self.name, status=ProviderStatus.EMPTY)

        base = "https://musicbrainz.org/ws/2/recording"
        out = self.request_detailed(base, params={"query": f'recording:"{clean_title}"', "fmt": "json", "limit": 5})
        if out.status != ProviderStatus.OK or not isinstance(out.data, dict):
            return [], out

        cands: List[Candidate] = []
        for rec in (out.data.get("recordings") or [])[:3]:
            if not isinstance(rec, dict):
                continue
            rec_title = str(rec.get("title") or "")
            if norm(rec_title) != norm(clean_title):
                continue
            artists = [
                v.get("artist", {}).get("name", v.get("name", ""))
                for v in (rec.get("artist-credit") or [])
                if isinstance(v, dict)
            ]
            cands.append(
                Candidate(
                    provider="MusicBrainz",
                    title=rec_title,
                    artists=artists,
                    duration=seconds_from_val(rec.get("length")),
                    url=f"https://musicbrainz.org/recording/{rec.get('id', '')}",
                    verification="Title-only review",
                    match_confidence="title_only_unverified",
                    source_family="musicbrainz",
                )
            )
        if not cands:
            out = ProviderCallOutcome(
                provider=self.name,
                status=ProviderStatus.EMPTY,
                http_status=out.http_status,
                from_cache=out.from_cache,
                request_made=out.request_made,
            )
        return cands, out

    def search(self, artist: str, title: str, isrc: Optional[str] = None) -> List[Candidate]:
        cands, _ = self.search_detailed(artist, title, isrc=isrc)
        return cands
