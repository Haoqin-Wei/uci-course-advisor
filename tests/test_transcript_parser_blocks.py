"""What the transcript parser keeps from the header and the credit blocks."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

LINES = [
    "2026/9/18 08:23    Unofficial Transcript",
    "Doe, Jordan (12345678)",
    "UNDECLARED (UNAFFILIATED)",
    "********* THIS IS NOT AN OFFICIAL TRANSCRIPT *********",
    "University Requirements",
    "03/01/26 Entry Level Writing - Course Passed",
    "09/01/24 American History - Satisfied",
    "AP PSYCHOLOGY (Score 5, Units 4.0) 05/24",
    "",
    "AP ECONOMICS:MI (Score 4, Units 4.0) 05/24",
    "",
    "AP PHYSICS C:ME (Score 3, Units 4.0) 05/23",
    "Units Transferred 12.0",
    "2026 First Summer Session",
    "INTRO DIGITAL SYST    EECS    31    4.0    A-    14.8",
    "Term Totals    ATTM: 4.0    PSSD: 4.0    GPTS: 14.8    GPA: 3.700    BAL: 6.8",
    "BRIGHAM YOUNG U (Units 4.5) 1 Terms to 08/26",
    "Units Transferred 4.5",
    "2026 Special / 10-Week Summer Session",
    "ARGUMENT & RESEARCH    WRITING    60    4.0    A+    16.0",
    "Term Totals    ATTM: 4.0    PSSD: 4.0    GPTS: 16.0    GPA: 4.000    BAL: 8.0",
    "INCOMPLETE GRADES: 0 UNITS: 0.0",
    "GRADE UNITS ATTEMPTED 8.0 GRADE POINTS 30.8 UC GPA 3.850 BALANCE 14.8",
    "TOTAL UNITS PASSED 8.0 UNITS COMPLETED 24.5",
]
SCRIPT = """
const [parserPath, lines] = [process.argv[1], JSON.parse(process.argv[2])];
import(parserPath).then(parser => console.log(JSON.stringify(parser.parseUciTranscriptLines(lines))));
"""


def parse(lines):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-e", SCRIPT, str(ROOT / "static/js/transcript-parser.mjs"), json.dumps(lines)],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def test_exam_subtotal_is_not_a_second_transfer_credit():
    payload = parse(LINES)
    assert [(c["subject"], c["score"], c["units"]) for c in payload["exam_credits"]] == [
        ("PSYCHOLOGY", 5, 4), ("ECONOMICS:MI", 4, 4), ("PHYSICS C:ME", 3, 4)]
    assert [(t["institution_name"], t["units"]) for t in payload["transfer_credits"]] == [("BRIGHAM YOUNG U", 4.5)]


def test_a_transfer_total_without_any_detail_line_is_still_kept():
    lines = [line for line in LINES if not line.startswith(("AP ", "BRIGHAM"))]
    transfers = parse(lines)["transfer_credits"]
    assert [(t["institution_name"], t["units"]) for t in transfers] == [(None, 12.0), (None, 4.5)]


def test_major_on_record_is_read_and_the_name_is_not():
    payload = parse(LINES)
    assert payload["declared_major"] == "UNDECLARED (UNAFFILIATED)"
    assert "Doe" not in json.dumps(payload) and "12345678" not in json.dumps(payload)


def test_missing_major_line_gives_none():
    lines = [line for line in LINES if not line.startswith("UNDECLARED")]
    assert parse(lines)["declared_major"] is None


def test_ten_week_summer_courses_get_their_own_term():
    courses = {course["course_id"]: course["effective_term"] for course in parse(LINES)["courses"]}
    assert courses == {"EECS 31": "2026 First Summer Session",
                       "WRITING 60": "2026 Special / 10-Week Summer Session"}
