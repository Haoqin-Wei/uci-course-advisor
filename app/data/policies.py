# app/data/policies.py
"""
UCI academic policies, hardcoded from catalogue + registrar + the
Manual of the Irvine Division of the Academic Senate. Each top-level
dict is exposed via the `get_policy` agent tool by its registry key
(see app/agent/tools.py:_tool_get_policy).

For each block, the keys are policy-fact identifiers and the values
are either the literal number/string the LLM should quote, or a short
plain-English rule (kept under ~250 chars so it stays quotable). When
a fact comes from a regulation, the value or its sibling `cite` field
references the Senate Manual section (e.g. "IR A350(A)" = Irvine
Regulation A350 paragraph A) so the LLM can attribute claims.

Last verified: 2026-05-29 from the sources at the bottom.
"""
from __future__ import annotations

from typing import Optional

# ── Unit / enrollment limits ─────────────────────────────
UNIT_LIMITS = {
    "undergrad": {
        "min_per_quarter":              12,
        "max_initial":                  18,    # initial enrollment cap
        "max_open":                     20,    # raised at Second Open Enrollment
        "max_petition":                 None,  # >20 by petition, case-by-case
        "ics_max_petition":             26,
        "summer_session_default":       10,
        "summer_session_ics_petition":  14,
        "career_total_non_engineering": 216,   # max units after 12th quarter
        "career_total_engineering":     236,
        "transfer_max_quarters":        9,     # 10 for Engineering
        "transfer_max_quarters_engineering": 10,
        "ap_ib_credit_excluded":        True,  # AP/IB doesn't count toward career cap
        "below_min_needs_dean":         True,  # <12 units needs dean approval
    },
    "grad": {
        "min_per_quarter":       8,
        "max_per_quarter":       16,
        "max_strictly_grad":     12,           # IR 446: per quarter
        "max_upper_div_major":   16,           # IR 446: per quarter
    },
    "cite": "IR 445, IR 446, IR 386",
}

# ── Degree requirements ──────────────────────────────────
DEGREE_REQUIREMENTS = {
    "min_units":                       180,
    "min_gpa":                         2.0,
    "min_residence_units_of_final_45": 36,
    "uc_residence_min_quarters":       3,
    "cite":                            "IR 515, IR 525, SR 612, SR 634",
}

# ── Class level (UG) ─────────────────────────────────────
CLASS_LEVEL = [  # ordered: (label, lower_bound, upper_bound_exclusive)
    ("freshman",  0,    45),
    ("sophomore", 45,   90),
    ("junior",    90,   135),
    ("senior",    135,  float("inf")),
]
CLASS_LEVEL_CITE = "IR 380 (also IR 385 'Normal Progress')"

# ── Grading (letter grades, points, repeats, I / IP / NR) ─
GRADING = {
    "passing_letter":      "A+ / A / A- (excellent); B+/B/B- (good); "
                           "C+/C/C- (fair); D+/D/D- (barely passing)",
    "passing_alt":         "P (passed, UG); S (satisfactory, grad only)",
    "not_passing":         "F (failure); NP (not passed); U (unsatisfactory, grad)",
    "undetermined":        "I (incomplete)",
    "grade_points": {
        "A": 4.0, "B": 3.0, "C": 2.0, "D": 1.0, "F": 0.0, "I": 0.0,
        "+": "+0.3 (except A+, no GPA boost — used only for truly extraordinary work)",
        "-": "-0.3",
    },
    "a_plus_rule":         "A+ exists but counts as 4.0 (same as A) for GPA; "
                           "instructors are told to reserve it for truly "
                           "extraordinary work",
    "passing_threshold_undergrad": "D- or higher (1.7-and-above counts as P for P/NP)",
    "passing_threshold_graduate":  "B or better (A, A-, B+, B, S); "
                                   "B- accepted if prior overall GPA ≥ 3.0",
    "repeatable_grades":   "Only C-, D+, D, D-, F, NP can be repeated for credit",
    "repeat_letter_match": "A course originally taken for a letter grade must be "
                           "repeated for a letter grade. P/NP originals may repeat "
                           "either way (if offered).",
    "repeat_gpa_rule":     "First 16 units repeated → only most recent grade counts "
                           "toward GPA. Beyond 16 → both old and new grades count.",
    "repeat_credit_rule":  "Degree credit awarded only once per course, but every "
                           "attempt stays permanently on the transcript.",
    "repeat_more_than_once": "Repeating a course MORE than once requires school approval.",
    "graduate_repeat_rule":  "Grad student may repeat once a course with grade < B or U; "
                             "first 8 units repeated → only most recent grade counts.",
    "honors_swap":         "If a UG earns C-, D+, D, D-, F, or NP in an honors-only "
                           "or majors-only course, the grade may be replaced by the "
                           "non-honors / non-major iteration of the same course.",
    "grade_change_authority": "Registrar may change a final grade only on written "
                              "request of the instructor citing a clerical or "
                              "procedural error (no re-assessment-of-quality changes). "
                              "Grievance redress route: IR Appendix II grade appeals.",
    "cite": "IR A345 Grading; IR A365 Change of Grade",
}

# ── Incomplete (I) and In-Progress (IP) transcript notations ─
INCOMPLETE_GRADE = {
    "when_assigned":          "Work is of passing quality but incomplete for good cause. "
                              "Cannot be used for failing work or when no prior "
                              "arrangement was made with the instructor.",

    # Undergraduate I-grade timeline
    "ug_replacement_window":  "Whichever comes FIRST: the period set by the instructor; "
                              "12 months following the quarter the I was awarded; or "
                              "the end of the quarter immediately preceding award of the "
                              "degree.",
    "ug_not_currently_enrolled": "12 months from awarding quarter; up to 2 additional "
                                  "years in exceptional cases (prolonged inability to "
                                  "study) with instructor + Dean approval; petition "
                                  "must be filed within the original 12 months.",
    "ug_gpa_during_window":   "I is NOT included in GPA while the replacement window "
                              "is open. If not replaced in time it auto-converts to "
                              "F (or NP / U as appropriate) and DOES then count in GPA.",

    # Graduate I-grade timeline
    "grad_replacement_window":  "End of the THIRD quarter following the awarding "
                                "quarter, or end of the quarter preceding degree "
                                "award, whichever comes first. Only enrolled quarters "
                                "count toward the 3-quarter window.",
    "grad_not_currently_enrolled": "1 calendar year; up to 2 additional years in "
                                    "exceptional cases with instructor + Dean approval; "
                                    "petition within the original year.",
    "grad_gpa_at_graduation":  "Grad I-grades are NOT in GPA until graduation; at "
                                "graduation, courses graded I count as 'attempted' for "
                                "IR A525 GPA assessment, but the I notation itself "
                                "stays on transcript.",

    # IP and NR notations
    "ip_in_progress_meaning": "For multi-quarter sequences where evaluation is "
                              "deferred to the final quarter. IP carries no grade "
                              "points and doesn't count toward GPA.",
    "ip_auto_conversion":     "If a final grade isn't filed, IP auto-converts to I "
                              "at the end of the 3rd quarter after the original IP "
                              "quarter (or end of quarter before degree, whichever "
                              "first). That I cannot be subsequently replaced.",
    "nr_no_report_meaning":   "Student was on the official roster but the instructor "
                              "turned in no grade. Carries no GPA effect.",
    "nr_auto_conversion":     "NR auto-converts to F or NP at the end of the FIRST "
                              "quarter following its assignment if not removed.",

    "cite": "IR A345(F) Incomplete; IR A345(H) IP and NR notations",
}

# ── Pass / Not-Passed option ─────────────────────────────
PASS_NO_PASS = {
    "max_avg_units_per_quarter":    4,
    "good_standing_required":       True,
    "extra_pnp_only_unit_cap":      12,    # in PNP-only-designated courses beyond the 4/qtr avg
    "minimum_grade_for_pass":       "C- (1.7) or better",
    "academic_notice_eligible":     False,  # students on notice can't elect P/NP unless course is P/NP-only
    "election_deadline":            "At enrollment. Changes after the 10th day of "
                                    "instruction require dean (or equivalent) approval.",
    "counts_toward_180_units":      True,
    "counts_toward_breadth":        True,
    "counts_toward_major_required": False,  # unless the course is offered P/NP-only OR dean approves
    "gpa_effect":                   "Disregarded — P/NP courses don't affect GPA.",
    "graduate_rule":                "Grad students: one P/NP course per quarter, but "
                                    "those courses cannot count toward degree requirements.",
    "cite": "IR A350",
}

# ── Adding / Dropping / Withdrawal ───────────────────────
# Combines the Senate-level rule (IR 440 — what's authorized and when)
# with the registrar's operational procedure (which TOOL to use during
# each window: WebReg in weeks 1-2, Enrollment Exceptions in Student
# Access weeks 3-6). Sourced from
# https://www.reg.uci.edu/enrollment/adc/adcpolicy.html plus IR 440.
ADD_DROP = {
    "free_window":             "Weeks 1-2: free add, drop, change grading option, "
                               "and change unit value — done in WebReg, no academic "
                               "approval needed. Subject to IR 445 / IR A350 caps.",
    "add_after_week_2":        "Weeks 3-6: ADD requires approval of (1) dean of "
                               "student's major AND (2) dean (or equivalent) of the "
                               "school / unit offering the course. Done via "
                               "Enrollment Exceptions in Student Access (NOT WebReg). "
                               "Senate Manual also requires instructor permission "
                               "(IR 440); the registrar's portal collects both dean "
                               "approvals.",
    "drop_after_week_2":       "Weeks 3-6: DROP requires approval of (1) dean of "
                               "student's major AND (2) dean (or equivalent) of the "
                               "school / unit offering the course. Done via "
                               "Enrollment Exceptions in Student Access (NOT WebReg).",
    "withdraw_after_week_6":   "After week 6: withdrawal allowed only with permission "
                               "of dean of offering school + dean of major. Records "
                               "a 'W' on the transcript.",
    "w_notation_gpa_effect":   "W is on transcript but doesn't count toward GPA and "
                               "doesn't count as a 'course attempted' for SR 634.",
    "change_grading_basis":    "Weeks 1-2 free in WebReg. Weeks 3-10: change requires "
                               "approval of dean of student's major, done via "
                               "Enrollment Exceptions in Student Access. Final "
                               "deadline is end of week 10 / final day of instruction.",
    "change_unit_value":       "Variable-unit courses only. Weeks 1-2 free in WebReg. "
                               "Weeks 3-6: requires major dean approval via "
                               "Enrollment Exceptions in Student Access.",
    "unique_deadlines_clause": "Individual courses may have impaction-driven unique "
                               "add/drop deadlines; must be published in the Schedule "
                               "of Classes AND the syllabus.",
    "tools": {
        "weeks_1_2":           "WebReg (https://www.reg.uci.edu/)",
        "weeks_3_6":           "Enrollment Exceptions, available in Student Access",
        "after_week_6":        "Petition through your major dean's office; "
                               "registrar processes via Enrollment Exceptions",
    },
    "cite": "IR 440 (Senate Manual); UCI Registrar add/drop/change policy "
            "(https://www.reg.uci.edu/enrollment/adc/adcpolicy.html)",
}

# ── Enrollment responsibility (registrar's "student responsibility" notice) ──
ENROLLMENT_RESPONSIBILITY = {
    "official_enrollment":     "You are responsible for your OFFICIAL enrollment. "
                               "Attending class without enrolling does not earn credit. "
                               "Enrolling without attending does not relieve you of "
                               "grade / unit consequences.",
    "must_add_for_credit":     "Enroll in (or add) classes for which you expect to "
                               "receive credit. If your name isn't on the official "
                               "roster by the end of the add window, no credit will "
                               "be awarded — instructors cannot retroactively add you.",
    "must_drop_if_stopped":    "Officially DROP classes you have stopped attending. "
                               "Not attending is not the same as dropping — an "
                               "unattended class will receive its final grade "
                               "(typically F or NP) if you don't drop.",
    "verify_enrollment":       "Verify your current enrollment any time via WebReg, "
                               "Student Access, or at the Registrar's Office. "
                               "Discrepancies become harder to fix the longer they "
                               "sit.",
    "cite": "UCI Registrar 'Enrollment Responsibility' notice on "
            "https://www.reg.uci.edu/enrollment/adc/adcpolicy.html",
}

WITHDRAWAL = {
    "honorable_withdrawal":     "Student in good academic standing and not under "
                                "disciplinary disqualification may receive a 'statement "
                                "of honorable withdrawal' with dean (or equivalent) "
                                "approval. Dean may attach comments.",
    "unauthorized_withdrawal":  "Withdrawing from the University before the end of the "
                                "quarter WITHOUT authorization gets an F (or NP where "
                                "appropriate) in every enrolled course.",
    "cite": "IR 470 (honorable), IR 475 (unauthorized)",
}

# ── Academic notice / disqualification (UG) ──────────────
ACADEMIC_NOTICE = {
    "academic_notice_trigger":  "At end of any quarter: that-quarter GPA < 2.0 OR "
                                "cumulative GPA < 2.0. Plus the IR A385 normal-progress "
                                "table (missed unit milestones).",
    "disqualification_trigger": "Quarter GPA < 1.5 in any single quarter, OR two "
                                "consecutive quarters on academic notice without "
                                "achieving cumulative GPA ≥ 2.0.",
    "stricter_school_norms":    "Schools may adopt stricter standards via Assembly-"
                                "approved Divisional regulation (e.g. major-specific "
                                "minimum grades — see IR 390).",
    "transcript_visibility":    "'Academic Notice' and 'Subject to Disqualification' "
                                "are INTERNAL only — NOT posted on official transcripts "
                                "(but a current-status statement accompanies outbound "
                                "transcripts).",
    "appeal_rights":            "Disqualification appeals — students have the right "
                                "to be notified of inadequacies + consequences, the "
                                "opportunity to improve, and to appear personally to "
                                "contest. More than one Faculty member must deliberate.",
    "good_standing_meaning":    "A UG is in good standing if NOT subject to academic "
                                "notice or disqualification per IR 395.",
    "cite": "IR 395, IR 400, IR 405; normal-progress table in IR A385",
}

# ── Major GPA / declaration ──────────────────────────────
MAJOR_GPA = {
    "min_overall_major":          "2.0 (C) in all courses required in the major program",
    "min_upper_div_major":        "2.0 (C) in upper-division courses required in the major",
    "denial_of_major_consequence": "Schools may deny continuance in the major if a "
                                    "student falls below 2.0 in major coursework "
                                    "(notice + procedures required).",
    "declare_major_deadline":     "By junior status (90 units, excluding pre-HS-graduation "
                                  "college work). Failure to declare → may be subject to "
                                  "academic notice / disqualification per IR A385.",
    "honors_min_threshold":       "Higher minimums may apply for major honors programs.",
    "computation_frequency":      "GPA in the major is recomputed every quarter; "
                                  "denial of major can take effect before the next "
                                  "quarter begins.",
    "cite": "IR 390, IR A385",
}

# ── Honors ──────────────────────────────────────────────
HONORS = {
    "quarterly_eligibility":      "≥ 12 letter-grade units that quarter AND GPA ≥ 3.5",
    "graduation_cap_total":       "≤ 16% of graduating seniors per school can receive "
                                  "Latin honors",
    "graduation_cap_summa":       "≤ 2%",
    "graduation_cap_magna":       "≤ 4%",
    "graduation_cap_cum_laude":   "≤ 10%",
    "uc_residence_for_honors":    "≥ 72 UC units (Education Abroad satisfies per SR 630)",
    "ics_criterion":              "ICS undergrad honors at graduation: cumulative GPA "
                                  "(primary), with possible adjustments for significant "
                                  "school/departmental governance or research contributions; "
                                  "final selection by the ICS Faculty.",
    "cite": "IR 415, IR 607 (ICS)",
}

# ── Finals ──────────────────────────────────────────────
FINALS = {
    "ug_required":              "Final assessments are REQUIRED in all undergraduate "
                                "courses (with rare exceptions noted in the regulation).",
    "must_appear_in_syllabus":  "Structure, expectations, and timing must be in the "
                                "syllabus FROM THE FIRST DAY OF INSTRUCTION.",
    "sync_max_duration":        "3 hours for in-person / synchronous exams in non-lab/"
                                "non-studio courses.",
    "scheduling_rule":          "Must be given at the time announced in the Schedule "
                                "of Classes; exceptions only with department / academic "
                                "unit approval for sound educational reason. Instructors "
                                "who deviate must accommodate students with conflicts.",
    "async_window":             "Asynchronous final assessments must be DUE during final "
                                "examination week; >3-hour async work should be assigned "
                                "BEFORE finals week.",
    "lab_studio_exception":     "Lab/studio courses normally don't require a final; if "
                                "the department/unit requires one, it may be held during "
                                "class time of the final week of instruction (must be "
                                "in syllabus from day 1).",
    "exam_modalities_allowed":  "Final exam (take-home or in-person), oral exam, project, "
                                "or one+ term papers — instructor's choice, declared in "
                                "syllabus.",
    "general_exam_in_major":    "End-of-graduation quarter: department may give a "
                                "general exam in the major and may excuse the student "
                                "from finals in that department's courses that quarter "
                                "(with SCOC approval for unit value).",
    "cite": "IR A465",
}

# ── Entry Level Writing Requirement ──────────────────────
ELWR = {
    "deadline":                 "Must be satisfied within the FIRST 3 quarters of UC "
                                "enrollment; a student who hasn't satisfied by then "
                                "is NOT eligible to enroll for a 4th quarter.",
    "pre_enrollment_paths":     ["UCI placement process placing student in a post-ELWR "
                                  "writing class",
                                  "Acceptable score on an approved writing measure "
                                  "(AP / IELTS / TOEFL list at UCOP)",
                                  "≥ 3 semester credits or ≥ 4 quarter units of "
                                  "transferable college English composition, earned "
                                  "with a letter grade ≥ C"],
    "post_enrollment_paths":    ["Local placement into a post-ELWR course",
                                  "Successful completion of a Senate-approved course "
                                  "with a grade of C or better (C- or below = not "
                                  "satisfied, may repeat)"],
    "credit_after_enrollment":  "Transfer credit for writing courses taken at another "
                                "institution is only counted toward ELWR satisfaction "
                                "AFTER ELWR is fulfilled at UC.",
    "cite": "IR 505; SR 636",
}

# ── Residence (degree residency) ─────────────────────────
RESIDENCE = {
    "uc_min_quarters":          3,
    "irvine_final_45_rule":     "≥ 36 of the FINAL 45 units must be earned in residence "
                                "at UCI.",
    "eap_exception":            "Education Abroad Program / UC DC / UC Sacramento "
                                "Center students retain residence credit per SR 630.",
    "summer_session_credit":    "Summer session courses with ≥ 2 units count as half a "
                                "quarter's residence; ≥ 6 units in summer counts as a "
                                "full semester of residence.",
    "cite": "IR 515; SR 612, SR 614, SR 630, SR 688, SR 690, SR 694",
}

# ── General Education ────────────────────────────────────
GENERAL_EDUCATION = {
    "categories": {
        "I":    "Writing (3 courses: 2 lower-division + 1 upper-division)",
        "II":   "Science and Technology (3 courses)",
        "III":  "Social and Behavioral Sciences (3 courses)",
        "IV":   "Arts and Humanities (3 courses)",
        "V":    "Quantitative, Symbolic, and Computational Reasoning (3 courses; "
                "≥ 1 each in subcategories VA and VB, third from either)",
        "VA":   "Quantitative Literacy",
        "VB":   "Formal Reasoning",
        "VI":   "A Language Other Than English (3 courses in the SAME language)",
        "VII":  "Multicultural Studies (1 course)",
        "VIII": "International / Global Issues (1 course)",
    },
    "upper_div_writing_prereq": "Upper-division writing course (Cat I, course 3) "
                                "cannot be taken before completing the lower-division "
                                "writing requirement.",
    "double_counting_rule":     "A single course approved for multiple GE categories "
                                "can simultaneously satisfy both. Cat V courses double-"
                                "counted with Cat VII/VIII/V another need not carry "
                                "extra units; otherwise a multi-cat course usually "
                                "carries > 4 units (e.g. Humanities Core = 8 units).",
    "p_np_allowed":             True,
    "approval_authority":       "Council on Educational Policy (CEP); routine course "
                                "approval delegated to the Subcommittee on Courses.",
    "cite": "IR 520",
}

# ── Academic Integrity (Appendix VIII) ───────────────────
ACADEMIC_INTEGRITY = {
    "violation_types": {
        "cheating":         "Use of unauthorized materials, info, or aids; dishonest "
                            "or unfair acts to gain academic advantage; failure to "
                            "observe announced course procedures.",
        "dishonest_conduct": "Fabricating information or knowingly furnishing false "
                             "information.",
        "plagiarism":       "Using another person's or entity's work (words, ideas, "
                            "designs, data) without appropriate citation. Also includes "
                            "submitting purchased work as your own, OR re-submitting "
                            "your OWN previous work as if it were new.",
        "collusion":        "Assisting or seeking assistance beyond expectations or "
                            "without instructor permission to complete course work.",
    },
    "sanctions": [
        "Educational Assignments (tutorial / required consultation / written assignment)",
        "Warning (written notice of violation, future violations escalate)",
        "Disciplinary Probation (timed status; further violations escalate)",
        "Suspension (timed termination of student status; transcript notation while "
        "suspended; cannot transfer or enroll elsewhere in UC during suspension)",
        "Dismissal (indefinite; readmission requires Chancellor's approval; "
        "transcript notation during dismissal)",
    ],
    "report_window_business_days": 30,    # for instructor to file
    "respond_window_business_days": 10,   # for student to schedule meeting with OAISC
    "appeal_window_business_days":  10,   # for student to appeal after OAISC decision
    "evidence_standard":            "preponderance of the evidence",
    "appeal_grounds": [
        "New evidence which could not be adduced earlier and is likely to change "
        "the result",
        "Violation of due process",
        "Imposed sanction is too harsh given the findings",
    ],
    "appeal_paths": {
        "written_review": "For Warning, Disciplinary Probation, and/or educational "
                          "assignments — panel reviews written appeal + record. No "
                          "student appearance.",
        "hearing":        "For Suspension or Dismissal — student may appear, present "
                          "evidence, bring a support person/advisor.",
    },
    "transcript_for_aip_reduced_grade": "A reduced course grade resulting from an "
                                       "AIP violation STAYS on the transcript even if "
                                       "the student later retakes the course and "
                                       "earns a better grade.",
    "managing_office":           "OAISC — Office of Academic Integrity and Student Conduct",
    "appeal_panel":              "Convened by the Academic Integrity Review Board (AIRB)",
    "cite": "Senate Manual Appendix VIII",
}

# ── WebReg system mechanics (registrar's WebReg help page) ───
# https://www.reg.uci.edu/registrar/soc/webreg.html
# Operational rules for using WebReg itself — distinct from IR 440
# (which is the Senate-level add/drop rule). Covers system hours,
# enrollment window vs Open Enrollment, waitlist mechanics, holds,
# late-enrollment fee, and what gets dropped automatically.
WEBREG = {
    "login_required":            "UCInetID + password. Activate via OIT if you don't "
                                 "have one yet.",
    "system_hours":              "WebReg is available 6:00 a.m. – 4:00 a.m. daily "
                                 "(occasional maintenance downtime).",
    "enrollment_window_concept": "There are TWO phases: (1) Enrollment by Window — "
                                 "students get staggered windows by priority; "
                                 "(2) Open Enrollment — everyone gets access "
                                 "simultaneously. Both phases' dates are in the "
                                 "Quarterly Academic Calendar.",
    "window_48hr_priority":      "When your enrollment window opens, you get 48 "
                                 "HOURS OF UNRESTRICTED ACCESS before being limited "
                                 "to non-prime-time hours.",
    "non_prime_time":            "After the 48-hour priority window: WebReg is open "
                                 "only 7:00 p.m. – 7:00 a.m. (no daytime access). "
                                 "This restriction lifts at Open Enrollment.",
    "find_your_window":          "Continuing students see their enrollment window in "
                                 "StudentAccess starting the SEVENTH week of the "
                                 "current quarter.",

    # Unit caps within WebReg (cross-reference UNIT_LIMITS for the
    # canonical Senate-level rule).
    "ug_cap_pre_fee_deadline":   "18.0 units. Hard ceiling enforced by WebReg until "
                                 "WebReg reopens after the fee-payment deadline.",
    "ug_cap_post_fee_deadline":  "20.0 units. >20.1 requires academic dean approval. "
                                 "<12 also requires dean approval (part-time status).",
    "grad_cap":                  "16 units total (graduate + upper-div combined); "
                                 ">16 requires Graduate Advisor approval in advance.",
    "waitlist_units_count":      "Units from waitlisted classes COUNT toward the "
                                 "18/20 unit max. They do NOT count toward Minimum "
                                 "Required Units (MRU) for financial aid.",

    # Status / hold behavior
    "holds_block_enroll":        "Holds block enrollment and adds. WebReg notifies "
                                 "you on login. Contact the office that placed the "
                                 "hold immediately.",
    "holds_auto_drop":           "If a hold remains on your record at 5:00 p.m. on "
                                 "the fee-payment deadline, your classes are "
                                 "AUTOMATICALLY DROPPED.",
    "late_enrollment_fee":       "$50 late enrollment charge if you are enrolled in "
                                 "ZERO units OR if you enroll after the end of the "
                                 "second week of instruction.",

    # Loss of student status (cross-ref STUDENT_STATUS_LOSS)
    "absolute_final_deadline":   "Friday 5:00 p.m. at the end of the THIRD week of "
                                 "instruction — final deadline to pay fees AND/OR "
                                 "enroll. Miss it → loss of student status. (Note: "
                                 "the WebReg help page says 4:30 p.m.; the official "
                                 "Loss of Student Status page says 5:00 p.m. — "
                                 "treat 4:30 as the safe target.)",

    # Status column meanings
    "status_open":               "Seats available; you may enroll.",
    "status_full":               "No seats AND waitlist (if any) is full.",
    "status_waitl":              "No seats, but the waitlist has room — you may "
                                 "join the waitlist.",
    "status_newonly":            "Seats are reserved for new students (freshmen / "
                                 "first-quarter transfers). The 'Nor' column shows "
                                 "how many of the seats are reserved.",

    # Co-class + waitlist interaction (see ENROLLMENT_RULES / CO_CLASSES too)
    "co_class_same_session":     "If you tentatively enroll in a Lec that requires a "
                                 "Dis/Lab, you must successfully ADD the Dis/Lab in "
                                 "the SAME WebReg session, or you are automatically "
                                 "dropped from the Lec.",
    "co_class_all_dis_full":     "If ALL Discussion / Lab sections of a Lec are "
                                 "full, the Lec itself becomes unenrollable AND "
                                 "unwaitlistable.",

    # Misc operational
    "summer_session_separate":   "Summer Session enrolls through the Summer Session "
                                 "website, NOT WebReg.",
    "email_official":            "UCI email is the OFFICIAL notice channel — check "
                                 "daily during the first two weeks of every quarter.",
    "mailbox_full_warning":      "A full inbox blocks delivery of official messages.",

    "cite": "UCI Registrar — WebReg help "
            "(https://www.reg.uci.edu/registrar/soc/webreg.html); "
            "IR 445 for unit caps; IR 440 for add/drop windows.",
}

# ── Co-class mechanics (operational detail beyond ENROLLMENT_RULES) ───
# https://www.reg.uci.edu/enrollment/course_enrollment/co-classes.html
# ENROLLMENT_RULES has the high-level "Lec+Dis required" rule;
# CO_CLASSES has the WebReg mechanics (tentative enrollment, auto-drop
# on logout, switching, primary/secondary unit values, group matching).
CO_CLASSES = {
    "definition":                "A 'co-class' is a course consisting of multiple "
                                 "components (e.g. Lecture + Discussion, or "
                                 "Lecture + Lab) that MUST be taken concurrently. "
                                 "The PRIMARY component carries all the units; "
                                 "SECONDARY components carry 0.0 units.",
    "section_grouping":          "You must enroll in co-classes that are GROUPED "
                                 "TOGETHER in the Schedule of Classes (e.g. Lec A "
                                 "→ Dis A1/A2/A3). Mixing groups (Lec A + Dis B2) "
                                 "is rejected — you'll get an error message.",
    "same_session_required":     "Both the primary AND a secondary must be added in "
                                 "the SAME WebReg session. Adding only one half puts "
                                 "you in TENTATIVE status until the other is added.",
    "tentative_status":          "If you add only the primary OR only the secondary, "
                                 "you are 'tentatively enrolled' — but if you log "
                                 "out without completing the pair, BOTH are dropped.",
    "auto_logout_drops_pair":    "Co-classes are dropped if WebReg auto-logs-you-out "
                                 "for ANY reason: period of inactivity, closing the "
                                 "browser without logging out, minimizing the WebReg "
                                 "window.",
    "all_secondaries_full":      "If ALL secondary sections are full, the primary "
                                 "becomes unenrollable AND unwaitlistable — there's "
                                 "no way to complete the pair.",
    "switch_secondary":          "Switching to a different secondary (e.g. moving "
                                 "from Dis A1 to Dis A2 within the same Lec) is "
                                 "called a 'Switch' transaction. Through week 2 it's "
                                 "done in WebReg; after week 2 it requires an "
                                 "Enrollment Exception Request in Student Access.",
    "switch_via_webreg_only":    "Switching components of a co-class through WebReg "
                                 "is NOT permitted; use Enrollment Exception. "
                                 "Only zero-unit secondaries linked to a unit-bearing "
                                 "primary are switchable this way.",
    "change_primary":            "To switch to a different PRIMARY (e.g. Lec A → "
                                 "Lec B), you must first DROP all existing co-class "
                                 "components, then enroll in the new primary + a "
                                 "new matching secondary.",
    "cite": "UCI Registrar — Co-classes "
            "(https://www.reg.uci.edu/enrollment/course_enrollment/co-classes.html)",
}

# ── Course restriction codes (Schedule of Classes 'Rstr' column) ────
# https://www.reg.uci.edu/enrollment/restrict_codes.html
# Each code is one letter. The data layer surfaces a section's
# restriction codes as a string like "AB" or "EJL" — multiple codes
# stack. RESTRICTION_CODES["meanings"] gives the human-readable
# expansion. RESTRICTION_CODES["class_level_codes"] is the subset
# the dispatcher uses for hard filtering against the student's units.
RESTRICTION_CODES = {
    "intro":                     "Course restrictions are determined and placed by "
                                 "the offering department; the department also "
                                 "decides when restrictions are removed. Courses "
                                 "may stay restricted through all enrollment "
                                 "periods. Check the 'Rstr' column in the Schedule "
                                 "of Classes for each section.",
    "meanings": {
        "A": "Prerequisite required — a prerequisite course must be completed first.",
        "B": "Authorization code required — get a 4-digit code from instructor / "
             "department; one code may be used for multiple transactions.",
        "C": "Fee required — additional fee beyond registration; auto-billed to "
             "ZOTAccount (typically end of first week).",
        "D": "Pass/Not Pass option only — course offers only P/NP grading.",
        "E": "Freshmen only — 0–44.9 units.",
        "F": "Sophomores only — 45.0–89.9 units.",
        "G": "Lower-division only — 0–89.9 units.",
        "H": "Juniors only — 90.0–134.9 units.",
        "I": "Seniors only — 135+ units.",
        "J": "Upper-division only — 90+ units.",
        "K": "Graduate only.",
        "L": "Major only — open only to specific majors authorized by the department.",
        "M": "Non-major only — open only to students outside the offering department.",
        "N": "School major only — open to majors within the offering school.",
        "O": "Non-school major only — open to students outside the offering school.",
        "R": "Biomedical Pass/Fail course — School of Medicine only.",
        "S": "Satisfactory/Unsatisfactory only — course offers only S/U grading.",
        "X": "Separate authorization codes required — a unique 4-digit code is "
             "needed for EACH transaction (add, drop, change).",
    },
    "class_level_codes": {
        # code → (min_units_inclusive, max_units_exclusive_or_None).
        # Used by the dispatcher to hard-filter cards that the student
        # literally cannot enroll into because of their class standing.
        "E": (0,    45.0),                          # Freshmen
        "F": (45.0, 90.0),                          # Sophomores
        "G": (0,    90.0),                          # Lower-div
        "H": (90.0, 135.0),                         # Juniors
        "I": (135.0, None),                         # Seniors
        "J": (90.0, None),                          # Upper-div
        # K (graduate) and E-only undergrad-vs-grad split is handled
        # separately because we don't yet track UG-vs-grad on the
        # frontend selector — leave K as "warning chip" not hard-block.
    },
    "auth_required_codes":       ("B", "X"),
    "major_codes":               ("L", "M", "N", "O"),
    "grading_codes":             ("D", "R", "S"),    # forced grading basis
    "fee_codes":                 ("C",),
    "prereq_codes":              ("A",),
    "cite": "UCI Registrar — Course Restriction Codes "
            "(https://www.reg.uci.edu/enrollment/restrict_codes.html)",
}

# ── Loss of Student Status (registrar's Student Status page) ──────
# https://www.reg.uci.edu/enrollment/studentstatus.html
STUDENT_STATUS_LOSS = {
    "final_deadline":            "Friday 5:00 p.m. at the end of the THIRD week of "
                                 "instruction. This is the absolute last day to "
                                 "(a) pay fees AND (b) enroll in at least one class. "
                                 "Miss either → automatic loss of student status.",
    "consequences": [
        "Deassessment (your fees / registration are wiped, EVEN IF you'd already paid)",
        "Loss of all credit for the quarter — no course credit awarded",
        "Loss of all student privileges: financial aid, housing, library access, "
        "student fees-funded services",
        "Ineligibility for the Reduced Fee Part-Time Study Program for that quarter",
    ],
    "reinstatement_paths": {
        "new_undergrad":         "Contact the Office of Undergraduate Admissions.",
        "continuing_undergrad":  "Contact your academic advising office to initiate "
                                 "readmission + pay a $70 application fee.",
        "graduate":              "Obtain approvals from your school's Associate Dean "
                                 "AND the Dean of the Graduate Division, plus pay "
                                 "the appropriate application fee.",
    },
    "all_reinstatement_must":    "Pay FULL-TIME registration fees AND enroll via "
                                 "Enrollment Exceptions (under 'Applications' in "
                                 "Student Access).",
    "cite": "UCI Registrar — Loss of Student Status "
            "(https://www.reg.uci.edu/enrollment/studentstatus.html)",
}

# ── Enrollment mechanics (WebReg / section pairing) ──────
ENROLLMENT_RULES = {
    "lec_plus_secondary_required": (
        "Most UCI courses with a separate Lecture and Discussion/Lab "
        "section require enrolling in BOTH a Lec code AND a paired "
        "Dis (or Lab) code. Adding only the Lec — or only the Dis — "
        "on WebReg is NOT a complete enrollment. The registrar will "
        "reject a schedule that's missing one of the paired sections."
    ),
    "single_section_courses": (
        "Courses listed with ONLY a Lec (no Dis/Lab) — typical for "
        "writing courses (e.g. I&C SCI 139W), seminars, and many "
        "upper-division electives — need only the Lec code."
    ),
    "pairing_convention": (
        "Discussion / Lab sections are organized into groups tied to "
        "a specific Lec. The section-letter pattern is the cue: Lec A "
        "pairs with Dis A1, A2, A3...; Lec B pairs with B1, B2, B3...; "
        "etc. Pick a Dis whose letter group matches the Lec you took."
    ),
    "lab_vs_discussion": (
        "'Lab' and 'Dis' are both 'secondary' sections from the "
        "enrollment system's perspective — same pairing rule applies. "
        "Some courses also use 'Stu' (studio, mostly arts), 'Act' "
        "(activity), 'Tut' (tutorial); treat these as Dis-equivalent "
        "for pairing purposes."
    ),
    "studio_only_courses": (
        "Studio-only courses (some arts / dance / drama) may list "
        "ONLY a Stu section — no Lec. In that case, enrolling in the "
        "Stu alone is complete."
    ),
    "late_secondary_add": (
        "If a student enrolls in the Lec but the matching Dis was full, "
        "they must add a Dis section by IR 440's add deadline (week 1-2 "
        "free; weeks 3-6 with offering-dean + major-dean permission). "
        "Otherwise the Lec enrollment may be dropped by the registrar."
    ),
    "cite": "UCI Registrar enrollment guidelines + IR 440 (add/drop windows)",
}

# ── Course value (informational, mostly for the LLM) ─────
COURSE_VALUE = {
    "default_units_per_course":   4,                # IR 435: unit equivalency
    "credit_assignment_rule":     "Students may NOT receive upper-division credit for "
                                  "a lower-division course or graduate credit for an "
                                  "upper-division course just by doing extra work.",
    "credit_by_exam":             "Permitted only with the consent of the offering "
                                  "instructor (who decides format and whether grade "
                                  "is letter or P/NP) AND the dean's approval. "
                                  "Available only once per course; student may accept "
                                  "or reject the grade.",
    "cite": "IR 360 (credit by exam), IR 370 (credit assignment), IR 435 (course value)",
}

# ── Academic calendar ────────────────────────────────────
# Per-quarter date schema:
#   quarter_begin       — first official day of the term (often a weekend)
#   instruction_begin   — first day of class
#   free_window_end     — last day to add/drop/change grading or units
#                         without academic approval (~ end of Week 2,
#                         done in WebReg)
#   late_add_drop_end   — last day to add OR drop with dean approval
#                         (~ end of Week 6, via Enrollment Exceptions
#                         in Student Access). Adds and drops share the
#                         same date even though the registrar only
#                         publishes the drop date explicitly.
#   change_grading_end  — last day to change the grading option
#                         (~ end of Week 10 / last day of instruction,
#                         via Enrollment Exceptions)
#   instruction_end     — last day of regular class
#   finals_begin /
#   finals_end          — final-examination window
#   quarter_end         — last official day of the term
#
# Source: https://www.reg.uci.edu/navigation/calendars.html
# (quarterly subpages per academic year). Last refreshed 2026-05-30.
ACADEMIC_CALENDAR = {
    "2025-2026": {
        "fall": {
            "quarter_begin":      "2025-09-22",
            "instruction_begin":  "2025-09-25",
            "free_window_end":    "2025-10-10",
            "late_add_drop_end":  "2025-11-07",
            "change_grading_end": "2025-12-05",
            "instruction_end":    "2025-12-05",
            "finals_begin":       "2025-12-06",
            "finals_end":         "2025-12-12",
            "quarter_end":        "2025-12-12",
        },
        "winter": {
            "quarter_begin":      "2026-01-02",
            "instruction_begin":  "2026-01-05",
            "free_window_end":    "2026-01-16",
            "late_add_drop_end":  "2026-02-13",
            "change_grading_end": "2026-03-13",
            "instruction_end":    "2026-03-13",
            "finals_begin":       "2026-03-14",
            "finals_end":         "2026-03-20",
            "quarter_end":        "2026-03-20",
        },
        "spring": {
            "quarter_begin":      "2026-03-25",
            "instruction_begin":  "2026-03-30",
            "free_window_end":    "2026-04-10",
            "late_add_drop_end":  "2026-05-08",
            "change_grading_end": "2026-06-05",
            "instruction_end":    "2026-06-05",
            "finals_begin":       "2026-06-06",
            "finals_end":         "2026-06-11",
            "quarter_end":        "2026-06-12",
        },
        "summer": {
            # Sessions don't follow the standard 10-week cadence; only
            # the term envelopes are listed by the registrar.
            "session_1": ("2026-06-22", "2026-07-29"),
            "session_2": ("2026-08-03", "2026-09-09"),
            "ten_week":  ("2026-06-22", "2026-08-28"),
        },
    },
    "2026-2027": {
        "fall": {
            "quarter_begin":      "2026-09-21",
            "instruction_begin":  "2026-09-24",
            "free_window_end":    "2026-10-09",
            "late_add_drop_end":  "2026-11-06",
            "change_grading_end": "2026-12-04",
            "instruction_end":    "2026-12-04",
            "finals_begin":       "2026-12-05",
            "finals_end":         "2026-12-11",
            "quarter_end":        "2026-12-11",
        },
        "winter": {
            "quarter_begin":      "2027-01-04",
            "instruction_begin":  "2027-01-04",
            "free_window_end":    "2027-01-15",
            "late_add_drop_end":  "2027-02-12",
            "change_grading_end": "2027-03-12",
            "instruction_end":    "2027-03-12",
            "finals_begin":       "2027-03-13",
            "finals_end":         "2027-03-19",
            "quarter_end":        "2027-03-19",
        },
        "spring": {
            "quarter_begin":      "2027-03-24",
            "instruction_begin":  "2027-03-29",
            "free_window_end":    "2027-04-09",
            "late_add_drop_end":  "2027-05-07",
            "change_grading_end": "2027-06-04",
            "instruction_end":    "2027-06-04",
            "finals_begin":       "2027-06-05",
            "finals_end":         "2027-06-10",
            "quarter_end":        "2027-06-11",
        },
    },
}


# ── Calendar helpers (used by hard-rule enforcement) ─────

def parse_term_label(term: str) -> Optional["tuple[str, str]"]:
    """
    Normalise a human term label like 'Fall 2026' / 'spring2026' /
    'WINTER 2027' into ('2026-2027', 'fall'). Returns None on miss so
    callers can degrade gracefully (don't crash the agent loop just
    because the model wrote 'Autumn 2026').
    """
    if not term:
        return None
    parts = term.replace("_", " ").split()
    season = year = None
    for p in parts:
        p_low = p.lower()
        if p_low.startswith(("fall", "autumn")):
            season = "fall"
        elif p_low.startswith("winter"):
            season = "winter"
        elif p_low.startswith("spring"):
            season = "spring"
        elif p_low.startswith("summer"):
            season = "summer"
        elif p.isdigit() and len(p) == 4:
            year = int(p)
    if not (season and year):
        return None
    # Fall belongs to the academic year starting THAT year (e.g. Fall 2026
    # → 2026-2027); Winter and Spring belong to the previous starting year
    # (Winter 2027 → 2026-2027).
    ay_start = year if season == "fall" else year - 1
    return (f"{ay_start}-{ay_start + 1}", season)


def get_term_calendar(term: str) -> Optional[dict]:
    """Return the calendar block for a term label, or None on miss."""
    parsed = parse_term_label(term)
    if not parsed:
        return None
    ay, season = parsed
    return (ACADEMIC_CALENDAR.get(ay) or {}).get(season)


def is_term_past_deadline(term: str, today: "Optional[str]" = None,
                          deadline_key: str = "late_add_drop_end") -> Optional[bool]:
    """
    Has the given term already passed the named deadline?

    `today` is an ISO YYYY-MM-DD string; if omitted we use date.today().
    Returns None on lookup miss (unknown term, missing deadline) so the
    caller can decide whether None = "don't know, allow" or
    "don't know, block".

    Used by the propose_recommendation dispatcher (hard block) and by
    the frontend card footer (passive display).
    """
    cal = get_term_calendar(term)
    if not cal or deadline_key not in cal:
        return None
    if today is None:
        from datetime import date as _date
        today = _date.today().isoformat()
    return today > cal[deadline_key]

# ── Sources (for citation by the LLM) ────────────────────
SOURCES = {
    "academic_regulations":     "https://catalogue.uci.edu/informationforadmittedstudents/academicregulationsandprocedures/",
    "registration":             "https://catalogue.uci.edu/informationforadmittedstudents/registrationandotherprocedures/",
    "bachelor_requirements":    "https://catalogue.uci.edu/informationforadmittedstudents/requirementsforabachelorsdegree/",
    "academic_calendar":        "https://catalogue.uci.edu/academiccalendar/academiccalendar.pdf",
    "registrar_calendar":       "https://www.reg.uci.edu/navigation/calendars.html",
    "ics_policies":             "https://ics.uci.edu/academics/undergrad/ics-course-enrollment-policies/",
    "extra_units_petition":     "https://uu.uci.edu/current-students/policies-and-procedures/extra-units-petition-process/",
    "senate_manual":            "https://senate.uci.edu/divisional-administration/manual-of-the-irvine-division/",
    "academic_integrity":       "https://aisc.uci.edu/",
    "webreg":                   "https://www.reg.uci.edu/registrar/soc/webreg.html",
    "co_classes":               "https://www.reg.uci.edu/enrollment/course_enrollment/co-classes.html",
    "restriction_codes":        "https://www.reg.uci.edu/enrollment/restrict_codes.html",
    "student_status":           "https://www.reg.uci.edu/enrollment/studentstatus.html",
}
