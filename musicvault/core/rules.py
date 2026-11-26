"""
Field-level decision engine, lock integrity checks, and spreadsheet row planning.
Implements conservative preservation, independent multi-provider agreement rules,
fail-closed malformed lock handling, and review stars.
"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from ..models import Candidate, TrackMetadata
from ..providers.base import canonical_provider_family
from .genre import GENERIC_TAGS, SUBGENRE_PARENT, canonical_genre, classify_track_genres
from .matcher import is_valid_bpm, is_valid_camelot, norm, normalize_key_to_camelot

FIELDS = ["bpm", "key", "genre", "subgenre"]
COL = {"bpm": 4, "key": 5, "genre": 2, "subgenre": 3}
STATE_COL = {"bpm": 18, "key": 19, "genre": 20, "subgenre": 21}

EXTRA_HEADERS = [
    "Review Star", "Review Reasons", "BPM State", "Key State",
    "Genre State", "Subgenre State", "Metadata Lock JSON",
    "Metadata Evidence Summary", "Last Review UTC", "Review Status", "Secondary Influence",
]

_IMMUTABLE_STATES = ("HIGH_LOCKED", "USER_LOCKED")
_CONFLICT_STATES = ("CONFLICT", "LOW_CONFLICT", "REJECTED")


def candidate_field_values(candidates: List[Candidate], field: str) -> List[Tuple[Any, str, str]]:
    """
    Extract validated candidate values supported by ISRC or duration recording matching.
    Excludes inferred knowledge-graph hints and unverified text-only hits.
    Returns: list of (normalized_value, independent_source_family, verification_status)
    """
    out: List[Tuple[Any, str, str]] = []
    for c in candidates:
        v_status = c.verification or ""
        if v_status not in ("ISRC supported", "Duration supported"):
            continue
        family = c.source_family or canonical_provider_family(c.provider)
        if family == "wikidata":
            continue

        if field == "bpm":
            val = getattr(c, "bpm", None)
            try:
                v_num = round(float(val), 1)
            except (ValueError, TypeError):
                continue
            if not is_valid_bpm(v_num):
                continue
            out.append((v_num, family, v_status))
        elif field == "key":
            raw_k = getattr(c, "key", None)
            norm_k = normalize_key_to_camelot(raw_k)
            if not is_valid_camelot(norm_k):
                continue
            out.append((norm_k, family, v_status))
        elif field == "genre":
            cand_genres = {
                canonical_genre(g)
                for g in (c.genre or [])
                if isinstance(g, str) and norm(g) not in GENERIC_TAGS
            } - {""}
            # Only emit when a single unambiguous parent genre is reported by this recording candidate
            if len(cand_genres) == 1:
                out.append((next(iter(cand_genres)), family, v_status))
    return out


def build_track_proposal(meta: TrackMetadata, candidates: List[Candidate]) -> Dict[str, Any]:
    """
    Build unified metadata proposal from recording-supported provider candidates and local metadata.
    Fixes v2.0.0 bug where proposals used embedded BPM/key only and ignored provider BPM/key.
    """
    def _propose_numeric_or_key(field_name: str, embedded_fallback: str) -> Tuple[Any, str]:
        matched = candidate_field_values(candidates, field_name)
        if matched:
            vals = {x[0] for x in matched}
            families = {x[1] for x in matched}
            if len(vals) > 1:
                return "CONFLICT", "contradictory recording-supported candidates"
            chosen = next(iter(vals))
            conf = (
                "independent recording-supported agreement"
                if len(families) >= 2
                else "single recording-supported candidate"
            )
            return chosen, conf

        # Embedded tag fallback stays unverified (field_decision will keep it LOW)
        if embedded_fallback:
            if field_name == "bpm":
                try:
                    b_val = round(float(embedded_fallback), 1)
                    if is_valid_bpm(b_val):
                        return b_val, "embedded tag only (unverified)"
                except ValueError:
                    pass
            elif field_name == "key":
                k_val = normalize_key_to_camelot(embedded_fallback)
                if is_valid_camelot(k_val):
                    return k_val, "embedded tag only (unverified)"
        return "", "missing"

    prop_bpm, bpm_conf = _propose_numeric_or_key("bpm", meta.embedded_bpm)
    prop_key, key_conf = _propose_numeric_or_key("key", meta.embedded_key)
    g_info = classify_track_genres(meta, candidates, enable_subgenres=False)
    prop_genre = g_info["genre"]
    prop_sub = g_info["subgenre"]

    if prop_sub and SUBGENRE_PARENT.get(prop_sub) and SUBGENRE_PARENT.get(prop_sub) != prop_genre:
        prop_sub = ""

    tempo_hint = ""
    if isinstance(prop_bpm, (int, float)) and prop_bpm <= 125 and prop_genre in ("Hardstyle", "Hardcore"):
        tempo_hint = f"Check half/double-time ({prop_bpm * 2:g} BPM)"

    return {
        "bpm": prop_bpm,
        "key": prop_key,
        "genre": prop_genre,
        "subgenre": prop_sub,
        "bpm_confidence": bpm_conf,
        "key_confidence": key_conf,
        "genre_confidence": g_info["genre_confidence"],
        "subgenre_confidence": g_info["subgenre_confidence"],
        "genre_scores": g_info["genre_scores"],
        "unmapped_tags": g_info["unmapped_tags"],
        "tempo_hint": tempo_hint,
    }


def field_decision(candidates: List[Candidate], field: str, proposed_value: Any) -> Tuple[str, str]:
    """
    Decide confidence level (HIGH / MEDIUM / LOW) and reason for a specific field.
    Enforces independent source families and recording-level verification gates.
    """
    val = proposed_value.get(field, "") if isinstance(proposed_value, dict) else proposed_value
    if val in ("", "CONFLICT", None):
        return ("LOW", "missing or conflicting")

    if field in ("bpm", "key"):
        matched = candidate_field_values(candidates, field)
        if not matched:
            return ("LOW", "no recording-supported evidence")
        norm_val = round(float(val), 1) if field == "bpm" else normalize_key_to_camelot(val)
        vals = {x[0] for x in matched}
        if len(vals) != 1 or norm_val not in vals:
            return ("LOW", "contradictory candidate values")
        families = {x[1] for x in matched}
        if len(families) >= 2 and all(x[2] in ("ISRC supported", "Duration supported") for x in matched):
            return ("HIGH", "independent recording-supported agreement: " + ", ".join(sorted(families)))
        return ("MEDIUM", "single matched provider: " + ", ".join(sorted(families)))

    if field == "genre":
        canon_val = canonical_genre(str(val))
        if not canon_val:
            return ("LOW", "unknown or novel unmapped genre; review required")
        matched_genres = candidate_field_values(candidates, "genre")
        if not matched_genres:
            return ("LOW", "genre weighted inference; review required")
        vals = {x[0] for x in matched_genres}
        if len(vals) != 1 or canon_val not in vals:
            return ("LOW", "conflicting catalogue genre evidence; review required")
        # Also block promotion if any non-conflicting catalogue candidate (e.g. Discogs release) disagrees;
        # weak inferred knowledge-graph or title-only hints never downgrade verified catalogue evidence.
        for c in candidates:
            v_low = (c.verification or "").lower()
            fam = c.source_family or canonical_provider_family(c.provider)
            if (
                "conflict" in v_low
                or "mismatch" in v_low
                or "inferred" in v_low
                or "title-only" in v_low
                or fam == "wikidata"
            ):
                continue
            for g in (c.genre or []):
                cg = canonical_genre(g)
                if cg and cg != canon_val:
                    return ("LOW", "cross-source genre disagreement; review required")
        families = {x[1] for x in matched_genres}
        if len(families) >= 2:
            return ("HIGH", "independent recording-supported genre agreement: " + ", ".join(sorted(families)))
        return ("MEDIUM", "single recording-supported catalogue genre: " + ", ".join(sorted(families)))

    return ("LOW", "subgenre artist/label inference; review required")


def parse_lock_states(row: List[Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    """
    Parse metadata lock JSON from column W (index 22) and cross-check state columns S:V (18:21).
    Fails closed ('MALFORMED_LOCK_JSON') if column W is non-empty and invalid/corrupt.
    """
    states: Dict[str, Any] = {}
    if len(row) > 22 and str(row[22] or "").strip():
        raw_lock = str(row[22]).strip()
        try:
            parsed = json.loads(raw_lock)
        except (ValueError, TypeError):
            return {}, "MALFORMED_LOCK_JSON"
        if not isinstance(parsed, dict):
            return {}, "MALFORMED_LOCK_JSON"
        for k, v in parsed.items():
            if not isinstance(v, dict):
                return {}, "MALFORMED_LOCK_JSON"
            states[str(k)] = dict(v)

    # Also honor explicit state columns S:V (18..21) if present even when lock JSON omitted them
    for f_name, col_idx in STATE_COL.items():
        if len(row) > col_idx and str(row[col_idx] or "").strip():
            col_state = str(row[col_idx]).strip()
            existing_entry = states.get(f_name)
            if not existing_entry:
                states[f_name] = {"state": col_state}
            elif col_state in _IMMUTABLE_STATES and existing_entry.get("state") not in _IMMUTABLE_STATES:
                states[f_name]["state"] = col_state

    return states, None


def load_existing_states(row: List[Any]) -> Dict[str, Any]:
    """Load metadata lock JSON from column W (backward-compatible helper)."""
    states, err = parse_lock_states(row)
    if err is not None:
        # Fail closed: mark all managed fields as locked/conflict so callers never overwrite
        return {f: {"state": "CONFLICT", "reason": err} for f in FIELDS}
    return states


def planned_row(
    candidates: List[Candidate],
    proposal_row: Dict[str, Any],
    old_row: List[Any],
    save_medium: bool = True,
    provider_terms_cleared: bool = False,
    overrides: Optional[Dict[str, str]] = None,
) -> Tuple[List[str], Dict[str, str], List[str]]:
    """
    Compute proposed row updates while strictly preserving existing metadata, user locks,
    and existing review-column content. Fails closed if lock JSON cannot be trusted.
    """
    out = list(old_row) + [""] * max(0, 27 - len(old_row))
    out = [str(x) if x is not None else "" for x in out[:27]]

    states, lock_err = parse_lock_states(old_row)
    if lock_err is not None:
        # Fail closed: preserve entire existing row (including Q:AA) without clearing or mutating!
        decisions = {f: "LOCK_ERROR_HOLD" for f in FIELDS}
        out[16] = "★"
        return out, decisions, []

    reasons: List[str] = []
    decisions: Dict[str, str] = {}
    changes: List[str] = []

    effective_proposal = dict(proposal_row)
    if overrides:
        for field, value in overrides.items():
            if field in FIELDS:
                effective_proposal[field] = value

    now_iso = datetime.now(timezone.utc).isoformat()

    for field in FIELDS:
        val = effective_proposal.get(field, "")
        level, why = field_decision(candidates, field, effective_proposal)

        if overrides and field in overrides:
            level = "HIGH"
            why = "manual override"

        existing = out[COL[field]]
        prior = states.get(field, {}) if isinstance(states.get(field, {}), dict) else {}
        prior_status = prior.get("state", "")

        if prior_status in _IMMUTABLE_STATES:
            decisions[field] = "LOCKED"
            continue

        if existing not in ("", None):
            decisions[field] = (
                "EXISTING_UNVERIFIED"
                if prior_status not in ("MEDIUM_PROVISIONAL", "HIGH_LOCKED", "USER_LOCKED")
                else prior_status
            )
            if decisions[field] == "EXISTING_UNVERIFIED":
                reasons.append(f"{field}: existing value preserved; verification unknown")
            continue

        if level == "HIGH" or (level == "MEDIUM" and save_medium):
            if not provider_terms_cleared:
                decisions[field] = "TERMS_HOLD"
                reasons.append(f"{field}: provider retention not cleared")
                continue

            out[COL[field]] = str(val)
            state = "HIGH_LOCKED" if level == "HIGH" else "MEDIUM_PROVISIONAL"
            states[field] = {
                "state": state,
                "reason": why,
                "source": "catalogue",
                "reviewed_utc": now_iso,
            }
            decisions[field] = state
            changes.append(field)
            if level == "MEDIUM":
                reasons.append(f"{field}: provisional")
        else:
            decisions[field] = "REVIEW"
            reasons.append(f"{field}: {why}")

    # Preserve existing state column values when states dict had no update for that field
    for field in FIELDS:
        s_val = states.get(field, {}).get("state", "")
        if s_val:
            out[STATE_COL[field]] = s_val

    if states:
        out[22] = json.dumps(states, sort_keys=True, separators=(",", ":"))
    out[23] = "; ".join(f"{k}={v}" for k, v in decisions.items())
    out[24] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    star = any(
        decisions.get(f) in ("REVIEW", "TERMS_HOLD", "MEDIUM_PROVISIONAL", "EXISTING")
        and (not out[COL[f]] or decisions.get(f) != "EXISTING")
        for f in FIELDS
    )
    star = star or any(states.get(f, {}).get("state") == "MEDIUM_PROVISIONAL" for f in FIELDS)
    star = star or any(decisions.get(f) == "EXISTING_UNVERIFIED" for f in FIELDS)

    # Preserve existing review star (Q) and Secondary Influence (AA); never clear manual review notes if reasons empty
    out[16] = "★" if star or (len(old_row) > 16 and old_row[16] == "★") else ""
    out[17] = "; ".join(reasons) if reasons else out[17]
    out[25] = "STARRED" if out[16] else "COMPLETE"
    if len(old_row) > 26 and old_row[26] and not out[26]:
        out[26] = str(old_row[26])

    return out, decisions, changes
