"""The departments one term actually has, for the model and for tool guards.

A student writes "psycho", "心理学" or "poli sci". Working out which
department that means is the model's job, not a table of aliases. What the
model cannot know is this term's registrar code: Psychology was PSYCH, then
PSCI, and is PSY in Winter 2027. So each turn carries the roster of the
queried term, and a tool call for a code the term does not have is answered
at once with that roster instead of a request that can only fail.
"""

from __future__ import annotations

from typing import Iterable, Optional
from xml.sax.saxutils import escape

from app.catalog import store
from app.catalog.term import Term

ERROR_CODE = "department_not_in_term"


def roster(term: str) -> dict[str, str]:
    """Registrar code → official name for a fully synced term, else empty.

    A partly imported term lists only some departments; treating its roster
    as complete would reject departments that exist.
    """
    parsed = Term.parse(term or "")
    if parsed is None:
        return {}
    complete = any(
        row.get("term_id") == parsed.term_id and row.get("status") == "complete"
        for row in store.read_term_snapshots()
    )
    if not complete:
        return {}
    return store.department_roster(parsed.term_id)


def roster_line(term: str) -> str:
    """``"ECON=Economics; PSY=Psychology"`` for one term, or ``""``."""
    return "; ".join(f"{code}={name}" for code, name in roster(term).items())


def prompt_block(terms: Iterable[str]) -> str:
    """The roster of each queried term, with how the model should use it."""
    parts = []
    for term in dict.fromkeys(str(t) for t in terms or []):
        line = roster_line(term)
        if line:
            parts.append(f'  <departments term="{escape(term)}">{escape(line)}</departments>')
    if not parts:
        return ""
    return (
        '<term_departments source="application">\n'
        + "\n".join(parts) + "\n"
        "  <how_to_use>Each entry is REGISTRAR_CODE=official department name for that term. "
        "Students name departments loosely: abbreviations, partial words, Chinese names or "
        "slang (psycho, 心理学, poli sci, 计科, bio). Work out which department they mean and "
        "pass that entry's code to tools. Codes change between years, so never use a code "
        "from memory that is not listed for the term. If two entries fit equally well "
        "(for example Psychology and Psychiatry), say which one you used, or ask when "
        "you cannot tell. If nothing fits, say the term does not list that department."
        "</how_to_use>\n"
        "</term_departments>"
    )


def unknown_department(term: str, department: Optional[str]) -> Optional[dict]:
    """A structured refusal when the term has a roster and lacks this code."""
    code = " ".join(str(department or "").split()).upper()
    if not code:
        return None
    listed = roster(term)
    if not listed or code in listed:
        return None
    return {
        "ok": False,
        "found": False,
        "error_code": ERROR_CODE,
        "term": term,
        "department": code,
        "message": (
            f"{code} is not a department code in {term}. No request was made. "
            "Pick the code of the department the student means from "
            "departments_this_term and call again once; do not try other tools "
            "with this code."
        ),
        "departments_this_term": roster_line(term),
    }
