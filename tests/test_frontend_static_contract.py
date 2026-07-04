from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
EXPECTED_FRONTEND_MODULES = {
    STATIC / "styles" / "tokens.css",
    STATIC / "styles" / "components.css",
    STATIC / "js" / "api-client.js",
    STATIC / "js" / "chat.js",
    STATIC / "js" / "cards.js",
    STATIC / "js" / "schedule.js",
    STATIC / "js" / "sessions.js",
    STATIC / "js" / "auth.js",
    STATIC / "js" / "profile-memory.js",
    STATIC / "js" / "onboarding.js",
}


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


def test_frontend_assets_are_split_into_roadmap_modules() -> None:
    missing = [path.relative_to(ROOT).as_posix() for path in EXPECTED_FRONTEND_MODULES if not path.exists()]
    assert missing == []

    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "<style>" not in index
    assert "<script>" not in index

    for path in EXPECTED_FRONTEND_MODULES:
        rel = "/" + path.relative_to(ROOT).as_posix()
        assert rel in index
