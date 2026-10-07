from app import observability
from app.llm.context_builder import build_messages
from app.llm.safety import untrusted_context_message


def test_prompt_layer_estimate_counts_full_context_not_only_question() -> None:
    messages = [
        {
            "role": "system",
            "content": (
                "base rules\n\n"
                "# Persistent student profile\n- Computer Science\n\n"
                '<runtime_context source="application">\n'
                "  <default_term>2025 Fall</default_term>\n"
                "</runtime_context>\n\n"
                "<runtime_rules>\n  <rule>immutable</rule>\n</runtime_rules>"
            ),
        },
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "tool", "content": '{"term":"2025 Fall"}'},
        {"role": "user", "content": "Q3"},
    ]
    layers = observability.estimate_prompt_layers(
        messages,
        tool_schemas=[{"name": "get_sections", "parameters": {"term": "string"}}],
    )
    assert layers["base_system"] > 0
    assert layers["runtime_context"] > 0
    assert layers["memory_summary"] > 0
    assert layers["recent_turns"] > 0
    assert layers["tool_schemas"] > 0
    assert layers["tool_calls_results"] > 0
    assert layers["current_user"] == 1
    assert layers["total_input"] > observability.estimate_tokens("Q3")


def test_real_builder_context_data_is_counted_in_its_own_layers_once() -> None:
    history = [
        {"role": "user", "content": "Earlier question"},
        {"role": "assistant", "content": "Earlier answer"},
    ]
    memory_texts = [
        "Computer Science", "Prefers morning classes", "Completed ICS 31",
        "Take ICS 33", "Earlier discussion was about prerequisites",
        "Legacy profile includes a writing preference", "Recalled morning preference",
    ]
    extension = "Use a concise course comparison"
    messages = build_messages(
        "Application-owned advisor rules",
        "Q3",
        profile={"major": memory_texts[0]},
        preferences=[memory_texts[1]],
        facts=[memory_texts[2]],
        decisions=[{"text": memory_texts[3]}],
        summary=memory_texts[4],
        context_messages=[
            {"source": "legacy student memory", "content": memory_texts[5]},
            {"source": "custom style/task extension", "content": extension},
        ],
        recent_turns=history,
        retrieved_data={"courses": [{"course_id": "ICS 33", "title": "Intermediate Programming"}]},
        memory_evidence=memory_texts[6],
    )
    result_evidence = [
        untrusted_context_message("Primary workflow results", '{"sections": ["A"]}'),
        untrusted_context_message("Deep-search history hint", '{"paths": ["public course page"]}'),
    ]
    messages[-1:-1] = result_evidence
    schemas = [{"name": "get_sections", "parameters": {"term": "string"}}]

    layers = observability.estimate_prompt_layers(messages, tool_schemas=schemas)

    assert layers["memory_summary"] >= sum(observability.estimate_tokens(text) for text in memory_texts)
    assert layers["context_data"] >= observability.estimate_tokens(extension)
    assert layers["recent_turns"] == sum(observability.estimate_tokens(turn["content"]) for turn in history)
    assert layers["tool_calls_results"] > sum(observability.estimate_tokens(item["content"]) for item in result_evidence)
    assert layers["current_user"] == observability.estimate_tokens("Q3")
    expected_total = (
        sum(observability.estimate_tokens(block) for block in messages[0]["content"].split("\n\n"))
        + sum(observability.estimate_tokens(message["content"]) for message in messages[1:])
        + layers["tool_schemas"]
    )
    assert layers["total_input"] == expected_total
    assert layers["total_input"] == sum(value for name, value in layers.items() if name != "total_input")
