"""
Regression test for the 10-track control benchmark.
Validates consistency against reference results in 02_Latest_Test_Report_v1.3.txt.
"""
import pytest
from musicvault.core.organizer import build_move_plan, RoutingConfidence

# 10 control tracks with reference spreadsheet states
BENCHMARK_ROWS = [
    (2, "1heVoqsB5HpgGHCNAU0liJDrhrIFCWytX", "Believe - Jesse Bloch, Coby Watts.wav", "", "", False),
    (2169, "1ehnUyjzwrwjeEeUv1nLymSk5LXUUDYmW", "Atmozfears & David Spekter - Release (10 Year Anniversary Remix).aiff", "Hardstyle", "Euphoric", True),
    (7, "1v3jdlhhx3hqfY6ivYBut3DcJxKsu6gSL", "Act of Rage - Living Fast.flac", "Hardstyle", "Rawstyle", True),
    (23, "1NJxM8N3WyJMS7-KEhDriERqlJYwk8YLf", "Big City Life - Luude, Mattafix.wav", "Drum & Bass", "", True),
    (159, "1N74Lus5IHsDXslNA3iB3eC9EbuoDt-zB", "Amen - Dexperia.flac", "Drum & Bass", "", True),
    (355, "1E50C7UI78bE6hwRpIDOuHciWqP-fYBiK", "Another High - Wasted Penguinz.wav", "Hardstyle", "Euphoric", True),
    (1914, "1VpA4yDXwkykR7YzN9hWFZZS-TMK3ps18", "The Loudest City (Decibel outdoor 2026 Anthem) - Rebellion.flac", "Hardstyle", "Rawstyle", True),
    (1040, "1IikiHEWOKy_Su_9NHSx5EKdKQyoTOrt-", "Young, Wild & Free (feat. Bruno Mars) - Snoop Dogg, Wiz Khalifa, Bruno Mars.wav", "", "", False),
    (2181, "1QuI-Y-pNGDxob7Ez_q4mN6aNtDCPOiks", "Hard Driver - You're Not Alone.flac", "Hardstyle", "", True),
    (1204, "1GSmBaNJj9SR80Kw_9XtByPKyUKUg5F1C", "YoU'rE nOt OriGiNaL - Adjuzt.flac", "Hardstyle", "Xtra Raw", True),
]


def test_benchmark_10_track_routing():
    eligible_count = 0
    authorized_count = 0

    for row_num, file_id, track_name, genre, subgenre, expected_eligible in BENCHMARK_ROWS:
        sheet_row = [''] * 27
        sheet_row[2] = genre
        sheet_row[3] = subgenre
        sheet_row[14] = file_id

        plan = build_move_plan(
            file_id=file_id,
            track_name=track_name,
            row_num=row_num,
            sheet_row=sheet_row,
            allow_provisional=False
        )

        assert plan.eligible == expected_eligible, f"Mismatch for {track_name}"
        assert plan.move_authorized is False  # Safe default: no moves authorized without explicit opt-in

        if plan.eligible:
            eligible_count += 1
            assert plan.routing_confidence == RoutingConfidence.PROVISIONAL_REVIEW
            assert plan.reason == "PROVISIONAL_EXISTING_GENRE"
            assert plan.destination in ("Hardstyle", "Drum & Bass")
        else:
            assert plan.routing_confidence == RoutingConfidence.HOLD
            assert plan.reason == "UNKNOWN_OR_UNSUPPORTED_GENRE"
            assert plan.destination == ""

        if plan.move_authorized:
            authorized_count += 1

    # Exact replication of benchmark: 8 of 10 eligible, 0 authorized by default
    assert eligible_count == 8
    assert authorized_count == 0
