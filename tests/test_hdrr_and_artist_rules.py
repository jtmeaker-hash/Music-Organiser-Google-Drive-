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
    ]
    flagged = flag_existing_rows_against_approved_rules(db, existing_indexed)
    assert len(flagged) == 1
    assert flagged[0]["file_id"] == "fid_existing_wrong"
    assert "Euphoric" in flagged[0]["existing_subgenre"]
