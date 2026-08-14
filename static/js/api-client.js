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
let automaticTermContext = null;
let currentTermContext = null;
let addedCourses = new Set();
// Schedule identity is term-scoped. This keeps the same course/section in
// two quarters independently addable and removable.
let addedSectionKeys = new Set();
let pendingScheduleEntries = [];
let scheduleEvents = [];
let scheduleValidation = {valid: true, warnings: [], conflicts: [], unknowns: []};
let scheduleOpen = false;
let colorIndex = 0;
const courseColors = {};

function scheduleEntryKey(term, courseId, section) {
  return `${term || 'unknown'}|${courseId || ''}:${section || ''}`;
}

function scheduleCourseKey(term, courseId) {
  return `${term || 'unknown'}|${courseId || ''}`;
}

/* ── Settings / generation state ───────────────────── */
let currentAbortController = null;   // active fetch controller (null when idle)
let defaultSystemPrompt = null;      // loaded lazily when Settings opens
let defaultSystemPromptPromise = null;
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

function setTermContext(term, source, status, mode, availableTerms) {
  if (!term) return;
  const normalizedMode = mode === 'manual' ? 'manual' : 'auto';
  const terms = Array.from(new Set(
    [...(availableTerms || []), term].filter(Boolean)
  ));
  currentTermContext = {
    term,
    source: source || null,
    status: status || null,
    mode: normalizedMode,
    availableTerms: terms,
  };
  renderTermSelector();
  updateWelcomeTerm();
}

function applyTermPayload(payload) {
  if (!payload) return;
  const term = payload.default_term || payload.automatic_term;
  const source = payload.term_source || payload.source;
  const status = payload.term_status || payload.status;
  const mode = payload.term_mode || 'auto';
  setTermContext(term, source, status, mode, payload.available_terms);
}

function useAutomaticTermContext() {
  if (automaticTermContext) applyTermPayload(automaticTermContext);
}

function renderTermSelector() {
  const select = document.getElementById('termSelect');
  if (!select || !currentTermContext) return;
  const automatic = automaticTermContext?.automatic_term || currentTermContext.term;
  const choices = currentTermContext.availableTerms || [];
  const options = [
    `<option value="__auto__">Auto · ${escHTML(automatic)}</option>`,
    ...choices.map(term => (
      `<option value="${escAttr(term)}">${escHTML(term)}</option>`
    )),
  ];
  select.innerHTML = options.join('');
  select.value = currentTermContext.mode === 'manual'
    ? currentTermContext.term
    : '__auto__';
  select.disabled = false;
  const restore = document.getElementById('termRestoreAuto');
  if (restore) restore.hidden = currentTermContext.mode !== 'manual';
}

function setTermError(message) {
  const el = document.getElementById('termError');
  if (!el) return;
  el.textContent = message || '';
  el.title = message || '';
}

async function ensureSessionForTermMutation() {
  if (currentSessionId) return currentSessionId;
  const response = await fetch(`${API}/api/sessions/${USER_ID}`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({title: 'New conversation'}),
  });
  if (!response.ok) throw new Error(`Could not create conversation (${response.status})`);
  const data = await response.json();
  currentSessionId = data.session_id;
  applyTermPayload(data);
  setActiveSessionItem(currentSessionId);
  loadSessionList();
  return currentSessionId;
}

async function mutateDefaultTerm(mode, term) {
  const select = document.getElementById('termSelect');
  if (select) select.disabled = true;
  setTermError('');
  try {
    const sessionId = await ensureSessionForTermMutation();
    const response = await fetch(`${API}/api/sessions/${sessionId}/default-term`, {
      method: 'PUT',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(mode === 'manual' ? {mode, term} : {mode: 'auto'}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = data.detail?.message || data.detail || `HTTP ${response.status}`;
      throw new Error(String(detail));
    }
    applyTermPayload(data);
    return true;
  } catch (error) {
    setTermError(`Term not changed: ${error.message}`);
    renderTermSelector();
    return false;
  } finally {
    if (select) select.disabled = false;
  }
}

async function handleTermSelection(event) {
  const requested = event.target.value;
  // Keep the authoritative value visible until the API confirms the change.
  renderTermSelector();
  if (requested === '__auto__') {
    await mutateDefaultTerm('auto');
  } else {
    await mutateDefaultTerm('manual', requested);
  }
}

async function restoreAutoTerm() {
  await mutateDefaultTerm('auto');
}

async function confirmSuggestedTerm(term) {
  await mutateDefaultTerm('manual', term);
}

/* ── Boot: resolve automatic term + conversation selector context ── */
async function loadTermState() {
  try {
    const res = await fetch(`${API}/api/term-state`);
    if (!res.ok) throw new Error(`status ${res.status}`);
    const data = await res.json();
    automaticTermContext = data;
    if (!currentSessionId) applyTermPayload(data);
  } catch (err) {
    console.warn('Failed to load /api/term-state', err);
    const select = document.getElementById('termSelect');
    if (select) select.innerHTML = '<option>Term unavailable</option>';
  }
}

async function loadDefaultPromptFromAPI() {
  if (defaultSystemPrompt !== null) return defaultSystemPrompt;
  if (!defaultSystemPromptPromise) {
    defaultSystemPromptPromise = (async () => {
      try {
        const res = await fetch(`${API}/api/system_prompt`);
        if (!res.ok) throw new Error(`status ${res.status}`);
        const data = await res.json();
        defaultSystemPrompt = data.prompt || '';
      } catch (err) {
        console.warn('Failed to load /api/system_prompt — custom prompts can still be set manually', err);
        defaultSystemPrompt = '';
      }
      return defaultSystemPrompt;
    })();
  }
  try {
    return await defaultSystemPromptPromise;
  } finally {
    defaultSystemPromptPromise = null;
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
