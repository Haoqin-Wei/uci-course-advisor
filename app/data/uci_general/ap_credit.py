"""UC Irvine credit for College Board AP examinations.

Transcribed from "AP credits: Irvine" on the UC Admissions site
(https://admission.universityofcalifornia.edu/admission-requirements/ap-exam-credits/ap-credits/irvine.html),
read 2026-11-19 and confirmed by the project owner. The page states no
academic year; re-read it when a new catalogue year starts.

A transcript prints the exam, the score and the units, not what the credit
counts for, so this chart supplies it. For each exam and score:

* ``credited``   every course treated as taken (prerequisites, no repeats).
                 "A or B" on the chart credits both IDs; the student has one
                 course either way.
* ``ge_courses`` the courses actually earned, whose catalogue GE labels count.
* ``ge_direct``  GE categories the chart itself says are satisfied.

Not modelled yet: Biology differs for Biological Sciences majors (treated as
non-majors here), the 8-unit cap when both Calculus exams are taken, and
"may not replace School of ... requirements". Those change unit totals and
some majors' requirements, not prerequisites or GE.
"""

from __future__ import annotations

import re
from typing import Optional

from app.catalog.course_equivalents import expand

SOURCE_URL = ("https://admission.universityofcalifornia.edu/admission-requirements/"
              "ap-exam-credits/ap-credits/irvine.html")
SOURCE_READ_ON = "2026-11-19"


def _rule(low, high, credited=(), ge_courses=None, ge_direct=None, note=None):
    return {"scores": (low, high), "credited": tuple(credited),
            "ge_courses": tuple(credited if ge_courses is None else ge_courses),
            "ge_direct": dict(ge_direct or {}), "note": note}


def _language(department):
    first = [f"{department} 1A", f"{department} 1B", f"{department} 1C"]
    second = [f"{department} 2A", f"{department} 2B", f"{department} 2C"]
    return (_rule(3, 3, first, ge_direct={"VI": 1}),
            _rule(4, 5, second, ge_direct={"VI": 1, "VIII": 1}))


_ELECTIVE = (_rule(3, 5, note="elective units only"),)

# (exam name, pattern on the transcript's upper-cased subject, rules). Only
# PSYCHOLOGY, ECONOMICS:MI and PHYSICS C:ME are transcript spellings that
# have been seen; the other patterns follow the same abbreviations and an
# exam that matches none is reported as unmatched, never guessed.
CHART = (
    ("African American Studies", r"AFRICAN\s*AMER", _ELECTIVE),
    ("Art History", r"ART\s*HIST", (
        _rule(3, 3, ["ART HIS 40A"], ge_direct={"IV": 1, "VIII": 1}),
        _rule(4, 5, ["ART HIS 40A", "ART HIS 40B"], ge_direct={"IV": 2, "VIII": 1}))),
    ("Art (Studio)", r"STUDIO|ART.*(?:2-?D|3-?D|DRAW|DESIGN)", _ELECTIVE),
    ("Biology", r"^BIOLOGY", (_rule(3, 5, ge_direct={"II": 1}, note="one Biological Sciences course toward GE II"),)),
    ("Chemistry", r"^CHEM", (
        _rule(3, 3, note="elective units only"),
        _rule(4, 5, ["CHEM 1A", "ENGR 1A"], ge_courses=["CHEM 1A"]))),
    ("Chinese Language and Culture", r"CHINESE", (
        _rule(3, 3, note="elective units only"),
        _rule(4, 5, ["CHINESE 1A", "CHINESE 1B", "CHINESE 1C", "CHINESE 2A"], ge_direct={"VI": 1, "VIII": 1}))),
    ("Computer Science Principles", r"COMP.*SCI.*(?:PRIN|:\s*P\b)|CS\s*PRIN", (_rule(3, 5, ["I&C SCI 20"]),)),
    ("Computer Science A", r"COMP.*SCI|^CS\b", _ELECTIVE),
    ("Microeconomics", r"ECON\w*\s*:?\s*MI\b|MICRO", (
        _rule(3, 3, note="elective units only"),
        _rule(4, 5, ["ECON 20A", "MGMT 4A"], ge_courses=["ECON 20A"]))),
    ("Macroeconomics", r"ECON\w*\s*:?\s*MA\b|MACRO", (
        _rule(3, 3, note="elective units only"),
        _rule(4, 5, ["ECON 20B", "MGMT 4B"], ge_courses=["ECON 20B"]))),
    # The chart prints "Writing 505" for one exam and "Writing 50" for both;
    # the owner confirmed Writing 50 is meant in both places.
    ("English Language and Composition", r"ENGL.*LANG", (
        _rule(3, 3, note="satisfies Entry Level Writing; elective units"),
        _rule(4, 5, ["WRITING 50"], ge_direct={"IV": 1}))),
    ("English Literature and Composition", r"ENGL.*LIT", (
        _rule(3, 3, note="satisfies Entry Level Writing; elective units"),
        _rule(4, 5, ["WRITING 50"], ge_direct={"IV": 1}))),
    ("Environmental Science", r"ENVIR", (
        _rule(3, 3, note="elective units only"), _rule(4, 5, ["EARTHSS 1"]))),
    ("French Language and Culture", r"FRENCH", _language("FRENCH")),
    ("German Language and Culture", r"GERMAN", _language("GERMAN")),
    ("Italian Language and Culture", r"ITALIAN", _language("ITALIAN")),
    ("Japanese Language and Culture", r"JAPAN", _language("JAPANSE")),
    ("Spanish Literature and Culture", r"SPAN.*LIT", (
        _rule(3, 3, ["SPANISH 1A", "SPANISH 1B", "SPANISH 1C"], ge_direct={"VI": 1}),
        _rule(4, 5, ["SPANISH 3", "SPANISH 3H"], ge_courses=[], ge_direct={"VI": 1, "VII": 1, "VIII": 1}))),
    ("Spanish Language and Culture", r"SPAN", _language("SPANISH")),
    ("Latin", r"LATIN", (
        _rule(3, 3, note="elective units only"),
        _rule(4, 5, ge_direct={"VI": 1, "VIII": 1}, note="course credit by petition"))),
    ("US Government and Politics", r"GOV.*(?:U\.?S|UNITED|AMER)|(?:U\.?S|AMER).*GOV", (
        _rule(3, 3, note="elective units only"),
        _rule(4, 5, ["POL SCI 21A"], note="also satisfies American Institutions"))),
    ("Comparative Government and Politics", r"GOV.*COMP|COMP.*GOV", (
        _rule(3, 3, note="elective units only"), _rule(4, 5, ["POL SCI 51A"]))),
    ("European History", r"HIST.*EUR|EUR.*HIST", (
        _rule(3, 4, note="elective units only"),
        _rule(5, 5, ge_direct={"IV": 1, "VIII": 1}, note="one lower-division History course, not 70B"))),
    ("United States History", r"HIST.*(?:U\.?S|UNITED|AMER)|(?:U\.?S|AMER).*HIST", (
        _rule(3, 4, note="satisfies American History and Institutions"),
        _rule(5, 5, ge_direct={"IV": 1}, note="one of History 40A-40B-40C; satisfies American History and Institutions"))),
    ("World History: Modern", r"HIST.*WORLD|WORLD.*HIST", (
        _rule(3, 4, note="elective units only"),
        _rule(5, 5, ge_direct={"IV": 1, "VIII": 1}, note="one of History 21B-21C"))),
    ("Human Geography", r"GEOG", _ELECTIVE),
    ("Calculus BC", r"CALC.*BC", (
        _rule(3, 3, ["MATH 2A", "MATH 5A"], ge_courses=["MATH 2A"]),
        _rule(4, 5, ["MATH 2A", "MATH 2B", "MATH 5A", "MATH 5B"], ge_courses=["MATH 2A", "MATH 2B"]))),
    ("Calculus AB", r"CALC.*AB|CALC", (_rule(3, 5, ["MATH 2A", "MATH 5A"], ge_courses=["MATH 2A"]),)),
    ("Precalculus", r"PRE-?CALC", (_rule(3, 5, note="no university credit"),)),
    ("Music Theory", r"MUSIC", _ELECTIVE),
    ("Physics C: Mechanics", r"PHYS\w*\s+C\s*:?\s*M", (
        _rule(3, 3, note="elective units only"), _rule(4, 4, ["PHYSICS 2"]), _rule(5, 5, ["PHYSICS 3A"]))),
    ("Physics C: Electricity and Magnetism", r"PHYS\w*\s+C\s*:?\s*E", (
        _rule(3, 3, note="elective units only"), _rule(4, 4, ["PHYSICS 2"]), _rule(5, 5, ["PHYSICS 3C"]))),
    ("Physics 1", r"PHYS.*\b1\b", _ELECTIVE),
    ("Physics 2", r"PHYS.*\b2\b", _ELECTIVE),
    ("Psychology", r"PSYCH", (
        _rule(3, 3, note="elective units only"),
        _rule(4, 5, ["PSY 9", "COGS 7A"], ge_courses=["PSY 9"]))),
    ("Statistics", r"STAT", (_rule(3, 5, ["STATS 7", "STATS 8", "MGMT 7", "SOCECOL 13", "EDUC 15"],
                                   ge_courses=["STATS 7"]),)),
)
# Precalculus must be tried before the Calculus patterns.
_ORDER = sorted(CHART, key=lambda entry: entry[0] != "Precalculus")


def resolve(exam_type: str, subject: str, score) -> dict:
    """What one exam credit counts for at UCI.

    ``matched`` is False for an exam type other than AP, a subject the chart
    does not recognise, or a score it has no rule for; such credit keeps its
    units and counts for nothing else until the student maps it by hand.
    """
    empty = {"matched": False, "exam": None, "credited": [], "ge_courses": [], "ge_direct": {}, "note": None}
    if str(exam_type or "").upper() != "AP":
        return empty
    text = " ".join(str(subject or "").upper().split())
    try:
        value = float(score)
    except (TypeError, ValueError):
        return empty
    for name, pattern, rules in _ORDER:
        if not re.search(pattern, text):
            continue
        for rule in rules:
            low, high = rule["scores"]
            if low <= value <= high:
                return {"matched": True, "exam": name,
                        "credited": expand(rule["credited"]), "ge_courses": list(rule["ge_courses"]),
                        "ge_direct": dict(rule["ge_direct"]), "note": rule["note"]}
        return {**empty, "exam": name, "note": "no credit rule for this score"}
    return empty
