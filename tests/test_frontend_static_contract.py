from pathlib import Path
import re


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


def test_frontend_term_is_read_only_and_backend_resolved() -> None:
    text = _frontend_text()
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    api_client = (STATIC / "js" / "api-client.js").read_text(encoding="utf-8")
    chat = (STATIC / "js" / "chat.js").read_text(encoding="utf-8")
    sessions = (STATIC / "js" / "sessions.js").read_text(encoding="utf-8")

    assert 'id="termDisplay"' in index
    assert 'aria-label="Current academic term"' in index
    assert "termSelect" not in text
    assert "term-select" not in text
    assert "loadTerms(" not in text
    assert "/api/term-state" in api_client
    assert "· fallback" in api_client
    assert "payload.term" not in chat
    assert "applyTermPayload(event)" in chat
    assert "useAutomaticTermContext()" in sessions
    assert "applyTermPayload(data)" in sessions


def test_frontend_assets_are_split_into_roadmap_modules() -> None:
    missing = [path.relative_to(ROOT).as_posix() for path in EXPECTED_FRONTEND_MODULES if not path.exists()]
    assert missing == []

    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "<style>" not in index
    assert "<script>" not in index

    for path in EXPECTED_FRONTEND_MODULES:
        rel = "/" + path.relative_to(ROOT).as_posix()
        assert rel in index


def test_key_browser_regression_flows_are_wired() -> None:
    chat = (STATIC / "js" / "chat.js").read_text(encoding="utf-8")
    cards = (STATIC / "js" / "cards.js").read_text(encoding="utf-8")
    schedule = (STATIC / "js" / "schedule.js").read_text(encoding="utf-8")
    auth = (STATIC / "js" / "auth.js").read_text(encoding="utf-8")
    onboarding = (STATIC / "js" / "onboarding.js").read_text(encoding="utf-8")

    assert "/api/auth/login" in auth
    assert "/api/auth/request_code" in auth
    assert "/api/auth/verify" in auth
    assert "function continueAsGuest(" in auth

    assert "/api/onboarding/schools" in onboarding
    assert "/api/onboarding/majors" in onboarding
    assert "/api/onboarding/courses/all" in onboarding
    assert "/api/memory/" in onboarding

    assert "/api/chat/stream" in chat
    assert "function startToolChip(" in chat
    assert "tool-chip-web-search" in chat
    assert 'event.label || event.name, event.name' in chat
    assert "function renderContinueBanner(" in chat
    assert "/api/chat/continue" in chat
    assert '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>' in chat

    assert "SOURCE_BADGE_LABELS" in cards
    assert "DB Verified" in cards
    assert "Official UCI" in cards
    assert "Live WebSoc" in cards
    assert "WebSoc Comments" in cards
    assert "Official Department Link" in cards
    assert "Not Live" in cards
    assert "External Web" in cards
    assert "field_source_badges" in cards
    assert "course_provenance" in cards
    assert "sectionSources" in cards
    assert "section?.source" in cards

    assert "tool-chip-live-websoc" in chat
    assert "tool-chip-websoc-comments" in chat

    assert "/api/schedule/add" in schedule
    assert "/api/schedule/remove" in schedule
    assert "/api/schedule/clear" in schedule
    assert "toggleScheduleBtn')?.setAttribute('aria-expanded', 'true')" in schedule
    assert "toggleScheduleBtn')?.setAttribute('aria-expanded', String(scheduleOpen))" in schedule


def test_cross_term_schedule_identity_and_notice_are_wired() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    api_client = (STATIC / "js" / "api-client.js").read_text(encoding="utf-8")
    cards = (STATIC / "js" / "cards.js").read_text(encoding="utf-8")
    schedule = (STATIC / "js" / "schedule.js").read_text(encoding="utf-8")

    assert "function scheduleEntryKey(term, courseId, section)" in api_client
    assert "let pendingScheduleEntries = []" in api_client
    assert 'data-term="${escAttr(term)}"' in cards
    assert "term:       term || null" in schedule
    assert "/api/schedule?session_id=" in schedule
    assert "function _renderScheduleEntryList()" in schedule
    assert "evEl.dataset.term = ev.term || ''" in schedule
    assert "ev.term || ''" in schedule
    assert "requires_confirmation" not in schedule
    assert "confirm_conflicts" not in schedule

    assert 'id="scheduleToastRegion"' in index
    assert "function showCrossTermToast(notice)" in schedule
    assert "function dismissScheduleToast()" in schedule
    assert "setTimeout(dismissScheduleToast, 5000)" in schedule


def test_live_and_restored_messages_share_structured_renderers() -> None:
    chat = (STATIC / "js" / "chat.js").read_text(encoding="utf-8")
    sessions = (STATIC / "js" / "sessions.js").read_text(encoding="utf-8")

    assert "renderCardsBlock(meta.cards)" in chat
    assert "renderValidationFooter(meta.validation_report)" in chat
    assert "meta.final_answer" in chat
    assert "sendFollowup(" in chat

    assert "renderCardsBlock(extras.cards)" in sessions
    assert "renderValidationFooter(extras.validation)" in sessions
    assert "sendFollowup(" in sessions


def test_frontend_accessibility_and_mobile_contracts() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "styles" / "components.css").read_text(encoding="utf-8")

    assert 'aria-label="Current academic term"' in index
    assert 'aria-label="Ask ZotAdvisor a question"' in index
    assert 'role="log"' in index
    assert 'aria-live="polite"' in index
    assert 'role="dialog"' in index
    assert 'aria-modal="true"' in index
    assert '<a onclick="continueAsGuest()"' not in index
    assert 'class="auth-guest-btn"' in index
    assert 'aria-label="Close settings"' in index
    assert 'aria-label="Close weekly schedule"' in index
    assert index.count("Data from <a href=\"https://icssc.link/about-anteaterapi\"") == 2

    assert ":focus-visible" in css
    assert "@media (max-width: 700px)" in css
    assert "@media (max-width: 680px)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_core_color_tokens_keep_minimum_text_contrast() -> None:
    tokens = (STATIC / "styles" / "tokens.css").read_text(encoding="utf-8")
    colors = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", tokens))

    def rgb(hex_color: str) -> tuple[float, float, float]:
        raw = hex_color.lstrip("#")
        return tuple(int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def luminance(hex_color: str) -> float:
        channels = []
        for channel in rgb(hex_color):
            channels.append(
                channel / 12.92 if channel <= 0.03928
                else ((channel + 0.055) / 1.055) ** 2.4
            )
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    def contrast(foreground: str, background: str) -> float:
        a = luminance(colors[foreground])
        b = luminance(colors[background])
        lighter, darker = max(a, b), min(a, b)
        return (lighter + 0.05) / (darker + 0.05)

    pairs = [
      ("text-primary", "bg-app"),
      ("text-secondary", "bg-app"),
      ("text-tertiary", "bg-app"),
      ("text-muted", "bg-app"),
      ("text-primary", "bg-panel"),
      ("bg-panel", "accent"),
      ("rmp-mixed-text", "rmp-mixed"),
    ]
    failures = {
        pair: round(contrast(*pair), 2)
        for pair in pairs
        if contrast(*pair) < 4.5
    }
    assert failures == {}
