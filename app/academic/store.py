"""SQLite persistence for structured academic records.

SQLite is the private-beta server database. The schema is deliberately
normalized and uses ordinary SQL types so it can be migrated to a US-region
PostgreSQL service without changing the API contract.
"""

from __future__ import annotations

import csv
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from app import config
from app.academic.models import TranscriptCourseInput, TranscriptImportRequest
from app.catalog.departments import resolve_department
from app.catalog.normalization import parse_course_mention


_COURSE_NUMBER_RE = re.compile(r"^[A-Z]?\d{1,3}[A-Z0-9/]{0,5}$")
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_time(body: TranscriptImportRequest, imported_at: str) -> str:
    if body.printed_at is None:
        return imported_at
    value = body.printed_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


@contextmanager
def _conn():
    path = config.academic_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        _init_schema(conn)
        yield conn
    finally:
        conn.close()


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS student_academic_profiles (
            user_id TEXT PRIMARY KEY,
            official_uc_gpa REAL,
            gpa_as_of TEXT,
            gpa_imported_at TEXT,
            grade_units_attempted REAL,
            total_units_passed REAL,
            units_completed REAL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS student_courses (
            user_id TEXT NOT NULL,
            course_id TEXT NOT NULL,
            transcript_title TEXT NOT NULL DEFAULT '',
            catalog_title TEXT,
            effective_grade TEXT,
            grade_points REAL,
            units REAL,
            status TEXT NOT NULL,
            credit_code TEXT,
            manual_added INTEGER NOT NULL DEFAULT 0,
            transcript_seen INTEGER NOT NULL DEFAULT 1,
            catalog_matched INTEGER NOT NULL DEFAULT 0,
            repeat_applied INTEGER NOT NULL DEFAULT 0,
            effective_term TEXT,
            source_printed_at TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (user_id, course_id)
        );
        CREATE INDEX IF NOT EXISTS idx_student_courses_user_status
            ON student_courses(user_id, status);

        CREATE TABLE IF NOT EXISTS exam_credits (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            exam_type TEXT NOT NULL,
            subject TEXT NOT NULL,
            score REAL,
            units REAL,
            exam_date TEXT,
            uci_equivalent_course TEXT,
            source_printed_at TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(user_id, exam_type, subject, exam_date)
        );

        CREATE TABLE IF NOT EXISTS transfer_credits (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            institution_name TEXT,
            units REAL NOT NULL,
            terms_through TEXT,
            uci_equivalent_course TEXT,
            source_printed_at TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(user_id, institution_name, units, terms_through)
        );

        CREATE TABLE IF NOT EXISTS university_requirements (
            user_id TEXT NOT NULL,
            requirement_code TEXT NOT NULL,
            status TEXT NOT NULL,
            status_date TEXT,
            source_printed_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY(user_id, requirement_code)
        );

        CREATE TABLE IF NOT EXISTS transcript_imports (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            client_request_id TEXT,
            parser_version TEXT NOT NULL,
            printed_at TEXT,
            imported_at TEXT NOT NULL,
            added_count INTEGER NOT NULL,
            updated_count INTEGER NOT NULL,
            read_count INTEGER NOT NULL DEFAULT 0,
            accepted_count INTEGER NOT NULL DEFAULT 0,
            unchanged_count INTEGER NOT NULL DEFAULT 0,
            older_ignored_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL,
            status TEXT NOT NULL,
            error_code TEXT,
            UNIQUE(user_id, client_request_id)
        );
        CREATE INDEX IF NOT EXISTS idx_transcript_imports_user_time
            ON transcript_imports(user_id, imported_at DESC);
        """
    )
    # Private-beta databases created by the first transcript build do not have
    # the detailed import counters. Add them without requiring a migration
    # command or discarding existing academic records.
    existing_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(transcript_imports)")
    }
    for name in (
        "read_count",
        "accepted_count",
        "unchanged_count",
        "older_ignored_count",
    ):
        if name not in existing_columns:
            conn.execute(
                f"ALTER TABLE transcript_imports ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
            )
    conn.commit()


@lru_cache(maxsize=1)
def _catalog_titles() -> dict[str, str]:
    path = _REPO_ROOT / "data" / "uci" / "courses.csv"
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    with path.open("r", encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            department = resolve_department(row.get("department") or "")
            number = (row.get("course_number") or "").strip().upper()
            title = (row.get("title") or "").strip()
            if department and _COURSE_NUMBER_RE.fullmatch(number) and title:
                out[f"{department} {number}"] = title
    return out


def _canonical_course_id(raw: str) -> str | None:
    """Normalize free-form/manual course text, not transcript records."""
    ref = parse_course_mention(raw or "")
    return ref.display() if ref else None


def _structured_course_id(course: TranscriptCourseInput) -> tuple[str | None, str | None]:
    """Validate the parser's registrar fields without reparsing natural text."""
    department = resolve_department(course.department)
    if not department:
        return None, "unsupported_department"
    number = (course.course_number or "").strip().upper()
    if not _COURSE_NUMBER_RE.fullmatch(number):
        return None, "invalid_course_id"
    course_id = f"{department} {number}"
    submitted = " ".join((course.course_id or "").strip().upper().split())
    if submitted != course_id:
        return None, "course_id_mismatch"
    return course_id, None


def _import_issue(
    reason_code: str,
    message: str,
    *,
    course_id: str | None = None,
    count: int = 1,
    level: str = "warning",
) -> dict:
    return {
        "course_id": course_id,
        "reason_code": reason_code,
        "message": message,
        "count": count,
        "level": level,
    }


def _same_course_values(existing: sqlite3.Row, values: dict) -> bool:
    return all(existing[key] == value for key, value in values.items())


def _course_status(grade: str) -> str:
    value = (grade or "").strip().upper()
    if value in {"F", "NP", "U"}:
        return "failed"
    if value == "W":
        return "withdrawn"
    if value == "I":
        return "incomplete"
    if value == "IP":
        return "in_progress"
    if value == "NR":
        return "no_report"
    if value in {
        "A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-",
        "D+", "D", "D-", "P", "S",
    }:
        return "passed"
    return "unknown"


def _should_replace(existing: sqlite3.Row, source_time: str) -> bool:
    previous = existing["source_printed_at"] or ""
    return not previous or source_time >= previous


def import_transcript(user_id: str, body: TranscriptImportRequest) -> dict:
    imported_at = _now_iso()
    source_time = _source_time(body, imported_at)
    import_id = uuid.uuid4().hex
    added = 0
    updated = 0
    unchanged = 0
    older_ignored = 0
    skipped = int(body.skipped_count)
    read_count = len(body.courses) + skipped
    issues: list[dict] = []
    if body.skipped_count:
        issues.append(
            _import_issue(
                "parser_unrecognized",
                f"{body.skipped_count} course-like row(s) could not be parsed locally.",
                course_id=None,
                count=body.skipped_count,
            )
        )
    titles = _catalog_titles()

    with _conn() as conn:
        if body.client_request_id:
            duplicate = conn.execute(
                "SELECT * FROM transcript_imports WHERE user_id = ? AND client_request_id = ?",
                (user_id, body.client_request_id),
            ).fetchone()
            if duplicate:
                return {
                    "ok": True,
                    "duplicate": True,
                    "import_id": duplicate["id"],
                    "added": duplicate["added_count"],
                    "updated": duplicate["updated_count"],
                    "read": duplicate["read_count"],
                    "accepted": duplicate["accepted_count"],
                    "unchanged": duplicate["unchanged_count"],
                    "older_ignored": duplicate["older_ignored_count"],
                    "skipped": duplicate["skipped_count"],
                    "issues": [],
                    "completed_course_ids": _completed_ids(conn, user_id),
                    "transcript_course_ids": _known_ids(conn, user_id),
                }

        for course in body.courses:
            submitted_id = " ".join((course.course_id or "").strip().upper().split()) or None
            if course.confidence < 0.80:
                skipped += 1
                issues.append(
                    _import_issue(
                        "low_confidence",
                        "The parser confidence was below the import threshold.",
                        course_id=submitted_id,
                    )
                )
                continue
            course_id, validation_error = _structured_course_id(course)
            if not course_id:
                skipped += 1
                if validation_error == "unsupported_department":
                    message = f"Department {course.department.upper()} is not in the current UCI catalog."
                elif validation_error == "course_id_mismatch":
                    message = "The structured department and number do not match the course ID."
                else:
                    message = "The course number does not match a supported UCI format."
                issues.append(
                    _import_issue(
                        validation_error or "invalid_course_id",
                        message,
                        course_id=submitted_id,
                    )
                )
                continue
            grade = course.grade.upper()
            status = _course_status(grade)
            if status == "unknown":
                skipped += 1
                issues.append(
                    _import_issue(
                        "unsupported_grade",
                        "The transcript grade code is not supported.",
                        course_id=course_id,
                    )
                )
                continue
            transcript_title = course.title.strip().upper()
            catalog_title = titles.get(course_id)
            existing = conn.execute(
                "SELECT * FROM student_courses WHERE user_id = ? AND course_id = ?",
                (user_id, course_id),
            ).fetchone()
            if existing and not _should_replace(existing, source_time):
                older_ignored += 1
                issues.append(
                    _import_issue(
                        "older_record_ignored",
                        "A newer transcript record is already stored for this course.",
                        course_id=course_id,
                        level="info",
                    )
                )
                continue

            record_values = {
                "transcript_title": transcript_title,
                "catalog_title": catalog_title,
                "effective_grade": grade,
                "grade_points": course.grade_points,
                "units": course.units,
                "status": status,
                "credit_code": (course.credit_code or "").upper() or None,
                "catalog_matched": 1 if catalog_title else 0,
                "repeat_applied": 1
                if (course.credit_code or "").upper() in {"RF", "G0", "G1", "G2"}
                else 0,
                "effective_term": course.effective_term,
            }
            values = (
                *record_values.values(),
                source_time,
                imported_at,
            )
            if existing:
                same_values = _same_course_values(existing, record_values)
                conn.execute(
                    """
                    UPDATE student_courses SET
                        transcript_title = ?, catalog_title = ?, effective_grade = ?,
                        grade_points = ?, units = ?, status = ?, credit_code = ?,
                        transcript_seen = 1, catalog_matched = ?, repeat_applied = ?,
                        effective_term = ?, source_printed_at = ?, last_seen_at = ?
                    WHERE user_id = ? AND course_id = ?
                    """,
                    (*values, user_id, course_id),
                )
                if same_values:
                    unchanged += 1
                else:
                    updated += 1
            else:
                conn.execute(
                    """
                    INSERT INTO student_courses (
                        user_id, course_id, transcript_title, catalog_title,
                        effective_grade, grade_points, units, status, credit_code,
                        catalog_matched, repeat_applied, effective_term,
                        source_printed_at, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, course_id, *values[:-1], imported_at, imported_at),
                )
                added += 1

        _upsert_profile(conn, user_id, body, source_time, imported_at)
        _upsert_exam_credits(conn, user_id, body, source_time, imported_at)
        _upsert_transfer_credits(conn, user_id, body, source_time, imported_at)
        _upsert_requirements(conn, user_id, body, source_time, imported_at)
        conn.execute(
            """
            INSERT INTO transcript_imports (
                id, user_id, client_request_id, parser_version, printed_at,
                imported_at, added_count, updated_count, read_count,
                accepted_count, unchanged_count, older_ignored_count,
                skipped_count, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'success')
            """,
            (
                import_id,
                user_id,
                body.client_request_id,
                body.parser_version,
                source_time if body.printed_at else None,
                imported_at,
                added,
                updated,
                read_count,
                added + updated + unchanged,
                unchanged,
                older_ignored,
                skipped,
            ),
        )
        conn.commit()
        all_completed = _completed_ids(conn, user_id)
        all_known = _known_ids(conn, user_id)

    return {
        "ok": True,
        "duplicate": False,
        "import_id": import_id,
        "added": added,
        "updated": updated,
        "read": read_count,
        "accepted": added + updated + unchanged,
        "unchanged": unchanged,
        "older_ignored": older_ignored,
        "skipped": skipped,
        "issues": issues,
        "completed_course_ids": all_completed,
        "transcript_course_ids": all_known,
    }


def _completed_ids(conn: sqlite3.Connection, user_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT course_id FROM student_courses
        WHERE user_id = ? AND status = 'passed'
        ORDER BY course_id
        """,
        (user_id,),
    ).fetchall()
    return [row["course_id"] for row in rows]


def _known_ids(conn: sqlite3.Connection, user_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT course_id FROM student_courses WHERE user_id = ? ORDER BY course_id",
        (user_id,),
    ).fetchall()
    return [row["course_id"] for row in rows]


def _upsert_profile(
    conn: sqlite3.Connection,
    user_id: str,
    body: TranscriptImportRequest,
    source_time: str,
    imported_at: str,
) -> None:
    summary = body.summary
    existing = conn.execute(
        "SELECT * FROM student_academic_profiles WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if (
        existing
        and existing["official_uc_gpa"] is not None
        and existing["gpa_as_of"]
        and source_time < existing["gpa_as_of"]
    ):
        return
    conn.execute(
        """
        INSERT INTO student_academic_profiles (
            user_id, official_uc_gpa, gpa_as_of, gpa_imported_at,
            grade_units_attempted, total_units_passed, units_completed, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            official_uc_gpa = COALESCE(excluded.official_uc_gpa, student_academic_profiles.official_uc_gpa),
            gpa_as_of = CASE
                WHEN excluded.official_uc_gpa IS NOT NULL THEN excluded.gpa_as_of
                ELSE student_academic_profiles.gpa_as_of
            END,
            gpa_imported_at = CASE
                WHEN excluded.official_uc_gpa IS NOT NULL THEN excluded.gpa_imported_at
                ELSE student_academic_profiles.gpa_imported_at
            END,
            grade_units_attempted = COALESCE(excluded.grade_units_attempted, student_academic_profiles.grade_units_attempted),
            total_units_passed = COALESCE(excluded.total_units_passed, student_academic_profiles.total_units_passed),
            units_completed = COALESCE(excluded.units_completed, student_academic_profiles.units_completed),
            updated_at = excluded.updated_at
        """,
        (
            user_id,
            summary.official_uc_gpa,
            source_time if summary.official_uc_gpa is not None else None,
            imported_at if summary.official_uc_gpa is not None else None,
            summary.grade_units_attempted,
            summary.total_units_passed,
            summary.units_completed,
            imported_at,
        ),
    )


def _upsert_exam_credits(
    conn: sqlite3.Connection,
    user_id: str,
    body: TranscriptImportRequest,
    source_time: str,
    imported_at: str,
) -> None:
    for item in body.exam_credits:
        if item.confidence < 0.80:
            continue
        equivalent = _canonical_course_id(item.uci_equivalent_course or "")
        conn.execute(
            """
            INSERT INTO exam_credits (
                id, user_id, exam_type, subject, score, units, exam_date,
                uci_equivalent_course, source_printed_at, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, exam_type, subject, exam_date) DO UPDATE SET
                score = excluded.score,
                units = excluded.units,
                uci_equivalent_course = excluded.uci_equivalent_course,
                source_printed_at = excluded.source_printed_at,
                last_seen_at = excluded.last_seen_at
            """,
            (
                uuid.uuid4().hex,
                user_id,
                item.exam_type,
                item.subject.upper(),
                item.score,
                item.units,
                item.exam_date,
                equivalent,
                source_time,
                imported_at,
                imported_at,
            ),
        )


def _upsert_transfer_credits(
    conn: sqlite3.Connection,
    user_id: str,
    body: TranscriptImportRequest,
    source_time: str,
    imported_at: str,
) -> None:
    for item in body.transfer_credits:
        if item.confidence < 0.80:
            continue
        equivalent = _canonical_course_id(item.uci_equivalent_course or "")
        conn.execute(
            """
            INSERT INTO transfer_credits (
                id, user_id, institution_name, units, terms_through,
                uci_equivalent_course, source_printed_at, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, institution_name, units, terms_through) DO UPDATE SET
                uci_equivalent_course = excluded.uci_equivalent_course,
                source_printed_at = excluded.source_printed_at,
                last_seen_at = excluded.last_seen_at
            """,
            (
                uuid.uuid4().hex,
                user_id,
                item.institution_name,
                item.units,
                item.terms_through,
                equivalent,
                source_time,
                imported_at,
                imported_at,
            ),
        )


def _upsert_requirements(
    conn: sqlite3.Connection,
    user_id: str,
    body: TranscriptImportRequest,
    source_time: str,
    imported_at: str,
) -> None:
    for item in body.university_requirements:
        conn.execute(
            """
            INSERT INTO university_requirements (
                user_id, requirement_code, status, status_date,
                source_printed_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, requirement_code) DO UPDATE SET
                status = excluded.status,
                status_date = excluded.status_date,
                source_printed_at = excluded.source_printed_at,
                last_seen_at = excluded.last_seen_at
            """,
            (
                user_id,
                item.requirement_code,
                item.status,
                item.status_date,
                source_time,
                imported_at,
            ),
        )


def get_academic_profile(
    user_id: str, manual_course_ids: Iterable[str] = (), *, include_gpa: bool = False
) -> dict:
    with _conn() as conn:
        course_rows = conn.execute(
            """
            SELECT course_id, transcript_title, catalog_title
            FROM student_courses
            WHERE user_id = ? AND status = 'passed'
            ORDER BY course_id
            """,
            (user_id,),
        ).fetchall()
        known_rows = conn.execute(
            "SELECT course_id FROM student_courses WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        profile = conn.execute(
            """SELECT official_uc_gpa, gpa_as_of, units_completed, total_units_passed
               FROM student_academic_profiles WHERE user_id = ?""",
            (user_id,),
        ).fetchone()
        latest = conn.execute(
            """
            SELECT imported_at, read_count, accepted_count, added_count,
                   updated_count, unchanged_count, older_ignored_count,
                   skipped_count
            FROM transcript_imports WHERE user_id = ? AND status = 'success'
            ORDER BY imported_at DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    completed = {
        row["course_id"]: {
            "course_id": row["course_id"],
            "title": row["catalog_title"] or row["transcript_title"],
            "catalog_matched": bool(row["catalog_title"]),
            "transcript_seen": True,
        }
        for row in course_rows
    }
    transcript_known = {row["course_id"] for row in known_rows}
    titles = _catalog_titles()
    for raw in manual_course_ids:
        course_id = _canonical_course_id(raw)
        if not course_id or course_id in completed or course_id in transcript_known:
            continue
        completed[course_id] = {
            "course_id": course_id,
            "title": titles.get(course_id, ""),
            "catalog_matched": course_id in titles,
            "transcript_seen": False,
        }
    result = {
        "completed_courses": sorted(completed.values(), key=lambda item: item["course_id"]),
        "last_import": dict(latest) if latest else None,
        "gpa_as_of": profile["gpa_as_of"] if profile else None,
        "gpa_available": bool(profile and profile["official_uc_gpa"] is not None),
        "units_completed": profile["units_completed"] if profile else None,
        "total_units_passed": profile["total_units_passed"] if profile else None,
    }
    # GPA stays out of the default page payload; reveal only on the user's request.
    if include_gpa:
        result["official_uc_gpa"] = profile["official_uc_gpa"] if profile else None
    return result


def get_ai_academic_context(user_id: str) -> dict:
    """Return the only academic fields allowed into an AI request."""
    with _conn() as conn:
        courses = conn.execute(
            """
            SELECT course_id, effective_grade, units FROM student_courses
            WHERE user_id = ? AND status = 'passed'
            ORDER BY course_id
            """,
            (user_id,),
        ).fetchall()
        profile = conn.execute(
            "SELECT official_uc_gpa FROM student_academic_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return {
        "courses": [dict(row) for row in courses],
        "uc_gpa": profile["official_uc_gpa"] if profile else None,
    }


def delete_user_data(user_id: str) -> None:
    with _conn() as conn:
        for table in (
            "student_courses",
            "student_academic_profiles",
            "exam_credits",
            "transfer_credits",
            "university_requirements",
            "transcript_imports",
        ):
            conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        conn.commit()
