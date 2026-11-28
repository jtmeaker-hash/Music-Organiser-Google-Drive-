"""
Discogs metadata provider client (https://api.discogs.com).
"""
import re
from typing import Any, List, Optional, Tuple

from ..core.matcher import match_track, seconds_from_val
from ..models import Candidate
from .base import BaseProvider, ProviderCallOutcome, ProviderStatus


class DiscogsProvider(BaseProvider):
    """Client for Discogs API (releases, genres/styles, track durations, and labels)."""

    ALLOWS_OPEN_PERSISTENT_CACHE = False
    DEFAULT_CACHE_TTL_SECONDS = 86400

    def __init__(
        self,
        token: Optional[str] = None,
        contact: str = "",
        max_requests: int = 40,
        db: Optional[Any] = None,
        provider_terms_cleared: bool = False,
        default_delay: float = 1.2,
        **kwargs: Any,
    ):
        super().__init__(
            "Discogs",
            default_delay=default_delay,
            max_requests=max_requests,
            contact=contact,
            db=db,
            provider_terms_cleared=provider_terms_cleared,
            **kwargs,
        )
        self.token = (token or "").strip() or None
        self.register_secret(self.token, is_primary_credential=True)

    def verify_connectivity(self) -> ProviderCallOutcome:
        """Verify Discogs token authentication and response schema."""
        if not self.token:
            return ProviderCallOutcome(provider=self.name, status=ProviderStatus.NOT_CONFIGURED, error="Token not set")
        headers = {"Authorization": f"Discogs token={self.token}"}
        outcome = self.request_detailed(
            "https://api.discogs.com/database/search",
            params={"artist": "Headhunterz", "track": "Dragonborn", "type": "release", "per_page": 1},
            headers=headers,
            use_cache=False,
        )
        if outcome.status == ProviderStatus.OK:
            if not isinstance(outcome.data, dict) or not isinstance(outcome.data.get("results"), list):
                return ProviderCallOutcome(
                    provider=self.name,
                    status=ProviderStatus.MALFORMED_JSON,
                    http_status=outcome.http_status,
                    error="Unexpected Discogs response schema (missing 'results')",
                    request_made=True,
                )
            if len(outcome.data["results"]) == 0:
                outcome.status = ProviderStatus.EMPTY
        return outcome

    def search_detailed(self, artist: str, title: str) -> Tuple[List[Candidate], ProviderCallOutcome]:
        if not self.token:
            return [], ProviderCallOutcome(provider=self.name, status=ProviderStatus.NOT_CONFIGURED)

        headers = {"Authorization": f"Discogs token={self.token}"}
        url = "https://api.discogs.com/database/search"
        params = {"artist": artist, "track": title, "type": "release", "per_page": 10}

        outcome = self.request_detailed(url, params=params, headers=headers)
        if outcome.status != ProviderStatus.OK or not isinstance(outcome.data, dict):
            return [], outcome

        results = outcome.data.get("results")
        if not isinstance(results, list):
            return [], ProviderCallOutcome(
                provider=self.name,
                status=ProviderStatus.MALFORMED_JSON,
                http_status=outcome.http_status,
                error="Discogs 'results' field is not a list",
                request_made=outcome.request_made,
            )

        candidates: List[Candidate] = []
        incomplete_out: Optional[ProviderCallOutcome] = None
        for hit in results[:3]:
            if not isinstance(hit, dict):
                continue
            release_id = hit.get("id")
            if not release_id:
                continue

            rel_url = f"https://api.discogs.com/releases/{release_id}"
            rel_out = self.request_detailed(rel_url, headers=headers, allow_404_as_empty=True)
            if rel_out.status != ProviderStatus.OK or not isinstance(rel_out.data, dict):
                if rel_out.status != ProviderStatus.EMPTY:
                    incomplete_out = rel_out
                    break
                continue

            rel_data = rel_out.data
            labels = [
                x.get("name", "") if isinstance(x, dict) else str(x)
                for x in (rel_data.get("labels") or [])
            ]
            styles = [str(s) for s in (rel_data.get("styles") or []) if s]
            genres = [str(g) for g in (rel_data.get("genres") or []) if g]
            combined_genres = list(dict.fromkeys(styles + genres))

            for track_item in (rel_data.get("tracklist") or []):
                if not isinstance(track_item, dict):
                    continue
                t_title = str(track_item.get("title") or "")
                raw_artists = track_item.get("artists") or rel_data.get("artists") or []
                artists = [
                    re.sub(r" \(\d+\)$", "", x.get("name", "") if isinstance(x, dict) else str(x))
                    for x in raw_artists
                ]
                t_dur = seconds_from_val(track_item.get("duration"))

                if match_track(artist, title, t_title, artists):
                    candidates.append(
                        Candidate(
                            provider="Discogs release",
                            title=t_title,
                            artists=artists,
                            duration=t_dur,
                            genre=combined_genres,
                            labels=labels,
                            url=rel_data.get("uri"),
                            source_family="discogs",
                        )
                    )

        if incomplete_out is not None:
            return candidates, incomplete_out

        if not candidates:
            outcome = ProviderCallOutcome(
                provider=self.name,
                status=ProviderStatus.EMPTY,
                http_status=outcome.http_status,
                from_cache=outcome.from_cache,
                request_made=outcome.request_made,
            )
        return candidates, outcome

    def search(self, artist: str, title: str) -> List[Candidate]:
        cands, _ = self.search_detailed(artist, title)
        return cands
