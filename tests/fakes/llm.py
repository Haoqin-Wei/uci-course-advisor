"""Scriptable offline replacement for the streaming LLM client."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any


@dataclass(frozen=True)
class RecordedCall:
    """One immutable snapshot of a chat-completions request."""

    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    tool_choice: str | None
    stream: bool


@dataclass(frozen=True)
class ToolCall:
    """A complete tool call emitted in one streaming delta."""

    name: str
    arguments: str
    call_id: str
    index: int = 0


@dataclass(frozen=True)
class _Delta:
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ScriptedResponse:
    """Ordered streaming deltas returned by one fake LLM request."""

    deltas: tuple[_Delta, ...]


ScriptStep = (
    ScriptedResponse
    | BaseException
    | Callable[[RecordedCall], ScriptedResponse | BaseException]
)


def text_response(*tokens: str) -> ScriptedResponse:
    """Build a plain-text streaming response."""

    return ScriptedResponse(tuple(_Delta(content=token) for token in tokens))


def tool_call(
    name: str,
    arguments: Mapping[str, Any] | str | None = None,
    *,
    call_id: str | None = None,
    index: int = 0,
) -> ToolCall:
    """Build one OpenAI-compatible function call delta."""

    if arguments is None:
        encoded_arguments = "{}"
    elif isinstance(arguments, str):
        encoded_arguments = arguments
    else:
        encoded_arguments = json.dumps(arguments)

    return ToolCall(
        name=name,
        arguments=encoded_arguments,
        call_id=call_id or f"call_{index}",
        index=index,
    )


def tool_response(
    *tool_calls: ToolCall,
    leading_tokens: Sequence[str] = (),
    reasoning_tokens: Sequence[str] = (),
) -> ScriptedResponse:
    """Build optional narration/reasoning followed by tool calls."""

    deltas = [_Delta(content=token) for token in leading_tokens]
    deltas.extend(_Delta(reasoning_content=token) for token in reasoning_tokens)
    deltas.append(_Delta(tool_calls=tuple(tool_calls)))
    return ScriptedResponse(tuple(deltas))


class ScriptedLLMClient:
    """Minimal async client that consumes one scripted step per request."""

    def __init__(self, *steps: ScriptStep):
        self._steps = list(steps)
        self.calls: list[RecordedCall] = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    async def _create(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        stream: bool,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ):
        call = RecordedCall(
            model=model,
            messages=copy.deepcopy(messages),
            tools=copy.deepcopy(tools),
            tool_choice=tool_choice,
            stream=stream,
        )
        self.calls.append(call)

        if not self._steps:
            raise AssertionError(
                f"Unexpected fake LLM request #{len(self.calls)}; no scripted steps remain"
            )

        step = self._steps.pop(0)
        result = step(call) if callable(step) else step
        if isinstance(result, BaseException):
            raise result
        if not isinstance(result, ScriptedResponse):
            raise TypeError(
                "Fake LLM steps must resolve to ScriptedResponse or BaseException"
            )
        return _stream(result)

    def assert_exhausted(self) -> None:
        """Fail when a test did not consume every expected LLM request."""

        if self._steps:
            raise AssertionError(
                f"{len(self._steps)} scripted fake LLM step(s) were not consumed"
            )


async def _stream(response: ScriptedResponse):
    for delta in response.deltas:
        tool_calls = [
            SimpleNamespace(
                index=item.index,
                id=item.call_id,
                function=SimpleNamespace(
                    name=item.name,
                    arguments=item.arguments,
                ),
            )
            for item in delta.tool_calls
        ]
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=delta.content,
                        reasoning_content=delta.reasoning_content,
                        tool_calls=tool_calls,
                    )
                )
            ]
        )
