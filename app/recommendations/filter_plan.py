"""Bounded recommendation intent and deterministic hard-constraint validation."""

from __future__ import annotations

import copy
import logging
import re
from app.catalog.ge import normalize_ge_category
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.catalog.departments import DEPARTMENT_ALIASES


Goal = Literal["gpa_optimization", "graduation_progress", "balanced", "interest_exploration"]
Weekday = Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ConstraintField = Literal["course_scope", "only_unmet_ge", "earliest_start", "excluded_weekdays"]
TRUSTED_GE_SOURCES = frozenset({"registrar_degree_audit", "verified_academic_audit"})
_GE_CODES = frozenset({"I", "IA", "IB", "II", "III", "IV", "V", "VA", "VB", "VI", "VII", "VIII"})
_DAY_PATTERNS = {
    "Mon": r"\bmon(?:day)?s?\b|(?:周|星期|礼拜)一",
    "Tue": r"\btue(?:s(?:day)?)?s?\b|(?:周|星期|礼拜)二",
    "Wed": r"\bwed(?:nesday)?s?\b|(?:周|星期|礼拜)三",
    "Thu": r"\bthu(?:rs(?:day)?)?s?\b|(?:周|星期|礼拜)四",
    "Fri": r"\bfri(?:day)?s?\b|(?:周|星期|礼拜)五",
    "Sat": r"\bsat(?:urday)?s?\b|(?:周|星期|礼拜)六",
    "Sun": r"\bsun(?:day)?s?\b|(?:周|星期|礼拜)(?:日|天)",
}
_SOFT = re.compile(
    r"最好|尽量|优先|如果可以|能的话|可以的话|不强制|可选|倾向|"
    r"\b(?:prefer(?:ably)?|ideally|if possible|optional|try to|nice to have)\b", re.I
)
_RELAXED = re.compile(
    r"不是硬性|并非硬性|不是强制|并非强制|不是必须|不作硬性|只是偏好|不绝对|并非绝对|"
    r"(?:取消|撤销).{0,6}(?:限制|条件|要求)|"
    r"\b(?:not (?:a )?hard|not mandatory|only a preference|no longer require)\b", re.I
)
_BAN = re.compile(
    r"不能|不可以|不要|禁止|不允许|不接受|不得|绝对不|"
    r"\b(?:cannot|can't|can not|do not|don't|must not|never|no classes|exclude)\b", re.I
)
_MUST = re.compile(r"必须|务必|只能|只接受|只考虑|只推荐|\b(?:must|required|only)\b", re.I)
_NON_REQUEST = re.compile(
    r"举例|假设|如果用户|他(?:说|要求)|她(?:说|要求)|之前(?:说|要求)|曾经(?:说|要求)|"
    r"不要.{0,20}当成.{0,10}要求|翻译|"
    r"\b(?:for example|suppose|hypothetical|translate|previously said|earlier said)\b", re.I
)
_TIME = re.compile(
    r"(?<!\d)(?P<hour>\d{1,2})(?:(?::(?P<minute>\d{2}))|\s*(?P<chinese>点|时|時))"
    r"\s*(?P<half>半)?\s*(?P<meridiem>[ap]\.?m\.?)?|"
    r"(?<!\d)(?P<am_hour>\d{1,2})\s*(?P<am_meridiem>[ap]\.?m\.?)\b|"
    r"(?<![\w:])(?P<bare_hour>\d{1,2})(?![\d:])", re.I
)
_COURSE_MENTION = re.compile(
    r"(?<![A-Za-z0-9])(?:" + "|".join(
        re.escape(alias).replace(r"\ ", r"\s*")
        for alias in sorted({name for department, aliases in DEPARTMENT_ALIASES.items()
                             for name in (department, *aliases)}, key=len, reverse=True)
    ) + r")\s*[A-Za-z]?\d{1,3}[A-Za-z]{0,3}(?![A-Za-z0-9])", re.I
)
_GLOBAL_TIME_SCOPE = re.compile(r"所有(?:课|课程|天)|全部(?:课|课程|天)|任何(?:课|课程)|每天|每一天|"
                                r"\b(?:all|every|any)\s+(?:classes|class|courses|course|days|day)\b", re.I)
_TIME_EXCEPTION = re.compile(r"除了|除外|除非|例外|\b(?:except|unless)\b", re.I)
_LOCAL_TIME_SCOPE = re.compile(r"这门课|那门课|该课程|这节课|那节课|工作日|周末|"
                               r"\b(?:this course|that course|this class|that class|weekdays?|weekends?)\b", re.I)


logger = logging.getLogger(__name__)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class HardFilters(_StrictModel):
    only_unmet_ge: bool = False
    earliest_start: str | None = None
    earliest_start_is_hard: bool = False
    excluded_weekdays: list[Weekday] = Field(default_factory=list, max_length=7)

    @field_validator("earliest_start")
    @classmethod
    def validate_clock(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("earliest_start must be a 24-hour HH:MM clock")
        return value


class SoftPreferences(_StrictModel):
    high_a_rate: bool = False
    professor_rating: Literal["any", "high"] = "any"
    avoid_early_classes: bool = False
    avoid_weekdays: list[Weekday] = Field(default_factory=list, max_length=7)


class ExplicitConstraint(_StrictModel):
    field: ConstraintField
    quote: str = Field(min_length=1, max_length=2000)
    source: Literal["user_message"] = "user_message"


class FilterPlan(_StrictModel):
    goal: Goal = "balanced"
    secondary_goals: list[Goal] = Field(default_factory=list, max_length=4)
    course_scope: Literal["any", "ge"] = "any"
    hard_filters: HardFilters = Field(default_factory=HardFilters)
    soft_preferences: SoftPreferences = Field(default_factory=SoftPreferences)
    explicit_constraints: list[ExplicitConstraint] = Field(default_factory=list, max_length=16)
    reason_summary: str = Field(default="", max_length=1000)


def empty_filter_plan() -> dict:
    """A fresh conservative plan that cannot remove optional candidates."""
    return FilterPlan().model_dump(mode="json")


def trusted_ge_audit(profile: object) -> dict | None:
    """Accept only an authorized, verified server-supplied academic audit."""
    if not isinstance(profile, dict) or not isinstance(profile.get("ge_audit"), dict):
        return None
    audit = profile["ge_audit"]
    if (audit.get("verified") is not True or audit.get("authorized") is not True
            or not isinstance(audit.get("source"), str)
            or audit["source"] not in TRUSTED_GE_SOURCES):
        return None
    if "unmet_categories" not in audit:
        return None
    out = copy.deepcopy(audit)
    for field in ("unmet_categories", "projected_categories"):
        if field not in audit:
            continue
        values = audit[field]
        if not isinstance(values, list) or len(values) > len(_GE_CODES):
            return None
        normalized = [normalize_ge_category(value) for value in values]
        if any(value is None for value in normalized):
            return None
        out[field] = list(dict.fromkeys(normalized))
    return out


def _evidence_clauses(plan: dict, field: str, message: str) -> list[str]:
    """Use the enclosing original clause so a quote cannot hide 'preferably'."""
    clauses = []
    for evidence in plan["explicit_constraints"]:
        if evidence["field"] != field:
            continue
        quote = evidence["quote"]
        for match in re.finditer(re.escape(quote), message):
            for clause in re.finditer(r"[^，,。;；!?！？\n]+", message):
                if clause.start() < match.end() and clause.end() > match.start():
                    if clause.group() not in clauses:
                        clauses.append(clause.group())
    return clauses


def _unmet_only_requested(clause: str) -> bool:
    if _SOFT.search(clause) or _RELAXED.search(clause) or _NON_REQUEST.search(clause):
        return False
    if re.search(r"(?:不|并非|不是|无需|不想)\s*(?:只要|仅要|仅限|只考虑|只推荐)|"
                 r"没说|没有说|不代表|不是说|已完成也|已满足也|"
                 r"\b(?:not only|don't only|do not only|never said|didn't say|don't assume|"
                 r"only (?:goal|priority|objective|focus)|regardless)\b", clause, re.I):
        return False
    return bool(
        re.search(r"只要|仅要|仅限|只考虑|只推荐|只想选|只能|\bonly\b", clause, re.I)
        and re.search(r"(?<![A-Za-z])GE(?![A-Za-z])|通识|general education", clause, re.I)
        and re.search(r"未完成|没完成|尚未完成|未满足|尚未满足|剩余|未修完|还没完成|缺项|缺的|"
                      r"\b(?:unmet|remaining|unfinished|not yet completed|not yet met)\b", clause, re.I)
    )


def _ge_scope_requested(clause: str) -> bool:
    if _SOFT.search(clause) or _RELAXED.search(clause) or _NON_REQUEST.search(clause):
        return False
    ge = r"(?<![A-Za-z])GE(?![A-Za-z])|通识|general\s+education"
    if not re.search(ge, clause, re.I):
        return False
    # Mentioning GE, rejecting it, or offering a choice is not GE-only scope.
    if re.search(r"(?:不要|不选|不修|不推荐|不考虑|不想选|不想修|不想上|不想要|别推荐|别选|"
                 r"排除|避开|非|不限于|不限|不限定).{0,16}(?:" + ge + r")|"
                 r"\b(?:don't|do not|no|not|exclude|avoid|skip)\b.{0,24}(?:" + ge + r")|"
                 r"(?:" + ge + r").{0,16}(?:无关|没(?:有)?关系|不限制|不限定)|"
                 r"或|任选|都行|不限|也可以|也可|\b(?:or|either|regardless)\b", clause, re.I):
        return False
    if _unmet_only_requested(clause):
        return True
    return bool(
        re.search(r"(?:推荐|选择|选|修|上|\b(?:recommend|suggest|take|enroll|choose|pick)\b)"
                  r".{0,48}(?:" + ge + r")", clause, re.I)
        or re.search(r"(?:" + ge + r").{0,6}(?:课程|课).{0,16}(?:推荐|选择|选)|"
                     r"(?:" + ge + r").{0,6}有什么推荐|"
                     r"(?:" + ge + r")\s+(?:courses|classes)\s+"
                     r"(?:(?:do|would|can)\s+you\s+)?recommend|"
                     r"(?:" + ge + r")\s+(?:courses|classes).{0,16}\bto take\b", clause, re.I)
    )


def _earliest_values(clause: str) -> set[str]:
    values = set()
    for match in _TIME.finditer(clause):
        hour = int(match.group("hour") or match.group("am_hour") or match.group("bare_hour"))
        minute = int(match.group("minute") or ("30" if match.group("half") else "0"))
        meridiem = match.group("meridiem") or match.group("am_meridiem")
        if meridiem:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if meridiem.lower().startswith("p") else 0)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            continue
        left, right = clause[:match.start()], clause[match.end():]
        # Bare-hour matches include the English comparator itself.
        before = bool(re.search(r"(?:before|earlier than)\s*$", left, re.I)
                      or re.match(r"\s*(?:以?前|之前)", right)
                      or re.match(r"(?:before|earlier than)\b", match.group(), re.I))
        after = bool(re.search(r"(?:after|at or after)\s*$", left, re.I)
                     or re.match(r"\s*(?:以后|之后)", right)
                     or re.match(r"after\b", match.group(), re.I))
        no_earlier = bool(re.search(r"(?:不|不得|不能|不要)早于\s*$|no earlier than\s*$|not before\s*$", left, re.I)
                          or re.match(r"no earlier than\b", match.group(), re.I))
        if no_earlier or (before and _BAN.search(clause)) or (after and _MUST.search(clause) and not _BAN.search(clause)):
            values.add(f"{hour:02d}:{minute:02d}")
    return values


def _global_earliest_scope(clause: str, message: str) -> bool:
    if _TIME_EXCEPTION.search(message):
        return False
    if _GLOBAL_TIME_SCOPE.search(clause):
        return True
    local = lambda text: (any(re.search(pattern, text, re.I) for pattern in _DAY_PATTERNS.values())
                          or _COURSE_MENTION.search(text) or _LOCAL_TIME_SCOPE.search(text))
    if local(clause):
        return False
    # A bare preceding topic ("For CS161, ...") cannot be hidden by quoting
    # just the later time clause. An unrelated earlier sentence stays separate.
    prefix = message[:message.find(clause)]
    previous = re.split(r"[，,。;；!?！？\n]", prefix.rstrip("，, "))[-1]
    if local(previous) and len(previous.strip()) <= 35:
        topic = re.sub(r"^(?:for|on|对于|关于|在|仅限|只在)\s*", "", previous.strip(), flags=re.I)
        if (_COURSE_MENTION.fullmatch(topic)
                or any(re.fullmatch(pattern, topic, re.I) for pattern in _DAY_PATTERNS.values())
                or _LOCAL_TIME_SCOPE.fullmatch(topic)):
            return False
    return True


def _weekday_off_requested(clause: str, day: str) -> bool:
    if not re.search(_DAY_PATTERNS[day], clause, re.I):
        return False
    if re.search(r"不能没课|不能没有课|不要没课|不想没课|\bnot (?:have |be )?free\b", clause, re.I):
        return False
    # A restriction on part of a day must not become a whole-day exclusion.
    if (_TIME.search(clause) or re.search(r"早八|上午|下午|晚上|早上|中午|太早|"
                                        r"\b(?:morning|afternoon|evening|early)\b", clause, re.I)):
        return False
    if re.search(r"考试|\bexam", clause, re.I) and not re.search(r"上课|课程|\bclass", clause, re.I):
        return False
    return bool(
        _BAN.search(clause)
        or (_MUST.search(clause) and re.search(r"没课|无课|休息|空出|\b(?:free|off|no classes)\b", clause, re.I))
    )


def _schema_errors(error: Exception) -> list[str]:
    """Where the schema broke, never what the text was."""
    if isinstance(error, ValidationError):
        return [".".join(str(part) for part in item["loc"]) + ":" + item["type"] for item in error.errors()[:8]]
    return [type(error).__name__]


def validate_filter_plan(raw: object, user_message: str, profile: object = None) -> dict:
    """Validate model output without trusting inferred hard rules or GE credit."""
    if not isinstance(user_message, str):
        return {"plan": empty_filter_plan(), "status": "fallback", "issues": ["invalid_user_message"]}
    try:
        candidate = raw.model_dump(mode="json") if isinstance(raw, FilterPlan) else raw
        plan = FilterPlan.model_validate(candidate).model_dump(mode="json")
    except (ValidationError, TypeError, ValueError) as error:
        logger.info("filter plan schema rejected at %s", _schema_errors(error))
        return {"plan": empty_filter_plan(), "status": "fallback", "issues": ["invalid_schema"]}

    requested_plan = copy.deepcopy(plan)
    hard, soft = plan["hard_filters"], plan["soft_preferences"]
    issues: list[str] = []
    plan["secondary_goals"] = list(dict.fromkeys(plan["secondary_goals"]))
    soft["avoid_weekdays"] = list(dict.fromkeys(soft["avoid_weekdays"]))
    globally_relaxed = bool(_RELAXED.search(user_message))

    if plan["course_scope"] == "ge":
        supported = any(_ge_scope_requested(clause)
                        for clause in _evidence_clauses(plan, "course_scope", user_message))
        if globally_relaxed or not supported:
            plan["course_scope"] = "any"
            issues.append("course_scope_not_explicit")

    if hard["only_unmet_ge"]:
        supported = any(_unmet_only_requested(clause)
                        for clause in _evidence_clauses(plan, "only_unmet_ge", user_message))
        if globally_relaxed or not supported:
            hard["only_unmet_ge"] = False
            issues.append("only_unmet_ge_not_explicit")
        elif trusted_ge_audit(profile) is None:
            hard["only_unmet_ge"] = False
            issues.append("ge_audit_unverified")

    if hard["earliest_start_is_hard"]:
        clauses = _evidence_clauses(plan, "earliest_start", user_message)
        value_clauses = [clause for clause in clauses
                         if hard["earliest_start"] in _earliest_values(clause)]
        supported = (hard["earliest_start"] is not None and not globally_relaxed
                     and any(not _SOFT.search(clause) and not _RELAXED.search(clause)
                             and not _NON_REQUEST.search(clause)
                             and _global_earliest_scope(clause, user_message) for clause in value_clauses))
        if not supported:
            hard["earliest_start_is_hard"] = False
            hard["earliest_start"] = None
            scoped = any(not _global_earliest_scope(clause, user_message) for clause in value_clauses)
            if not scoped and any(_earliest_values(clause) or re.search(r"早八|早课|early classes|8\s*a\.?m", clause, re.I)
                                  for clause in clauses):
                soft["avoid_early_classes"] = True
            issues.append("earliest_start_scope_unsupported" if scoped else "earliest_start_not_explicit")

    days = []
    clauses = _evidence_clauses(plan, "excluded_weekdays", user_message)
    for day in dict.fromkeys(hard["excluded_weekdays"]):
        supported = not globally_relaxed and any(
            not _SOFT.search(clause) and not _RELAXED.search(clause)
            and not _NON_REQUEST.search(clause)
            and _weekday_off_requested(clause, day) for clause in clauses)
        if supported:
            days.append(day)
        else:
            issues.append(f"excluded_weekday_not_explicit:{day}")
            if any(_weekday_off_requested(clause, day) for clause in clauses) and day not in soft["avoid_weekdays"]:
                soft["avoid_weekdays"].append(day)
    hard["excluded_weekdays"] = days
    result = {"plan": plan, "status": "downgraded" if issues else "validated", "issues": issues}
    if "ge_audit_unverified" in issues:
        result["requested_plan"] = requested_plan
        result["unresolved_constraints"] = ["ge_audit_unverified"]
    return result


_CONDITION_WORDS = re.compile(
    r"\bGE\b|通识|早八|早课|早上|上午|下午|晚上|中午|之前|以前|之后|以后|"
    r"\b(?:morning|afternoon|evening|early|late|before|after|a\.?m\.?|p\.?m\.?)\b", re.I)


def states_no_condition(message: object) -> bool:
    """True when the message cannot be carrying a rule for the parser to extract.

    Such a message is not sent to the parser at all. Any clock time, weekday,
    part of the day, GE mention or "must / cannot / only" wording counts as a
    possible condition, so the error is on the side of asking the model.
    """
    if not isinstance(message, str) or not message.strip():
        return False
    if _TIME.search(message) or _BAN.search(message) or _MUST.search(message) or _CONDITION_WORDS.search(message):
        return False
    if any(re.search(pattern, message, re.I) for pattern in _DAY_PATTERNS.values()):
        return False
    return not unresolved_requested_constraints(empty_filter_plan(), message)


def unresolved_requested_constraints(plan: dict, message: str, profile: object = None) -> list[str]:
    """Audit omissions without granting model-authored rules any authority.

    The LLM still proposes the plan. This deterministic comparison prevents an
    explicit supported user condition from silently disappearing on downgrade.
    It never fills a missing filter, guesses a GE audit, or removes a candidate.
    """
    hard = plan.get("hard_filters") or {}
    unresolved = []
    for clause in re.findall(r"[^，,。;；!?！？\n]+", message or ""):
        if _NON_REQUEST.search(clause) or _SOFT.search(clause) or _RELAXED.search(clause):
            continue
        if _unmet_only_requested(clause) and not hard.get("only_unmet_ge"):
            unresolved.append("ge_audit_unverified" if trusted_ge_audit(profile) is None else "only_unmet_ge_not_applied")
        if _ge_scope_requested(clause) and plan.get("course_scope") != "ge":
            unresolved.append("course_scope_not_applied")
        for day in _DAY_PATTERNS:
            if _weekday_off_requested(clause, day) and day not in hard.get("excluded_weekdays", []):
                unresolved.append("excluded_weekday_not_applied:" + day)
            elif (re.search(_DAY_PATTERNS[day], clause, re.I) and _BAN.search(clause)
                  and re.search(r"上午|下午|晚上|早上|中午|\b(?:morning|afternoon|evening)\b", clause, re.I)):
                unresolved.append("partial_day_time_scope_unsupported:" + day)
        values = _earliest_values(clause)
        if values:
            if not _global_earliest_scope(clause, message):
                unresolved.append("earliest_start_scope_unsupported")
            elif not hard.get("earliest_start_is_hard") or hard.get("earliest_start") not in values:
                unresolved.append("earliest_start_not_applied")
    return list(dict.fromkeys(unresolved))
