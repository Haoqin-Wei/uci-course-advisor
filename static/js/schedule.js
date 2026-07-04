function _scheduleValidationSummary(validation) {
  const parts = [];
  for (const issue of (validation?.conflicts || [])) {
    if (issue?.message) parts.push('• ' + issue.message);
  }
  for (const issue of (validation?.unknowns || [])) {
    if (issue?.message) parts.push('• ' + issue.message);
  }
  if (parts.length === 0) {
    return 'This section may create a schedule issue.';
  }
  return parts.slice(0, 4).join('\n');
}

async function _postAddCourse(courseId, section, confirmConflicts=false) {
  const res = await fetch(`${API}/api/schedule/add`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      session_id: currentSessionId || '',
      course_id:  courseId,
      section:    section,
      term:       document.getElementById('termSelect').value || null,
      confirm_conflicts: !!confirmConflicts,
    }),
  });
  return await res.json();
}

async function addCourse(courseId, section, btnEl) {
  let data = await _postAddCourse(courseId, section, false);
  if (!data.ok && data.requires_confirmation) {
    const summary = _scheduleValidationSummary(data.schedule_validation);
    const ok = confirm(
      'This section has schedule conflicts or unknown timing.\n\n'
      + summary
      + '\n\nAdd it anyway?'
    );
    if (!ok) throw new Error('add cancelled');
    data = await _postAddCourse(courseId, section, true);
  }
  if (!data.ok) throw new Error(data.reason || 'add failed');
  scheduleEvents = data.events || [];
  _hydrateScheduleState();        // rebuild local sets from server-of-truth
  renderScheduleGrid();
  if (!scheduleOpen) openSchedule();
}

async function removeCourse(courseId, btnEl, section) {
  const res = await fetch(`${API}/api/schedule/remove`, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      session_id: currentSessionId || '',
      course_id:  courseId,
      section:    section || null,   // section-aware remove (Phase E4 picker)
      term:       document.getElementById('termSelect').value || null,
    }),
  });
  const data = await res.json();
  if (!data.ok) throw new Error(data.reason || 'remove failed');
  scheduleEvents = data.events || [];
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
  // Source of truth: distinct course_ids in the backend-resolved
  // scheduleEvents. Reading from `addedCourses` was brittle — it
  // could drift out of sync with the rail and show "0 courses"
  // while events were still rendered.
  const n = new Set(scheduleEvents.map(e => e.course_id)).size;
  document.getElementById('scheduleCount').textContent =
    n + ' course' + (n === 1 ? '' : 's');
}

function openSchedule() {
  scheduleOpen = true;
  document.getElementById('schedulePanel').classList.add('open');
  document.body.classList.add('schedule-open');     // chat shrinks to make room
}

/* Wipe every entry from the session's pending_schedule. Used as an
   escape hatch when a session got into a bad state (e.g. legacy
   entries stored with 5-digit registrar codes where the new picker
   expects section_num). Confirms first so a stray click doesn't
   destroy work. */
async function clearSchedule() {
  if (scheduleEvents.length === 0) return;
  if (!confirm('Clear ALL sections from this schedule?')) return;
  try {
    const res = await fetch(`${API}/api/schedule/clear`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        session_id: currentSessionId || '',
        term: document.getElementById('termSelect').value || null,
      }),
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.reason || 'clear failed');
    scheduleEvents = data.events || [];
    _hydrateScheduleState();
    renderScheduleGrid();
  } catch (err) {
    console.error('clear schedule failed:', err);
  }
}
function toggleSchedule() {
  scheduleOpen = !scheduleOpen;
  document.getElementById('schedulePanel').classList.toggle('open', scheduleOpen);
  document.body.classList.toggle('schedule-open', scheduleOpen);
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

  if (scheduleEvents.length === 0) {
    body.innerHTML = `<div class="schedule-empty">
      <span class="material-symbols-outlined schedule-empty-icon">calendar_month</span>
      <div class="schedule-empty-title">No sections yet</div>
      <div class="schedule-empty-sub">Click <b>+</b> on a Lec card to pin it here.</div>
    </div>`;
    return;
  }

  let html = '<div class="schedule-grid">';
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
  for (const ev of scheduleEvents) {
    const meta = layout.get(ev) || {col: 0, cols: 1};
    placeEvent(ev, meta.col, meta.cols);
  }
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

function placeEvent(ev, col, cols) {
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
  evEl.className = 'sg-event';
  evEl.dataset.cid = ev.course_id;
  evEl.dataset.sec = ev.section_num || '';
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
    `.sg-event[data-cid="${CSS.escape(cid)}"][data-sec="${CSS.escape(sec || '')}"]`
  );
  if (tileEl) tileEl.style.opacity = '0.4';
  removeCourse(cid, null, sec || null).catch(err => {
    console.error('remove failed:', err);
    if (tileEl) tileEl.style.opacity = '';   // unfade on failure
  });
}

function parseTimeToMinutes(t) {
  const parts = t.split(':');
  if (parts.length !== 2) return -1;
  return parseInt(parts[0]) * 60 + parseInt(parts[1]);
}
