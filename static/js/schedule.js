async function _postAddCourse(courseId, section, term) {
  const res = await fetch(`${API}/api/schedule/add`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      session_id: currentSessionId || '',
      course_id:  courseId,
      section:    section,
      term:       term || null,
    }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || data.reason || 'add failed');
  return data;
}

async function addCourse(courseId, section, btnEl, term) {
  const data = await _postAddCourse(courseId, section, term);
  if (!data.ok) throw new Error(data.reason || 'add failed');
  pendingScheduleEntries = data.pending_schedule || [];
  scheduleEvents = data.events || [];
  scheduleValidation = data.schedule_validation || scheduleValidation;
  _hydrateScheduleState();        // rebuild local sets from server-of-truth
  renderScheduleGrid();
  showCrossTermToast(data.cross_term_notice);
  if (!scheduleOpen) openSchedule();
}

async function removeCourse(courseId, btnEl, section, term) {
  const res = await fetch(`${API}/api/schedule/remove`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      session_id: currentSessionId || '',
      course_id:  courseId,
      section:    section || null,   // section-aware remove (Phase E4 picker)
      term:       term || null,
    }),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) throw new Error(data.detail || data.reason || 'remove failed');
  pendingScheduleEntries = data.pending_schedule || [];
  scheduleEvents = data.events || [];
  scheduleValidation = data.schedule_validation || scheduleValidation;
  _hydrateScheduleState();        // rebuild local sets from server-of-truth
  renderScheduleGrid();
}

function updateCardState(courseId, added, section) {
  const card = document.getElementById('card-' + courseId);
  if (!card) return;
  card.classList.toggle('added', !!added);
  const btn = card.querySelector('.cc-add-btn');
  if (btn) {
    const icon = btn.querySelector('.material-symbols-outlined');
    if (icon) icon.textContent = added ? 'check' : 'add';
    btn.setAttribute('aria-label', added ? 'Remove from schedule' : 'Add to schedule');
    if (section) btn.setAttribute('data-section', section);
  }
}

function updateScheduleCount() {
  const n = new Set(pendingScheduleEntries.map(
    entry => scheduleCourseKey(entry.term, entry.course_id)
  )).size;
  document.getElementById('scheduleCount').textContent =
    n + ' course' + (n === 1 ? '' : 's');
}

function openSchedule() {
  scheduleOpen = true;
  document.getElementById('schedulePanel').classList.add('open');
  document.body.classList.add('schedule-open');     // chat shrinks to make room
  document.getElementById('toggleScheduleBtn')?.setAttribute('aria-expanded', 'true');
}

/* Wipe every entry from the session's pending_schedule. Used as an
   escape hatch when a session got into a bad state (e.g. legacy
   entries stored with 5-digit registrar codes where the new picker
   expects section_num). Confirms first so a stray click doesn't
   destroy work. */
async function clearSchedule() {
  if (pendingScheduleEntries.length === 0) return;
  if (!confirm('Clear ALL sections from this schedule?')) return;
  try {
    const res = await fetch(`${API}/api/schedule/clear`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({session_id: currentSessionId || ''}),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.reason || 'clear failed');
    pendingScheduleEntries = data.pending_schedule || [];
    scheduleEvents = data.events || [];
    scheduleValidation = data.schedule_validation || {
      valid: true, warnings: [], conflicts: [], unknowns: [],
    };
    _hydrateScheduleState();
    renderScheduleGrid();
  } catch (err) {
    console.error('clear schedule failed:', err);
  }
}

async function loadScheduleForSession(sessionId) {
  if (!sessionId) {
    pendingScheduleEntries = [];
    scheduleEvents = [];
    scheduleValidation = {valid: true, warnings: [], conflicts: [], unknowns: []};
    _hydrateScheduleState();
    renderScheduleGrid();
    return;
  }
  try {
    const res = await fetch(
      `${API}/api/schedule?session_id=${encodeURIComponent(sessionId)}`
    );
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.detail || 'schedule load failed');
    pendingScheduleEntries = data.pending_schedule || [];
    scheduleEvents = data.events || [];
    scheduleValidation = data.schedule_validation || {
      valid: true, warnings: [], conflicts: [], unknowns: [],
    };
    _hydrateScheduleState();
    renderScheduleGrid();
  } catch (err) {
    console.warn('schedule load failed:', err);
  }
}

async function refreshSchedule() {
  if (!currentSessionId || pendingScheduleEntries.length === 0) return;
  const button = document.getElementById('scheduleRefreshBtn');
  if (button) button.disabled = true;
  try {
    const res = await fetch(`${API}/api/schedule/refresh`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({session_id: currentSessionId}),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.detail || 'schedule refresh failed');
    }
    pendingScheduleEntries = data.pending_schedule || [];
    scheduleEvents = data.events || [];
    scheduleValidation = data.schedule_validation || scheduleValidation;
    _hydrateScheduleState();
    renderScheduleGrid();
  } catch (err) {
    console.warn('schedule refresh failed:', err);
  } finally {
    if (button) button.disabled = false;
  }
}

let scheduleToastTimer = null;

function dismissScheduleToast() {
  const region = document.getElementById('scheduleToastRegion');
  if (region) region.replaceChildren();
  if (scheduleToastTimer) clearTimeout(scheduleToastTimer);
  scheduleToastTimer = null;
}

function showCrossTermToast(notice) {
  if (!notice) return;
  const region = document.getElementById('scheduleToastRegion');
  if (!region) return;
  dismissScheduleToast();
  const toast = document.createElement('div');
  toast.className = 'schedule-toast';
  const prior = (notice.existing_terms || []).join(', ');
  toast.innerHTML = `<div><strong>${escHTML(notice.added_term)}</strong> section added.</div>
    <div class="schedule-toast-sub">Your schedule also includes ${escHTML(prior)}.</div>
    <button type="button" aria-label="Dismiss notification" onclick="dismissScheduleToast()">
      <span class="material-symbols-outlined">close</span>
    </button>`;
  region.appendChild(toast);
  scheduleToastTimer = setTimeout(dismissScheduleToast, 5000);
}
function toggleSchedule() {
  scheduleOpen = !scheduleOpen;
  document.getElementById('schedulePanel').classList.toggle('open', scheduleOpen);
  document.body.classList.toggle('schedule-open', scheduleOpen);
  document.getElementById('toggleScheduleBtn')?.setAttribute('aria-expanded', String(scheduleOpen));
}

(function initResize() {
  const panel = document.getElementById('schedulePanel');
  const handle = document.getElementById('resizeHandle');
  const SCHED_MIN = 420;
  const SCHED_MAX = 760;
  let dragging = false;

  handle.addEventListener('mousedown', e => {
    if (!scheduleOpen) return;
    dragging = true;
    panel.classList.add('resizing');
    document.body.style.cursor = 'ew-resize';
    e.preventDefault();
  });

  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const newWidth = Math.max(SCHED_MIN, Math.min(SCHED_MAX, window.innerWidth - e.clientX));
    panel.style.width = newWidth + 'px';
  });

  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    panel.classList.remove('resizing');
    document.body.style.cursor = '';
    if (scheduleEvents.length > 0) renderScheduleGrid();
  });
})();

const DAYS = ['Mon','Tue','Wed','Thu','Fri'];
// Day grid hours: 7 AM through end of 9 PM (covers 7-22:00).
// Render rows 7..21 inclusive (so 15 rows × ROW_HEIGHT = 780px of
// content + HEADER_HEIGHT). schedule-body scrolls vertically when
// the viewport is shorter — typical case for laptop screens.
const START_HOUR = 7;
const END_HOUR = 22;
const ROW_HEIGHT = 52;
// Must match the actual `.sg-day-header` height (padding 12+12 +
// text 16 + 1px border = 41). Keeping it at 41 makes JS topOffset
// math align pixel-for-pixel with the rendered grid.
const HEADER_HEIGHT = 41;

function renderScheduleGrid() {
  const body = document.getElementById('scheduleBody');
  const entryList = _renderScheduleEntryList();

  if (pendingScheduleEntries.length === 0) {
    body.innerHTML = `<div class="schedule-empty">
      <span class="material-symbols-outlined schedule-empty-icon">calendar_month</span>
      <div class="schedule-empty-title">No sections yet</div>
      <div class="schedule-empty-sub">Click <b>+</b> on a Lec card to pin it here.</div>
    </div>`;
    return;
  }

  if (scheduleEvents.length === 0) {
    body.innerHTML = entryList + `<div class="schedule-empty schedule-empty-compact">
      <span class="material-symbols-outlined schedule-empty-icon">event_busy</span>
      <div class="schedule-empty-title">No timed meetings</div>
      <div class="schedule-empty-sub">TBA and unknown-term entries stay listed above.</div>
    </div>`;
    return;
  }

  let html = entryList + '<div class="schedule-grid">';
  html += '<div class="sg-corner"></div>';
  for (const d of DAYS) html += `<div class="sg-day-header">${d}</div>`;

  for (let h = START_HOUR; h < END_HOUR; h++) {
    let label;
    if (h === 12) label = '12 PM';
    else if (h < 12) label = `${h} AM`;
    else label = `${h-12} PM`;
    // Alternating hour bands (odd hours get a faint stripe) — borrows
    // from AntAlmanac's .rbc-timeslot-group:nth-child(odd) trick to
    // give vertical rhythm without harsh horizontal lines.
    const band = (h % 2 === 1) ? ' sg-band' : '';
    html += `<div class="sg-time-label${band}">${label}</div>`;
    for (let d = 0; d < 5; d++) {
      html += `<div class="sg-cell${band}" data-day="${DAYS[d]}" data-hour="${h}"></div>`;
    }
  }
  html += '</div>';
  body.innerHTML = html;

  // Compute side-by-side layout for overlapping events on the same
  // day so two events at the same time don't stack on top of each
  // other (the previous renderer placed both at left:0).
  const layout = _computeOverlapLayout(scheduleEvents);
  const conflictKeys = _scheduleConflictKeys();
  for (const ev of scheduleEvents) {
    const meta = layout.get(ev) || {col: 0, cols: 1};
    placeEvent(
      ev,
      meta.col,
      meta.cols,
      _scheduleItemHasConflict(conflictKeys, ev),
    );
  }
}

function _scheduleConflictKeys() {
  const keys = new Set();
  for (const issue of scheduleValidation?.conflicts || []) {
    if (!['time_conflict', 'final_exam_conflict'].includes(issue?.type)) continue;
    for (const section of issue.sections || []) {
      const cid = section.course_id || '';
      for (const value of [section.section_num, section.section_code]) {
        if (cid && value) keys.add(`${cid}|${value}`);
      }
    }
  }
  return keys;
}

function _scheduleItemHasConflict(keys, item) {
  const cid = item?.course_id || '';
  return [item?.section, item?.section_num, item?.section_code]
    .filter(Boolean)
    .some(section => keys.has(`${cid}|${section}`));
}

function _renderScheduleEntryList() {
  if (pendingScheduleEntries.length === 0) return '';
  const conflictKeys = _scheduleConflictKeys();
  const rows = pendingScheduleEntries.map(entry => {
    const term = entry.term || 'unknown';
    const notice = (entry.notices || []).join(' ');
    const materialization = entry.materialization_status || 'unresolved';
    const hasConflict = _scheduleItemHasConflict(conflictKeys, entry);
    const snapshot = entry.materialized_section || {};
    const liveStatus = snapshot.is_cancelled
      ? 'CANCELLED'
      : String(snapshot.status || '').toUpperCase();
    return `<div class="schedule-entry${hasConflict ? ' has-conflict' : ''}" data-term="${escAttr(term)}"
                 data-cid="${escAttr(entry.course_id || '')}"
                 data-sec="${escAttr(entry.section || '')}"
                 title="${escAttr(notice)}">
      <span class="schedule-entry-course">${escHTML(entry.course_id || 'Course')}</span>
      <span class="schedule-entry-section">${escHTML(entry.section || '—')}</span>
      <span class="schedule-entry-term">${escHTML(term)}</span>
      ${hasConflict
        ? '<span class="schedule-entry-risk schedule-entry-risk-conflict">time conflict</span>'
        : ''}
      ${liveStatus && liveStatus !== 'OPEN'
        ? `<span class="schedule-entry-risk schedule-entry-risk-${escAttr(liveStatus.toLowerCase())}">${escHTML(liveStatus)}</span>`
        : ''}
      ${materialization !== 'resolved'
        ? `<span class="schedule-entry-risk schedule-entry-risk-unresolved">${escHTML(materialization)}</span>`
        : ''}
      <button type="button" aria-label="Remove ${escAttr(entry.course_id || 'course')}"
              onclick="removeScheduledEntry(this)">
        <span class="material-symbols-outlined">close</span>
      </button>
    </div>`;
  }).join('');
  return `<div class="schedule-entry-list" aria-label="Scheduled sections">
    <div class="schedule-planning-note">Planning draft only — confirm eligibility and seats in official UCI systems.</div>
    ${rows}
  </div>`;
}

function removeScheduledEntry(btn) {
  const row = btn.closest('.schedule-entry');
  if (!row) return;
  btn.disabled = true;
  removeCourse(row.dataset.cid, btn, row.dataset.sec, row.dataset.term).catch(err => {
    console.error('remove failed:', err);
    btn.disabled = false;
  });
}

/* Cheap overlap layout: per day, sweep events sorted by start time.
   For each event, find the lowest column index that doesn't overlap
   anything still "active" at its start time. Returns a Map<ev, {col, cols}>
   where `cols` is the max overlap depth in that event's day. */
function _computeOverlapLayout(events) {
  const out = new Map();
  const byDay = {};
  for (const e of events) {
    (byDay[e.day] = byDay[e.day] || []).push(e);
  }
  for (const day of Object.keys(byDay)) {
    const list = byDay[day].slice().sort(
      (a, b) => parseTimeToMinutes(a.start) - parseTimeToMinutes(b.start)
    );
    // Assign columns: for each event, find the first column whose
    // latest end is ≤ this event's start.
    const colEnds = [];     // colEnds[i] = end-minute of latest event in col i
    const assigned = [];     // [{ev, col}]
    for (const ev of list) {
      const start = parseTimeToMinutes(ev.start);
      const end   = parseTimeToMinutes(ev.end);
      let col = colEnds.findIndex(end_ => end_ <= start);
      if (col === -1) col = colEnds.length;
      colEnds[col] = end;
      assigned.push({ev, col});
    }
    const cols = colEnds.length;
    for (const a of assigned) out.set(a.ev, {col: a.col, cols});
  }
  return out;
}

function placeEvent(ev, col, cols, hasConflict = false) {
  const dayIdx = DAYS.indexOf(ev.day);
  if (dayIdx < 0) return;

  const startMin = parseTimeToMinutes(ev.start);
  const endMin = parseTimeToMinutes(ev.end);
  if (startMin < 0 || endMin < 0) return;

  const gridEl = document.querySelector('.schedule-grid');
  if (!gridEl) return;

  const topOffset = ((startMin - START_HOUR * 60) / 60) * ROW_HEIGHT + HEADER_HEIGHT;
  const height = ((endMin - startMin) / 60) * ROW_HEIGHT;

  const [stroke, fill] = getCourseColor(ev.course_id);

  // Column math: each day column is split into `cols` sub-tracks.
  // 4px outer gap on each side of the day column + 2px between tracks.
  const dayCol = `((100% - 56px) / 5)`;
  const trackW = `((${dayCol} - 8px) / ${cols} - ${cols > 1 ? '2px' : '0px'})`;
  const trackX = `(${col} * (${trackW} + 2px))`;

  const evEl = document.createElement('div');
  evEl.className = `sg-event${hasConflict ? ' has-conflict' : ''}`;
  evEl.dataset.cid = ev.course_id;
  evEl.dataset.sec = ev.section_num || '';
  evEl.dataset.term = ev.term || '';
  evEl.style.position = 'absolute';
  evEl.style.top = topOffset + 'px';
  evEl.style.height = Math.max(height - 4, 24) + 'px';
  evEl.style.left = `calc(56px + ${dayIdx} * ${dayCol} + 4px + ${trackX})`;
  evEl.style.width = `calc(${trackW})`;
  evEl.style.background = fill;
  evEl.style.borderLeftColor = stroke;
  evEl.style.color = stroke;

  const tall = height > 40;
  const sectionTag = ev.section_type ? `<span class="sg-event-type">${ev.section_type}</span>` : '';
  const top2 = `<div class="sg-event-top">
    <span class="sg-event-id">${ev.course_id}${ev.section_num ? ' · ' + ev.section_num : ''}</span>
    ${sectionTag}
  </div>`;
  const bottom2 = tall ? `<div class="sg-event-bot">
    <span class="sg-event-time">${ev.start}-${ev.end}</span>
    ${ev.location ? `<span class="sg-event-room">${ev.location}</span>` : ''}
  </div>${ev.section_code ? `<div class="sg-event-code">${ev.section_code}</div>` : ''}` : '';
  evEl.innerHTML = top2 + bottom2;
  evEl.title = [
    `${ev.course_id}${ev.section_num ? ' · ' + ev.section_num : ''}`,
    ev.term || '',
    ev.title || '',
    `${ev.day} ${ev.start}-${ev.end}`,
    ev.location || '',
    ev.instructor || '',
    ev.section_code ? `Code ${ev.section_code}` : '',
    'Click to remove',
  ].filter(Boolean).join('\n');
  evEl.addEventListener('click', () => _removeFromCalendar(ev));

  gridEl.appendChild(evEl);
}

/* Clicking an event tile removes that specific (course, section_num)
   from the schedule. Section-aware now that the backend supports it
   (Phase E4). Also flips the matching + button on the recommendation
   card back to the un-added state. */
function _removeFromCalendar(ev) {
  const cid = ev.course_id;
  // Fall back to the legacy `section` field for events that pre-date
  // the section_num split. Without this, clicking an old tile silently
  // no-op'd and the user couldn't dismiss the math course they added
  // before today's changes.
  const sec = ev.section_num || ev.section;
  if (!cid) return;
  // Optimistic: hide the tile immediately so the click feels live —
  // _hydrateScheduleState (called from removeCourse) will reconcile
  // any drift.
  const tileEl = document.querySelector(
    `.sg-event[data-cid="${CSS.escape(cid)}"][data-sec="${CSS.escape(sec || '')}"][data-term="${CSS.escape(ev.term || '')}"]`
  );
  if (tileEl) tileEl.style.opacity = '0.4';
  removeCourse(cid, null, sec || null, ev.term || null).catch(err => {
    console.error('remove failed:', err);
    if (tileEl) tileEl.style.opacity = '';   // unfade on failure
  });
}

function parseTimeToMinutes(t) {
  const parts = t.split(':');
  if (parts.length !== 2) return -1;
  return parseInt(parts[0]) * 60 + parseInt(parts[1]);
}
