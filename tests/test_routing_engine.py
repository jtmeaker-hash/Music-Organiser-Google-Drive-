"""
Unit tests for Drive routing and organization engine.
Validates exact behavior of routing_self_test() from v1.3.
"""
import json
import pytest
from musicvault.core.organizer import routing_choice


def make_row(genre: str, subgenre: str, genre_state: str = '', subgenre_state: str = '') -> list:
    row = [''] * 27
    row[2] = genre
    row[3] = subgenre
    row[22] = json.dumps({'genre': {'state': genre_state}, 'subgenre': {'state': subgenre_state}})
    return row


def test_verified_broad_and_nested():
    # Broad genre verified
    assert routing_choice(make_row('Hardstyle', '', 'USER_LOCKED', ''))[0] == ['Hardstyle']

    # Genre and compatible subgenre verified -> nested
    assert routing_choice(make_row('Hardstyle', 'Euphoric', 'USER_LOCKED', 'USER_LOCKED'))[0] == ['Hardstyle', 'Euphoric']

    # Subgenre hierarchy conflict -> falls back to broad parent
    assert routing_choice(make_row('Hardstyle', 'Jump Up', 'USER_LOCKED', 'USER_LOCKED'))[0] == ['Hardstyle']


def test_provisional_routing():
    # Existing unverified genre -> provisional review
    dest, reason, conf = routing_choice(make_row('Hardstyle', 'Rawstyle', '', ''))
    assert dest == ['Hardstyle']
    assert reason == 'PROVISIONAL_EXISTING_GENRE'
    assert conf == 'PROVISIONAL_REVIEW'

    # Medium provisional genre -> broad review
    assert routing_choice(make_row('Hardstyle', 'Rawstyle', 'MEDIUM_PROVISIONAL', ''))[0] == ['Hardstyle']


def test_conflict_and_unknown_genres():
    # Unknown genre -> hold (None)
    assert routing_choice(make_row('Unknown', '', '', ''))[0] is None

    # Genre conflict in locks -> hold (None)
    assert routing_choice(make_row('Hardstyle', '', 'CONFLICT', ''))[0] is None
