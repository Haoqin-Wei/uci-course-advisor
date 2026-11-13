"""Render recommendation facts without model-written claims or additional I/O.

This module does not rank, retrieve, forecast grades, or infer degree progress.
Only the actual selected lecture and explicitly scoped source statistics enter
the controlled answer. Original grade payloads and Python scores are preserved.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import math
import re
from urllib.parse import urlsplit

from app.catalog.normalization import normalize_instructor, parse_course_mention
from app.recommendations.features import build_candidate_features
from app.recommendations.filter_plan import normalize_ge_category, trusted_ge_audit
from app.terms import parse_term_key
from app.data.ge_evidence import verified_ge_provenance
from app.scheduling.service import section_meetings


PRESENTATION_VERSION = 1
_LETTERS = ("A", "B", "C", "D", "F", "P", "NP")


def _mapping(value):
    return value if isinstance(value, Mapping) else {}


def _number(value, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError):
        return None
    if not finite or value < minimum or (maximum is not None and value > maximum):
        return None
    return value


def _count(value):
    number = _number(value)
    return int(number) if number is not None and int(number) == number else None


def _course(value):
    ref = parse_course_mention(value) if isinstance(value, str) else None
    return ref.display() if ref else None


def _term(value):
    parsed = parse_term_key(value) if isinstance(value, str) else None
    return parsed.terms[0].canonical_name if parsed and parsed.kind == "single" else None


def _name(value):
    return re.sub(r"[.\s]+", "", normalize_instructor(value or "")) if isinstance(value, str) else ""


def _without_model_reason(value):
    if isinstance(value, Mapping):
        return {key: _without_model_reason(item) for key, item in value.items()
                if key not in {"model_reason", "_model_reason", "model_rationale"}}
    if isinstance(value, (list, tuple)):
        return [_without_model_reason(item) for item in value]
    return deepcopy(value)


def _warnings(card):
    warnings = _mapping(card.get("prefilter")).get("warnings") or []
    return {warning.get("code") for warning in warnings
            if isinstance(warning, Mapping) and isinstance(warning.get("code"), str)}


def normalize_ge_evidence(card):
    """Verify course tags independently of a student's authorized degree audit.

    A singular legacy ``ge_category=None`` is not evidence of a non-GE course.
    Complete, source-attributed canonical lists may validly contain many labels
    or no labels. Unknown labels never become confirmed counts or badges.
    """
    card = _mapping(card)
    categories = card.get("ge_categories")
    provenance = _mapping(card.get("ge_provenance"))
    identity_ok = (bool(_course(card.get("course_id")))
                   and _course(card.get("ge_evidence_course_id")) == _course(card.get("course_id"))
                   and bool(_term(card.get("term")))
                   and _term(card.get("ge_evidence_term")) == _term(card.get("term")))
    source_known = any(isinstance(provenance.get(key), str) and provenance[key].strip()
                       for key in ("source", "source_file", "loader", "url", "source_url"))
    provenance_terms_match = all(_term(provenance[key]) == _term(card.get("term"))
                                 for key in ("source_term", "target_term") if key in provenance)
    normalized = ([normalize_ge_category(value) for value in categories]
                  if isinstance(categories, (list, tuple)) else None)
    verified = (card.get("ge_labels_verified") is True and identity_ok and source_known and provenance_terms_match
                and verified_ge_provenance(provenance, card.get("term"))
                and provenance.get("is_historical_proxy") is not True
                and "ge_labels_unknown" not in _warnings(card)
                and normalized is not None and all(value is not None for value in normalized))
    labels = list(dict.fromkeys(normalized)) if verified else []
    personal = {"status": "unknown", "unmet_categories": [], "projected_categories": [],
                "remaining_categories": [], "source": None}
    audit = trusted_ge_audit({"ge_audit": card.get("personal_ge_audit")})
    if verified and audit is not None:
        unmet = set(labels) & set(audit["unmet_categories"])
        projected = unmet & set(audit.get("projected_categories", []))
        personal = {"status": "verified", "unmet_categories": sorted(unmet),
                    "projected_categories": sorted(projected), "remaining_categories": sorted(unmet - projected),
                    "source": audit["source"]}
    return {"version": PRESENTATION_VERSION, "status": "verified" if verified else "unknown",
            "categories": labels, "source": deepcopy(dict(provenance)) if source_known else None,
            "is_ge": bool(labels) if verified else None, "personal_contribution": personal}


def _selected_lecture(card):
    code = card.get("primary_code")
    if code is None:
        return {}
    for group in card.get("section_groups") or []:
        primary = _mapping(_mapping(group).get("primary"))
        if str(primary.get("code") or primary.get("section_code")) == str(code):
            return primary
    return next((section for section in card.get("sections") or [] if isinstance(section, Mapping)
                 and str(section.get("section_code") or section.get("code")) == str(code)), {})


def _section_fact(section):
    if not section:
        return None
    names = section.get("instructors")
    fact = {"code": section.get("code") or section.get("section_code"),
            "num": section.get("num") or section.get("section_num"),
            "type": section.get("type") or section.get("section_type"),
            "instructors": [name for name in names if isinstance(name, str)] if isinstance(names, list) else [],
            "days": section.get("days"),
            "time": section.get("time") or section.get("time_display") or (
                f"{section['start_time']}–{section['end_time']}"
                if section.get("start_time") and section.get("end_time") else None)}
    if "meetings" in section:
        fact["meetings"] = deepcopy(section["meetings"])
    if section.get("meetings") is not None:
        # The flat compatibility fields describe only the first meeting. Build
        # the displayed schedule from every authoritative row, including TBA.
        meetings = section_meetings(section)
        fact["meeting_schedule"] = [{key: deepcopy(meeting.get(key)) for key in (
            "days", "start_time", "end_time", "location", "time_is_tba")}
            for meeting in meetings]
        arrangements = []
        for meeting in meetings:
            clock = (f"{meeting['start_time']}–{meeting['end_time']}"
                     if not meeting["time_is_tba"] else "TBA")
            arrangement = " ".join(value for value in (meeting.get("days"), clock) if value)
            if meeting.get("location"):
                arrangement += f" ({meeting['location']})"
            arrangements.append(arrangement)
        fact["days"] = None
        fact["time"] = "; ".join(arrangements)
    return fact


def _ranking(card, selected):
    raw = _mapping(selected.get("ranking") or card.get("ranking"))
    score = _number(raw.get("final_score"), maximum=100)
    features = _mapping(raw.get("features"))
    weights = _mapping(raw.get("effective_weights"))
    names = {_name(name) for name in selected.get("instructors") or [] if isinstance(name, str)}
    lecturer = str(selected.get("type") or selected.get("section_type") or "").lower().startswith(("lec", "sem"))
    used = {}
    for key in ("grade_a_rate", "professor_rating"):
        weight = _number(weights.get(key), maximum=1)
        if weight is None:
            return {"status": "unknown", "final_score": None, "effective_weights": {}}
        feature = _mapping(features.get(key))
        if weight:
            instructor = feature.get("instructor_name")
            if (feature.get("valid") is not True or not selected or not lecturer
                    or (instructor and _name(instructor) not in names)
                    or (feature.get("course_id") and _course(feature["course_id"]) != _course(card.get("course_id")))):
                return {"status": "unknown", "final_score": None, "effective_weights": {}}
            used[key] = weight
    valid = score is not None and bool(used) and math.isclose(sum(used.values()), 1, abs_tol=1e-8)
    return {"status": "available" if valid else "unknown", "final_score": score if valid else None,
            "effective_weights": dict(weights) if valid else {}, "score_version": raw.get("score_version"),
            "features": {key: {field: deepcopy(feature.get(field)) for field in (
                "value", "sample_count", "scope", "instructor_name", "source", "source_url", "valid")}
                for key, feature in features.items() if key in {"grade_a_rate", "professor_rating"}
                and isinstance(feature, Mapping)}}


def _grade_snapshot(history, card, selected, *, course_only=False):
    history = _mapping(history)
    grades = _mapping(history.get("grades"))
    scope = history.get("scope") or grades.get("scope")
    status = history.get("status")
    if history.get("found") is not True or status != "ok" or str(history.get("source", "")).lower() != "zotistics":
        return None
    if scope not in ({"course"} if course_only else {"course_instructor", "instructor"}):
        return None
    if scope in {"course", "course_instructor"} and (
            not _course(card.get("course_id")) or _course(grades.get("course_id")) != _course(card.get("course_id"))):
        return None
    data = deepcopy(dict(history))
    data["grades"] = dict(grades)
    if grades.get("course_id"):
        data["grades"]["course_id"] = _course(grades["course_id"])
    if data.get("requested_course_id"):
        data["requested_course_id"] = _course(data["requested_course_id"])
    if scope == "instructor" and isinstance(data.get("pair_query"), Mapping):
        data["query"] = dict(data["pair_query"])
    instructor = history.get("instructor") or grades.get("instructor_name") or history.get("instructor_name")
    # Use the existing pure scope/identity validator; no name guessing or I/O.
    feature = build_candidate_features(data, course_id=_course(card.get("course_id")),
                                       instructor_name=instructor, primary_section=selected or None)["grade_a_rate"]
    if feature.get("missing_reason") not in {None, "nonpositive_sample_count"}:
        return None
    if scope != "course" and not selected:
        return None
    counts = _mapping(grades.get("letter_counts"))
    checked = {letter: _count(counts.get(letter)) for letter in _LETTERS}
    if any(value is None for value in checked.values()):
        return None
    graded = sum(checked[letter] for letter in _LETTERS[:5])
    samples = sum(checked.values())
    if _count(grades.get("graded")) != graded or _count(grades.get("samples")) != samples:
        return None
    coverage = _mapping(history.get("coverage") or grades.get("coverage"))
    terms = grades.get("terms", coverage.get("terms"))
    terms = list(dict.fromkeys(_term(term) for term in terms if _term(term))) if isinstance(terms, list) else []
    query = _mapping(history.get("query"))
    return {"status": "ok", "source": "Zotistics", "source_url": history.get("source_url"),
            "scope": scope, "course_id": _course(grades.get("course_id")),
            "instructor_name": grades.get("instructor_name") or history.get("resolved_instructor_name"),
            "all_departments": scope == "instructor" and not query.get("department"),
            "letter_counts": checked, "graded": graded, "samples": samples,
            "pct_A": checked["A"] / graded * 100 if graded else None,
            "site_pct_A": _number(_mapping(grades.get("site_percentages")).get("A"), maximum=100),
            "avg_gpa": _number(grades.get("avg_gpa"), maximum=4),
            "source_reported_gpa": _number(grades.get("source_reported_gpa"), maximum=4),
            "class_count": _count(grades.get("class_count", coverage.get("class_count"))),
            "terms": terms, "term_range": deepcopy(grades.get("term_range") or coverage.get("term_range")),
            "last_grade_update": coverage.get("last_grade_update")}


def _grade_evidence(card, selected):
    out = []
    for history in selected.get("grade_history") or []:
        entry = _grade_snapshot(history, card, selected)
        if entry is not None and not any((item["scope"], item["instructor_name"]) ==
                                         (entry["scope"], entry["instructor_name"]) for item in out):
            out.append(entry)
    overall = _grade_snapshot(card.get("zotistics_grades"), card, selected, course_only=True)
    if overall is not None:
        out.append(overall)
    return out


def _selection_evidence(card, selected, pairing):
    from app.recommendations.pairing import verify_selection
    previous = _mapping(card.get("selection_verification"))
    requested = previous.get("selected_secondary_codes", [])
    requested = requested if isinstance(requested, list) else []
    requested = list(dict.fromkeys(code for code in requested if isinstance(code, str)))
    same_primary = str(previous.get("primary_code")) == str(card.get("primary_code"))
    chosen = [selected] if selected else []
    available = [section for section in card.get("sections") or [] if isinstance(section, Mapping)]
    for group in card.get("section_groups") or []:
        available.extend(section for section in _mapping(group).get("secondaries", []) if isinstance(section, Mapping))
    by_code = {str(section.get("section_code") or section.get("code")): section for section in available}
    if same_primary:
        chosen.extend(by_code[code] for code in requested if code in by_code)
    result = verify_selection(pairing, chosen, primary_code=card.get("primary_code"))
    if same_primary and any(code not in by_code for code in requested):
        result.update(status="unknown", reason_codes=["selected_section_evidence_missing"],
                      selected_secondary_codes=requested)
    return result


def normalize_recommendation_card(card, language="zh"):
    """Return a safe new card; model rationales never enter the public result."""
    out = _without_model_reason(_mapping(card))
    ge = normalize_ge_evidence(out)
    out["ge_evidence"] = ge
    out["ge_categories"] = ["GE-" + label for label in ge["categories"]]
    out["ge_category"] = " / ".join(out["ge_categories"]) or None
    out["ge_labels_verified"] = ge["status"] == "verified"
    selected = _selected_lecture(out)
    ranking = _ranking(out, selected)
    from app.recommendations.pairing import normalize_pairing_evidence, pairing_for_primary
    pairing = normalize_pairing_evidence(out, term=out.get("term"))
    out["pairing"] = pairing
    pairing_selected = pairing_for_primary(pairing, out.get("primary_code"))
    out["pairing_selected"] = pairing_selected
    selection = _selection_evidence(out, selected, pairing_selected)
    out["selection_verification"] = selection
    out["presentation"] = {"version": PRESENTATION_VERSION,
                           "language": "zh" if str(language).startswith("zh") else "en",
                           "selected_lecture": _section_fact(selected), "ge": deepcopy(ge),
                           "pairing": deepcopy(pairing_selected), "ranking": ranking,
                           "selection_verification": deepcopy(selection),
                           "grade_evidence": _grade_evidence(out, selected),
                           "comparison_baseline": "not_established"}
    out["presentation_version"] = PRESENTATION_VERSION
    zh = str(language).startswith("zh")
    if ranking["final_score"] is not None:
        out["reason"] = ((f"历史证据推荐分 {ranking['final_score']:.1f}/100；修读条件另行核实。") if zh else
                         f"Historical-evidence score {ranking['final_score']:.1f}/100; verify enrollment conditions.")
    elif ge["status"] == "verified" and ge["categories"]:
        labels = " / ".join(ge["categories"])
        out["reason"] = (f"课程 GE 类别已核实：{labels}；个人抵扣另行核实。" if zh else
                         f"Verified course GE categories: {labels}; verify personal degree credit separately.")
    else:
        out["reason"] = ("课程候选；历史证据、GE 类别与修读条件另行核实。" if zh else
                         "Course candidate; verify historical evidence, GE categories and enrollment conditions.")
    return out


def _text(value):
    """Escape source labels as Markdown data, not instructions or markup."""
    return re.sub(r"([\\`*_{}\[\]<>|])", r"\\\1", str(value)).replace("\n", " ").replace("\r", " ")


def _source(entry):
    url = entry.get("source_url")
    if isinstance(url, str) and urlsplit(url).scheme == "https" and urlsplit(url).netloc == "zotistics.com":
        return "[Zotistics](https://zotistics.com/)"
    return "Zotistics"


def _grade_line(entry, zh):
    scope = entry["scope"]
    name = _text(entry.get("instructor_name") or "")
    if scope == "course_instructor":
        label = f"本课＋教授历史（{name}）" if zh else f"Course–instructor history ({name})"
    elif scope == "instructor":
        label = (f"教授跨课程历史汇总（{name}；非本课成绩" + ("，全部院系查询" if entry["all_departments"] else "") + "）") if zh else (
            f"Instructor-wide, cross-course history ({name}; not this course's grades" +
            ("; all-department query" if entry["all_departments"] else "") + ")")
    else:
        label = "课程整体历史" if zh else "Course-wide history"
    a, n = entry["letter_counts"]["A"], entry["graded"]
    pct = entry["pct_A"]
    stat = (f"A {pct:.1f}%（{a}/{n}，A–F 记录）" if zh else f"A {pct:.1f}% ({a}/{n} A–F records)") if pct is not None else (
        "没有 A–F 记录，A 率未知" if zh else "No A–F records; A rate unknown")
    details = [stat, f"{_source(entry)}"]
    if entry["class_count"] is not None:
        details.append(f"{entry['class_count']} 个班次记录" if zh else f"{entry['class_count']} class records")
    if entry["terms"]:
        details.append(f"{len(entry['terms'])} 个不同学期" if zh else f"{len(entry['terms'])} distinct terms")
    term_range = _mapping(entry.get("term_range"))
    if _term(term_range.get("first")) and _term(term_range.get("last")):
        details.append(f"{_text(term_range['first'])}–{_text(term_range['last'])}")
    details.append((f"总记录 {entry['samples']}（含 P/NP）" if zh else f"{entry['samples']} total records including P/NP"))
    if entry["site_pct_A"] is not None:
        details.append((f"原站 A 占比 {entry['site_pct_A']:.1f}%（含 P/NP 分母）" if zh else
                        f"Site A share {entry['site_pct_A']:.1f}% (denominator includes P/NP)"))
    if entry["avg_gpa"] is not None:
        details.append((f"估算 GPA {entry['avg_gpa']:.3f}（无 ±，A–F）" if zh else
                        f"Estimated GPA {entry['avg_gpa']:.3f} (no +/−, A–F only)"))
    if entry["source_reported_gpa"] is not None:
        details.append((f"原站报告 GPA {entry['source_reported_gpa']:.3f}（计算口径未核实）" if zh else
                        f"Source-reported GPA {entry['source_reported_gpa']:.3f} (method unverified)"))
    if entry["last_grade_update"]:
        details.append(("来源最后更新 " if zh else "Source last updated ") + _text(entry["last_grade_update"]))
    return label + "：" + "；".join(details) + "。" if zh else label + ": " + "; ".join(details) + "."


def _ge_source_label(ge, zh):
    source = _mapping(ge.get("source"))
    # Internal loader names and source-file paths are provenance, not user-facing labels.
    url = source.get("source_url") or source.get("url")
    if isinstance(url, str):
        parsed = urlsplit(url)
        if parsed.scheme == "https" and parsed.hostname in {"catalogue.uci.edu", "catalog.uci.edu"}:
            label = "UCI 官方课程目录" if zh else "Official UCI course catalogue"
            return f"[{label} {source['catalogue_year']}]({url})"
        if parsed.scheme == "https" and parsed.hostname == "reg.uci.edu":
            return "UCI Registrar 官方课程来源" if zh else "Official UCI Registrar course source"
    return "课程目录的已核实 GE 字段" if zh else "Verified course-catalogue GE fields"


def _blocked_recommendation_text(gate, zh):
    raw_reasons = gate.get("reasons")
    reasons = [reason for reason in raw_reasons if isinstance(reason, str)] if isinstance(raw_reasons, list) else []
    descriptions, actions = [], []
    messages = {
        "ge_audit_unverified": (
            "你要求只补尚未完成的 GE；当前缺少已授权且已核实的个人 GE 审计，无法确认剩余类别，不能猜测缺项。筛选未执行不代表你没有提出该要求。",
            "You requested only remaining GE requirements. An authorized, verified personal GE audit is unavailable, so the remaining categories cannot be established or guessed. An unapplied filter does not cancel your request.",
            "请提供可核实的个人 GE 缺项审计，并确认允许使用该记录。",
            "Provide a verifiable audit of your remaining personal GE requirements and authorize its use."),
        "filter_plan_unavailable": (
            "尚未形成通过校验的筛选计划，不能把默认条件当作你已确认的要求。",
            "A validated filter plan is unavailable; default settings cannot stand in for your confirmed requirements.",
            "请用一句话确认最重要的硬性选课条件，以便重新核实。",
            "Confirm the most important mandatory course constraints in one sentence so they can be verified."),
        "only_unmet_ge_not_applied": (
            "你要求只补剩余 GE，但该硬性筛选尚未生效，不能把一般 GE 候选当作已符合缺项的推荐。",
            "Your remaining-GE-only constraint has not been applied; general GE candidates cannot be presented as meeting your remaining requirements.",
            "请确认保留“只补剩余 GE”的要求，以便按可信审计重新核实。",
            "Confirm that the remaining-GE-only condition must be retained and rechecked against the verified audit."),
        "course_scope_not_applied": (
            "你要求的 GE 课程范围尚未应用，不能把范围外课程作为已符合条件的推荐。",
            "Your requested GE course scope has not been applied; courses outside that scope cannot be presented as qualifying recommendations.",
            "请确认本次只考虑 GE 课程，以便按该范围重新核实。",
            "Confirm that this request should consider GE courses only so that scope can be verified."),
        "earliest_start_scope_unsupported": (
            "限定特定课程或星期的最早上课时间尚不能可靠应用；本次没有把局部条件擅自扩大为所有课程。",
            "The earliest-start rule scoped to particular courses or weekdays cannot yet be applied reliably; it was not broadened to every course.",
            "请说明最早上课时间适用于哪些课程或星期。",
            "Specify which courses or weekdays the earliest-start time applies to."),
        "earliest_start_not_applied": (
            "你提出的最早上课时间硬性条件尚未应用，不能宣称候选已经符合该时间要求。",
            "Your mandatory earliest-start time has not been applied; candidates cannot be claimed to meet it.",
            "请确认最早可接受的上课时间及适用范围。",
            "Confirm the earliest acceptable start time and the courses or days it applies to."),
    }
    days = {"Mon": ("周一", "Monday"), "Tue": ("周二", "Tuesday"), "Wed": ("周三", "Wednesday"),
            "Thu": ("周四", "Thursday"), "Fri": ("周五", "Friday"), "Sat": ("周六", "Saturday"), "Sun": ("周日", "Sunday")}
    ordered = sorted(set(reasons), key=lambda reason: reason != "ge_audit_unverified")
    for reason in ordered:
        if reason in messages:
            item = messages[reason]
            descriptions.append(item[0 if zh else 1])
            actions.append(item[2 if zh else 3])
        elif ":" in reason:
            kind, day = reason.split(":", 1)
            if day not in days:
                continue
            label = days[day][0 if zh else 1]
            if kind == "excluded_weekday_not_applied":
                descriptions.append(f"你要求排除{label}上课，但该硬性条件尚未应用。" if zh else
                                    f"Your mandatory exclusion of {label} classes has not been applied.")
                actions.append(f"请确认是否排除所有{label}班次。" if zh else f"Confirm whether all {label} sections must be excluded.")
            elif kind == "partial_day_time_scope_unsupported":
                descriptions.append(f"你对{label}特定时段的限制尚不能可靠应用；没有把它扩大为全天禁课。" if zh else
                                    f"Your time-window restriction on {label} cannot yet be applied reliably; it was not broadened to an all-day exclusion.")
                actions.append(f"请明确{label}不可上课的具体时间范围。" if zh else f"Specify the unavailable time window on {label}.")
    opening = ("推荐条件仍待核实，未生成符合这些条件的正式推荐。" if zh else
               "Recommendation conditions remain unverified. No formal recommendation meeting those conditions was generated.")
    if not descriptions:
        descriptions = ["仍有硬性选课条件尚未核实。" if zh else "Some mandatory course constraints remain unverified."]
        actions = ["请确认尚未解决的硬性选课条件。" if zh else "Clarify the unresolved mandatory course constraints."]
    return "\n\n".join([opening, *descriptions, actions[0]])


def render_recommendation_answer(cards, language="zh", *, gate=None, incomplete=False, failure_reason=None,
                                 plan=None, requested_count=None, requested_plan=None):
    """Controlled text from normalized source facts, never an LLM answer.

    ``failure_reason`` is a signal only: raw provider/model error text is not
    echoed. No function here changes ranking, reads caches or makes requests.
    """
    zh = str(language).startswith("zh")
    gate = _mapping(gate)
    requested = _mapping(requested_plan)
    plan = requested or _mapping(plan)
    ge_scope = plan.get("course_scope") == "ge" or _mapping(plan.get("hard_filters")).get("only_unmet_ge") is True
    if gate.get("enabled") is True and gate.get("ready") is not True:
        return _blocked_recommendation_text(gate, zh)
    normalized = [normalize_recommendation_card(card, language) for card in cards or [] if isinstance(card, Mapping)]
    if not normalized:
        return ("本次核实尚未完成，暂未生成有完整证据的正式推荐。" if zh else
                "Verification is incomplete; no formal recommendation with complete evidence was generated.") if incomplete or failure_reason else (
                    "暂未取得可展示的课程候选；未知信息不代表未开课或不合格。" if zh else
                    "No course candidates are available to display. Missing evidence does not mean a course is not offered or that you are ineligible.")
    confirmed = sum(card["ge_evidence"]["is_ge"] is True for card in normalized)
    unknown = sum(card["ge_evidence"]["status"] == "unknown" for card in normalized)
    opening = (("本次核实尚未完成；以下仅列已取得证据的课程候选。" if zh else
                "Verification is incomplete; the following are course candidates with the evidence obtained so far.")
               if incomplete or failure_reason else ("以下为当前课程候选及已取得的证据。" if zh else
                                                     "Current course candidates and the evidence obtained are listed below."))
    count_line = (f"课程 GE 类别已确认 {confirmed} 门，待核实 {unknown} 门；课程标签不等于个人毕业抵扣已确认。" if zh else
                  f"Verified GE course tags: {confirmed} courses; unknown: {unknown}. Course tags do not establish personal degree credit.")
    blocks = [opening + ("\n\n" + count_line if ge_scope else "")]
    count = _count(requested_count)
    if count is not None and count > len(normalized):
        blocks.append((f"目前可展示 {len(normalized)} 门，少于请求的 {count} 门；未用未经核实的课程补足数量。" if zh else
                       f"{len(normalized)} candidates are available, fewer than the requested {count}; unverified courses were not added to fill the count."))
    for card in normalized:
        facts = card["presentation"]
        lines = ["**" + _text(card.get("course_id") or "Unknown course") + "**"]
        selected = facts["selected_lecture"]
        if selected:
            identity = " ".join(_text(value) for value in (selected["type"], selected["num"]) if value)
            code = _text(selected["code"]) if selected["code"] is not None else "?"
            names = " / ".join(_text(name) for name in selected["instructors"])
            schedule_parts = ((selected["time"],) if selected["days"] and str(selected["time"] or "").startswith(
                f"{selected['days']} ") else (selected["days"], selected["time"]))
            schedule = " ".join(_text(value) for value in schedule_parts if value)
            lines.append(("当前主班：" if zh else "Current primary: ") + f"{identity} ({code})" +
                         (f"，{names}" if names else "") + (f"，{schedule}" if schedule else "") + "。")
        else:
            lines.append("当前主班信息待核实。" if zh else "The current primary section is unverified.")
        rank = facts["ranking"]
        if rank["final_score"] is not None:
            weights = rank["effective_weights"]
            lines.append((f"固定推荐分 {rank['final_score']:.1f}/100；实际权重 A 率 {weights['grade_a_rate']:.0%}、RMP {weights['professor_rating']:.0%}。" if zh else
                          f"Fixed recommendation score {rank['final_score']:.1f}/100; effective weights: A rate {weights['grade_a_rate']:.0%}, RMP {weights['professor_rating']:.0%}."))
            rating = _mapping(_mapping(rank.get("features")).get("professor_rating"))
            value, sample = _number(rating.get("value"), minimum=1, maximum=5), _count(rating.get("sample_count"))
            if rating.get("valid") is True and value is not None and sample is not None:
                lines.append((f"教授评分（本地 RMP 快照）：{value:g}/5，{sample} 条评分；教授范围，不是本课专属评价。" if zh else
                              f"Professor rating (local RMP snapshot): {value:g}/5 from {sample} ratings; instructor-wide, not specific to this course."))
        else:
            lines.append("推荐分未知；缺失证据不按零分解释。" if zh else "Recommendation score unknown; missing evidence is not a zero score.")
        if facts["grade_evidence"]:
            lines.extend(_grade_line(entry, zh) for entry in facts["grade_evidence"])
        else:
            lines.append("可核对范围的历史成绩证据不足。" if zh else "Historical grade evidence with a verified scope is unavailable.")
        ge = facts["ge"]
        if ge_scope and ge["status"] == "unknown":
            lines.append("课程 GE 类别待核实，暂不计入已确认 GE 课程。" if zh else "Course GE categories are unverified; this course is not counted as a confirmed GE course.")
        elif ge_scope and ge["categories"]:
            lines.append(("已核实课程 GE 类别：" if zh else "Verified course GE categories: ") + " / ".join(ge["categories"]) + "。")
        elif ge_scope:
            lines.append("已核实的课程 GE 列表为空。" if zh else "The verified course GE list is empty.")
        if ge_scope and ge["status"] == "verified":
            lines.append(("GE 标签来源：" if zh else "GE tag source: ") + _ge_source_label(ge, zh) + "。")
        personal = ge["personal_contribution"]
        if ge_scope and personal["status"] == "unknown":
            lines.append("个人毕业要求抵扣仍待核实。" if zh else "Personal degree-requirement credit remains unverified.")
        elif ge_scope:
            remaining = " / ".join(personal["remaining_categories"])
            lines.append((f"可信学业审计中的剩余类别贡献：{remaining or '无'}；预计覆盖与已完成分列。" if zh else
                          f"Contribution to remaining categories in the verified audit: {remaining or 'none'}; projected credit is separate from completion."))
        requirement = _mapping(facts["pairing"]).get("requirement", "unknown")
        if requirement == "unknown":
            lines.append("讲座与讨论课／实验课配对要求待核实，不能确认仅讲座代码即可完成注册。" if zh else
                         "Lecture/discussion/lab pairing remains unverified; the lecture code alone is not confirmed sufficient for registration.")
        elif requirement == "required":
            rule = _mapping(facts["pairing"])
            groups = rule.get("required_components")
            if isinstance(groups, list) and groups:
                rendered = []
                for group in groups:
                    label = (" 或 " if zh else " or ").join(group.get("types") or [])
                    low, high = group.get("min", 1), group.get("max", 1)
                    if (low, high) != (1, 1):
                        label += f" ({low}–{high})"
                    rendered.append(label)
                components = " + ".join(rendered)
            else:
                components = " + ".join(rule.get("required_types") or [])
            from app.recommendations.pairing import discussion_policy_current
            if discussion_policy_current(rule):
                lines.append((f"按课程课表与当前选课规则，需选 {components or '配套班次'}。" if zh else
                              f"The course listing and current selection policy require {components or 'secondary components'}."))
                linked = rule.get("required_course_components") or []
                if linked:
                    courses = " / ".join(_text(item.get("course_id")) for item in linked if isinstance(item, Mapping) and item.get("course_id"))
                    if courses:
                        lines.append((f"关联 co-class：{courses}；需使用其当期班次核对组合。" if zh else
                                      f"Linked co-class: {courses}; use its target-term sections to verify the combination."))
                if rule.get("relationship") == "unknown":
                    lines.append((f"{components or '配套班次'} 为必选，但可配的具体班次仍待核实。" if zh else
                                  f"{components or 'Secondary components'} required; the compatible section relationship remains unverified."))
            else:
                lines.append((f"来源已确认需要配套 {components or '教学组件'}；仍需核对完整班次组合。" if zh else
                              f"The source confirms required secondary components ({components or 'secondary'}); verify the complete section combination."))
        else:
            from app.recommendations.pairing import discussion_policy_current
            if discussion_policy_current(facts["pairing"]):
                lines.append("按课程课表与当前选课规则，此主班无需选 Dis；其他修读条件分开判断。" if zh else
                             "The course listing and current selection policy do not require discussion for this primary; other enrollment conditions are assessed separately.")
            else:
                lines.append("来源已确认此主班无需配套教学组件；其他注册条件另行核实。" if zh else
                             "The source confirms no secondary component is required for this primary; other enrollment conditions remain separate.")
        selection = _mapping(facts["selection_verification"])
        secondary_codes = selection.get("selected_secondary_codes") or []
        if secondary_codes:
            lines.append(("当前选择的配套班次代码：" if zh else "Currently selected secondary section codes: ") +
                         " / ".join(_text(code) for code in secondary_codes) + "。")
        if selection.get("status") == "satisfied":
            lines.append("当前选择满足已核实的配对规则；不代表其他注册条件已通过。" if zh else
                         "The current selection satisfies the verified pairing rules; this does not establish other enrollment eligibility.")
        elif selection.get("status") == "unsatisfied":
            lines.append("当前选择未满足已核实的完整配对规则，不能视为可直接注册的完整组合。" if zh else
                         "The current selection does not satisfy the verified complete pairing rules and is not a confirmed registration-ready combination.")
        else:
            lines.append("当前组合是否满足配对规则仍未知。" if zh else
                         "Whether the current combination satisfies pairing rules remains unknown.")
        blocks.append("\n\n".join(lines))
    blocks.append(("历史统计与固定推荐分不预测未来成绩、工作量或个人拿 A 风险；本次未建立其他课程的比较基线。" if zh else
                   "Historical statistics and fixed scores do not predict future grades, workload or your chance of an A. No comparison baseline for other courses was established."))
    return "\n\n".join(blocks)
