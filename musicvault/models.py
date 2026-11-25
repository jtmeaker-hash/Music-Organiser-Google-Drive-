"""
Data models for Music Vault Manager.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any


class VerificationStatus(str, Enum):
    ISRC_SUPPORTED = "ISRC supported"
    DURATION_SUPPORTED = "Duration supported"
    ISRC_CONFLICT = "ISRC conflict"
    DURATION_CONFLICT = "Duration conflict"
    VERSION_CONFLICT = "Version conflict"
    IDENTITY_MISMATCH = "Identity mismatch"
    TITLE_ONLY_REVIEW = "Title-only review"
    INFERRED_REVIEW = "Review only (inferred)"
    TEXT_ONLY = "Text only"
    MISSING = "missing"


class HydrationStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    PARTIAL = "partial"
    UNRESOLVED = "unresolved"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_UNSUPPORTED = "permanent_unsupported"


class ResearchStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    PARTIAL = "partial"
    NO_MATCH = "no_match"
    UNRESOLVED = "unresolved"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_UNSUPPORTED = "permanent_unsupported"
    DEFERRED_MISSING_IDENTITY = "deferred_missing_identity"
    DEFERRED_TITLE_ONLY = "deferred_title_only"


class IdentityConfidence(str, Enum):
    VERIFIED_EMBEDDED = "verified_embedded"
    SHEET_EXACT_ID = "sheet_exact_id"
    FILENAME_PARSED = "filename_parsed"
    FILENAME_AMBIGUOUS = "filename_ambiguous"
    TITLE_ONLY = "title_only"
    UNRESOLVED = "unresolved"


class DecisionState(str, Enum):
    HIGH_LOCKED = "HIGH_LOCKED"
    USER_LOCKED = "USER_LOCKED"
    MEDIUM_PROVISIONAL = "MEDIUM_PROVISIONAL"
    EXISTING_UNVERIFIED = "EXISTING_UNVERIFIED"
    REVIEW = "REVIEW"
    TERMS_HOLD = "TERMS_HOLD"
    LOCKED = "LOCKED"


class ConfidenceLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class RoutingConfidence(str, Enum):
    VERIFIED_NESTED = "VERIFIED_NESTED"
    VERIFIED_BROAD = "VERIFIED_BROAD"
    PROVISIONAL_REVIEW = "PROVISIONAL_REVIEW"
    HOLD = "HOLD"


@dataclass
class TrackMetadata:
    """Audio metadata extracted from tags, exact-ID sheet index, or filename hints."""
    version: str = ""
    artist: str = ""
    title: str = ""
    duration: float = 0.0
    isrc: str = ""
    embedded_bpm: str = ""
    embedded_key: str = ""
    embedded_genre: str = ""
    identity_origin: str = "embedded"  # 'embedded', 'sheet_exact_id', 'filename_hint', 'filename fallback', 'unresolved'
    raw_artist: str = ""
    raw_title: str = ""
    raw_bpm: str = ""
    raw_key: str = ""
    raw_genre: str = ""
    raw_isrc: str = ""
    identity_confidence: str = ""
    sheet_artist: str = ""
    sheet_title: str = ""
    filename_artist_hint: str = ""
    filename_title_hint: str = ""
    filename_remixer_hint: str = ""
    audio_format: str = ""


@dataclass
class Track:
    """A track representation in Google Drive and local SQLite store."""
    file_id: str
    name: str
    size: int = 0
    md5: str = ""
    modified_time: str = ""
    parent_id: str = ""
    metadata: TrackMetadata = field(default_factory=TrackMetadata)
    mime_type: str = ""
    record_origin: str = "unknown"
    is_live_source: bool = False
    quarantined: bool = False
    quarantine_reason: str = ""
    configured_source_id: str = ""
    observed_parent_id: str = ""
    scan_generation: int = 0
    content_stale: bool = False
    hydration_status: str = "pending"
    hydration_version: str = ""
    hydration_method: str = ""
    hydration_error: str = ""
    hydration_bytes: int = 0
    hydration_attempts: int = 0
    last_hydrated_utc: str = ""
    next_hydrate_retry_utc: str = ""
    research_status: str = "pending"
    research_version: str = ""
    research_attempts: int = 0
    research_last_utc: str = ""
    research_next_retry_utc: str = ""
    research_error: str = ""
    provider_calls_json: str = "{}"


@dataclass
class Candidate:
    """Metadata candidate retrieved from an online provider."""
    provider: str
    title: str
    artists: List[str] = field(default_factory=list)
    duration: Optional[float] = None
    isrc: Optional[str] = None
    bpm: Optional[float] = None
    key: Optional[str] = None
    genre: List[str] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)
    url: Optional[str] = None
    verification: str = "Text only"
    content_version: str = ""
    is_stale: bool = False
    match_confidence: str = ""
    source_family: str = ""


@dataclass
class FieldDecisionResult:
    """Decision for an individual metadata field."""
    field: str
    level: ConfidenceLevel
    reason: str
    value: str = ""


@dataclass
class MovePlanItem:
    """Planned move action for a track."""
    row_num: Optional[int]
    file_id: str
    track_name: str
    destination: str
    destination_segments: List[str]
    reason: str
    routing_confidence: RoutingConfidence
    eligible: bool
    move_authorized: bool
    provisional: bool = False
    executable: bool = False
    existing_genre: str = ""
    proposed_genre: str = ""
    evidence_summary: str = ""
    folder_action: str = ""
