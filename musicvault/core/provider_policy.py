"""Field ordering, durable enrichment, SonoVault fallback protection, and provider request telemetry."""
import json
from contextlib import closing
from datetime import datetime, timedelta

from ..db import _utc_iso
from ..providers.base import canonical_provider_family
from .artist_rules import load_artist_rules
from .genre import SUBGENRE_PARENT, canonical_subgenre
from .rules import build_track_proposal, field_decision

CAPABILITIES = {
    "musicbrainz": {"identity", "genre", "duration", "edition", "isrc", "label"},
    "discogs": {"identity", "genre", "subgenre", "label", "duration", "edition"},
    "hdrr": {"identity", "genre", "subgenre", "edition", "isrc"},
    "audiometa": {"identity", "bpm", "key", "duration", "isrc"},
    "sonovault": {"identity", "bpm", "key", "genre", "subgenre", "label", "duration", "isrc"},
}
AUDIT_NAMES = {
    "musicbrainz": "MusicBrainz",
    "sonovault": "SonoVault",
    "audiometa": "AudioMeta",
    "discogs": "Discogs",
    "hdrr": "HDRR",
}
DEFAULT_ORDER = ("musicbrainz", "discogs", "hdrr", "audiometa", "sonovault")
NON_HARD_DANCE_PARENTS = frozenset({
    "Country", "Rock", "Hip-Hop", "Pop", "R&B", "Reggae", "Jazz", "Classical", "Soul", "Funk", "Disco", "Ambient",
})
FIELDS = set().union(*CAPABILITIES.values())
ESSENTIAL_FALLBACK_FIELDS = frozenset({"identity", "genre", "bpm", "key"})
MAX_ENRICHMENT_ATTEMPTS = 3

_REAL_HTTP_FAILURE_REASONS = frozenset({
    "rate_limited",
    "server_error",
    "timeout",
    "network_error",
    "malformed_json",
})


def _families(values, allow_wikidata=False):
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("Provider settings require lists of provider names.")
    allowed = set(CAPABILITIES) | ({"wikidata"} if allow_wikidata else set())
    names = list(dict.fromkeys(canonical_provider_family(value.strip()) for value in values if value.strip()))
    if any(name not in allowed for name in names):
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
        self.sonovault_mode = mode
        self.fallback = set(_families((config.get("FALLBACK_RESEARCH_PROVIDERS", "") or "").split(",")))
        if mode == "fallback":
            self.fallback.add("sonovault")
        disabled_raw = (config.get("DISABLED_RESEARCH_PROVIDERS", "") or "").strip()
        self.disabled = set(_families(disabled_raw.split(","), allow_wikidata=True)) if disabled_raw else set()
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
        self.config = config
        self.db = None
        self.provider = ""
        self.providers = {}
        self.artist_rules = None

    def _get_artist_rules(self):
        if self.artist_rules is None:
            self.artist_rules = load_artist_rules(db=self.db, config=self.config)
        return self.artist_rules

    def bind(self, db, providers, provider=""):
        self.db = db
        self.artist_rules = load_artist_rules(db=db, config=self.config)
        self.provider = canonical_provider_family(provider.strip()) if provider else ""
        if self.provider and self.provider not in CAPABILITIES:
            raise ValueError("Unknown provider for selective resumption.")
        self.providers = {
            p.source_family: p
            for p in providers.values()
            if p is not None and hasattr(p, "source_family")
        }
        for name, p in self.providers.items():
            self.monitor(p, name)

    def monitor(self, provider, name):
        provider.request_monitor = RequestMonitor(self.db, name, self.breaker)
        with closing(self.db._get_connection()) as conn:
            row = conn.execute("SELECT until_utc FROM provider_breakers WHERE provider=?", (name,)).fetchone()
        if row and row[0]:
            remaining = (datetime.fromisoformat(row[0]) - provider._now_utc_normalized()).total_seconds()
            if remaining > 0 and not provider.hard_blocked:
                provider._set_cooldown(
                    remaining,
                    "server_error",
                    None,
                    "circuit_breaker",
                    "Persisted provider circuit breaker",
                )

    def new_batch(self):
        for provider in self.providers.values():
            if getattr(provider, "request_monitor", None) is not None:
                provider.request_monitor.batch.clear()

    def prepare_track(self):
        """Reset per-track runaway request/sleep guards while preserving pacing, cooldowns, breakers, and blocks."""
        for provider in self.providers.values():
            if hasattr(provider, "request_count"):
                provider.request_count = 0
            if hasattr(provider, "retry_sleep_used"):
                provider.retry_sleep_used = 0.0

    def permits(self, family):
        """
        Return True when in-session querying and transient candidate inspection are permitted.
        Persistent raw response caching (`provider_cache`) and spreadsheet writes remain
        independently gated by `BaseProvider.can_persist_cache()` and `TERMS_HOLD`.
        """
        return family not in self.disabled

    def is_required(self, family):
        if self.required is not None:
            return family in self.required
        if family in self.disabled:
            return False
        return family not in self.fallback

    def is_fallback_only(self, family):
        return (family in self.fallback) and (not self.is_required(family)) and (self.provider != family)

    def _manual_locked_fields(self, file_id):
        if self.db is None:
            return set()
        with closing(self.db._get_connection()) as conn:
            return {
                field
                for field, in conn.execute(
                    """
                    SELECT field_name FROM field_decisions WHERE file_id=? AND value!=''
                    AND state IN ('USER_LOCKED','HIGH_LOCKED','ACCEPTED','VERIFIED')
                    """,
                    (file_id,),
                )
            }

    def needed_fields(self, track, candidates):
        proposed = build_track_proposal(
            track.metadata,
            candidates,
            artist_rules=self._get_artist_rules(),
            file_id=track.file_id,
        )
        missing = {
            field
            for field in ("bpm", "key", "genre", "subgenre")
            if field_decision(candidates, field, proposed)[0] != "HIGH"
        }
        verified = [
            c
            for c in candidates
            if c.verification in {"ISRC supported", "Duration supported"}
            and canonical_provider_family(c.source_family or c.provider) != "wikidata"
        ]
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
            if family in {"wikidata", "hdrr"} or getattr(candidate, "genre_evidence_kind", "") == "artist_derived":
                continue
            for raw_style in candidate.genre:
                canon_sub = canonical_subgenre(raw_style)
                if canon_sub and SUBGENRE_PARENT.get(canon_sub) == proposed["genre"] and proposed["genre"]:
                    styles.setdefault(canon_sub, set()).add(family)
        if len(styles) == 1 and len(next(iter(styles.values()))) >= 2:
            missing.discard("subgenre")
        manual = self._manual_locked_fields(track.file_id)
        missing -= manual
        if {"artist", "title"} <= manual:
            missing.discard("identity")
        return missing

    def fallback_needed_fields(self, track, candidates):
        """
        Compute unresolved essential fields (`identity`, `genre`, `bpm`, `key`) eligible to trigger
        a fallback provider (such as SonoVault in `SONOVAULT_MODE=fallback`).
        - Parent-genre routing requires HIGH (2-family) verification on `genre`, whereas `bpm` and
          `key` have a single primary provider (`AudioMeta`) so MEDIUM (1-family) suffices.
        - Fields already in cross-provider conflict cannot be promoted by fallback and are skipped.
        - Missing optional `subgenre` or `label` alone never trigger fallback calls.
        """
        proposed = build_track_proposal(
            track.metadata,
            candidates,
            artist_rules=self._get_artist_rules(),
            file_id=track.file_id,
        )
        missing = set()
        verified = [
            c
            for c in candidates
            if c.verification in {"ISRC supported", "Duration supported"}
            and canonical_provider_family(c.source_family or c.provider) != "wikidata"
        ]
        if not verified:
            missing.add("identity")
        for field in ("genre", "bpm", "key"):
            val = proposed.get(field, "")
            level, reason = field_decision(candidates, field, proposed)
            rsn_low = (reason or "").lower()
            already_conflicting = (val == "CONFLICT") or (
                bool(val)
                and any(tok in rsn_low for tok in ("contradictory", "conflicting", "disagreement"))
            )
            if already_conflicting:
                continue
            if field == "genre":
                if level != "HIGH":
                    missing.add("genre")
            elif level == "LOW":
                missing.add(field)
        manual = self._manual_locked_fields(track.file_id)
        missing -= (manual & ESSENTIAL_FALLBACK_FIELDS)
        if {"artist", "title"} <= manual:
            missing.discard("identity")
        return missing

    def primary_incomplete_for_fallback(self, family, essential_needed, provider_audit):
        """
        Return configured, permitted non-fallback providers covering `essential_needed` that have
        not completed a clean (`ok`/`empty`) lookup on this track (e.g. cooling down, rate-limited,
        timed out, budget-exhausted, or quota-exhausted).
        """
        if not self.is_fallback_only(family):
            return []
        target_fields = CAPABILITIES[family] & set(essential_needed) & ESSENTIAL_FALLBACK_FIELDS
        if not target_fields:
            return []
        incomplete = []
        for other_fam, other_caps in CAPABILITIES.items():
            if other_fam == family or self.is_fallback_only(other_fam) or not self.permits(other_fam):
                continue
            if not (other_caps & target_fields):
                continue
            p_inst = self.providers.get(other_fam)
            entry = provider_audit.get(AUDIT_NAMES[other_fam])
            if p_inst is None and (not isinstance(entry, dict) or entry.get("status") in ("", "not_configured")):
                continue
            if isinstance(entry, dict) and entry.get("status") in (
                "not_configured",
                "disabled_by_policy",
                "sufficient_evidence",
            ):
                continue
            if not (isinstance(entry, dict) and entry.get("lookup_completed") is True):
                incomplete.append(other_fam)
        return incomplete

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
            return self.is_fallback_only(family), not available, min(rank, default=99)

        return sorted(calls, key=priority)

    def reason_to_skip(self, family, needed, track=None, candidates=()):
        if self.provider and family != self.provider:
            return "outside_provider_selection"
        if not self.permits(family):
            return "disabled_by_policy"
        if family == "hdrr" and candidates:
            prop = (
                build_track_proposal(
                    track.metadata,
                    list(candidates),
                    artist_rules=self._get_artist_rules(),
                    file_id=track.file_id,
                )
                if track is not None
                else {}
            )
            if prop.get("genre") in NON_HARD_DANCE_PARENTS and field_decision(list(candidates), "genre", prop)[0] == "HIGH":
                return "sufficient_evidence"
        if self.is_fallback_only(family):
            if not (CAPABILITIES[family] & set(needed) & ESSENTIAL_FALLBACK_FIELDS):
                return "sufficient_evidence"
            return ""
        if not self.is_required(family) and not (CAPABILITIES[family] & set(needed)):
            return "sufficient_evidence"
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
            row = conn.execute(
                "SELECT provider_calls_json,research_version FROM tracks WHERE file_id=?",
                (track.file_id,),
            ).fetchone()
            try:
                existing = json.loads(row[0]) if row and row[1] in ("", version) else {}
            except (ValueError, TypeError):
                existing = {}
            existing[name] = audit
            conn.execute(
                "UPDATE tracks SET provider_calls_json=?,research_version=? WHERE file_id=?",
                (json.dumps(existing), version, track.file_id),
            )
            if audit.get("lookup_completed") or status in ("sufficient_evidence", "disabled_by_policy"):
                conn.execute(
                    "DELETE FROM enrichment_work WHERE file_id=? AND version=? AND provider=?",
                    (track.file_id, version, family),
                )
                if status == "sufficient_evidence":
                    RequestMonitor.increment(conn, family, "requests_avoided")
                return
            if status in ("not_configured", "deferred_primary_incomplete"):
                conn.execute(
                    "DELETE FROM enrichment_work WHERE file_id=? AND version=? AND provider=?",
                    (track.file_id, version, family),
                )
                return
            prev_row = conn.execute(
                "SELECT COALESCE(MAX(attempts), 0) FROM enrichment_work WHERE file_id=? AND version=? AND provider=?",
                (track.file_id, version, family),
            ).fetchone()
            prev_attempts = int(prev_row[0] or 0) if prev_row else 0
            consume_attempt = bool(audit.get("request_made") and status in _REAL_HTTP_FAILURE_REASONS)
            next_attempts = prev_attempts + (1 if consume_attempt else 0)
            query_key = (
                "alternate"
                if (audit.get("primary_query") or {}).get("status") in ("ok", "empty")
                and (audit.get("alternate_query") or status == "in_progress_alternate")
                else "primary"
            )
            conn.execute(
                "DELETE FROM enrichment_work WHERE file_id=? AND version=? AND provider=?",
                (track.file_id, version, family),
            )
            conn.execute(
                """
                INSERT INTO enrichment_work(file_id, version, provider, query_key, reason, due_utc, attempts)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (track.file_id, version, family, query_key, status, audit.get("retry_until_utc", ""), next_attempts),
            )

    def enrichment_condition(self, max_attempts=MAX_ENRICHMENT_ATTEMPTS):
        # Families come from the static catalogue and are safe as SQL literals.
        active = [
            f
            for f, p in self.providers.items()
            if f in CAPABILITIES
            and self.permits(f)
            and p.can_request()
            and (not self.provider or self.provider == f)
        ]
        if not active:
            return "0"
        quoted = ",".join("'" + family + "'" for family in active)
        now = _utc_iso()
        max_att = int(max_attempts)
        pending = f"""EXISTS (SELECT 1 FROM enrichment_work e WHERE e.file_id=tracks.file_id
            AND e.version=tracks.version AND e.provider IN ({quoted})
            AND COALESCE(e.attempts, 0) < {max_att}
            AND (e.due_utc='' OR e.due_utc<='{now}'))"""
        if self.provider:
            audit_name = AUDIT_NAMES[self.provider]
            explicit_deferred = (
                f"(CASE WHEN json_valid(provider_calls_json) THEN "
                f"json_extract(provider_calls_json, '$.{audit_name}.status') IN ('not_configured','deferred_primary_incomplete') "
                f"ELSE 0 END)"
            )
            return f"({pending} OR {explicit_deferred})"
        new_provider = " OR ".join(
            f"json_extract(provider_calls_json, '$.{AUDIT_NAMES[family]}.status')='not_configured'"
            for family in active
            if self.is_required(family)
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
        conn.execute(
            """
            INSERT INTO provider_metrics VALUES (?, ?, ?)
            ON CONFLICT(provider,metric) DO UPDATE SET value=value+excluded.value
            """,
            (family, kind, value),
        )

    def count(self, kind):
        with closing(self.db._get_connection()) as conn, conn:
            self.increment(conn, self.family, kind)

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
            if server_wait and (transient or kind == "quota_exhaustion"):
                conn.execute(
                    """
                    INSERT INTO provider_server_deadlines VALUES (?, ?)
                    ON CONFLICT(provider) DO UPDATE SET until_utc=excluded.until_utc
                    """,
                    (self.family, _utc_iso(now + timedelta(seconds=server_wait))),
                )
            elif clean:
                conn.execute("DELETE FROM provider_server_deadlines WHERE provider=?", (self.family,))
            stored = conn.execute(
                "SELECT history,streak,level FROM provider_breakers WHERE provider=?",
                (self.family,),
            ).fetchone()
            history, streak, level = (json.loads(stored[0]), stored[1], stored[2]) if stored else ([], 0, 0)
            history = (history + [transient])[-int(window_size):]
            streak = streak + 1 if transient else 0
            trip = transient and (
                level > 0
                or streak >= self.options.get("consecutive", 3)
                or (len(history) >= window_size and sum(history) / len(history) >= self.options.get("rate", 0.6))
                or (
                    len(self.batch) >= self.options.get("batch_min", 5)
                    and sum(self.batch) / len(self.batch) >= self.options.get("batch_rate", 0.6)
                )
            )
            if clean and level > 0:
                history, level = [], 0
            wait = 0
            if trip:
                level = min(level + 1, 15)
                wait = min(
                    self.options.get("max_cooldown", 3600),
                    self.options.get("cooldown", 300) * 2 ** (level - 1),
                )
                wait = max(wait, server_wait or 0)
            conn.execute(
                """
                INSERT INTO provider_breakers VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET history=excluded.history,
                    streak=excluded.streak, level=excluded.level,
                    until_utc=CASE WHEN excluded.until_utc!='' OR excluded.level=0
                                  THEN excluded.until_utc ELSE provider_breakers.until_utc END
                """,
                (
                    self.family,
                    json.dumps(history),
                    streak,
                    level,
                    _utc_iso(now + timedelta(seconds=wait)) if wait else "",
                ),
            )
        return wait


def provider_report(conn, config=None):
    families = {**CAPABILITIES, "wikidata": set()}
    rows = conn.execute("SELECT provider,metric,value FROM provider_metrics")
    disabled = set()
    if config is not None:
        raw_dis = (config.get("DISABLED_RESEARCH_PROVIDERS", "") or "").strip()
        if raw_dis:
            disabled = set(_families(raw_dis.split(","), allow_wikidata=True))
    enabled = set()
    if config is not None:
        enabled.update({"musicbrainz", "wikidata"} - disabled)
        if config.get_bool("HDRR_ENABLED", True) and "hdrr" not in disabled:
            enabled.add("hdrr")
        enabled.update(
            name
            for name, configured in (
                ("sonovault", config.sonovault_api_key),
                ("audiometa", config.audiometa_api_key),
                ("discogs", config.discogs_token),
            )
            if configured and name not in disabled
        )
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
        result.setdefault(name, {}).update(
            availability=state,
            next_retry_utc=deadline or "",
            reason=row["error_kind"],
        )
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
