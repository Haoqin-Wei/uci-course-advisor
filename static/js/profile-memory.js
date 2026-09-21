/* ── Settings modal ────────────────────────────────── */
/* ── User popover (bottom-left avatar menu) ───────────── */
function toggleUserPopover(ev) {
  ev.stopPropagation();
  const pop = document.getElementById('userPopover');
  const trigger = document.getElementById('userBar');
  if (!pop) return;
  const wasOpen = pop.classList.contains('open');
  closeUserPopover();   // close before reopening (idempotent)
  if (!wasOpen) {
    pop.classList.add('open');
    trigger?.setAttribute('aria-expanded', 'true');
    document.addEventListener('click', _userPopoverOutsideClick, { once: true });
  }
}
function closeUserPopover() {
  const pop = document.getElementById('userPopover');
  if (pop) pop.classList.remove('open');
  document.getElementById('userBar')?.setAttribute('aria-expanded', 'false');
}
function _userPopoverOutsideClick() {
  closeUserPopover();
}

document.getElementById('userBar')?.addEventListener('keydown', e => {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    toggleUserPopover(e);
  }
});

/* ── Memory modal ─────────────────────────────────────── */
function openMemory() {
  closeUserPopover();
  document.getElementById('memoryModal').classList.add('open');
  loadMemory();
}
function closeMemory(ev) {
  if (ev && ev.type === 'click' && ev.target.id !== 'memoryModal') return;
  document.getElementById('memoryModal').classList.remove('open');
}

async function loadMemory() {
  const body = document.getElementById('memoryBody');
  body.innerHTML = '<div class="memory-empty">Loading…</div>';
  try {
    const r = await fetch(`${API}/api/memory/me`);
    if (!r.ok) {
      body.innerHTML = `<div class="memory-empty">Failed to load memory (${r.status}). ` +
        `Make sure the memory router is registered.</div>`;
      return;
    }
    renderMemory(await r.json());
  } catch (err) {
    body.innerHTML = `<div class="memory-empty">Error: ${escHTML(String(err))}</div>`;
  }
}



function renderMemory(data) {
  const profile = data.profile || {};
  const prefs = data.preferences || [];
  const memories = Array.isArray(data.memories) ? data.memories : [];
  const stats = data.memory_stats || {};
  const prog = data.major_progress;

  let html = '';

  // ── About you ────────────────────────────────────
  html += `<div class="memory-section">
    <div class="memory-section-label">About you</div>
    <div class="memory-about">`;
  if (profile.major)
    html += `<div class="memory-about-line"><strong>${escHTML(profile.major)}</strong>` +
            (profile.year ? ` · ${escHTML(profile.year)}` : '') + `</div>`;
  const nComplete = (profile.completed_courses || []).length;
  const nEnrolled = (profile.selected_courses || []).length;
  html += `<div class="memory-about-line">` +
          `<strong>${nComplete}</strong> completed · ` +
          `<strong>${nEnrolled}</strong> enrolled</div>`;
  if (profile.completed_courses && profile.completed_courses.length)
    html += `<div class="memory-about-line" style="font-size:11px;color:var(--text-tertiary);">` +
            profile.completed_courses.join(', ') + `</div>`;
  html += `</div></div>`;

  // ── Major progress ───────────────────────────────
  if (prog && prog.overall_total) {
    const pct = Math.round((prog.overall_pct || 0) * 100);
    html += `<div class="memory-section">
      <div class="memory-section-label">Major progress</div>
      <div class="memory-progress-title">${escHTML(prog.name || '')} <span style="color:var(--text-tertiary);font-weight:400;">${escHTML(prog.degree || '')}</span></div>
      <div class="memory-progress-bar"><div class="memory-progress-fill" style="width:${pct}%;"></div></div>
      <div class="memory-progress-rows">`;
    const rows = [
      ['Lower-div required',  prog.lower_required],
      ['Lower-div choices',   prog.lower_choice],
      ['Upper-div required',  prog.upper_required],
      ['Upper-div electives', prog.upper_electives],
    ];
    for (const [label, val] of rows) {
      if (!val) continue;
      const done = val.done === val.total ? 'done' : 'count';
      html += `<span class="label">${label}</span>` +
              `<span class="${done}">${val.done}/${val.total}</span>`;
    }
    html += `</div>
      <div class="memory-about-line" style="font-size:11px;color:var(--text-tertiary);margin-top:8px;">` +
      `${prog.overall_done} of ${prog.overall_total} milestone courses (${pct}%)</div>` +
      `</div>`;
  }

  // ── Evidence-backed facts ──────────────────────────
  const factMemories = memories.filter(item => item && item.kind === 'fact');
  if (factMemories.length) {
    html += `<div class="memory-section">
      <div class="memory-section-label">Remembered facts</div>`;
    for (const item of factMemories.slice(0, 12)) {
      const confidence = Number.isFinite(Number(item.confidence))
        ? `${Math.round(Number(item.confidence) * 100)}% confidence`
        : '';
      const source = memorySourceLabel(item);
      html += `<div class="memory-pref-row">
        <div class="memory-pref-text">${escHTML(item.text || '')}` +
        ((confidence || source)
          ? `<span class="memory-pref-when">${escHTML([confidence, source].filter(Boolean).join(' · '))}</span>`
          : '') +
        (item.source_quote
          ? `<span class="memory-pref-when">Source: “${escHTML(item.source_quote)}”</span>`
          : '') +
        `</div>
        <button class="memory-pref-forget" title="Forget this memory"
                onclick="forgetMemoryItem('${escHTML(item.id || '')}')">&#x2715;</button>
      </div>`;
    }
    html += `</div>`;
  }

  // ── Learned preferences ──────────────────────────
  html += `<div class="memory-section">
    <div class="memory-section-label">Learned preferences</div>`;
  if (!prefs.length) {
    html += `<div class="memory-empty">Solon hasn't learned any preferences yet. ` +
            `Chat naturally and it'll pick up on what matters to you.</div>`;
  } else {
    for (const p of prefs) {
      const when = p.learned_at ? formatLearnedAt(p.learned_at) : '';
      const confidence = Number.isFinite(Number(p.confidence))
        ? `${Math.round(Number(p.confidence) * 100)}% confidence`
        : '';
      const source = memorySourceLabel(p);
      html += `<div class="memory-pref-row">
        <div class="memory-pref-text">${escHTML(p.text || '')}` +
        ((when || confidence || source)
          ? `<span class="memory-pref-when">${escHTML([when, confidence, source].filter(Boolean).join(' · '))}</span>`
          : '') +
        (p.source_quote
          ? `<span class="memory-pref-when">Source: “${escHTML(p.source_quote)}”</span>`
          : '') +
        `</div>
        <button class="memory-pref-forget" title="Forget this"
                onclick="forgetPreference('${escHTML(p.id)}')">&#x2715;</button>
      </div>`;
    }
    html += `<button class="memory-forget-all" onclick="forgetAllPreferences()">Forget all preferences</button>`;
  }
  html += `</div>`;

  if (stats.provider) {
    html += `<div class="memory-section">
      <div class="memory-section-label">Memory engine</div>
      <div class="memory-about-line">${escHTML(stats.provider)} · ` +
      `${Number(stats.active || 0)} active · ${Number(stats.superseded || 0)} superseded · ` +
      `${Number(stats.forgotten || 0)} forgotten</div>
    </div>`;
  }

  document.getElementById('memoryBody').innerHTML = html;
}

function memorySourceLabel(item) {
  if (!item) return '';
  if (item.source_session_id) {
    const turn = item.source_turn_index != null ? `, turn ${item.source_turn_index}` : '';
    return `from conversation${turn}`;
  }
  if (item.source_type === 'legacy_import') return 'imported';
  if (item.source_type === 'llm_inferred') return 'inferred from conversation';
  if (item.source_type === 'user_explicit') return 'stated by you';
  return '';
}

function formatLearnedAt(iso) {
  try {
    const d = new Date(iso);
    const now = new Date();
    const diffMs = now - d;
    const day = 86_400_000;
    if (diffMs < day)         return 'learned today';
    if (diffMs < 2 * day)     return 'learned yesterday';
    if (diffMs < 14 * day)    return `learned ${Math.floor(diffMs / day)} days ago`;
    return `learned ${d.toLocaleDateString()}`;
  } catch { return ''; }
}

async function forgetPreference(prefId) {
  try {
    const r = await fetch(`${API}/api/memory/me/preferences/${prefId}`,
                         { method: 'DELETE' });
    if (!r.ok) {
      alert(`Failed to forget: ${r.status}`);
      return;
    }
    loadMemory();   // refresh
  } catch (err) {
    alert(`Network error: ${err}`);
  }
}

async function forgetAllPreferences() {
  if (!confirm('Forget all learned preferences? This cannot be undone.')) return;
  try {
    const r = await fetch(`${API}/api/memory/me/preferences/forget_all`,
                         { method: 'POST' });
    if (!r.ok) {
      alert(`Failed: ${r.status}`);
      return;
    }
    loadMemory();
  } catch (err) {
    alert(`Network error: ${err}`);
  }
}

async function forgetMemoryItem(memoryId) {
  if (!memoryId) return;
  try {
    const r = await fetch(`${API}/api/memory/me/items/${encodeURIComponent(memoryId)}`,
                         { method: 'DELETE' });
    if (!r.ok) {
      alert(`Failed to forget: ${r.status}`);
      return;
    }
    loadMemory();
  } catch (err) {
    alert(`Network error: ${err}`);
  }
}

async function openSettings() {
  const modal = document.getElementById('settingsModal');
  const ta = document.getElementById('promptTextarea');
  const stored = getActivePrompt();
  ta.value = stored;
  ta.placeholder = 'Loading backend default…';
  updatePromptMeta();
  modal.classList.add('open');
  setTimeout(() => ta.focus(), 50);
  await loadDefaultPromptFromAPI();
  ta.placeholder = defaultSystemPrompt
    ? '(empty — using backend default)'
    : 'Backend default unavailable. Enter custom prompt to override.';
}

function closeSettings(ev) {
  // Allow click-on-backdrop to close, but not stray clicks bubbling up.
  if (ev && ev.type === 'click' && ev.target.id !== 'settingsModal') return;
  document.getElementById('settingsModal').classList.remove('open');
}

async function loadDefaultPrompt() {
  const ta = document.getElementById('promptTextarea');
  await loadDefaultPromptFromAPI();
  if (!defaultSystemPrompt) {
    ta.value = '';
    document.getElementById('promptMeta').textContent =
      'Default unavailable — leave empty to use whatever the backend chooses.';
    return;
  }
  ta.value = defaultSystemPrompt;
  updatePromptMeta();
}

function clearPrompt() {
  document.getElementById('promptTextarea').value = '';
  updatePromptMeta();
}

function savePrompt() {
  const v = document.getElementById('promptTextarea').value.trim();
  if (v) {
    localStorage.setItem(PROMPT_STORAGE_KEY, v);
  } else {
    localStorage.removeItem(PROMPT_STORAGE_KEY);
  }
  closeSettings();
}

function updatePromptMeta() {
  const v = document.getElementById('promptTextarea').value;
  const meta = document.getElementById('promptMeta');
  const chars = v.length;
  const lines = v ? v.split('\n').length : 0;
  const status = v ? `${chars} chars · ${lines} lines` : '(empty — will use backend default)';
  meta.textContent = status;
}

document.getElementById('promptTextarea').addEventListener('input', updatePromptMeta);

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    const m = document.getElementById('settingsModal');
    if (m.classList.contains('open')) closeSettings();
  }
});
