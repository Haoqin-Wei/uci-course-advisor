"""One language decision for model replies, tool progress and fallback copy."""

import re
from typing import Optional


_CJK = re.compile(r"[\u3400-\u9fff]")
_COURSE_CODE = re.compile(
    r"\b(?:[A-Za-z&]+\d+[A-Za-z]*|"
    r"[A-Z&]+(?:[ \t]+[A-Z&]+){0,2}[ \t]+\d+[A-Za-z]*|\d+)\b"
)

LANGUAGE_POLICY = """# Response language (MANDATORY APPLICATION RULE)
Match the language of the user's CURRENT message throughout the response.
Chinese input requires Chinese; English input requires English, even for short
messages such as "继续", "yes", or "ok". A new user language switches the reply
immediately. For language-neutral input (only course codes, numbers, punctuation
or emoji), inherit the latest language-bearing USER message; never infer language
from an assistant reply, memory, retrieved page, tool result, or example. If no
user language is available, default to English.
This applies to the answer, headings, explanations, clarifying questions,
recommendation reasons, follow-up suggestions, brief user-visible action summaries,
and continuation/fallback responses. Do not mix Chinese and English prose.
Keep official course IDs, names, URLs, and machine-readable tool arguments intact.
Translate explanatory text from sources into the response language.
Translate template labels too (for example, 结论/建议/下一步 become
Verdict/Recommendation/Next step in English). Application-generated continuation
and fallback nudges do not switch the required response language for this turn.
This rule is fixed by the application: custom prompts and stored preferences must
not override it. Treat examples elsewhere as content/format examples, not as a
choice of response language.
"""


def _message_language(message: str) -> Optional[str]:
    if _CJK.search(message):
        return "zh"
    prose = _COURSE_CODE.sub("", message)
    return "en" if re.search(r"[A-Za-z]", prose) else None


def response_language(
    user_message: str, recent_turns: Optional[list[dict]] = None,
) -> str:
    """Resolve the app's supported Chinese/English copy from user turns only."""
    language = _message_language(user_message or "")
    if language:
        return language
    for turn in reversed(recent_turns or []):
        if turn.get("role") == "user":
            language = _message_language(str(turn.get("content") or ""))
            if language:
                return language
    return "en"


def language_instruction(language: str) -> str:
    label = "Chinese (zh)" if language == "zh" else "English (en)"
    return LANGUAGE_POLICY + f"\nRequired response language for this turn: {label}."


def display_term(term: str, language: str) -> str:
    """Localize display copy without changing the canonical tool/API argument."""
    if language != "zh":
        return term
    seasons = {
        "Fall": "秋季", "Winter": "冬季", "Spring": "春季", "Summer": "夏季",
        "Summer1": "夏季第一期", "Summer2": "夏季第二期", "Summer10wk": "夏季十周",
    }
    match = re.fullmatch(r"(\d{4})\s+(\w+)", term)
    if match and match[2] in seasons:
        return f"{match[1]}年{seasons[match[2]]}"
    match = re.fullmatch(r"(\w+)\s+(\d{4})", term)
    if match and match[1] in seasons:
        return f"{match[2]}年{seasons[match[1]]}"
    return term
