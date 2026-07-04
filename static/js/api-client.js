const API = '';

/* ──────────────────────────────────────────────────────
   Phase 3 R3 — session management
   ─────────────────────────────────────────────────────
   USER_ID is fixed for the demo (single-tenant). The session_id is
   no longer hardcoded — it's:
     null            → "next message starts a new session"
                       (the empty-state UI is shown)
     "sess_XXXXXX"   → an active persistent session loaded from disk
   When sendMessage() sends an empty session_id, the backend
   auto-creates a new session and returns its real ID via the meta
   event; we then update currentSessionId and refresh the sidebar list.
*/
// USER_ID is the value the frontend sends in URLs/bodies. The backend
// IGNORES the value and pulls the authoritative user id from the auth
// cookie via current_user_optional (anon → demo_001, real session →
// the registered user's UUID). So this is purely a URL-shape stand-in;
// any non-empty string works. Updated to the email after login for
// display purposes only.
let USER_ID = 'demo_001';
// Populated by /api/auth/me on boot or after a successful login.
// null = guest (backend will route to demo_001).
let currentAuthUser = null;
let currentSessionId = null;
let addedCourses = new Set();
// Per-section add state for Phase E4's section picker. Keys are
// "<course_id>:<section_num>" — e.g. "CS161:A" for the Lec, "CS161:A1"
// for a Discussion. Distinct from addedCourses (course-level) which
// the schedule rail's counter still uses. A course is in addedCourses
// as long as ANY of its sections is in addedSectionKeys.
let addedSectionKeys = new Set();
let scheduleEvents = [];
let scheduleOpen = false;
let colorIndex = 0;
const courseColors = {};

/* ── Settings / generation state ───────────────────── */
let currentAbortController = null;   // active fetch controller (null when idle)
let defaultSystemPrompt = '';        // populated by /api/system_prompt
const PROMPT_STORAGE_KEY = 'zotadvisor.systemPrompt';

const COURSE_COLOR_TOKEN_COUNT = 6;

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function scheduleColorPair(index) {
  const n = (index % COURSE_COLOR_TOKEN_COUNT) + 1;
  return [cssVar(`--course-${n}`), cssVar(`--course-${n}s`)];
}

function getCourseColor(id) {
  if (!courseColors[id]) {
    courseColors[id] = scheduleColorPair(colorIndex);
    colorIndex++;
  }
  return courseColors[id];
}

const inputEl = document.getElementById('userInput');
inputEl.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});

// Keep the welcome-state subtitle in sync when the user toggles term.
// The select exists from page load; loadTerms() only populates options,
// so attaching the listener immediately is safe.
document.getElementById('termSelect')?.addEventListener('change', () => updateWelcomeTerm());

/* ── Boot: populate terms dropdown + cache default prompt ── */
async function loadTerms() {
  const select = document.getElementById('termSelect');
  try {
    const res = await fetch(`${API}/api/terms`);
    if (!res.ok) throw new Error(`status ${res.status}`);
    const data = await res.json();
    const terms = (data.terms || []).filter(t => t && typeof t === 'object' && t.name);
    const def = data.default || (terms.length ? terms[0].name : '');
    if (!terms.length) {
      select.innerHTML = '<option value="" disabled selected>Terms unavailable</option>';
      updateWelcomeTerm();
      return;
    }
    select.innerHTML = terms.map(t => {
      const status = t.coverage_status || 'unavailable';
      const suffix = status === 'complete' ? '' : ` (${status})`;
      return `<option value="${escAttr(t.name)}"${t.name === def ? ' selected' : ''}>${escHTML(t.name + suffix)}</option>`;
    }
    ).join('');
    updateWelcomeTerm();  // dropdown now populated — refresh subtitle
  } catch (err) {
    console.warn('Failed to load /api/terms', err);
    select.innerHTML = '<option value="" disabled selected>Terms unavailable</option>';
    updateWelcomeTerm();
  }
}

async function loadDefaultPromptFromAPI() {
  try {
    const res = await fetch(`${API}/api/system_prompt`);
    if (!res.ok) throw new Error(`status ${res.status}`);
    const data = await res.json();
    defaultSystemPrompt = data.prompt || '';
  } catch (err) {
    console.warn('Failed to load /api/system_prompt — custom prompts can still be set manually', err);
    defaultSystemPrompt = '';
  }
}

function escAttr(s) {
  return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function getActivePrompt() {
  return localStorage.getItem(PROMPT_STORAGE_KEY) || '';
}

async function consumeSSE(response, handlers = {}) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  function dispatchBlock(block) {
    const lines = block.split('\n');
    const payload = lines
      .filter(line => line.startsWith('data: '))
      .map(line => line.slice(6))
      .join('\n');
    if (!payload) return;

    let event;
    try {
      event = JSON.parse(payload);
    } catch (err) {
      if (handlers.parseError) handlers.parseError(payload, err);
      else console.warn('SSE parse error:', payload);
      return;
    }

    const handler = handlers[event.type];
    if (handler) handler(event);
  }

  while (true) {
    const {done, value} = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, {stream: true});

    let sepIdx;
    while ((sepIdx = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, sepIdx);
      buffer = buffer.slice(sepIdx + 2);
      dispatchBlock(block);
    }
  }

  buffer += decoder.decode();
  if (buffer.trim()) dispatchBlock(buffer.trim());
}
