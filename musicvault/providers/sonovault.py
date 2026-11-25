"""
SonoVault metadata provider client (https://api.sonovault.now/v1/tracks/search).
"""
import urllib.parse
from typing import Any, List, Optional, Tuple

from ..core.matcher import match_track, norm, normalize_key_to_camelot, seconds_from_val
from ..models import Candidate
from .base import BaseProvider, ProviderCallOutcome, ProviderStatus


def _as_str_list(val: Any) -> List[str]:
    if val is None:
        return []
    if isinstance(val, str):
        return [val.strip()] if val.strip() else []
    if isinstance(val, (list, tuple)):
        out: List[str] = []
        for item in val:
            if isinstance(item, dict):
                n = str(item.get("name") or "").strip()
                if n:
                    out.append(n)
            elif str(item).strip():
                out.append(str(item).strip())
        return out
    return []


class SonoVaultProvider(BaseProvider):
    """Client for SonoVault API (BPM, Key, Genre, ISRC)."""

    ALLOWS_OPEN_PERSISTENT_CACHE = False
    DEFAULT_CACHE_TTL_SECONDS = 86400

    def __init__(
        self,
        api_key: Optional[str] = None,
        contact: str = "",
        max_requests: int = 25,
        db: Optional[Any] = None,
        provider_terms_cleared: bool = False,
        default_delay: float = 3.2,
    ):
        super().__init__(
            "SonoVault",
            default_delay=default_delay,
            max_requests=max_requests,
            contact=contact,
            db=db,
            provider_terms_cleared=provider_terms_cleared,
        )
        self.api_key = (api_key or "").strip() or None
        self.register_secret(self.api_key)

    def _parse_item_to_candidate(self, item: dict) -> Candidate:
        artists = [
            x.get("name", "") if isinstance(x, dict) else str(x)
            for x in (item.get("artists") or [])
        ]
        t_name = str(item.get("title") or "")
        genres = _as_str_list(item.get("genre")) + _as_str_list(item.get("subgenre"))
        labels: List[str] = []
        for rel in (item.get("releases") or []):
            if isinstance(rel, dict):
                lbl = rel.get("label")
                labels.extend(_as_str_list(lbl))

        raw_key = item.get("key_alphanumeric") or item.get("key_open") or item.get("key_musical")
        key_cam = normalize_key_to_camelot(raw_key) or (str(raw_key).strip() if raw_key else None)

        bpm_val: Optional[float] = None
        if item.get("bpm") is not None:
            try:
                bpm_val = round(float(item["bpm"]), 1)
            except (ValueError, TypeError):
                bpm_val = None

        return Candidate(
            provider="SonoVault",
            title=t_name,
            artists=artists,
            duration=seconds_from_val(item.get("duration_ms") or item.get("duration")),
            isrc=item.get("isrc"),
            bpm=bpm_val,
            key=key_cam,
            genre=genres,
            labels=labels,
            url=item.get("url"),
            source_family="sonovault",
        )

    def verify_connectivity(self) -> ProviderCallOutcome:
        """Verify API key authentication and response schema with a single probe request."""
        if not self.api_key:
            return ProviderCallOutcome(provider=self.name, status=ProviderStatus.NOT_CONFIGURED, error="API key not set")
        url = "https://api.sonovault.now/v1/tracks/search"
        headers = {"x-api-key": self.api_key}
        outcome = self.request_detailed(
            url, params={"artist": "Daft Punk", "title": "One More Time", "limit": 1}, headers=headers, use_cache=False
        )
        if outcome.status == ProviderStatus.OK:
            if not isinstance(outcome.data, dict) or not isinstance(outcome.data.get("results"), list):
                return ProviderCallOutcome(
                    provider=self.name,
                    status=ProviderStatus.MALFORMED_JSON,
                    http_status=outcome.http_status,
                    error="Unexpected response schema (missing 'results' list)",
                    request_made=True,
                )
            if len(outcome.data["results"]) == 0:
                outcome.status = ProviderStatus.EMPTY
        return outcome

    def search_detailed(
        self, artist: str, title: str, isrc: Optional[str] = None
    ) -> Tuple[List[Candidate], ProviderCallOutcome]:
        if not self.api_key:
            return [], ProviderCallOutcome(provider=self.name, status=ProviderStatus.NOT_CONFIGURED)

        headers = {"x-api-key": self.api_key}
        candidates: List[Candidate] = []

        # 1. Primary track search
        query_artist = "Rebelion" if norm(artist) == "rebellion" else artist
        url = "https://api.sonovault.now/v1/tracks/search"
        params = {"artist": query_artist, "title": title, "limit": 10}
        outcome = self.request_detailed(url, params=params, headers=headers)

        if outcome.status == ProviderStatus.OK and isinstance(outcome.data, dict):
            results = outcome.data.get("results")
            if isinstance(results, list):
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    cand = self._parse_item_to_candidate(item)
                    if match_track(artist, title, cand.title, cand.artists):
                        candidates.append(cand)

        # 2. Exact ISRC lookup fallback when local ISRC is known and search found nothing
        if not candidates and isrc and self.can_request() and outcome.status in (ProviderStatus.OK, ProviderStatus.EMPTY):
            isrc_url = f"https://api.sonovault.now/v1/tracks/isrc/{urllib.parse.quote(isrc.strip(), safe='')}"
            isrc_out = self.request_detailed(isrc_url, headers=headers)
            if isrc_out.status == ProviderStatus.OK and isinstance(isrc_out.data, dict):
                item_obj = isrc_out.data.get("track") if isinstance(isrc_out.data.get("track"), dict) else isrc_out.data
                if isinstance(item_obj, dict) and item_obj.get("title"):
                    cand = self._parse_item_to_candidate(item_obj)
                    if match_track(artist, title, cand.title, cand.artists):
                        candidates.append(cand)

        if outcome.status == ProviderStatus.OK and not candidates:
            outcome = ProviderCallOutcome(
                provider=self.name,
                status=ProviderStatus.EMPTY,
                http_status=outcome.http_status,
                from_cache=outcome.from_cache,
                request_made=outcome.request_made,
            )
        return candidates, outcome

    def search(self, artist: str, title: str, isrc: Optional[str] = None) -> List[Candidate]:
        cands, _ = self.search_detailed(artist, title, isrc=isrc)
        return cands
