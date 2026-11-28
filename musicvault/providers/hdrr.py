"""
Native MusicVault research provider for Hard Dance Release Radar (https://www.hdrr.info/).

Retrieval Architecture & Compliance Rationale:
- Option 1 (`/api/v1/public/...` internal JSON endpoints) is explicitly disallowed for automated
  clients by `https://www.hdrr.info/robots.txt` (`Disallow: /api/` and `Disallow: /share/` under
  `User-agent: *`).
- Option 2 (Permitted Public SSR HTML + Schema.org JSON-LD + Astro SSR Props Extraction) is
  explicitly permitted by `robots.txt` (`# AI crawlers are deliberately welcome here...`) on
  public routes (`/artists/<slug>`, `/artists?q=...`, `/search?q=...`, `/tracks/<slug>`).
- Strictly enforces `robots.txt` (`Disallow: /api/`, `Disallow: /share/`, no `/media/img/` pulls),
  never attempts to bypass authentication or anti-bot gates, paces requests politely (>= 2.0s
  default), caches structured artist catalogues in SQLite (`hdrr_artists` + `hdrr_tracks`) so
  multiple library tracks by the same artist cost at most 1 HTTP request, and preserves original
  HDRR classifications separately from normalized MusicVault `(genre, subgenre)` classifications
  marked explicitly as `genre_evidence_kind="artist_derived"`.
"""
import difflib
import html as html_mod
import json
import logging
import posixpath
import re
import unicodedata
import urllib.parse
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from ..core.genre import canonical_genre, canonical_subgenre
from ..core.matcher import extract_version_tokens, norm, parse_artists, versions_compatible
from ..models import Candidate
from .base import BaseProvider, ProviderCallOutcome, ProviderStatus

logger = logging.getLogger("musicvault.providers.hdrr")

HDRR_BASE_URL = "https://www.hdrr.info"
_DISALLOWED_HDRR_PREFIXES = ("/api/", "/share/", "/media/img/")
_DISALLOWED_HDRR_EXACT = frozenset({"/api", "/share", "/media/img"})


def _assert_allowed_hdrr_url(url_or_path: str) -> str:
    """
    Validate and normalize an HDRR URL or path against `robots.txt` rules:
    - Enforces `https://www.hdrr.info` when a scheme/host is supplied.
    - Normalizes dot-segments (`..`, `.`) and percent-encoded slashes before checking prefixes.
    - Hard-blocks `/api`, `/api/*`, `/share`, `/share/*`, `/media/img`, and `/media/img/*`.
    Returns the normalized path starting with `/`.
    """
    raw = str(url_or_path or "").strip()
    if not raw:
        raise RuntimeError("HDRR robots.txt compliance guard blocked empty URL/path")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme.lower() != "https" or parsed.netloc.lower() != "www.hdrr.info":
            raise RuntimeError(f"HDRR compliance guard blocked unexpected host/scheme: {raw}")
        raw_path = parsed.path or "/"
    else:
        raw_path = parsed.path or "/"
    unquoted = urllib.parse.unquote(raw_path)
    norm_path = posixpath.normpath("/" + unquoted.lstrip("/"))
    if not norm_path.startswith("/"):
        norm_path = "/" + norm_path
    low_path = norm_path.casefold()
    if low_path in _DISALLOWED_HDRR_EXACT or any(low_path.startswith(dis) for dis in _DISALLOWED_HDRR_PREFIXES):
        raise RuntimeError(f"HDRR robots.txt compliance guard blocked disallowed path: {norm_path}")
    return norm_path

_ANTI_BOT_GATE_RE = re.compile(
    r"(?:cf-turnstile|g-recaptcha|h-captcha|Just a moment\.\.\.|Enable JavaScript and cookies to continue|verify you are human)",
    re.IGNORECASE,
)

# Explicit mapping from HDRR's 23 source genres/slugs -> Normalized MusicVault (parent_genre, subgenre)
# Original HDRR label/slug is ALWAYS preserved separately in `Candidate.raw_source_genre` and `source_metadata`.
HDRR_GENRE_MAP: Dict[str, Tuple[str, str]] = {
    "euphorichardstyle": ("Hardstyle", "Euphoric"),
    "rawstyle": ("Hardstyle", "Rawstyle"),
    "rawphorichardstyle": ("Hardstyle", "Rawphoric"),
    "rawphoric": ("Hardstyle", "Rawphoric"),
    "xtrarawstyle": ("Hardstyle", "Xtra Raw"),
    "xtraraw": ("Hardstyle", "Xtra Raw"),
    "experimentalraw": ("Hardstyle", "Experimental Raw"),
    "experimentalrawstyle": ("Hardstyle", "Experimental Raw"),
    "mainsteamhardstyle": ("Hardstyle", "Mainstream Hardstyle"),  # Exact HDRR slug typo in llms.txt
    "mainstreamhardstyle": ("Hardstyle", "Mainstream Hardstyle"),
    "classichardstyle": ("Hardstyle", "Reverse Bass"),
    "earlyhardstyle": ("Hardstyle", "Reverse Bass"),
    "reversebass": ("Hardstyle", "Reverse Bass"),
    "hardstyle": ("Hardstyle", ""),
    "uptempohardcore": ("Hardcore", "Uptempo"),
    "uptempo": ("Hardcore", "Uptempo"),
    "deutscherkrach": ("Hardcore", "Uptempo"),
    "frenchcore": ("Hardcore", "Frenchcore"),
    "gabba": ("Hardcore", "Gabber"),
    "gabber": ("Hardcore", "Gabber"),
    "earlyhardcore": ("Hardcore", "Gabber"),
    "hardcoretechno": ("Hardcore", "Mainstream Hardcore"),
    "mainstreamhardcore": ("Hardcore", "Mainstream Hardcore"),
    "doomcore": ("Hardcore", "Doomcore"),
    "speedcore": ("Hardcore", "Speedcore"),
    "jcore": ("Hardcore", "J-Core"),
    "hardcore": ("Hardcore", ""),
    "happyhardcore": ("Happy Hardcore", ""),
    "ukhardcore": ("Happy Hardcore", "UK Hardcore"),
    "deephappyhardcore": ("Happy Hardcore", "Deep Happy Hardcore"),
    "hardtechno": ("Techno", "Hard Techno"),
    "schranz": ("Techno", "Hard Techno"),
    "industrialtechno": ("Techno", "Industrial Techno"),
    "techno": ("Techno", ""),
}


def slugify_hdrr(value: str) -> str:
    """Convert an artist or track name to HDRR's standard ASCII hyphenated URL slug."""
    if not value:
        return ""
    ascii_txt = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_txt.lower()).strip("-")
    return slug


def map_hdrr_genre(raw_slug: str, raw_label: str) -> Tuple[str, str]:
    """
    Map an HDRR genre slug/label to normalized MusicVault `(parent_genre, subgenre)`
    without losing the raw HDRR classification.
    """
    for candidate_raw in (raw_slug, raw_label):
        key = norm(candidate_raw or "")
        if key in HDRR_GENRE_MAP:
            return HDRR_GENRE_MAP[key]
    for candidate_raw in (raw_label, raw_slug):
        if not candidate_raw:
            continue
        sub = canonical_subgenre(candidate_raw)
        parent = canonical_genre(candidate_raw)
        if parent or sub:
            return parent, sub
    return "", ""


def _decode_astro_value(node: Any) -> Any:
    """Decode Astro's serialized SSR island prop tuples (`[0, primitive]`, `[1, array]`)."""
    if isinstance(node, list) and len(node) == 2 and isinstance(node[0], int):
        tag, val = node
        if tag == 0:
            return _decode_astro_value(val)
        if tag == 1 and isinstance(val, list):
            return [_decode_astro_value(x) for x in val]
        return val
    if isinstance(node, dict):
        return {k: _decode_astro_value(v) for k, v in node.items()}
    return node


def _parse_year(date_str: Any) -> Optional[int]:
    if not date_str:
        return None
    m = re.match(r"^(\d{4})\b", str(date_str).strip())
    if not m:
        return None
    yr = int(m.group(1))
    return yr if 1985 <= yr <= 2100 else None


def _strip_benign_subtitle(raw_title: str) -> str:
    """Strip festival anthem / year subtitles from a raw title before version/base normalization."""
    cleaned = re.sub(
        r"[\(\[\-]?\s*\b(?:official\s+)?(?:defqon\.?\s*1|decibel(?:\s+outdoor)?|intents(?:\s+festival)?|qlimax|reverze|dominator|supremacy|rebirth|rawzone(?:\s+arena)?)\s*(?:20\d{2})?\s*(?:closing\s+theme|anthem|ost)\b\s*[\)\]]?",
        "",
        raw_title,
        flags=re.IGNORECASE,
    )
    base, _, _ = extract_version_tokens(cleaned)
    return base or norm(cleaned)


def parse_hdrr_artist_html(html_text: str, fallback_slug: str = "") -> Optional[Dict[str, Any]]:
    """
    Extract structured artist profile and track catalogue items from a permitted HDRR
    `/artists/<slug>` SSR HTML page using Schema.org JSON-LD + Astro `TrackExplorer` props,
    with semantic `<main>` HTML fallback.
    """
    if not html_text or "<html" not in html_text.lower():
        return None

    artist_name = ""
    artist_slug = fallback_slug
    artist_genres: List[str] = []
    spotify_url = ""
    followers = 0
    tracked_releases = 0

    # 1. Schema.org MusicGroup JSON-LD
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html_text, flags=re.S | re.I):
        try:
            ld = json.loads(m.group(1).strip())
        except ValueError:
            continue
        if isinstance(ld, dict) and ld.get("@type") in ("MusicGroup", "Person"):
            artist_name = str(ld.get("name") or artist_name).strip()
            ld_url = str(ld.get("url") or "")
            if "/artists/" in ld_url:
                artist_slug = ld_url.rstrip("/").rsplit("/artists/", 1)[-1].split("?")[0] or artist_slug
            raw_g = ld.get("genre")
            if isinstance(raw_g, list):
                artist_genres = [str(g).strip() for g in raw_g if str(g).strip()]
            elif isinstance(raw_g, str) and raw_g.strip():
                artist_genres = [raw_g.strip()]
            same_as = ld.get("sameAs") or []
            if isinstance(same_as, list):
                for u in same_as:
                    if isinstance(u, str) and "open.spotify.com/artist/" in u:
                        spotify_url = u
                        break
            inter = ld.get("interactionStatistic")
            if isinstance(inter, dict) and str(inter.get("userInteractionCount", "")).isdigit():
                followers = int(inter["userInteractionCount"])

    # Fallback artist H1 if JSON-LD absent
    if not artist_name:
        h1_m = re.search(r"<h1[^>]*>(.*?)</h1>", html_text, flags=re.S | re.I)
        if h1_m:
            artist_name = html_mod.unescape(re.sub(r"<[^>]+>", "", h1_m.group(1))).strip()

    rel_cnt_m = re.search(r"<dt>\s*Tracked releases:\s*</dt>\s*<dd[^>]*>\s*([\d,]+)\s*</dd>", html_text, flags=re.I)
    if rel_cnt_m:
        tracked_releases = int(rel_cnt_m.group(1).replace(",", ""))

    items_out: List[Dict[str, Any]] = []
    total_tracks = 0
    page_num = 1
    page_size = 20

    # 2. Primary structured source: Astro SSR TrackExplorer island props
    for m in re.finditer(r'<astro-island[^>]*component-url="([^"]*TrackExplorer[^"]*)"[^>]*props="([^"]*)"', html_text, flags=re.I):
        try:
            decoded = _decode_astro_value(json.loads(html_mod.unescape(m.group(2))))
        except ValueError:
            continue
        if not isinstance(decoded, dict):
            continue
        fp = str(decoded.get("fetchPath") or "")
        if "/artists/" in fp and "/tracks" in fp and not artist_slug:
            artist_slug = fp.split("/artists/", 1)[1].split("/tracks", 1)[0].strip("/")
        init_obj = decoded.get("initial")
        if not isinstance(init_obj, dict):
            continue
        total_tracks = int(init_obj.get("total") or 0)
        page_num = int(init_obj.get("page") or 1)
        page_size = int(init_obj.get("pageSize") or 20)
        for raw_item in (init_obj.get("items") or []):
            if not isinstance(raw_item, dict):
                continue
            t_slug = str(raw_item.get("slug") or "").strip()
            t_name = html_mod.unescape(str(raw_item.get("name") or "")).strip()
            if not t_slug or not t_name:
                continue
            g_obj = raw_item.get("genre") if isinstance(raw_item.get("genre"), dict) else {}
            g_slug = str(g_obj.get("slug") or "").strip()
            g_label = html_mod.unescape(str(g_obj.get("label") or "")).strip()
            art_names: List[str] = []
            art_slugs: List[str] = []
            for a_obj in (raw_item.get("artists") or []):
                if isinstance(a_obj, dict):
                    an = html_mod.unescape(str(a_obj.get("name") or "")).strip()
                    asl = str(a_obj.get("slug") or "").strip()
                    if an:
                        art_names.append(an)
                    if asl:
                        art_slugs.append(asl)
                elif isinstance(a_obj, str) and a_obj.strip():
                    art_names.append(html_mod.unescape(a_obj).strip())
            rel_date = str(raw_item.get("releaseDate") or "").strip()
            pop_val = raw_item.get("popularity")
            items_out.append({
                "track_slug": t_slug,
                "title": t_name,
                "artists": art_names or ([artist_name] if artist_name else []),
                "artist_slugs": art_slugs or ([artist_slug] if artist_slug else []),
                "raw_genre_slug": g_slug,
                "raw_genre_label": g_label,
                "release_date": rel_date,
                "release_year": _parse_year(rel_date),
                "popularity": int(pop_val) if isinstance(pop_val, (int, float)) else None,
            })
        break

    # 3. Semantic HTML fallback if TrackExplorer island props absent
    if not items_out:
        main_m = re.search(r"<main[^>]*>(.*?)</main>", html_text, flags=re.S | re.I)
        main_html = main_m.group(1) if main_m else html_text
        for card_m in re.finditer(
            r'<a[^>]*href="/tracks/([a-z0-9\-]+)"[^>]*>(.*?)</a>',
            main_html,
            flags=re.S | re.I,
        ):
            t_slug = card_m.group(1).strip()
            t_title = html_mod.unescape(re.sub(r"<[^>]+>", " ", card_m.group(2))).strip()
            t_title = re.sub(r"\s+", " ", t_title)
            if t_slug and t_title and t_title.lower() not in ("play", "share", "view"):
                items_out.append({
                    "track_slug": t_slug,
                    "title": t_title,
                    "artists": [artist_name] if artist_name else [],
                    "artist_slugs": [artist_slug] if artist_slug else [],
                    "raw_genre_slug": slugify_hdrr(artist_genres[0]) if artist_genres else "",
                    "raw_genre_label": artist_genres[0] if artist_genres else "",
                    "release_date": "",
                    "release_year": None,
                    "popularity": None,
                })
        if not total_tracks:
            total_tracks = tracked_releases or len(items_out)

    if not artist_name and not items_out:
        return None

    return {
        "artist_slug": artist_slug or slugify_hdrr(artist_name),
        "name": artist_name,
        "genres": artist_genres,
        "spotify_url": spotify_url,
        "followers": followers,
        "tracked_releases": tracked_releases or total_tracks or len(items_out),
        "total_tracks": total_tracks or tracked_releases or len(items_out),
        "page": page_num,
        "page_size": page_size,
        "items": items_out,
    }


def parse_hdrr_search_html(html_text: str) -> Dict[str, List[Dict[str, Any]]]:
    """Parse permitted `/search?q=...` or `/artists?q=...` SSR HTML into artist and track links."""
    artists: List[Dict[str, Any]] = []
    tracks: List[Dict[str, Any]] = []
    if not html_text:
        return {"artists": artists, "tracks": tracks}

    seen_a: Set[str] = set()
    for m in re.finditer(r'<a[^>]*href="/artists/([a-z0-9\-]+)"[^>]*>(.*?)</a>', html_text, flags=re.S | re.I):
        slug = m.group(1).strip()
        if not slug or slug in seen_a or slug == "kit":
            continue
        spans = [
            html_mod.unescape(re.sub(r"<[^>]+>", "", s)).strip()
            for s in re.findall(r"<span[^>]*>(.*?)</span>", m.group(2), flags=re.S | re.I)
        ]
        spans = [s for s in spans if s and "follower" not in s.lower()]
        label = spans[0] if spans else html_mod.unescape(re.sub(r"<[^>]+>", " ", m.group(2))).strip()
        label = re.sub(r"\s+", " ", label)
        if label:
            seen_a.add(slug)
            artists.append({"artist_slug": slug, "name": label})

    seen_t: Set[str] = set()
    for m in re.finditer(r'<a[^>]*href="/tracks/([a-z0-9\-]+)"[^>]*>(.*?)</a>', html_text, flags=re.S | re.I):
        slug = m.group(1).strip()
        if not slug or slug in seen_t:
            continue
        inner = m.group(2)
        alt_m = re.search(r'alt="([^"]+)"', inner)
        spans = [
            html_mod.unescape(re.sub(r"<[^>]+>", "", s)).strip()
            for s in re.findall(r"<span[^>]*>(.*?)</span>", inner, flags=re.S | re.I)
        ]
        spans = [s for s in spans if s]
        t_title = spans[0] if len(spans) >= 2 else ""
        t_artists_str = spans[-1] if len(spans) >= 2 else ""
        if (not t_title or t_title == t_artists_str) and alt_m and " - " in alt_m.group(1):
            t_title, t_artists_str = html_mod.unescape(alt_m.group(1)).rsplit(" - ", 1)
        if t_title:
            seen_t.add(slug)
            t_artists = [x.strip() for x in t_artists_str.split(",") if x.strip()]
            tracks.append({"track_slug": slug, "title": t_title.strip(), "artists": t_artists})

    return {"artists": artists, "tracks": tracks}


def parse_hdrr_track_html(html_text: str, fallback_slug: str = "") -> Optional[Dict[str, Any]]:
    """Parse permitted `/tracks/<slug>` SSR HTML & Schema.org `MusicRecording` JSON-LD."""
    if not html_text or "<html" not in html_text.lower():
        return None
    title = ""
    track_slug = fallback_slug
    isrc = ""
    release_date = ""
    raw_genre_label = ""
    raw_genre_slug = ""
    spotify_url = ""
    artists: List[str] = []
    artist_slugs: List[str] = []

    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html_text, flags=re.S | re.I):
        try:
            ld = json.loads(m.group(1).strip())
        except ValueError:
            continue
        if isinstance(ld, dict) and ld.get("@type") == "MusicRecording":
            title = html_mod.unescape(str(ld.get("name") or title)).strip()
            u = str(ld.get("url") or "")
            if "/tracks/" in u:
                track_slug = u.rstrip("/").rsplit("/tracks/", 1)[-1].split("?")[0] or track_slug
            isrc = str(ld.get("isrcCode") or isrc).strip()
            release_date = str(ld.get("datePublished") or release_date).strip()
            g_val = ld.get("genre")
            if isinstance(g_val, str):
                raw_genre_label = html_mod.unescape(g_val).strip()
            elif isinstance(g_val, list) and g_val:
                raw_genre_label = html_mod.unescape(str(g_val[0])).strip()
            for a_obj in (ld.get("byArtist") or []):
                if isinstance(a_obj, dict):
                    an = html_mod.unescape(str(a_obj.get("name") or "")).strip()
                    au = str(a_obj.get("url") or "")
                    if an:
                        artists.append(an)
                    if "/artists/" in au:
                        artist_slugs.append(au.rstrip("/").rsplit("/artists/", 1)[-1])
            for su in (ld.get("sameAs") or []):
                if isinstance(su, str) and "open.spotify.com/track/" in su:
                    spotify_url = su
                    break

    g_link_m = re.search(r'href="/new-([a-z0-9\-]+)-releases"[^>]*>([^<]+)</a>', html_text, flags=re.I)
    if g_link_m:
        raw_genre_slug = g_link_m.group(1).strip()
        if not raw_genre_label:
            raw_genre_label = html_mod.unescape(g_link_m.group(2)).strip()

    if not isrc:
        isrc_m = re.search(r"<dt>\s*ISRC\s*</dt>\s*<dd[^>]*>\s*([A-Z0-9]{12})\s*</dd>", html_text, flags=re.I)
        if isrc_m:
            isrc = isrc_m.group(1).strip().upper()

    if not title:
        return None

    return {
        "track_slug": track_slug,
        "title": title,
        "artists": artists,
        "artist_slugs": artist_slugs,
        "raw_genre_slug": raw_genre_slug or slugify_hdrr(raw_genre_label),
        "raw_genre_label": raw_genre_label,
        "release_date": release_date,
        "release_year": _parse_year(release_date),
        "isrc": isrc,
        "spotify_url": spotify_url,
    }


class HDRRProvider(BaseProvider):
    """
    Native MusicVault provider for Hard Dance Release Radar (https://www.hdrr.info/).
    - Caches artist-level catalogues in SQLite (`hdrr_artists`, `hdrr_tracks`, `hdrr_artist_tracks`).
    - Supports incremental query/page updates and 100% offline cached lookups (`offline_only=True`).
    - Strictly obeys `robots.txt` (`Disallow: /api/`, `Disallow: /share/`), anti-bot boundaries,
      and configurable polite throttling (`MIN_REQUIRED_DELAY_SECONDS = 2.0`).
    """

    ALLOWS_OPEN_PERSISTENT_CACHE = True
    DEFAULT_CACHE_TTL_SECONDS = 7 * 86400
    MIN_REQUIRED_DELAY_SECONDS = 2.0

    def __init__(
        self,
        contact: str = "",
        max_requests: int = 40,
        db: Optional[Any] = None,
        provider_terms_cleared: bool = False,
        default_delay: float = 2.0,
        cache_ttl_seconds: int = 7 * 86400,
        offline_only: bool = False,
        fetch_track_details_for_isrc: bool = True,
        **kwargs: Any,
    ):
        enforced_delay = max(self.MIN_REQUIRED_DELAY_SECONDS, float(default_delay))
        super().__init__(
            "HDRR",
            default_delay=enforced_delay,
            max_requests=max_requests,
            contact=contact,
            db=db,
            provider_terms_cleared=provider_terms_cleared,
            **kwargs,
        )
        self.DEFAULT_CACHE_TTL_SECONDS = max(60, int(cache_ttl_seconds))
        self.offline_only = bool(offline_only)
        self.fetch_track_details_for_isrc = bool(fetch_track_details_for_isrc)

        def _guard_redirect_response(resp: Any, *args: Any, **kw: Any) -> None:
            resp_url = getattr(resp, "url", "")
            if resp_url:
                _assert_allowed_hdrr_url(str(resp_url))

        if hasattr(self, "session") and hasattr(self.session, "hooks"):
            self.session.hooks.setdefault("response", []).append(_guard_redirect_response)

    def _record_cache_hit(self) -> None:
        self.stats["cache_hits"] += 1
        mon = getattr(self, "request_monitor", None)
        if mon is not None:
            mon.count("cache_hits")

    def _fetch_permitted_html(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        allow_404_as_empty: bool = True,
        use_cache: bool = False,
    ) -> ProviderCallOutcome:
        """Fetch a permitted public HDRR HTML route while hard-blocking `/api/`, `/share/`, and anti-bot challenges."""
        clean_path = _assert_allowed_hdrr_url(path)

        if self.offline_only:
            return ProviderCallOutcome(
                provider=self.name,
                status=ProviderStatus.EMPTY,
                from_cache=True,
                request_made=False,
            )

        url = f"{HDRR_BASE_URL}{clean_path}"
        out = self.request_detailed(
            url,
            params=params,
            headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"},
            use_cache=use_cache,
            allow_404_as_empty=allow_404_as_empty,
            expect_json=False,
        )
        if out.status == ProviderStatus.OK and isinstance(out.data, str):
            snippet = out.data[:8192]
            if _ANTI_BOT_GATE_RE.search(snippet):
                self.hard_blocked = True
                self.hard_block_status = ProviderStatus.AUTH_ERROR
                self.hard_block_kind = "anti_bot_challenge_detected"
                self.hard_block_reason = "HDRR anti-bot/challenge gate detected; halting requests per compliance policy"
                self._persist_operational_state(
                    cooldown_until_utc="",
                    last_status=ProviderStatus.AUTH_ERROR.value,
                    last_http_status=out.http_status,
                    error_kind="anti_bot_challenge_detected",
                    reason=self.hard_block_reason,
                )
                blocked_out = ProviderCallOutcome(
                    provider=self.name,
                    status=ProviderStatus.AUTH_ERROR,
                    http_status=out.http_status,
                    error=self.hard_block_reason,
                    error_kind="anti_bot_challenge_detected",
                    hard_blocked=True,
                    request_made=out.request_made,
                )
                self.last_outcome = blocked_out
                return blocked_out
        return out

    # --- Persistent SQLite Artist & Track Catalogue Helpers ---

    def _is_fresh(self, fetched_utc: str, ttl_seconds: int) -> bool:
        if self.offline_only:
            return True
        if not fetched_utc:
            return False
        try:
            dt = datetime.fromisoformat(fetched_utc.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (self._now_utc_normalized() - dt).total_seconds() < max(60, int(ttl_seconds))
        except ValueError:
            return False

    def _has_fresh_negative(self, lookup_key: str) -> bool:
        if self.db is None:
            return False
        with closing(self.db._get_connection()) as conn:
            row = conn.execute(
                "SELECT fetched_utc, ttl_seconds FROM hdrr_negative_lookups WHERE lookup_key = ?",
                (lookup_key,),
            ).fetchone()
        if not row:
            return False
        return self._is_fresh(row["fetched_utc"], row["ttl_seconds"])

    def _save_negative(self, lookup_key: str, ttl_seconds: int = 3 * 86400) -> None:
        if self.db is None:
            return
        with closing(self.db._get_connection()) as conn, conn:
            conn.execute(
                """
                INSERT INTO hdrr_negative_lookups(lookup_key, fetched_utc, ttl_seconds)
                VALUES (?, ?, ?)
                ON CONFLICT(lookup_key) DO UPDATE SET
                    fetched_utc=excluded.fetched_utc, ttl_seconds=excluded.ttl_seconds
                """,
                (lookup_key, self._now_utc_normalized().isoformat(), int(ttl_seconds)),
            )

    def _upsert_catalogue(self, parsed_artist: Dict[str, Any], mark_complete: bool = False) -> None:
        if self.db is None:
            return
        now_iso = self._now_utc_normalized().isoformat()
        a_slug = parsed_artist["artist_slug"]
        a_name = parsed_artist["name"]
        items = parsed_artist.get("items") or []
        total = int(parsed_artist.get("total_tracks") or len(items))
        complete = int(mark_complete or (len(items) >= total and parsed_artist.get("page", 1) == 1))

        with closing(self.db._get_connection()) as conn, conn:
            prev = conn.execute(
                "SELECT catalogue_complete, pages_cached FROM hdrr_artists WHERE artist_slug = ?",
                (a_slug,),
            ).fetchone()
            merged_complete = 1 if (complete or (prev and prev["catalogue_complete"])) else 0
            pages_cached = max(1, int(prev["pages_cached"]) if prev else 1)
            conn.execute(
                """
                INSERT INTO hdrr_artists(
                    artist_slug, name, norm_name, genres_json, spotify_url, followers,
                    tracked_releases, total_tracks, pages_cached, catalogue_complete, fetched_utc, ttl_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artist_slug) DO UPDATE SET
                    name=excluded.name,
                    norm_name=excluded.norm_name,
                    genres_json=CASE WHEN excluded.genres_json!='[]' THEN excluded.genres_json ELSE hdrr_artists.genres_json END,
                    spotify_url=CASE WHEN excluded.spotify_url!='' THEN excluded.spotify_url ELSE hdrr_artists.spotify_url END,
                    followers=CASE WHEN excluded.followers>0 THEN excluded.followers ELSE hdrr_artists.followers END,
                    tracked_releases=CASE WHEN excluded.tracked_releases>0 THEN excluded.tracked_releases ELSE hdrr_artists.tracked_releases END,
                    total_tracks=CASE WHEN excluded.total_tracks>0 THEN excluded.total_tracks ELSE hdrr_artists.total_tracks END,
                    pages_cached=excluded.pages_cached,
                    catalogue_complete=excluded.catalogue_complete,
                    fetched_utc=excluded.fetched_utc,
                    ttl_seconds=excluded.ttl_seconds
                """,
                (
                    a_slug,
                    a_name,
                    norm(a_name),
                    json.dumps(parsed_artist.get("genres") or []),
                    parsed_artist.get("spotify_url") or "",
                    int(parsed_artist.get("followers") or 0),
                    int(parsed_artist.get("tracked_releases") or total),
                    total,
                    pages_cached,
                    merged_complete,
                    now_iso,
                    self.DEFAULT_CACHE_TTL_SECONDS,
                ),
            )
            for item in items:
                self._upsert_track_conn(conn, item, now_iso)
                conn.execute(
                    "INSERT OR IGNORE INTO hdrr_artist_tracks(artist_slug, track_slug) VALUES (?, ?)",
                    (a_slug, item["track_slug"]),
                )
                for co_slug in (item.get("artist_slugs") or []):
                    if co_slug:
                        conn.execute(
                            "INSERT OR IGNORE INTO hdrr_artist_tracks(artist_slug, track_slug) VALUES (?, ?)",
                            (co_slug, item["track_slug"]),
                        )

    @staticmethod
    def _upsert_track_conn(conn: Any, item: Dict[str, Any], now_iso: str) -> None:
        t_slug = item["track_slug"]
        t_title = item["title"]
        base_t, _, _ = extract_version_tokens(t_title)
        det = 1 if item.get("detail_fetched") else 0
        conn.execute(
            """
            INSERT INTO hdrr_tracks(
                track_slug, title, norm_title, base_title, artists_json, artist_slugs_json,
                raw_genre_slug, raw_genre_label, release_date, release_year, popularity,
                isrc, spotify_url, detail_fetched, updated_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(track_slug) DO UPDATE SET
                title=excluded.title,
                norm_title=excluded.norm_title,
                base_title=excluded.base_title,
                artists_json=CASE WHEN excluded.artists_json!='[]' THEN excluded.artists_json ELSE hdrr_tracks.artists_json END,
                artist_slugs_json=CASE WHEN excluded.artist_slugs_json!='[]' THEN excluded.artist_slugs_json ELSE hdrr_tracks.artist_slugs_json END,
                raw_genre_slug=CASE WHEN excluded.raw_genre_slug!='' THEN excluded.raw_genre_slug ELSE hdrr_tracks.raw_genre_slug END,
                raw_genre_label=CASE WHEN excluded.raw_genre_label!='' THEN excluded.raw_genre_label ELSE hdrr_tracks.raw_genre_label END,
                release_date=CASE WHEN excluded.release_date!='' THEN excluded.release_date ELSE hdrr_tracks.release_date END,
                release_year=COALESCE(excluded.release_year, hdrr_tracks.release_year),
                popularity=COALESCE(excluded.popularity, hdrr_tracks.popularity),
                isrc=CASE WHEN excluded.isrc!='' THEN excluded.isrc ELSE hdrr_tracks.isrc END,
                spotify_url=CASE WHEN excluded.spotify_url!='' THEN excluded.spotify_url ELSE hdrr_tracks.spotify_url END,
                detail_fetched=MAX(hdrr_tracks.detail_fetched, excluded.detail_fetched),
                updated_utc=excluded.updated_utc
            """,
            (
                t_slug,
                t_title,
                norm(t_title),
                base_t or norm(t_title),
                json.dumps(item.get("artists") or []),
                json.dumps(item.get("artist_slugs") or []),
                item.get("raw_genre_slug") or "",
                item.get("raw_genre_label") or "",
                item.get("release_date") or "",
                item.get("release_year"),
                item.get("popularity"),
                item.get("isrc") or "",
                item.get("spotify_url") or "",
                det,
                now_iso,
            ),
        )

    def _get_cached_artist_by_name(self, artist_name: str) -> Optional[Dict[str, Any]]:
        if self.db is None:
            return None
        n_art = norm(artist_name)
        if not n_art:
            return None
        with closing(self.db._get_connection()) as conn:
            row = conn.execute(
                "SELECT * FROM hdrr_artists WHERE norm_name = ? ORDER BY tracked_releases DESC LIMIT 1",
                (n_art,),
            ).fetchone()
        if not row:
            return None
        if not self._is_fresh(row["fetched_utc"], row["ttl_seconds"]):
            return None
        return dict(row)

    def _get_cached_artist_by_slug(self, artist_slug: str) -> Optional[Dict[str, Any]]:
        if self.db is None or not artist_slug:
            return None
        with closing(self.db._get_connection()) as conn:
            row = conn.execute(
                "SELECT * FROM hdrr_artists WHERE artist_slug = ? LIMIT 1",
                (artist_slug,),
            ).fetchone()
        if not row:
            return None
        if not self._is_fresh(row["fetched_utc"], row["ttl_seconds"]):
            return None
        return dict(row)

    @staticmethod
    def _parsed_artist_to_inline_row(parsed: Dict[str, Any]) -> Dict[str, Any]:
        items = list(parsed.get("items") or [])
        total = int(parsed.get("total_tracks") or len(items))
        return {
            "artist_slug": parsed["artist_slug"],
            "name": parsed["name"],
            "norm_name": norm(parsed["name"]),
            "genres_json": json.dumps(parsed.get("genres") or []),
            "spotify_url": parsed.get("spotify_url") or "",
            "followers": int(parsed.get("followers") or 0),
            "tracked_releases": int(parsed.get("tracked_releases") or total),
            "total_tracks": total,
            "catalogue_complete": int(len(items) >= total and parsed.get("page", 1) == 1),
            "_inline_items": items,
        }

    def _get_cached_tracks_for_artist(self, artist_slug: str) -> List[Dict[str, Any]]:
        if self.db is None or not artist_slug:
            return []
        with closing(self.db._get_connection()) as conn:
            rows = conn.execute(
                """
                SELECT t.* FROM hdrr_tracks t
                JOIN hdrr_artist_tracks at ON at.track_slug = t.track_slug
                WHERE at.artist_slug = ?
                ORDER BY t.release_date DESC, t.track_slug ASC
                """,
                (artist_slug,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["artists"] = json.loads(d.pop("artists_json", "[]") or "[]")
            d["artist_slugs"] = json.loads(d.pop("artist_slugs_json", "[]") or "[]")
            out.append(d)
        return out

    # --- Matching & Candidate Construction ---

    @staticmethod
    def _match_track_item(
        query_artist: str,
        query_title: str,
        item: Dict[str, Any],
    ) -> Optional[str]:
        """
        Evaluate whether an HDRR track item matches `(query_artist, query_title)`:
        - Enforces `versions_compatible()` across original, remix, named DJ edit, and extended mixes.
        - Requires at least one overlapping artist (accounting for remixers credited either in
          `item["artists"]` or in the remix title).
        - Returns `'exact'` on normalized base-title equality, `'fuzzy_conservative'` on >= 0.90
          similarity (or festival anthem subtitle strip) when editions & artists match, else `None`.
        """
        cand_title = str(item.get("title") or "").strip()
        cand_artists = list(item.get("artists") or [])
        if not cand_title or not query_title or not query_artist:
            return None

        if not versions_compatible(query_title, cand_title):
            return None

        q_base, q_vers, q_rem = extract_version_tokens(query_title)
        c_base, c_vers, c_rem = extract_version_tokens(cand_title)

        q_art_set = set(parse_artists(query_artist))
        if q_rem:
            q_art_set.add(q_rem)
        c_art_set: Set[str] = set()
        for ca in cand_artists:
            c_art_set.update(parse_artists(ca))
        if c_rem:
            c_art_set.add(c_rem)

        if not (q_art_set & c_art_set):
            return None

        if norm(query_title) == norm(cand_title) or (q_base and q_base == c_base):
            return "exact"

        # Conservative fuzzy match: strip festival anthem subtitles from raw titles or check >= 0.92 base similarity
        q_stripped = _strip_benign_subtitle(query_title)
        c_stripped = _strip_benign_subtitle(cand_title)
        if q_stripped and q_stripped == c_stripped and len(q_stripped) >= 4:
            return "fuzzy_conservative"

        if q_base and c_base and min(len(q_base), len(c_base)) >= 6:
            ratio = difflib.SequenceMatcher(None, q_base, c_base).ratio()
            if ratio >= 0.92 and q_base[0] == c_base[0]:
                return "fuzzy_conservative"

        return None

    def _enrich_track_detail_if_needed(
        self, item: Dict[str, Any], local_isrc: Optional[str]
    ) -> Tuple[Dict[str, Any], Optional[ProviderCallOutcome]]:
        """Optionally fetch `/tracks/<slug>` once (persisted in SQLite) to obtain ISRC and Spotify track URL."""
        if not self.fetch_track_details_for_isrc or self.offline_only:
            return item, None
        if item.get("detail_fetched") or item.get("isrc"):
            return item, None
        # Only spend an extra request when track matched AND we haven't hit our cap
        if not self.can_request():
            return item, None

        t_slug = item["track_slug"]
        det_out = self._fetch_permitted_html(f"/tracks/{urllib.parse.quote(t_slug, safe='')}", allow_404_as_empty=True)
        if det_out.status != ProviderStatus.OK or not isinstance(det_out.data, str):
            return item, (det_out if det_out.status != ProviderStatus.EMPTY else None)

        parsed_det = parse_hdrr_track_html(det_out.data, fallback_slug=t_slug)
        if not parsed_det:
            return item, None

        merged = {
            **item,
            "isrc": parsed_det.get("isrc") or item.get("isrc") or "",
            "spotify_url": parsed_det.get("spotify_url") or item.get("spotify_url") or "",
            "release_date": parsed_det.get("release_date") or item.get("release_date") or "",
            "release_year": parsed_det.get("release_year") or item.get("release_year"),
            "raw_genre_slug": parsed_det.get("raw_genre_slug") or item.get("raw_genre_slug") or "",
            "raw_genre_label": parsed_det.get("raw_genre_label") or item.get("raw_genre_label") or "",
            "detail_fetched": 1,
        }
        if self.db is not None:
            now_iso = self._now_utc_normalized().isoformat()
            with closing(self.db._get_connection()) as conn, conn:
                self._upsert_track_conn(conn, merged, now_iso)
        return merged, det_out

    def _build_candidate(
        self,
        item: Dict[str, Any],
        match_kind: str,
        artist_row: Optional[Dict[str, Any]] = None,
    ) -> Candidate:
        raw_slug = str(item.get("raw_genre_slug") or "").strip()
        raw_label = str(item.get("raw_genre_label") or "").strip()
        norm_parent, norm_sub = map_hdrr_genre(raw_slug, raw_label)
        norm_genres = [g for g in (norm_parent, norm_sub) if g]
        raw_sources = [raw_label] if raw_label else ([raw_slug] if raw_slug else [])

        _, v_tokens, remixer = extract_version_tokens(item["title"])
        artists_list = list(item.get("artists") or [])
        artist_genres = json.loads(artist_row["genres_json"]) if (artist_row and artist_row.get("genres_json")) else []

        return Candidate(
            provider="HDRR",
            title=item["title"],
            artists=artists_list,
            duration=None,  # HDRR does not publish track durations
            isrc=item.get("isrc") or None,
            genre=norm_genres,
            labels=[],
            url=f"{HDRR_BASE_URL}/tracks/{item['track_slug']}",
            verification="Text only",
            match_confidence=match_kind,
            source_family="hdrr",
            raw_source_genre=raw_sources,
            release_year=item.get("release_year"),
            genre_evidence_kind="artist_derived",
            source_metadata={
                "track_slug": item["track_slug"],
                "artist_slugs": list(item.get("artist_slugs") or []),
                "raw_genre_slug": raw_slug,
                "raw_genre_label": raw_label,
                "normalized_parent_genre": norm_parent,
                "normalized_subgenre": norm_sub,
                "artist_source_genres": artist_genres,
                "release_date": item.get("release_date") or "",
                "popularity": item.get("popularity"),
                "spotify_url": item.get("spotify_url") or "",
                "is_collaboration": len(artists_list) >= 2,
                "is_remix": bool(remixer or "remix" in v_tokens),
                "remixer_norm": remixer or "",
                "retrieval_method": "permitted_ssr_html",
            },
        )

    def _resolve_and_cache_artist(
        self, target_artist: str
    ) -> Tuple[Optional[Dict[str, Any]], ProviderCallOutcome]:
        """Resolve an artist's HDRR catalogue via SQLite cache first, then direct `/artists/<slug>`, then `/artists?q=`."""
        cached = self._get_cached_artist_by_name(target_artist)
        if cached is not None:
            self._record_cache_hit()
            return cached, ProviderCallOutcome(provider=self.name, status=ProviderStatus.OK, from_cache=True)

        neg_key = f"artist:{norm(target_artist)}"
        if self._has_fresh_negative(neg_key):
            self._record_cache_hit()
            return None, ProviderCallOutcome(provider=self.name, status=ProviderStatus.EMPTY, from_cache=True)

        if self.offline_only:
            return None, ProviderCallOutcome(provider=self.name, status=ProviderStatus.EMPTY, from_cache=True)

        slug_guess = slugify_hdrr(target_artist)
        if slug_guess:
            out = self._fetch_permitted_html(f"/artists/{urllib.parse.quote(slug_guess, safe='')}", allow_404_as_empty=True)
            if out.status == ProviderStatus.OK and isinstance(out.data, str):
                parsed = parse_hdrr_artist_html(out.data, fallback_slug=slug_guess)
                if parsed and norm(parsed["name"]) == norm(target_artist):
                    if self.db is not None:
                        self._upsert_catalogue(parsed)
                        return self._get_cached_artist_by_slug(parsed["artist_slug"]), out
                    return self._parsed_artist_to_inline_row(parsed), out
            elif out.status not in (ProviderStatus.OK, ProviderStatus.EMPTY):
                return None, out

        # Direct slug 404'd or collided with a homonym (e.g. 'TNT' -> 'tnt-2'): query `/artists?q=<artist>`
        s_out = self._fetch_permitted_html("/artists", params={"q": target_artist.strip()}, allow_404_as_empty=True)
        if s_out.status != ProviderStatus.OK or not isinstance(s_out.data, str):
            if s_out.status == ProviderStatus.EMPTY:
                self._save_negative(neg_key)
            return None, s_out

        hits = parse_hdrr_search_html(s_out.data).get("artists") or []
        exact_slug = next((h["artist_slug"] for h in hits if norm(h["name"]) == norm(target_artist)), "")
        if not exact_slug:
            self._save_negative(neg_key)
            return None, ProviderCallOutcome(
                provider=self.name,
                status=ProviderStatus.EMPTY,
                http_status=s_out.http_status,
                from_cache=s_out.from_cache,
                request_made=s_out.request_made,
            )

        a_out = self._fetch_permitted_html(f"/artists/{urllib.parse.quote(exact_slug, safe='')}", allow_404_as_empty=True)
        if a_out.status == ProviderStatus.OK and isinstance(a_out.data, str):
            parsed = parse_hdrr_artist_html(a_out.data, fallback_slug=exact_slug)
            if parsed:
                if self.db is not None:
                    self._upsert_catalogue(parsed)
                    return self._get_cached_artist_by_slug(parsed["artist_slug"]), a_out
                return self._parsed_artist_to_inline_row(parsed), a_out
        return None, a_out

    def search_detailed(
        self,
        artist: str,
        title: str,
        isrc: Optional[str] = None,
    ) -> Tuple[List[Candidate], ProviderCallOutcome]:
        """
        Look up `(artist, title)` in HDRR with SQLite artist-catalogue caching, incremental
        per-artist track search for large catalogues (`total > 20`), remix/collaboration
        awareness, and optional `/tracks/<slug>` ISRC enrichment.
        """
        clean_artist = (artist or "").strip()
        clean_title = (title or "").strip()
        if not clean_artist or not clean_title:
            return [], ProviderCallOutcome(provider=self.name, status=ProviderStatus.EMPTY)

        # Build ordered artist names to inspect (cached full duo first, remixer, split collaborators, unsplit '&' duo)
        artist_tokens: List[str] = []
        if self._get_cached_artist_by_name(clean_artist) is not None:
            artist_tokens.append(clean_artist)
        remix_m = re.search(
            r"[\(\[\-]\s*([A-Za-z0-9][A-Za-z0-9 &.'+\-]{1,40}?)\s+(?:extended\s+|radio\s+|club\s+)?(?:remix|bootleg|rework|flip)\b",
            clean_title,
            flags=re.I,
        )
        if remix_m:
            rem_raw = remix_m.group(1).strip()
            if rem_raw and norm(rem_raw) not in ("original", "extended", "radio", "club", "official", "vip"):
                if not any(norm(rem_raw) == norm(existing) for existing in artist_tokens):
                    artist_tokens.append(rem_raw)
        for raw_part in re.split(r"\s*(?:,|;|&|\bfeat\.?|\bft\.?|\bvs\.?|\bx\b|\bwith\b)\s*", clean_artist, flags=re.I):
            p = raw_part.strip()
            if p and norm(p) not in ("unknown", "unknownartist", "various", "variousartists", "id"):
                if not any(norm(p) == norm(existing) for existing in artist_tokens):
                    artist_tokens.append(p)
        if re.search(r"(?:&|\band\b)", clean_artist, flags=re.I) and not any(
            norm(clean_artist) == norm(existing) for existing in artist_tokens
        ):
            artist_tokens.append(clean_artist)
        if not artist_tokens:
            return [], ProviderCallOutcome(provider=self.name, status=ProviderStatus.EMPTY)

        matched_candidates: Dict[str, Candidate] = {}
        incomplete_out: Optional[ProviderCallOutcome] = None
        any_request_made = False
        any_cache_hit = False
        last_http: Optional[int] = None

        for target_art in artist_tokens[:4]:
            artist_row, a_out = self._resolve_and_cache_artist(target_art)
            any_request_made = any_request_made or a_out.request_made
            any_cache_hit = any_cache_hit or a_out.from_cache
            last_http = a_out.http_status or last_http
            if a_out.status not in (ProviderStatus.OK, ProviderStatus.EMPTY):
                incomplete_out = a_out
                break
            if not artist_row:
                continue

            a_slug = artist_row["artist_slug"]
            cached_tracks = (
                list(artist_row["_inline_items"])
                if "_inline_items" in artist_row
                else self._get_cached_tracks_for_artist(a_slug)
            )
            local_hits: List[Tuple[Dict[str, Any], str]] = []
            for item in cached_tracks:
                mk = self._match_track_item(clean_artist, clean_title, item)
                if mk:
                    local_hits.append((item, mk))

            # Incremental lookup for prolific artists (>20 releases) whose full catalogue isn't cached yet
            if not local_hits and not artist_row.get("catalogue_complete") and not self.offline_only:
                q_term = re.sub(r"\s*\([^)]*\)|\s*\[[^\]]*\]", "", clean_title).strip() or clean_title
                q_key = f"artist_q:{a_slug}:{norm(q_term)}"
                if self._has_fresh_negative(q_key):
                    self._record_cache_hit()
                    any_cache_hit = True
                else:
                    q_out = self._fetch_permitted_html(
                        f"/artists/{urllib.parse.quote(a_slug, safe='')}",
                        params={"q": q_term},
                        allow_404_as_empty=True,
                    )
                    any_request_made = any_request_made or q_out.request_made
                    any_cache_hit = any_cache_hit or q_out.from_cache
                    last_http = q_out.http_status or last_http
                    if q_out.status == ProviderStatus.OK and isinstance(q_out.data, str):
                        parsed_q = parse_hdrr_artist_html(q_out.data, fallback_slug=a_slug)
                        if parsed_q and parsed_q.get("items"):
                            if self.db is not None:
                                # Persist newly discovered items incrementally without falsely marking full catalogue complete
                                with closing(self.db._get_connection()) as conn, conn:
                                    now_iso = self._now_utc_normalized().isoformat()
                                    for it in parsed_q["items"]:
                                        self._upsert_track_conn(conn, it, now_iso)
                                        conn.execute(
                                            "INSERT OR IGNORE INTO hdrr_artist_tracks(artist_slug, track_slug) VALUES (?, ?)",
                                            (a_slug, it["track_slug"]),
                                        )
                            for it in parsed_q["items"]:
                                mk = self._match_track_item(clean_artist, clean_title, it)
                                if mk:
                                    local_hits.append((it, mk))
                        if not local_hits:
                            self._save_negative(q_key)
                    elif q_out.status not in (ProviderStatus.OK, ProviderStatus.EMPTY):
                        incomplete_out = q_out
                        break

            for item, mk in local_hits:
                enriched_item, det_err = self._enrich_track_detail_if_needed(item, isrc)
                if det_err is not None and det_err.status not in (ProviderStatus.OK, ProviderStatus.EMPTY):
                    if incomplete_out is None:
                        incomplete_out = det_err
                cand = self._build_candidate(enriched_item, mk, artist_row=artist_row)
                matched_candidates[enriched_item["track_slug"]] = cand

            if matched_candidates:
                break

        candidates = list(matched_candidates.values())
        if incomplete_out is not None:
            return candidates, incomplete_out

        status = ProviderStatus.OK if candidates else ProviderStatus.EMPTY
        final_out = ProviderCallOutcome(
            provider=self.name,
            status=status,
            http_status=last_http or (200 if (any_cache_hit or candidates) else None),
            from_cache=bool(any_cache_hit and not any_request_made),
            request_made=any_request_made,
        )
        return candidates, final_out

    def search(self, artist: str, title: str, isrc: Optional[str] = None) -> List[Candidate]:
        cands, _ = self.search_detailed(artist, title, isrc=isrc)
        return cands
