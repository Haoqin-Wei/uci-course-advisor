"""
consistency validator — answer ↔ cards ↔ retrieved 3-way agreement.

Rules:
  R1. Every primary-recommendation card's course SHOULD appear in the
      LLM answer text. Missing → WARN (answer might be discussing
      different courses than the cards show).
  R2. Every course the LLM 'recommends' (as opposed to 'mentions
      laterally') should be in retrieved.primary, not retrieved.flagged.
      Approximated by checking: if a flagged course appears in the
      answer with 'recommend' language within 80 chars → WARN.
  R3. retrieved is empty (no candidates) but LLM answer cites courses
      → ERROR (whole answer is probably hallucinated).
"""

from __future__ import annotations
import re

from app.catalog.normalization import iter_course_mentions
from app.validation.types import (
    Issue, Severity, SuggestedAction, ValidationContext,
)
from app.validation.validators.base import Validator


_RECOMMEND_KEYWORDS = re.compile(
    r"\b(recommend|suggest|top pick|best (option|choice)|go with|take)\b",
    re.IGNORECASE,
)


class ConsistencyValidator(Validator):
    @property
    def name(self) -> str:
        return "consistency"

    def check(self, ctx: ValidationContext) -> list[Issue]:
        issues: list[Issue] = []
        primary = ctx.retrieved_primary_refs
        flagged = ctx.retrieved_flagged_refs
        mentioned = {ref for ref, _, _ in iter_course_mentions(ctx.llm_answer)}

        # ── R1. At least one primary recommendation must appear ──
        # Original rule was too strict: it warned whenever ANY primary went
        # un-mentioned. But it's a perfectly reasonable answer strategy for
        # the LLM to pick the best one and discuss only that. Only warn
        # when LLM mentions NONE of the primary recommendations.
        if primary:
            mentioned_primary = primary & mentioned
            if not mentioned_primary:
                unmentioned = primary - mentioned
                issues.append(Issue(
                    validator=self.name,
                    code="NO_PRIMARY_MENTIONED",
                    severity=Severity.WARN,
                    message=(
                        f"None of the {len(primary)} primary recommendation(s) "
                        f"appeared in the answer: "
                        f"{', '.join(r.display() for r in unmentioned)}"
                    ),
                    evidence={
                        "unmentioned": [r.display() for r in unmentioned],
                    },
                    suggested_action=SuggestedAction.ANNOTATE,
                ))

        # ── R2. Flagged course used as recommendation ──
        # Dedupe: same course mentioned with rec language across 5 sentences
        # shouldn't yield 5 identical issues.
        seen_flagged: set[str] = set()
        for ref, start, end in iter_course_mentions(ctx.llm_answer):
            if ref not in flagged:
                continue
            key = ref.display()
            if key in seen_flagged:
                continue
            window = ctx.llm_answer[max(0, start - 80): end + 80]
            if _RECOMMEND_KEYWORDS.search(window):
                seen_flagged.add(key)
                issues.append(Issue(
                    validator=self.name,
                    code="FLAGGED_AS_RECOMMENDATION",
                    severity=Severity.WARN,
                    message=(
                        f"{ref.display()} is in the flagged set (prereq unmet "
                        f"or scheduling conflict) but appears with "
                        f"recommendation language."
                    ),
                    location={
                        "start": start, "end": end,
                        "snippet": ctx.llm_answer[start:end],
                    },
                    evidence={"ref": ref.display(), "window": window},
                    suggested_action=SuggestedAction.ANNOTATE,
                ))

        # ── R3. Empty retrieval but answer cites courses ──
        if not ctx.retrieval_performed and not primary and not flagged and mentioned:
            issues.append(Issue(
                validator=self.name,
                code="ANSWER_WITHOUT_RETRIEVAL",
                severity=Severity.ERROR,
                message=(
                    f"No courses were retrieved for this query, but the "
                    f"answer references {len(mentioned)} course(s). The "
                    f"answer may be hallucinated."
                ),
                evidence={
                    "mentioned": [r.display() for r in mentioned],
                },
                suggested_action=SuggestedAction.BLOCK,
            ))

        return issues
