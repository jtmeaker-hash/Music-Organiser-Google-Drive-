"""
Configurable Artist Classification Rules Engine, Google Sheets `Artist_Rules` Tab Schema,
Audit History, and Human-Correction Rule Learning (Suggested Rules Pending User Approval).

Core Governance Guarantees:
1. Artist preferences (`preferred`) guide investigation and review cues; they are NEVER treated
   as verified track-level evidence and NEVER force every track by an artist into one subgenre.
2. `discouraged` and `blocked` classifications detect conflicts against artist-derived sources
   (such as HDRR) and uncorroborated single-source tags, preventing unsupported automatic
   classifications while allowing track-specific exceptions (`file_id`, `isrc`, or `title`).
3. Supports release-year ranges (for artists whose style evolved), collaboration policies,
   remix-artist priority (`remixer_first` / `equal` / `original_artist_first`), configurable
   confidence adjustments, `enabled`/`disabled` toggles, `approval_status`, `verification_status`,
   and immutable audit history (`artist_rules_audit`).
4. Manual track overrides (`USER_LOCKED`, `HIGH_LOCKED`, `MOVED`/`ANALYSED`, verified `M`/`P` notes)
   are strictly protected against automated changes.
5. Inferred rules learned from recurring manual corrections are ALWAYS created with
   `status='pending_approval'` (`enabled=False`) and NEVER auto-activated without user approval.
"""
import hashlib
import json
import re
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ..models import Candidate, TrackMetadata
from .genre import (
    SUBGENRE_PARENT,
    canonical_genre,
    canonical_subgenre,
    is_genre_placeholder,
    is_subgenre_placeholder,
)
from .matcher import extract_version_tokens, norm, parse_artists

ARTIST_RULES_TAB_NAME = "Artist_Rules"

ARTIST_RULES_HEADERS: List[str] = [
    "Rule ID",
    "Artist",
    "Enabled",
    "Approval Status",
    "Verification Status",
    "Preferred Classifications",
    "Discouraged Classifications",
    "Blocked Classifications",
    "Explicit Override Genre",
    "Explicit Override Subgenre",
    "Track Exceptions",
    "Release Year Range",
    "Collaboration Policy",
    "Remix Artist Priority",
    "Confidence Boost",
    "Confidence Penalty",
    "Notes / Rationale",
    "Audit History",
]

VALID_APPROVAL_STATUSES = frozenset({"approved", "pending_approval", "rejected", "draft"})
VALID_VERIFICATION_STATUSES = frozenset({"user_verified", "provisional", "inferred_unapproved"})
VALID_COLLAB_POLICIES = frozenset({
    "apply_discouraged_and_blocked",
    "strict_all",
    "primary_artist_only",
    "defer_to_collaborator",
    "hold_on_conflict",
})
VALID_REMIX_PRIORITIES = frozenset({"remixer_first", "equal", "original_artist_first"})


@dataclass
class TrackExceptionSpec:
    """Track-specific exception allowing a specific recording to bypass an artist-level rule."""
    match_type: str  # 'file_id', 'isrc', 'title'
    match_value: str
    allowed_classification: str = ""  # e.g. 'Euphoric' or 'Rawphoric' or '*' (any)
    notes: str = ""


@dataclass
class ArtistRule:
    """Structured artist classification rule synchronized across SQLite, Config, and `Artist_Rules` sheet."""
    rule_id: str
    artist_name: str
    enabled: bool = True
    approval_status: str = "approved"
    verification_status: str = "user_verified"
    preferred: List[str] = field(default_factory=list)
    discouraged: List[str] = field(default_factory=list)
    blocked: List[str] = field(default_factory=list)
    explicit_override_genre: str = ""
    explicit_override_subgenre: str = ""
    track_exceptions: List[Dict[str, str]] = field(default_factory=list)
    year_min: Optional[int] = None
    year_max: Optional[int] = None
    collab_policy: str = "apply_discouraged_and_blocked"
    remix_priority: str = "remixer_first"
    confidence_boost: int = 0
    confidence_penalty: int = 1
    notes: str = ""
    source_origin: str = "config"
    created_utc: str = ""
    updated_utc: str = ""
    audit_summary: str = ""

    @property
    def norm_artist(self) -> str:
        return norm(self.artist_name)

    @property
    def is_active(self) -> bool:
        return bool(self.enabled and self.approval_status == "approved" and self.norm_artist)


# Initial user-defined seed rule required by Section 5: Peygan
DEFAULT_PEYGAN_RULE = ArtistRule(
    rule_id="AR-PEYGAN-001",
    artist_name="Peygan",
    enabled=True,
    approval_status="approved",
    verification_status="user_verified",
    preferred=["Rawstyle", "Xtra Raw", "Experimental Raw"],
    discouraged=["Euphoric"],
    blocked=[],
    explicit_override_genre="",
    explicit_override_subgenre="",
    track_exceptions=[],
    year_min=None,
    year_max=None,
    collab_policy="apply_discouraged_and_blocked",
    remix_priority="remixer_first",
    confidence_boost=0,
    confidence_penalty=1,
    notes=(
        "Strongly discourage Euphoric Hardstyle; prefer investigation into Rawstyle, Xtra Raw "
        "and Experimental Raw. Allow track-specific exceptions; do not force every track into Rawstyle."
    ),
    source_origin="seed_default",
)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_bool(val: Any, default: bool = True) -> bool:
    if isinstance(val, bool):
        return val
    s = str(val or "").strip().lower()
    if not s:
        return default
    if s in ("true", "1", "yes", "y", "on", "enabled", "active"):
        return True
    if s in ("false", "0", "no", "n", "off", "disabled", "inactive"):
        return False
    return default


def _split_list(raw: Any) -> List[str]:
    if isinstance(raw, list):
        items = [str(x).strip() for x in raw if str(x).strip()]
    else:
        s = str(raw or "").strip()
        if not s:
            return []
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
            except ValueError:
                pass
        items = [p.strip() for p in re.split(r"[;,|]", s) if p.strip()]
    return list(dict.fromkeys(items))


def normalize_classification_token(token: str) -> Tuple[str, str]:
    """Normalize a rule classification token (e.g. 'Euphoric Hardstyle', 'Xtra Raw', 'Hardcore') -> `(parent, subgenre)`."""
    raw = (token or "").strip()
    if not raw:
        return "", ""
    sub = canonical_subgenre(raw)
    if sub:
        return SUBGENRE_PARENT.get(sub, ""), sub
    parent = canonical_genre(raw)
    return parent, ""


def _classification_matches_list(genre_val: str, subgenre_val: str, target_list: Iterable[str]) -> List[str]:
    """
    Check whether `(genre_val, subgenre_val)` matches any token in `target_list`.
    - If a rule token specifies a subgenre (e.g. `'Euphoric'` or `'Euphoric Hardstyle'`), it matches
      ONLY when `subgenre_val` canonicalizes to `'Euphoric'` (NOT merely because parent is `'Hardstyle'`).
    - If a rule token specifies only a parent genre (e.g. `'Country'`), it matches when `genre_val`
      canonicalizes to `'Country'`.
    """
    c_g = canonical_genre(genre_val)
    c_s = canonical_subgenre(subgenre_val) or canonical_subgenre(genre_val)
    matched: List[str] = []
    for tok in target_list:
        t_parent, t_sub = normalize_classification_token(tok)
        if t_sub:
            if c_s and c_s == t_sub:
                matched.append(t_sub)
        elif t_parent:
            if c_g and c_g == t_parent:
                matched.append(t_parent)
        elif norm(tok) and norm(tok) in (norm(genre_val), norm(subgenre_val)):
            matched.append(tok.strip())
    return list(dict.fromkeys(matched))


def parse_year_range(raw: Any) -> Tuple[Optional[int], Optional[int]]:
    """Parse `'2022-2026'`, `'<=2019'`, `'>=2023'`, or `'2025'` into `(year_min, year_max)`."""
    s = str(raw or "").strip()
    if not s:
        return None, None
    m_span = re.fullmatch(r"(\d{4})\s*[-–..]+\s*(\d{4})", s)
    if m_span:
        y1, y2 = int(m_span.group(1)), int(m_span.group(2))
        return min(y1, y2), max(y1, y2)
    m_le = re.fullmatch(r"(?:<=?|before\s*)(\d{4})", s, flags=re.I)
    if m_le:
        return None, int(m_le.group(1))
    m_ge = re.fullmatch(r"(?:>=?|from\s*|since\s*)(\d{4})\+?", s, flags=re.I)
    if m_ge:
        return int(m_ge.group(1)), None
    m_eq = re.fullmatch(r"(\d{4})", s)
    if m_eq:
        y = int(m_eq.group(1))
        return y, y
    return None, None


def format_year_range(year_min: Optional[int], year_max: Optional[int]) -> str:
    if year_min is not None and year_max is not None:
        return str(year_min) if year_min == year_max else f"{year_min}-{year_max}"
    if year_min is not None:
        return f">={year_min}"
    if year_max is not None:
        return f"<={year_max}"
    return ""


def parse_track_exceptions(raw: Any) -> List[Dict[str, str]]:
    """
    Parse track exceptions from JSON list or compact sheet string:
    e.g. `'fid:1abc=Euphoric; isrc:BEB682600029=Rawphoric; Famous (Acoustic)=*'`
    """
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, dict) and item.get("match_value"):
                out.append({
                    "match_type": str(item.get("match_type") or "title").strip().lower(),
                    "match_value": str(item["match_value"]).strip(),
                    "allowed": str(item.get("allowed") or item.get("allowed_classification") or "*").strip(),
                })
        return out
    s = str(raw or "").strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            return parse_track_exceptions(json.loads(s))
        except ValueError:
            pass
    specs: List[Dict[str, str]] = []
    for part in [p.strip() for p in s.split(";") if p.strip()]:
        lhs, allowed = part.split("=", 1) if "=" in part else (part, "*")
        lhs = lhs.strip()
        allowed = allowed.strip() or "*"
        if lhs.lower().startswith(("fid:", "file_id:")):
            m_type, m_val = "file_id", lhs.split(":", 1)[1].strip()
        elif lhs.lower().startswith("isrc:"):
            m_type, m_val = "isrc", lhs.split(":", 1)[1].strip()
        else:
            m_type, m_val = "title", lhs
        if m_val:
            specs.append({"match_type": m_type, "match_value": m_val, "allowed": allowed})
    return specs


def format_track_exceptions(specs: List[Dict[str, str]]) -> str:
    parts = []
    for sp in specs or []:
        mt = sp.get("match_type", "title")
        mv = sp.get("match_value", "")
        al = sp.get("allowed", "*") or "*"
        if not mv:
            continue
        prefix = "fid:" if mt == "file_id" else ("isrc:" if mt == "isrc" else "")
        parts.append(f"{prefix}{mv}={al}" if al != "*" else f"{prefix}{mv}")
    return "; ".join(parts)


# --- Sheet Tab Parsing & Formatting ---

_HEADER_ALIASES = {
    "ruleid": "rule_id",
    "id": "rule_id",
    "artist": "artist_name",
    "artistname": "artist_name",
    "enabled": "enabled",
    "active": "enabled",
    "approvalstatus": "approval_status",
    "approval": "approval_status",
    "verificationstatus": "verification_status",
    "verification": "verification_status",
    "preferredclassifications": "preferred",
    "preferred": "preferred",
    "preferredsubgenres": "preferred",
    "discouragedclassifications": "discouraged",
    "discouraged": "discouraged",
    "discouragedsubgenres": "discouraged",
    "blockedclassifications": "blocked",
    "blocked": "blocked",
    "blockedsubgenres": "blocked",
    "explicitoverridegenre": "explicit_override_genre",
    "overridegenre": "explicit_override_genre",
    "explicitoverridesubgenre": "explicit_override_subgenre",
    "overridesubgenre": "explicit_override_subgenre",
    "trackexceptions": "track_exceptions",
    "exceptions": "track_exceptions",
    "releaseyearrange": "year_range",
    "yearrange": "year_range",
    "years": "year_range",
    "collaborationpolicy": "collab_policy",
    "collaborationrule": "collab_policy",
    "remixartistpriority": "remix_priority",
    "remixpriority": "remix_priority",
    "confidenceboost": "confidence_boost",
    "boost": "confidence_boost",
    "confidencepenalty": "confidence_penalty",
    "penalty": "confidence_penalty",
    "notesrationale": "notes",
    "notes": "notes",
    "rationale": "notes",
    "audithistory": "audit_summary",
    "audit": "audit_summary",
}


def parse_artist_rules_sheet(
    header_row: List[Any],
    data_rows: List[List[Any]],
) -> Tuple[List[ArtistRule], List[str]]:
    """
    Validate and parse rows from the Google Sheets `Artist_Rules` tab.
    Returns `(parsed_rules, validation_warnings)` without failing on optional blank cells.
    """
    warnings: List[str] = []
    if not header_row or not any(str(c or "").strip() for c in header_row):
        return [], ["Artist_Rules header row is empty"]

    col_map: Dict[str, int] = {}
    for idx, raw_h in enumerate(header_row):
        key = _HEADER_ALIASES.get(norm(str(raw_h or "")))
        if key and key not in col_map:
            col_map[key] = idx

    if "artist_name" not in col_map:
        return [], ["INCOMPATIBLE_ARTIST_RULES_HEADERS: missing required 'Artist' column"]

    rules_out: List[ArtistRule] = []
    seen_ids: Set[str] = set()

    def _cell(row_vals: List[str], field_key: str) -> str:
        c_idx = col_map.get(field_key)
        if c_idx is None or c_idx >= len(row_vals):
            return ""
        return row_vals[c_idx].strip()

    for offset, raw_r in enumerate(data_rows):
        row_num = offset + 2
        str_r = [str(x) if x is not None else "" for x in raw_r]
        if not any(x.strip() for x in str_r):
            continue
        artist = _cell(str_r, "artist_name")
        if not artist:
            warnings.append(f"Row {row_num}: skipped blank Artist cell")
            continue

        r_id = _cell(str_r, "rule_id")
        if not r_id:
            digest = hashlib.sha1(f"{norm(artist)}:{row_num}".encode("utf-8")).hexdigest()[:6].upper()
            r_id = f"AR-{norm(artist)[:8].upper()}-{digest}"
        if r_id in seen_ids:
            warnings.append(f"Row {row_num}: duplicate Rule ID '{r_id}' skipped")
            continue
        seen_ids.add(r_id)

        approval = (_cell(str_r, "approval_status") or "approved").lower()
        if approval not in VALID_APPROVAL_STATUSES:
            warnings.append(f"Row {row_num} ({artist}): unknown Approval Status '{approval}', holding as draft")
            approval = "draft"

        verif = (_cell(str_r, "verification_status") or "user_verified").lower()
        if verif not in VALID_VERIFICATION_STATUSES:
            verif = "provisional"

        collab = (_cell(str_r, "collab_policy") or "apply_discouraged_and_blocked").lower()
        if collab not in VALID_COLLAB_POLICIES:
            collab = "apply_discouraged_and_blocked"

        remix_p = (_cell(str_r, "remix_priority") or "remixer_first").lower()
        if remix_p not in VALID_REMIX_PRIORITIES:
            remix_p = "remixer_first"

        y_min, y_max = parse_year_range(_cell(str_r, "year_range"))
        try:
            boost = max(0, min(1, int(_cell(str_r, "confidence_boost") or "0")))
        except ValueError:
            boost = 0
        try:
            penalty = max(0, min(2, int(_cell(str_r, "confidence_penalty") or "1")))
        except ValueError:
            penalty = 1

        rules_out.append(
            ArtistRule(
                rule_id=r_id,
                artist_name=artist,
                enabled=_parse_bool(_cell(str_r, "enabled"), default=True),
                approval_status=approval,
                verification_status=verif,
                preferred=_split_list(_cell(str_r, "preferred")),
                discouraged=_split_list(_cell(str_r, "discouraged")),
                blocked=_split_list(_cell(str_r, "blocked")),
                explicit_override_genre=canonical_genre(_cell(str_r, "explicit_override_genre")),
                explicit_override_subgenre=canonical_subgenre(_cell(str_r, "explicit_override_subgenre")),
                track_exceptions=parse_track_exceptions(_cell(str_r, "track_exceptions")),
                year_min=y_min,
                year_max=y_max,
                collab_policy=collab,
                remix_priority=remix_p,
                confidence_boost=boost,
                confidence_penalty=penalty,
                notes=_cell(str_r, "notes"),
                source_origin="sheet_artist_rules",
                audit_summary=_cell(str_r, "audit_summary"),
            )
        )

    return rules_out, warnings


def artist_rule_to_sheet_row(rule: ArtistRule) -> List[str]:
    """Serialize an `ArtistRule` to the 18-column `Artist_Rules` Google Sheets layout."""
    return [
        rule.rule_id,
        rule.artist_name,
        "TRUE" if rule.enabled else "FALSE",
        rule.approval_status,
        rule.verification_status,
        "; ".join(rule.preferred),
        "; ".join(rule.discouraged),
        "; ".join(rule.blocked),
        rule.explicit_override_genre,
        rule.explicit_override_subgenre,
        format_track_exceptions(rule.track_exceptions),
        format_year_range(rule.year_min, rule.year_max),
        rule.collab_policy,
        rule.remix_priority,
        str(rule.confidence_boost),
        str(rule.confidence_penalty),
        rule.notes,
        rule.audit_summary or f"updated:{rule.updated_utc or _utc_iso()} ({rule.source_origin})",
    ]


# --- SQLite Persistence & Audit Log ---

def ensure_default_artist_rules(db: Any) -> None:
    """Seed `DEFAULT_PEYGAN_RULE` idempotently into SQLite if no rule for Peygan exists yet."""
    if db is None:
        return
    with closing(db._get_connection()) as conn:
        existing = conn.execute(
            "SELECT 1 FROM artist_rules WHERE rule_id = ? OR norm_artist = ? LIMIT 1",
            (DEFAULT_PEYGAN_RULE.rule_id, DEFAULT_PEYGAN_RULE.norm_artist),
        ).fetchone()
    if not existing:
        upsert_artist_rule(db, DEFAULT_PEYGAN_RULE, actor="seed_default", notes="Initial Peygan artist rule")


def upsert_artist_rule(db: Any, rule: ArtistRule, actor: str = "user", notes: str = "") -> ArtistRule:
    """Insert or update an `ArtistRule` in SQLite and append an immutable `artist_rules_audit` entry."""
    now_iso = _utc_iso()
    with closing(db._get_connection()) as conn, conn:
        prev_row = conn.execute("SELECT * FROM artist_rules WHERE rule_id = ?", (rule.rule_id,)).fetchone()
        before_dict = dict(prev_row) if prev_row else {}
        created = str(before_dict.get("created_utc") or rule.created_utc or now_iso)
        rule.created_utc = created
        rule.updated_utc = now_iso
        conn.execute(
            """
            INSERT INTO artist_rules(
                rule_id, artist_name, norm_artist, enabled, approval_status, verification_status,
                preferred_json, discouraged_json, blocked_json,
                explicit_override_genre, explicit_override_subgenre, track_exceptions_json,
                year_min, year_max, collab_policy, remix_priority,
                confidence_boost, confidence_penalty, notes, source_origin, created_utc, updated_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rule_id) DO UPDATE SET
                artist_name=excluded.artist_name,
                norm_artist=excluded.norm_artist,
                enabled=excluded.enabled,
                approval_status=excluded.approval_status,
                verification_status=excluded.verification_status,
                preferred_json=excluded.preferred_json,
                discouraged_json=excluded.discouraged_json,
                blocked_json=excluded.blocked_json,
                explicit_override_genre=excluded.explicit_override_genre,
                explicit_override_subgenre=excluded.explicit_override_subgenre,
                track_exceptions_json=excluded.track_exceptions_json,
                year_min=excluded.year_min,
                year_max=excluded.year_max,
                collab_policy=excluded.collab_policy,
                remix_priority=excluded.remix_priority,
                confidence_boost=excluded.confidence_boost,
                confidence_penalty=excluded.confidence_penalty,
                notes=excluded.notes,
                source_origin=excluded.source_origin,
                updated_utc=excluded.updated_utc
            """,
            (
                rule.rule_id,
                rule.artist_name,
                rule.norm_artist,
                int(bool(rule.enabled)),
                rule.approval_status,
                rule.verification_status,
                json.dumps(list(rule.preferred)),
                json.dumps(list(rule.discouraged)),
                json.dumps(list(rule.blocked)),
                rule.explicit_override_genre,
                rule.explicit_override_subgenre,
                json.dumps(list(rule.track_exceptions)),
                rule.year_min,
                rule.year_max,
                rule.collab_policy,
                rule.remix_priority,
                int(rule.confidence_boost),
                int(rule.confidence_penalty),
                rule.notes,
                rule.source_origin,
                created,
                now_iso,
            ),
        )
        action = "UPDATE_RULE" if prev_row else "CREATE_RULE"
        after_dict = asdict(rule)
        if before_dict != after_dict:
            conn.execute(
                """
                INSERT INTO artist_rules_audit(rule_id, action, actor, before_json, after_json, notes, created_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule.rule_id,
                    action,
                    actor,
                    json.dumps(before_dict, sort_keys=True),
                    json.dumps(after_dict, sort_keys=True),
                    notes or rule.notes,
                    now_iso,
                ),
            )
    return rule


def load_artist_rules(
    db: Optional[Any] = None,
    config: Optional[Any] = None,
    sheet_rules: Optional[List[ArtistRule]] = None,
    active_only: bool = False,
) -> List[ArtistRule]:
    """
    Load merged artist rules from SQLite, optional `ARTIST_RULES_JSON` config, and optional
    `Artist_Rules` sheet rows (sheet rows take precedence by `rule_id` when present).
    """
    by_id: Dict[str, ArtistRule] = {}

    if db is not None:
        ensure_default_artist_rules(db)
        with closing(db._get_connection()) as conn:
            rows = conn.execute("SELECT * FROM artist_rules ORDER BY rule_id ASC").fetchall()
        for r in rows:
            by_id[r["rule_id"]] = ArtistRule(
                rule_id=r["rule_id"],
                artist_name=r["artist_name"],
                enabled=bool(r["enabled"]),
                approval_status=r["approval_status"],
                verification_status=r["verification_status"],
                preferred=json.loads(r["preferred_json"] or "[]"),
                discouraged=json.loads(r["discouraged_json"] or "[]"),
                blocked=json.loads(r["blocked_json"] or "[]"),
                explicit_override_genre=r["explicit_override_genre"] or "",
                explicit_override_subgenre=r["explicit_override_subgenre"] or "",
                track_exceptions=json.loads(r["track_exceptions_json"] or "[]"),
                year_min=r["year_min"],
                year_max=r["year_max"],
                collab_policy=r["collab_policy"] or "apply_discouraged_and_blocked",
                remix_priority=r["remix_priority"] or "remixer_first",
                confidence_boost=int(r["confidence_boost"] or 0),
                confidence_penalty=int(r["confidence_penalty"] or 1),
                notes=r["notes"] or "",
                source_origin=r["source_origin"] or "sqlite",
                created_utc=r["created_utc"] or "",
                updated_utc=r["updated_utc"] or "",
            )
    else:
        by_id[DEFAULT_PEYGAN_RULE.rule_id] = DEFAULT_PEYGAN_RULE

    if config is not None:
        raw_cfg = (config.get("ARTIST_RULES_JSON", "") or "").strip()
        if raw_cfg:
            try:
                cfg_list = json.loads(raw_cfg)
                if isinstance(cfg_list, list):
                    for item in cfg_list:
                        if isinstance(item, dict) and item.get("artist_name"):
                            rid = str(item.get("rule_id") or f"CFG-{norm(item['artist_name'])[:10].upper()}")
                            by_id[rid] = ArtistRule(
                                rule_id=rid,
                                artist_name=str(item["artist_name"]),
                                enabled=_parse_bool(item.get("enabled", True)),
                                approval_status=str(item.get("approval_status") or "approved"),
                                verification_status=str(item.get("verification_status") or "user_verified"),
                                preferred=_split_list(item.get("preferred", [])),
                                discouraged=_split_list(item.get("discouraged", [])),
                                blocked=_split_list(item.get("blocked", [])),
                                explicit_override_genre=canonical_genre(item.get("explicit_override_genre", "")),
                                explicit_override_subgenre=canonical_subgenre(item.get("explicit_override_subgenre", "")),
                                track_exceptions=parse_track_exceptions(item.get("track_exceptions", [])),
                                year_min=item.get("year_min"),
                                year_max=item.get("year_max"),
                                collab_policy=str(item.get("collab_policy") or "apply_discouraged_and_blocked"),
                                remix_priority=str(item.get("remix_priority") or "remixer_first"),
                                confidence_boost=int(item.get("confidence_boost", 0)),
                                confidence_penalty=int(item.get("confidence_penalty", 1)),
                                notes=str(item.get("notes") or ""),
                                source_origin="env_config",
                            )
            except ValueError:
                pass

    for s_rule in (sheet_rules or []):
        by_id[s_rule.rule_id] = s_rule

    rules = list(by_id.values())
    if active_only:
        return [r for r in rules if r.is_active]
    return rules


# --- Track-Level Rule Evaluation ---

def _infer_track_release_year(meta: TrackMetadata, candidates: List[Candidate]) -> Optional[int]:
    years = [c.release_year for c in candidates if c.release_year is not None and 1985 <= c.release_year <= 2100]
    if years:
        return min(years)
    m = re.search(r"\b(19[9]\d|20[0-3]\d)\b", meta.title or "")
    return int(m.group(1)) if m else None


def _find_matching_exception(
    rule: ArtistRule,
    file_id: str,
    meta: TrackMetadata,
    candidates: List[Candidate],
) -> Optional[Dict[str, str]]:
    if not rule.track_exceptions:
        return None
    isrcs = {norm(meta.isrc)} if meta.isrc else set()
    for c in candidates:
        if c.isrc:
            isrcs.add(norm(c.isrc))
    t_norm = norm(meta.title)
    t_base, _, _ = extract_version_tokens(meta.title or "")
    for sp in rule.track_exceptions:
        mt = (sp.get("match_type") or "title").lower()
        mv = (sp.get("match_value") or "").strip()
        if not mv:
            continue
        if mt == "file_id" and file_id and mv == file_id:
            return sp
        if mt == "isrc" and norm(mv) in isrcs:
            return sp
        if mt == "title" and (norm(mv) == t_norm or (t_base and norm(mv) == t_base and "(" not in mv)):
            return sp
    return None


def evaluate_artist_rules(
    meta: TrackMetadata,
    candidates: List[Candidate],
    rules: Iterable[ArtistRule],
    proposed_genre: str = "",
    proposed_subgenre: str = "",
    file_id: str = "",
) -> Dict[str, Any]:
    """
    Evaluate active approved `ArtistRule`s for a track across primary artists, collaborations,
    remixers, release-year ranges, and track-specific exceptions.

    Checks BOTH:
    1. `proposed_genre` / `proposed_subgenre` (from recording-supported providers), AND
    2. All candidate genres/subgenres (including artist-derived HDRR candidates, e.g. HDRR
       reporting `'Euphoric Hardstyle'` on `'Famous — Peygan & Firelite'`)!
    """
    active_rules = [r for r in rules if r.is_active]
    if not active_rules:
        return {
            "matched_rules": [],
            "applicable_artists": [],
            "is_collaboration": False,
            "is_remix": False,
            "release_year": _infer_track_release_year(meta, candidates),
            "exception_applied": None,
            "discouraged_conflict": False,
            "blocked_conflict": False,
            "genre_discouraged_conflict": False,
            "genre_blocked_conflict": False,
            "subgenre_discouraged_conflict": False,
            "subgenre_blocked_conflict": False,
            "hdrr_rule_conflict": False,
            "conflict_reasons": [],
            "investigation_cues": [],
            "explicit_override_genre": "",
            "explicit_override_subgenre": "",
            "confidence_delta": 0,
        }

    primary_norms = list(dict.fromkeys(parse_artists(meta.artist)))
    for c in candidates:
        if c.verification in ("ISRC supported", "Duration supported") or c.source_family == "hdrr":
            for ca in (c.artists or []):
                for pa in parse_artists(ca):
                    if pa not in primary_norms:
                        primary_norms.append(pa)

    _, ver_tokens, remixer_norm = extract_version_tokens(meta.title or "")
    is_remix = bool(remixer_norm or "remix" in ver_tokens)
    is_collab = len(primary_norms) >= 2
    rel_year = _infer_track_release_year(meta, candidates)

    # Index active rules by normalized artist and filter by release-year range
    applicable: List[Tuple[ArtistRule, str]] = []  # (rule, role: 'remixer' | 'primary' | 'collaborator')
    for idx, p_norm in enumerate(primary_norms):
        for r in active_rules:
            if r.norm_artist != p_norm:
                continue
            if rel_year is not None:
                if r.year_min is not None and rel_year < r.year_min:
                    continue
                if r.year_max is not None and rel_year > r.year_max:
                    continue
            role = "primary" if idx == 0 else "collaborator"
            applicable.append((r, role))

    if is_remix and remixer_norm:
        for r in active_rules:
            if r.norm_artist != remixer_norm:
                continue
            if rel_year is not None:
                if r.year_min is not None and rel_year < r.year_min:
                    continue
                if r.year_max is not None and rel_year > r.year_max:
                    continue
            applicable.append((r, "remixer"))

    # Apply remix_priority filtering: if track is a remix by ANOTHER artist (remixer != rule artist),
    # a primary artist rule with `remix_priority == 'remixer_first'` yields stylistic control to the remixer!
    filtered_applicable: List[Tuple[ArtistRule, str]] = []
    for r, role in applicable:
        if is_remix and remixer_norm and role != "remixer" and r.norm_artist != remixer_norm:
            if r.remix_priority == "remixer_first":
                continue
        if is_collab and role == "collaborator" and r.collab_policy == "primary_artist_only":
            continue
        filtered_applicable.append((r, role))

    if is_remix:
        # Put remixer rules first when present
        filtered_applicable.sort(key=lambda pair: 0 if pair[1] == "remixer" else 1)

    conflict_reasons: List[str] = []
    investigation_cues: List[str] = []
    discouraged_conflict = False
    blocked_conflict = False
    genre_discouraged_conflict = False
    genre_blocked_conflict = False
    subgenre_discouraged_conflict = False
    subgenre_blocked_conflict = False
    hdrr_rule_conflict = False
    net_delta = 0
    exception_info: Optional[Dict[str, str]] = None
    override_g = ""
    override_s = ""

    # Gather all HDRR candidate classifications on this track to cross-reference against rules
    hdrr_pairs: List[Tuple[str, str, str]] = []  # (norm_g, norm_sub, raw_label)
    for c in candidates:
        if (c.source_family or "").lower() == "hdrr" or c.provider == "HDRR":
            cg = next((canonical_genre(g) for g in (c.genre or []) if canonical_genre(g)), "")
            cs = next((canonical_subgenre(g) for g in (c.genre or []) if canonical_subgenre(g)), "")
            raw_lbl = ", ".join(c.raw_source_genre or c.genre or [])
            hdrr_pairs.append((cg, cs, raw_lbl))

    for r, role in filtered_applicable:
        exc = _find_matching_exception(r, file_id, meta, candidates)
        if exc is not None:
            exception_info = {"rule_id": r.rule_id, "artist": r.artist_name, **exc}
            allowed_tok = exc.get("allowed", "*")
            if allowed_tok != "*":
                p_exc, s_exc = normalize_classification_token(allowed_tok)
                if p_exc and not override_g:
                    override_g = p_exc
                if s_exc and not override_s:
                    override_s = s_exc
            continue

        for pref in r.preferred:
            if pref not in investigation_cues:
                investigation_cues.append(pref)

        # 1. Check proposed classification against blocked / discouraged / preferred (separating parent vs subgenre scope)
        if proposed_genre or proposed_subgenre:
            b_genre_hits = _classification_matches_list(proposed_genre, "", r.blocked) if proposed_genre else []
            d_genre_hits = _classification_matches_list(proposed_genre, "", r.discouraged) if proposed_genre else []
            b_hits = _classification_matches_list(proposed_genre, proposed_subgenre, r.blocked)
            d_hits = _classification_matches_list(proposed_genre, proposed_subgenre, r.discouraged)
            p_hits = _classification_matches_list(proposed_genre, proposed_subgenre, r.preferred)
            if b_hits:
                blocked_conflict = True
                if b_genre_hits:
                    genre_blocked_conflict = True
                if set(b_hits) - set(b_genre_hits) or (proposed_subgenre and b_genre_hits):
                    subgenre_blocked_conflict = True
                net_delta -= max(2, r.confidence_penalty)
                conflict_reasons.append(
                    f"Artist rule {r.rule_id} ({r.artist_name}): blocked classification {', '.join(b_hits)}"
                )
            elif d_hits and not (is_collab and r.collab_policy == "defer_to_collaborator"):
                discouraged_conflict = True
                if d_genre_hits:
                    genre_discouraged_conflict = True
                if set(d_hits) - set(d_genre_hits) or (proposed_subgenre and d_genre_hits):
                    subgenre_discouraged_conflict = True
                net_delta -= max(1, r.confidence_penalty)
                conflict_reasons.append(
                    f"Artist rule {r.rule_id} ({r.artist_name}): discouraged classification {', '.join(d_hits)}"
                )
            elif p_hits and r.confidence_boost > 0:
                net_delta += r.confidence_boost

        # 2. Check HDRR candidate classifications against blocked / discouraged (e.g. Famous — Peygan & Firelite!)
        for hg, hs, h_raw in hdrr_pairs:
            hb = _classification_matches_list(hg, hs, r.blocked)
            hd = _classification_matches_list(hg, hs, r.discouraged)
            if hb or hd:
                hdrr_rule_conflict = True
                kind_lbl = "blocked" if hb else "discouraged"
                matched_lbl = ", ".join(hb or hd)
                msg = (
                    f"HDRR classification '{h_raw or matched_lbl}' conflicts with Artist_Rule {r.rule_id} "
                    f"({r.artist_name}: {matched_lbl} {kind_lbl})"
                )
                if msg not in conflict_reasons:
                    conflict_reasons.append(msg)

        # 3. Explicit override (only when non-collab or strict_all, user_verified, and no clash)
        if (
            r.verification_status == "user_verified"
            and (r.explicit_override_genre or r.explicit_override_subgenre)
            and (not is_collab or r.collab_policy == "strict_all")
        ):
            if r.explicit_override_genre and not override_g:
                override_g = r.explicit_override_genre
            if r.explicit_override_subgenre and not override_s:
                override_s = r.explicit_override_subgenre

    return {
        "matched_rules": [r.rule_id for r, _ in filtered_applicable],
        "applicable_artists": [r.artist_name for r, _ in filtered_applicable],
        "is_collaboration": is_collab,
        "is_remix": is_remix,
        "release_year": rel_year,
        "exception_applied": exception_info,
        "discouraged_conflict": discouraged_conflict,
        "blocked_conflict": blocked_conflict,
        "genre_discouraged_conflict": genre_discouraged_conflict,
        "genre_blocked_conflict": genre_blocked_conflict,
        "subgenre_discouraged_conflict": subgenre_discouraged_conflict,
        "subgenre_blocked_conflict": subgenre_blocked_conflict,
        "hdrr_rule_conflict": hdrr_rule_conflict,
        "conflict_reasons": conflict_reasons,
        "investigation_cues": investigation_cues,
        "explicit_override_genre": override_g,
        "explicit_override_subgenre": override_s,
        "confidence_delta": net_delta,
    }


# --- Section 6: Learning Suggested Artist Rules From Manual Corrections ---

def learn_artist_rule_suggestions(
    db: Any,
    sheet_rows: Optional[List[List[str]]] = None,
    min_corrections: int = 2,
) -> List[Dict[str, Any]]:
    """
    Identify recurring patterns where human-verified `Tracks` classifications or manual overrides
    repeatedly diverge from provider/HDRR classifications for the same artist, generating
    `pending_approval` suggestions in `artist_rule_suggestions`.

    NEVER auto-activates inferred rules (`status='pending_approval'`, `enabled=False`).
    """
    if db is None:
        return []

    ensure_default_artist_rules(db)
    existing_rules = {r.norm_artist: r for r in load_artist_rules(db=db, active_only=False)}

    # Map file_id -> human-verified (genre, subgenre, filename) from Tracks rows or field_decisions
    verified_by_fid: Dict[str, Tuple[str, str, str]] = {}
    for raw_r in (sheet_rows or []):
        padded = [str(x or "").strip() for x in raw_r] + [""] * max(0, 23 - len(raw_r))
        fid = padded[14]
        if not fid:
            continue
        status = padded[11].upper()
        g_cell, s_cell = padded[2], padded[3]
        notes_low = f"{padded[12]} {padded[15]}".lower()
        is_human_curated = (
            status in ("MOVED", "ANALYSED")
            or padded[20] == "USER_LOCKED"
            or padded[21] == "USER_LOCKED"
            or any(k in notes_low for k in ("override", "corrected", "verified", "reclassified"))
        )
        if is_human_curated and g_cell and not is_genre_placeholder(g_cell):
            cg = canonical_genre(g_cell)
            cs = "" if is_subgenre_placeholder(s_cell) else canonical_subgenre(s_cell)
            if cg:
                verified_by_fid[fid] = (cg, cs, padded[0])

    with closing(db._get_connection()) as conn:
        for fid, fname, fval, fstate in conn.execute(
            "SELECT file_id, field_name, value, state FROM field_decisions WHERE state IN ('USER_LOCKED', 'VERIFIED') AND value != ''"
        ):
            cur_g, cur_s, cur_n = verified_by_fid.get(fid, ("", "", fid))
            if fname == "genre":
                cur_g = canonical_genre(fval) or cur_g
            elif fname == "subgenre":
                cur_s = canonical_subgenre(fval) or cur_s
            if cur_g or cur_s:
                verified_by_fid[fid] = (cur_g, cur_s, cur_n)

    # Compare human-verified classifications against stored provider/HDRR candidates per artist
    rejected_sub_counts: Dict[str, Counter] = defaultdict(Counter)
    preferred_sub_counts: Dict[str, Counter] = defaultdict(Counter)
    artist_display: Dict[str, str] = {}
    evidence_tracks: Dict[str, List[Dict[str, str]]] = defaultdict(list)

    for fid, (ver_g, ver_s, disp_name) in verified_by_fid.items():
        tr = db.get_track(fid)
        if not tr:
            continue
        raw_artist = tr.metadata.artist.strip()
        if not raw_artist:
            continue
        artists = parse_artists(raw_artist)
        if len(artists) != 1:
            # Learn artist-level tendencies primarily from solo/lead releases to avoid collaborator noise
            continue
        a_norm = artists[0]
        artist_display.setdefault(a_norm, raw_artist)
        cands = [c for c in db.get_candidates(fid) if not c.is_stale]

        cand_subs: Set[str] = set()
        for c in cands:
            for g in (c.genre or []):
                cs = canonical_subgenre(g)
                if cs:
                    cand_subs.add(cs)

        if ver_s:
            preferred_sub_counts[a_norm][ver_s] += 1
            for wrong_sub in (cand_subs - {ver_s}):
                rejected_sub_counts[a_norm][wrong_sub] += 1
                evidence_tracks[a_norm].append({
                    "file_id": fid,
                    "track": tr.name or disp_name,
                    "provider_reported": wrong_sub,
                    "human_verified": ver_s,
                })

    now_iso = _utc_iso()
    suggestions_out: List[Dict[str, Any]] = []

    with closing(db._get_connection()) as conn, conn:
        for a_norm, rej_counter in rejected_sub_counts.items():
            discouraged_subs = [sub for sub, cnt in rej_counter.items() if cnt >= min_corrections]
            if not discouraged_subs:
                continue
            # Skip if an active rule already discourages/blocks all of these for this artist
            ex_rule = existing_rules.get(a_norm)
            if ex_rule and set(discouraged_subs) <= (set(ex_rule.discouraged) | set(ex_rule.blocked)):
                continue

            pref_subs = [sub for sub, cnt in preferred_sub_counts[a_norm].most_common(3) if cnt >= 1 and sub not in discouraged_subs]
            disp = artist_display.get(a_norm, a_norm)
            sug_id = f"SUG-{a_norm[:10].upper()}-{hashlib.sha1(','.join(sorted(discouraged_subs)).encode()).hexdigest()[:6].upper()}"
            draft_rule = ArtistRule(
                rule_id=f"AR-{a_norm[:10].upper()}-LEARNED",
                artist_name=disp,
                enabled=False,  # NEVER auto-activated!
                approval_status="pending_approval",
                verification_status="inferred_unapproved",
                preferred=pref_subs,
                discouraged=discouraged_subs,
                notes=f"Suggested from {len(evidence_tracks[a_norm])} recurring manual corrections; requires user approval.",
                source_origin="learned_suggestion",
                created_utc=now_iso,
                updated_utc=now_iso,
            )
            sug_payload = asdict(draft_rule)
            tracks_sample = evidence_tracks[a_norm][:10]
            conn.execute(
                """
                INSERT INTO artist_rule_suggestions(
                    suggestion_id, artist_name, norm_artist, pattern_type,
                    suggested_rule_json, supporting_tracks_json, evidence_count,
                    status, created_utc, updated_utc
                ) VALUES (?, ?, ?, 'recurring_subgenre_correction', ?, ?, ?, 'pending_approval', ?, ?)
                ON CONFLICT(suggestion_id) DO UPDATE SET
                    suggested_rule_json=excluded.suggested_rule_json,
                    supporting_tracks_json=excluded.supporting_tracks_json,
                    evidence_count=excluded.evidence_count,
                    updated_utc=excluded.updated_utc
                WHERE artist_rule_suggestions.status = 'pending_approval'
                """,
                (
                    sug_id,
                    disp,
                    a_norm,
                    json.dumps(sug_payload, sort_keys=True),
                    json.dumps(tracks_sample),
                    len(evidence_tracks[a_norm]),
                    now_iso,
                    now_iso,
                ),
            )
            suggestions_out.append({
                "suggestion_id": sug_id,
                "artist_name": disp,
                "status": "pending_approval",
                "enabled": False,
                "discouraged": discouraged_subs,
                "preferred": pref_subs,
                "evidence_count": len(evidence_tracks[a_norm]),
                "supporting_tracks": tracks_sample,
            })

    return suggestions_out


def approve_artist_rule_suggestion(db: Any, suggestion_id: str, actor: str = "user") -> Optional[ArtistRule]:
    """Explicitly approve a `pending_approval` suggestion, promoting it to an active `ArtistRule` with audit trail."""
    now_iso = _utc_iso()
    with closing(db._get_connection()) as conn:
        row = conn.execute(
            "SELECT * FROM artist_rule_suggestions WHERE suggestion_id = ?",
            (suggestion_id,),
        ).fetchone()
    if not row:
        return None
    raw_rule = json.loads(row["suggested_rule_json"])
    approved_rule = ArtistRule(
        rule_id=str(raw_rule.get("rule_id") or f"AR-{row['norm_artist'][:10].upper()}-001"),
        artist_name=row["artist_name"],
        enabled=True,
        approval_status="approved",
        verification_status="user_verified",
        preferred=list(raw_rule.get("preferred") or []),
        discouraged=list(raw_rule.get("discouraged") or []),
        blocked=list(raw_rule.get("blocked") or []),
        notes=str(raw_rule.get("notes") or ""),
        source_origin="approved_suggestion",
    )
    upsert_artist_rule(db, approved_rule, actor=actor, notes=f"Approved suggestion {suggestion_id}")
    with closing(db._get_connection()) as conn, conn:
        conn.execute(
            "UPDATE artist_rule_suggestions SET status = 'approved', updated_utc = ? WHERE suggestion_id = ?",
            (now_iso, suggestion_id),
        )
    return approved_rule


def flag_existing_rows_against_approved_rules(
    db: Any,
    sheet_rows: List[Tuple[int, List[str]]],
    rules: Optional[Iterable[ArtistRule]] = None,
) -> List[Dict[str, Any]]:
    """
    Scan existing `Tracks` rows against active approved `ArtistRule`s and flag unlocked rows whose
    current genre/subgenre conflicts with an approved rule (`discouraged` or `blocked`) for review,
    WITHOUT overwriting any existing metadata!
    """
    active_rules = list(rules) if rules is not None else load_artist_rules(db=db, active_only=True)
    flagged: List[Dict[str, Any]] = []
    for row_num, canon_r in sheet_rows:
        fid = canon_r[14].strip() if len(canon_r) > 14 else ""
        ex_g = canon_r[2].strip() if len(canon_r) > 2 else ""
        ex_s = canon_r[3].strip() if len(canon_r) > 3 else ""
        if not fid or (is_genre_placeholder(ex_g) and is_subgenre_placeholder(ex_s)):
            continue
        tr = db.get_track(fid) if db is not None else None
        meta = tr.metadata if tr else TrackMetadata(artist=canon_r[1] if len(canon_r) > 1 else "", title=canon_r[0] if canon_r else "")
        cands = db.get_candidates(fid) if (db is not None and tr) else []
        ev = evaluate_artist_rules(
            meta=meta,
            candidates=cands,
            rules=active_rules,
            proposed_genre=ex_g,
            proposed_subgenre="" if is_subgenre_placeholder(ex_s) else ex_s,
            file_id=fid,
        )
        if ev["blocked_conflict"] or ev["discouraged_conflict"]:
            flagged.append({
                "row_num": row_num,
                "file_id": fid,
                "track_name": tr.name if tr else canon_r[0],
                "existing_genre": ex_g,
                "existing_subgenre": ex_s,
                "matched_rules": ev["matched_rules"],
                "conflict_reasons": ev["conflict_reasons"],
                "investigation_cues": ev["investigation_cues"],
            })
    return flagged
