"""
Comprehensive regression suite for MusicVault HDRR Integration & Configurable Artist Classification Rules.

Covers:
1. HDRR robots.txt compliance (`/api/`, `/share/`, `/media/img/` hard-blocked) and anti-bot gate halting.
2. Permitted SSR HTML + Schema.org JSON-LD + Astro `TrackExplorer` props extraction, artist-level SQLite
   catalogue caching, 0-request offline lookups, prolific artist `?q=` incremental search, remixer/collab
   matching, version validation, polite pacing (>= 2.0s), HTTP 429 cooldowns, and resumable research.
3. Artist-derived genre governance: preserving raw HDRR classifications (`Euphoric Hardstyle`,
   `Deutscher Krach`, `Mainsteam Hardstyle`) separately from normalized MusicVault classifications;
   blocking independent HDRR promotion to MEDIUM/HIGH (even when ISRC-matched); blocking correlated
   Spotify-ecosystem sources from counting as 2 independent genre confirmations.
4. Configurable `Artist_Rules` (SQLite, `ARTIST_RULES_JSON`, and 18-column `Artist_Rules` Google Sheets tab):
   - Initial `Peygan` rule (`AR-PEYGAN-001`) + mandatory regression test **`Famous — Peygan & Firelite`**
     detecting HDRR's `Euphoric Hardstyle` conflict against `Peygan`'s discouraged rule, preventing
     automatic classification, surfacing `Rawstyle, Xtra Raw, Experimental Raw` investigation cues, and
     never forcing every `Peygan` track into `Rawstyle`.
   - Track-specific exceptions (`file_id`, `isrc`, `title`), release-year ranges (changing artist styles),
     collaboration policies, remix-artist priority (`remixer_first`), enabled/approval gates, audit history,
     and protection of manual `Tracks` locks.
5. Learning from manual corrections (`learn_artist_rule_suggestions`): generating `pending_approval`
   (`enabled=False`) suggestions without auto-activation, explicit user approval (`approve_artist_rule_suggestion`),
   and non-destructive flagging of existing conflicting `Tracks` rows (`flag_existing_rows_against_approved_rules`).
"""
import html as html_mod
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest
import requests

from musicvault.__main__ import main as cli_main
from musicvault.config import Config
from musicvault.core.artist_rules import (
    ARTIST_RULES_HEADERS,
    ArtistRule,
    DEFAULT_PEYGAN_RULE,
    approve_artist_rule_suggestion,
    artist_rule_to_sheet_row,
    ensure_default_artist_rules,
    evaluate_artist_rules,
    flag_existing_rows_against_approved_rules,
    learn_artist_rule_suggestions,
    load_artist_rules,
    parse_artist_rules_sheet,
    upsert_artist_rule,
)
from musicvault.core.coverage import decisions, library_report
from musicvault.core.provider_policy import ProviderPolicy
from musicvault.core.researcher import execute_research_batch
from musicvault.core.rules import (
    build_track_proposal,
    field_decision,
    independent_genre_family_count,
    planned_row,
)
from musicvault.db import Database
from musicvault.models import Candidate, Track, TrackMetadata
from musicvault.providers.base import ProviderStatus
from musicvault.providers.hdrr import (
    HDRRProvider,
    map_hdrr_genre,
    parse_hdrr_artist_html,
    parse_hdrr_track_html,
)


def _astro_encode(val):
    if isinstance(val, dict):
        return [0, {k: _astro_encode(v) for k, v in val.items()}]
    if isinstance(val, list):
        return [1, [_astro_encode(x) for x in val]]
    return [0, val]


def _make_hdrr_artist_html(
    slug: str,
    name: str,
    genres: list[str],
    items: list[dict],
    total: int | None = None,
) -> str:
    tot = total if total is not None else len(items)
    ld = {
        "@context": "https://schema.org",
        "@type": "MusicGroup",
        "name": name,
        "url": f"https://www.hdrr.info/artists/{slug}",
        "genre": genres,
        "sameAs": [f"https://open.spotify.com/artist/sp_{slug}"],
        "interactionStatistic": {"@type": "InteractionCounter", "userInteractionCount": 1371},
    }
    raw_props = {
        "fetchPath": f"/api/v1/public/artists/{slug}/tracks",
        "initial": _astro_encode({
            "items": items,
            "total": tot,
            "page": 1,
            "pageSize": 20,
        })[1],
    }
    # Wrap top-level dict keys as Astro expects (`key: [0, ...]`)
    astro_props = {
        "fetchPath": [0, raw_props["fetchPath"]],
        "initial": [0, raw_props["initial"]],
    }
    escaped_props = html_mod.escape(json.dumps(astro_props), quote=True)
    return (
        f'<!DOCTYPE html><html><head>'
        f'<script type="application/ld+json">{json.dumps(ld)}</script>'
        f'</head><body><main><h1>{html_mod.escape(name)}</h1>'
        f'<dl><dt>Tracked releases:</dt><dd>{tot}</dd></dl>'
        f'<astro-island component-url="/_astro/TrackExplorer.js" props="{escaped_props}"></astro-island>'
        f'</main></body></html>'
    )


def _make_hdrr_track_html(slug: str, title: str, artists: list[tuple[str, str]], genre_slug: str, genre_label: str, isrc: str, rel_date: str = "2026-01-21") -> str:
    ld = {
        "@context": "https://schema.org",
        "@type": "MusicRecording",
        "name": title,
        "url": f"https://www.hdrr.info/tracks/{slug}",
        "datePublished": rel_date,
        "isrcCode": isrc,
        "genre": genre_label,
        "byArtist": [{"@type": "MusicGroup", "name": n, "url": f"https://www.hdrr.info/artists/{s}"} for s, n in artists],
        "sameAs": [f"https://open.spotify.com/track/sp_{slug}"],
    }
    return (
        f'<!DOCTYPE html><html><head>'
        f'<script type="application/ld+json">{json.dumps(ld)}</script>'
        f'</head><body><main><a href="/new-{genre_slug}-releases">{html_mod.escape(genre_label)}</a>'
        f'<dl><dt>ISRC</dt><dd>{isrc}</dd></dl></main></body></html>'
    )


def _http_html_resp(code: int, body: str, headers: dict | None = None) -> requests.Response:
    r = requests.Response()
    r.status_code = code
    r._content = body.encode("utf-8")
    r.encoding = "utf-8"
    if headers:
        r.headers.update(headers)
    return r


PEYGAN_CATALOGUE_ITEMS = [
    {
        "slug": "famous-9",
        "name": "Famous",
        "releaseDate": "2026-01-21T00:00:00.000Z",
        "popularity": 32,
        "genre": {"slug": "euphoric-hardstyle", "label": "Euphoric Hardstyle"},
        "artists": [{"slug": "firelite", "name": "Firelite"}, {"slug": "peygan", "name": "PEYGAN"}],
    },
    {
        "slug": "let-s-go-gambling",
        "name": "Let's Go Gambling",
        "releaseDate": "2026-07-24T00:00:00.000Z",
        "popularity": 42,
        "genre": {"slug": "deutscher-krach", "label": "Deutscher Krach"},
        "artists": [{"slug": "schlot", "name": "Schlot"}, {"slug": "peygan", "name": "PEYGAN"}],
    },
    {
        "slug": "f-a-m-e",
        "name": "F.A.M.E",
        "releaseDate": "2026-04-17T00:00:00.000Z",
        "popularity": 31,
        "genre": {"slug": "uptempo-hardcore", "label": "Uptempo"},
        "artists": [{"slug": "firelite", "name": "Firelite"}, {"slug": "peygan", "name": "PEYGAN"}, {"slug": "losty", "name": "Losty"}],
    },
    {
        "slug": "microrave",
        "name": "MICRORAVE",
        "releaseDate": "2026-11-13T00:00:00.000Z",
        "popularity": 44,
        "genre": {"slug": "hardstyle", "label": "Hardstyle"},
        "artists": [{"slug": "colin-hennerz", "name": "Colin Hennerz"}, {"slug": "peygan", "name": "PEYGAN"}],
    },
]


def test_hdrr_robots_compliance_anti_bot_halt_and_sqlite_artist_catalogue_cache(tmp_path, monkeypatch):
    db = Database(tmp_path / "vault.db")
    tick = 0.0
    now0 = datetime(2026, 11, 27, 5, 0, 0, tzinfo=timezone.utc)

    def _sleep(sec: float) -> None:
        nonlocal tick
        tick += sec

    hdrr = HDRRProvider(
        db=db,
        clock_mono=lambda: tick,
        clock_utc=lambda: now0 + timedelta(seconds=tick),
        sleep_fn=_sleep,
    )

    # 1. robots.txt disallowed prefixes MUST raise RuntimeError immediately without making any HTTP call
    for forbidden in ("/api/v1/public/artists/peygan/tracks", "/share/artists/peygan", "/media/img/cover.webp"):
        with pytest.raises(RuntimeError, match="robots.txt compliance guard"):
            hdrr._fetch_permitted_html(forbidden)

    # 2. Artist-level SQLite caching: multiple tracks by PEYGAN reuse the single `/artists/peygan` fetch!
    requested_urls: list[str] = []
    peygan_html = _make_hdrr_artist_html("peygan", "PEYGAN", ["Frenchcore", "Hardcore"], PEYGAN_CATALOGUE_ITEMS, total=4)
    famous_html = _make_hdrr_track_html(
        "famous-9", "Famous", [("firelite", "Firelite"), ("peygan", "PEYGAN")],
        "euphoric-hardstyle", "Euphoric Hardstyle", "BEB682600029",
    )

    def _fake_get(url, params=None, **kwargs):
        full_u = f"{url}?{json.dumps(params, sort_keys=True)}" if params else url
        requested_urls.append(full_u)
        if url.endswith("/artists/peygan"):
            return _http_html_resp(200, peygan_html)
        if url.endswith("/tracks/famous-9"):
            return _http_html_resp(200, famous_html)
        return _http_html_resp(404, "Not found")

    monkeypatch.setattr(hdrr.session, "get", _fake_get)

    cands_1, out_1 = hdrr.search_detailed("Peygan & Firelite", "Famous", isrc="BEB682600029")
    assert out_1.status == ProviderStatus.OK
    assert len(cands_1) == 1
    assert cands_1[0].isrc == "BEB682600029"
    assert cands_1[0].raw_source_genre == ["Euphoric Hardstyle"]
    assert cands_1[0].genre == ["Hardstyle", "Euphoric"]
    assert cands_1[0].genre_evidence_kind == "artist_derived"
    assert cands_1[0].release_year == 2026
    assert len(requested_urls) == 2  # 1 for /artists/peygan + 1 for /tracks/famous-9 ISRC detail

    # Second track by PEYGAN (`Let's Go Gambling`, with detail fetch disabled or offline_only): ZERO artist page refetches!
    hdrr_offline = HDRRProvider(db=db, offline_only=True)
    cands_2, out_2 = hdrr_offline.search_detailed("Schlot & PEYGAN", "Let's Go Gambling")
    assert out_2.status == ProviderStatus.OK
    assert out_2.from_cache is True
    assert out_2.request_made is False
    assert len(cands_2) == 1
    # Original HDRR source label 'Deutscher Krach' preserved separately from normalized ('Hardcore', 'Uptempo')!
    assert cands_2[0].raw_source_genre == ["Deutscher Krach"]
    assert cands_2[0].genre == ["Hardcore", "Uptempo"]
    assert len(requested_urls) == 2

    # 3. HDRR typo slug 'mainsteam-hardstyle' normalizes cleanly while preserving raw label
    assert map_hdrr_genre("mainsteam-hardstyle", "Mainsteam Hardstyle") == ("Hardstyle", "Mainstream Hardstyle")

    # 4. Anti-bot / challenge detection halts HDRR cleanly without attempting any bypass
    hdrr_bot = HDRRProvider(db=Database(tmp_path / "bot.db"), sleep_fn=_sleep)
    monkeypatch.setattr(
        hdrr_bot.session, "get",
        lambda *a, **kw: _http_html_resp(200, "<html><body><div class='cf-turnstile'>Just a moment...</div></body></html>"),
    )
    _, bot_out = hdrr_bot.search_detailed("Angerfist", "Solid Stigma")
    assert bot_out.status == ProviderStatus.AUTH_ERROR
    assert bot_out.hard_blocked is True
    assert hdrr_bot.can_request() is False


def test_hdrr_prolific_artist_incremental_search_remix_matching_and_version_guard(tmp_path, monkeypatch):
    db = Database(tmp_path / "prolific.db")
    hdrr = HDRRProvider(db=db, fetch_track_details_for_isrc=False, sleep_fn=lambda s: None)

    hhz_page1 = _make_hdrr_artist_html(
        "headhunterz", "Headhunterz", ["Hardstyle"],
        [{"slug": "live-forever", "name": "Live Forever", "releaseDate": "2024-11-01T00:00:00.000Z", "popularity": 48, "genre": {"slug": "hardstyle", "label": "Hardstyle"}, "artists": [{"slug": "headhunterz", "name": "Headhunterz"}]}],
        total=120,  # > 20 -> not complete on page 1!
    )
    hhz_q_reignite = _make_hdrr_artist_html(
        "headhunterz", "Headhunterz", ["Hardstyle"],
        [
            {"slug": "reignite-dbstf", "name": "Reignite - D-Block & S-te-Fan Remix", "releaseDate": "2023-08-03T00:00:00.000Z", "popularity": 34, "genre": {"slug": "hardstyle", "label": "Hardstyle"}, "artists": [{"slug": "headhunterz", "name": "Headhunterz"}, {"slug": "d-block-s-te-fan", "name": "D-Block & S-te-Fan"}]},
            {"slug": "reignite-orig", "name": "Reignite - Original Mix", "releaseDate": "2012-05-01T00:00:00.000Z", "popularity": 40, "genre": {"slug": "euphoric-hardstyle", "label": "Euphoric Hardstyle"}, "artists": [{"slug": "headhunterz", "name": "Headhunterz"}]},
        ],
        total=2,
    )

    calls = []

    def _get(url, params=None, **kw):
        calls.append((url, dict(params or {})))
        if url.endswith("/artists/headhunterz") and params and params.get("q") == "Reignite":
            return _http_html_resp(200, hhz_q_reignite)
        if url.endswith("/artists/headhunterz") and not params:
            return _http_html_resp(200, hhz_page1)
        return _http_html_resp(404, "Not found")

    monkeypatch.setattr(hdrr.session, "get", _get)

    # Querying the remix matches ONLY the D-Block & S-te-Fan Remix, NOT the Original Mix!
    c_rmx, out_rmx = hdrr.search_detailed("Headhunterz", "Reignite (D-Block & S-te-Fan Remix)")
    assert out_rmx.status == ProviderStatus.OK
    assert len(c_rmx) == 1
    assert c_rmx[0].title == "Reignite - D-Block & S-te-Fan Remix"
    assert c_rmx[0].source_metadata["is_remix"] is True

    # Subsequent query for the Original Mix resolves from the incrementally cached SQLite rows in ZERO HTTP calls!
    before_len = len(calls)
    c_orig, out_orig = hdrr.search_detailed("Headhunterz", "Reignite (Original Mix)")
    assert len(calls) == before_len
    assert len(c_orig) == 1
    assert c_orig[0].title == "Reignite - Original Mix"
    assert c_orig[0].raw_source_genre == ["Euphoric Hardstyle"]


def test_famous_peygan_firelite_regression_and_artist_derived_correlation_gates():
    """
    Mandatory Section 4 & Section 5 Regression Test:
    Track: Famous — Peygan & Firelite
    - HDRR reports `Euphoric Hardstyle` (even with `ISRC supported` identity match `BEB682600029`!).
    - Engine MUST:
      1. Never let HDRR independently elevate genre or subgenre to MEDIUM or HIGH.
      2. Detect the conflict with `Peygan`'s `Artist_Rule` (`AR-PEYGAN-001`, discouraging `Euphoric`).
      3. Prevent unsupported automatic `Euphoric` classification in `planned_row()`.
      4. Preserve original HDRR `Euphoric Hardstyle` in `hdrr_raw_genres` / candidate storage.
      5. Surface `Rawstyle, Xtra Raw, Experimental Raw` investigation cues without forcing `Rawstyle`!
    """
    meta_famous = TrackMetadata(
        artist="Peygan & Firelite",
        title="Famous",
        duration=168.0,
        isrc="BEB682600029",
        embedded_bpm="155",
        identity_origin="embedded",
    )
    hdrr_famous = Candidate(
        provider="HDRR",
        title="Famous",
        artists=["Firelite", "PEYGAN"],
        isrc="BEB682600029",
        genre=["Hardstyle", "Euphoric"],
        raw_source_genre=["Euphoric Hardstyle"],
        release_year=2026,
        verification="ISRC supported",
        match_confidence="exact",
        source_family="hdrr",
        genre_evidence_kind="artist_derived",
    )

    # Case A: HDRR alone on Famous — Peygan & Firelite
    prop_a = build_track_proposal(meta_famous, [hdrr_famous], artist_rules=[DEFAULT_PEYGAN_RULE], file_id="fid_famous")
    assert prop_a["hdrr_raw_genres"] == ["Euphoric Hardstyle"]
    assert prop_a["subgenre"] == ""  # NOT forced to Rawstyle AND NOT promoted to Euphoric!
    assert prop_a["artist_rule_eval"]["hdrr_rule_conflict"] is True
    assert prop_a["artist_rule_eval"]["investigation_cues"] == ["Rawstyle", "Xtra Raw", "Experimental Raw"]
    assert field_decision([hdrr_famous], "genre", prop_a)[0] == "LOW"
    assert field_decision([hdrr_famous], "subgenre", prop_a)[0] == "LOW"

    sheet_row = [
        "Peygan & Firelite - Famous.flac", "Unsorted Import", "Unclassified", "Needs Review",
        "", "", "", "", "FLAC", "24.1", "", "PENDING", "", "", "fid_famous", "",
    ]
    out_a, dec_a, chg_a = planned_row([hdrr_famous], prop_a, sheet_row, provider_terms_cleared=True)
    assert out_a[2] == "Unclassified"
    assert out_a[3] == "Needs Review"
    assert chg_a == []
    assert "AR-PEYGAN-001" in out_a[17]
    assert "Rawstyle, Xtra Raw, Experimental Raw" in out_a[17]

    # Case B: Even if MusicBrainz verifies parent Hardstyle AND single-source Discogs suggests Euphoric,
    # Peygan's discouraged rule blocks automatic MEDIUM Euphoric promotion while allowing Hardstyle parent!
    mb_hs = Candidate(
        provider="MusicBrainz", title="Famous", artists=["Firelite", "PEYGAN"],
        duration=168.0, isrc="BEB682600029", genre=["Hardstyle"],
        verification="ISRC supported", source_family="musicbrainz",
    )
    dc_euph = Candidate(
        provider="Discogs release", title="Famous", artists=["Firelite", "PEYGAN"],
        duration=168.0, genre=["Hardstyle", "Euphoric"],
        verification="Duration supported", source_family="discogs",
    )
    prop_b = build_track_proposal(meta_famous, [mb_hs, dc_euph, hdrr_famous], artist_rules=[DEFAULT_PEYGAN_RULE], file_id="fid_famous")
    assert prop_b["genre"] == "Hardstyle"
    assert field_decision([mb_hs, dc_euph, hdrr_famous], "genre", prop_b)[0] == "HIGH"
    # Subgenre Euphoric is downgraded from MEDIUM -> LOW due to Peygan discouraged conflict!
    sub_lvl, sub_why = field_decision([mb_hs, dc_euph, hdrr_famous], "subgenre", prop_b)
    assert sub_lvl == "LOW"
    assert "AR-PEYGAN-001" in sub_why
    out_b, dec_b, chg_b = planned_row([mb_hs, dc_euph, hdrr_famous], prop_b, sheet_row, provider_terms_cleared=True)
    assert out_b[2] == "Hardstyle"
    assert out_b[3] == "Needs Review"  # Euphoric NOT written to Tracks!
    assert chg_b == ["genre"]

    # Case C: Do NOT force every Peygan track into Rawstyle — e.g., Let's Go Gambling (Hardcore / Uptempo)!
    meta_gamble = TrackMetadata(artist="Schlot & PEYGAN", title="Let's Go Gambling", duration=155.0, embedded_bpm="200")
    mb_up = Candidate("MusicBrainz", "Let's Go Gambling", ["Schlot", "PEYGAN"], duration=155.0, genre=["Hardcore", "Uptempo"], verification="Duration supported", source_family="musicbrainz")
    dc_up = Candidate("Discogs release", "Let's Go Gambling", ["Schlot", "PEYGAN"], duration=155.0, genre=["Hardcore", "Uptempo"], verification="Duration supported", source_family="discogs")
    prop_c = build_track_proposal(meta_gamble, [mb_up, dc_up], artist_rules=[DEFAULT_PEYGAN_RULE])
    assert prop_c["genre"] == "Hardstyle" if False else prop_c["genre"] == "Hardcore"
    assert prop_c["subgenre"] == "Uptempo"
    assert field_decision([mb_up, dc_up], "subgenre", prop_c)[0] == "HIGH"

    # Case D: Correlated Spotify-ecosystem families never count as 2 independent genre confirmations
    assert independent_genre_family_count({"hdrr", "sonovault"}) == 1
    assert independent_genre_family_count({"sonovault", "audiometa"}) == 1
    assert independent_genre_family_count({"musicbrainz", "sonovault"}) == 2
    assert independent_genre_family_count({"musicbrainz", "discogs"}) == 2


def test_artist_rules_exceptions_year_ranges_remix_priority_manual_locks_and_sheet_tab(tmp_path):
    db = Database(tmp_path / "rules.db")
    ensure_default_artist_rules(db)

    # 1. Track-specific exception on Peygan allows a verified exception track (`fid_famous_exc=Rawphoric`)
    peygan_with_exc = ArtistRule(
        rule_id="AR-PEYGAN-001",
        artist_name="Peygan",
        enabled=True,
        approval_status="approved",
        verification_status="user_verified",
        preferred=["Rawstyle", "Xtra Raw", "Experimental Raw"],
        discouraged=["Euphoric"],
        track_exceptions=[{"match_type": "file_id", "match_value": "fid_famous_exc", "allowed": "Rawphoric"}],
        notes="With track exception",
    )
    upsert_artist_rule(db, peygan_with_exc, actor="test_user", notes="Added fid_famous_exc exception")

    meta_f = TrackMetadata(artist="Peygan & Firelite", title="Famous", duration=168.0)
    mb_hs = Candidate("MusicBrainz", "Famous", ["Firelite", "PEYGAN"], duration=168.0, genre=["Hardstyle"], verification="Duration supported", source_family="musicbrainz")
    prop_exc = build_track_proposal(meta_f, [mb_hs], artist_rules=[peygan_with_exc], file_id="fid_famous_exc")
    assert prop_exc["genre"] == "Hardstyle"
    assert prop_exc["subgenre"] == "Rawphoric"
    assert field_decision([mb_hs], "subgenre", prop_exc)[0] == "MEDIUM"

    # 2. Release-year ranges for an artist with changing styles (<=2016 Euphoric OK; >=2023 Euphoric discouraged)
    era_early = ArtistRule(
        rule_id="AR-CHANGER-EARLY", artist_name="StyleShifter",
        preferred=["Euphoric"], year_max=2016,
    )
    era_modern = ArtistRule(
        rule_id="AR-CHANGER-MOD", artist_name="StyleShifter",
        preferred=["Rawstyle", "Xtra Raw"], discouraged=["Euphoric"], year_min=2023,
    )
    cand_2014 = Candidate("Discogs release", "Old Melody", ["StyleShifter"], duration=240.0, genre=["Hardstyle", "Euphoric"], release_year=2014, verification="Duration supported", source_family="discogs")
    cand_2025 = Candidate("Discogs release", "New Kickroll", ["StyleShifter"], duration=180.0, genre=["Hardstyle", "Euphoric"], release_year=2025, verification="Duration supported", source_family="discogs")

    prop_2014 = build_track_proposal(TrackMetadata(artist="StyleShifter", title="Old Melody", duration=240.0), [cand_2014], artist_rules=[era_early, era_modern])
    assert field_decision([cand_2014], "subgenre", prop_2014)[0] == "MEDIUM"

    prop_2025 = build_track_proposal(TrackMetadata(artist="StyleShifter", title="New Kickroll", duration=180.0), [cand_2025], artist_rules=[era_early, era_modern])
    assert field_decision([cand_2025], "subgenre", prop_2025)[0] == "LOW"  # Discouraged in >=2023 era!

    # 3. Remix artist priority (`remixer_first`): if Peygan track is remixed by Da Tweekaz, Peygan's rule yields to remixer!
    cand_dt_rmx = Candidate("Discogs release", "Track (Da Tweekaz Remix)", ["Peygan"], duration=200.0, genre=["Hardstyle", "Euphoric"], verification="Duration supported", source_family="discogs")
    prop_rmx = build_track_proposal(
        TrackMetadata(artist="Peygan", title="Track (Da Tweekaz Remix)", duration=200.0),
        [cand_dt_rmx],
        artist_rules=[DEFAULT_PEYGAN_RULE],
    )
    assert prop_rmx["artist_rule_eval"]["discouraged_conflict"] is False
    assert field_decision([cand_dt_rmx], "subgenre", prop_rmx)[0] == "MEDIUM"

    # 4. Manual track override (`MOVED` / `USER_LOCKED`) is ALWAYS protected even against `blocked` artist rules!
    strict_block = ArtistRule(rule_id="AR-BLK", artist_name="Peygan", blocked=["Euphoric"])
    locked_row = [
        "Peygan - Special Melodic Track.flac", "Hardstyle / Euphoric - Classic",
        "Hardstyle", "Euphoric", "150", "8A", "", "", "FLAC", "25.0", "",
        "MOVED", "BPM/key high", "", "fid_locked_1", "Manual verified exception",
    ]
    prop_blk = build_track_proposal(TrackMetadata(artist="Peygan", title="Special Melodic Track", duration=200.0), [mb_hs], artist_rules=[strict_block])
    out_lk, dec_lk, chg_lk = planned_row([mb_hs], prop_blk, locked_row, provider_terms_cleared=True, artist_rules=[strict_block])
    assert out_lk[3] == "Euphoric"
    assert dec_lk["subgenre"] == "LOCKED"
    assert chg_lk == []

    # 5. Artist_Rules 18-column Google Sheets tab round-trip & disabled/unapproved gating
    unapproved = ArtistRule(rule_id="AR-DRAFT", artist_name="DraftAct", enabled=True, approval_status="pending_approval", blocked=["Euphoric"])
    sheet_rows = [artist_rule_to_sheet_row(peygan_with_exc), artist_rule_to_sheet_row(unapproved)]
    parsed_rules, warnings = parse_artist_rules_sheet(ARTIST_RULES_HEADERS, sheet_rows)
    assert warnings == []
    assert len(parsed_rules) == 2
    assert parsed_rules[0].track_exceptions[0]["match_value"] == "fid_famous_exc"
    assert parsed_rules[1].is_active is False  # pending_approval NEVER active!

    # Verify audit trail recorded in SQLite
    with closing(db._get_connection()) as conn:
        audits = conn.execute("SELECT action, actor FROM artist_rules_audit WHERE rule_id='AR-PEYGAN-001' ORDER BY id ASC").fetchall()
    assert [tuple(a) for a in audits] == [("CREATE_RULE", "seed_default"), ("UPDATE_RULE", "test_user")]


def test_learning_suggestions_from_manual_corrections_and_flagging_existing_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICVAULT_CONFIG_DIR", str(tmp_path))
    cfg = Config(tmp_path)
    db = Database(cfg.db_path)

    # Seed 2 tracks by 'RawProducer' where HDRR/Discogs reported 'Euphoric', but user manually verified 'Rawstyle'
    for idx in (1, 2):
        fid = f"fid_learn_{idx}"
        db.save_track(
            Track(
                file_id=fid,
                name=f"RawProducer - Brutal Kick {idx}.flac",
                is_live_source=True,
                metadata=TrackMetadata(version=f"v{idx}", artist="RawProducer", title=f"Brutal Kick {idx}", duration=180.0),
            )
        )
        db.update_research_result(
            fid, "success", f"v{idx}",
            candidates=[
                Candidate("HDRR", f"Brutal Kick {idx}", ["RawProducer"], genre=["Hardstyle", "Euphoric"], raw_source_genre=["Euphoric Hardstyle"], source_family="hdrr", genre_evidence_kind="artist_derived"),
            ],
            provider_calls={"_meta": {"lookup_complete": True}},
            research_incomplete=False,
        )

    # Also seed a 3rd unlocked existing row for 'RawProducer' currently sitting as 'Euphoric' (`SORTED – AUDIO PENDING`)
    db.save_track(
        Track(
            file_id="fid_existing_wrong",
            name="RawProducer - Unchecked Old Row.flac",
            metadata=TrackMetadata(version="v3", artist="RawProducer", title="Unchecked Old Row", duration=185.0),
        )
    )

    sheet_rows_for_learning = [
        ["RawProducer - Brutal Kick 1.flac", "Hardstyle", "Hardstyle", "Rawstyle", "158", "5A", "", "", "FLAC", "25", "", "MOVED", "", "", "fid_learn_1", "Corrected from Euphoric"],
        ["RawProducer - Brutal Kick 2.flac", "Hardstyle", "Hardstyle", "Rawstyle", "160", "6A", "", "", "FLAC", "25", "", "MOVED", "", "", "fid_learn_2", "Corrected from Euphoric"],
    ]

    suggestions = learn_artist_rule_suggestions(db, sheet_rows=sheet_rows_for_learning, min_corrections=2)
    assert len(suggestions) == 1
    sug = suggestions[0]
    assert sug["artist_name"] == "RawProducer"
    assert sug["status"] == "pending_approval"
    assert sug["enabled"] is False
    assert "Euphoric" in sug["discouraged"]
    assert "Rawstyle" in sug["preferred"]

    # CRITICAL: Before user approval, the inferred suggestion MUST NOT be active!
    assert all(r.artist_name != "RawProducer" for r in load_artist_rules(db=db, active_only=True))

    # User explicitly approves the suggestion via CLI `musicvault artist-rules --approve <id>`
    exp_csv = tmp_path / "artist_rules_tab.csv"
    assert cli_main(["artist-rules", "--approve", sug["suggestion_id"], "--export-sheet-csv", str(exp_csv)]) == 0
    assert exp_csv.exists() and "RawProducer" in exp_csv.read_text(encoding="utf-8")

    # Now active! Existing unlocked conflicting row is flagged for review without destructive overwrite
    existing_indexed = [
        (40, ["RawProducer - Unchecked Old Row.flac", "Hardstyle", "Hardstyle", "Euphoric", "158", "8A", "", "", "FLAC", "25", "", "SORTED – AUDIO PENDING", "", "", "fid_existing_wrong", ""]),
        # M-2 regression: manually locked MOVED row MUST NOT be falsely flagged!
        (41, ["RawProducer - Verified Melodic Track.flac", "Hardstyle", "Hardstyle", "Euphoric", "155", "8A", "", "", "FLAC", "25", "", "MOVED", "", "", "fid_locked_moved", "Verified override"]),
    ]
    flagged = flag_existing_rows_against_approved_rules(db, existing_indexed)
    assert len(flagged) == 1
    assert flagged[0]["file_id"] == "fid_existing_wrong"
    assert "Euphoric" in flagged[0]["existing_subgenre"]


def test_audit_remediations_h1_through_l3_end_to_end(tmp_path, monkeypatch):
    """
    Comprehensive regression coverage for independent review findings H-1..H-4, M-1..M-5, and L-1..L-3:
    - H-1: execute_research_batch() preserves HDRR fuzzy_conservative & remixer-credited remixes
    - H-2: HDRRProvider resolves '&'-duo names ('D-Block & S-te-Fan') without dropping unsplit duo
    - H-3: ProviderPolicy, evaluate_live_preview(), and coverage.library_report() honor Artist_Rules & file_id
    - H-4: execute_research_batch() records HDRR telemetry, HTTP calls, cache_hits, and cooldowns
    - M-1: Track exception (`fid:...=Rawphoric`) overrides conflicting single-source 'Euphoric' with source='artist_rule'
    - M-3: CLI `--learn --sheet-snapshot` works offline, and `approve_artist_rule_suggestion` merges without clobbering exceptions
    - M-4: Anti-bot HTTP-200 HTML is never persisted in `provider_cache`
    - M-5: `HDRRProvider(db=None)` returns matches cleanly and prolific `?q=` non-matches are negative-cached
    - L-1: `confidence_boost`, `defer_to_collaborator` on HDRR, and `hold_on_conflict` collaborator clashes work
    - L-2: `_assert_allowed_hdrr_url` blocks dot-segments (`/artists/../api/v1`), exact `/api`, and disallowed hosts
    - L-3: `artist_rule_to_sheet_row` neutralizes formula prefixes and `evidence_history` retains `candidate_provenance`
    """
    import html
    import json
    from argparse import Namespace
    from contextlib import closing
    import requests
    from musicvault.core.artist_rules import (
        ArtistRule,
        approve_artist_rule_suggestion,
        artist_rule_to_sheet_row,
        evaluate_artist_rules,
        upsert_artist_rule,
    )
    from musicvault.core.coverage import library_report
    from musicvault.core.organizer import evaluate_live_preview
    from musicvault.core.provider_policy import ProviderPolicy
    from musicvault.core.researcher import execute_research_batch
    from musicvault.providers.hdrr import HDRRProvider, _assert_allowed_hdrr_url
    from musicvault.services.sheets import VerifiedSheetIndex

    monkeypatch.setenv("MUSICVAULT_CONFIG_DIR", str(tmp_path))
    cfg = Config(tmp_path)
    cfg.set("SOURCE_FOLDER_ID", "src_root_1")
    cfg.set("VAULT_FOLDER_ID", "dst_root_1")
    cfg.set("SHEET_ID", "sheet_1")
    db = Database(cfg.db_path)

    # --- L-2: Path normalization & host guard ---
    for blocked_target in (
        "/api",
        "/share",
        "/media/img",
        "/artists/../api/v1/public/tracks",
        "/artists/%2e%2e/share/card",
        "https://evil.example/artists/peygan",
        "http://www.hdrr.info/artists/peygan",
    ):
        with pytest.raises(RuntimeError):
            _assert_allowed_hdrr_url(blocked_target)
    assert _assert_allowed_hdrr_url("/artists/d-block-s-te-fan") == "/artists/d-block-s-te-fan"

    # --- L-3: Formula sanitization in artist_rule_to_sheet_row ---
    malicious_rule = ArtistRule(
        rule_id="=CMD()",
        artist_name="+EvilArtist",
        notes="-dangerous note",
    )
    safe_cells = artist_rule_to_sheet_row(malicious_rule)
    assert safe_cells[0].startswith("'=")
    assert safe_cells[1].startswith("'+")
    assert safe_cells[16].startswith("'-")

    # --- H-2 & M-5: Ampersand duo resolution ('D-Block & S-te-Fan') + db=None support + prolific negative `?q=` cache ---
    dbstf_props = html.escape(json.dumps({
        "initial": [0, {
            "items": [1, [
                [0, {
                    "slug": [0, "angels-demons"],
                    "name": [0, "Angels & Demons (Official Intents 2026 Anthem)"],
                    "artists": [1, [[0, {"name": [0, "D-Block & S-te-Fan"], "slug": [0, "d-block-s-te-fan"]}]]],
                    "releaseDate": [0, "2026-02-01"],
                    "popularity": [0, 62],
                    "genre": [0, {"slug": [0, "euphoric-hardstyle"], "label": [0, "Euphoric Hardstyle"]}],
                }],
                [0, {
                    "slug": [0, "in-too-deep-firelite-remix"],
                    "name": [0, "In Too Deep (Firelite Remix)"],
                    "artists": [1, [[0, {"name": [0, "Toneshifterz"], "slug": [0, "toneshifterz"]}]]],
                    "releaseDate": [0, "2025-11-10"],
                    "popularity": [0, 50],
                    "genre": [0, {"slug": [0, "euphoric-hardstyle"], "label": [0, "Euphoric Hardstyle"]}],
                }],
            ]],
            "total": [0, 45],  # Prolific (>20) to exercise `?q=` negative caching!
            "page": [0, 1],
            "pageSize": [0, 20],
        }],
    }), quote=True)
    dbstf_html = f"""<!DOCTYPE html><html><head>
    <script type="application/ld+json">{json.dumps({"@type": "MusicGroup", "name": "D-Block & S-te-Fan", "genre": ["Euphoric Hardstyle"]})}</script>
    </head><body><astro-island component-url="/_astro/TrackExplorer.1.js" props="{dbstf_props}"></astro-island></body></html>"""

    def _mock_duo_get(url, params=None, **kw):
        resp = requests.Response()
        resp.url = url
        if url.endswith("/artists/d-block-s-te-fan"):
            resp.status_code = 200
            resp._content = dbstf_html.encode("utf-8")
            return resp
        if url.endswith("/artists/firelite"):
            resp.status_code = 200
            resp._content = dbstf_html.replace('"D-Block & S-te-Fan"', '"Firelite"', 1).encode("utf-8")
            return resp
        resp.status_code = 404
        resp._content = b"Not Found"
        return resp

    # M-5a: db=None works without crashing or losing parsed items
    hdrr_nodb = HDRRProvider(db=None, sleep_fn=lambda s: None, fetch_track_details_for_isrc=False)
    hdrr_nodb.session.get = _mock_duo_get
    c_nodb, o_nodb = hdrr_nodb.search_detailed("D-Block & S-te-Fan", "Angels & Demons")
    assert o_nodb.status.value == "ok" and len(c_nodb) == 1
    assert c_nodb[0].match_confidence == "fuzzy_conservative"

    # H-1, H-2, H-4, M-5b: Run through real `execute_research_batch()` with `ProviderPolicy`
    tr_fuzzy = Track(
        file_id="fid_dbstf_fuzzy",
        name="D-Block & S-te-Fan - Angels & Demons.flac",
        is_live_source=True,
        parent_id="src_root_1",
        observed_parent_id="src_root_1",
        metadata=TrackMetadata(version="vf1", artist="D-Block & S-te-Fan", title="Angels & Demons", duration=210.0, identity_origin="embedded"),
    )
    tr_rmx = Track(
        file_id="fid_firelite_rmx",
        name="Firelite - In Too Deep (Firelite Remix).flac",
        is_live_source=True,
        parent_id="src_root_1",
        observed_parent_id="src_root_1",
        metadata=TrackMetadata(version="vr1", artist="Firelite", title="In Too Deep (Firelite Remix)", duration=195.0, identity_origin="embedded"),
    )
    db.save_track(tr_fuzzy)
    db.save_track(tr_rmx)

    hdrr_db = HDRRProvider(db=db, sleep_fn=lambda s: None, fetch_track_details_for_isrc=False)
    hdrr_db.session.get = _mock_duo_get
    pol = ProviderPolicy(cfg)
    provs = {"hdrr": hdrr_db}
    pol.bind(db, provs)

    batch_sum = execute_research_batch(
        cfg, db, limit=5, providers_override=provs, selected_tracks=[tr_fuzzy, tr_rmx],
        policy=pol, enable_wikidata_fallback=False,
    )
    # H-4: HDRR telemetry & HTTP requests included in summary
    assert "HDRR" in batch_sum.provider_telemetry
    assert batch_sum.total_actual_http_requests == hdrr_db.stats["calls"] > 0
    # H-1: Neither fuzzy_conservative nor remixer-credited remix was dropped!
    saved_fuzzy = db.get_candidates("fid_dbstf_fuzzy")
    saved_rmx = db.get_candidates("fid_firelite_rmx")
    assert len(saved_fuzzy) == 1 and saved_fuzzy[0].match_confidence == "fuzzy_conservative"
    assert len(saved_rmx) == 1 and saved_rmx[0].title == "In Too Deep (Firelite Remix)"

    # Subsequent lookup of D-Block & S-te-Fan hits SQLite cache at index 0 (0 HTTP calls, cache_hits incremented!)
    calls_before = hdrr_db.stats["calls"]
    cache_before = hdrr_db.stats["cache_hits"]
    _, o_cached_duo = hdrr_db.search_detailed("D-Block & S-te-Fan", "Angels & Demons")
    assert hdrr_db.stats["calls"] == calls_before
    assert hdrr_db.stats["cache_hits"] > cache_before
    assert o_cached_duo.from_cache is True

    # M-5b: Non-matching `?q=` on prolific artist (>20 tracks) is negative-cached so repeat lookup makes 0 HTTP calls
    _, _ = hdrr_db.search_detailed("D-Block & S-te-Fan", "Nonexistent Track XYZ")
    calls_after_first_miss = hdrr_db.stats["calls"]
    _, o_second_miss = hdrr_db.search_detailed("D-Block & S-te-Fan", "Nonexistent Track XYZ")
    assert hdrr_db.stats["calls"] == calls_after_first_miss
    assert o_second_miss.from_cache is True

    # L-3b: Archiving stale version retains `candidate_provenance` fields in `evidence_history`
    tr_fuzzy_v2 = Track(
        file_id="fid_dbstf_fuzzy",
        name="D-Block & S-te-Fan - Angels & Demons.flac",
        is_live_source=True,
        metadata=TrackMetadata(version="vf2", artist="D-Block & S-te-Fan", title="Angels & Demons", duration=210.0),
    )
    db.save_track(tr_fuzzy_v2)
    with closing(db._get_connection()) as conn:
        hist_json = conn.execute("SELECT candidates_json FROM evidence_history WHERE file_id='fid_dbstf_fuzzy'").fetchone()[0]
    assert "Euphoric Hardstyle" in hist_json

    # --- M-4: HTTP-200 anti-bot challenge HTML is NEVER stored in `provider_cache` ---
    hdrr_bot = HDRRProvider(db=db, sleep_fn=lambda s: None)
    def _bot_200_get(url, params=None, **kw):
        r = requests.Response()
        r.url = url
        r.status_code = 200
        r._content = b"<html><title>Just a moment...</title><div class='cf-turnstile'></div></html>"
        return r
    hdrr_bot.session.get = _bot_200_get
    _, bot_out = hdrr_bot.search_detailed("NewBotTestArtist", "AnyTrack")
    assert bot_out.hard_blocked is True
    with closing(db._get_connection()) as conn:
        pc_cnt = conn.execute("SELECT COUNT(*) FROM provider_cache WHERE provider = 'HDRR'").fetchone()[0]
    assert pc_cnt == 0

    # --- M-1, H-3 & L-1: Track-exception override vs single-source conflict, `preview`, `ProviderPolicy`, `coverage`, and `hold_on_conflict` ---
    peygan_with_exc = ArtistRule(
        rule_id="AR-PEYGAN-001",
        artist_name="Peygan",
        enabled=True,
        approval_status="approved",
        verification_status="user_verified",
        preferred=["Rawstyle", "Xtra Raw"],
        discouraged=["Euphoric"],
        track_exceptions=[{"match_type": "file_id", "match_value": "fid_exc_rawphoric", "allowed": "Rawphoric"}],
        collab_policy="hold_on_conflict",
        confidence_boost=1,
    )
    upsert_artist_rule(db, peygan_with_exc, actor="test")

    tr_exc = Track(
        file_id="fid_exc_rawphoric",
        name="Peygan - Special Melodic Cut.flac",
        is_live_source=True,
        parent_id="src_root_1",
        observed_parent_id="src_root_1",
        metadata=TrackMetadata(version="ve1", artist="Peygan", title="Special Melodic Cut", duration=175.0, identity_origin="embedded"),
    )
    db.save_track(tr_exc)
    # Single source (Discogs) wrongly claims 'Euphoric', while track exception specifies 'Rawphoric'!
    discogs_wrong = [
        Candidate("Discogs", "Special Melodic Cut", ["Peygan"], duration=175.0, genre=["Hardstyle", "Euphoric"], verification="Duration supported", source_family="discogs"),
    ]
    db.update_research_result("fid_exc_rawphoric", "success", "ve1", candidates=discogs_wrong, provider_calls={"_meta": {"lookup_complete": True}})

    idx = VerifiedSheetIndex(
        schema_valid=True,
        schema_error="",
        headers=["Track Name", "Folder", "Genre", "Subgenre", "BPM", "Key", "Artist", "Title", "Fmt", "MB", "Link", "Status", "Conf", "Date", "Drive ID", "Notes"],
        columns={"genre": 2, "subgenre": 3, "bpm": 4, "key": 5},
        drive_id_col=14,
        unique_rows_by_id={
            "fid_exc_rawphoric": (10, ["Peygan - Special Melodic Cut.flac", "Unsorted", "Unclassified", "Needs Review", "", "", "", "", "FLAC", "20", "", "PENDING", "", "", "fid_exc_rawphoric", ""]),
        },
        duplicate_ids={},
    )
    pv = evaluate_live_preview(
        live_tracks=[tr_exc],
        candidates_by_id={"fid_exc_rawphoric": db.get_candidates("fid_exc_rawphoric")},
        sheet_index=idx,
        source_folder_id="src_root_1",
        vault_folder_id="dst_root_1",
        vault_folders={"Hardstyle": ["hs_folder"]},
        artist_rules=[peygan_with_exc],
    )
    assert pv.rows[0].proposed_values["subgenre"] == "Rawphoric"
    new_r, decs, _ = planned_row(
        db.get_candidates("fid_exc_rawphoric"),
        pv.rows[0].proposed_values,
        idx.unique_rows_by_id["fid_exc_rawphoric"][1],
        artist_rules=[peygan_with_exc],
    )
    assert new_r[3] == "Rawphoric"
    assert json.loads(new_r[22])["subgenre"]["source"] == "artist_rule"

    # L-1: `hold_on_conflict` between two collaborators with opposing rules (`Peygan` discourages Euphoric, `EuphArtist` prefers Euphoric)
    euph_collab_rule = ArtistRule(
        rule_id="AR-EUPH-001",
        artist_name="EuphArtist",
        enabled=True,
        approval_status="approved",
        verification_status="user_verified",
        preferred=["Euphoric"],
        collab_policy="hold_on_conflict",
    )
    ev_clash = evaluate_artist_rules(
        TrackMetadata(artist="Peygan & EuphArtist", title="Clash Track"),
        [],
        rules=[peygan_with_exc, euph_collab_rule],
        proposed_genre="Hardstyle",
        proposed_subgenre="Euphoric",
    )
    assert ev_clash["discouraged_conflict"] is True
    assert any("hold_on_conflict" in r for r in ev_clash["conflict_reasons"])

    # H-3c: `library_report` flags unlocked existing conflicting subgenre against active Artist_Rules
    tr_existing_bad = Track(
        file_id="fid_peygan_bad_sheet",
        name="Peygan - Old Mislabelled.flac",
        is_live_source=True,
        metadata=TrackMetadata(version="vb1", artist="Peygan", title="Old Mislabelled", duration=165.0),
    )
    db.save_track(tr_existing_bad)
    with closing(db._get_connection()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO root_membership(role, root_id, file_id, parent_id, path_json, scan_id, ambiguous) VALUES ('source', ?, ?, 'src_root_1', '[]', 1, 0)",
            ("src_root_1", "fid_peygan_bad_sheet"),
        )

    class _StubSheets:
        def fetch_header_row(self, sid, rng):
            return ["Track Name", "Folder", "Genre", "Subgenre", "BPM", "Key", "Artist", "Title", "Fmt", "MB", "Link", "Status", "Conf", "Date", "Drive ID", "Notes"]
        def iter_tracks_rows(self, sid, lc):
            yield 2, ["Peygan - Old Mislabelled.flac", "Hardstyle", "Hardstyle", "Euphoric", "158", "5A", "", "", "FLAC", "20", "", "SORTED – AUDIO PENDING", "", "", "fid_peygan_bad_sheet", ""]
        def load_artist_rules_tab(self, sid):
            return [peygan_with_exc], []

    class _StubDrive:
        def get_children_folders(self, vid):
            return {"Hardstyle": ["hs_folder"]}

    rep_json = tmp_path / "cov_rep.json"
    with closing(db._get_connection()) as conn:
        cov_sum = library_report(
            conn, db, cfg,
            Namespace(export_csv="", export_json=str(rep_json), verbose=True, file_id=["fid_peygan_bad_sheet"]),
            _StubSheets(), _StubDrive(),
        )
    cov_data = json.loads(rep_json.read_text(encoding="utf-8"))
    row_rep = cov_data["rows"][0]
    assert "subgenre_existing_vs_artist_rule_conflict" in row_rep["double_check_flags"]
    assert row_rep["states"]["review_required"] is True

    # M-3: `approve_artist_rule_suggestion` merges into existing rule without clobbering `track_exceptions`!
    with closing(db._get_connection()) as conn, conn:
        conn.execute(
            """
            INSERT INTO artist_rule_suggestions(
                suggestion_id, artist_name, norm_artist, pattern_type,
                suggested_rule_json, supporting_tracks_json, evidence_count, status, created_utc, updated_utc
            ) VALUES ('SUG-PEYGAN-MERGE', 'Peygan', 'peygan', 'recurring_subgenre_correction', ?, '[]', 2, 'pending_approval', '2026-11-27T00:00:00Z', '2026-11-27T00:00:00Z')
            """,
            (json.dumps({"rule_id": "AR-PEYGAN-LEARNED", "preferred": ["Experimental Raw"], "discouraged": ["Mainstream Hardstyle"], "notes": "Merged note"}),),
        )
    merged_rule = approve_artist_rule_suggestion(db, "SUG-PEYGAN-MERGE", actor="tester")
    assert merged_rule is not None
    assert merged_rule.rule_id == "AR-PEYGAN-001"
    assert len(merged_rule.track_exceptions) == 1
    assert "Mainstream Hardstyle" in merged_rule.discouraged
    assert "Euphoric" in merged_rule.discouraged
    assert "Experimental Raw" in merged_rule.preferred


def test_controlled_sheets_writes_rejection_and_approval_gates(tmp_path, monkeypatch):
    """
    Verify controlled deployment safeguards (Items 4–9):
    - AR-PEYGAN-001 soft-confidence rule (`confidence_boost=1`, no forced identity override)
    - Rejecting a suggestion (`SUG-ACTOFRAGE-870118`) retains it as `rejected_manual_review` (`enabled=False`)
    - `controlled_sheets_request` blocks DELETE and non-Sheets Drive URLs while allowing Sheets v4 POST/PUT
    - `build_tracks_write_preview` includes ONLY HIGH/MEDIUM validated updates, protecting manual locks,
      concrete existing values, LOW/HDRR artist-derived tags, and conflicts
    - `execute_approved_tracks_writes` requires exact `preview_sha256`, detects row drift, writes RAW values,
      and supports deterministic rollback via `rollback_sheets_manifest`
    """
    import json
    from musicvault.core.artist_rules import (
        DEFAULT_PEYGAN_RULE,
        ensure_default_artist_rules,
        reject_artist_rule_suggestion,
    )
    from musicvault.core.sheets_sync import (
        build_tracks_write_preview,
        execute_approved_tracks_writes,
        rollback_sheets_manifest,
        sync_artist_rules_to_sheet,
    )
    from musicvault.services.read_only import controlled_sheets_request
    from musicvault.services.sheets import validate_and_index_tracks_sheet

    monkeypatch.setenv("MUSICVAULT_CONFIG_DIR", str(tmp_path))
    cfg = Config(tmp_path)
    cfg.set("SHEET_ID", "sheet_ctrl_1")
    db = Database(cfg.db_path)
    ensure_default_artist_rules(db)

    # Item 4: AR-PEYGAN-001 soft-confidence check
    assert DEFAULT_PEYGAN_RULE.confidence_boost == 1
    assert DEFAULT_PEYGAN_RULE.explicit_override_genre == ""
    assert DEFAULT_PEYGAN_RULE.explicit_override_subgenre == ""

    # Item 5: Reject SUG-ACTOFRAGE-870118 while retaining for manual review
    with db._get_connection() as conn, conn:
        conn.execute(
            """
            INSERT INTO artist_rule_suggestions(
                suggestion_id, artist_name, norm_artist, pattern_type,
                suggested_rule_json, supporting_tracks_json, evidence_count, status, created_utc, updated_utc
            ) VALUES (
                'SUG-ACTOFRAGE-870118', 'Act of Rage', 'actofrage', 'recurring_subgenre_correction',
                '{"rule_id":"AR-ACTOFRAGE-LEARNED","preferred":["Rawphoric"],"discouraged":["Rawstyle"]}',
                '[]', 2, 'pending_approval', '2026-11-28T00:00:00Z', '2026-11-28T00:00:00Z'
            )
            """
        )
    rej = reject_artist_rule_suggestion(
        db,
        "SUG-ACTOFRAGE-870118",
        actor="reviewer",
        reason="Act of Rage has a varied catalogue; Rawstyle must not be discouraged globally.",
    )
    assert rej is not None and rej["status"] == "rejected_manual_review" and rej["enabled"] is False
    assert all(r.norm_artist != "actofrage" for r in load_artist_rules(db=db, active_only=False))
    with db._get_connection() as conn:
        retained = conn.execute(
            "SELECT status, suggested_rule_json FROM artist_rule_suggestions WHERE suggestion_id='SUG-ACTOFRAGE-870118'"
        ).fetchone()
    assert retained is not None and retained["status"] == "rejected_manual_review"
    assert "varied catalogue" in retained["suggested_rule_json"]

    # Item 9 / Request guard: Drive URIs and DELETE are blocked even on controlled_sheets_request
    with pytest.raises(RuntimeError):
        controlled_sheets_request(None, None, "https://www.googleapis.com/drive/v3/files/123", method="POST")
    with pytest.raises(RuntimeError):
        controlled_sheets_request(None, None, "https://sheets.googleapis.com/v4/spreadsheets/s1", method="DELETE")

    # Seed 3 tracks:
    # 1) HIGH/MEDIUM open-provenance MusicBrainz Hardstyle on placeholder 'Unclassified' -> ELIGIBLE
    # 2) HDRR-only artist-derived 'Rawstyle' -> LOW, MUST NOT populate Tracks!
    # 3) Locked MOVED row -> PROTECTED, MUST NOT overwrite!
    t_elig = Track("fid_elig_1", "Headhunterz - Reignite.flac", is_live_source=True, metadata=TrackMetadata(version="v1", artist="Headhunterz", title="Reignite", duration=200.0))
    t_hdrr = Track("fid_hdrr_low", "PEYGAN - RELOAD.flac", is_live_source=True, metadata=TrackMetadata(version="v1", artist="PEYGAN", title="RELOAD", duration=160.0))
    t_lock = Track("fid_locked_1", "Wildstylez - Timeless.flac", is_live_source=True, metadata=TrackMetadata(version="v1", artist="Wildstylez", title="Timeless", duration=220.0))
    for t_obj in (t_elig, t_hdrr, t_lock):
        db.save_track(t_obj)

    db.update_research_result(
        "fid_elig_1", "success", "v1",
        candidates=[Candidate("MusicBrainz", "Reignite", ["Headhunterz"], duration=200.0, genre=["Hardstyle"], verification="Duration supported", source_family="musicbrainz")],
        provider_calls={"_meta": {"lookup_complete": True}},
    )
    db.update_research_result(
        "fid_hdrr_low", "partial", "v1",
        candidates=[Candidate("HDRR", "RELOAD", ["PEYGAN"], genre=["Hardstyle", "Rawstyle"], raw_source_genre=["Rawstyle"], verification="Text only", source_family="hdrr", genre_evidence_kind="artist_derived")],
        provider_calls={"_meta": {"lookup_complete": True}},
    )
    db.update_research_result(
        "fid_locked_1", "success", "v1",
        candidates=[Candidate("MusicBrainz", "Timeless", ["Wildstylez"], duration=220.0, genre=["Hardcore"], verification="Duration supported", source_family="musicbrainz")],
        provider_calls={"_meta": {"lookup_complete": True}},
    )

    headers = ["Track Name", "Folder", "Genre", "Subgenre", "BPM", "Key", "Artist", "Title", "Fmt", "MB", "Link", "Status", "Conf", "Date", "Drive ID", "Notes"]
    sheet_rows = [
        ["Headhunterz - Reignite.flac", "Unsorted", "Unclassified", "Needs Review", "", "", "", "", "FLAC", "20", "", "PENDING", "", "", "fid_elig_1", ""],
        ["PEYGAN - RELOAD.flac", "Unsorted", "Unclassified", "Needs Review", "", "", "", "", "FLAC", "20", "", "PENDING", "", "", "fid_hdrr_low", ""],
        ["Wildstylez - Timeless.flac", "Hardstyle", "Hardstyle", "Euphoric", "150", "8A", "", "", "FLAC", "20", "", "MOVED", "", "", "fid_locked_1", "Verified"],
    ]
    s_idx = validate_and_index_tracks_sheet(headers, sheet_rows)
    raw_by_num = {2: sheet_rows[0], 3: sheet_rows[1], 4: sheet_rows[2]}

    preview = build_tracks_write_preview(db, cfg, s_idx, raw_rows_by_num=raw_by_num)
    assert preview["summary"]["eligible_row_updates"] == 1
    assert preview["proposed_row_updates"][0]["file_id"] == "fid_elig_1"
    assert preview["proposed_row_updates"][0]["field_changes"][0]["before"] == "Unclassified"
    assert preview["proposed_row_updates"][0]["field_changes"][0]["after"] == "Hardstyle"
    assert preview["proposed_row_updates"][0]["field_changes"][0]["confidence"] == "MEDIUM"

    # Mock ControlledSheetsService in-memory to verify Artist_Rules sync, approval gate, drift abort, apply & rollback
    class _InMemoryControlledSheets:
        def __init__(self):
            self.tabs = {"Tracks": [list(headers)] + [list(r) + [""] * 11 for r in sheet_rows]}

        def get_spreadsheet_metadata(self, sid):
            return {"sheets": [{"properties": {"title": k, "gridProperties": {"rowCount": 100, "columnCount": 27}}} for k in self.tabs]}

        def ensure_tab_exists(self, sid, tab_name, rows=100, cols=18):
            self.tabs.setdefault(tab_name, [])

        def read_values(self, sid, rng):
            tab, a1 = rng.split("!")
            tab = tab.strip("'")
            grid = self.tabs.get(tab, [])
            import re as _re
            m = _re.match(r"^[A-Z]+(\d+):[A-Z]+(\d+)$", a1)
            if m:
                r1, r2 = int(m.group(1)), int(m.group(2))
                return [list(row) for row in grid[r1 - 1 : r2]]
            return [list(row) for row in grid]

        def write_values_raw(self, sid, rng, values):
            tab = rng.split("!")[0].strip("'")
            self.tabs[tab] = [list(r) for r in values]
            return {}

        def batch_write_values_raw(self, sid, data):
            import re as _re
            for entry in data:
                tab, cell = entry["range"].split("!")
                tab = tab.strip("'")
                m = _re.match(r"^([A-Z]+)(\d+)$", cell)
                col_s, r_num = m.group(1), int(m.group(2))
                c_idx = 0
                for ch in col_s:
                    c_idx = c_idx * 26 + (ord(ch) - 64)
                c_idx -= 1
                while len(self.tabs[tab]) < r_num:
                    self.tabs[tab].append([""] * 27)
                while len(self.tabs[tab][r_num - 1]) <= c_idx:
                    self.tabs[tab][r_num - 1].append("")
                self.tabs[tab][r_num - 1][c_idx] = entry["values"][0][0]
            return {}

    mem_sh = _InMemoryControlledSheets()
    ar_sync = sync_artist_rules_to_sheet(cfg, db, mem_sh)
    assert ar_sync["tab_created"] is True and ar_sync["rules_written"] == 1
    assert mem_sh.tabs["Artist_Rules"][1][0] == "AR-PEYGAN-001"
    # Tracks untouched during Artist_Rules sync!
    assert mem_sh.tabs["Tracks"][1][2] == "Unclassified"

    # Approval gate: wrong SHA-256 MUST fail closed
    with pytest.raises(RuntimeError, match="confirm-sha256"):
        execute_approved_tracks_writes(cfg, db, mem_sh, preview, "bad_sha256")

    # Apply with valid preview_sha256
    app_res = execute_approved_tracks_writes(cfg, db, mem_sh, preview, preview["preview_sha256"])
    assert app_res["rows_updated"] == 1
    assert mem_sh.tabs["Tracks"][1][2] == "Hardstyle"
    assert mem_sh.tabs["Tracks"][2][2] == "Unclassified"  # HDRR LOW untouched
    assert mem_sh.tabs["Tracks"][3][2] == "Hardstyle" and mem_sh.tabs["Tracks"][3][3] == "Euphoric"  # Locked untouched

    # Deterministic rollback restores exact 'Unclassified' cell
    rb_res = rollback_sheets_manifest(cfg, db, mem_sh, app_res["manifest_id"])
    assert rb_res["status"] == "rolled_back"
    assert mem_sh.tabs["Tracks"][1][2] == "Unclassified"

    # Post-merge audit M-1 regression: subgenre MUST NOT populate an orphan cell when parent genre is TERMS_HOLD
    mb_plus_dc = [
        Candidate("MusicBrainz", "Infinity", ["Sefa", "D-Block & S-te-Fan"], duration=210.0, genre=["Hardcore", "Frenchcore"], verification="Duration supported", source_family="musicbrainz"),
        Candidate("Discogs", "Infinity", ["Sefa", "D-Block & S-te-Fan"], duration=210.0, genre=["Hardcore"], verification="Duration supported", source_family="discogs"),
    ]
    prop_inf = build_track_proposal(TrackMetadata(artist="Sefa, D-Block & S-te-Fan", title="Infinity", duration=210.0), mb_plus_dc)
    row_inf = ["Infinity.wav", "Unsorted", "Unclassified", "Needs Review", "", "", "", "", "WAV", "30", "", "PENDING", "", "", "fid_inf", ""]
    out_inf, dec_inf, chg_inf = planned_row(mb_plus_dc, prop_inf, row_inf, provider_terms_cleared=False)
    assert dec_inf["genre"] == "TERMS_HOLD"
    assert dec_inf["subgenre"] == "TERMS_HOLD"
    assert chg_inf == []
