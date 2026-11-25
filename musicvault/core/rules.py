"""
Field-level decision engine and spreadsheet row planning.
Implements conservative preservation, multi-provider agreement rules, and review stars.
"""
import json
import re
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Any, Optional

from ..models import Candidate, ConfidenceLevel, DecisionState
from .matcher import is_valid_bpm, is_valid_camelot

FIELDS = ['bpm', 'key', 'genre', 'subgenre']
COL = {'bpm': 4, 'key': 5, 'genre': 2, 'subgenre': 3}
STATE_COL = {'bpm': 18, 'key': 19, 'genre': 20, 'subgenre': 21}

EXTRA_HEADERS = [
    'Review Star', 'Review Reasons', 'BPM State', 'Key State',
    'Genre State', 'Subgenre State', 'Metadata Lock JSON',
    'Metadata Evidence Summary', 'Last Review UTC', 'Review Status', 'Secondary Influence'
]


def candidate_field_values(candidates: List[Candidate], field: str) -> List[Tuple[Any, str, str]]:
    """
    Extract validated candidate values supported by ISRC or duration matching.
    Returns: list of (value, provider, verification_status)
    """
    out = []
    for c in candidates:
        v_status = c.verification or ""
        if v_status not in ('ISRC supported', 'Duration supported'):
            continue
        val = getattr(c, field, None)
        if field == 'bpm':
            try:
                v_num = round(float(val), 1)
            except (ValueError, TypeError):
                continue
            if not is_valid_bpm(v_num):
                continue
            out.append((v_num, c.provider, v_status))
        elif field == 'key':
            if not is_valid_camelot(val):
                continue
            out.append((str(val).strip(), c.provider, v_status))
    return out


def field_decision(candidates: List[Candidate], field: str, proposed_value: Any) -> Tuple[str, str]:
    """
    Decide confidence level (HIGH / MEDIUM / LOW) and reason for a specific field.
    """
    val = proposed_value.get(field, '') if isinstance(proposed_value, dict) else proposed_value
    if val in ('', 'CONFLICT', None):
        return ('LOW', 'missing or conflicting')

    if field in ('bpm', 'key'):
        matched = candidate_field_values(candidates, field)
        if not matched:
            return ('LOW', 'no recording-supported evidence')
        vals = {x[0] for x in matched}
        if len(vals) != 1 or val not in vals:
            return ('LOW', 'contradictory candidate values')
        providers = {x[1] for x in matched}
        # Multi-provider independent recording-supported agreement
        if len(providers) >= 2 and all(x[2] in ('ISRC supported', 'Duration supported') for x in matched):
            return ('HIGH', 'independent recording-supported agreement: ' + ', '.join(sorted(providers)))
        return ('MEDIUM', 'single matched provider: ' + ', '.join(sorted(providers)))

    if field == 'genre':
        return ('LOW', 'genre weighted inference; review required')

    return ('LOW', 'subgenre artist/label inference; review required')


def load_existing_states(row: List[Any]) -> Dict[str, Any]:
    """Load metadata lock JSON from column W (index 22)."""
    if len(row) > 22 and row[22]:
        try:
            parsed = json.loads(row[22])
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def planned_row(candidates: List[Candidate], proposal_row: Dict[str, Any], old_row: List[Any],
                save_medium: bool = True, provider_terms_cleared: bool = False,
                overrides: Optional[Dict[str, str]] = None) -> Tuple[List[str], Dict[str, str], List[str]]:
    """
    Compute proposed row updates while strictly preserving existing metadata and user locks.
    Returns: (new_row, decisions_dict, changed_fields)
    """
    out = list(old_row) + [''] * max(0, 27 - len(old_row))
    out = [str(x) if x is not None else '' for x in out[:27]]

    states = load_existing_states(old_row)
    reasons: List[str] = []
    decisions: Dict[str, str] = {}
    changes: List[str] = []

    # Apply any manual overrides
    effective_proposal = dict(proposal_row)
    if overrides:
        for field, value in overrides.items():
            if field in FIELDS:
                effective_proposal[field] = value

    now_iso = datetime.now(timezone.utc).isoformat()

    for field in FIELDS:
        val = effective_proposal.get(field, '')
        level, why = field_decision(candidates, field, effective_proposal)

        # Check overrides
        if overrides and field in overrides:
            level = 'HIGH'
            why = 'manual override'

        existing = out[COL[field]]
        prior = states.get(field, {}) if isinstance(states.get(field, {}), dict) else {}
        prior_status = prior.get('state', '')

        # Never overwrite high locked or user locked fields
        if prior_status in ('HIGH_LOCKED', 'USER_LOCKED'):
            decisions[field] = 'LOCKED'
            continue

        # Treat existing non-empty cells as unmanaged/preserved
        if existing not in ('', None):
            decisions[field] = (
                'EXISTING_UNVERIFIED'
                if prior_status not in ('MEDIUM_PROVISIONAL', 'HIGH_LOCKED', 'USER_LOCKED')
                else prior_status
            )
            if decisions[field] == 'EXISTING_UNVERIFIED':
                reasons.append(f"{field}: existing value preserved; verification unknown")
            continue

        # If eligible to write
        if level == 'HIGH' or (level == 'MEDIUM' and save_medium):
            if not provider_terms_cleared:
                decisions[field] = 'TERMS_HOLD'
                reasons.append(f"{field}: provider retention not cleared")
                continue

            out[COL[field]] = str(val)
            state = 'HIGH_LOCKED' if level == 'HIGH' else 'MEDIUM_PROVISIONAL'
            states[field] = {
                'state': state,
                'reason': why,
                'source': 'catalogue',
                'reviewed_utc': now_iso
            }
            decisions[field] = state
            changes.append(field)
            if level == 'MEDIUM':
                reasons.append(f"{field}: provisional")
        else:
            decisions[field] = 'REVIEW'
            reasons.append(f"{field}: {why}")

    # Update state columns S:V (18:21)
    for field in FIELDS:
        out[STATE_COL[field]] = states.get(field, {}).get('state', '')

    out[22] = json.dumps(states, sort_keys=True, separators=(',', ':')) if states else out[22]
    out[23] = '; '.join(f"{k}={v}" for k, v in decisions.items())
    out[24] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    # Star if any field is missing, provisional, unverified, or in review
    star = any(
        decisions.get(f) in ('REVIEW', 'TERMS_HOLD', 'MEDIUM_PROVISIONAL', 'EXISTING')
        and (not out[COL[f]] or decisions.get(f) != 'EXISTING')
        for f in FIELDS
    )
    star = star or any(states.get(f, {}).get('state') == 'MEDIUM_PROVISIONAL' for f in FIELDS)
    star = star or any(decisions.get(f) == 'EXISTING_UNVERIFIED' for f in FIELDS)

    out[16] = '★' if star or (len(old_row) > 16 and old_row[16] == '★') else ''
    out[17] = '; '.join(reasons)
    out[25] = 'STARRED' if out[16] else 'COMPLETE'

    return out, decisions, changes
