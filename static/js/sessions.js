/* ── Sidebar (left panel) ───────────────────────────────
   Fetches the same /api/memory/me endpoint and renders the profile +
   completed/enrolled lists. Called on boot and after each chat reply
   in case Channel A extracted new facts that updated the profile.
*/
let sidebarSnapshot = null;
let sidebarSnapshotUserId = null;
let sidebarLoadPromise = null;
let sessionListLoadPromise = null;

async function loadSidebar(forceRefresh = false) {
  const uid = USER_ID;
  if (!forceRefresh && sidebarSnapshot && sidebarSnapshotUserId === uid) {
    renderSidebar(sidebarSnapshot, uid);
    return;
  }
  if (!sidebarLoadPromise) {
    sidebarLoadPromise = (async () => {
      try {
        const r = await fetch(`${API}/api/memory/me`);
        if (!r.ok) {
          // Fallback: leave the placeholder text so the UI isn't blank.
          return;
        }
        sidebarSnapshot = await r.json();
        sidebarSnapshotUserId = uid;
        renderSidebar(sidebarSnapshot, uid);
      } catch (err) {
        console.warn('sidebar load failed:', err);
      }
    })();
  }
  try {
    await sidebarLoadPromise;
  } finally {
    sidebarLoadPromise = null;
  }
}

function setActiveSessionItem(sessionId) {
  document.querySelectorAll('.session-item').forEach(el => {
    el.classList.toggle('is-active', el.dataset.sid === sessionId);
    el.querySelector('.session-item-title')?.setAttribute('aria-current', String(el.dataset.sid === sessionId));
  });
}

async function loadSessionList() {
  const listEl = document.getElementById('sessionsList');
  if (!listEl) return;
  if (!sessionListLoadPromise) {
    sessionListLoadPromise = (async () => {
      try {
        const r = await fetch(`${API}/api/sessions/me?limit=20`);
        if (!r.ok) {
          listEl.innerHTML = '<div class="sessions-empty">No conversations yet</div>';
          return;
        }
        const data = await r.json();
        renderSessionList(data.sessions || []);
      } catch (err) {
        console.warn('session list load failed:', err);
        listEl.innerHTML = '<div class="sessions-empty">No conversations yet</div>';
      }
    })();
  }
  try {
    await sessionListLoadPromise;
  } finally {
    sessionListLoadPromise = null;
  }
}

function renderSidebar(memory, userId) {
  // Phase 3 R3: the identity block (.profile-block + .sb-stats) was
  // removed from the sidebar; identity now lives only in the bottom
  // user-bar popover and inside the Profile modal. We only update
  // the user-bar avatar/name here.
  const profile = memory.profile || {};
  const displayName = profile.display_name
    || userId.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  const initials = displayName
    .split(/\s+/).filter(Boolean).slice(0, 2)
    .map(s => s[0].toUpperCase()).join('') || 'U';

  document.getElementById('ub-avatar').textContent = initials;
  document.getElementById('ub-name').textContent   = userId;

}

/* ──────────────────────────────────────────────────────
   Phase 3 R3 — session list management
   ────────────────────────────────────────────────────── */

function renderSessionList(sessions) {
  const listEl = document.getElementById('sessionsList');
  if (!listEl) return;
  listEl.replaceChildren();
  const current = sessions.find(s => s.session_id === currentSessionId);
  if (current) setThreadTitle(current.title);
  if (!sessions.length) {
    listEl.innerHTML = '<div class="sessions-empty">No conversations yet</div>';
    return;
  }
  const today = new Date().toDateString();
  const groups = {Today: [], Earlier: []};
  for (const session of sessions) {
    const date = new Date(session.last_active_at || session.updated_at || session.created_at || '');
    groups[date.toDateString() === today ? 'Today' : 'Earlier'].push(session);
  }
  for (const [label, items] of Object.entries(groups)) {
    if (!items.length) continue;
    const heading = document.createElement('div');
    heading.className = 'sessions-label';
    heading.textContent = label;
    listEl.appendChild(heading);
    for (const session of items) {
      const row = document.createElement('div');
      row.className = 'session-item' + (session.session_id === currentSessionId ? ' is-active' : '');
      row.dataset.sid = session.session_id;
      const pick = document.createElement('button');
      pick.className = 'session-item-title';
      pick.textContent = session.title || 'New conversation';
      pick.title = pick.textContent;
      pick.setAttribute('aria-current', String(session.session_id === currentSessionId));
      pick.addEventListener('click', () => loadSession(session.session_id));
      const remove = document.createElement('button');
      remove.className = 'session-item-delete';
      remove.innerHTML = solonIcon('close');
      remove.setAttribute('aria-label', `Delete ${pick.textContent}`);
      remove.addEventListener('click', () => deleteSession(session.session_id));
      row.append(pick, remove);
      listEl.appendChild(row);
    }
  }
}

function startNewChat() {
  setWorkspaceView('ask');
  // Stop any in-flight generation; user's about to switch context.
  if (currentAbortController) currentAbortController.abort();

  conversationEpoch++;
  currentAbortController = null;
  toggleSendStop(false);
  currentSessionId = null;
  currentResponseLanguage = 'en';
  resetPlanObjects();
  setThreadTitle();
  setComposerSuggestions();
  toggleMobileSidebar(false);
  scheduleValidation = {valid: true, warnings: [], conflicts: [], unknowns: []};
  useAutomaticTermContext();
  pendingScheduleEntries = [];
  scheduleEvents = [];
  _hydrateScheduleState();
  renderScheduleGrid();
  resetChatToWelcome();
  // Highlight nothing in the sidebar
  document.querySelectorAll('.session-item.is-active')
    .forEach(el => el.classList.remove('is-active'));
  if (scheduleOpen) toggleSchedule();
  inputEl && inputEl.focus();
}

function resetChatToWelcome() {
  const scrollEl = document.getElementById('chatScroll');
  scrollEl.replaceChildren();
  scrollEl.scrollTop = 0;
  setWelcomePresentation(true);
  // Sync the welcome sub-line to the current read-only context.
  updateWelcomeTerm();
}

/* Reflect the backend-resolved term into the welcome-state sub-line. */
function updateWelcomeTerm() {
  const span = document.getElementById('welcomeTerm');
  if (!span) return;
  span.textContent = currentTermContext?.term || 'the current term';
}

async function loadSession(sessionId) {
  if (!sessionId) return;
  setWorkspaceView('ask');
  if (sessionId === currentSessionId) return;

  if (currentAbortController) currentAbortController.abort();
  currentAbortController = null;
  toggleSendStop(false);
  const epoch = ++conversationEpoch;
  toggleMobileSidebar(false);

  try {
    const r = await fetch(`${API}/api/sessions/me/${sessionId}?include_turns=true`);
    if (!r.ok) {
      console.warn('failed to load session', sessionId, r.status);
      return;
    }
    const data = await r.json();
    if (epoch !== conversationEpoch) return;
    resetPlanObjects();
    currentSessionId = sessionId;
    currentResponseLanguage = 'en';
    setComposerSuggestions();
    pendingScheduleEntries = [];
    scheduleEvents = [];
    scheduleValidation = {valid: true, warnings: [], conflicts: [], unknowns: []};
    _hydrateScheduleState();
    setThreadTitle(data.title || data.meta?.title);
    // The server refreshes every session to the same automatic default.
    applyTermPayload(data);

    // Replace the chat scroll with the historical turns. Assistant
    // turns reuse the same card/followup/validation renderers as live
    // streaming finalization so session restore preserves structured UI.
    const scrollEl = document.getElementById('chatScroll');
    setWelcomePresentation(false);
    scrollEl.innerHTML = '';
    const turns = data.turns || [];
    if (!turns.length) {
      // Edge case: session exists but no turns yet — keep welcome.
      resetChatToWelcome();
    } else {
    let pendingQueryMeta = null;
    for (const t of turns) {
      if (t.role === 'user') {
        currentResponseLanguage = detectResponseLanguage(t.content || '', currentResponseLanguage);
        appendUser(t.content || '', {time: t.timestamp, restored: true});
        pendingQueryMeta = {
          query_terms: t.query_terms,
          query_term_source: t.query_term_source,
          default_term: data.default_term,
        };
      } else if (t.role === 'assistant') {
        appendAssistantStatic(t.content || '', {
          response_language: currentResponseLanguage,
            cards:      t.cards,
            followups:  t.followups,
          validation: t.validation,
          web_fetches: t.web_fetches,
          ...pendingQueryMeta,
        });
        pendingQueryMeta = null;
        }
      }
      // Scroll to bottom after rendering history
      requestAnimationFrame(() => {
        scrollEl.scrollTop = scrollEl.scrollHeight;
      });
    }
    await loadScheduleForSession(sessionId);
    if (epoch !== conversationEpoch) return;
    // The session list itself is unchanged; update its highlight locally.
    setActiveSessionItem(sessionId);
  } catch (err) {
    console.error('loadSession failed:', err);
  }
}

/*
 * Append an assistant message in its FINAL static form (no streaming
 * shimmer). Used when re-rendering a historical session. Mirrors the
 * structure built by finalizeAiMessage so an opened session looks
 * identical to the live one — cards, followup chips, and the
 * `extras` is the parsed-back JSONL extras dict ({cards, followups,
 * web_fetches}); each field is optional and falsy values are skipped
 * (legacy turns persisted before Round 4 won't have them).
 */
function appendAssistantStatic(text, extras) {
  extras = extras || {};
  const wrap = document.createElement('div');
  wrap.className = 'msg msg-ai';
  wrap._responseLanguage = extras.response_language || currentResponseLanguage;
  wrap.innerHTML = `
    <div class="msg-ai-label">${solonIcon('sparkles')}Solon</div>
    <div class="msg-ai-body"></div>
  `;
  renderQueryTermBadge(wrap, extras);
  wrap.querySelector('.msg-ai-body').innerHTML = formatMarkdown(text || '');
  document.getElementById('chatScroll').appendChild(wrap);

  if (extras.web_fetches && extras.web_fetches.length > 0) {
    renderWebFetchSummary(wrap, extras.web_fetches);
  }

  if (extras.cards && extras.cards.length > 0) {
    const block = document.createElement('div');
    block.innerHTML = renderCardsBlock(extras.cards);
    if (block.firstElementChild) wrap.appendChild(block.firstElementChild);
  }

  setComposerSuggestions(Array.isArray(extras.followups) ? extras.followups : []);

}

async function deleteSession(sessionId) {
  if (!sessionId) return;
  if (!confirm('Delete this conversation? This cannot be undone.')) return;
  try {
    const r = await fetch(`${API}/api/sessions/me/${sessionId}`, {
      method: 'DELETE',
    });
    if (!r.ok) {
      console.warn('delete failed', sessionId, r.status);
      return;
    }
    // If we just deleted the active session, fall back to empty state
    if (sessionId === currentSessionId) {
      startNewChat();
    }
    loadSessionList();
  } catch (err) {
    console.error('deleteSession failed:', err);
  }
}

function _prettyCourseId(cid) {
  // 'ICS33' → 'ICS 33', 'CS122A' → 'CS 122A', 'MATH2A' → 'MATH 2A'.
  return cid.replace(/^([A-Z]+)(\d.*)$/, '$1 $2');
}

function _inferGradClass(year) {
  // Heuristic: current academic year is 2025-26, ending in June 2026.
  if (!year) return null;
  const y = year.toLowerCase();
  const ACADEMIC_YEAR_END = 2026;
  const offsets = {
    freshman: 4, sophomore: 3, junior: 2, senior: 1,
    "1st year": 4, "2nd year": 3, "3rd year": 2, "4th year": 1,
  };
  return offsets[y] != null ? ACADEMIC_YEAR_END + offsets[y] - 1 : null;
}
