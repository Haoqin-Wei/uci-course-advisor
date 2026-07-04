from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def _frontend_text() -> str:
    parts = []
    for suffix in ("*.html", "*.js", "*.css"):
        for path in sorted(STATIC.rglob(suffix)):
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_streaming_uses_single_shared_sse_consumer() -> None:
    text = _frontend_text()

    assert "function consumeSSE(" in text
    assert text.count(".getReader()") == 1
    assert "function appendTyping(" not in text
    assert "function appendAI(" not in text


def test_frontend_does_not_fake_term_or_duplicate_palette_tokens() -> None:
    text = _frontend_text()

    assert "Spring 2026</span>" not in text
    assert "const COLORS = [" not in text
    assert "rgba(79, 93, 128, 0.12)']" not in text
