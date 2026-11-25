"""
Resumable, honest online metadata research scheduler and execution engine.
Selects only live, non-stale tracks with searchable identity, advances across batches without
repeating completed work, verifies recording identity + mix/version before duration/ISRC,
preserves prior valid candidates on transient provider failures, and reports exact telemetry.
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..config import Config
from ..db import Database
from ..models import Candidate, Track
from ..providers.audiometa import AudioMetaProvider
from ..providers.base import ProviderCallOutcome, ProviderStatus
from ..providers.discogs import DiscogsProvider
from ..providers.musicbrainz import MusicBrainzProvider
from ..providers.research import ResearchProvider
from ..providers.sonovault import SonoVaultProvider
from .matcher import parse_filename_identity, verify_recording_candidate


_TRANSIENT_FAILURE_STATUSES = {
    ProviderStatus.RATE_LIMITED,
    ProviderStatus.TIMEOUT,
    ProviderStatus.SERVER_ERROR,
    ProviderStatus.NETWORK_ERROR,
}


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
    matched_review_only: int = 0
    no_match: int = 0
    failed_retryable: int = 0
    failed_permanent: int = 0
    paused_provider_budget: bool = False
    interrupted: bool = False
    no_work_reason: str = ""
    provider_telemetry: Dict[str, Dict[str, int]] = field(default_factory=dict)
    actionable_issues: List[str] = field(default_factory=list)


def execute_research_batch(
    config: Config,
    db: Database,
    limit: int = 10,
    force_refresh: bool = False,
    include_title_only: bool = False,
    enable_wikidata_fallback: bool = True,
    providers_override: Optional[Dict[str, Any]] = None,
    progress_callback: Optional[Callable[[int, int, Track, str, int], None]] = None,
) -> ResearchRunSummary:
    """
    Execute bounded metadata research on up to `limit` actually attempted live tracks.
    Never counts skipped/missing-identity rows toward `--limit` and never prints fake success.
    """
    summary = ResearchRunSummary()
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
        elif t.research_status in ("success", "partial", "no_match") and (
            not t.research_version or t.research_version == t.metadata.version
        ):
            summary.already_completed += 1

    queue = db.get_live_tracks_for_research(
        limit=max(1, int(limit)),
        force_refresh=force_refresh,
        include_title_only=include_title_only,
    )
    summary.eligible_queue_size = len(queue)

    if not queue:
        if summary.live_total == 0:
            summary.no_work_reason = "No live source tracks in database. Run 'musicvault scan' first."
        elif summary.deferred_missing_identity > 0 or summary.deferred_title_only > 0:
            summary.no_work_reason = (
                f"No eligible pending tracks with artist+title ({summary.deferred_missing_identity} missing identity, "
                f"{summary.deferred_title_only} title-only). Run 'musicvault hydrate' first."
            )
        elif summary.already_completed > 0:
            summary.no_work_reason = (
                f"All {summary.already_completed} eligible tracks with identity have already been researched "
                "(use --force-refresh to re-query)."
            )
        else:
            summary.no_work_reason = "No eligible tracks due for research or retry."
        return summary

    terms_ok = config.provider_terms_cleared
    if providers_override is not None:
        sono = providers_override.get("sono")
        am = providers_override.get("am")
        dc = providers_override.get("dc")
        mb = providers_override.get("mb")
        wiki = providers_override.get("wiki")
    else:
        scaled_req_cap = min(250, max(30, int(limit) * 5))
        sono = (
            SonoVaultProvider(
                api_key=config.sonovault_api_key,
                contact=config.contact,
                max_requests=min(200, max(25, int(limit) * 3)),
                db=db,
                provider_terms_cleared=terms_ok,
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
            )
            if config.discogs_token
            else None
        )
        mb = MusicBrainzProvider(
            contact=config.contact,
            max_requests=scaled_req_cap,
            db=db,
            provider_terms_cleared=terms_ok,
        )
        wiki = ResearchProvider(
            contact=config.contact,
            max_requests=min(100, max(15, int(limit) * 2)),
            db=db,
            provider_terms_cleared=terms_ok,
        )

    seen_issue_keys = set()

    def _record_issue(outcome: ProviderCallOutcome) -> None:
        if outcome.status in (
            ProviderStatus.AUTH_ERROR,
            ProviderStatus.QUOTA_EXCEEDED,
            ProviderStatus.RATE_LIMITED,
            ProviderStatus.UNSUPPORTED_ENDPOINT,
            ProviderStatus.MALFORMED_JSON,
        ):
            key = (outcome.provider, outcome.status.value)
            if key not in seen_issue_keys:
                seen_issue_keys.add(key)
                summary.actionable_issues.append(
                    config.redact(f"{outcome.provider}: {outcome.status.value} ({outcome.error})")
                )

    try:
        for idx, t in enumerate(queue, 1):
            ver = t.metadata.version or t.md5 or t.modified_time or ""
            artist = t.metadata.artist.strip()
            title = t.metadata.title.strip()
            title_only_mode = not (artist and title)

            parsed_fn = parse_filename_identity(t.name)
            alt_artist = parsed_fn.alt_artist if t.metadata.identity_confidence == "filename_ambiguous" else ""
            alt_title = parsed_fn.alt_title if t.metadata.identity_confidence == "filename_ambiguous" else ""

            track_candidates: List[Candidate] = []
            provider_audit: Dict[str, Any] = {}
            transient_failures = 0
            successful_or_empty_responses = 0
            configured_providers = 0
            budget_exhausted_without_call = 0
            max_retry_wait = 0.0

            if title_only_mode:
                t_hint = t.metadata.filename_title_hint or title or parsed_fn.title_hint
                if mb is not None and t_hint:
                    configured_providers += 1
                    cands, out = mb.search_title_only(t_hint)
                    provider_audit["MusicBrainz"] = {
                        "status": out.status.value,
                        "http": out.http_status,
                        "matches": len(cands),
                        "request_made": out.request_made,
                    }
                    _record_issue(out)
                    if out.status == ProviderStatus.BUDGET_EXHAUSTED and not out.request_made and not out.from_cache:
                        budget_exhausted_without_call += 1
                    elif out.status in (ProviderStatus.OK, ProviderStatus.EMPTY):
                        successful_or_empty_responses += 1
                        track_candidates.extend(cands)
                    elif out.status in _TRANSIENT_FAILURE_STATUSES:
                        transient_failures += 1
                        max_retry_wait = max(max_retry_wait, out.retry_after_seconds or 120.0)
            else:
                # Full artist + title queries across configured providers
                active_calls = [
                    ("SonoVault", sono, lambda p, a, ti: p.search_detailed(a, ti, isrc=t.metadata.isrc)),
                    ("AudioMeta", am, lambda p, a, ti: p.search_detailed(a, ti)),
                    ("Discogs", dc, lambda p, a, ti: p.search_detailed(a, ti)),
                    ("MusicBrainz", mb, lambda p, a, ti: p.search_detailed(a, ti, isrc=t.metadata.isrc)),
                ]

                for p_name, p_inst, call_fn in active_calls:
                    if p_inst is None:
                        provider_audit[p_name] = {"status": "not_configured", "matches": 0, "request_made": False}
                        continue

                    configured_providers += 1
                    cands, out = call_fn(p_inst, artist, title)
                    # If filename was ambiguous ('Title - Artist') and primary orientation found 0 matches, try swapped!
                    if (
                        not cands
                        and alt_artist
                        and alt_title
                        and out.status in (ProviderStatus.OK, ProviderStatus.EMPTY)
                        and p_inst.can_request()
                    ):
                        alt_cands, alt_out = call_fn(p_inst, alt_artist, alt_title)
                        if alt_cands:
                            cands = alt_cands
                            out = alt_out

                    _record_issue(out)
                    provider_audit[p_name] = {
                        "status": out.status.value,
                        "http": out.http_status,
                        "matches": len(cands),
                        "from_cache": out.from_cache,
                        "request_made": out.request_made,
                    }
                    if out.status == ProviderStatus.BUDGET_EXHAUSTED and not out.request_made and not out.from_cache:
                        budget_exhausted_without_call += 1
                    elif out.status in (ProviderStatus.OK, ProviderStatus.EMPTY):
                        successful_or_empty_responses += 1
                        track_candidates.extend(cands)
                    elif out.status in _TRANSIENT_FAILURE_STATUSES:
                        transient_failures += 1
                        max_retry_wait = max(max_retry_wait, out.retry_after_seconds or 120.0)

                # If every configured provider exhausted its run budget before making any request for this track,
                # stop cleanly without penalizing unqueried tracks or incrementing research_attempts!
                if configured_providers > 0 and budget_exhausted_without_call == configured_providers:
                    summary.paused_provider_budget = True
                    break

                # Verify each candidate recording identity + mix/version BEFORE ISRC/duration
                verified_candidates: List[Candidate] = []
                for c in track_candidates:
                    c.verification = verify_recording_candidate(
                        query_artist=artist,
                        query_title=title,
                        identity_confidence=t.metadata.identity_confidence or "verified_embedded",
                        local_isrc=t.metadata.isrc,
                        local_duration=t.metadata.duration,
                        candidate_title=c.title,
                        candidate_artists=c.artists,
                        candidate_isrc=c.isrc,
                        candidate_duration=c.duration,
                        alt_artist=alt_artist,
                        alt_title=alt_title,
                    )
                    c.content_version = ver
                    if c.verification != "Identity mismatch":
                        verified_candidates.append(c)

                track_candidates = verified_candidates

                # Wikidata inferred fallback ONLY when at least one catalogue provider responded cleanly
                # (never mask an all-provider transient outage with a Wikidata hint, which would wipe prior candidates!)
                all_catalogue_failed_transiently = (transient_failures > 0 and successful_or_empty_responses == 0)
                has_good_genre = any(
                    c.verification in ("ISRC supported", "Duration supported") and c.genre
                    for c in track_candidates
                )
                if (
                    enable_wikidata_fallback
                    and wiki is not None
                    and not has_good_genre
                    and not all_catalogue_failed_transiently
                ):
                    res_info = wiki.research_track(artist, title)
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
                        "status": "ok" if w_added else "empty",
                        "matches": w_added,
                        "evidence_type": "inferred",
                    }

            if configured_providers > 0 and budget_exhausted_without_call == configured_providers:
                summary.paused_provider_budget = True
                break

            summary.attempted += 1

            # Decide per-track research checkpoint status honestly
            has_recording_verified = any(
                c.verification in ("ISRC supported", "Duration supported") for c in track_candidates
            )

            if not track_candidates and transient_failures > 0 and successful_or_empty_responses == 0:
                # All attempted providers failed transiently -> preserve old valid candidates & schedule backoff retry!
                backoff_s = int(max(60.0, min(3600.0, max_retry_wait or (120.0 * (2 ** t.research_attempts)))))
                db.update_research_result(
                    file_id=t.file_id,
                    status="retryable_failure",
                    version=ver,
                    candidates=[],
                    provider_calls=provider_audit,
                    error_msg="Transient provider outage/rate-limit; previous evidence preserved",
                    retry_delay_seconds=backoff_s,
                    preserve_existing_candidates_on_failure=True,
                )
                summary.failed_retryable += 1
                track_outcome_label = "retryable_failure"
            elif has_recording_verified:
                db.update_research_result(
                    file_id=t.file_id,
                    status="success",
                    version=ver,
                    candidates=track_candidates,
                    provider_calls=provider_audit,
                )
                summary.matched_verified += 1
                track_outcome_label = "success"
            elif track_candidates:
                db.update_research_result(
                    file_id=t.file_id,
                    status="partial",
                    version=ver,
                    candidates=track_candidates,
                    provider_calls=provider_audit,
                )
                summary.matched_review_only += 1
                track_outcome_label = "partial"
            elif successful_or_empty_responses > 0:
                db.update_research_result(
                    file_id=t.file_id,
                    status="no_match",
                    version=ver,
                    candidates=[],
                    provider_calls=provider_audit,
                )
                summary.no_match += 1
                track_outcome_label = "no_match"
            else:
                db.update_research_result(
                    file_id=t.file_id,
                    status="retryable_failure",
                    version=ver,
                    candidates=[],
                    provider_calls=provider_audit,
                    error_msg="No configured provider could complete lookup",
                    retry_delay_seconds=300,
                    preserve_existing_candidates_on_failure=True,
                )
                summary.failed_retryable += 1
                track_outcome_label = "retryable_failure"

            if progress_callback:
                progress_callback(idx, len(queue), t, track_outcome_label, len(track_candidates))

    except KeyboardInterrupt:
        summary.interrupted = True

    for p_label, p_obj in [("SonoVault", sono), ("AudioMeta", am), ("Discogs", dc), ("MusicBrainz", mb), ("Wikidata", wiki)]:
        if p_obj is not None and hasattr(p_obj, "stats"):
            summary.provider_telemetry[p_label] = dict(p_obj.stats)

    return summary
