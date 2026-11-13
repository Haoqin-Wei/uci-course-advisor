"""One isolated real-model reading check, separate from the recorded app run.

Input: unchanged real tool messages captured from live public API responses.
This checks comprehension only, without app routing or automatic tools.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OUT = Path(__file__).resolve().parent / "reading-check"
OUT.mkdir(exist_ok=True)
SOURCE = ROOT / "artifacts/anteater-payload-audit-2026-11-05"
from app.llm import adapter

QUESTION = "请核实 SWE43 和 IN4MATX43 是什么关系，并说明依据。假设我在 2026 Winter 修过 IN4MATX43，而且已经获得这门课的学分，还能在 2026 Fall 再修 SWE43 吗？请根据课程说明回答。"


def write(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


async def main():
    messages = [{"role": "system", "content": adapter.AGENT_SYSTEM_PROMPT},
                {"role": "user", "content": QUESTION}]
    tool_messages, tool_calls, sources = [], [], []
    for stem, course, term in (("2026-fall-swe43", "SWE43", "2026 Fall"),
                               ("2026-winter-in4matx43", "IN4MATX43", "2026 Winter")):
        path = SOURCE / f"{stem}-ai-tool-message.json"
        item = json.loads(path.read_text())
        tool_messages.append(item)
        tool_calls.append({"id": item["tool_call_id"], "type": "function", "function": {
            "name": "get_live_sections", "arguments": json.dumps({"course_id": course, "term": term, "force_refresh": True})}})
        sources.append({"file": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    messages.append({"role": "assistant", "content": None, "tool_calls": tool_calls})
    messages.extend(tool_messages)
    messages.append({"role": "user", "content": "以上是程序实际取得的工具结果。请只依据这些记录回答最初的问题；证据不足的部分明确说明。"})
    kwargs = {"model": adapter.LLM_MODEL, "messages": messages, "stream": True}
    write("model-request.json", kwargs)
    metadata = {"model": adapter.LLM_MODEL, "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "purpose": "isolated_comment_comprehension_only_not_app_routing_acceptance",
                "source_tool_messages_unchanged": True, "sources": sources,
                "original_application_system_prompt": True, "max_wall_seconds": 120,
                "provider_requests": 1}
    client = adapter._get_client()
    start = time.monotonic()
    text, finishes, usage = [], [], []
    try:
        async with asyncio.timeout(120):
            stream = await client.chat.completions.create(**kwargs)
            try:
                async for chunk in stream:
                    if chunk.usage is not None:
                        usage.append(chunk.usage.model_dump())
                    for choice in chunk.choices:
                        if choice.delta.content:
                            text.append(choice.delta.content)
                        if choice.finish_reason:
                            finishes.append(choice.finish_reason)
            finally:
                await stream.close()
        answer = "".join(text)
        (OUT / "answer.txt").write_text(answer, encoding="utf-8")
        metadata.update(outcome="completed", finish_reasons=finishes, usage=usage)
        print(json.dumps({"model_answer": answer, "finish_reasons": finishes}, ensure_ascii=False), flush=True)
    except Exception as exc:
        error = str(exc).replace(adapter.DEEPSEEK_API_KEY, "[redacted]") if adapter.DEEPSEEK_API_KEY else str(exc)
        metadata.update(outcome="error", error_type=type(exc).__name__, error=error)
        print(json.dumps({"error_type": type(exc).__name__, "error": error}, ensure_ascii=False), flush=True)
    finally:
        metadata["elapsed_seconds"] = time.monotonic() - start
        write("run-metadata.json", metadata)
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
