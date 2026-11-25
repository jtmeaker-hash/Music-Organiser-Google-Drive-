"""
Drive routing, folder hierarchy organization, move planning, and rollback manifests.
"""
import re
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timezone

from ..models import MovePlanItem, RoutingConfidence
from .rules import load_existing_states
from .genre import ALIASES_PARENT, SUBGENRE_PARENT, canonical_genre


def safe_segment(s: Any) -> Optional[str]:
    """Validate that folder path segment is safe for Drive and Android."""
    seg = str(s or '').strip()
    if not seg or seg in ('.', '..') or '/' in seg or '\\' in seg or len(seg) > 80:
        return None
    return seg


def routing_choice(old_row: List[Any]) -> Tuple[Optional[List[str]], str, str]:
    """
    Determine destination folder path, reason, and routing confidence.
    Never alters metadata locks.
    """
    row = list(old_row) + [''] * max(0, 27 - len(old_row))
    locks = load_existing_states(row)
    raw_genre = str(row[2]).strip()
    genre = canonical_genre(raw_genre)
    sub = str(row[3]).strip()

    if not genre:
        return None, 'UNKNOWN_OR_UNSUPPORTED_GENRE', 'HOLD'

    gs = locks.get('genre', {}).get('state', '')
    ss = locks.get('subgenre', {}).get('state', '')

    if gs in ('CONFLICT', 'LOW_CONFLICT', 'REJECTED') or ss in ('CONFLICT', 'LOW_CONFLICT', 'REJECTED'):
        return None, 'METADATA_CONFLICT', 'HOLD'

    if gs in ('HIGH_LOCKED', 'USER_LOCKED'):
        if sub and ss in ('HIGH_LOCKED', 'USER_LOCKED') and SUBGENRE_PARENT.get(sub) == genre:
            return [genre, sub], 'GENRE_AND_SUBGENRE_VERIFIED', 'VERIFIED_NESTED'
        return [genre], 'BROAD_GENRE_ONLY', 'VERIFIED_BROAD'

    if gs == 'MEDIUM_PROVISIONAL':
        return [genre], 'PROVISIONAL_MEDIUM_GENRE', 'PROVISIONAL_REVIEW'

    return [genre], 'PROVISIONAL_EXISTING_GENRE', 'PROVISIONAL_REVIEW'


def build_move_plan(file_id: str, track_name: str, row_num: Optional[int],
                    sheet_row: List[Any], allow_provisional: bool = False) -> MovePlanItem:
    """Construct move plan item for a single track."""
    parts, reason, confidence = routing_choice(sheet_row)
    dest_str = '/'.join(parts) if parts else ''
    eligible = bool(parts)
    authorized = eligible and (confidence != 'PROVISIONAL_REVIEW' or allow_provisional)

    return MovePlanItem(
        row_num=row_num,
        file_id=file_id,
        track_name=track_name,
        destination=dest_str,
        destination_segments=parts or [],
        reason=reason,
        routing_confidence=RoutingConfidence(confidence),
        eligible=eligible,
        move_authorized=authorized
    )


def create_rollback_manifest(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Generate a rollback manifest before executing Drive moves."""
    return {
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'status': 'PRE_MOVE',
        'entries': entries
    }
