let scheduleViewTerm = '';
let scheduleLoadSequence = 0;
let scheduleSyncState = 'idle';

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
  const epoch = conversationEpoch;
  const data = await _postAddCourse(courseId, section, term);
  if (epoch !== conversationEpoch) return;
  scheduleSyncState = 'ready';
  scheduleViewTerm = canonicalTerm(term);
  if (!data.ok) throw new Error(data.reason || 'add failed');
  ++scheduleLoadSequence; // An older GET must not erase this completed addition.
  pendingScheduleEntries = data.pending_schedule || [];
  scheduleEvents = data.events || [];
  scheduleValidation = data.schedule_validation || scheduleValidation;
  _hydrateScheduleState();        // rebuild local sets from server-of-truth
  renderScheduleGrid();
  showCrossTermToast(data.cross_term_notice);
  // A completed background request must not interrupt the profile workspace.
  if (!scheduleOpen && !document.body.classList.contains('profile-open')) openSchedule();
  if (scheduleOpen && !document.body.classList.contains('profile-open')) {
    selectedScheduleCourse = scheduleCourseKey(scheduleViewTerm, courseId);
    syncScheduleHighlight();
    const blocks = [...document.querySelectorAll('.sg-event, .schedule-untimed-item')].filter(el =>
      scheduleCourseKey(el.dataset.term, el.dataset.cid) === selectedScheduleCourse);
    const addedBlock = blocks.find(el => el.dataset.sec === section) || blocks[0];
    addedBlock?.scrollIntoView({block: 'nearest', inline: 'nearest'});
  }
}

async function removeCourse(courseId, btnEl, section, term) {
  const epoch = conversationEpoch;
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
  if (epoch !== conversationEpoch) return;
  ++scheduleLoadSequence;
  scheduleSyncState = 'ready';
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
    if (icon) icon.innerHTML = solonIcon(added ? 'check' : 'add');
    btn.setAttribute('aria-label', added ? 'Remove from schedule' : 'Add to schedule');
    if (section) btn.setAttribute('data-section', section);
  }
}

function updateScheduleCount() {
  const n = new Set(pendingScheduleEntries.map(
    entry => scheduleCourseKey(entry.term, entry.course_id)
  )).size;
  const count = document.getElementById('scheduleCount');
  if (count) count.textContent = n + ' course' + (n === 1 ? '' : 's');
}

function openSchedule() {
  scheduleOpen = true;
  document.getElementById('schedulePanel').classList.add('open');
  document.body.classList.add('schedule-open');     // chat shrinks to make room
  document.getElementById('toggleScheduleBtn')?.setAttribute('aria-expanded', 'true');
  syncScheduleChrome();
}

/* Wipe every entry from the session's pending_schedule. Used as an
   escape hatch when a session got into a bad state (e.g. legacy
   entries stored with 5-digit registrar codes where the new picker
   expects section_num). Confirms first so a stray click doesn't
   destroy work. */
async function clearSchedule() {
  if (pendingScheduleEntries.length === 0) return;
  if (!confirm('Clear ALL sections from this schedule?')) return;
  const epoch = conversationEpoch;
  try {
    const res = await fetch(`${API}/api/schedule/clear`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({session_id: currentSessionId || ''}),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.reason || 'clear failed');
    if (epoch !== conversationEpoch) return;
    scheduleSyncState = 'ready';
    pendingScheduleEntries = data.pending_schedule || [];
    scheduleEvents = data.events || [];
    scheduleValidation = data.schedule_validation || {
      valid: true, warnings: [], conflicts: [], unknowns: [],
    };
    _hydrateScheduleState();
    renderScheduleGrid();
  } catch (err) {
    console.error('clear schedule failed:', err);
    if (epoch === conversationEpoch) notifySchedule('Could not clear your plan. Please try again.');
  }
}

async function loadScheduleForSession(sessionId) {
  const sequence = ++scheduleLoadSequence;
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
    if (sessionId !== currentSessionId || sequence !== scheduleLoadSequence) return;
    scheduleSyncState = 'ready';
    pendingScheduleEntries = data.pending_schedule || [];
    scheduleEvents = data.events || [];
    scheduleValidation = data.schedule_validation || {
      valid: true, warnings: [], conflicts: [], unknowns: [],
    };
    _hydrateScheduleState();
    renderScheduleGrid();
  } catch (err) {
    console.warn('schedule load failed:', err);
    if (sessionId === currentSessionId && sequence === scheduleLoadSequence) {
      scheduleSyncState = 'error';
      renderScheduleGrid();
    }
  }
}

async function refreshSchedule() {
  if (!currentSessionId || pendingScheduleEntries.length === 0) return;
  const epoch = conversationEpoch;
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
    if (epoch !== conversationEpoch) return;
    scheduleSyncState = 'ready';
    pendingScheduleEntries = data.pending_schedule || [];
    scheduleEvents = data.events || [];
    scheduleValidation = data.schedule_validation || scheduleValidation;
    _hydrateScheduleState();
    renderScheduleGrid();
  } catch (err) {
    console.warn('schedule refresh failed:', err);
    if (epoch === conversationEpoch) { scheduleSyncState = 'error'; renderScheduleGrid(); notifySchedule('Could not refresh your schedule. Try again.'); }
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
      <span class="material-symbols-outlined">${solonIcon('close')}</span>
    </button>`;
  region.appendChild(toast);
  scheduleToastTimer = setTimeout(dismissScheduleToast, 5000);
}
function toggleSchedule() {
  setWorkspaceView('ask');
  scheduleOpen = !scheduleOpen;
  document.getElementById('schedulePanel').classList.toggle('open', scheduleOpen);
  document.body.classList.toggle('schedule-open', scheduleOpen);
  document.getElementById('toggleScheduleBtn')?.setAttribute('aria-expanded', String(scheduleOpen));
  syncScheduleChrome();
}

function syncScheduleChrome() {
  const panel = document.getElementById('schedulePanel');
  panel.inert = !scheduleOpen;
  document.getElementById('scheduleNav').classList.toggle('is-active', scheduleOpen);
  document.getElementById('scheduleNav').setAttribute('aria-expanded', String(scheduleOpen));
  toggleMobileSidebar(false);
  if (scheduleOpen) {
    closeUserPopover();
  } else if (panel.contains(document.activeElement)) {
    document.getElementById('toggleScheduleBtn').focus();
  }
}

const DAYS = ['Mon','Tue','Wed','Thu','Fri'];
const ROW_HEIGHT = 58;
const HEADER_HEIGHT = 34;
const SCHEDULE_START_HOUR = 8;
const SCHEDULE_END_HOUR = 22;
const FRIDAY_FREE_EMOJIS = ['🎉', '🥳', '😊', '😄'];
// Choose once per page visit so normal rerenders don't make the metric jump.
const fridayFreeEmoji = FRIDAY_FREE_EMOJIS[Math.floor(Math.random() * FRIDAY_FREE_EMOJIS.length)];
let scheduleGridStart = SCHEDULE_START_HOUR;
let scheduleGridDays = DAYS;

function isTimedScheduleEvent(event) {
  return ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].includes(event.day)
    && parseTimeToMinutes(event.start) >= 0 && parseTimeToMinutes(event.end) > parseTimeToMinutes(event.start);
}

function scheduleEntryHasEvent(entry, events = scheduleEvents) {
  return events.some(e => isTimedScheduleEvent(e)
    && scheduleCourseKey(e.term, e.course_id) === scheduleCourseKey(entry.term, entry.course_id)
    && [e.section_num, e.section_code, e.section].includes(entry.section));
}

function scheduleEntrySection(entry) {
  const matches = section => section && [section.section_num, section.section_code, section.num, section.code].includes(entry.section);
  if (matches(entry.materialized_section)) return entry.materialized_section;
  // Saved cards contain the exact server-sourced section, including TBA/online details.
  const cards = [...planObjects.values()].flat().filter(card =>
    scheduleCourseKey(planTerm(card), card.course_id) === scheduleCourseKey(entry.term, entry.course_id));
  for (const card of cards.reverse()) {
    const sections = [...(card.sections || []), ...(card.section_groups || []).flatMap(g => [g.primary, ...(g.secondaries || [])])];
    const match = sections.find(matches);
    if (match) return match;
  }
  return null;
}

function scheduleEntryStatus(entry) {
  if (scheduleEntryHasEvent(entry)) return 'resolved';
  const section = scheduleEntrySection(entry);
  const start = parseTimeToMinutes(section?.start_time);
  const end = parseTimeToMinutes(section?.end_time);
  if (entry.materialization_status === 'tba' || section &&
      (!section.days || section.days === 'TBA' || /^(true|1|yes)$/i.test(String(section.time_is_tba)) || start < 0 || end <= start)) return 'tba';
  return 'unresolved';
}

function renderUntimedSchedule(entries, events) {
  const untimed = entries.filter(entry => !scheduleEntryHasEvent(entry, events));
  if (!untimed.length) return '';
  return `<section class="schedule-untimed" aria-label="Sections without a scheduled time">
    <h3>Time not scheduled <span>${untimed.length} ${untimed.length === 1 ? 'section' : 'sections'}</span></h3>
    <p>Added to your plan. These sections have no confirmed time on the weekly grid.</p>
    <div class="schedule-untimed-items">${untimed.map(entry => {
      const section = scheduleEntrySection(entry) || {};
      const label = scheduleEntryStatus(entry) === 'tba' ? 'Time TBA' : 'Time unavailable';
      const details = [section.section_type || section.type, entry.section, section.location].filter(Boolean).join(' · ');
      return `<button type="button" class="schedule-untimed-item" data-cid="${escAttr(entry.course_id)}" data-sec="${escAttr(entry.section)}" data-term="${escAttr(entry.term)}" onclick="selectUntimedScheduleEntry(this)">
        <strong>${escHTML(_prettyCourseId(entry.course_id))}</strong><span>${escHTML(details)}</span><span class="schedule-untimed-status">${label}</span>
      </button>`;
    }).join('')}</div></section>`;
}

function selectUntimedScheduleEntry(button) {
  selectedScheduleCourse = scheduleCourseKey(button.dataset.term, button.dataset.cid);
  syncScheduleHighlight();
  const details = document.querySelector('.schedule-details');
  if (details) { details.open = true; details.scrollIntoView({block: 'nearest'}); }
}

function scheduleMetrics(entries, events) {
  const courses = new Map();
  const knownCards = [...planObjects.values()].flat();
  for (const entry of entries) {
    const key = scheduleCourseKey(entry.term, entry.course_id);
    if (courses.has(key)) continue;
    const card = knownCards.find(c => scheduleCourseKey(planTerm(c), c.course_id) === key);
    courses.set(key, exactUnits(entry.units ?? entry.materialized_section?.units ?? card?.units));
  }
  const values = [...courses.values()];
  const units = values.every(u => u !== null) ? values.reduce((a,b) => a+b, 0) : null;
  const times = events.map(e => parseTimeToMinutes(e.start)).filter(t => t >= 0);
  const earliest = times.length ? displayTime(`${Math.floor(Math.min(...times) / 60)}:${String(Math.min(...times) % 60).padStart(2, '0')}`) : '—';
  const incomplete = entries.some(entry => !scheduleEntryHasEvent(entry, events));
  const fridayBusy = events.some(e => e.day === 'Fri' && parseTimeToMinutes(e.end) > 12 * 60);
  const friday = fridayBusy ? 'Busy' : incomplete ? 'Unknown' : times.length ? 'Free' : '—';
  return {count: courses.size, units, earliest, friday, incomplete};
}

function renderScheduleOverview(entries, events, term, terms) {
  const m = scheduleMetrics(entries, events);
  const season = displayTerm(term === 'unknown' ? '' : term).replace(/\s+\d{4}$/, '').toLowerCase();
  const title = season ? `Your ${season} quarter` : 'Your quarter';
  const metric = (label, value, note, tone = '') => `<div class="schedule-metric"><span class="metric-label">${label}</span><span class="metric-value">${escHTML(String(value))}</span><span class="metric-note ${tone}">${escHTML(note)}</span></div>`;
  const unitNote = m.units === null ? 'Units unavailable' : 'In your plan';
  return `<div class="schedule-overview">
    <div class="schedule-overview-heading"><h2>${escHTML(title)}</h2>
      <div class="constraint-chips">${terms.length > 1 ? terms.map(t => `<button class="constraint-chip" onclick="selectScheduleTerm(this)" data-term="${escAttr(t)}" aria-pressed="${t === term}">${escHTML(displayTerm(t))}</button>`).join('') : `<span class="constraint-chip" id="scheduleCount">${m.count} courses</span>`}</div>
    </div>
    <div class="schedule-metrics">
      ${metric('UNITS', m.units ?? '—', unitNote)}
      ${metric('COURSES', m.count, 'In your plan')}
      ${metric('EARLIEST CLASS', m.earliest, m.incomplete ? 'Some times unknown' : events.length ? 'First meeting' : 'No meetings yet')}
      ${metric('FRIDAY AFTERNOON', m.friday === 'Free' ? `Free ${fridayFreeEmoji}` : m.friday, m.friday === 'Free' ? 'No classes after noon' : m.friday === 'Busy' ? 'Classes after noon' : 'Awaiting meeting times', m.friday === 'Free' ? 'ok' : '')}
    </div>
  </div>`;
}

function selectScheduleTerm(btn) {
  scheduleViewTerm = btn.dataset.term;
  document.getElementById('scheduleBody').scrollTop = 0;
  selectedScheduleCourse = null;
  renderScheduleGrid();
}

function renderScheduleGrid() {
  const body = document.getElementById('scheduleBody');
  if (!body) return;
  const terms = [...new Set(pendingScheduleEntries.map(e => canonicalTerm(e.term) || 'unknown'))];
  if (!terms.includes(scheduleViewTerm)) scheduleViewTerm = terms[0] || canonicalTerm(currentTermContext?.term || '');
  const term = scheduleViewTerm;
  const entries = pendingScheduleEntries.filter(e => (canonicalTerm(e.term) || 'unknown') === term);
  const events = scheduleEvents.filter(e => canonicalTerm(e.term) === term && isTimedScheduleEvent(e));
  const risk = (scheduleValidation?.conflicts || []).length || (scheduleValidation?.warnings || []).length || (scheduleValidation?.unknowns || []).length;
  const sync = document.getElementById('scheduleSync');
  sync.textContent = scheduleSyncState === 'error' ? 'Unable to sync' : risk ? 'Needs a look' : pendingScheduleEntries.length && scheduleSyncState === 'ready' ? 'In sync' : 'Draft';
  sync.classList.toggle('is-warning', !!risk || scheduleSyncState === 'error');
  document.getElementById('scheduleTermLabel').textContent = (term === 'unknown' ? 'Term not set' : displayTerm(term)) || 'Your schedule';
  document.getElementById('scheduleRefreshBtn').disabled = !pendingScheduleEntries.length;
  document.getElementById('scheduleClearBtn').disabled = !pendingScheduleEntries.length;
  let html = renderScheduleOverview(entries, events, term, terms);
  if (!entries.length) {
    html += `<div class="schedule-empty">${solonIcon('calendar')}<div class="schedule-empty-title">Your quarter starts here</div><div class="schedule-empty-sub">Ask Solon for course recommendations, then add sections to your plan.</div></div>`;
  }
  html += renderUntimedSchedule(entries, events);
  // Always show 08:00–22:00; extend only if a real meeting falls outside it.
  scheduleGridStart = Math.min(SCHEDULE_START_HOUR, ...events.map(e => Math.floor(parseTimeToMinutes(e.start) / 60)));
  const end = Math.max(SCHEDULE_END_HOUR, ...events.map(e => Math.ceil(parseTimeToMinutes(e.end) / 60)));
  scheduleGridDays = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].filter(d => DAYS.includes(d) || events.some(e => e.day === d));
  html += `<div class="schedule-calendar"><div class="schedule-grid" style="--schedule-days:${scheduleGridDays.length}"><div class="sg-corner"></div>`;
  for (const d of scheduleGridDays) html += `<div class="sg-day-header">${d}</div>`;
  for (let h = scheduleGridStart; h < end; h++) {
    html += `<div class="sg-time-label">${displayTime(`${h}:00`)}</div>`;
    for (const day of scheduleGridDays) html += `<div class="sg-cell" data-day="${day}" data-hour="${h}"></div>`;
  }
  // Label the closing boundary without introducing a spurious 22:00–23:00 slot.
  html += `<div class="sg-time-label sg-time-end">${displayTime(`${end}:00`)}</div>`;
  for (const day of scheduleGridDays) html += '<div class="sg-end-cell" aria-hidden="true"></div>';
  html += '</div></div><div class="schedule-grid-note">Select a class to see it in your plan. Choose offered sections in the course details.</div>';
  if (pendingScheduleEntries.length) html += `<details class="schedule-details"${risk ? ' open' : ''}><summary>Plan details · ${pendingScheduleEntries.length} sections${risk ? ' · Review notices' : ''}</summary>${_renderScheduleEntryList()}</details>`;
  const previousScroll = body.scrollTop;
  body.innerHTML = html;
  const layout = _computeOverlapLayout(events);
  const conflictKeys = _scheduleConflictKeys();
  for (const ev of events) {
    const meta = layout.get(ev) || {col: 0, cols: 1};
    placeEvent(ev, meta.col, meta.cols, _scheduleItemHasConflict(conflictKeys, ev));
  }
  body.scrollTop = previousScroll;
  syncPlanObjects();
  syncScheduleHighlight();
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
    const materialization = scheduleEntryStatus(entry);
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
        <span class="material-symbols-outlined">${solonIcon('close')}</span>
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
    notifySchedule('Could not remove this section. Please try again.');
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
  const dayIdx = scheduleGridDays.indexOf(ev.day);
  if (dayIdx < 0) return;

  const startMin = parseTimeToMinutes(ev.start);
  const endMin = parseTimeToMinutes(ev.end);
  if (startMin < 0 || endMin < 0) return;

  const gridEl = document.querySelector('.schedule-grid');
  if (!gridEl) return;

  const topOffset = ((startMin - scheduleGridStart * 60) / 60) * ROW_HEIGHT + HEADER_HEIGHT + 2;
  const height = ((endMin - startMin) / 60) * ROW_HEIGHT;

  // Column math: each day column is split into `cols` sub-tracks.
  // 4px outer gap on each side of the day column + 2px between tracks.
  const dayCol = `((100% - 48px) / ${scheduleGridDays.length})`;
  const trackW = `((${dayCol} - 8px) / ${cols} - ${cols > 1 ? '2px' : '0px'})`;
  const trackX = `(${col} * (${trackW} + 2px))`;

  const evEl = document.createElement('button');
  evEl.type = 'button';
  evEl.className = `sg-event${hasConflict ? ' has-conflict' : ''}`;
  evEl.dataset.cid = ev.course_id;
  evEl.dataset.sec = ev.section_num || '';
  evEl.dataset.term = ev.term || '';
  evEl.style.position = 'absolute';
  evEl.style.top = topOffset + 'px';
  evEl.style.height = Math.max(height - 4, 24) + 'px';
  evEl.style.left = `calc(48px + ${dayIdx} * ${dayCol} + 4px + ${trackX})`;
  evEl.style.width = `calc(${trackW})`;

  evEl.innerHTML = `<div class="sg-event-top"><span class="sg-event-id">${escHTML(_prettyCourseId(ev.course_id || ''))}</span>${hasConflict ? '<span class="sg-event-type">Check</span>' : ''}</div>
    <div class="sg-event-bot"><span class="sg-event-time">${escHTML(displayTime(ev.start))}–${escHTML(displayTime(ev.end))}</span></div>`;
  evEl.title = [
    `${ev.course_id}${ev.section_num ? ' · ' + ev.section_num : ''}`,
    ev.term || '',
    ev.title || '',
    `${ev.day} ${ev.start}-${ev.end}`,
    ev.location || '',
    ev.instructor || '',
    ev.section_code ? `Code ${ev.section_code}` : '',
    'Select to view plan details',
  ].filter(Boolean).join('\n');
  evEl.addEventListener('click', () => {
    selectedScheduleCourse = scheduleCourseKey(ev.term, ev.course_id);
    syncScheduleHighlight();
    const details = document.querySelector('.schedule-details');
    if (details) details.open = true;
  });

  gridEl.appendChild(evEl);
}

function parseTimeToMinutes(t) {
  const match = /^(\d{1,2}):(\d{2})\s*([AP]M?)?$/i.exec(String(t || '').trim());
  if (!match) return -1;
  let hour = Number(match[1]);
  const minute = Number(match[2]);
  if (minute > 59 || hour > 23 || match[3] && (hour < 1 || hour > 12)) return -1;
  if (match[3]) hour = hour % 12 + (/^p/i.test(match[3]) ? 12 : 0);
  return hour * 60 + minute;
}
