"""
Unit tests for field-level decision engine.
Validates exact behavior of self_test() from v1.3.
"""
import json
import pytest
from musicvault.models import Candidate
from musicvault.core.rules import field_decision, planned_row


def test_field_decision_medium_high_conflict():
    base_candidate = lambda p, v: Candidate(
        provider=p,
        title="Test",
        verification="ISRC supported",
        bpm=v,
        key="5A"
    )

    # 1. Single provider -> MEDIUM
    c1 = [base_candidate("SonoVault", 150.0)]
    level, _ = field_decision(c1, "bpm", {"bpm": 150.0})
    assert level == "MEDIUM"

    # 2. Two independent agreeing providers -> HIGH
    c2 = [base_candidate("SonoVault", 150.0), base_candidate("Audiometa", 150.0)]
    level, _ = field_decision(c2, "bpm", {"bpm": 150.0})
    assert level == "HIGH"

    # 3. Two conflicting providers -> LOW
    c3 = [base_candidate("SonoVault", 150.0), base_candidate("Audiometa", 151.0)]
    level, _ = field_decision(c3, "bpm", {"bpm": 150.0})
    assert level == "LOW"

    # 4. No candidates -> LOW
    level, _ = field_decision([], "bpm", {"bpm": 150.0})
    assert level == "LOW"


def test_existing_value_preservation():
    # Existing values must never be overwritten, and unverified existing values get review star
    old = [''] * 27
    old[4] = '147'
    old[5] = '1A'
    old[2] = 'Techno'
    old[3] = 'Hypertechno'

    new, dec, changes = planned_row([], {'bpm': '', 'key': '', 'genre': '', 'subgenre': ''}, old)
    assert all(new[k] == old[k] for k in (2, 3, 4, 5))
    assert new[16] == '★'
    assert dec['bpm'] == 'EXISTING_UNVERIFIED'


def test_user_locked_fields():
    # User locked fields are strictly immutable
    base_candidate = lambda p, v: Candidate(
        provider=p,
        title="Test",
        verification="ISRC supported",
        bpm=v,
        key="5A"
    )
    old = [''] * 27
    old[22] = json.dumps({'bpm': {'state': 'USER_LOCKED'}})
    cs = [base_candidate("SonoVault", 150.0), base_candidate("Audiometa", 150.0)]

    new, dec, _ = planned_row(cs, {'bpm': 150.0, 'key': '', 'genre': '', 'subgenre': ''}, old)
    assert new[4] == ''
    assert dec['bpm'] == 'LOCKED'
