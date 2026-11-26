"""Field ordering, durable enrichment, and provider request telemetry."""
import json
from contextlib import closing
from datetime import datetime, timedelta

from ..db import _utc_iso
from ..providers.base import canonical_provider_family
from .genre import SUBGENRE_PARENT
from .rules import build_track_proposal, field_decision

CAPABILITIES = {
    "musicbrainz": {"identity", "genre", "duration", "edition", "isrc"},
    "discogs": {"identity", "genre", "subgenre", "label", "duration", "edition"},
    "audiometa": {"identity", "bpm", "key"},
    "sonovault": {"identity", "bpm", "key", "genre", "subgenre"},
}
AUDIT_NAMES = {"musicbrainz": "MusicBrainz", "sonovault": "SonoVault",
               "audiometa": "AudioMeta", "discogs": "Discogs"}
DEFAULT_ORDER = ("musicbrainz", "discogs", "audiometa", "sonovault")
FIELDS = set().union(*CAPABILITIES.values())


def _families(values):
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("Provider settings require lists of provider names.")
    names = list(dict.fromkeys(canonical_provider_family(value.strip()) for value in values if value.strip()))
    if any(name not in CAPABILITIES for name in names):
        raise ValueError("Unknown catalogue provider in research settings.")
    return names


class ProviderPolicy:
    def __init__(self, config):
        order = json.loads(config.get("PROVIDER_PRIORITY_JSON", "{}") or "{}")
        if not isinstance(order, dict) or any(field not in FIELDS for field in order):
            raise ValueError("PROVIDER_PRIORITY_JSON must map known fields to ordered providers.")
        self.order = {field: _families(names) for field, names in order.items()}
        mode = config.get("SONOVAULT_MODE", "fallback")
        if mode not in {"standard", "fallback"}:
            raise ValueError("SONOVAULT_MODE must be standard or fallback.")
        self.fallback = set(_families((config.get("FALLBACK_RESEARCH_PROVIDERS", "") or "").split(",")))
        if mode == "fallback":
            self.fallback.add("sonovault")
        required = config.get("REQUIRED_RESEARCH_PROVIDERS")
        self.required = set(_families(required.split(","))) if required is not None else None
        self.terms_ok = config.provider_terms_cleared
        self.breaker = json.loads(config.get("PROVIDER_BREAKER_JSON", "{}") or "{}")
        allowed = {"consecutive", "window", "rate", "batch_min", "batch_rate", "cooldown", "max_cooldown"}
        if not isinstance(self.breaker, dict) or set(self.breaker) - allowed:
            raise ValueError("Unknown PROVIDER_BREAKER_JSON setting.")
        for name, value in self.breaker.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Breaker settings must be positive numbers.")
            if name in {"rate", "batch_rate"}:
                if not 0 < value <= 1:
                    raise ValueError("Breaker rates must be in (0,1].")
            elif value != int(value) or not 1 <= value <= (100 if name in {"consecutive", "window", "batch_min"} else 3600):
                raise ValueError("Breaker counts and cooldowns are out of bounds.")
        if self.breaker.get("cooldown", 300) > self.breaker.get("max_cooldown", 3600):
            raise ValueError("Breaker cooldown exceeds its maximum.")

    def bind(self, db, providers, provider=""):
        self.db = db
        self.provider = canonical_provider_family(provider.strip()) if provider else ""
        if self.provider and self.provider not in CAPABILITIES:
            raise ValueError("Unknown provider for selective resumption.")
        self.providers = {p.source_family: p for p in providers.values()
                          if p is not None and hasattr(p, "source_family")}
        for name, p in self.providers.items():
            self.monitor(p, name)

    def monitor(self, provider, name):
        provider.request_monitor = RequestMonitor(self.db, name, self.breaker)
        with closing(self.db._get_connection()) as conn:
            row = conn.execute("SELECT until_utc FROM provider_breakers WHERE provider=?", (name,)).fetchone()
        if row and row[0]:
            remaining = (datetime.fromisoformat(row[0]) - provider._now_utc_normalized()).total_seconds()
            if remaining > 0 and not provider.hard_blocked:
                provider._set_cooldown(remaining, "server_error", None, "circuit_breaker",
                                       "Persisted provider circuit breaker")

    def new_batch(self):
        for provider in self.providers.values():
            provider.request_monitor.batch.clear()

    def permits(self, family):
        return self.terms_ok or family in {"musicbrainz", "wikidata"}

    def is_required(self, family):
        return family in self.required if self.required is not None else family not in self.fallback

    def needed_fields(self, track, candidates):
        proposed = build_track_proposal(track.metadata, candidates)
        missing = {field for field in ("bpm", "key", "genre", "subgenre")
                   if field_decision(candidates, field, proposed)[0] != "HIGH"}
        verified = [c for c in candidates if c.verification in {"ISRC supported", "Duration supported"}]
        if not verified:
            missing.add("identity")
        if not (track.metadata.duration or any(c.duration for c in verified)):
            missing.add("duration")
        if not (track.metadata.isrc or any(c.isrc for c in verified)):
            missing.add("isrc")
        if not any(c.labels for c in verified):
            missing.add("label")
        if not verified:
            missing.add("edition")
        styles = {}
        for candidate in verified:
            family = canonical_provider_family(candidate.source_family or candidate.provider)
            for style in candidate.genre:
                if SUBGENRE_PARENT.get(style) == proposed["genre"] and proposed["genre"]:
                    styles.setdefault(style, set()).add(family)
        if len(styles) == 1 and len(next(iter(styles.values()))) >= 2:
            missing.discard("subgenre")
        with closing(self.db._get_connection()) as conn:
            manual = {field for field, in conn.execute("""
                SELECT field_name FROM field_decisions WHERE file_id=? AND value!=''
                AND state IN ('USER_LOCKED','HIGH_LOCKED','ACCEPTED','VERIFIED')
            """, (track.file_id,))}
        missing -= manual
        if {"artist", "title"} <= manual:
            missing.discard("identity")
        return missing

    def ordered(self, calls, needed):
        def priority(entry):
            name, provider, _call = entry
            family = canonical_provider_family(name)
            fields = sorted(needed & CAPABILITIES[family])
            rank = []
            for field in fields:
                order = self.order.get(field, []) + list(DEFAULT_ORDER)
                rank.append(order.index(family))
            available = provider is not None and provider.can_request() and self.permits(family)
            return family in self.fallback and not self.is_required(family), not available, min(rank, default=99)
        return sorted(calls, key=priority)

    def reason_to_skip(self, family, needed):
        if self.provider and family != self.provider:
            return "outside_provider_selection"
        if not self.is_required(family) and not (CAPABILITIES[family] & needed):
            return "sufficient_evidence"
        if not self.permits(family):
            return "terms_hold"
        return ""

    def checkpoint(self, track, name, audit, candidates=()):
        family = canonical_provider_family(name)
        status = audit["status"]
        version = track.metadata.version
        with closing(self.db._get_connection()) as conn, conn:
            if status == "outside_provider_selection":
                return
            if candidates:
                self.db._merge_candidates_conn(conn, track.file_id, version, list(candidates), _utc_iso())
            row = conn.execute("SELECT provider_calls_json,research_version FROM tracks WHERE file_id=?",
                               (track.file_id,)).fetchone()
            try:
                existing = json.loads(row[0]) if row[1] in ("", version) else {}
            except (ValueError, TypeError):
                existing = {}
            existing[name] = audit
            conn.execute("UPDATE tracks SET provider_calls_json=?,research_version=? WHERE file_id=?",
                         (json.dumps(existing), version, track.file_id))
            if audit.get("lookup_completed") or status == "sufficient_evidence":
                conn.execute("DELETE FROM enrichment_work WHERE file_id=? AND version=? AND provider=?",
                             (track.file_id, version, family))
                if status == "sufficient_evidence":
                    RequestMonitor.increment(conn, family, "requests_avoided")
                return
            if status == "not_configured":
                return
            query_key = "alternate" if (audit.get("primary_query") or {}).get("status") == "empty" and audit.get("alternate_query") else "primary"
            conn.execute("DELETE FROM enrichment_work WHERE file_id=? AND version=? AND provider=?",
                         (track.file_id, version, family))
            conn.execute("""
                INSERT INTO enrichment_work VALUES (?, ?, ?, ?, ?, ?)
            """, (track.file_id, version, family, query_key, status, audit.get("retry_until_utc", "")))

    def enrichment_condition(self):
        # Families come from the static catalogue and are safe as SQL literals.
        active = [f for f, p in self.providers.items() if f in CAPABILITIES
                  and self.permits(f) and p.can_request() and (not self.provider or self.provider == f)]
        if not active:
            return "0"
        quoted = ",".join("'" + family + "'" for family in active)
        now = _utc_iso()
        pending = f"""EXISTS (SELECT 1 FROM enrichment_work e WHERE e.file_id=tracks.file_id
            AND e.version=tracks.version AND e.provider IN ({quoted})
            AND (e.due_utc='' OR e.due_utc<='{now}'))"""
        if self.provider:
            return pending
        new_provider = " OR ".join(
            f"json_extract(provider_calls_json, '$.{AUDIT_NAMES[family]}.status')='not_configured'"
            for family in active if self.is_required(family)
        )
        if new_provider:
            return f"""({pending} OR (CASE WHEN json_valid(provider_calls_json)
                         THEN ({new_provider}) ELSE 0 END))"""
        return pending


class RequestMonitor:
    def __init__(self, db, family, options):
        self.db, self.family = db, family
        self.options = options
        self.batch = []

    @staticmethod
    def increment(conn, family, kind, value=1):
        conn.execute("""
            INSERT INTO provider_metrics VALUES (?, ?, ?)
            ON CONFLICT(provider,metric) DO UPDATE SET value=value+excluded.value
        """, (family, kind, value))

    def count(self, kind):
        with closing(self.db._get_connection()) as conn, conn:
            self.increment(conn, self.family, kind)
            if server_wait and (transient or kind == "quota_exhaustion"):
                conn.execute("""
                    INSERT INTO provider_server_deadlines VALUES (?, ?)
                    ON CONFLICT(provider) DO UPDATE SET until_utc=excluded.until_utc
                """, (self.family, _utc_iso(now + timedelta(seconds=server_wait))))
            elif clean:
                conn.execute("DELETE FROM provider_server_deadlines WHERE provider=?", (self.family,))

    def observe(self, kind, latency, now, server_wait=None):
        transient = kind in {"rate_limits", "temporary_failures"}
        clean = kind in {"successful_responses", "expected_empty_responses"}
        self.batch.append(transient)
        del self.batch[:-100]
        window_size = self.options.get("window", 10)
        with closing(self.db._get_connection()) as conn, conn:
            self.increment(conn, self.family, "http_attempts")
            self.increment(conn, self.family, "latency_seconds", latency)
            self.increment(conn, self.family, kind)
            if transient:
                self.increment(conn, self.family, "retryable_failures")
            stored = conn.execute("SELECT history,streak,level FROM provider_breakers WHERE provider=?",
                                  (self.family,)).fetchone()
            history, streak, level = (json.loads(stored[0]), stored[1], stored[2]) if stored else ([], 0, 0)
            history = (history + [transient])[-int(window_size):]
            streak = streak + 1 if transient else 0
            trip = transient and (
                level > 0
                or streak >= self.options.get("consecutive", 3)
                or (len(history) >= window_size and sum(history) / len(history) >= self.options.get("rate", .6))
                or (len(self.batch) >= self.options.get("batch_min", 5)
                    and sum(self.batch) / len(self.batch) >= self.options.get("batch_rate", .6))
            )
            if clean and level > 0:
                history, level = [], 0
            wait = 0
            if trip:
                level = min(level + 1, 15)
                wait = min(self.options.get("max_cooldown", 3600),
                           self.options.get("cooldown", 300) * 2 ** (level - 1))
                wait = max(wait, server_wait or 0)
            conn.execute("""
                INSERT INTO provider_breakers VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET history=excluded.history,
                    streak=excluded.streak, level=excluded.level,
                    until_utc=CASE WHEN excluded.until_utc!='' OR excluded.level=0
                                  THEN excluded.until_utc ELSE provider_breakers.until_utc END
            """, (self.family, json.dumps(history), streak, level,
                  _utc_iso(now + timedelta(seconds=wait)) if wait else ""))
        return wait


def provider_report(conn, config=None):
    families = {**CAPABILITIES, "wikidata": set()}
    rows = conn.execute("SELECT provider,metric,value FROM provider_metrics")
    enabled = {"musicbrainz", "wikidata"} if config is not None else set()
    if config and config.provider_terms_cleared:
        enabled.update(name for name, configured in (
            ("sonovault", config.sonovault_api_key), ("audiometa", config.audiometa_api_key),
            ("discogs", config.discogs_token),
        ) if configured)
    result = {name: {"availability": "AVAILABLE" if name in enabled else "DISABLED"} for name in families}
    for name, kind, value in rows:
        result.setdefault(name, {})[kind] = value
    now = _utc_iso()
    for row in conn.execute("SELECT * FROM provider_operational_state"):
        name, status, deadline = row["provider"], row["last_status"], row["cooldown_until_utc"]
        if status == "quota_exceeded" and not deadline:
            state = "RESET_UNKNOWN"
        elif status == "auth_error":
            state = "AUTH_FAILURE"
        elif status == "unsupported_endpoint":
            state = "DISABLED"
        elif deadline and deadline > now:
            if status == "quota_exceeded":
                state = "QUOTA_EXHAUSTED"
            elif row["error_kind"] == "circuit_breaker":
                state = "COOLDOWN"
            elif status == "rate_limited":
                state = "RATE_LIMITED"
            else:
                state = "TEMPORARILY_UNAVAILABLE"
        else:
            state = "AVAILABLE"
        if config is not None and name not in enabled:
            state, deadline = "DISABLED", ""
        result.setdefault(name, {}).update(availability=state, next_retry_utc=deadline or "",
                                           reason=row["error_kind"])
    for family, deadline in conn.execute("SELECT provider,until_utc FROM provider_server_deadlines"):
        if deadline > now and family in result and result[family]["availability"] != "DISABLED":
            result[family]["server_retry_until_utc"] = deadline
    for stats in result.values():
        attempts = stats.get("http_attempts", 0)
        clean = stats.get("successful_responses", 0) + stats.get("expected_empty_responses", 0)
        stats["failure_rate"] = (attempts - clean) / attempts if attempts else None
        deadline = stats.get("next_retry_utc", "")
        if deadline and deadline > now:
            stats["deadline_source"] = "server" if stats.get("server_retry_until_utc") == deadline else "local_hold"
        stats["average_request_latency"] = stats.get("latency_seconds", 0) / attempts if attempts else None
    return result
