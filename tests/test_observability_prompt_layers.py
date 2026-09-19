from app import observability


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
