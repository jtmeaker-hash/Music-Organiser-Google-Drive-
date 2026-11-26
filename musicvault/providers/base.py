"""
Base provider client with separated cooldowns, hard auth/quota blocks, and request caps,
provider-specific Retry-After / RateLimit-Reset parsing without shortening server waits,
monotonic pacing, injectable clocks/sleep, persisted minimal operational state,
terms-aware SQLite/session caching, and secret/contact redaction.
"""
import email.utils
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, Iterable, Mapping, Optional
import requests

from .. import __version__
from ..config import DEFAULT_CONTACT, redact_secrets

logger = logging.getLogger("musicvault.providers")

_EPOCH_SECONDS_THRESHOLD = 1_000_000_000.0
_EPOCH_MILLIS_THRESHOLD = 1_000_000_000_000.0

_DOCUMENTED_QUOTA_CODES = {
    "quota_exceeded",
    "insufficient_quota",
    "monthly_quota_exceeded",
    "daily_quota_exceeded",
    "annual_quota_exceeded",
    "plan_limit_exceeded",
    "billing_limit_exceeded",
    "billing_hard_limit_reached",
}

_DOCUMENTED_QUOTA_MSG_RE = re.compile(
    r"\b(?:"
    r"(?:monthly|daily|annual|billing|plan|account)\s+(?:plan\s+)?(?:api\s+)?quota\s+(?:exceeded|exhausted|reached)"
    r"|insufficient[_\s]+quota"
    r"|quota\s+(?:exceeded|exhausted)"
    r")\b",
    re.IGNORECASE,
)


class ProviderStatus(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    AUTH_ERROR = "auth_error"
    RATE_LIMITED = "rate_limited"
    COOLDOWN_DEFERRED = "cooldown_deferred"
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
    retry_until_utc: str = ""
    error_kind: str = ""
    deferred_by_cooldown: bool = False
    hard_blocked: bool = False


def _default_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _get_header_ci(headers: Optional[Mapping[str, Any]], target: str) -> Optional[str]:
    if not headers:
        return None
    val = headers.get(target)
    if val is not None:
        return str(val)
    target_low = target.lower()
    for k, v in headers.items():
        if str(k).lower() == target_low and v is not None:
            return str(v)
    return None


def parse_retry_after(
    header_val: Optional[str],
    max_cap: Optional[float] = None,
    now_utc: Optional[datetime] = None,
) -> float:
    """
    Parse RFC 9110 Retry-After header supporting delta-seconds and HTTP-date.
    Does NOT artificially cap/shorten server waits unless caller explicitly passes max_cap.
    """
    if not header_val:
        return 0.0
    raw = str(header_val).strip()
    if not raw:
        return 0.0

    delta: Optional[float] = None
    try:
        secs = float(raw)
        if secs >= 0.0:
            delta = secs
    except ValueError:
        pass

    if delta is None:
        try:
            dt = email.utils.parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ref_now = now_utc or _default_utc_now()
            if ref_now.tzinfo is None:
                ref_now = ref_now.replace(tzinfo=timezone.utc)
            delta = max(0.0, (dt - ref_now).total_seconds())
        except Exception:
            return 0.0

    if max_cap is not None:
        return max(0.0, min(float(max_cap), delta))
    return max(0.0, delta)


def parse_provider_reset_wait(
    headers: Optional[Mapping[str, Any]],
    source_family: str,
    now_utc: Optional[datetime] = None,
) -> Optional[float]:
    """
    Parse server-communicated retry/reset delay according to RFC 9110 and provider-specific
    header semantics without shortening long server waits:
      1. Retry-After: RFC 9110 delta-seconds or HTTP-date (authoritative across all providers).
      2. X-RateLimit-Reset: Unix epoch seconds on MusicBrainz (and any epoch >= 1e9);
         delta-seconds only if < 1e9 on non-MusicBrainz gateways.
      3. RateLimit-Reset: IETF delta-seconds (with epoch >= 1e9 safety conversion if a gateway
         non-conformantly emits Unix epoch timestamps).
      4. Discogs X-Discogs-Ratelimit-Remaining == 0 fallback: 60s moving window when no
         explicit Retry-After/RateLimit-Reset header is present.
    """
    if not headers:
        return None
    ref_now = now_utc or _default_utc_now()
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)
    else:
        ref_now = ref_now.astimezone(timezone.utc)
    now_epoch = ref_now.timestamp()

    positive_waits: list[float] = []
    explicit_zero = False

    retry_hdr = _get_header_ci(headers, "Retry-After")
    if retry_hdr is not None and retry_hdr.strip():
        ra = parse_retry_after(retry_hdr, max_cap=None, now_utc=ref_now)
        if ra > 0.0:
            positive_waits.append(ra)
        elif retry_hdr.strip() == "0":
            explicit_zero = True

    x_reset = _get_header_ci(headers, "X-RateLimit-Reset")
    if x_reset is not None and x_reset.strip():
        raw_x = x_reset.strip()
        try:
            val_x = float(raw_x)
            if val_x >= _EPOCH_MILLIS_THRESHOLD:
                wx = max(0.0, (val_x / 1000.0) - now_epoch)
            elif source_family == "musicbrainz" or val_x >= _EPOCH_SECONDS_THRESHOLD:
                wx = max(0.0, val_x - now_epoch)
            elif val_x >= 0.0:
                wx = val_x
            else:
                wx = 0.0
            if wx > 0.0:
                positive_waits.append(wx)
            elif raw_x == "0":
                explicit_zero = True
        except ValueError:
            ra_x = parse_retry_after(raw_x, max_cap=None, now_utc=ref_now)
            if ra_x > 0.0:
                positive_waits.append(ra_x)

    ietf_reset = _get_header_ci(headers, "RateLimit-Reset")
    if ietf_reset is not None and ietf_reset.strip():
        raw_r = ietf_reset.strip()
        try:
            val_r = float(raw_r)
            if val_r >= _EPOCH_MILLIS_THRESHOLD:
                wr = max(0.0, (val_r / 1000.0) - now_epoch)
            elif val_r >= _EPOCH_SECONDS_THRESHOLD:
                wr = max(0.0, val_r - now_epoch)
            elif val_r >= 0.0:
                wr = val_r
            else:
                wr = 0.0
            if wr > 0.0:
                positive_waits.append(wr)
            elif raw_r == "0":
                explicit_zero = True
        except ValueError:
            ra_r = parse_retry_after(raw_r, max_cap=None, now_utc=ref_now)
            if ra_r > 0.0:
                positive_waits.append(ra_r)

    if source_family == "discogs":
        dc_rem = _get_header_ci(headers, "X-Discogs-Ratelimit-Remaining")
        if dc_rem is not None and dc_rem.strip().isdigit() and int(dc_rem.strip()) == 0:
            positive_waits.append(60.0)

    if positive_waits:
        # Never let Retry-After: 0 or a shorter header mask a longer server-required reset window
        return max(positive_waits)
    if explicit_zero:
        return 0.0
    return None


def _is_documented_quota_exceeded(
    code: int,
    headers: Optional[Mapping[str, Any]],
    resp_text: str,
    parsed_json: Any = None,
) -> bool:
    """
    Detect hard plan/billing quota exhaustion from documented HTTP status, quota headers,
    structured JSON error codes, or explicit quota phrases (avoiding naive substring false
    positives such as 'plan' inside 'explanation').
    """
    if code == 402:
        return True

    for q_hdr in ("X-Quota-Remaining", "X-Monthly-Quota-Remaining", "X-Daily-Quota-Remaining"):
        hv = _get_header_ci(headers, q_hdr)
        if hv is not None and hv.strip() == "0":
            return True

    candidates_text: list[str] = []
    if isinstance(parsed_json, dict):
        nodes = [parsed_json]
        if isinstance(parsed_json.get("error"), dict):
            nodes.append(parsed_json["error"])
        for node in nodes:
            for code_key in ("code", "error_code", "type", "status", "reason"):
                raw_c = node.get(code_key)
                if isinstance(raw_c, str) and raw_c.strip().lower() in _DOCUMENTED_QUOTA_CODES:
                    return True
            for msg_key in ("error", "message", "detail", "description"):
                raw_m = node.get(msg_key)
                if isinstance(raw_m, str) and raw_m.strip():
                    candidates_text.append(raw_m.strip())
    elif resp_text:
        # Try parsing small JSON error bodies if caller didn't pre-parse
        stripped = resp_text.strip()
        if stripped.startswith("{") and len(stripped) <= 4096:
            try:
                j_obj = json.loads(stripped)
                return _is_documented_quota_exceeded(code, headers, "", parsed_json=j_obj)
            except ValueError:
                candidates_text.append(stripped[:400])
        else:
            candidates_text.append(stripped[:400])

    for txt in candidates_text:
        if _DOCUMENTED_QUOTA_MSG_RE.search(txt):
            return True
    return False


def _is_musicbrainz_503_rate_limit(
    source_family: str,
    code: int,
    headers: Optional[Mapping[str, Any]],
    resp_text: str,
) -> bool:
    """Distinguish MusicBrainz HTTP 503 rate-limiting from generic 503 server maintenance."""
    if source_family != "musicbrainz" or code != 503:
        return False
    if _get_header_ci(headers, "Retry-After") or _get_header_ci(headers, "X-RateLimit-Reset"):
        return True
    x_rem = _get_header_ci(headers, "X-RateLimit-Remaining")
    if x_rem is not None and x_rem.strip() == "0":
        return True
    body_low = (resp_text or "")[:500].lower()
    if "rate limit" in body_low or "exceeding the allowable rate" in body_low:
        return True
    return False


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
        clock_mono: Optional[Callable[[], float]] = None,
        clock_utc: Optional[Callable[[], datetime]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ):
        self.name = name
        self.source_family = canonical_provider_family(name)
        self.default_delay = max(0.0, float(default_delay))
        self.max_requests = max(1, int(max_requests))
        self.request_count = 0
        self.request_monitor = None
        self.db = db
        self.provider_terms_cleared = bool(provider_terms_cleared)
        self.max_retry_sleep_seconds = max(0.0, float(max_retry_sleep_seconds))
        self.retry_sleep_used = 0.0
        self.last_outcome: Optional[ProviderCallOutcome] = None
        self._session_cache: Dict[str, Any] = {}
        self._secrets: list[str] = []

        self._clock_mono: Callable[[], float] = clock_mono or time.monotonic
        self._clock_utc: Callable[[], datetime] = clock_utc or _default_utc_now
        self._sleep_fn: Callable[[float], None] = sleep_fn or time.sleep

        # Separated operational states:
        # 1. Hard block (auth error, hard quota without short retry, unsupported search endpoint)
        self.hard_blocked: bool = False
        self.hard_block_status: ProviderStatus = ProviderStatus.AUTH_ERROR
        self.hard_block_kind: str = ""
        self.hard_block_reason: str = ""

        # 2. Timed cooldown (429, MusicBrainz 503 rate-limit, 5xx/timeout backoff, or proactive 0-remaining window)
        self._cooldown_until_mono: float = 0.0
        self._cooldown_until_utc: str = ""
        self._cooldown_reason: str = ""
        self._cooldown_kind: str = ""

        # 3. Monotonic same-provider inter-request pacing deadline
        self._next_pacing_mono: float = 0.0
        self._cred_fingerprint: str = ""
        self._quota_reset_utc = ""

        if contact and contact.strip() and contact.strip() != DEFAULT_CONTACT:
            self.register_secret(contact.strip())

        self.stats: Dict[str, int] = {
            "calls": 0,
            "cache_hits": 0,
            "ok": 0,
            "auth_errors": 0,
            "rate_limited": 0,
            "mb_503_rate_limited": 0,
            "quota_exceeded": 0,
            "timeouts": 0,
            "malformed_json": 0,
            "unsupported_endpoint": 0,
            "server_errors": 0,
            "network_errors": 0,
            "skipped_cooldown": 0,
            "skipped_blocked": 0,
            "skipped_budget": 0,
        }

        self.session = requests.Session()
        ua = f"MusicVault/{__version__} ({contact})" if contact else f"MusicVault/{__version__}"
        self.session.headers.update({"User-Agent": ua})

        self._load_persisted_operational_state()

    @property
    def is_blocked(self) -> bool:
        """Backward-compatible check: True if hard-blocked or currently in active cooldown."""
        return self.hard_blocked or (self.remaining_cooldown_seconds() > 0.0)

    @is_blocked.setter
    def is_blocked(self, val: bool) -> None:
        self.hard_blocked = bool(val)
        if not val:
            self._cooldown_until_mono = 0.0
            self._cooldown_until_utc = ""

    @property
    def block_reason(self) -> str:
        if self.hard_blocked:
            return self.hard_block_reason
        if self.remaining_cooldown_seconds() > 0.0:
            return self._cooldown_reason
        return ""

    @block_reason.setter
    def block_reason(self, reason: str) -> None:
        self.hard_block_reason = str(reason or "")

    @property
    def cooldown_until_utc(self) -> str:
        return self._cooldown_until_utc if self.remaining_cooldown_seconds() > 0.0 else ""

    def total_skipped_calls(self) -> int:
        return (
            int(self.stats.get("skipped_cooldown", 0))
            + int(self.stats.get("skipped_blocked", 0))
            + int(self.stats.get("skipped_budget", 0))
        )

    def _now_utc_normalized(self) -> datetime:
        dt = self._clock_utc()
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _load_persisted_operational_state(self) -> None:
        """Hydrate active provider cooldown and recent-request pacing from SQLite operational state."""
        if self.db is None or not hasattr(self.db, "get_provider_operational_state"):
            return
        try:
            st = self.db.get_provider_operational_state(self.source_family)
        except Exception:
            return
        if not st:
            return

        now_utc = self._now_utc_normalized()
        now_mono = self._clock_mono()

        cd_raw = str(st.get("cooldown_until_utc") or "").strip()
        stored_fp = str(st.get("cred_fingerprint") or "").strip()
        last_st = str(st.get("last_status") or "").strip()
        is_cred_scoped_block = last_st in (
            ProviderStatus.QUOTA_EXCEEDED.value,
            ProviderStatus.AUTH_ERROR.value,
        )
        # Credential-scoped quota blocks apply only to the same credential fingerprint;
        # server/IP rate-limit cooldowns apply unconditionally across restarts.
        cred_mismatch = bool(
            is_cred_scoped_block
            and self._cred_fingerprint
            and stored_fp
            and self._cred_fingerprint != stored_fp
        )
        if cd_raw and not cred_mismatch:
            try:
                cd_dt = datetime.fromisoformat(cd_raw.replace("Z", "+00:00"))
                if cd_dt.tzinfo is None:
                    cd_dt = cd_dt.replace(tzinfo=timezone.utc)
                rem = (cd_dt - now_utc).total_seconds()
                if rem > 0.0:
                    self._cooldown_until_mono = now_mono + rem
                    self._cooldown_until_utc = cd_dt.isoformat()
                    self._cooldown_reason = str(st.get("reason") or "persisted provider cooldown")
                    self._cooldown_kind = str(st.get("error_kind") or "provider_cooldown")
                    if last_st == ProviderStatus.QUOTA_EXCEEDED.value:
                        self.hard_blocked = True
                        self.hard_block_status = ProviderStatus.QUOTA_EXCEEDED
                        self._quota_reset_utc = cd_dt.isoformat()
                        self.hard_block_kind = "quota_exceeded"
                        self.hard_block_reason = self._cooldown_reason
            except ValueError:
                pass

        if not cd_raw and not cred_mismatch and is_cred_scoped_block:
            self.hard_blocked = True
            self.hard_block_status = ProviderStatus(last_st)
            self.hard_block_kind = str(st.get("error_kind") or last_st)
            self.hard_block_reason = str(st.get("reason") or last_st)

        last_req_raw = str(st.get("last_request_utc") or "").strip()
        if last_req_raw and self.default_delay > 0.0:
            try:
                lr_dt = datetime.fromisoformat(last_req_raw.replace("Z", "+00:00"))
                if lr_dt.tzinfo is None:
                    lr_dt = lr_dt.replace(tzinfo=timezone.utc)
                elapsed = (now_utc - lr_dt).total_seconds()
                if 0.0 <= elapsed < self.default_delay:
                    self._next_pacing_mono = max(self._next_pacing_mono, now_mono + (self.default_delay - elapsed))
            except ValueError:
                pass

    def _persist_operational_state(
        self,
        cooldown_until_utc: Optional[str] = None,
        last_request_utc: Optional[str] = None,
        last_status: str = "",
        last_http_status: Optional[int] = None,
        error_kind: str = "",
        reason: str = "",
    ) -> None:
        if self.db is None or not hasattr(self.db, "set_provider_operational_state"):
            return
        try:
            self.db.set_provider_operational_state(
                provider=self.source_family,
                cooldown_until_utc=cooldown_until_utc,
                last_request_utc=last_request_utc,
                last_status=last_status,
                last_http_status=last_http_status,
                error_kind=error_kind,
                reason=self._redact(reason)[:300],
                cred_fingerprint=self._cred_fingerprint or None,
                now_utc=self._now_utc_normalized(),
            )
        except Exception:
            pass

    def remaining_cooldown_seconds(self) -> float:
        if self._cooldown_until_mono <= 0.0:
            return 0.0
        rem = self._cooldown_until_mono - self._clock_mono()
        if rem <= 0.0:
            self._cooldown_until_mono = 0.0
            self._cooldown_until_utc = ""
            self._cooldown_reason = ""
            self._cooldown_kind = ""
            return 0.0
        return rem

    def _set_cooldown(
        self,
        wait_seconds: float,
        status: str,
        http_status: Optional[int],
        error_kind: str,
        reason: str,
    ) -> str:
        wait_s = max(0.0, float(wait_seconds))
        if wait_s <= 0.0:
            return ""
        now_mono = self._clock_mono()
        now_utc = self._now_utc_normalized()
        target_mono = now_mono + wait_s
        target_utc = (now_utc + timedelta(seconds=wait_s)).isoformat()
        if target_mono >= self._cooldown_until_mono:
            self._cooldown_until_mono = target_mono
            self._cooldown_until_utc = target_utc
            self._cooldown_reason = self._redact(reason)
            self._cooldown_kind = error_kind
        self._persist_operational_state(
            cooldown_until_utc=self._cooldown_until_utc,
            last_status=status,
            last_http_status=http_status,
            error_kind=error_kind,
            reason=reason,
        )
        return self._cooldown_until_utc

    def _clear_cooldown_after_ok(self, http_status: int) -> None:
        if self._cooldown_until_mono > 0.0 or self._cooldown_until_utc:
            self._cooldown_until_mono = 0.0
            self._cooldown_until_utc = ""
            self._cooldown_reason = ""
            self._cooldown_kind = ""
        self._persist_operational_state(
            cooldown_until_utc="",
            last_status=ProviderStatus.OK.value,
            last_http_status=http_status,
            error_kind="",
            reason="",
        )

    def register_secret(self, secret: Optional[str], is_primary_credential: bool = False) -> None:
        if secret and secret.strip():
            clean = secret.strip()
            self._secrets.append(clean)
            if is_primary_credential:
                self._cred_fingerprint = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:16]
                # Re-evaluate persisted state now that primary credential fingerprint is known
                self.hard_blocked = False
                self.hard_block_reason = ""
                self.hard_block_kind = ""
                self._cooldown_until_mono = 0.0
                self._cooldown_until_utc = ""
                self._quota_reset_utc = ""
                self._load_persisted_operational_state()

    def _redact(self, msg: Any) -> str:
        return redact_secrets(msg, self._secrets)

    def can_persist_cache(self) -> bool:
        """Check whether provider terms allow writing responses to persistent SQLite cache."""
        return self.ALLOWS_OPEN_PERSISTENT_CACHE or self.provider_terms_cleared

    def is_request_cap_reached(self) -> bool:
        return self.request_count >= self.max_requests

    def _expire_timed_quota(self) -> None:
        if self.hard_block_status == ProviderStatus.QUOTA_EXCEEDED and self._quota_reset_utc:
            if self._quota_reset_utc <= self._now_utc_normalized().isoformat():
                self.hard_blocked = False
                self.hard_block_reason = self.hard_block_kind = self._quota_reset_utc = ""

    def can_request(self) -> bool:
        self._expire_timed_quota()
        return (
            not self.hard_blocked
            and self.remaining_cooldown_seconds() <= 0.0
            and self.request_count < self.max_requests
        )

    @staticmethod
    def _build_cache_key(url: str, params: Optional[Dict[str, Any]]) -> str:
        serialized = json.dumps({"u": url, "p": params or {}}, sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _read_cache(self, cache_key: str) -> Optional[Any]:
        if cache_key in self._session_cache:
            return self._session_cache[cache_key]
        if self.db is not None and self.can_persist_cache():
            return self.db.get_cache(self.source_family, cache_key, now_utc=self._now_utc_normalized())
        return None

    def _write_cache(self, cache_key: str, payload: Any) -> None:
        self._session_cache[cache_key] = payload
        if self.db is not None and self.can_persist_cache():
            self.db.set_cache(
                self.source_family,
                cache_key,
                payload,
                ttl_seconds=self.DEFAULT_CACHE_TTL_SECONDS,
                now_utc=self._now_utc_normalized(),
            )

    def _can_sleep_within_budget(self, delay: float) -> bool:
        if delay <= 0.0:
            return True
        return (self.retry_sleep_used + delay) <= self.max_retry_sleep_seconds

    def _sleep_bounded(self, delay: float) -> bool:
        """Sleep full required delay if within run retry-sleep budget; never shorten server waits."""
        if delay <= 0.0:
            return True
        if not self._can_sleep_within_budget(delay):
            return False
        self._sleep_fn(delay)
        self.retry_sleep_used += delay
        return True

    def _effective_pacing_delay(self) -> float:
        min_req = float(getattr(self, "MIN_REQUIRED_DELAY_SECONDS", 0.0) or 0.0)
        return max(0.0, float(self.default_delay), min_req)

    def _enforce_pacing_before_request(self) -> None:
        eff_delay = self._effective_pacing_delay()
        if eff_delay > 0.0:
            if self.db is not None and hasattr(self.db, "get_provider_operational_state"):
                try:
                    st = self.db.get_provider_operational_state(self.source_family)
                    lr_raw = str((st or {}).get("last_request_utc") or "").strip()
                    if lr_raw:
                        lr_dt = datetime.fromisoformat(lr_raw.replace("Z", "+00:00"))
                        lr_dt = lr_dt.replace(tzinfo=timezone.utc) if lr_dt.tzinfo is None else lr_dt.astimezone(timezone.utc)
                        elapsed = (self._now_utc_normalized() - lr_dt).total_seconds()
                        if 0.0 <= elapsed < eff_delay:
                            self._next_pacing_mono = max(
                                self._next_pacing_mono,
                                self._clock_mono() + (eff_delay - elapsed),
                            )
                except Exception:
                    pass
            now_m = self._clock_mono()
            wait_p = self._next_pacing_mono - now_m
            if wait_p > 0.0:
                self._sleep_fn(wait_p)
            req_start_m = self._clock_mono()
            self._next_pacing_mono = req_start_m + eff_delay
        req_utc = self._now_utc_normalized().isoformat()
        self._persist_operational_state(last_request_utc=req_utc)

    def _record_pacing_after_request(self) -> None:
        eff_delay = self._effective_pacing_delay()
        if eff_delay > 0.0:
            self._next_pacing_mono = max(self._next_pacing_mono, self._clock_mono() + eff_delay)
        self._persist_operational_state(last_request_utc=self._now_utc_normalized().isoformat())

    def _precheck_unavailable(self) -> Optional[ProviderCallOutcome]:
        self._expire_timed_quota()
        if self.hard_blocked:
            self.stats["skipped_blocked"] += 1
            rem_q = self.remaining_cooldown_seconds()
            outcome = ProviderCallOutcome(
                provider=self.name,
                status=self.hard_block_status,
                error=self.hard_block_reason or "provider hard-blocked",
                retry_after_seconds=rem_q,
                retry_until_utc=self.cooldown_until_utc,
                error_kind=self.hard_block_kind or self.hard_block_status.value,
                hard_blocked=True,
                request_made=False,
            )
            self.last_outcome = outcome
            return outcome

        rem_cd = self.remaining_cooldown_seconds()
        if rem_cd > 0.0:
            self.stats["skipped_cooldown"] += 1
            if self.request_monitor:
                self.request_monitor.count("cooldown_skips")
            outcome = ProviderCallOutcome(
                provider=self.name,
                status=ProviderStatus.COOLDOWN_DEFERRED,
                error=f"{self._cooldown_reason or 'provider cooling down'} ({rem_cd:.1f}s remaining)",
                retry_after_seconds=rem_cd,
                retry_until_utc=self._cooldown_until_utc,
                error_kind=self._cooldown_kind or "provider_cooldown",
                deferred_by_cooldown=True,
                request_made=False,
            )
            self.last_outcome = outcome
            return outcome

        if self.request_count >= self.max_requests:
            self.stats["skipped_budget"] += 1
            outcome = ProviderCallOutcome(
                provider=self.name,
                status=ProviderStatus.BUDGET_EXHAUSTED,
                error=f"run request cap reached ({self.request_count}/{self.max_requests})",
                error_kind="request_cap_exhausted",
                request_made=False,
            )
            self.last_outcome = outcome
            return outcome

        return None

    def request_detailed(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: int = 20,
        use_cache: bool = True,
        allow_404_as_empty: bool = False,
    ) -> ProviderCallOutcome:
        """Execute paced HTTP GET with separated cooldown, hard-block, and request-cap handling."""
        cache_key = self._build_cache_key(url, params)
        if use_cache:
            cached = self._read_cache(cache_key)
            if cached is not None:
                self.stats["cache_hits"] += 1
                if self.request_monitor:
                    self.request_monitor.count("cache_hits")
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

        blocked_out = self._precheck_unavailable()
        if blocked_out is not None:
            return blocked_out

        merged_headers = dict(self.session.headers)
        if headers:
            merged_headers.update(headers)

        for attempt in range(3):
            if attempt > 0:
                recheck = self._precheck_unavailable()
                if recheck is not None:
                    return recheck

            self._enforce_pacing_before_request()
            self.request_count += 1
            self.stats["calls"] += 1

            started = self._clock_mono()
            try:
                try:
                    resp = self.session.get(url, params=params, headers=merged_headers, timeout=timeout)
                finally:
                    self._record_pacing_after_request()
                code = resp.status_code
                resp_headers = resp.headers or {}
                now_utc = self._now_utc_normalized()

                server_wait = parse_provider_reset_wait(resp_headers, self.source_family, now_utc=now_utc)
                quota = code == 402 or (code == 429 and _is_documented_quota_exceeded(code, resp_headers, resp.text or ""))
                is_mb_503_rl = _is_musicbrainz_503_rate_limit(self.source_family, code, resp_headers, resp.text or "")
                breaker_wait = 0.0
                if self.request_monitor and not resp.ok:
                    kind = (
                        "quota_exhaustion" if quota else
                        "auth_failures" if code in (401, 403) else
                        "rate_limits" if code == 429 or is_mb_503_rl else
                        "temporary_failures" if code >= 500 else
                        "expected_empty_responses" if code == 404 and allow_404_as_empty else
                        "other_failures"
                    )
                    breaker_wait = self.request_monitor.observe(
                        kind, self._clock_mono() - started, now_utc, server_wait,
                    )

                if code in (401, 403):
                    self.hard_blocked = True
                    self.hard_block_status = ProviderStatus.AUTH_ERROR
                    self.hard_block_kind = "auth_error"
                    self.hard_block_reason = f"HTTP {code} authentication/permission denied"
                    self.stats["auth_errors"] += 1
                    self._persist_operational_state(
                        cooldown_until_utc="",
                        last_status=ProviderStatus.AUTH_ERROR.value,
                        last_http_status=code,
                        error_kind="auth_error",
                        reason=self.hard_block_reason,
                    )
                    outcome = ProviderCallOutcome(
                        provider=self.name,
                        status=ProviderStatus.AUTH_ERROR,
                        http_status=code,
                        error=self.hard_block_reason,
                        error_kind="auth_error",
                        hard_blocked=True,
                        request_made=True,
                    )
                    self.last_outcome = outcome
                    return outcome

                if code == 404 and allow_404_as_empty:
                    self._clear_cooldown_after_ok(code)
                    outcome = ProviderCallOutcome(
                        provider=self.name,
                        status=ProviderStatus.EMPTY,
                        http_status=404,
                        request_made=True,
                    )
                    self.last_outcome = outcome
                    return outcome

                if code in (404, 405, 410, 501):
                    self.hard_blocked = True
                    self.hard_block_status = ProviderStatus.UNSUPPORTED_ENDPOINT
                    self.hard_block_kind = "unsupported_endpoint"
                    self.hard_block_reason = f"HTTP {code} endpoint not found or unsupported"
                    self.stats["unsupported_endpoint"] += 1
                    self._persist_operational_state(
                        cooldown_until_utc="",
                        last_status=ProviderStatus.UNSUPPORTED_ENDPOINT.value,
                        last_http_status=code,
                        error_kind="unsupported_endpoint",
                        reason=self.hard_block_reason,
                    )
                    outcome = ProviderCallOutcome(
                        provider=self.name,
                        status=ProviderStatus.UNSUPPORTED_ENDPOINT,
                        http_status=code,
                        error=self.hard_block_reason,
                        error_kind="unsupported_endpoint",
                        hard_blocked=True,
                        request_made=True,
                    )
                    self.last_outcome = outcome
                    return outcome

                if quota:
                    q_wait = server_wait if (server_wait is not None and server_wait > 0.0) else 0.0
                    self.hard_blocked = True
                    self.hard_block_status = ProviderStatus.QUOTA_EXCEEDED
                    self.hard_block_kind = "quota_exceeded"
                    self.hard_block_reason = (
                        "HTTP 402 quota/payment required" if code == 402 else f"HTTP {code} quota exceeded"
                    )
                    self.stats["quota_exceeded"] += 1
                    cd_utc = ""
                    if q_wait > 0.0:
                        cd_utc = self._set_cooldown(
                            q_wait,
                            status=ProviderStatus.QUOTA_EXCEEDED.value,
                            http_status=code,
                            error_kind="quota_exceeded",
                            reason=self.hard_block_reason,
                        )
                    else:
                        self._persist_operational_state(
                            cooldown_until_utc="",
                            last_status=ProviderStatus.QUOTA_EXCEEDED.value,
                            last_http_status=code,
                            error_kind="quota_exceeded",
                            reason=self.hard_block_reason,
                        )
                    self._quota_reset_utc = cd_utc
                    outcome = ProviderCallOutcome(
                        provider=self.name,
                        status=ProviderStatus.QUOTA_EXCEEDED,
                        http_status=code,
                        error=self.hard_block_reason,
                        retry_after_seconds=q_wait,
                        retry_until_utc=cd_utc,
                        error_kind="quota_exceeded",
                        hard_blocked=True,
                        request_made=True,
                    )
                    self.last_outcome = outcome
                    return outcome

                if code == 429 or is_mb_503_rl:
                    wait_s = max(breaker_wait, server_wait or (2.0 * (2 ** attempt)))
                    err_kind = "musicbrainz_503_rate_limit" if is_mb_503_rl else "http_429_rate_limit"
                    self.stats["rate_limited"] += 1
                    if is_mb_503_rl:
                        self.stats["mb_503_rate_limited"] += 1

                    # Retry in-place ONLY if within remaining run cap AND full server wait fits sleep budget
                    if not breaker_wait and attempt < 2 and self.request_count < self.max_requests and self._sleep_bounded(wait_s):
                        continue

                    reason = f"HTTP {code} rate limited (retry after {wait_s:.1f}s)"
                    cd_utc = self._set_cooldown(
                        wait_s,
                        status=ProviderStatus.RATE_LIMITED.value,
                        http_status=code,
                        error_kind="circuit_breaker" if breaker_wait else err_kind,
                        reason=reason,
                    )
                    outcome = ProviderCallOutcome(
                        provider=self.name,
                        status=ProviderStatus.RATE_LIMITED,
                        http_status=code,
                        error=reason,
                        retry_after_seconds=wait_s,
                        retry_until_utc=cd_utc,
                        error_kind=err_kind,
                        request_made=True,
                    )
                    self.last_outcome = outcome
                    return outcome

                if 500 <= code < 600:
                    self.stats["server_errors"] += 1
                    backoff = max(server_wait or 0.0, 2.0 * (2 ** attempt))
                    if not breaker_wait and attempt < 2 and self.request_count < self.max_requests and self._sleep_bounded(backoff):
                        continue

                    cd_wait = max(breaker_wait, server_wait or 0.0, 30.0)
                    reason = f"HTTP {code} server unavailable"
                    cd_utc = self._set_cooldown(
                        cd_wait,
                        status=ProviderStatus.SERVER_ERROR.value,
                        http_status=code,
                        error_kind="circuit_breaker" if breaker_wait else "http_5xx_server_error",
                        reason=reason,
                    )
                    outcome = ProviderCallOutcome(
                        provider=self.name,
                        status=ProviderStatus.SERVER_ERROR,
                        http_status=code,
                        error=reason,
                        retry_after_seconds=cd_wait,
                        retry_until_utc=cd_utc,
                        error_kind="http_5xx_server_error",
                        request_made=True,
                    )
                    self.last_outcome = outcome
                    return outcome

                if resp.ok:
                    try:
                        payload = resp.json()
                    except ValueError as json_exc:
                        breaker_until = ""
                        if self.request_monitor:
                            self.request_monitor.count("malformed_responses")
                            wait = self.request_monitor.observe("temporary_failures", self._clock_mono() - started, now_utc)
                            if wait:
                                breaker_until = self._set_cooldown(
                                    wait, ProviderStatus.SERVER_ERROR.value, code, "circuit_breaker",
                                    "Repeated malformed provider responses",
                                )
                        self.stats["malformed_json"] += 1
                        outcome = ProviderCallOutcome(
                            provider=self.name,
                            status=ProviderStatus.MALFORMED_JSON,
                            http_status=code,
                            error=f"Malformed JSON response ({self._redact(json_exc)})",
                            error_kind="malformed_json",
                            retry_until_utc=breaker_until,
                            request_made=True,
                        )
                        self.last_outcome = outcome
                        return outcome

                    if self.request_monitor:
                        self.request_monitor.observe("successful_responses", self._clock_mono() - started, now_utc)
                    self._clear_cooldown_after_ok(code)

                    # Proactive exhausted-window cooldown without shortening server reset or stalling current OK result
                    rem_hdr = (
                        _get_header_ci(resp_headers, "X-Discogs-Ratelimit-Remaining")
                        or _get_header_ci(resp_headers, "RateLimit-Remaining")
                        or _get_header_ci(resp_headers, "X-RateLimit-Remaining")
                    )
                    if rem_hdr is not None and rem_hdr.strip().isdigit() and int(rem_hdr.strip()) == 0:
                        proactive_wait = server_wait if (server_wait is not None and server_wait > 0.0) else (
                            60.0 if self.source_family == "discogs" else 2.0
                        )
                        self._set_cooldown(
                            proactive_wait,
                            status=ProviderStatus.RATE_LIMITED.value,
                            http_status=code,
                            error_kind="proactive_ratelimit_window",
                            reason=f"rate-limit window exhausted; retry held for {proactive_wait:.1f}s",
                        )

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

                unexp_wait = server_wait if (server_wait is not None and server_wait > 0.0) else 0.0
                cd_unexp = ""
                if unexp_wait > 0.0:
                    cd_unexp = self._set_cooldown(
                        unexp_wait,
                        status=ProviderStatus.SERVER_ERROR.value,
                        http_status=code,
                        error_kind="unexpected_http_status",
                        reason=f"HTTP {code}",
                    )
                outcome = ProviderCallOutcome(
                    provider=self.name,
                    status=ProviderStatus.SERVER_ERROR,
                    http_status=code,
                    error=f"HTTP {code}",
                    retry_after_seconds=unexp_wait,
                    retry_until_utc=cd_unexp,
                    error_kind="unexpected_http_status",
                    request_made=True,
                )
                self.last_outcome = outcome
                return outcome

            except requests.Timeout as exc:
                self.stats["timeouts"] += 1
                breaker_wait = self.request_monitor.observe(
                    "temporary_failures", self._clock_mono() - started, self._now_utc_normalized(),
                ) if self.request_monitor else 0.0
                backoff = 1.5 * (2 ** attempt)
                if not breaker_wait and attempt < 2 and self.request_count < self.max_requests and self._sleep_bounded(backoff):
                    continue
                reason = f"Request timeout ({self._redact(exc)})"
                cd_utc = self._set_cooldown(
                    max(15.0, breaker_wait),
                    status=ProviderStatus.TIMEOUT.value,
                    http_status=None,
                    error_kind="timeout",
                    reason=reason,
                )
                outcome = ProviderCallOutcome(
                    provider=self.name,
                    status=ProviderStatus.TIMEOUT,
                    error=reason,
                    retry_after_seconds=max(15.0, breaker_wait),
                    retry_until_utc=cd_utc,
                    error_kind="timeout",
                    request_made=True,
                )
                self.last_outcome = outcome
                return outcome
            except requests.RequestException as exc:
                self.stats["network_errors"] += 1
                breaker_wait = self.request_monitor.observe(
                    "temporary_failures", self._clock_mono() - started, self._now_utc_normalized(),
                ) if self.request_monitor else 0.0
                backoff = 1.5 * (2 ** attempt)
                if not breaker_wait and attempt < 2 and self.request_count < self.max_requests and self._sleep_bounded(backoff):
                    continue
                reason = f"Network error ({self._redact(exc)})"
                cd_utc = self._set_cooldown(
                    max(15.0, breaker_wait),
                    status=ProviderStatus.NETWORK_ERROR.value,
                    http_status=None,
                    error_kind="network_error",
                    reason=reason,
                )
                outcome = ProviderCallOutcome(
                    provider=self.name,
                    status=ProviderStatus.NETWORK_ERROR,
                    error=reason,
                    retry_after_seconds=max(15.0, breaker_wait),
                    retry_until_utc=cd_utc,
                    error_kind="network_error",
                    request_made=True,
                )
                self.last_outcome = outcome
                return outcome

        outcome = ProviderCallOutcome(
            provider=self.name,
            status=ProviderStatus.BUDGET_EXHAUSTED,
            error=self.block_reason or "retry budget exhausted",
            error_kind="request_cap_exhausted",
            request_made=False,
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
