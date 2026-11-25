"""
Base provider client with structured HTTP outcome classification, Retry-After (integer + HTTP-date),
bounded retry sleep budget, terms-aware SQLite/session caching, and secret redaction.
"""
import email.utils
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, Optional
import requests

from ..config import redact_secrets

logger = logging.getLogger("musicvault.providers")


class ProviderStatus(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    AUTH_ERROR = "auth_error"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    TIMEOUT = "timeout"
    MALFORMED_JSON = "malformed_json"
    UNSUPPORTED_ENDPOINT = "unsupported_endpoint"
    SERVER_ERROR = "server_error"
    NETWORK_ERROR = "network_error"
    NOT_CONFIGURED = "not_configured"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass
class ProviderCallOutcome:
    """Structured diagnostic result of a provider API request."""
    provider: str
    status: ProviderStatus
    http_status: Optional[int] = None
    data: Optional[Any] = None
    error: str = ""
    retry_after_seconds: float = 0.0
    from_cache: bool = False
    request_made: bool = False


def parse_retry_after(header_val: Optional[str], max_cap: float = 120.0) -> float:
    """Parse HTTP Retry-After header supporting both integer seconds and RFC 7231 HTTP-date."""
    if not header_val:
        return 0.0
    raw = header_val.strip()
    if not raw:
        return 0.0
    try:
        secs = float(raw)
        return max(0.0, min(max_cap, secs))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, min(max_cap, delta))
    except Exception:
        return 0.0


def canonical_provider_family(provider_name: str) -> str:
    """Resolve provider label to canonical independent source family."""
    p = (provider_name or "").strip().casefold()
    if "sonovault" in p:
        return "sonovault"
    if "audiometa" in p:
        return "audiometa"
    if "discogs" in p:
        return "discogs"
    if "musicbrainz" in p:
        return "musicbrainz"
    if "wikidata" in p or "onlineresearch" in p:
        return "wikidata"
    return p or "unknown"


class BaseProvider:
    """Base HTTP client for music metadata providers."""

    # Subclasses set to True ONLY for open-data licenses (CC0: MusicBrainz, Wikidata)
    ALLOWS_OPEN_PERSISTENT_CACHE: bool = False
    DEFAULT_CACHE_TTL_SECONDS: int = 86400

    def __init__(
        self,
        name: str,
        default_delay: float = 1.0,
        max_requests: int = 50,
        contact: str = "",
        db: Optional[Any] = None,
        provider_terms_cleared: bool = False,
        max_retry_sleep_seconds: float = 15.0,
    ):
        self.name = name
        self.source_family = canonical_provider_family(name)
        self.default_delay = max(0.0, float(default_delay))
        self.max_requests = max(1, int(max_requests))
        self.request_count = 0
        self.is_blocked = False
        self.block_reason: str = ""
        self.db = db
        self.provider_terms_cleared = bool(provider_terms_cleared)
        self.max_retry_sleep_seconds = max(0.0, float(max_retry_sleep_seconds))
        self.retry_sleep_used = 0.0
        self.last_outcome: Optional[ProviderCallOutcome] = None
        self._session_cache: Dict[str, Any] = {}
        self._secrets: list[str] = []

        self.stats: Dict[str, int] = {
            "calls": 0,
            "cache_hits": 0,
            "ok": 0,
            "auth_errors": 0,
            "rate_limited": 0,
            "quota_exceeded": 0,
            "timeouts": 0,
            "malformed_json": 0,
            "unsupported_endpoint": 0,
            "server_errors": 0,
            "network_errors": 0,
        }

        self.session = requests.Session()
        ua = f"MusicVault/2.0.1 ({contact})" if contact else "MusicVault/2.0.1"
        self.session.headers.update({"User-Agent": ua})

    def register_secret(self, secret: Optional[str]) -> None:
        if secret and secret.strip():
            self._secrets.append(secret.strip())

    def _redact(self, msg: Any) -> str:
        return redact_secrets(msg, self._secrets)

    def can_persist_cache(self) -> bool:
        """Check whether provider terms allow writing responses to persistent SQLite cache."""
        return self.ALLOWS_OPEN_PERSISTENT_CACHE or self.provider_terms_cleared

    def can_request(self) -> bool:
        """Check if provider is available and under budget."""
        return not self.is_blocked and self.request_count < self.max_requests

    @staticmethod
    def _build_cache_key(url: str, params: Optional[Dict[str, Any]]) -> str:
        serialized = json.dumps({"u": url, "p": params or {}}, sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _read_cache(self, cache_key: str) -> Optional[Any]:
        if cache_key in self._session_cache:
            return self._session_cache[cache_key]
        if self.db is not None and self.can_persist_cache():
            return self.db.get_cache(self.source_family, cache_key)
        return None

    def _write_cache(self, cache_key: str, payload: Any) -> None:
        self._session_cache[cache_key] = payload
        if self.db is not None and self.can_persist_cache():
            self.db.set_cache(self.source_family, cache_key, payload, ttl_seconds=self.DEFAULT_CACHE_TTL_SECONDS)

    def _sleep_bounded(self, delay: float) -> bool:
        """Sleep up to remaining retry sleep budget; return False if budget would be exceeded."""
        if delay <= 0:
            return True
        if self.retry_sleep_used + delay > self.max_retry_sleep_seconds:
            return False
        time.sleep(delay)
        self.retry_sleep_used += delay
        return True

    def request_detailed(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: int = 20,
        use_cache: bool = True,
    ) -> ProviderCallOutcome:
        """Execute rate-limited HTTP GET with distinct HTTP/error status classification."""
        cache_key = self._build_cache_key(url, params)
        if use_cache:
            cached = self._read_cache(cache_key)
            if cached is not None:
                self.stats["cache_hits"] += 1
                outcome = ProviderCallOutcome(
                    provider=self.name,
                    status=ProviderStatus.OK,
                    http_status=200,
                    data=cached,
                    from_cache=True,
                    request_made=False,
                )
                self.last_outcome = outcome
                return outcome

        if not self.can_request():
            outcome = ProviderCallOutcome(
                provider=self.name,
                status=ProviderStatus.BUDGET_EXHAUSTED,
                error=self.block_reason or "provider blocked or request cap reached",
                request_made=False,
            )
            self.last_outcome = outcome
            return outcome

        merged_headers = dict(self.session.headers)
        if headers:
            merged_headers.update(headers)

        for attempt in range(3):
            if not self.can_request():
                break
            if self.default_delay > 0:
                time.sleep(self.default_delay)

            self.request_count += 1
            self.stats["calls"] += 1
            try:
                resp = self.session.get(url, params=params, headers=merged_headers, timeout=timeout)
                code = resp.status_code
                resp_headers = resp.headers or {}

                # Proactive Discogs / SonoVault rate-limit header pacing
                rem_hdr = resp_headers.get("X-Discogs-Ratelimit-Remaining") or resp_headers.get("RateLimit-Remaining")
                if rem_hdr and str(rem_hdr).isdigit() and int(rem_hdr) <= 1:
                    reset_hdr = resp_headers.get("RateLimit-Reset")
                    pause_s = min(5.0, float(reset_hdr)) if (reset_hdr and str(reset_hdr).isdigit()) else 1.5
                    self._sleep_bounded(pause_s)

                if code in (401, 403):
                    self.is_blocked = True
                    self.block_reason = f"HTTP {code} authentication/permission denied"
                    self.stats["auth_errors"] += 1
                    outcome = ProviderCallOutcome(
                        provider=self.name,
                        status=ProviderStatus.AUTH_ERROR,
                        http_status=code,
                        error=self.block_reason,
                        request_made=True,
                    )
                    self.last_outcome = outcome
                    return outcome

                if code == 402:
                    self.is_blocked = True
                    self.block_reason = "HTTP 402 quota/payment required"
                    self.stats["quota_exceeded"] += 1
                    outcome = ProviderCallOutcome(
                        provider=self.name,
                        status=ProviderStatus.QUOTA_EXCEEDED,
                        http_status=code,
                        error=self.block_reason,
                        request_made=True,
                    )
                    self.last_outcome = outcome
                    return outcome

                if code in (404, 405, 410):
                    self.stats["unsupported_endpoint"] += 1
                    outcome = ProviderCallOutcome(
                        provider=self.name,
                        status=ProviderStatus.UNSUPPORTED_ENDPOINT,
                        http_status=code,
                        error=f"HTTP {code} endpoint not found or unsupported",
                        request_made=True,
                    )
                    self.last_outcome = outcome
                    return outcome

                if code == 429 or (code == 503 and self.source_family == "musicbrainz"):
                    retry_after = parse_retry_after(
                        resp_headers.get("Retry-After") or resp_headers.get("RateLimit-Reset")
                    )
                    wait_s = retry_after if retry_after > 0 else (2.0 * (2 ** attempt))
                    body_low = (resp.text or "")[:300].lower()
                    is_quota = "quota" in body_low or "monthly" in body_low or "plan" in body_low
                    if is_quota:
                        self.is_blocked = True
                        self.block_reason = f"HTTP {code} quota exceeded"
                        self.stats["quota_exceeded"] += 1
                        outcome = ProviderCallOutcome(
                            provider=self.name,
                            status=ProviderStatus.QUOTA_EXCEEDED,
                            http_status=code,
                            error=self.block_reason,
                            retry_after_seconds=wait_s,
                            request_made=True,
                        )
                        self.last_outcome = outcome
                        return outcome

                    self.stats["rate_limited"] += 1
                    if attempt < 2 and self._sleep_bounded(wait_s):
                        continue

                    self.is_blocked = True
                    self.block_reason = f"HTTP {code} rate limited (retry after {wait_s:.0f}s)"
                    outcome = ProviderCallOutcome(
                        provider=self.name,
                        status=ProviderStatus.RATE_LIMITED,
                        http_status=code,
                        error=self.block_reason,
                        retry_after_seconds=wait_s,
                        request_made=True,
                    )
                    self.last_outcome = outcome
                    return outcome

                if code in (500, 502, 503, 504):
                    self.stats["server_errors"] += 1
                    backoff = 2.0 * (2 ** attempt)
                    if attempt < 2 and self._sleep_bounded(backoff):
                        continue
                    self.is_blocked = True
                    self.block_reason = f"HTTP {code} server unavailable"
                    outcome = ProviderCallOutcome(
                        provider=self.name,
                        status=ProviderStatus.SERVER_ERROR,
                        http_status=code,
                        error=self.block_reason,
                        retry_after_seconds=30.0,
                        request_made=True,
                    )
                    self.last_outcome = outcome
                    return outcome

                if resp.ok:
                    try:
                        payload = resp.json()
                    except ValueError as json_exc:
                        self.stats["malformed_json"] += 1
                        outcome = ProviderCallOutcome(
                            provider=self.name,
                            status=ProviderStatus.MALFORMED_JSON,
                            http_status=code,
                            error=f"Malformed JSON response ({self._redact(json_exc)})",
                            request_made=True,
                        )
                        self.last_outcome = outcome
                        return outcome

                    if use_cache:
                        self._write_cache(cache_key, payload)
                    self.stats["ok"] += 1
                    outcome = ProviderCallOutcome(
                        provider=self.name,
                        status=ProviderStatus.OK,
                        http_status=code,
                        data=payload,
                        request_made=True,
                    )
                    self.last_outcome = outcome
                    return outcome

                outcome = ProviderCallOutcome(
                    provider=self.name,
                    status=ProviderStatus.SERVER_ERROR,
                    http_status=code,
                    error=f"HTTP {code}",
                    request_made=True,
                )
                self.last_outcome = outcome
                return outcome

            except requests.Timeout as exc:
                self.stats["timeouts"] += 1
                if attempt < 2 and self._sleep_bounded(1.5 * (2 ** attempt)):
                    continue
                outcome = ProviderCallOutcome(
                    provider=self.name,
                    status=ProviderStatus.TIMEOUT,
                    error=f"Request timeout ({self._redact(exc)})",
                    retry_after_seconds=30.0,
                    request_made=True,
                )
                self.last_outcome = outcome
                return outcome
            except requests.RequestException as exc:
                self.stats["network_errors"] += 1
                if attempt < 2 and self._sleep_bounded(1.5 * (2 ** attempt)):
                    continue
                outcome = ProviderCallOutcome(
                    provider=self.name,
                    status=ProviderStatus.NETWORK_ERROR,
                    error=f"Network error ({self._redact(exc)})",
                    retry_after_seconds=30.0,
                    request_made=True,
                )
                self.last_outcome = outcome
                return outcome

        outcome = ProviderCallOutcome(
            provider=self.name,
            status=ProviderStatus.BUDGET_EXHAUSTED,
            error=self.block_reason or "retry budget exhausted",
            request_made=True,
        )
        self.last_outcome = outcome
        return outcome

    def request(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: int = 20,
    ) -> Optional[Dict[str, Any]]:
        """Backward-compatible wrapper returning parsed JSON dict on HTTP 200 OK or None."""
        res = self.request_detailed(url, params=params, headers=headers, timeout=timeout)
        if res.status == ProviderStatus.OK and isinstance(res.data, dict):
            return res.data
        return None
