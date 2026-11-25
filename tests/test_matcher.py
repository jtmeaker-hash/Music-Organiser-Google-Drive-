"""
Unit tests for matcher, Camelot key conversion, and audio tags verification.
"""
import pytest
from musicvault.core.matcher import (
    camelot_key,
    norm,
    artist_alias,
    clean_filename,
    parse_artist_title_from_stem,
    verify_candidate
)


def test_camelot_key_conversion():
    # Pitch class 0 = C
    assert camelot_key(0, 1) == "8B"  # C Major
    assert camelot_key(0, 0) == "5A"  # C Minor

    # Pitch class 9 = A
    assert camelot_key(9, 1) == "11B" # A Major
    assert camelot_key(9, 0) == "8A"  # A Minor

    # Invalid values
    assert camelot_key(12, 0) is None
    assert camelot_key(-1, 1) is None
    assert camelot_key(0, 2) is None


def test_artist_alias_resolution():
    assert artist_alias("Rebellion") == "rebelion"
    assert artist_alias("rebellion") == "rebelion"
    assert artist_alias("Act of Rage") == "actofrage"


def test_filename_cleaning():
    stem = "Believe - Jesse Bloch, Coby Watts [Free DL]"
    assert clean_filename(stem) == "Believe - Jesse Bloch, Coby Watts"

    artist, title = parse_artist_title_from_stem(stem)
    assert artist == "Believe"
    assert title == "Jesse Bloch, Coby Watts"


def test_candidate_verification():
    # ISRC match
    assert verify_candidate("US1234567890", 200.0, "US1234567890", 200.0) == "ISRC supported"

    # ISRC mismatch
    assert verify_candidate("US1111111111", 200.0, "US2222222222", 200.0) == "ISRC conflict"

    # Duration match within tolerance
    assert verify_candidate("", 200.0, "", 201.0) == "Duration supported"

    # Duration mismatch exceeds 3s / 2%
    assert verify_candidate("", 200.0, "", 220.0) == "Duration conflict"
