const API = '';

/* ──────────────────────────────────────────────────────
   Phase 3 R3 — session management
   ─────────────────────────────────────────────────────
   User-owned endpoints resolve identity from the session cookie. The session_id is
   no longer hardcoded — it's:
     null            → "next message starts a new session"
                       (the empty-state UI is shown)
     "sess_XXXXXX"   → an active persistent session loaded from disk
   When sendMessage() sends an empty session_id, the backend
   auto-creates a new session and returns its real ID via the meta
   event; we then update currentSessionId and refresh the sidebar list.
*/
// Keep only the authenticated UUID in client state. URLs use /me so email
// addresses and account identifiers do not enter access logs.
let USER_ID = 'me';
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

let termRefreshTimer = null;

function setTermContext(term, source, status) {
  if (!term) return;
  currentTermContext = {term, source: source || null, status: status || null, mode: 'auto'};
  const label = document.getElementById('defaultTermLabel');
  if (label) label.textContent = term;
  updateWelcomeTerm();
}

function applyTermPayload(payload) {
  if (!payload) return;
  // Query terms belong to individual answers and never change this label.
  setTermContext(payload.default_term || payload.automatic_term,
    payload.term_source || payload.source, payload.term_status || payload.status);
}

function useAutomaticTermContext() {
  if (automaticTermContext) applyTermPayload(automaticTermContext);
}

async function loadTermState() {
  try {
    const res = await fetch(`${API}/api/term-state`);
    if (!res.ok) throw new Error(`status ${res.status}`);
    const data = await res.json();
    automaticTermContext = data;
    applyTermPayload(data);
    clearTimeout(termRefreshTimer);
    const boundary = Date.parse(data.next_cutoff || '');
    const delay = Number.isFinite(boundary) && boundary > Date.now()
      ? Math.min(boundary - Date.now() + 100, 86400000) : 86400000;
    termRefreshTimer = setTimeout(loadTermState, delay);
  } catch (err) {
    console.warn('Failed to load /api/term-state', err);
    const label = document.getElementById('defaultTermLabel');
    if (label && !currentTermContext) label.textContent = 'Term unavailable';
    clearTimeout(termRefreshTimer);
    termRefreshTimer = setTimeout(loadTermState, 60000);
  }
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') loadTermState();
});

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
