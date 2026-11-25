"""
Unit tests for parent genre taxonomy, alias normalization, and decision logic.
Validates v1.4 parent genre beta assertions.
"""
import pytest
from musicvault.core.genre import decide_genre, canonical_genre


def test_parent_routing_self_test_assertions():
    # 1. Agreement
    assert decide_genre('Hip Hop', ['Hip-Hop']) == ('Hip-Hop', 'AGREEMENT')

    # 2. Conflict
    assert decide_genre('Drum & Bass', ['Techno'])[1] == 'CONFLICT'

    # 3. New catalogue candidate
    assert decide_genre('', ['Trance'])[0] == 'Trance'

    # 4. Existing only review
    assert decide_genre('Hardstyle', [])[1] == 'EXISTING_ONLY_REVIEW'

    # 5. User locked preserves locked genre
    assert decide_genre('Hardstyle', ['Techno'], locked=True)[0] == 'Hardstyle'


def test_genre_alias_normalization():
    assert canonical_genre('drumnbass') == 'Drum & Bass'
    assert canonical_genre('dnb') == 'Drum & Bass'
    assert canonical_genre('rawstyle') == 'Hardstyle'
    assert canonical_genre('euphorichardstyle') == 'Hardstyle'
    assert canonical_genre('hypertechno') == 'Techno'
    assert canonical_genre('hiphoprap') == 'Hip-Hop'
    assert canonical_genre('garage') == 'UK Garage'
