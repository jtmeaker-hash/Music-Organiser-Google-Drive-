"""
Field-level decision engine, spreadsheet lock/provenance checks (including columns L/M/P and Q:AA),
credible BPM clustering with corroborated octave reconciliation, granular subgenre verification,
and conservative spreadsheet row planning.
"""
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from ..models import Candidate, TrackMetadata
from ..providers.base import canonical_provider_family
from .artist_rules import (
    ArtistRule,
    DEFAULT_PEYGAN_RULE,
    _classification_matches_list,
    evaluate_artist_rules,
)
from .genre import (
    GENERIC_TAGS,
    SUBGENRE_PARENT,
    canonical_genre,
    canonical_subgenre,
    classify_track_genres,
    is_genre_placeholder,
    is_subgenre_placeholder,
    is_umbrella_club_genre,
)
from .matcher import format_display_bpm, is_valid_bpm, is_valid_camelot, norm, normalize_key_to_camelot

FIELDS = ["bpm", "key", "genre", "subgenre"]
COL = {"bpm": 4, "key": 5, "genre": 2, "subgenre": 3}
STATE_COL = {"bpm": 18, "key": 19, "genre": 20, "subgenre": 21}
SHEET_STATUS_COL = 11
SHEET_ANALYSIS_CONF_COL = 12
SHEET_NOTES_COL = 15

EXTRA_HEADERS = [
    "Review Star", "Review Reasons", "BPM State", "Key State",
    "Genre State", "Subgenre State", "Metadata Lock JSON",
    "Metadata Evidence Summary", "Last Review UTC", "Review Status", "Secondary Influence",
]

_IMMUTABLE_STATES = ("HIGH_LOCKED", "USER_LOCKED")
_HOLD_STATES = ("REVIEW_HOLD",)
_CONFLICT_STATES = ("CONFLICT", "LOW_CONFLICT", "REJECTED")
_OPEN_FAMILIES = frozenset({"musicbrainz", "embedded", "manual", "hdrr"})
_ARTIST_DERIVED_GENRE_FAMILIES = frozenset({"wikidata", "hdrr"})
_SPOTIFY_CORRELATED_FAMILIES = frozenset({"sonovault", "audiometa", "hdrr"})


def independent_genre_family_count(families: Set[str]) -> int:
    """
    Count genuinely independent catalogue source families for genre/subgenre confirmation.
    Collapses correlated Spotify-ecosystem families (`sonovault`, `audiometa`, `hdrr`) into at most
    one confirmation bucket so correlated upstream metadata never masquerades as independent agreement.
    """
    buckets: Set[str] = set()
    for fam in families:
        if fam in _SPOTIFY_CORRELATED_FAMILIES:
            buckets.add("spotify_ecosystem")
        elif fam and fam != "wikidata":
            buckets.add(fam)
    return len(buckets)
UPTEMPO_OCTAVE_GENRES = frozenset({
    "Hardstyle", "Hardcore", "Happy Hardcore", "Techno", "Drum & Bass", "Jungle",
    "House", "Trance", "UK Garage", "Hard Bounce", "Bassline", "Breakbeat",
    "Electro", "Dubstep", "Trap",
})


def strip_automated_vault_audit_notes(notes_text: Any) -> str:
    """Strip automated '[Vault audit ...]' footers so human override notes in Col P are isolated cleanly."""
    if not notes_text:
        return ""
    lines = [
        ln.strip()
        for ln in str(notes_text).splitlines()
        if ln.strip() and not re.match(r"^\[vault\s+audit\b", ln.strip(), flags=re.IGNORECASE)
    ]
    return "\n".join(lines)


def inspect_sheet_row_semantics(row: List[Any]) -> Dict[str, Any]:
    """
    Inspect existing Tracks columns L (Processing Status), M (Analysis Confidence), and P (Notes / Override)
    to extract human verification locks, deliberate review holds, and row-level double-check flags.
    """
    padded = [str(x or "").strip() for x in row] + [""] * max(0, 16 - len(row))
    raw_genre, raw_sub, raw_bpm, raw_key = padded[2], padded[3], padded[4], padded[5]
    proc_status = padded[SHEET_STATUS_COL].upper()
    anal_conf = padded[SHEET_ANALYSIS_CONF_COL]
    human_notes = strip_automated_vault_audit_notes(padded[SHEET_NOTES_COL])

    m_low = anal_conf.casefold()
    p_low = human_notes.casefold()
    combined_mp = f"{m_low}\n{p_low}"

    field_locks: Dict[str, Dict[str, str]] = {}
    field_holds: Dict[str, Dict[str, str]] = {}
    row_flags: List[str] = []

    # 1. Processing Status == MOVED / ANALYSED locks verified populated fields
    if proc_status in ("MOVED", "ANALYSED"):
        if raw_genre and not is_genre_placeholder(raw_genre):
            field_locks["genre"] = {"state": "USER_LOCKED", "reason": f"sheet status {proc_status}", "source": "sheet_human"}
        if raw_sub and not is_subgenre_placeholder(raw_sub) and "(provisional)" not in raw_sub.casefold():
            field_locks["subgenre"] = {"state": "USER_LOCKED", "reason": f"sheet status {proc_status}", "source": "sheet_human"}
        if is_valid_bpm(raw_bpm):
            field_locks["bpm"] = {"state": "USER_LOCKED", "reason": f"sheet status {proc_status}", "source": "sheet_human"}
        if is_valid_camelot(normalize_key_to_camelot(raw_key)):
            field_locks["key"] = {"state": "USER_LOCKED", "reason": f"sheet status {proc_status}", "source": "sheet_human"}

    # 2. Analysis Confidence / Notes explicit human BPM & Key verification locks
    if is_valid_bpm(raw_bpm) and any(
        kw in combined_mp for kw in ("bpm high", "bpm/key high", "source bpm verified", "verified bpm")
    ):
        field_locks.setdefault(
            "bpm",
            {"state": "USER_LOCKED", "reason": "human verified BPM in Analysis Confidence/Notes", "source": "sheet_human"},
        )

    if is_valid_camelot(normalize_key_to_camelot(raw_key)) and any(
        kw in combined_mp for kw in ("bpm/key high", "key high", "existing key retained", "verified key")
    ):
        field_locks.setdefault(
            "key",
            {"state": "USER_LOCKED", "reason": "human verified key in Analysis Confidence/Notes", "source": "sheet_human"},
        )

    # 3. Deliberate human withholds & unresolved conflicts in Col M / Col P
    if not raw_key and (
        any(
            kw in combined_mp
            for kw in (
                "key withheld",
                "omit key",
                "key conflict",
                "key conflicts",
                "key conflicting",
                "key unconfirmed",
                "key not confirmed",
                "no reliable key",
            )
        )
        or (proc_status == "REVIEW" and "key pending" in combined_mp)
    ):
        field_holds["key"] = {
            "state": "REVIEW_HOLD",
            "reason": "human note withheld/conflicting key",
            "source": "sheet_human",
        }
        row_flags.append("human_key_withheld_or_conflicting")

    if ("genre " in p_low and "inconsistent" in p_low) or any(
        kw in combined_mp for kw in ("genre conflict", "genre/key conflict", "key/genre conflict")
    ):
        field_holds["genre"] = {
            "state": "REVIEW_HOLD",
            "reason": "human note genre conflict hold",
            "source": "sheet_human",
        }
        row_flags.append("human_genre_conflict_hold")

    if any(
        kw in combined_mp
        for kw in (
            "potential duplicate",
            "suspected duplicate",
            "possible duplicate",
            "exact duplicate",
            "duplicate-release",
            "duplicate encoding",
            "duplicate unresolved",
        )
    ):
        row_flags.append("potential_duplicate_hold")
    if any(kw in combined_mp for kw in ("keep source in import", "do not rename or move")):
        row_flags.append("keep_in_source_hold")
    if proc_status == "REVIEW":
        row_flags.append("sheet_status_review_hold")

    return {
        "field_locks": field_locks,
        "field_holds": field_holds,
        "row_flags": row_flags,
        "human_notes": human_notes,
        "processing_status": proc_status,
        "analysis_confidence": anal_conf,
    }


def candidate_field_values(
    candidates: List[Candidate],
    field: str,
    parent_genre: str = "",
) -> List[Tuple[Any, str, str]]:
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
                v_num = round(float(val), 2)
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
            if family in _ARTIST_DERIVED_GENRE_FAMILIES or getattr(c, "genre_evidence_kind", "") == "artist_derived":
                continue
            cand_genres = {
                canonical_genre(g)
                for g in (c.genre or [])
                if isinstance(g, str) and norm(g) not in GENERIC_TAGS
            } - {""}
            if len(cand_genres) == 1:
                out.append((next(iter(cand_genres)), family, v_status))
        elif field == "subgenre":
            if family in _ARTIST_DERIVED_GENRE_FAMILIES or getattr(c, "genre_evidence_kind", "") == "artist_derived":
                continue
            cand_subs = {
                sub
                for g in (c.genre or [])
                if isinstance(g, str)
                and (sub := canonical_subgenre(g))
                and (not parent_genre or SUBGENRE_PARENT.get(sub) == parent_genre)
            }
            if len(cand_subs) == 1:
                out.append((next(iter(cand_subs)), family, v_status))
    return out


def cluster_bpm_candidates(
    candidates: List[Candidate],
    meta: Optional[TrackMetadata] = None,
    parent_genre: str = "",
    subgenre: str = "",
) -> Dict[str, Any]:
    """
    Cluster recording-verified BPM measurements and resolve half/double tempo ONLY when
    corroborated by track-specific evidence on uptempo DJ genres:
    - Direct cluster: values rounding to the SAME integer BPM (`int(round(v))`) with spread <= 0.5 BPM.
      (e.g. 151.9 + 152.0 -> 152 HIGH; 150.0 vs 151.0 -> 2 distinct clusters -> CONFLICT).
    - Octave corroboration: ONLY when `parent_genre` is in `UPTEMPO_OCTAVE_GENRES` and `subgenre != 'Slowtempo'`,
      if a slow cluster (60..107.5 BPM) co-occurs on the SAME track with a full-time DJ tempo cluster
      (118..215 BPM) where |slow * 2 - fast| <= 1.0 (or with this track's own embedded 2x BPM tag),
      resolves to the full-time integer BPM with `octave_adjusted=True` while preserving all raw float
      measurements and provenance.
    - Never auto-doubles uncorroborated slow tempos or non-DJ genres (Pop, Rock, Country, etc.);
      keeps genuine slow-tempo Hardstyle intact.
    """
    matched = candidate_field_values(candidates, "bpm")
    raw_measurements = [
        {"family": fam, "raw_bpm": v_num, "verification": v_st}
        for v_num, fam, v_st in matched
    ]
    if not matched:
        return {
            "status": "missing",
            "display_int": None,
            "families": set(),
            "direct_families": set(),
            "octave_adjusted": False,
            "raw_measurements": [],
        }

    by_int: Dict[int, List[Tuple[float, str, str]]] = {}
    for v_num, fam, v_st in matched:
        by_int.setdefault(int(round(v_num)), []).append((v_num, fam, v_st))

    clusters: List[Dict[str, Any]] = []
    for int_bpm, items in sorted(by_int.items()):
        vals = [x[0] for x in items]
        if max(vals) - min(vals) > 0.5:
            # Spread within integer bucket exceeds 0.5 BPM -> split conflicting raw values
            for v_num, fam, v_st in items:
                clusters.append({
                    "int_bpm": int_bpm,
                    "mean": v_num,
                    "vals": [v_num],
                    "families": {fam},
                    "direct_families": {fam},
                })
        else:
            clusters.append({
                "int_bpm": int_bpm,
                "mean": sum(vals) / len(vals),
                "vals": vals,
                "families": {x[1] for x in items},
                "direct_families": {x[1] for x in items},
            })

    allow_octave = bool(parent_genre in UPTEMPO_OCTAVE_GENRES and subgenre != "Slowtempo")

    # Case 1: Exactly 2 candidate clusters that form a corroborated 0.5x <-> 2x octave pair
    if len(clusters) == 2 and allow_octave:
        c_slow, c_fast = sorted(clusters, key=lambda c: c["mean"])
        if (
            60.0 <= c_slow["mean"] <= 107.5
            and 118.0 <= c_fast["mean"] <= 215.0
            and abs(c_slow["mean"] * 2.0 - c_fast["mean"]) <= 1.0
        ):
            combined_fams = c_fast["families"] | c_slow["families"]
            return {
                "status": "resolved",
                "display_int": int(round(c_fast["mean"])),
                "families": combined_fams,
                "direct_families": set(c_fast["direct_families"]),
                "octave_adjusted": True,
                "raw_measurements": raw_measurements,
            }

    if len(clusters) > 1:
        return {
            "status": "conflict",
            "display_int": None,
            "families": {fam for _, fam, _ in matched},
            "direct_families": set(),
            "octave_adjusted": False,
            "raw_measurements": raw_measurements,
        }

    # Single unanimous candidate cluster
    only = clusters[0]
    if allow_octave and meta is not None and meta.embedded_bpm and 65.0 <= only["mean"] <= 107.5:
        try:
            emb_val = float(meta.embedded_bpm)
        except ValueError:
            emb_val = 0.0
        if 130.0 <= emb_val <= 215.0 and abs(only["mean"] * 2.0 - emb_val) <= 1.0:
            raw_with_emb = raw_measurements + [{"family": "embedded", "raw_bpm": round(emb_val, 2), "verification": "local_tag"}]
            return {
                "status": "resolved",
                "display_int": int(round(emb_val)),
                "families": set(only["families"]),
                "direct_families": set(),  # Keep MEDIUM (provisional + star) since 2x came from local tag corroboration
                "octave_adjusted": True,
                "raw_measurements": raw_with_emb,
            }

    return {
        "status": "resolved",
        "display_int": int(round(only["mean"])),
        "families": set(only["families"]),
        "direct_families": set(only["direct_families"]),
        "octave_adjusted": False,
        "raw_measurements": raw_measurements,
    }


def build_track_proposal(
    meta: TrackMetadata,
    candidates: List[Candidate],
    artist_rules: Optional[List[ArtistRule]] = None,
    file_id: str = "",
) -> Dict[str, Any]:
    """
    Build unified metadata proposal from recording-supported provider candidates, HDRR artist-derived
    catalogue provenance, configurable `Artist_Rules`, and local metadata.
    - Normalizes verified/proposed BPM to integer display while retaining raw float measurements & provenance.
    - Resolves recording-supported granular subgenres within the approved SUBGENRE_PARENT taxonomy.
    - Preserves original HDRR classifications (`hdrr_raw_genres`) separately from normalized MusicVault
      classifications and NEVER lets HDRR or `Artist_Rules` preferences independently elevate to MEDIUM/HIGH.
    """
    active_rules = list(artist_rules) if artist_rules is not None else [DEFAULT_PEYGAN_RULE]

    # Exclude artist-derived HDRR candidates from weighted track-level genre promotion while retaining provenance
    non_hdrr_cands = [
        c
        for c in candidates
        if (c.source_family or canonical_provider_family(c.provider)) != "hdrr"
        and getattr(c, "genre_evidence_kind", "") != "artist_derived"
    ]
    g_info = classify_track_genres(meta, non_hdrr_cands, enable_subgenres=True)
    prop_genre = g_info["genre"]

    hdrr_raw_genres: List[str] = []
    hdrr_normalized: List[Dict[str, str]] = []
    for c in candidates:
        fam = c.source_family or canonical_provider_family(c.provider)
        if fam == "hdrr":
            for rg in (c.raw_source_genre or []):
                if rg and rg not in hdrr_raw_genres:
                    hdrr_raw_genres.append(rg)
            cg = next((canonical_genre(g) for g in (c.genre or []) if canonical_genre(g)), "")
            cs = next((canonical_subgenre(g) for g in (c.genre or []) if canonical_subgenre(g)), "")
            if cg or cs:
                entry = {"genre": cg, "subgenre": cs, "raw": ", ".join(c.raw_source_genre or c.genre or [])}
                if entry not in hdrr_normalized:
                    hdrr_normalized.append(entry)

    # Recording-verified subgenre resolution (artist/label/HDRR hints never populate verified/provisional subgenre)
    matched_subs = candidate_field_values(candidates, "subgenre", parent_genre=prop_genre) if prop_genre else []
    sub_vals = {x[0] for x in matched_subs}
    sub_fams = {x[1] for x in matched_subs}
    if len(sub_vals) > 1:
        prop_sub = "CONFLICT"
        sub_conf = "conflicting recording-supported subgenres"
    elif len(sub_vals) == 1:
        prop_sub = next(iter(sub_vals))
        sub_conf = (
            "independent recording-supported subgenre agreement"
            if independent_genre_family_count(sub_fams) >= 2
            else "single recording-supported subgenre"
        )
    else:
        prop_sub = ""
        if hdrr_normalized:
            sub_conf = "HDRR artist-derived hint only (requires independent confirmation)"
        elif g_info["subgenre"]:
            sub_conf = "artist/label hint only (unverified)"
        else:
            sub_conf = "missing"

    rule_eval = evaluate_artist_rules(
        meta=meta,
        candidates=candidates,
        rules=active_rules,
        proposed_genre=prop_genre,
        proposed_subgenre=prop_sub if prop_sub != "CONFLICT" else "",
        file_id=file_id,
    )

    override_fields: Set[str] = set()
    rec_genre_fams = independent_genre_family_count(
        {x[1] for x in candidate_field_values(candidates, "genre")}
    )
    if rule_eval["explicit_override_genre"]:
        ov_g = rule_eval["explicit_override_genre"]
        if not prop_genre:
            prop_genre = ov_g
            override_fields.add("genre")
        elif prop_genre != ov_g and rec_genre_fams < 2:
            prop_genre = ov_g
            override_fields.add("genre")
        elif prop_genre != ov_g:
            prop_genre = "CONFLICT"

    rec_sub_fams = independent_genre_family_count(
        {x[1] for x in candidate_field_values(candidates, "subgenre", parent_genre=prop_genre if prop_genre != "CONFLICT" else "")}
    )
    if rule_eval["explicit_override_subgenre"]:
        ov_s = rule_eval["explicit_override_subgenre"]
        ov_p = SUBGENRE_PARENT.get(ov_s, "")
        if not prop_genre and ov_p:
            prop_genre = ov_p
            override_fields.add("genre")
        if prop_genre == ov_p:
            if prop_sub in ("", "CONFLICT") or (prop_sub != ov_s and rec_sub_fams < 2):
                prop_sub = ov_s
                sub_conf = "explicit approved artist rule / track exception"
                override_fields.add("subgenre")
            elif prop_sub != ov_s:
                prop_sub = "CONFLICT"
                sub_conf = "explicit artist rule conflicts with >=2 independent recording-verified sources"

    if override_fields:
        rule_eval = evaluate_artist_rules(
            meta=meta,
            candidates=candidates,
            rules=active_rules,
            proposed_genre=prop_genre if prop_genre != "CONFLICT" else "",
            proposed_subgenre=prop_sub if prop_sub != "CONFLICT" else "",
            file_id=file_id,
        )

    bpm_info = cluster_bpm_candidates(candidates, meta=meta, parent_genre=prop_genre, subgenre=prop_sub)
    if bpm_info["status"] == "conflict":
        prop_bpm: Any = "CONFLICT"
        bpm_conf = "contradictory recording-supported candidates"
    elif bpm_info["status"] == "resolved" and bpm_info["display_int"] is not None:
        prop_bpm = bpm_info["display_int"]
        if bpm_info["octave_adjusted"]:
            bpm_conf = "corroborated 2x full-time tempo from half-time candidate"
        elif len(bpm_info["direct_families"]) >= 2:
            bpm_conf = "independent recording-supported agreement"
        else:
            bpm_conf = "single recording-supported candidate"
    else:
        prop_bpm = ""
        bpm_conf = "missing"
        if meta.embedded_bpm:
            try:
                b_val = round(float(meta.embedded_bpm), 1)
                if is_valid_bpm(b_val):
                    prop_bpm = int(round(b_val)) if b_val == int(b_val) else b_val
                    bpm_conf = "embedded tag only (unverified)"
            except ValueError:
                pass

    # Key proposal
    matched_keys = candidate_field_values(candidates, "key")
    if matched_keys:
        k_vals = {x[0] for x in matched_keys}
        k_fams = {x[1] for x in matched_keys}
        if len(k_vals) > 1:
            prop_key = "CONFLICT"
            key_conf = "contradictory recording-supported candidates"
        else:
            prop_key = next(iter(k_vals))
            key_conf = (
                "independent recording-supported agreement"
                if len(k_fams) >= 2
                else "single recording-supported candidate"
            )
    else:
        prop_key = ""
        key_conf = "missing"
        if meta.embedded_key:
            k_val = normalize_key_to_camelot(meta.embedded_key)
            if is_valid_camelot(k_val):
                prop_key = k_val
                key_conf = "embedded tag only (unverified)"

    tempo_hint = ""
    if (
        isinstance(prop_bpm, (int, float))
        and prop_sub != "Slowtempo"
        and not bpm_info["octave_adjusted"]
    ):
        if prop_genre in ("Hardstyle", "Hardcore", "Happy Hardcore", "Techno") and 65 <= prop_bpm <= 115:
            tempo_hint = f"Check half/double-time ({prop_bpm * 2:g} BPM)"
        elif prop_genre == "Drum & Bass" and 75 <= prop_bpm <= 98:
            tempo_hint = f"Check half/double-time ({prop_bpm * 2:g} BPM)"

    hint_sub = g_info["subgenre"] if not prop_sub else ""
    if not hint_sub and not prop_sub and hdrr_normalized and not rule_eval["hdrr_rule_conflict"]:
        hint_sub = hdrr_normalized[0].get("subgenre", "")

    return {
        "bpm": prop_bpm,
        "bpm_display": format_display_bpm(prop_bpm),
        "bpm_raw_measurements": bpm_info["raw_measurements"],
        "bpm_octave_adjusted": bpm_info["octave_adjusted"],
        "key": prop_key,
        "genre": prop_genre,
        "subgenre": prop_sub,
        "subgenre_hint": hint_sub,
        "bpm_confidence": bpm_conf,
        "key_confidence": key_conf,
        "genre_confidence": g_info["genre_confidence"],
        "subgenre_confidence": sub_conf,
        "genre_scores": g_info["genre_scores"],
        "unmapped_tags": g_info["unmapped_tags"],
        "tempo_hint": tempo_hint,
        "hdrr_raw_genres": hdrr_raw_genres,
        "hdrr_normalized": hdrr_normalized,
        "artist_rule_eval": rule_eval,
        "artist_rule_override_fields": sorted(override_fields),
    }


def field_decision(candidates: List[Candidate], field: str, proposed_value: Any) -> Tuple[str, str]:
    """
    Decide confidence level (HIGH / MEDIUM / LOW) and reason for a specific field.
    Enforces independent source families, recording-level verification gates, credible BPM
    clustering, and taxonomy-validated subgenre corroboration.
    """
    val = proposed_value.get(field, "") if isinstance(proposed_value, dict) else proposed_value
    rule_eval = proposed_value.get("artist_rule_eval", {}) if isinstance(proposed_value, dict) else {}
    override_fields = set(proposed_value.get("artist_rule_override_fields", [])) if isinstance(proposed_value, dict) else set()

    if field in ("genre", "subgenre") and rule_eval.get("hdrr_rule_conflict") and val in ("", "CONFLICT", None):
        return ("LOW", "; ".join(rule_eval.get("conflict_reasons") or ["HDRR artist-rule conflict; review required"]))

    if val in ("", "CONFLICT", None):
        if field in ("genre", "subgenre"):
            has_hdrr = any(
                (c.source_family or canonical_provider_family(c.provider)) == "hdrr"
                for c in candidates
            )
            if has_hdrr:
                return ("LOW", "HDRR artist-derived classification requires independent track-level confirmation")
        return ("LOW", "missing or conflicting")

    if field == "bpm":
        parent_g_bpm = canonical_genre(str(proposed_value.get("genre", ""))) if isinstance(proposed_value, dict) else ""
        sub = proposed_value.get("subgenre", "") if isinstance(proposed_value, dict) else ""
        bpm_info = cluster_bpm_candidates(
            candidates,
            meta=None,
            parent_genre=parent_g_bpm,
            subgenre=sub if sub != "CONFLICT" else "",
        )
        if bpm_info["status"] == "missing":
            # Check if embedded-corroborated octave resolution occurred in build_track_proposal
            if isinstance(proposed_value, dict) and proposed_value.get("bpm_octave_adjusted") and bpm_info["raw_measurements"]:
                return ("MEDIUM", "corroborated 2x full-time tempo with local tag")
            return ("LOW", "no recording-supported evidence")
        if bpm_info["status"] == "conflict":
            return ("LOW", "contradictory candidate values")
        try:
            target_int = int(round(float(val)))
        except (ValueError, TypeError):
            return ("LOW", "invalid BPM value")
        if bpm_info["display_int"] != target_int:
            if isinstance(proposed_value, dict) and proposed_value.get("bpm_octave_adjusted"):
                return ("MEDIUM", "corroborated 2x full-time tempo: " + ", ".join(sorted(bpm_info["families"])))
            return ("LOW", "contradictory candidate values")
        if bpm_info["octave_adjusted"]:
            if len(bpm_info["direct_families"]) >= 2:
                return ("HIGH", "independent recording-supported agreement (with octave corroboration): " + ", ".join(sorted(bpm_info["families"])))
            return ("MEDIUM", "corroborated 2x full-time tempo: " + ", ".join(sorted(bpm_info["families"])))
        if len(bpm_info["direct_families"]) >= 2:
            return ("HIGH", "independent recording-supported agreement: " + ", ".join(sorted(bpm_info["direct_families"])))
        return ("MEDIUM", "single matched provider: " + ", ".join(sorted(bpm_info["families"])))

    if field == "key":
        matched = candidate_field_values(candidates, "key")
        if not matched:
            return ("LOW", "no recording-supported evidence")
        norm_val = normalize_key_to_camelot(val)
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
        if rule_eval.get("genre_blocked_conflict"):
            return ("LOW", "; ".join(rule_eval.get("conflict_reasons") or ["blocked by artist rule"]))
        matched_genres = candidate_field_values(candidates, "genre")
        if "genre" in override_fields and (not matched_genres or canon_val not in {x[0] for x in matched_genres}):
            return ("MEDIUM", "explicit approved artist rule / track exception")
        if not matched_genres:
            if rule_eval.get("hdrr_rule_conflict"):
                return ("LOW", "; ".join(rule_eval["conflict_reasons"]))
            if any((c.source_family or canonical_provider_family(c.provider)) == "hdrr" for c in candidates):
                return ("LOW", "HDRR artist-derived classification requires independent track-level confirmation")
            return ("LOW", "genre weighted inference; review required")
        vals = {x[0] for x in matched_genres}
        if len(vals) != 1 or canon_val not in vals:
            return ("LOW", "conflicting catalogue genre evidence; review required")
        families = {x[1] for x in matched_genres}
        indep_count = independent_genre_family_count(families)
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
            # Artist-derived HDRR cannot veto >=2 independent recording-verified sources, nor veto when HDRR itself violates an Artist_Rule
            if fam == "hdrr" or getattr(c, "genre_evidence_kind", "") == "artist_derived":
                if indep_count >= 2 or rule_eval.get("hdrr_rule_conflict"):
                    continue
                if (c.match_confidence or "") != "exact" and v_low not in ("isrc supported", "duration supported"):
                    continue
            for g in (c.genre or []):
                cg = canonical_genre(g)
                if cg and cg != canon_val:
                    return ("LOW", "cross-source genre disagreement; review required")
        if rule_eval.get("genre_discouraged_conflict"):
            if indep_count >= 2 and int(rule_eval.get("confidence_delta", -1)) >= -1:
                return ("MEDIUM", "catalogue genre conflicts with discouraged artist rule; double-check: " + ", ".join(sorted(families)))
            return ("LOW", "; ".join(rule_eval.get("conflict_reasons") or ["discouraged by artist rule; review required"]))
        if indep_count >= 2:
            return ("HIGH", "independent recording-supported genre agreement: " + ", ".join(sorted(families)))
        return ("MEDIUM", "single recording-supported catalogue genre: " + ", ".join(sorted(families)))

    if field == "subgenre":
        canon_sub = canonical_subgenre(str(val))
        if not canon_sub:
            return ("LOW", "unknown or unmapped subgenre; review required")
        if rule_eval.get("subgenre_blocked_conflict") or rule_eval.get("genre_blocked_conflict"):
            return ("LOW", "; ".join(rule_eval.get("conflict_reasons") or ["blocked by artist rule"]))
        parent_g = (
            canonical_genre(str(proposed_value.get("genre", "")))
            if isinstance(proposed_value, dict)
            else SUBGENRE_PARENT.get(canon_sub, "")
        ) or SUBGENRE_PARENT.get(canon_sub, "")
        if SUBGENRE_PARENT.get(canon_sub) != parent_g:
            return ("LOW", "subgenre incompatible with parent genre")
        if isinstance(proposed_value, dict):
            g_lvl, _ = field_decision(candidates, "genre", proposed_value)
            if g_lvl == "LOW":
                return ("LOW", "parent genre unresolved; subgenre held for review")
        matched_subs = candidate_field_values(candidates, "subgenre", parent_genre=parent_g)
        if "subgenre" in override_fields and (not matched_subs or canon_sub not in {x[0] for x in matched_subs}):
            return ("MEDIUM", "explicit approved artist rule / track exception")
        if not matched_subs:
            if rule_eval.get("hdrr_rule_conflict"):
                return ("LOW", "; ".join(rule_eval["conflict_reasons"]))
            if any((c.source_family or canonical_provider_family(c.provider)) == "hdrr" for c in candidates):
                return ("LOW", "HDRR artist-derived subgenre requires independent track-level confirmation")
            return ("LOW", "subgenre artist/label inference; review required")
        vals = {x[0] for x in matched_subs}
        if len(vals) != 1 or canon_sub not in vals:
            return ("LOW", "conflicting recording-supported subgenre evidence; review required")
        families = {x[1] for x in matched_subs}
        indep_count = independent_genre_family_count(families)
        # Also block promotion if any recording-verified candidate carries a contradictory subgenre under parent_g
        for c in candidates:
            if (c.verification or "") not in ("ISRC supported", "Duration supported"):
                continue
            fam = c.source_family or canonical_provider_family(c.provider)
            if fam in _ARTIST_DERIVED_GENRE_FAMILIES or getattr(c, "genre_evidence_kind", "") == "artist_derived":
                if indep_count >= 2 or rule_eval.get("hdrr_rule_conflict"):
                    continue
            for g in (c.genre or []):
                cs = canonical_subgenre(g)
                if cs and SUBGENRE_PARENT.get(cs) == parent_g and cs != canon_sub:
                    return ("LOW", "cross-source subgenre disagreement; review required")
        if rule_eval.get("subgenre_discouraged_conflict") or rule_eval.get("genre_discouraged_conflict"):
            if indep_count >= 2 and int(rule_eval.get("confidence_delta", -1)) >= -1:
                return ("MEDIUM", "catalogue subgenre conflicts with discouraged artist rule; double-check: " + ", ".join(sorted(families)))
            return ("LOW", "; ".join(rule_eval.get("conflict_reasons") or ["discouraged by artist rule; review required"]))
        parent_high = (
            field_decision(candidates, "genre", proposed_value)[0] == "HIGH"
            if isinstance(proposed_value, dict)
            else indep_count >= 2
        )
        hdrr_corroborates = any(
            (c.source_family or canonical_provider_family(c.provider)) == "hdrr"
            and any(canonical_subgenre(g) == canon_sub for g in (c.genre or []))
            for c in candidates
        )
        pref_suffix = " (+preferred artist rule)" if int(rule_eval.get("confidence_delta", 0)) > 0 else ""
        suffix = (" (+hdrr corroborated)" if hdrr_corroborates else "") + pref_suffix
        if indep_count >= 2 and parent_high:
            return ("HIGH", "independent recording-supported subgenre agreement: " + ", ".join(sorted(families)) + suffix)
        return ("MEDIUM", "recording-supported catalogue subgenre: " + ", ".join(sorted(families)) + suffix)

    return ("LOW", "review required")


def parse_lock_states(row: List[Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    """
    Parse metadata lock JSON from column W (index 22), cross-check state columns S:V (18:21),
    and incorporate human provenance locks & holds from columns L (11), M (12), and P (15).
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

    for f_name, col_idx in STATE_COL.items():
        if len(row) > col_idx and str(row[col_idx] or "").strip():
            col_state = str(row[col_idx]).strip()
            existing_entry = states.get(f_name)
            if not existing_entry:
                states[f_name] = {"state": col_state}
            elif col_state in _IMMUTABLE_STATES and existing_entry.get("state") not in _IMMUTABLE_STATES:
                states[f_name]["state"] = col_state

    # Incorporate human locks & review holds from Columns L, M, and P without overriding explicit Q:AA locks
    sem = inspect_sheet_row_semantics(row)
    for f_name, lock_info in sem["field_locks"].items():
        if f_name not in states or states[f_name].get("state") not in _IMMUTABLE_STATES:
            states[f_name] = dict(lock_info)
    for f_name, hold_info in sem["field_holds"].items():
        if f_name not in states:
            states[f_name] = dict(hold_info)

    return states, None


def load_existing_states(row: List[Any]) -> Dict[str, Any]:
    """Load metadata lock JSON from column W (backward-compatible helper)."""
    states, err = parse_lock_states(row)
    if err is not None:
        return {f: {"state": "CONFLICT", "reason": err} for f in FIELDS}
    return states


def _is_open_provenance_only(candidates: List[Candidate], field: str, parent_genre: str = "") -> bool:
    """Return True when all recording-verified candidate families for `field` belong to open CC0 provenance."""
    matched = candidate_field_values(candidates, field, parent_genre=parent_genre)
    if not matched:
        return False
    return {x[1] for x in matched} <= _OPEN_FAMILIES


def planned_row(
    candidates: List[Candidate],
    proposal_row: Dict[str, Any],
    old_row: List[Any],
    save_medium: bool = True,
    provider_terms_cleared: bool = False,
    overrides: Optional[Dict[str, str]] = None,
    artist_rules: Optional[List[ArtistRule]] = None,
) -> Tuple[List[str], Dict[str, str], List[str]]:
    """
    Compute proposed row updates while strictly preserving human locks (L/M/P & Q:AA),
    existing concrete values, review-column content, and provenance.
    - Treats 'Unclassified' / 'Needs Review' / '*Subgenre pending' as placeholders (preserving
      their text when evidence is LOW/MISSING/TERMS_HOLD, upgrading only on HIGH/MEDIUM).
    - Upgrades umbrella 'Dance / Club' & 'Dance & Club' to 'Hardstyle' ONLY on recording-supported
      HIGH/MEDIUM Hardstyle evidence without conflict or locks.
    - Formats proposed BPM as rounded integer display string.
    """
    out = list(old_row) + [""] * max(0, 27 - len(old_row))
    out = [str(x) if x is not None else "" for x in out[:27]]

    states, lock_err = parse_lock_states(old_row)
    if lock_err is not None:
        decisions = {f: "LOCK_ERROR_HOLD" for f in FIELDS}
        out[16] = "★"
        return out, decisions, []

    sem = inspect_sheet_row_semantics(old_row)
    reasons: List[str] = []
    decisions: Dict[str, str] = {}
    changes: List[str] = []

    effective_proposal = dict(proposal_row)
    if overrides:
        for field, value in overrides.items():
            if field in FIELDS:
                effective_proposal[field] = value

    now_iso = datetime.now(timezone.utc).isoformat()
    parent_g = canonical_genre(str(effective_proposal.get("genre", "")))

    for field in FIELDS:
        val = effective_proposal.get(field, "")
        level, why = field_decision(candidates, field, effective_proposal)
        manual_override = bool(overrides and field in overrides)
        if manual_override:
            level = "HIGH"
            why = "manual override"

        existing = out[COL[field]].strip()
        prior = states.get(field, {}) if isinstance(states.get(field, {}), dict) else {}
        prior_status = prior.get("state", "")

        if prior_status in _IMMUTABLE_STATES and not manual_override:
            decisions[field] = "LOCKED"
            continue

        if prior_status in _HOLD_STATES and not manual_override:
            decisions[field] = "REVIEW_HOLD"
            reasons.append(f"{field}: {prior.get('reason', 'human review hold')}")
            continue

        is_placeholder = (
            (field == "genre" and is_genre_placeholder(existing))
            or (field == "subgenre" and is_subgenre_placeholder(existing))
        )
        is_umbrella_upgrade = (
            field == "genre"
            and is_umbrella_club_genre(existing)
            and "sheet_status_review_hold" not in sem["row_flags"]
            and str(val) == "Hardstyle"
            and (level == "HIGH" or (level == "MEDIUM" and save_medium))
        )

        # Concrete existing value that is neither empty, placeholder, nor an evidence-backed Dance/Club->Hardstyle upgrade:
        if existing != "" and not is_placeholder and not is_umbrella_upgrade:
            # Check if strong new evidence contradicts an unlocked existing concrete value -> flag without overwriting!
            contradicts_existing = False
            if level in ("HIGH", "MEDIUM") and val not in ("", "CONFLICT", None):
                if field == "genre":
                    ex_cg = canonical_genre(existing)
                    contradicts_existing = bool(
                        (ex_cg and ex_cg != canonical_genre(str(val)))
                        or (is_umbrella_club_genre(existing) and canonical_genre(str(val)) == "Hardstyle")
                    )
                elif field == "bpm" and is_valid_bpm(existing):
                    contradicts_existing = abs(int(round(float(existing))) - int(round(float(val)))) >= 2
                elif field == "key":
                    ex_k = normalize_key_to_camelot(existing)
                    contradicts_existing = bool(ex_k and ex_k != normalize_key_to_camelot(val))
            if contradicts_existing:
                decisions[field] = "EXISTING_CONFLICT_REVIEW"
                reasons.append(f"{field}: existing '{existing}' differs from {level} catalogue '{val}'; preserved for review")
                continue

            # Check whether an active approved Artist_Rule flags this existing unlocked genre/subgenre for review
            active_r_list = list(artist_rules) if artist_rules is not None else [DEFAULT_PEYGAN_RULE]
            rule_ev_cur = effective_proposal.get("artist_rule_eval") or {}
            if field in ("genre", "subgenre") and rule_ev_cur.get("matched_rules"):
                matched_ids = set(rule_ev_cur["matched_rules"])
                for ar in active_r_list:
                    if ar.rule_id not in matched_ids or not ar.is_active:
                        continue
                    g_chk = existing if field == "genre" else out[COL["genre"]].strip()
                    s_chk = existing if field == "subgenre" else ""
                    hit_blk = _classification_matches_list(g_chk if field == "genre" else "", s_chk, ar.blocked)
                    hit_dsc = _classification_matches_list(g_chk if field == "genre" else "", s_chk, ar.discouraged)
                    if hit_blk or hit_dsc:
                        decisions[field] = "EXISTING_CONFLICT_REVIEW"
                        reasons.append(
                            f"{field}: existing '{existing}' conflicts with approved Artist_Rule {ar.rule_id} "
                            f"({ar.artist_name}); preserved for review"
                        )
                        break
                if decisions.get(field) == "EXISTING_CONFLICT_REVIEW":
                    continue

            decisions[field] = (
                "EXISTING_UNVERIFIED"
                if prior_status not in ("MEDIUM_PROVISIONAL", "HIGH_LOCKED", "USER_LOCKED")
                else prior_status
            )
            if decisions[field] == "EXISTING_UNVERIFIED":
                reasons.append(f"{field}: existing value preserved; verification unknown")
            continue

        # Blank, placeholder, or eligible Dance/Club -> Hardstyle upgrade:
        if (
            field == "bpm"
            and effective_proposal.get("bpm_octave_adjusted")
            and canonical_subgenre(out[COL["subgenre"]]) == "Slowtempo"
            and not manual_override
        ):
            decisions[field] = "REVIEW"
            reasons.append("bpm: sheet subgenre Slowtempo blocks 2x octave promotion")
            continue

        if level == "HIGH" or (level == "MEDIUM" and save_medium):
            ov_set = set(effective_proposal.get("artist_rule_override_fields") or [])
            is_rule_override = field in ov_set
            implied_by_sub_override = (
                field == "genre"
                and "subgenre" in ov_set
                and SUBGENRE_PARENT.get(str(effective_proposal.get("subgenre", ""))) == str(val)
            )
            open_only = (
                manual_override
                or is_rule_override
                or implied_by_sub_override
                or _is_open_provenance_only(candidates, field, parent_genre=parent_g)
            )
            if not provider_terms_cleared and not open_only:
                decisions[field] = "TERMS_HOLD"
                reasons.append(f"{field}: provider retention not cleared")
                continue
            if (
                field == "subgenre"
                and not manual_override
                and (not out[COL["genre"]].strip() or is_genre_placeholder(out[COL["genre"]].strip()))
            ):
                decisions[field] = "TERMS_HOLD" if decisions.get("genre") == "TERMS_HOLD" else "REVIEW"
                reasons.append("subgenre: deferred because parent genre remains held")
                continue

            display_val = format_display_bpm(val) if field == "bpm" else str(val)
            if not display_val:
                decisions[field] = "REVIEW"
                reasons.append(f"{field}: {why}")
                continue

            if out[COL[field]] != display_val:
                out[COL[field]] = display_val
                changes.append(field)
            octave_adj = bool(field == "bpm" and effective_proposal.get("bpm_octave_adjusted"))
            state = "HIGH_LOCKED" if (level == "HIGH" and not is_umbrella_upgrade and not octave_adj) else "MEDIUM_PROVISIONAL"
            if level == "HIGH" and is_umbrella_upgrade:
                state = "HIGH_LOCKED"
                reasons.append("genre: upgraded Dance/Club -> Hardstyle (HIGH recording agreement)")
            elif is_umbrella_upgrade:
                reasons.append("genre: provisional Dance/Club -> Hardstyle upgrade; double-check")
            elif octave_adj:
                reasons.append(f"bpm: {why}; double-check octave adjustment")
            elif level == "MEDIUM":
                reasons.append(f"{field}: provisional")

            states[field] = {
                "state": state,
                "reason": why,
                "source": "manual" if manual_override else ("artist_rule" if is_rule_override else "catalogue"),
                "reviewed_utc": now_iso,
            }
            decisions[field] = state
        else:
            # Preserve existing placeholder text intact when evidence is LOW / MISSING / CONFLICT!
            decisions[field] = "REVIEW"
            reasons.append(f"{field}: {why}")

    rule_eval = effective_proposal.get("artist_rule_eval") or {}
    for c_rsn in (rule_eval.get("conflict_reasons") or []):
        reasons.append(c_rsn)
    if rule_eval.get("investigation_cues") and decisions.get("subgenre") in ("REVIEW", "REVIEW_HOLD", "EXISTING_CONFLICT_REVIEW"):
        reasons.append("investigate_subgenres: " + ", ".join(rule_eval["investigation_cues"]))
    for flag in sem["row_flags"]:
        reasons.append(f"row_hold: {flag}")

    for field in FIELDS:
        s_val = states.get(field, {}).get("state", "")
        if s_val:
            out[STATE_COL[field]] = s_val

    if states:
        out[22] = json.dumps(states, sort_keys=True, separators=(",", ":"))
    out[23] = "; ".join(f"{k}={v}" for k, v in decisions.items())
    out[24] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    star = any(
        decisions.get(f) in ("REVIEW", "REVIEW_HOLD", "TERMS_HOLD", "MEDIUM_PROVISIONAL", "EXISTING_CONFLICT_REVIEW", "EXISTING")
        and (not out[COL[f]] or decisions.get(f) != "EXISTING")
        for f in FIELDS
    )
    star = star or any(states.get(f, {}).get("state") == "MEDIUM_PROVISIONAL" for f in FIELDS)
    star = star or any(decisions.get(f) == "EXISTING_UNVERIFIED" for f in FIELDS)
    star = star or bool(sem["row_flags"])

    out[16] = "★" if star or (len(old_row) > 16 and old_row[16] == "★") else ""
    out[17] = "; ".join(dict.fromkeys(reasons)) if reasons else out[17]
    out[25] = "STARRED" if out[16] else "COMPLETE"
    if len(old_row) > 26 and old_row[26] and not out[26]:
        out[26] = str(old_row[26])

    return out, decisions, changes
