"""Compare public HTTP captures to the exact tool messages sent to the model."""
import json
from pathlib import Path
import sys


root = Path(__file__).resolve().parent
run = root / (sys.argv[1] if len(sys.argv) > 1 else "run-03")
http = json.loads((run / "source-http-requests.json").read_text())
sources = {}
for row in http:
    args = row.get("params") or {}
    if row.get("status") == 200 and row["url"].endswith("/websoc") and args.get("courseNumber"):
        sources[(args["department"] + args["courseNumber"], args["year"] + " " + args["quarter"])] = (
            json.loads((run / row["response_file"]).read_text()), row["response_file"])

cases = {}
for request in sorted(run.glob("model-call-*-request.json")):
    calls = {}
    for message in json.loads(request.read_text())["messages"]:
        if message["role"] == "assistant":
            calls.update({call["id"]: call["function"] for call in message.get("tool_calls") or []})
        elif message["role"] == "tool":
            call = calls.get(message["tool_call_id"], {})
            if call.get("name") != "get_live_sections":
                continue
            args = json.loads(call["arguments"])
            key = ("".join(args["course_id"].split()).upper(), args["term"])
            if key in sources:
                cases[key] = (json.loads(message["content"]), request.name)

checks = []
for key, (payload, request_file) in cases.items():
    raw, raw_file = sources[key]
    sections = {str(row["section_code"]): row for row in payload["sections"]}
    contexts = list(payload["comment_contexts"].values())
    comment_count = meeting_count = 0
    for school in raw["data"]["schools"]:
        for department in school["departments"]:
            for course in department["courses"]:
                matches = [context for context in contexts if context["course"]["course_id"] == course["courseId"]]
                assert matches, (key, "course context missing")
                for context in matches:
                    for scope, source, fields in (
                        ("school", school, (("schoolComment", "comment"),)),
                        ("department", department, (("deptComment", "comment"),
                            ("courseNumberRangeComments", "course_number_range_comments"),
                            ("sectionCodeRangeComments", "section_code_range_comments"))),
                        ("course", course, (("courseComment", "comment"),)),
                    ):
                        for original, derived in fields:
                            assert source.get(original) == context[scope][derived], (key, scope, original)
                            comment_count += 1
                for section in course["sections"]:
                    received = sections[str(section["sectionCode"])]
                    assert received["section_comment"] == section.get("sectionComment")
                    assert received["meetings"] == section.get("meetings")
                    comment_count += 1
                    meeting_count += 1
    if key[0] == "SWE43":
        assert any(row["source_text"] == "INF 43" and row["normalized_course_id"] == "IN4MATX 43"
                   for row in payload["course_code_normalizations"])
    checks.append({"course_id": key[0], "term": key[1], "raw_file": raw_file,
                   "actual_model_request": request_file, "all_comments_unchanged": True,
                   "all_meetings_unchanged": True, "comment_fields_checked": comment_count,
                   "meeting_arrays_checked": meeting_count, "source": payload["source"]})
assert len(checks) == 2, "Both requested course/term pairs must appear in actual model input"
report = {"all_passed": True, "cases": checks}
(run / "live-payload-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(report, ensure_ascii=False))
