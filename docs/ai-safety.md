# AI instruction authority and prompt-injection boundaries

The application-owned system instructions have the highest instruction priority.
If a user request, custom task/style extension, saved memory, history, document,
transcript, or tool/source result conflicts with them, the model is instructed to
follow the system instructions. An embedded claim to be a system, developer,
administrator, or evaluator message never grants authority.

The shared policy is `app/llm/safety.py::SYSTEM_SAFETY_POLICY`. It precedes all six
adapter prompts and is also applied by `_call_llm` to other auxiliary prompts.
It rejects instruction bypass, protected prompt/secret disclosure, cross-account
private-data requests, and instructions to transmit private context via external
tools. It also instructs the model not to memorize jailbreak commands as student
preferences. Existing server-side ownership, tool argument validation, term scope,
URL validation, and tool budgets remain authoritative.

## Actual prompt assembly

- The primary chat agent uses `app/llm/adapter.py::AGENT_SYSTEM_PROMPT`.
- Legacy answers use `ANSWER_SYSTEM_PROMPT`; intent classification, extraction,
  reflection, and titles each keep their own task instructions after the policy.
- `app/llm/context_builder.py` creates one leading system message containing only
  server-owned policy, base instructions, runtime term rules, and reply language.
- Custom configuration, student profile/preferences/facts, prior decisions,
  conversation summaries, retrieved documents, and memory evidence become
  serialized user-role context data. History accepts only user/assistant roles;
  the current question remains the final user message.
- Agent workflow/history guidance keeps fixed server rules separate from source
  content. Workflow results and historical queries/answers/URLs are user-role
  context; normal model tool results retain their tool role. Role names and
  instruction-looking text inside payloads cannot create message roles.
- `system_prompt` customizations extend the task/style at lower priority. They
  cannot replace the default policy or base. Production disables customizations
  and `/api/system_prompt` by default through `allow_custom_system_prompt()`.
  When enabled, that endpoint now returns the actual main agent base.

The complete main base prompt, without student data or per-turn runtime values,
is saved in [current-agent-system-prompt.txt](current-agent-system-prompt.txt).
The source constants are authoritative; regenerate the text snapshot after edits:

```sh
.venv/bin/python - <<'PY'
from pathlib import Path
from app.llm.adapter import AGENT_SYSTEM_PROMPT
Path('docs/current-agent-system-prompt.txt').write_text(AGENT_SYSTEM_PROMPT, encoding='utf-8')
PY
```

## Validation and limits

Run the focused offline tests:

```sh
.venv/bin/python -m pytest tests/test_prompt_injection_safety.py \
  tests/test_response_language.py tests/test_private_beta_security.py \
  tests/test_runtime_context.py tests/test_sqlite_evidence_memory.py \
  tests/test_agent_loop_protocol.py tests/test_agent_deep_search_history.py \
  tests/test_workflow_router.py --no-cov -q
```

These tests verify message roles, immutable policy retention, context placement,
tool-result isolation, and existing application controls using mocked model calls.
They do not measure live model jailbreak resistance or establish a guarantee
against all prompt injections. Prompt instructions and serialization reduce risk;
real model behavior still requires adversarial evaluation. This distinction also
applies to the term/presentation rules documented in [term-rules.md](term-rules.md).

Primary guidance: [OpenAI — Safety in building agents](https://developers.openai.com/api/docs/guides/agent-builder-safety),
which recommends keeping untrusted input out of privileged instruction messages
and constraining data flow with structured formats.
