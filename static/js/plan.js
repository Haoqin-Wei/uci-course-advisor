/* Compact plan objects consume the existing structured recommendation cards.
   Only sections the user adds are shown in Schedule. */
const planObjects = new Map();
let planSequence = 0;
let activePlanId = null;
let selectedScheduleCourse = null;
let hoveredScheduleCourse = null;

function resetPlanObjects() {
  planObjects.clear();
  document.getElementById('scheduleBody').scrollTop = 0;
  activePlanId = null;
  scheduleViewTerm = '';
  scheduleSyncState = 'idle';
  selectedScheduleCourse = null;
  hoveredScheduleCourse = null;
}

function planTerm(card) { return canonicalTerm(card.term || currentTermContext?.term || ''); }

function planPrimary(card) {
  const sections = (card.sections || []).filter(s => !s.is_cancelled);
  const groups = (card.section_groups || []).map(g => g.primary).filter(s => s && !s.is_cancelled);
  const code = card.primary_code;
  const match = code && (sections.find(s => String(s.section_code) === String(code))
    || groups.find(s => String(s.code) === String(code)));
  if (match) return match;
  const primaries = sections.filter(s => /^(lec|sem|stu)$/i.test(s.section_type || s.type || ''));
  if (primaries.length === 1) return primaries[0];
  if (groups.length === 1) return groups[0];
  return null;
}

function planSectionId(section) {
  return section?.section_num || section?.num || section?.section_code || section?.code || '';
}

function planIsAdded(card) { return addedCourses.has(scheduleCourseKey(planTerm(card), card.course_id)); }

function exactUnits(value) {
  if (value === null || value === undefined || value === '') return null;
  const text = String(value).trim();
  if (!/^\d+(\.\d+)?$/.test(text)) return null;
  return Number(text);
}

function planMeetingLabel(card) {
  if (planIsAdded(card)) {
    const events = scheduleEvents.filter(e => scheduleCourseKey(e.term, e.course_id) === scheduleCourseKey(planTerm(card), card.course_id));
    const primary = events.find(e => /^(lec|sem|stu)$/i.test(e.section_type || '')) || events[0];
    if (primary) {
      const days = events.filter(e => e.section_num === primary.section_num && e.start === primary.start)
        .map(e => ({Mon:'M', Tue:'Tu', Wed:'W', Thu:'Th', Fri:'F', Sat:'Sa', Sun:'Su'})[e.day] || e.day).join('');
      return `${days} ${displayTime(primary.start)}–${displayTime(primary.end)}`;
    }
  }
  const section = planPrimary(card);
  if (!section) return 'Choose a section';
  if (section.start_time && section.end_time) {
    // Legacy cards may carry a shared WebSoc suffix, e.g. 3:30-4:50p.
    // Keep that source range intact instead of mislabelling the start as AM.
    if (/[ap]m?$/i.test(section.end_time) && !/[ap]m?$/i.test(section.start_time)) {
      return section.time_display || section.time || `${section.days || ''} ${section.start_time}–${section.end_time}`;
    }
    return `${section.days || ''} ${displayTime(section.start_time)}–${displayTime(section.end_time)}`.trim();
  }
  return section.time_display || section.time || 'Time to be confirmed';
}

function renderPlanObject(cards) {
  const id = `plan-${++planSequence}`;
  planObjects.set(id, cards);
  activePlanId = id;
  const units = cards.map(c => exactUnits(c.units));
  const unitLabel = units.every(u => u !== null) ? ` · ${units.reduce((a,b) => a+b, 0)} units` : '';
  const rows = cards.map((card, index) => {
    const added = planIsAdded(card);
    const check = !planPrimary(card);
    return `<div class="plan-item" data-index="${index}">
      <div class="plan-row" onmouseenter="highlightPlanCourse(this, true)" onmouseleave="highlightPlanCourse(this, false)">
        <button class="plan-pick" onclick="selectPlanCourse(this)" aria-label="Show ${escAttr(card.course_id || 'course')} in schedule">
          <span class="plan-code">${escHTML(_prettyCourseId(card.course_id || 'Course'))}</span>
          <span class="plan-time">${escHTML(planMeetingLabel(card))}</span>
          <span class="plan-tag${added ? ' is-kept' : check ? ' is-check' : ''}">${added ? 'Kept' : check ? 'Check' : 'New'}</span>
        </button>
        <button class="plan-details-toggle" aria-expanded="false" aria-controls="${id}-details-${index}" aria-label="Details and sections for ${escAttr(card.course_id || 'course')}" onclick="togglePlanDetails(this)">${solonIcon('chevron')}</button>
      </div>
      <div class="plan-details" id="${id}-details-${index}" hidden>${renderCard(card)}</div>
    </div>`;
  }).join('');
  return `<section class="plan-object" id="${id}" aria-label="Course plan">
    <div class="plan-heading"><span>COURSE PLAN</span><span>${cards.length} ${cards.length === 1 ? 'course' : 'courses'}${unitLabel}</span></div>
    ${rows}
    <div class="plan-actions">
      <button class="primary-button plan-add" onclick="acceptPlan(this)">${planActionLabel(cards)}</button>
      <button class="quiet-button plan-open" onclick="openPlan(this)">Open in schedule →</button>
    </div>
  </section>`;
}

function planActionLabel(cards) {
  const pending = cards.filter(c => !planIsAdded(c));
  const terms = [...new Set(cards.map(planTerm))];
  const season = terms.length === 1 ? displayTerm(terms[0]).replace(/\s+\d{4}$/, '') : '';
  const target = season ? `${season} plan` : 'plan';
  if (!pending.length) return `Added to ${target} ✓`;
  if (pending.some(c => !planSectionId(planPrimary(c)) || c.requires_secondary)) return 'Choose sections';
  return `Add to ${target}`;
}

function cardFromPlanElement(el) {
  const item = el.closest('.plan-item');
  const plan = el.closest('.plan-object');
  return planObjects.get(plan?.id)?.[Number(item?.dataset.index)];
}

function highlightPlanCourse(el, on) {
  const card = cardFromPlanElement(el);
  hoveredScheduleCourse = on && card ? scheduleCourseKey(planTerm(card), card.course_id) : null;
  syncScheduleHighlight();
}

function syncScheduleHighlight() {
  document.querySelectorAll('.sg-event, .schedule-untimed-item').forEach(el => {
    const key = scheduleCourseKey(el.dataset.term, el.dataset.cid);
    el.classList.toggle('is-highlighted', key === hoveredScheduleCourse);
    el.classList.toggle('is-selected', key === selectedScheduleCourse);
  });
}

function selectPlanCourse(el) {
  const card = cardFromPlanElement(el);
  if (!card) return;
  activePlanId = el.closest('.plan-object').id;
  scheduleViewTerm = planTerm(card);
  selectedScheduleCourse = scheduleCourseKey(planTerm(card), card.course_id);
  openSchedule();
  renderScheduleGrid();
}

function togglePlanDetails(btn, force) {
  const details = document.getElementById(btn.getAttribute('aria-controls'));
  const open = force ?? details.hidden;
  details.hidden = !open;
  btn.setAttribute('aria-expanded', String(open));
}

function openPlan(btn) {
  activePlanId = btn.closest('.plan-object').id;
  openSchedule();
  renderScheduleGrid();
}

async function acceptPlan(btn) {
  const root = btn.closest('.plan-object');
  const cards = planObjects.get(root.id) || [];
  const pending = cards.filter(c => !planIsAdded(c));
  if (!pending.length) return;
  if (pending.some(c => !planSectionId(planPrimary(c)) || c.requires_secondary)) {
    root.querySelectorAll('.plan-details-toggle').forEach(toggle => {
      if (planIsAdded(cardFromPlanElement(toggle))) return;
      togglePlanDetails(toggle, true);
      const sectionToggle = toggle.closest('.plan-item').querySelector('.cc-sg-toggle');
      if (sectionToggle?.getAttribute('aria-expanded') === 'false') toggleSectionGroups(sectionToggle);
    });
    return;
  }
  if (!currentSessionId) { notifySchedule('Send a message to start a plan first.'); return; }
  const epoch = conversationEpoch;
  btn.disabled = true;
  btn.dataset.saving = 'true';
  btn.textContent = 'Adding…';
  activePlanId = root.id;
  try {
    for (const card of pending) {
      if (epoch !== conversationEpoch) return;
      await addCourse(card.course_id, planSectionId(planPrimary(card)), null, planTerm(card));
    }
  } catch (error) {
    if (epoch === conversationEpoch) notifySchedule('Could not add every section. Your saved courses are still in the plan. Please try again.');
  } finally {
    delete btn.dataset.saving;
    if (epoch === conversationEpoch) syncPlanObjects();
  }
}

function syncPlanObjects() {
  for (const [id, cards] of planObjects) {
    const root = document.getElementById(id);
    if (!root) continue;
    root.querySelectorAll('.plan-item').forEach((item, index) => {
      const card = cards[index];
      const added = planIsAdded(card);
      const check = !planPrimary(card);
      const tag = item.querySelector('.plan-tag');
      tag.className = 'plan-tag' + (added ? ' is-kept' : check ? ' is-check' : '');
      tag.textContent = added ? 'Kept' : check ? 'Check' : 'New';
      item.querySelector('.plan-time').textContent = planMeetingLabel(card);
    });
    const button = root.querySelector('.plan-add');
    if (!button.dataset.saving) {
      button.textContent = planActionLabel(cards);
      button.disabled = cards.every(planIsAdded);
    }
  }
}
