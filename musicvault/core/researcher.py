"""
Resumable, honest online metadata research scheduler and execution engine (v2.0.2).
- Separates provider cooldowns, hard auth/quota blocks, and request-budget exhaustion.
- Tracks per-provider and per-orientation (primary + swapped ambiguous filename) outcomes,
  actual HTTP calls, cache hits, skipped calls, retry deadlines, and lookup completion.
- Never treats deferred, blocked, or unattempted providers as definitive no_match.
- Retains incomplete work and schedules retries without consuming track attempts on pure
  cooldown/budget deferrals; honors server retry waits beyond 3600s without shortening.
- Continues healthy providers across remaining tracks when a single provider cools down or
  caps out, avoids infinite retry loops on untimed hard blocks, and preserves partial
  candidates even when detail sub-requests hit rate limits.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config import Config
from ..db import Database
from ..models import Candidate, Track
from ..providers.audiometa import AudioMetaProvider
from ..providers.base import ProviderCallOutcome, ProviderStatus, canonical_provider_family
from ..providers.discogs import DiscogsProvider
from ..providers.hdrr import HDRRProvider
from ..providers.musicbrainz import MusicBrainzProvider
from ..providers.research import ResearchProvider
from ..providers.sonovault import SonoVaultProvider
from .genre import GENERIC_TAGS, canonical_genre
from .matcher import norm, parse_filename_identity, verify_recording_candidate


_REAL_TRANSIENT_HTTP_FAILURE_STATUSES = {
    ProviderStatus.RATE_LIMITED,
    ProviderStatus.TIMEOUT,
    ProviderStatus.SERVER_ERROR,
    ProviderStatus.NETWORK_ERROR,
}

_DEFERRED_WITHOUT_CALL_STATUSES = {
    ProviderStatus.COOLDOWN_DEFERRED,
    ProviderStatus.BUDGET_EXHAUSTED,
}

_RETRYABLE_OUTCOME_STATUSES = _REAL_TRANSIENT_HTTP_FAILURE_STATUSES | _DEFERRED_WITHOUT_CALL_STATUSES

_HARD_BLOCKED_OUTCOME_STATUSES = {
    ProviderStatus.AUTH_ERROR,
    ProviderStatus.QUOTA_EXCEEDED,
    ProviderStatus.UNSUPPORTED_ENDPOINT,
    ProviderStatus.MALFORMED_JSON,
}


def _default_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_utc(dt: Optional[datetime]) -> datetime:
    d = dt or _default_utc_now()
    if d.tzinfo is None:
        return d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _is_timed_quota_outcome(out: ProviderCallOutcome) -> bool:
    if out.status != ProviderStatus.QUOTA_EXCEEDED:
        return False
    return float(getattr(out, "retry_after_seconds", 0.0) or 0.0) > 0.0 or bool(
        str(getattr(out, "retry_until_utc", "") or "").strip()
    )


def _snapshot_provider_counters(p_inst: Any) -> Tuple[int, int, int]:
    stats = getattr(p_inst, "stats", None)
    if not isinstance(stats, dict):
        return 0, 0, 0
    calls = int(stats.get("calls", 0))
    cache_hits = int(stats.get("cache_hits", 0))
    skipped = (
        int(stats.get("skipped_cooldown", 0))
        + int(stats.get("skipped_blocked", 0))
        + int(stats.get("skipped_budget", 0))
    )
    return calls, cache_hits, skipped


def _has_verified_catalogue_genre(candidates: List[Candidate]) -> bool:
    for c in candidates:
        if c.verification not in ("ISRC supported", "Duration supported"):
            continue
        fam = canonical_provider_family(c.source_family or c.provider)
        if fam in ("wikidata", "hdrr") or getattr(c, "genre_evidence_kind", "") == "artist_derived":
            continue
        for g in (c.genre or []):
            if isinstance(g, str) and norm(g) not in GENERIC_TAGS and canonical_genre(g):
                return True
    return False


def _has_verified_recording(candidates: List[Candidate]) -> bool:
    return any(
        c.verification in ("ISRC supported", "Duration supported")
        and canonical_provider_family(c.source_family or c.provider) != "wikidata"
        for c in candidates
    )


@dataclass
class ResearchRunSummary:
    """Honest, auditable summary of a metadata research run."""
    live_total: int = 0
    quarantined_excluded: int = 0
    stale_excluded: int = 0
    deferred_missing_identity: int = 0
    deferred_title_only: int = 0
    already_completed: int = 0
    eligible_queue_size: int = 0
    attempted: int = 0
    matched_verified: int = 0
    matched_verified_with_genre: int = 0
    matched_verified_no_genre: int = 0
    matched_review_only: int = 0
    no_match: int = 0
    failed_retryable: int = 0
    failed_permanent: int = 0
    unresolved_blocked: int = 0
    completed_tracks: int = 0
    incomplete_tracks: int = 0
    deferred_without_attempt: int = 0
    deferred_retry_not_due: int = 0
    exhausted_retry_limit: int = 0
    paused_provider_budget: bool = False
    paused_provider_cooldown: bool = False
    paused_all_providers_unavailable: bool = False
    interrupted: bool = False
    no_work_reason: str = ""
    next_retry_utc: str = ""
    total_actual_http_requests: int = 0
    total_cache_hits: int = 0
    provider_cooldowns: Dict[str, str] = field(default_factory=dict)
    provider_telemetry: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    actionable_issues: List[str] = field(default_factory=list)


def build_research_providers(config, db, limit, clock_utc=None, clock_mono=None, sleep_fn=None):
    terms_ok = config.provider_terms_cleared
    clock_kwargs: Dict[str, Any] = {}
    if clock_utc is not None:
        clock_kwargs["clock_utc"] = clock_utc
    if clock_mono is not None:
        clock_kwargs["clock_mono"] = clock_mono
    if sleep_fn is not None:
        clock_kwargs["sleep_fn"] = sleep_fn

    scaled_req_cap = min(250, max(30, int(limit) * 5))
    sono = (
        SonoVaultProvider(
            api_key=config.sonovault_api_key,
            contact=config.contact,
            max_requests=min(200, max(25, int(limit) * 3)),
            db=db,
            provider_terms_cleared=terms_ok,
            **clock_kwargs,
        )
        if config.sonovault_api_key
        else None
    )
    am = (
        AudioMetaProvider(
            api_key=config.audiometa_api_key,
            contact=config.contact,
            max_requests=scaled_req_cap,
            db=db,
            provider_terms_cleared=terms_ok,
            **clock_kwargs,
        )
        if config.audiometa_api_key
        else None
    )
    dc = (
        DiscogsProvider(
            token=config.discogs_token,
            contact=config.contact,
            max_requests=min(250, max(40, int(limit) * 4)),
            db=db,
            provider_terms_cleared=terms_ok,
            **clock_kwargs,
        )
        if config.discogs_token
        else None
    )
    mb = MusicBrainzProvider(
        contact=config.contact,
        max_requests=scaled_req_cap,
        db=db,
        provider_terms_cleared=terms_ok,
        **clock_kwargs,
    )
    wiki = ResearchProvider(
        contact=config.contact,
        max_requests=min(100, max(15, int(limit) * 2)),
        db=db,
        provider_terms_cleared=terms_ok,
        **clock_kwargs,
    )
    hdrr_delay = float(config.get("HDRR_REQUEST_DELAY_SECONDS", "2.0") or 2.0)
    hdrr_ttl = int(config.get("HDRR_CACHE_TTL_SECONDS", str(7 * 86400)) or (7 * 86400))
    hdrr = (
        HDRRProvider(
            contact=config.contact,
            max_requests=min(150, max(20, int(limit) * 3)),
            db=db,
            provider_terms_cleared=terms_ok,
            default_delay=hdrr_delay,
            cache_ttl_seconds=hdrr_ttl,
            offline_only=config.get_bool("HDRR_OFFLINE_ONLY", False),
            **clock_kwargs,
        )
        if config.get_bool("HDRR_ENABLED", True)
        else None
    )

    return {"sono": sono, "am": am, "dc": dc, "mb": mb, "hdrr": hdrr, "wiki": wiki}


def execute_research_batch(
    config: Config,
    db: Database,
    limit: int = 10,
    force_refresh: bool = False,
    include_title_only: bool = False,
    enable_wikidata_fallback: bool = True,
    providers_override: Optional[Dict[str, Any]] = None,
    progress_callback: Optional[Callable[[int, int, Track, str, int], None]] = None,
    clock_utc: Optional[Callable[[], datetime]] = None,
    clock_mono: Optional[Callable[[], float]] = None,
    sleep_fn: Optional[Callable[[float], None]] = None,
    selected_tracks: Optional[List[Track]] = None,
    policy: Optional[Any] = None,
) -> ResearchRunSummary:
    """
    Execute bounded metadata research on up to `limit` actually attempted live tracks.
    Never counts skipped/missing-identity or pure cooldown/budget-deferred rows toward `--limit`,
    never shortens server retry waits, and preserves/merges valid candidates non-destructively.
    """
    now_fn = clock_utc or _default_utc_now
    cur_utc = _norm_utc(now_fn())

    summary = ResearchRunSummary()
    if selected_tracks is None:
        inv = db.get_inventory_summary()
        summary.live_total = inv["live_source_tracks"]
        summary.quarantined_excluded = inv["quarantined_records"]
        summary.stale_excluded = inv["stale_live_tracks"]

        all_live = db.get_all_tracks(live_only=True)
        for t in all_live:
            if t.content_stale:
                continue
            has_full_id = bool(t.metadata.artist.strip() and t.metadata.title.strip())
            if not has_full_id:
                if t.metadata.identity_confidence == "title_only" or t.metadata.filename_title_hint:
                    summary.deferred_title_only += 1
                else:
                    summary.deferred_missing_identity += 1
            elif (
                t.research_status in ("success", "partial", "no_match")
                and not t.research_incomplete
                and (not t.research_version or t.research_version == t.metadata.version)
            ):
                summary.already_completed += 1

        q_diag = db.get_research_queue_diagnostics(
            include_title_only=include_title_only,
            now_utc=cur_utc,
        )
        summary.deferred_retry_not_due = q_diag["deferred_retry_not_due"]
        summary.exhausted_retry_limit = q_diag["exhausted_retry_limit"]
        summary.next_retry_utc = q_diag["earliest_next_retry_utc"]
        for p_fam, p_state in (q_diag.get("active_provider_cooldowns") or {}).items():
            cd_u = str(p_state.get("cooldown_until_utc") or "")
            if cd_u:
                summary.provider_cooldowns[p_fam] = cd_u

        queue = db.get_live_tracks_for_research(
            limit=max(1, int(limit)),
            force_refresh=force_refresh,
            include_title_only=include_title_only,
            now_utc=cur_utc,
        )
        summary.eligible_queue_size = len(queue)

    else:
        q_diag = {"unresolved_blocked": 0}
        queue = selected_tracks
        summary.live_total = summary.eligible_queue_size = len(queue)

    if not queue:
        if summary.live_total == 0:
            summary.no_work_reason = "No live source tracks in database. Run 'musicvault scan' first."
        elif summary.deferred_retry_not_due > 0:
            summary.no_work_reason = (
                f"{summary.deferred_retry_not_due} eligible track(s) have incomplete/retryable research deferred "
                f"until {summary.next_retry_utc or 'cooldown expiry'} "
                f"(completed={summary.already_completed}; wait for retry window rather than forcing refresh)."
            )
        elif q_diag["unresolved_blocked"] > 0 or summary.exhausted_retry_limit > 0:
            summary.no_work_reason = (
                f"No tracks currently due ({summary.exhausted_retry_limit} reached retry cap, "
                f"{q_diag['unresolved_blocked']} blocked by provider auth/quota; "
                "inspect with 'musicvault recover-research' or update keys via 'musicvault setup')."
            )
        elif summary.already_completed > 0 and summary.deferred_missing_identity == 0 and summary.deferred_title_only == 0:
            summary.no_work_reason = (
                f"All {summary.already_completed} eligible tracks with identity have already been researched "
                "(use --force-refresh to re-query)."
            )
        elif summary.already_completed > 0 and (summary.deferred_missing_identity > 0 or summary.deferred_title_only > 0):
            summary.no_work_reason = (
                f"All {summary.already_completed} ready tracks have already been researched; remaining live tracks "
                f"lack full artist+title ({summary.deferred_missing_identity} missing identity, "
                f"{summary.deferred_title_only} title-only — run 'musicvault hydrate' or pass --include-title-only)."
            )
        elif summary.deferred_missing_identity > 0 or summary.deferred_title_only > 0:
            summary.no_work_reason = (
                f"No eligible pending tracks with artist+title ({summary.deferred_missing_identity} missing identity, "
                f"{summary.deferred_title_only} title-only). Run 'musicvault hydrate' first."
            )
        else:
            summary.no_work_reason = "No eligible tracks due for research or retry."
        return summary

    providers = providers_override if providers_override is not None else build_research_providers(
        config, db, limit, clock_utc, clock_mono, sleep_fn
    )
    sono, am, dc, mb, hdrr, wiki = (providers.get(key) for key in ("sono", "am", "dc", "mb", "hdrr", "wiki"))

    seen_issue_keys = set()
    observed_retry_deadlines: List[str] = []

    def _record_issue(outcome: ProviderCallOutcome) -> None:
        if outcome.status in (
            ProviderStatus.AUTH_ERROR,
            ProviderStatus.QUOTA_EXCEEDED,
            ProviderStatus.RATE_LIMITED,
            ProviderStatus.COOLDOWN_DEFERRED,
            ProviderStatus.UNSUPPORTED_ENDPOINT,
            ProviderStatus.MALFORMED_JSON,
        ):
            key = (outcome.provider, outcome.status.value)
            if key not in seen_issue_keys:
                seen_issue_keys.add(key)
                summary.actionable_issues.append(
                    config.redact(f"{outcome.provider}: {outcome.status.value} ({outcome.error})")
                )
        ru = getattr(outcome, "retry_until_utc", "") or ""
        if ru:
            observed_retry_deadlines.append(ru)

    try:
        for idx, t in enumerate(queue, 1):
            if summary.attempted >= max(1, int(limit)):
                break

            track_now_utc = _norm_utc(now_fn())
            ver = t.metadata.version or t.md5 or t.modified_time or ""
            artist = t.metadata.artist.strip()
            title = t.metadata.title.strip()
            title_only_mode = not (artist and title)

            parsed_fn = parse_filename_identity(t.name)
            alt_artist = parsed_fn.alt_artist if t.metadata.identity_confidence == "filename_ambiguous" else ""
            alt_title = parsed_fn.alt_title if t.metadata.identity_confidence == "filename_ambiguous" else ""

            prior_audit: Dict[str, Any] = {}
            if not force_refresh and (not t.research_version or t.research_version == ver) and t.provider_calls_json:
                try:
                    pa = json.loads(t.provider_calls_json)
                    if isinstance(pa, dict):
                        prior_audit = pa
                except ValueError:
                    prior_audit = {}

            track_candidates: List[Candidate] = []
            provider_audit: Dict[str, Any] = {}
            configured_providers = 0
            skipped_already_done_same_version = 0
            clean_catalogue_responses = 0
            retryable_incomplete_count = 0
            hard_blocked_incomplete_count = 0
            real_http_failure_count = 0
            budget_exhausted_without_call = 0
            cooldown_deferred_without_call = 0
            hard_blocked_without_call = 0
            track_actual_calls = 0
            track_cache_hits = 0

            retry_waits_sec: List[float] = []
            retry_deadlines_utc: List[str] = []

            def _collect_wait(out_obj: ProviderCallOutcome) -> None:
                w = float(getattr(out_obj, "retry_after_seconds", 0.0) or 0.0)
                if w > 0.0:
                    retry_waits_sec.append(w)
                ru = str(getattr(out_obj, "retry_until_utc", "") or "").strip()
                if ru:
                    retry_deadlines_utc.append(ru)
                elif w > 0.0:
                    retry_deadlines_utc.append((track_now_utc + timedelta(seconds=w)).isoformat())

            if title_only_mode:
                t_hint = t.metadata.filename_title_hint or title or parsed_fn.title_hint
                if mb is not None and t_hint:
                    configured_providers += 1
                    c0, h0, s0 = _snapshot_provider_counters(mb)
                    cands, out = mb.search_title_only(t_hint)
                    c1, h1, s1 = _snapshot_provider_counters(mb)
                    d_calls = max(int(out.request_made), c1 - c0)
                    d_cache = max(int(out.from_cache), h1 - h0)
                    d_skip = max(0, s1 - s0)
                    track_actual_calls += d_calls
                    track_cache_hits += d_cache

                    _record_issue(out)
                    is_clean = out.status in (ProviderStatus.OK, ProviderStatus.EMPTY)
                    timed_quota = _is_timed_quota_outcome(out)
                    is_retry = (out.status in _RETRYABLE_OUTCOME_STATUSES) or timed_quota
                    is_hard = out.status in _HARD_BLOCKED_OUTCOME_STATUSES

                    # Always retain any fetched partial candidates even if a follow-up sub-request failed
                    if cands:
                        track_candidates.extend(cands)

                    provider_audit["MusicBrainz"] = {
                        "status": out.status.value,
                        "error_kind": getattr(out, "error_kind", ""),
                        "http": out.http_status,
                        "matches": len(cands),
                        "from_cache": out.from_cache,
                        "request_made": out.request_made,
                        "actual_calls": d_calls,
                        "cache_hits": d_cache,
                        "skipped_calls": d_skip,
                        "lookup_completed": is_clean,
                        "retryable": is_retry,
                        "retry_until_utc": getattr(out, "retry_until_utc", ""),
                        "mode": "title_only",
                        "query_title": t_hint,
                    }
                    if is_clean:
                        clean_catalogue_responses += 1
                    if is_retry:
                        retryable_incomplete_count += 1
                        _collect_wait(out)
                        if out.request_made and (out.status in _REAL_TRANSIENT_HTTP_FAILURE_STATUSES or timed_quota):
                            real_http_failure_count += 1
                        else:
                            if out.status == ProviderStatus.BUDGET_EXHAUSTED:
                                budget_exhausted_without_call += 1
                            elif out.status == ProviderStatus.COOLDOWN_DEFERRED or timed_quota:
                                cooldown_deferred_without_call += 1
                    if is_hard:
                        hard_blocked_incomplete_count += 1
                        _collect_wait(out)
                        if not out.request_made and not out.from_cache and not timed_quota:
                            hard_blocked_without_call += 1
            else:
                active_calls = [
                    ("SonoVault", sono, lambda p, a, ti: p.search_detailed(a, ti, isrc=t.metadata.isrc)),
                    ("AudioMeta", am, lambda p, a, ti: p.search_detailed(a, ti)),
                    ("Discogs", dc, lambda p, a, ti: p.search_detailed(a, ti)),
                    ("MusicBrainz", mb, lambda p, a, ti: p.search_detailed(a, ti, isrc=t.metadata.isrc)),
                    ("HDRR", hdrr, lambda p, a, ti: p.search_detailed(a, ti, isrc=t.metadata.isrc)),
                ]

                def verify(candidates):
                    verified = []
                    for candidate in candidates:
                        candidate.verification = verify_recording_candidate(
                            query_artist=artist, query_title=title,
                            identity_confidence=t.metadata.identity_confidence or "verified_embedded",
                            local_isrc=t.metadata.isrc, local_duration=t.metadata.duration,
                            candidate_title=candidate.title, candidate_artists=candidate.artists,
                            candidate_isrc=candidate.isrc, candidate_duration=candidate.duration,
                            alt_artist=alt_artist, alt_title=alt_title,
                        )
                        candidate.content_version = ver
                        if candidate.verification not in ("Identity mismatch", "Version conflict"):
                            verified.append(candidate)
                    return verified

                if policy is not None:
                    stored = [c for c in db.get_candidates(t.file_id)
                              if (not c.content_version or c.content_version == ver)
                              and policy.permits(canonical_provider_family(c.source_family or c.provider))]
                    remaining_calls = list(active_calls)
                else:
                    remaining_calls = active_calls
                while remaining_calls:
                    if policy is not None:
                        combined_so_far = stored + verify(track_candidates)
                        needed = policy.needed_fields(t, combined_so_far)
                        remaining_calls = policy.ordered(remaining_calls, needed)
                    p_name, p_inst, call_fn = remaining_calls.pop(0)
                    if p_inst is None:
                        if policy is not None and policy.required is not None and policy.is_required(canonical_provider_family(p_name)):
                            configured_providers += 1
                            hard_blocked_incomplete_count += 1
                            hard_blocked_without_call += 1
                        provider_audit[p_name] = {
                            "status": "not_configured",
                            "matches": 0,
                            "request_made": False,
                            "actual_calls": 0,
                            "cache_hits": 0,
                            "skipped_calls": 0,
                            "lookup_completed": False,
                            "retryable": False,
                        }
                        continue

                    # Reuse already-completed provider outcome ONLY when version AND exact (artist, title) match
                    prev_entry = prior_audit.get(p_name)
                    if (
                        not force_refresh
                        and isinstance(prev_entry, dict)
                        and prev_entry.get("lookup_completed") is True
                        and str(prev_entry.get("status") or "") in ("ok", "empty")
                        and prev_entry.get("query_artist") == artist
                        and prev_entry.get("query_title") == title
                        and (policy is None or prev_entry.get("query_isrc", t.metadata.isrc) == t.metadata.isrc)
                    ):
                        configured_providers += 1
                        skipped_already_done_same_version += 1
                        clean_catalogue_responses += 1
                        provider_audit[p_name] = {
                            **prev_entry,
                            "reused_completed_same_version": True,
                            "request_made": False,
                            "actual_calls": 0,
                            "cache_hits": 0,
                            "skipped_calls": 1,
                            "lookup_completed": True,
                            "retryable": False,
                        }
                        continue

                    if policy is not None:
                        family = canonical_provider_family(p_name)
                        effective_needed = (
                            policy.fallback_needed_fields(t, combined_so_far)
                            if policy.is_fallback_only(family)
                            else needed
                        )
                        skip = policy.reason_to_skip(family, effective_needed, track=t, candidates=combined_so_far)
                        if skip:
                            provider_audit[p_name] = {
                                "status": skip, "lookup_completed": False, "actual_calls": 0,
                                "request_made": False, "query_artist": artist, "query_title": title,
                            }
                            if skip == "outside_provider_selection":
                                if isinstance(prev_entry, dict):
                                    provider_audit[p_name] = prev_entry
                                # An absent required audit must not look like a successful complete run.
                                if policy.is_required(family):
                                    hard_blocked_incomplete_count += 1
                                continue
                            policy.checkpoint(t, p_name, provider_audit[p_name])
                            if skip == "disabled_by_policy" and policy.required is not None and policy.is_required(family):
                                configured_providers += 1
                                hard_blocked_incomplete_count += 1
                                hard_blocked_without_call += 1
                            continue

                        incomplete_primaries = policy.primary_incomplete_for_fallback(
                            family, effective_needed, provider_audit
                        )
                        if incomplete_primaries:
                            from .provider_policy import AUDIT_NAMES as _AUDIT_NAMES
                            prim_deadlines = [
                                str(provider_audit.get(_AUDIT_NAMES[pf], {}).get("retry_until_utc") or "").strip()
                                for pf in incomplete_primaries
                            ]
                            prim_deadlines = [d for d in prim_deadlines if d]
                            defer_until = min(prim_deadlines) if prim_deadlines else ""
                            prim_retryable = bool(
                                defer_until
                                or any(
                                    provider_audit.get(_AUDIT_NAMES[pf], {}).get("retryable")
                                    for pf in incomplete_primaries
                                )
                            )
                            provider_audit[p_name] = {
                                "status": "deferred_primary_incomplete",
                                "waiting_on": incomplete_primaries,
                                "lookup_completed": False,
                                "retryable": prim_retryable,
                                "retry_until_utc": defer_until,
                                "actual_calls": 0,
                                "cache_hits": 0,
                                "skipped_calls": 1,
                                "request_made": False,
                                "query_artist": artist,
                                "query_title": title,
                            }
                            policy.checkpoint(t, p_name, provider_audit[p_name])
                            configured_providers += 1
                            if prim_retryable:
                                retryable_incomplete_count += 1
                                cooldown_deferred_without_call += 1
                            else:
                                hard_blocked_incomplete_count += 1
                                hard_blocked_without_call += 1
                            continue

                    configured_providers += 1
                    c0, h0, s0 = _snapshot_provider_counters(p_inst)
                    old_primary = (prev_entry.get("primary_query") or {}) if isinstance(prev_entry, dict) else {}
                    reuse_empty = (
                        policy is not None and alt_artist and alt_title
                        and old_primary.get("status") in ("ok", "empty")
                        and old_primary.get("matches") == 0
                        and prev_entry.get("query_artist") == artist
                        and prev_entry.get("query_title") == title
                        and prev_entry.get("query_isrc", t.metadata.isrc) == t.metadata.isrc
                    )
                    if reuse_empty:
                        prim_cands = []
                        prim_out = ProviderCallOutcome(provider=p_name, status=ProviderStatus.EMPTY)
                    else:
                        prim_cands, prim_out = call_fn(p_inst, artist, title)
                    _record_issue(prim_out)

                    prim_audit: Dict[str, Any] = {
                        "orientation": "primary",
                        "status": prim_out.status.value,
                        "error_kind": getattr(prim_out, "error_kind", ""),
                        "http": prim_out.http_status,
                        "matches": len(prim_cands),
                        "from_cache": prim_out.from_cache,
                        "request_made": prim_out.request_made,
                        "retry_until_utc": getattr(prim_out, "retry_until_utc", ""),
                    }
                    alt_audit: Optional[Dict[str, Any]] = None
                    effective_cands = list(prim_cands)
                    effective_out = prim_out

                    # Ambiguous 'A - B' filename: test swapped orientation whenever primary returned 0 matches cleanly
                    if (
                        not prim_cands
                        and alt_artist
                        and alt_title
                        and prim_out.status in (ProviderStatus.OK, ProviderStatus.EMPTY)
                    ):
                        if policy is not None and not reuse_empty:
                            # Persist completed empty primary orientation before starting swapped HTTP call
                            # so a hard kill during the alternate lookup never replays the primary request.
                            policy.checkpoint(
                                t,
                                p_name,
                                {
                                    "status": "in_progress_alternate",
                                    "lookup_completed": False,
                                    "retryable": True,
                                    "request_made": False,
                                    "query_artist": artist,
                                    "query_title": title,
                                    "query_isrc": t.metadata.isrc,
                                    "primary_query": prim_audit,
                                },
                            )
                        alt_cands, alt_out = call_fn(p_inst, alt_artist, alt_title)
                        _record_issue(alt_out)
                        alt_timed_q = _is_timed_quota_outcome(alt_out)
                        alt_audit = {
                            "orientation": "swapped",
                            "status": alt_out.status.value,
                            "error_kind": getattr(alt_out, "error_kind", ""),
                            "http": alt_out.http_status,
                            "matches": len(alt_cands),
                            "from_cache": alt_out.from_cache,
                            "request_made": alt_out.request_made,
                            "lookup_completed": alt_out.status in (ProviderStatus.OK, ProviderStatus.EMPTY),
                            "retryable": (alt_out.status in _RETRYABLE_OUTCOME_STATUSES) or alt_timed_q,
                            "retry_until_utc": getattr(alt_out, "retry_until_utc", ""),
                        }
                        if alt_cands:
                            effective_cands = list(alt_cands)
                            effective_out = alt_out
                        elif alt_out.status not in (ProviderStatus.OK, ProviderStatus.EMPTY):
                            # Never hide a transient/deferred/blocked alternate query failure behind primary EMPTY!
                            effective_out = alt_out

                    c1, h1, s1 = _snapshot_provider_counters(p_inst)
                    d_calls = max(int(prim_out.request_made), c1 - c0)
                    d_cache = max(int(prim_out.from_cache), h1 - h0)
                    d_skip = max(0, s1 - s0)
                    track_actual_calls += d_calls
                    track_cache_hits += d_cache

                    is_clean = effective_out.status in (ProviderStatus.OK, ProviderStatus.EMPTY)
                    timed_quota = _is_timed_quota_outcome(effective_out)
                    is_retry = (effective_out.status in _RETRYABLE_OUTCOME_STATUSES) or timed_quota
                    is_hard = effective_out.status in _HARD_BLOCKED_OUTCOME_STATUSES

                    # Always retain any fetched partial candidates even when a detail sub-request hit 429/cooldown/cap!
                    if effective_cands:
                        track_candidates.extend(effective_cands)

                    provider_audit[p_name] = {
                        "status": effective_out.status.value,
                        "error_kind": getattr(effective_out, "error_kind", ""),
                        "http": effective_out.http_status,
                        "matches": len(effective_cands),
                        "from_cache": effective_out.from_cache,
                        "request_made": bool(d_calls > 0 or effective_out.request_made),
                        "actual_calls": d_calls,
                        "cache_hits": d_cache,
                        "skipped_calls": d_skip,
                        "lookup_completed": is_clean,
                        "retryable": is_retry,
                        "retry_until_utc": getattr(effective_out, "retry_until_utc", ""),
                        "query_artist": artist,
                        "query_title": title,
                        "query_isrc": t.metadata.isrc,
                        "primary_query": prim_audit,
                        "alternate_query": alt_audit,
                    }

                    if policy is not None:
                        policy.checkpoint(t, p_name, provider_audit[p_name], verify(effective_cands))

                    if is_clean:
                        clean_catalogue_responses += 1
                    if is_retry:
                        retryable_incomplete_count += 1
                        _collect_wait(effective_out)
                        if (
                            (prim_out.request_made and (prim_out.status in _REAL_TRANSIENT_HTTP_FAILURE_STATUSES or _is_timed_quota_outcome(prim_out)))
                            or (
                                alt_audit is not None
                                and alt_audit.get("request_made")
                                and (effective_out.status in _REAL_TRANSIENT_HTTP_FAILURE_STATUSES or timed_quota)
                            )
                        ):
                            real_http_failure_count += 1
                        else:
                            if effective_out.status == ProviderStatus.BUDGET_EXHAUSTED:
                                budget_exhausted_without_call += 1
                            elif effective_out.status == ProviderStatus.COOLDOWN_DEFERRED or timed_quota:
                                cooldown_deferred_without_call += 1
                    if is_hard:
                        hard_blocked_incomplete_count += 1
                        _collect_wait(effective_out)
                        if d_calls == 0 and d_cache == 0 and not timed_quota:
                            hard_blocked_without_call += 1

            # Check if ZERO needed providers for THIS track could execute any HTTP or cached lookup
            needed_providers = configured_providers - skipped_already_done_same_version
            unattempted_unavailable = (
                budget_exhausted_without_call + cooldown_deferred_without_call + hard_blocked_without_call
            )
            if (
                needed_providers > 0
                and unattempted_unavailable == needed_providers
                and track_actual_calls == 0
                and track_cache_hits == 0
            ):
                summary.deferred_without_attempt += 1
                if policy is not None and not retry_deadlines_utc:
                    db.record_research_deferral(
                        file_id=t.file_id, retry_until_utc="",
                        reason="Deferred: no applicable provider request could run",
                        now_utc=track_now_utc,
                    )
                if retry_deadlines_utc:
                    earliest_cd = min(retry_deadlines_utc)
                    if not summary.next_retry_utc or earliest_cd < summary.next_retry_utc:
                        summary.next_retry_utc = earliest_cd
                    # Persist retry deadline whenever a timed cooldown or timed quota applies
                    db.record_research_deferral(
                        file_id=t.file_id,
                        retry_until_utc=earliest_cd,
                        reason="Deferred: needed provider(s) cooling down (attempts preserved)",
                        now_utc=track_now_utc,
                    )

                any_healthy_provider = (
                    (mb is not None and mb.can_request())
                    if title_only_mode
                    else any(
                        p is not None and p.can_request()
                        and (policy is None or (
                            policy.permits(canonical_provider_family(name))
                            and not policy.is_fallback_only(canonical_provider_family(name))
                            and (not policy.provider or policy.provider == canonical_provider_family(name))
                        ))
                        for name, p in (("SonoVault", sono), ("AudioMeta", am), ("Discogs", dc), ("MusicBrainz", mb), ("HDRR", hdrr))
                    )
                )
                if not any_healthy_provider:
                    if budget_exhausted_without_call > 0:
                        summary.paused_provider_budget = True
                    if cooldown_deferred_without_call > 0:
                        summary.paused_provider_cooldown = True
                    summary.paused_all_providers_unavailable = True
                    break
                # At least one configured provider is still healthy for subsequent tracks in queue -> continue!
                continue

            if not title_only_mode:
                track_candidates = verify(track_candidates)

                existing_active = [
                    c for c in db.get_candidates(t.file_id)
                    if (not c.content_version or c.content_version == ver)
                    and (policy is None or policy.permits(canonical_provider_family(c.source_family or c.provider)))
                ]
                has_good_genre = _has_verified_catalogue_genre(existing_active + track_candidates)

                if (
                    enable_wikidata_fallback
                    and wiki is not None
                    and not has_good_genre
                    and (policy is None or not policy.provider)
                    and clean_catalogue_responses > 0
                ):
                    wc0, wh0, ws0 = _snapshot_provider_counters(wiki)
                    if hasattr(wiki, "research_track_detailed"):
                        res_info, w_out = wiki.research_track_detailed(artist, title)
                    else:
                        res_info = wiki.research_track(artist, title)
                        w_out = ProviderCallOutcome(provider="OnlineResearch", status=ProviderStatus.OK, request_made=True)
                    wc1, wh1, ws1 = _snapshot_provider_counters(wiki)
                    wd_calls = max(0, wc1 - wc0)
                    wd_cache = max(0, wh1 - wh0)
                    wd_skip = max(0, ws1 - ws0)
                    track_actual_calls += wd_calls
                    track_cache_hits += wd_cache

                    w_hints = res_info.get("genre_hints") or []
                    w_added = 0
                    for h in w_hints:
                        if not isinstance(h, dict):
                            continue
                        p_genres = [str(g) for g in (h.get("parent_genres") or []) if isinstance(g, str) and g]
                        if p_genres:
                            track_candidates.append(
                                Candidate(
                                    provider="Wikidata (inferred)",
                                    title=title,
                                    artists=[artist],
                                    genre=p_genres,
                                    url=str(h.get("url") or ""),
                                    verification="Review only (inferred)",
                                    content_version=ver,
                                    match_confidence="inferred_knowledge_graph",
                                    source_family="wikidata",
                                )
                            )
                            w_added += 1
                    provider_audit["Wikidata"] = {
                        "status": w_out.status.value if w_out.status != ProviderStatus.OK else ("ok" if w_added else "empty"),
                        "matches": w_added,
                        "evidence_type": "inferred",
                        "actual_calls": wd_calls,
                        "cache_hits": wd_cache,
                        "skipped_calls": wd_skip,
                    }
            else:
                for c in track_candidates:
                    c.content_version = ver

            summary.attempted += 1

            existing_same_ver = [
                c for c in db.get_candidates(t.file_id)
                    if (not c.content_version or c.content_version == ver)
                    and (policy is None or policy.permits(canonical_provider_family(c.source_family or c.provider)))
            ]
            combined_preview = existing_same_ver + track_candidates
            has_rec_verified = (not title_only_mode) and _has_verified_recording(combined_preview)
            has_ver_genre = (not title_only_mode) and _has_verified_catalogue_genre(combined_preview)
            has_any_cands = bool(combined_preview)

            has_retryable = retryable_incomplete_count > 0
            has_hard_block = hard_blocked_incomplete_count > 0
            lookup_complete = (not has_retryable) and (not has_hard_block) and (clean_catalogue_responses > 0)

            # Retry wait calculation: NEVER cap server-communicated waits at 3600s!
            retry_delay_s = 0.0
            target_retry_utc = ""
            if has_retryable:
                if retry_waits_sec:
                    retry_delay_s = min(retry_waits_sec)
                elif real_http_failure_count > 0:
                    retry_delay_s = min(3600.0, max(60.0, 120.0 * (2 ** t.research_attempts)))
                else:
                    retry_delay_s = 0.0

                if retry_deadlines_utc:
                    target_retry_utc = min(retry_deadlines_utc)
                elif retry_delay_s > 0.0:
                    target_retry_utc = (track_now_utc + timedelta(seconds=retry_delay_s)).isoformat()

                if target_retry_utc:
                    observed_retry_deadlines.append(target_retry_utc)

            # Attempt accounting: never consume track attempts when incomplete providers were purely deferred by cooldown/cap!
            consume_attempt = bool(
                lookup_complete
                or real_http_failure_count > 0
                or (has_hard_block and not has_retryable)
            )
            increment_deferral = bool(has_retryable and not consume_attempt)

            # Determine honest checkpoint status (never downgrading verified recordings or busy-looping untimed hard blocks)
            if has_retryable:
                is_incomplete = True
                summary.incomplete_tracks += 1
                if clean_catalogue_responses == 0 and not track_candidates:
                    next_status = "retryable_failure"
                    err_text = "Transient provider rate-limit/outage; prior evidence preserved"
                    summary.failed_retryable += 1
                elif not has_any_cands:
                    next_status = "retryable_failure"
                    err_text = "Incomplete lookup (empty + retryable provider); scheduled for retry"
                    summary.failed_retryable += 1
                elif has_rec_verified:
                    next_status = "success"
                    err_text = "Verified recording retained; remaining retryable provider(s) scheduled for retry"
                    summary.matched_verified += 1
                    if has_ver_genre:
                        summary.matched_verified_with_genre += 1
                    else:
                        summary.matched_verified_no_genre += 1
                else:
                    next_status = "partial"
                    err_text = "Incomplete lookup (partial evidence retained; remaining provider(s) scheduled for retry)"
                    summary.matched_review_only += 1
            elif has_hard_block and not lookup_complete:
                summary.incomplete_tracks += 1
                if has_any_cands:
                    # Untimed hard block (e.g. 401/404) with candidates: do NOT set research_incomplete=True
                    # (which would busy-loop on every run); flag in _meta so recover-research reports it cleanly!
                    is_incomplete = False
                    err_text = "Incomplete lookup: one or more providers blocked by auth/quota/endpoint error"
                    if has_rec_verified:
                        next_status = "success"
                        summary.matched_verified += 1
                        if has_ver_genre:
                            summary.matched_verified_with_genre += 1
                        else:
                            summary.matched_verified_no_genre += 1
                    else:
                        next_status = "partial"
                        summary.matched_review_only += 1
                else:
                    # Never treat blocked providers as definitive no_match; 'unresolved' is excluded from busy-retry!
                    is_incomplete = True
                    next_status = "unresolved"
                    err_text = "Blocked provider(s) prevented complete lookup; not a definitive no_match"
                    summary.unresolved_blocked += 1
            else:
                is_incomplete = False
                err_text = ""
                if has_rec_verified:
                    summary.completed_tracks += 1
                    next_status = "success"
                    summary.matched_verified += 1
                    if has_ver_genre:
                        summary.matched_verified_with_genre += 1
                    else:
                        summary.matched_verified_no_genre += 1
                elif has_any_cands:
                    summary.completed_tracks += 1
                    next_status = "partial"
                    summary.matched_review_only += 1
                elif clean_catalogue_responses > 0:
                    summary.completed_tracks += 1
                    next_status = "no_match"
                    summary.no_match += 1
                else:
                    consume_attempt = True
                    if configured_providers == 0:
                        next_status = "unresolved"
                        is_incomplete = False
                        summary.unresolved_blocked += 1
                        err_text = "No metadata providers configured or applicable for this track"
                    else:
                        next_status = "retryable_failure"
                        is_incomplete = True
                        summary.incomplete_tracks += 1
                        summary.failed_retryable += 1
                        retry_delay_s = 300.0
                        target_retry_utc = (track_now_utc + timedelta(seconds=retry_delay_s)).isoformat()
                        observed_retry_deadlines.append(target_retry_utc)
                        err_text = "No configured provider could complete lookup"

            provider_audit["_meta"] = {
                "schema": "v2.0.2",
                "lookup_complete": lookup_complete,
                "has_retryable_incomplete": has_retryable,
                "has_hard_blocked_incomplete": has_hard_block,
                "verified_identity": has_rec_verified,
                "has_verified_genre": has_ver_genre,
                "routing_ready_from_catalogue": bool(has_rec_verified and has_ver_genre),
                "next_retry_utc": target_retry_utc,
                "attempt_consumed": consume_attempt,
                "query_artist": artist,
                "query_title": title,
            }

            merged_after = db.update_research_result(
                file_id=t.file_id,
                status=next_status,
                version=ver,
                candidates=track_candidates,
                provider_calls=provider_audit,
                error_msg=config.redact(err_text),
                retry_delay_seconds=retry_delay_s,
                retry_until_utc=target_retry_utc,
                preserve_existing_candidates_on_failure=True,
                research_incomplete=is_incomplete,
                consume_attempt=consume_attempt,
                increment_deferral=increment_deferral,
                now_utc=track_now_utc,
            )

            if progress_callback:
                progress_callback(idx, len(queue), t, next_status, len(merged_after))

    except KeyboardInterrupt:
        summary.interrupted = True

    for p_label, p_obj in [
        ("SonoVault", sono),
        ("AudioMeta", am),
        ("Discogs", dc),
        ("MusicBrainz", mb),
        ("Wikidata", wiki),
    ]:
        if p_obj is not None and hasattr(p_obj, "stats"):
            st_copy = dict(p_obj.stats)
            cd_u = getattr(p_obj, "cooldown_until_utc", "") or ""
            if cd_u:
                summary.provider_cooldowns[p_label] = cd_u
                observed_retry_deadlines.append(cd_u)
            summary.provider_telemetry[p_label] = st_copy
            summary.total_actual_http_requests += int(st_copy.get("calls", 0))
            summary.total_cache_hits += int(st_copy.get("cache_hits", 0))

    if observed_retry_deadlines:
        earliest_obs = min(observed_retry_deadlines)
        if not summary.next_retry_utc or earliest_obs < summary.next_retry_utc:
            summary.next_retry_utc = earliest_obs

    return summary
