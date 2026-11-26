"""
AudioMeta provider client for acoustic features, BPM, and musical key (https://api.audiometa.io/v1).
"""
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from ..core.matcher import camelot_key, match_track, normalize_key_to_camelot, seconds_from_val
from ..models import Candidate
from .base import BaseProvider, ProviderCallOutcome, ProviderStatus


def _extract_track_items(payload: Any) -> List[Dict[str, Any]]:
    """Extract track item dicts across documented AudioMeta payload wrappers."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        tracks_node = payload.get("tracks")
        if isinstance(tracks_node, dict) and isinstance(tracks_node.get("items"), list):
            return [x for x in tracks_node["items"] if isinstance(x, dict)]
        if isinstance(tracks_node, list):
            return [x for x in tracks_node if isinstance(x, dict)]
        for k in ("items", "results", "data", "hits"):
            node = payload.get(k)
            if isinstance(node, list):
                return [x for x in node if isinstance(x, dict)]
            if isinstance(node, dict) and isinstance(node.get("items"), list):
                return [x for x in node["items"] if isinstance(x, dict)]
    return []


class AudioMetaProvider(BaseProvider):
    """Client for AudioMeta API."""

    ALLOWS_OPEN_PERSISTENT_CACHE = False
    DEFAULT_CACHE_TTL_SECONDS = 86400

    def __init__(
        self,
        api_key: Optional[str] = None,
        contact: str = "",
        max_requests: int = 25,
        db: Optional[Any] = None,
        provider_terms_cleared: bool = False,
        default_delay: float = 0.85,
        **kwargs: Any,
    ):
        super().__init__(
            "AudioMeta",
            default_delay=default_delay,
            max_requests=max_requests,
            contact=contact,
            db=db,
            provider_terms_cleared=provider_terms_cleared,
            **kwargs,
        )
        self.api_key = (api_key or "").strip() or None
        self.register_secret(self.api_key, is_primary_credential=True)

    def verify_connectivity(self) -> ProviderCallOutcome:
        """Verify Bearer authentication and endpoint response with a lightweight search probe."""
        if not self.api_key:
            return ProviderCallOutcome(provider=self.name, status=ProviderStatus.NOT_CONFIGURED, error="API key not set")
        headers = {"Authorization": f"Bearer {self.api_key}"}
        outcome = self.request_detailed(
            "https://api.audiometa.io/v1/search",
            params={"q": "Act of Rage Living Fast", "type": "track", "limit": 1},
            headers=headers,
            use_cache=False,
        )
        if outcome.status == ProviderStatus.OK:
            if not isinstance(outcome.data, (dict, list)):
                return ProviderCallOutcome(
                    provider=self.name,
                    status=ProviderStatus.MALFORMED_JSON,
                    http_status=outcome.http_status,
                    error="Unexpected non-object JSON payload",
                    request_made=True,
                )
            items = _extract_track_items(outcome.data)
            if not items:
                outcome.status = ProviderStatus.EMPTY
        return outcome

    def search_detailed(self, artist: str, title: str) -> Tuple[List[Candidate], ProviderCallOutcome]:
        if not self.api_key:
            return [], ProviderCallOutcome(provider=self.name, status=ProviderStatus.NOT_CONFIGURED)

        headers = {"Authorization": f"Bearer {self.api_key}"}
        hits: Dict[str, Dict[str, Any]] = {}
        primary_outcome: Optional[ProviderCallOutcome] = None
        incomplete_outcome: Optional[ProviderCallOutcome] = None

        queries = list(dict.fromkeys(q for q in [f"{artist} {title}".strip(), title, artist] if q.strip()))
        for q in queries[:2]:
            out = self.request_detailed(
                "https://api.audiometa.io/v1/search",
                params={"q": q, "type": "track", "limit": 25},
                headers=headers,
            )
            if primary_outcome is None:
                primary_outcome = out
            if out.status != ProviderStatus.OK:
                if out.status != ProviderStatus.EMPTY:
                    return [], out
                continue

            for item in _extract_track_items(out.data):
                track_id = str(item.get("id") or item.get("track_id") or "").strip()
                t_title = str(item.get("name") or item.get("title") or "")
                artists = [
                    x.get("name", "") if isinstance(x, dict) else str(x)
                    for x in (item.get("artists") or [])
                ]
                if track_id and match_track(artist, title, t_title, artists):
                    hits[track_id] = item
            if hits:
                break

        candidates: List[Candidate] = []
        for track_id, search_item in list(hits.items())[:2]:
            track_data: Dict[str, Any] = search_item
            if "audio_features" not in search_item:
                detail_url = f"https://api.audiometa.io/v1/tracks/{urllib.parse.quote(track_id, safe='')}"
                det_out = self.request_detailed(detail_url, headers=headers, allow_404_as_empty=True)
                if det_out.status == ProviderStatus.OK and isinstance(det_out.data, dict):
                    inner = det_out.data.get("data")
                    track_data = inner if isinstance(inner, dict) else det_out.data
                elif det_out.status != ProviderStatus.EMPTY and incomplete_outcome is None:
                    incomplete_outcome = det_out

            artists = [
                x.get("name", "") if isinstance(x, dict) else str(x)
                for x in (track_data.get("artists") or search_item.get("artists") or [])
            ]
            t_name = str(track_data.get("name") or track_data.get("title") or search_item.get("name") or "")
            features = track_data.get("audio_features") if isinstance(track_data.get("audio_features"), dict) else {}

            if match_track(artist, title, t_name, artists):
                tempo = features.get("tempo") if features.get("tempo") is not None else features.get("bpm")
                bpm_val: Optional[float] = None
                if tempo is not None:
                    try:
                        bpm_val = round(float(tempo), 1)
                    except (ValueError, TypeError):
                        bpm_val = None

                k_raw = features.get("key")
                m_raw = features.get("mode")
                key_val = camelot_key(k_raw, m_raw)
                if not key_val and k_raw is not None:
                    mode_str = str(m_raw or "").strip().lower()
                    combined = f"{k_raw} {mode_str}".strip() if mode_str in ("major", "minor") else str(k_raw)
                    key_val = normalize_key_to_camelot(combined) or None

                ext_ids = track_data.get("external_ids") if isinstance(track_data.get("external_ids"), dict) else {}
                isrc_val = track_data.get("isrc") or ext_ids.get("isrc")

                candidates.append(
                    Candidate(
                        provider="Audiometa",
                        title=t_name,
                        artists=artists,
                        duration=seconds_from_val(track_data.get("duration_ms") or track_data.get("duration")),
                        isrc=isrc_val,
                        bpm=bpm_val,
                        key=key_val,
                        url=track_data.get("href") or track_data.get("url"),
                        source_family="audiometa",
                    )
                )

        if incomplete_outcome is not None:
            return candidates, incomplete_outcome

        final_out = primary_outcome or ProviderCallOutcome(provider=self.name, status=ProviderStatus.EMPTY)
        if final_out.status == ProviderStatus.OK and not candidates:
            final_out = ProviderCallOutcome(
                provider=self.name,
                status=ProviderStatus.EMPTY,
                http_status=final_out.http_status,
                from_cache=final_out.from_cache,
                request_made=final_out.request_made,
            )
        return candidates, final_out

    def search(self, artist: str, title: str) -> List[Candidate]:
        cands, _ = self.search_detailed(artist, title)
        return cands
