"""
Smoke-test the limit_reached + Continue flow without hitting a real LLM.

We stub `client.chat.completions.create` so:
  1. The first call (run_agent) emits a tool_call → loop runs the tool,
     then with MAX_ITERATIONS forced to 1 we exit the loop body and
     fall into the limit branch.
  2. The second call (the fallback with only propose_recommendation
     available) emits plain text.
  3. The third call (resume_agent) emits a clean final.

Run from repo root:   python scripts/smoke_limit_reached.py
"""

import asyncio
import sys
from types import SimpleNamespace

try:
    from scripts._bootstrap import ensure_repo_root_on_path
except ModuleNotFoundError:  # direct execution: python scripts/smoke_limit_reached.py
    from _bootstrap import ensure_repo_root_on_path

ensure_repo_root_on_path()

from app.agent import loop as agent_loop


def _delta(*, content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls,
                           reasoning_content=None)


def _chunk(delta):
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


async def _stream(chunks):
    for c in chunks:
        yield c


class StubClient:
    """Mimics enough of the OpenAI/Anthropic-style streaming client."""

    def __init__(self):
        self.call_count = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(
            create=self._create
        ))

    async def _create(self, *, model, messages, stream, tools=None, tool_choice=None):
        self.call_count += 1
        n = self.call_count
        if n == 1:
            # First call: model emits a tool_call (forces another iter,
            # which we don't get because MAX_ITERATIONS=1 trips first).
            tc = SimpleNamespace(
                index=0,
                id="call_abc",
                function=SimpleNamespace(name="get_policy", arguments='{}'),
            )
            chunks = [_chunk(_delta(tool_calls=[tc]))]
        elif n == 2:
            # Fallback may receive the one allowed card-staging tool,
            # but no data-gathering tools.
            tool_names = [s["function"]["name"] for s in (tools or [])]
            assert tool_names == ["propose_recommendation"], (
                f"fallback call received unexpected tools={tool_names!r}"
            )
            chunks = [
                _chunk(_delta(content="Sorry, ")),
                _chunk(_delta(content="hit the budget. ")),
                _chunk(_delta(content="Best-effort answer below.")),
            ]
        elif n == 3:
            # resume_agent: model gives a clean final, no tool_calls.
            chunks = [
                _chunk(_delta(content="Resumed answer ")),
                _chunk(_delta(content="completed.")),
            ]
        else:
            raise AssertionError(f"unexpected LLM call #{n}")
        return _stream(chunks)


async def main():
    # Force the loop into the limit branch on the very first iteration.
    agent_loop.MAX_ITERATIONS = 1
    client = StubClient()

    messages = [
        {"role": "system", "content": "you are a test"},
        {"role": "user",   "content": "hello"},
    ]

    print("── run 1: expect tool_call_start/done → limit_reached → fallback tokens → final(truncated=True)")
    events_seen = []
    saved_cid = None
    async for ev in agent_loop.run_agent(messages, client=client, model="stub",
                                         user_id="demo_001", term=None):
        events_seen.append(ev["type"])
        if ev["type"] == "limit_reached":
            saved_cid = ev["continuation_id"]
            print(f"  limit_reached: reason={ev['reason']!r}  iter={ev['iterations']}  tools={ev['tool_calls']}  cid={saved_cid[:8]}...")
        elif ev["type"] == "token":
            print(f"  token: {ev['text']!r}")
        elif ev["type"] == "tool_call_start":
            print(f"  tool_call_start: {ev['name']}")
        elif ev["type"] == "tool_call_done":
            print(f"  tool_call_done: {ev['name']} ok={ev['ok']}")
        elif ev["type"] == "final":
            print(f"  final: text={ev['text']!r}  truncated={ev.get('truncated')}")

    expected_order = [
        "tool_call_start", "tool_call_done",
        "limit_reached", "token", "token", "token", "final",
    ]
    assert events_seen == expected_order, (
        f"event order mismatch:\n  got      {events_seen}\n  expected {expected_order}"
    )
    assert saved_cid, "limit_reached event missing continuation_id"

    print("\n── run 2: resume_agent with saved continuation_id → expect clean final")
    events_seen2 = []
    async for ev in agent_loop.resume_agent(saved_cid, client=client, model="stub"):
        events_seen2.append(ev["type"])
        if ev["type"] == "token":
            print(f"  token: {ev['text']!r}")
        elif ev["type"] == "final":
            print(f"  final: text={ev['text']!r}  truncated={ev.get('truncated')}")

    assert events_seen2 == ["token", "token", "final"], events_seen2

    print("\n── run 3: resume_agent with already-popped cid → expect error")
    events_seen3 = []
    async for ev in agent_loop.resume_agent(saved_cid, client=client, model="stub"):
        events_seen3.append(ev["type"])
        print(f"  {ev['type']}: {ev.get('message', '')}")
    assert events_seen3 == ["error"], events_seen3

    print("\n── run 4: MAX_TOTAL_TOOLS path — fallback must NOT see dangling tool_calls")
    # Reset for a fresh scenario. Model emits TWO tool_calls in one
    # assistant message; we cap MAX_TOTAL_TOOLS at 1 so we trip MID
    # batch — the regression case: assistant declared 2 ids but only
    # 1 tool response exists when fallback is called.
    agent_loop.MAX_ITERATIONS = 5
    agent_loop.MAX_TOTAL_TOOLS = 1

    class BatchStubClient:
        def __init__(self):
            self.call_count = 0
            self.chat = SimpleNamespace(completions=SimpleNamespace(
                create=self._create
            ))
            self.last_fallback_messages = None

        async def _create(self, *, model, messages, stream, tools=None, tool_choice=None):
            self.call_count += 1
            n = self.call_count
            if n == 1:
                tc1 = SimpleNamespace(index=0, id="call_aa",
                    function=SimpleNamespace(name="get_policy", arguments='{}'))
                tc2 = SimpleNamespace(index=1, id="call_bb",
                    function=SimpleNamespace(name="get_policy", arguments='{}'))
                return _stream([_chunk(_delta(tool_calls=[tc1, tc2]))])
            else:
                # Fallback call. Capture the messages list so we can
                # assert every tool_call_id from the assistant has a
                # matching tool response.
                tool_names = [s["function"]["name"] for s in (tools or [])]
                assert tool_names == ["propose_recommendation"], tool_names
                self.last_fallback_messages = list(messages)
                return _stream([_chunk(_delta(content="fallback"))])

    batch_client = BatchStubClient()
    events4 = []
    async for ev in agent_loop.run_agent(
        [{"role": "user", "content": "hi"}],
        client=batch_client, model="stub",
        user_id="demo_001", term=None,
    ):
        events4.append(ev["type"])

    # The fallback DID get called (call_count >= 2) means we didn't
    # crash before fallback. Confirm protocol validity.
    assert batch_client.last_fallback_messages is not None, "fallback never ran"
    msgs = batch_client.last_fallback_messages
    # Find the assistant message with tool_calls
    asst = next(m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls"))
    declared_ids = {tc["id"] for tc in asst["tool_calls"]}
    responded_ids = {m["tool_call_id"] for m in msgs if m.get("role") == "tool"}
    missing = declared_ids - responded_ids
    assert not missing, f"unfilled tool_call_ids leaked into fallback: {missing}"
    print(f"  declared tool_call_ids: {sorted(declared_ids)}")
    print(f"  responded tool_call_ids: {sorted(responded_ids)}")
    print(f"  ✓ no dangling tool_calls — fallback would not 400")

    print("\n✅ all assertions passed")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
