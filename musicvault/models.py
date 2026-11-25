"""
Data models for Music Vault Manager.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from enum import Enum


class VerificationStatus(str, Enum):
    ISRC_SUPPORTED = "ISRC supported"
    DURATION_SUPPORTED = "Duration supported"
    ISRC_CONFLICT = "ISRC conflict"
    DURATION_CONFLICT = "Duration conflict"
    TEXT_ONLY = "Text only"
    MISSING = "missing"


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
    """Audio metadata extracted locally or from inventory."""
    version: str = ""
    artist: str = ""
    title: str = ""
    duration: float = 0.0
    isrc: str = ""
    embedded_bpm: str = ""
    embedded_key: str = ""
    embedded_genre: str = ""
    identity_origin: str = "embedded"  # 'embedded' or 'filename fallback'


@dataclass
class Track:
    """A track representation in Google Drive and local cache."""
    file_id: str
    name: str
    size: int = 0
    md5: str = ""
    modified_time: str = ""
    parent_id: str = ""
    metadata: TrackMetadata = field(default_factory=TrackMetadata)


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
