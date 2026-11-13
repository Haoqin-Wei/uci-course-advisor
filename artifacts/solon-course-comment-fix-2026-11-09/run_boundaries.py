"""Real-model reading diagnostics with explicitly synthetic boundary fixtures.

These are not live API responses or end-to-end application acceptance tests.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.llm import adapter

OUT = Path(__file__).resolve().parent / "synthetic-boundaries"
OUT.mkdir(exist_ok=True)
QUESTION = "SWE43 和 IN4MATX43 是什么关系？假设我已取得 IN4MATX43 的学分，还能再修 SWE43 吗？请根据给出的课程说明回答。"


async def main():
    client = adapter._get_client()

    async def run(name, comment):
        evidence = {
            "source": "synthetic_boundary_fixture", "term": "2026 Fall", "sections": [],
            "comment_contexts": {"synthetic": {"course": {"course_id": "SWE43", "comment": comment}}},
            "course_notices": [{"scope": "course", "course_id": "SWE43", "comment_context_id": "synthetic"}],
        }
        if comment:
            evidence["course_code_normalizations"] = [{
                "source_text": "INF 43", "normalized_course_id": "IN4MATX 43",
                "basis": "existing_department_alias_rules",
                "source_path": "/comment_contexts/synthetic/course/comment",
            }]
        messages = [
            {"role": "system", "content": adapter.AGENT_SYSTEM_PROMPT},
            {"role": "user", "content": QUESTION},
            {"role": "assistant", "content": "", "reasoning_content": "", "tool_calls": [{
                "id": "synthetic-comment", "type": "function", "function": {
                    "name": "get_live_sections", "arguments": json.dumps({"course_id": "SWE43", "term": "2026 Fall"}),
                },
            }]},
            {"role": "tool", "tool_call_id": "synthetic-comment", "content": json.dumps(evidence)},
            {"role": "user", "content": "以上是明确标注的合成边界测试记录，不是实时 API 数据。本次只测试阅读理解；请仅依据这些记录回答原问题，没有提供的事实请说明无法确认。"},
        ]
        (OUT / (name + "-request.json")).write_text(json.dumps(messages, ensure_ascii=False, indent=2) + "\n")
        text = ""
        async with asyncio.timeout(90):
            stream = await client.chat.completions.create(model=adapter.LLM_MODEL, messages=messages, stream=True)
            try:
                async for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        text += chunk.choices[0].delta.content
            finally:
                await stream.close()
        (OUT / (name + "-answer.txt")).write_text(text)
        print(json.dumps({"case": name, "answer": text}, ensure_ascii=False), flush=True)
        return {"case": name, "completed": True, "source_is_synthetic": True, "answer_file": name + "-answer.txt"}

    try:
        cases = await asyncio.gather(
            run("former-code-only", 'Previously offered as INF 43, "Intro Software Engr".'),
            run("missing-comment", ""),
        )
        report = {"model": adapter.LLM_MODEL, "started_at_utc": datetime.now(timezone.utc).isoformat(),
                  "provider_requests": 2, "application_prompt_sha256": hashlib.sha256(adapter.AGENT_SYSTEM_PROMPT.encode()).hexdigest(),
                  "test_type": "synthetic_reading_only_not_live_api_or_application_routing", "cases": cases}
        (OUT / "run-metadata.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
