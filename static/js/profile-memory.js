/* ── Settings modal ────────────────────────────────── */
/* ── User popover (bottom-left avatar menu) ───────────── */
function toggleUserPopover(ev) {
  ev.stopPropagation();
  const pop = document.getElementById('userPopover');
  if (!pop) return;
  const wasOpen = pop.classList.contains('open');
  closeUserPopover();   // close before reopening (idempotent)
  if (!wasOpen) {
    pop.classList.add('open');
    document.addEventListener('click', _userPopoverOutsideClick, { once: true });
  }
}
function closeUserPopover() {
  const pop = document.getElementById('userPopover');
  if (pop) pop.classList.remove('open');
}
function _userPopoverOutsideClick() {
  closeUserPopover();
}

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
    const r = await fetch(`${API}/api/memory/${USER_ID}`);
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



/* ── Profile modal ────────────────────────────────────── */
function openProfile() {
  closeUserPopover();
  document.getElementById('profileModal').classList.add('open');
  loadProfile();
}
function closeProfile(ev) {
  if (ev && ev.type === 'click' && ev.target.id !== 'profileModal') return;
  document.getElementById('profileModal').classList.remove('open');
}

async function loadProfile() {
  const body = document.getElementById('profileBody');
  body.innerHTML = '<div class="memory-empty">Loading…</div>';
  try {
    const uid = USER_ID;
    const r = await fetch(`${API}/api/memory/${uid}`);
    if (!r.ok) {
      body.innerHTML = `<div class="memory-empty">Failed to load profile (${r.status}).</div>`;
      return;
    }
    renderProfile(await r.json());
  } catch (err) {
    body.innerHTML = `<div class="memory-empty">Error: ${escHTML(String(err))}</div>`;
  }
}

function renderProfile(data) {
  const p = data.profile || {};
  let html = '';

  // ── Wizard re-open entry point ──
  // Always show; lets users revisit and edit major / year / completed
  // courses regardless of whether they originally saved or skipped.
  // Pre-populates wizard state from the current profile so existing
  // selections aren't lost.
  html += `<div class="profile-wizard-cta">
    <div class="profile-wizard-cta-text">
      <div class="profile-wizard-cta-title">Edit with the onboarding wizard</div>
      <div class="profile-wizard-cta-sub">
        Re-pick your school, year, major, or completed courses. Existing values pre-fill.
      </div>
    </div>
    <button class="wizard-btn primary" type="button" onclick="reopenWizardFromProfile()">Open wizard</button>
  </div>`;

  // ── About you ──
  html += `<div class="memory-section">
    <div class="memory-section-label">About you</div>
    <div class="memory-about">`;
  if (p.major)
    html += `<div class="memory-about-line"><strong>${escHTML(p.major)}</strong>` +
            (p.year ? ` · ${escHTML(p.year)}` : '') + `</div>`;
  if (p.target_gpa)
    html += `<div class="memory-about-line">Target GPA: <strong>${p.target_gpa}</strong></div>`;
  const grad = p.graduating_class || _inferGradClass(p.year);
  if (grad)
    html += `<div class="memory-about-line">Expected graduation: <strong>${grad}</strong></div>`;
  html += `</div></div>`;

  // ── Course lists (Completed / Enrolled / Waitlisted) ──
  const sections = [
    ['Completed',  p.completed_courses  || [], 'dot-complete'],
    ['Enrolled',   p.selected_courses   || [], 'dot-enrolled'],
    ['Waitlisted', p.waitlisted_courses || [], 'dot-waitlist'],
  ];
  for (const [label, courses, dotClass] of sections) {
    html += `<div class="memory-section">
      <div class="memory-section-label">${label}<span class="profile-section-count">${courses.length}</span></div>`;
    if (!courses.length) {
      html += `<div class="memory-empty">No ${label.toLowerCase()} courses.</div>`;
    } else {
      html += `<div class="profile-course-grid">`;
      for (const cid of courses) {
        html += `<div class="left-course-tag"><span class="${dotClass}"></span> ${escHTML(_prettyCourseId(cid))}</div>`;
      }
      html += `</div>`;
    }
    html += `</div>`;
  }

  document.getElementById('profileBody').innerHTML = html;
}

function renderMemory(data) {
  const profile = data.profile || {};
  const prefs = data.preferences || [];
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

  // ── Learned preferences ──────────────────────────
  html += `<div class="memory-section">
    <div class="memory-section-label">Learned preferences</div>`;
  if (!prefs.length) {
    html += `<div class="memory-empty">ZotAdvisor hasn't learned any preferences yet. ` +
            `Chat naturally and it'll pick up on what matters to you.</div>`;
  } else {
    for (const p of prefs) {
      const when = p.learned_at ? formatLearnedAt(p.learned_at) : '';
      html += `<div class="memory-pref-row">
        <div class="memory-pref-text">${escHTML(p.text || '')}` +
        (when ? `<span class="memory-pref-when">${when}</span>` : '') +
        `</div>
        <button class="memory-pref-forget" title="Forget this"
                onclick="forgetPreference('${escHTML(p.id)}')">&#x2715;</button>
      </div>`;
    }
    html += `<button class="memory-forget-all" onclick="forgetAllPreferences()">Forget all preferences</button>`;
  }
  html += `</div>`;

  document.getElementById('memoryBody').innerHTML = html;
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
  const uid = USER_ID;
  try {
    const r = await fetch(`${API}/api/memory/${uid}/preferences/${prefId}`,
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
  const uid = USER_ID;
  try {
    const r = await fetch(`${API}/api/memory/${uid}/preferences/forget_all`,
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

function openSettings() {
  const modal = document.getElementById('settingsModal');
  const ta = document.getElementById('promptTextarea');
  const stored = getActivePrompt();
  ta.value = stored;
  ta.placeholder = defaultSystemPrompt
    ? '(empty — using backend default)'
    : 'Backend default unavailable. Enter custom prompt to override.';
  updatePromptMeta();
  modal.classList.add('open');
  setTimeout(() => ta.focus(), 50);
}

function closeSettings(ev) {
  // Allow click-on-backdrop to close, but not stray clicks bubbling up.
  if (ev && ev.type === 'click' && ev.target.id !== 'settingsModal') return;
  document.getElementById('settingsModal').classList.remove('open');
}

function loadDefaultPrompt() {
  const ta = document.getElementById('promptTextarea');
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
