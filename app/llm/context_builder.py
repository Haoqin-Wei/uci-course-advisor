"""
6-layer prompt context builder (Phase 3.4).

Replaces the old monolithic `_build_answer_context` in adapter.py with
an explicit, layered structure that scales properly over long sessions.

Layers (in order):

  1. System prompt           static, ~500 tok    (ANSWER_SYSTEM_PROMPT or override)
  2. Memory snapshot         fresh, ~200 tok     (profile + facts + preferences)
  3. Decisions block         accumulating, ~100-400 tok  (pinned commitments)
  4. Older conversation summary  optional, ~300-600 tok  (Phase 3.9 LLM-generated)
  5. Last N raw turns        sliding window, ~N*200 tok  (history continuity)
  6. Current turn retrieved data  fresh, ~1500 tok  (course candidates, etc.)
  + Current user message

Layers 1-4 + 6 go into the system message. Layer 5 becomes alternating
user/assistant messages so the LLM sees them as actual conversation
turns. The current user message is the final user message.

Key invariant: this builder is STATELESS. It's called once per turn,
takes the current state as parameters, and returns a fresh messages
list. retrieved_data from prior turns NEVER leaks into history because
it's never persisted to turns.jsonl.

Public API:
    build_messages(...) -> list[dict]

Sub-functions are exposed for testing.
"""
from __future__ import annotations

import json
from typing import Optional


# Soft caps — exceeded values get truncated to keep prompt size bounded.
_MEMORY_SNAPSHOT_MAX_CHARS = 1200
_DECISIONS_BLOCK_MAX_CHARS = 1500
_RETRIEVED_DATA_MAX_CHARS  = 6000     # ~1500 tok; this is the biggest layer


# ── Memory snapshot ─────────────────────────────────────

def build_memory_snapshot(
    profile: Optional[dict] = None,
    preferences: Optional[list] = None,
    facts: Optional[list] = None,
) -> str:
    """
    Format the persistent user-level memory as a concise block.

    `profile` is a dict like {major, year, completed_courses, ...}.
    `preferences` is a list of
       {id, text, learned_at, last_confirmed_at} dicts.
    `facts` is a list of strings.
    """
    if not (profile or preferences or facts):
        return ""

    lines = ["# Persistent student profile"]
    profile = profile or {}

    # Identity
    if profile.get("major"):
        m = profile["major"]
        yr = profile.get("year")
        lines.append(f"- {m}" + (f", {yr}" if yr else ""))
    if profile.get("target_gpa"):
        lines.append(f"- Target GPA: {profile['target_gpa']}")

    # Courses
    completed   = profile.get("completed_courses") or []
    enrolled    = profile.get("selected_courses") or []
    waitlisted  = profile.get("waitlisted_courses") or []
    if completed:
        lines.append(f"- Completed ({len(completed)}): " + ", ".join(completed))
    if enrolled:
        lines.append(f"- Currently enrolled: " + ", ".join(enrolled))
    if waitlisted:
        lines.append(f"- Waitlisted: " + ", ".join(waitlisted))

    # Soft preferences (from Channel B reflection)
    pref_texts = _extract_pref_texts(preferences or [])
    if pref_texts:
        lines.append(f"- Learned preferences: " + "; ".join(pref_texts))

    # Hard facts (from Channel A)
    if facts:
        recent_facts = facts[-5:] if len(facts) > 5 else facts
        lines.append("- Recent facts:")
        for f in recent_facts:
            lines.append(f"  - {f}")

    out = "\n".join(lines)
    return _truncate(out, _MEMORY_SNAPSHOT_MAX_CHARS, suffix="\n  (...older details omitted)")


def _extract_pref_texts(preferences: list) -> list[str]:
    """Extract text from preference dicts; tolerate legacy strings defensively."""
    texts = []
    for p in preferences:
        if isinstance(p, str):
            t = p.strip()
        elif isinstance(p, dict):
            t = (p.get("text") or "").strip()
        else:
            t = ""
        if t:
            texts.append(t)
    return texts


# ── Decisions block ─────────────────────────────────────

def build_decisions_block(decisions: Optional[list[dict]] = None) -> str:
    """
    Format pinned session decisions. These are commitments the user has
    made earlier in this session that should NOT be forgotten even after
    summarization (e.g. "Take CS122A").
    """
    if not decisions:
        return ""
    lines = ["# Decisions made earlier in this session"]
    for d in decisions:
        text = (d.get("text") or "").strip() if isinstance(d, dict) else str(d).strip()
        if text:
            lines.append(f"- {text}")
    out = "\n".join(lines)
    return _truncate(out, _DECISIONS_BLOCK_MAX_CHARS,
                     suffix="\n  (...older decisions omitted)")


# ── Recent turns ────────────────────────────────────────

def build_recent_turns_messages(
    turns: Optional[list[dict]] = None,
    last_n: int = 10,
) -> list[dict]:
    """
    Convert the last N session turns into a list of LLM messages
    ({role, content}). Empty list if no turns.

    Each input turn is a dict from sessions.read_turns():
       {turn_index, role, content, timestamp}
    We keep just role + content for the LLM.

    Hard cap at last_n turns — older turns should already be in the
    summary layer (Phase 3.9). For Round 2 (no summary yet), this is
    the only memory of older context, so 10 is a reasonable default.
    """
    if not turns:
        return []
    recent = turns[-last_n:] if last_n > 0 else turns
    messages = []
    for t in recent:
        role = t.get("role")
        content = (t.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    return messages


# ── Retrieved-data block ────────────────────────────────

def build_retrieved_data_block(retrieved_data: Optional[dict]) -> str:
    """
    Format this-turn-only retrieved data (candidate courses, sections,
    professor info, etc.). This block is REBUILT every turn and never
    persisted to turns.jsonl — that's the central design property
    preventing context bloat over long sessions.

    For backwards compatibility with the previous JSON-dump approach,
    we just serialize to indented JSON. A more semantic formatter
    could replace this later.
    """
    if not retrieved_data:
        return ""
    try:
        body = json.dumps(retrieved_data, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        body = repr(retrieved_data)
    block = "# Retrieved data for this turn (do not assume this persists across turns)\n" + body
    return _truncate(block, _RETRIEVED_DATA_MAX_CHARS,
                     suffix="\n  (...retrieved data truncated; refine your query)")


# ── Assembly ────────────────────────────────────────────

def build_messages(
    system_prompt: str,
    user_message: str,
    *,
    profile: Optional[dict] = None,
    preferences: Optional[list] = None,
    facts: Optional[list] = None,
    decisions: Optional[list[dict]] = None,
    summary: Optional[str] = None,
    recent_turns: Optional[list[dict]] = None,
    retrieved_data: Optional[dict] = None,
    selected_term: Optional[str] = None,
    today: Optional[str] = None,
    last_n_turns: int = 10,
) -> list[dict]:
    """
    Assemble the full 6-layer prompt into an OpenAI-style messages list.

    Returns a list of {role, content} dicts ready to pass to
    client.chat.completions.create(messages=...).

    Shape of the result:
        [
          {"role": "system",    "content": "<system + memory + decisions + summary>"},
          {"role": "user",      "content": "<turn 1 user message>"},
          {"role": "assistant", "content": "<turn 1 assistant reply>"},
          ...                                              ← recent_turns expanded
          {"role": "user",      "content": "<retrieved data>\n\n<user_message>"},
        ]

    The retrieved_data is prepended to the *current* user message rather
    than going into the system block. Rationale: retrieved data is the
    direct context for THIS question, not background knowledge. Keeping
    it adjacent to the user message helps the LLM scope its answer.
    """
    # ── Layer 1-4: system block ──
    system_parts = [system_prompt.strip()] if system_prompt else []

    # Selected term + today's date go near the TOP of the system
    # context, before the memory snapshot, so the model can't miss
    # them. These are per-turn state (user can change the dropdown
    # between messages; date changes daily) — that's why they live
    # here rather than in the durable profile snapshot.
    context_lines: list[str] = []
    if today:
        context_lines.append(f"Today's date: **{today}**.")
    if selected_term:
        context_lines.append(
            f"Student's currently selected term: **{selected_term}**. "
            f"Use this whenever a tool needs a `term` argument. Do not "
            f"ask the student which term — it's already chosen. "
            f"Reason about whether this term is past / currently in "
            f"session (past week 2 add/drop deadline) / upcoming, and "
            f"frame your advice accordingly per the UCI policies above."
        )
    if context_lines:
        system_parts.append("# Current request context\n" + "\n\n".join(context_lines))

    mem = build_memory_snapshot(profile, preferences, facts)
    if mem:
        system_parts.append(mem)

    dec = build_decisions_block(decisions)
    if dec:
        system_parts.append(dec)

    if summary and summary.strip():
        system_parts.append("# Earlier conversation summary\n" + summary.strip())

    messages: list[dict] = []
    if system_parts:
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})

    # ── Layer 5: recent raw turns as proper messages ──
    messages.extend(build_recent_turns_messages(recent_turns, last_n=last_n_turns))

    # ── Layer 6 + current user message ──
    retrieved_block = build_retrieved_data_block(retrieved_data)
    if retrieved_block:
        user_content = retrieved_block + "\n\n---\n\nCurrent question:\n" + user_message
    else:
        user_content = user_message
    messages.append({"role": "user", "content": user_content})

    return messages


# ── Utility ─────────────────────────────────────────────

def _truncate(s: str, max_chars: int, suffix: str = "") -> str:
    if len(s) <= max_chars:
        return s
    cutoff = max_chars - len(suffix)
    return s[:cutoff] + suffix
