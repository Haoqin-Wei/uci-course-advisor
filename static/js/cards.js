function renderValidationFooter(report) {
  const ICONS = { error: '⚠', warn: '⚠', info: 'ℹ' };
  const issues = report.issues || [];
  let html = '<div class="validation-footer">';
  html += `<div class="validation-footer-header">🔍 Data check  ·  ${issues.length} issue${issues.length === 1 ? '' : 's'}</div>`;
  for (const issue of issues) {
    const sev = (issue.severity || 'info').toLowerCase();
    const icon = ICONS[sev] || '·';
    html += `<div class="validation-issue validation-issue-${escAttr(sev)}">`;
    html += `<div class="validation-issue-icon">${icon}</div>`;
    html += `<div class="validation-issue-msg">`;
    if (issue.code) html += `<code>${escHTML(issue.code)}</code> `;
    html += escHTML(issue.message || '');
    html += `</div></div>`;
  }
  html += '</div>';
  return html;
}

/* Stitch-style recommendation card. Consumes the schema produced by
   the `propose_recommendation` agent tool (app/agent/tools.py):
       {course_id, title, units, category, priority, reason,
        prereq_met, prereq_missing[], prereq_required[],
        sections: [...], grade_distribution: {avg_gpa, ...},
        ge_category, found}
   Falls through gracefully on missing fields — found=false renders
   a faded stub, missing sections/grade just hide their badges. */
const CATEGORY_LABELS = {
  core:      'Core',
  practical: 'Practical',
  career:    'Career',
  advanced:  'Advanced',
  elective:  'Elective',
};

const SOURCE_BADGE_LABELS = {
  db_verified: 'DB Verified',
  official_uci: 'Official UCI',
  official_web: 'Official Web',
  live_anteater_websoc: 'Live WebSoc',
  websoc_comments: 'WebSoc Comments',
  official_department_link: 'Official Department Link',
  local_not_live: 'Not Live',
  external_web: 'External Web',
  anecdotal: 'Anecdotal',
  unverified: 'Unverified',
};

function _catSlug(card) {
  const c = (card?.category || 'elective').toLowerCase();
  return CATEGORY_LABELS[c] ? c : 'elective';
}

function _firstSection(card) {
  // Sections from db.get_sections may include cancelled rows — prefer
  // the first non-cancelled section with a time, else first overall.
  const all = card?.sections || [];
  for (const s of all) {
    if (!s.is_cancelled && s.days && s.start_time) return s;
  }
  return all[0] || null;
}

function _renderCardBadges(card, cat) {
  const out = [];
  out.push(
    `<span class="cc-badge cc-badge-cat-${cat}">${CATEGORY_LABELS[cat]}</span>`
  );
  if ((card?.priority || '').toLowerCase() === 'high') {
    out.push(`<span class="cc-badge cc-badge-priority-high">High Priority</span>`);
  }
  if (card?.ge_category) {
    out.push(`<span class="cc-badge">${escHTML(card.ge_category)}</span>`);
  }
  const gpa = card?.grade_distribution?.avg_gpa;
  if (typeof gpa === 'number' && gpa > 0) {
    out.push(`<span class="cc-badge cc-badge-success">Avg GPA ${gpa.toFixed(1)}</span>`);
  }
  return out.join('');
}

function _renderCardSourceBadges(card) {
  // Optional future extension for field-level web provenance. Existing
  // card DB fields (`course_source`, `course_provenance`,
  // `section_source`) remain untouched; web badges must arrive in this
  // separate field so web evidence never masquerades as DB verified.
  const badges = card?.field_source_badges || [];
  if (!Array.isArray(badges) || badges.length === 0) return '';
  return badges.map((raw) => {
    const key = (typeof raw === 'string' ? raw : raw?.kind || raw?.source_class || '')
      .toLowerCase();
    const label = SOURCE_BADGE_LABELS[key] || SOURCE_BADGE_LABELS.unverified;
    const css = key && SOURCE_BADGE_LABELS[key] ? key : 'unverified';
    return `<span class="cc-badge cc-source-badge cc-source-${escAttr(css)}">${label}</span>`;
  }).join('');
}

/* Section enrollment row: the primary (Lec) code as the headline
   chip, and — when the course requires a paired Discussion / Lab /
   Studio — a smaller "+ Dis required" reminder pill. WebReg rejects
   schedules that have only the Lec without a paired secondary, so
   this isn't a hint, it's a hard rule (policies.ENROLLMENT_RULES).
   The pill's tooltip lists the bookable secondary codes. */
function _renderCodeRow(card) {
  const codes = card?.enrollment_codes || [];
  // Primary first (dispatcher orders enrollment_codes[0] = primary).
  const primary = codes[0] || null;
  // Legacy non-agent path: fall back to first raw section.
  const fallback = primary ? null : (card?.sections || [])[0];
  const code = primary?.code || fallback?.section_code;
  if (!code) return '';
  const meta = primary || fallback || {};
  const tip = [meta.type || meta.section_type,
               meta.days,
               meta.time || (meta.start_time && meta.end_time
                              ? `${meta.start_time}-${meta.end_time}` : null),
               (meta.instructors || [])[0]]
              .filter(Boolean).join(' · ');

  let secondaryPill = '';
  if (card?.requires_secondary) {
    const secs = card?.secondary_codes || [];
    const type = card?.secondary_type || 'Dis';
    // Tooltip lists each bookable secondary so the student can see
    // their options without rendering 10 chips on the card itself.
    const secTip = secs.slice(0, 12).map(s => {
      const inst = (s.instructors || [])[0];
      return [s.code, s.type, s.days, s.time, inst].filter(Boolean).join(' · ');
    }).join('\n');
    const overflow = secs.length > 12 ? `\n…and ${secs.length - 12} more` : '';
    secondaryPill = `<span class="cc-code-secondary"
                           title="Must also enroll in a ${escHTML(type)}:\n${escHTML(secTip + overflow)}">
      + ${escHTML(type)} required
    </span>`;
  }

  return `<div class="cc-codes">
    <span class="cc-code cc-code-primary"
          title="${escHTML(tip || '')}">${escHTML(code)}</span>
    ${secondaryPill}
  </div>`;
}

/* Per-term deadline footer.
   Renders a small monospace strip beneath the badges/reason showing
   the term's free-window end (last day of WebReg self-service) and
   the late add/drop end (last day to drop with dean approval — i.e.
   the practical "drop without W" deadline). Hidden if the card
   doesn't carry term_deadlines (legacy non-agent path). */
function _renderDeadlineRow(card) {
  const d = card?.term_deadlines || null;
  if (!d) return '';
  const fmt = (iso) => {
    if (!iso) return null;
    // 2026-10-09 → "Oct 9". Plain Date parsing is reliable for
    // YYYY-MM-DD; just guard against future schema drift.
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
    if (!m) return iso;
    const months = ['Jan','Feb','Mar','Apr','May','Jun',
                    'Jul','Aug','Sep','Oct','Nov','Dec'];
    return `${months[parseInt(m[2],10)-1]} ${parseInt(m[3],10)}`;
  };
  const pieces = [];
  if (d.free_window_end) {
    pieces.push(`<span title="Last day to add/drop/change without dean approval (WebReg)">
      Free add/drop · <b>${escHTML(fmt(d.free_window_end))}</b>
    </span>`);
  }
  if (d.late_add_drop_end) {
    pieces.push(`<span title="Last day to drop with dean approval (via Enrollment Exceptions in Student Access). After this date a drop becomes a W on the transcript.">
      Drop w/o W · <b>${escHTML(fmt(d.late_add_drop_end))}</b>
    </span>`);
  }
  if (!pieces.length) return '';
  return `<div class="cc-deadlines">${pieces.join('<span class="cc-deadlines-sep">·</span>')}</div>`;
}

/* Section-groups sub-cards — one sub-card per Lec letter group,
   laid out side-by-side (wraps on narrow screens). Hidden by
   default; toggleable via the "Show sections" chevron on the card.

   Each sub-card carries:
     · Lec letter badge + 5-digit registrar code
     · Professor name + RMP rating chip (star + score)
     · Days + meeting time
     · Classroom
     · Final-exam time + room
     · Paired Dis/Lab chip strip (codes + status badges)

   Each card holds its own collapse state so multiple recommendation
   cards can be expanded independently. */
function _renderSectionGroups(card) {
  const groups = card?.section_groups || [];
  if (groups.length === 0) return '';
  // Collapsed by default — `.open` is added by toggleSectionGroups()
  // or by cardClickHandler() when the user taps the card body.
  // No `hidden` attribute so CSS can animate the open/close transition.
  const cid = card?.course_id || '';
  return `<div class="cc-sg-wrap">
    <div class="cc-sg-cards">
      ${groups.map(g => _renderOneSectionGroup(g, cid)).join('')}
    </div>
  </div>`;
}

function _renderOneSectionGroup(g, courseId) {
  const p = g.primary;
  if (!p) {
    return `<div class="cc-sg-card cc-sg-orphan">
      <div class="cc-sg-card-head">
        <span class="cc-sg-letter">${escHTML(g.letter || '?')}</span>
        <em>Lec unavailable</em>
      </div>
      <div class="cc-sg-secs-block">
        ${_renderSecondaryChips(g.secondaries)}
      </div>
    </div>`;
  }

  const code     = escHTML(p.code || '');
  const letter   = escHTML(p.num || p.type || g.letter);
  const time     = _fmtSectionDaysTime(p);
  const room     = p.location ? escHTML(p.location) : '<em>Room TBA</em>';
  const status   = _renderSeatBadge(p);
  const profs    = _renderProfRow(p.ratings, p.instructors);
  const finalExam = _renderFinalExam(p.final_exam);

  const lecNum = p.num || g.letter || '?';
  const addBtn = courseId ? _renderAddBtn(courseId, lecNum, {}) : '';

  return `<div class="cc-sg-card">
    <div class="cc-sg-card-head">
      <span class="cc-sg-letter">${letter}</span>
      <span class="cc-sg-code">${code}</span>
      ${status}
      <span class="cc-sg-spacer"></span>
      ${addBtn}
    </div>
    ${profs}
    <dl class="cc-sg-facts">
      <div class="cc-sg-fact">
        <span class="material-symbols-outlined">schedule</span>
        <dd>${time}</dd>
      </div>
      <div class="cc-sg-fact">
        <span class="material-symbols-outlined">location_on</span>
        <dd>${room}</dd>
      </div>
      ${finalExam}
    </dl>
    ${g.secondaries.length === 0
      ? ''
      : `<div class="cc-sg-secs-block">
           <div class="cc-sg-secs-label">${escHTML(g.secondaries[0]?.type || 'Dis')} sections</div>
           ${_renderSecondaryChips(g.secondaries, courseId)}
         </div>`}
  </div>`;
}

/* Helpers — kept private to the section-group renderer. */
function _fmtSectionDaysTime(s) {
  // Prefer the pre-formatted time_display; otherwise build "MWF 10-10:50".
  if (s.time) return escHTML(s.time);
  const bits = [s.days, s.start_time && s.end_time
                  ? `${s.start_time}-${s.end_time}` : s.start_time].filter(Boolean);
  return bits.length ? escHTML(bits.join(' ')) : 'TBA';
}

function _renderSeatBadge(s) {
  if (s.status === 'FULL')    return `<span class="cc-sg-badge danger">FULL</span>`;
  if (s.status === 'Waitl')   return `<span class="cc-sg-badge warning">Waitlist</span>`;
  if (s.status === 'NewOnly') return `<span class="cc-sg-badge muted">New only</span>`;
  if (typeof s.seats_open === 'number' && s.seats_open <= 5 && s.seats_open > 0) {
    return `<span class="cc-sg-badge warning">${s.seats_open} left</span>`;
  }
  return '';
}

function _renderProfRow(ratings, fallback_instructors) {
  // ratings: [{instructor, avg_rating, num_ratings, tier, tier_label}]
  // fallback_instructors: raw strings if ratings array is missing
  const list = (ratings && ratings.length) ? ratings
                : (fallback_instructors || []).map(n => ({instructor: n}));
  if (!list.length) return '';
  return `<div class="cc-sg-profs">
    ${list.filter(p => p.instructor && p.instructor.toLowerCase() !== 'staff')
          .map(p => {
      const hasScore = typeof p.avg_rating === 'number' && p.avg_rating > 0;
      const tierClass = _rmpTierClass(p.tier, p.num_ratings);
      const star = hasScore
        ? `<span class="cc-sg-rmp ${tierClass}"
                 title="${escHTML(p.tier_label || 'RMP')} · avg ${p.avg_rating} from ${p.num_ratings || 0} ratings">
             <span class="material-symbols-outlined">star</span>
             ${p.avg_rating.toFixed(1)}
             <span class="cc-sg-rmp-n">(${p.num_ratings || 0})</span>
           </span>`
        : `<span class="cc-sg-rmp cc-sg-rmp-na" title="No RateMyProfessor data on file">no rmp</span>`;
      return `<div class="cc-sg-prof">
        <span class="cc-sg-prof-name">${escHTML(p.instructor)}</span>
        ${star}
      </div>`;
    }).join('')
      || `<div class="cc-sg-prof"><span class="cc-sg-prof-name">STAFF</span></div>`}
  </div>`;
}

/* Map the prof's RMP tier slug → CSS color class.
   The slug comes from db.get_professor_rating's `tier.tier` field
   (mostly_positive / mostly_negative / mixed / insufficient_data /
   unrated). We treat insufficient sample (< 10 ratings) the same as
   the explicit insufficient_data tier so a 1-review prof doesn't
   show as bright green. Order: positive → green; negative → red;
   mixed → yellow; else gray. */
function _rmpTierClass(tier, numRatings) {
  if (numRatings != null && numRatings < 10) return 'cc-sg-rmp-insufficient';
  switch ((tier || '').toLowerCase()) {
    case 'mostly_positive': return 'cc-sg-rmp-positive';
    case 'mostly_negative': return 'cc-sg-rmp-negative';
    case 'mixed':           return 'cc-sg-rmp-mixed';
    case 'insufficient_data':
    case 'unrated':
                            return 'cc-sg-rmp-insufficient';
    default:                return 'cc-sg-rmp-mixed';   // fallback when tier missing but score present
  }
}

function _renderFinalExam(fx) {
  if (!fx) return '';
  if (fx.status === 'none') {
    return `<div class="cc-sg-fact">
      <span class="material-symbols-outlined">event_busy</span>
      <dd>No final exam</dd>
    </div>`;
  }
  if (fx.status === 'tba') {
    return `<div class="cc-sg-fact">
      <span class="material-symbols-outlined">event</span>
      <dd>Final: TBA</dd>
    </div>`;
  }
  const loc = fx.location ? ` · ${escHTML(fx.location)}` : '';
  return `<div class="cc-sg-fact">
    <span class="material-symbols-outlined">event</span>
    <dd>Final: ${escHTML(fx.label || '')}${loc}</dd>
  </div>`;
}

/* Secondary (Dis / Lab / Stu) rows below each Lec.
   Each row carries the full enrollment-decision payload because
   the student picks a Dis based on time / room / instructor — a
   bare "A1 36251" doesn't help.

   Columns (mobile collapses to two flex rows):
     · section num (A1) — small letter badge
     · 5-digit code — mono
     · instructor — name only when it isn't STAFF; if the rare
       Dis-taught-by-prof case applies AND we have an RMP score,
       a tier-colored star pill rides alongside
     · meeting time — schedule icon + "F 10:00-10:50"
     · room — pin icon + location
     · status badge — FULL / Waitlist / N left when applicable */
/* Apple Store–style + → ✓ button.
   Args:
     courseId    — canonical course_id ("CS161")
     sectionNum  — section letter ("A") for Lec, letter+digit ("A1") for Dis
     opts.hoverOnly — true to apply `.cc-sg-add-hover` (Dis rows: shown only on row hover).
   The button reads its initial added state from addedSectionKeys
   so re-renders after a render-from-history don't lose the picker
   state. */
function _renderAddBtn(courseId, sectionNum, opts) {
  opts = opts || {};
  const key = `${courseId}:${sectionNum}`;
  const isAdded = addedSectionKeys.has(key);
  const classes = ['cc-sg-add'];
  if (opts.hoverOnly) classes.push('cc-sg-add-hover');
  if (isAdded)        classes.push('added');
  const aria = isAdded ? 'Remove section from schedule' : 'Add section to schedule';
  return `<button type="button" class="${classes.join(' ')}"
                  data-cid="${escHTML(courseId)}"
                  data-sec="${escHTML(sectionNum)}"
                  onclick="event.stopPropagation(); toggleSectionAdd(this)"
                  aria-label="${aria}"
                  aria-pressed="${isAdded ? 'true' : 'false'}">
    <span class="material-symbols-outlined icon-add">add</span>
    <span class="material-symbols-outlined icon-check">check</span>
  </button>`;
}

function _renderSecondaryChips(secondaries, courseId) {
  if (!secondaries || secondaries.length === 0) return '';
  return `<table class="cc-sg-sec-table">
    <tbody>
      ${secondaries.map(s => _renderOneSecondaryRow(s, courseId)).join('')}
    </tbody>
  </table>`;
}

function _renderOneSecondaryRow(s, courseId) {
  const num   = escHTML(s.num || s.type || '?');
  const code  = escHTML(s.code || '—');
  const time  = _fmtSectionDaysTime(s);          // already escaped
  const room  = (s.location && !/^(tba|tbd|on(line|line tba))$/i.test(s.location.trim()))
                ? escHTML(s.location) : '<em>TBA</em>';
  const status = _renderSeatBadge(s);
  // Instructor: drop STAFF / TBA / empty; show the first real name
  // and a tier-colored RMP pill if we have a rating for them.
  const real = (s.instructors || []).filter(
    n => n && !/^(staff|tba)$/i.test(n.trim())
  );
  let instCell = '<span class="cc-sg-row-staff">TA</span>';
  if (real.length > 0) {
    const ratings = (s.ratings || []).filter(r => r.instructor === real[0]);
    const rt = ratings[0] || {};
    const hasScore = typeof rt.avg_rating === 'number' && rt.avg_rating > 0;
    const star = hasScore
      ? `<span class="cc-sg-rmp ${_rmpTierClass(rt.tier, rt.num_ratings)}"
               title="${escHTML(rt.tier_label || 'RMP')} · avg ${rt.avg_rating} from ${rt.num_ratings || 0} ratings">
           <span class="material-symbols-outlined">star</span>
           ${rt.avg_rating.toFixed(1)}
         </span>` : '';
    instCell = `<span class="cc-sg-row-inst">${escHTML(real[0])}</span>${star}`;
  }
  const sNum = s.num || s.type || '?';
  const addBtn = courseId ? _renderAddBtn(courseId, sNum, {hoverOnly: true}) : '';
  return `<tr class="cc-sg-row"
              data-cid="${escHTML(courseId || '')}"
              data-sec="${escHTML(sNum)}"
              ondblclick="event.stopPropagation(); _toggleSectionAddByRow(this)">
    <td><span class="cc-sg-rownum">${num}</span></td>
    <td class="cc-sg-row-code">${code}</td>
    <td class="cc-sg-row-instcell">${instCell}</td>
    <td class="cc-sg-row-meta">
      <span class="material-symbols-outlined">schedule</span>${time}
    </td>
    <td class="cc-sg-row-meta">
      <span class="material-symbols-outlined">location_on</span>${room}
    </td>
    <td class="cc-sg-row-status">${status}</td>
    <td class="cc-sg-row-add">${addBtn}</td>
  </tr>`;
}

/* Toggle the section-groups panel for a card. Accepts either the
   toggle button (from its inline onclick) or the .course-card
   element itself (from cardClickHandler). Animates open/close via
   the .open class; the CSS does the actual transition. */
function toggleSectionGroups(originEl) {
  const card = originEl.classList?.contains('course-card')
               ? originEl
               : originEl.closest('.course-card');
  if (!card) return;
  const panel = card.querySelector('.cc-sg-wrap');
  if (!panel) return;
  const willOpen = !panel.classList.contains('open');
  panel.classList.toggle('open', willOpen);
  // Sync the explicit toggle button's chevron + aria — the button
  // may not be the element that triggered this call (could be the
  // card body), but we still want the chevron in the right state.
  const btn = card.querySelector('.cc-sg-toggle');
  if (btn) {
    btn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
    const icon = btn.querySelector('.material-symbols-outlined');
    if (icon) icon.textContent = willOpen ? 'expand_less' : 'expand_more';
  }
}

/* Card-body click → toggle the section panel. Skip clicks that
   land on a control with its own meaning so we don't fight the
   user's actual intent:
     · the floating + Add button
     · the explicit "Show sections" toggle (it routes through the
       same toggleSectionGroups; clicking either still works)
     · anything INSIDE the open section panel (selecting a section
       chip / hovering a Dis row shouldn't collapse the card)
     · standard interactive elements (button / a / input / select). */
function cardClickHandler(e) {
  // closest("...") returns the nearest matching ancestor or null.
  // If any of these match, we leave the click alone.
  // .cc-sg-add covers both Lec and Dis +/✓ buttons even though they
  // also stopPropagation in their own handler — belt and suspenders.
  const skip = e.target.closest(
    '.cc-sg-add, .cc-sg-toggle, .cc-sg-wrap, a, button, input, select, textarea'
  );
  if (skip) return;
  toggleSectionGroups(e.currentTarget);
}

/* Section-level add/remove toggle (Phase E4 picker).
   Called from each Lec or Dis + button. Optimistically flips the
   button's `.added` class so the App-Store-style + → ✓ animation
   fires immediately, then hits the backend. If the backend
   rejects, we revert the visual change. */
async function toggleSectionAdd(btn) {
  const cid = btn.dataset.cid;
  const sec = btn.dataset.sec;
  if (!cid || !sec) return;
  const key = `${cid}:${sec}`;
  const wasAdded = addedSectionKeys.has(key);
  // Optimistic flip — animation runs from CSS via the .added class.
  if (wasAdded) {
    addedSectionKeys.delete(key);
    btn.classList.remove('added');
    btn.setAttribute('aria-pressed', 'false');
    btn.setAttribute('aria-label', 'Add section to schedule');
  } else {
    addedSectionKeys.add(key);
    btn.classList.add('added');
    btn.setAttribute('aria-pressed', 'true');
    btn.setAttribute('aria-label', 'Remove section from schedule');
  }
  _syncAddedCourse(cid);
  try {
    if (wasAdded) {
      await removeCourse(cid, btn, sec);
    } else {
      await addCourse(cid, sec, btn);
    }
  } catch (err) {
    // Revert visual on failure
    console.error('section toggle failed:', err);
    if (wasAdded) addedSectionKeys.add(key); else addedSectionKeys.delete(key);
    btn.classList.toggle('added', wasAdded);
    _syncAddedCourse(cid);
  }
  // Mirror state on every other button bound to the same key (e.g.
  // both a Lec head + and a Dis row + can target the same key in
  // edge cases; or after a remove via the schedule rail we want all
  // displayed cards to re-sync).
  document.querySelectorAll(
    `.cc-sg-add[data-cid="${CSS.escape(cid)}"][data-sec="${CSS.escape(sec)}"]`
  ).forEach(el => {
    if (el !== btn) {
      el.classList.toggle('added', addedSectionKeys.has(key));
      el.setAttribute('aria-pressed', addedSectionKeys.has(key) ? 'true' : 'false');
    }
  });
}

/* Double-click on a Dis row delegates to the row's own + button so
   the animation runs in the same place — keeps one source of truth. */
function _toggleSectionAddByRow(rowEl) {
  const btn = rowEl.querySelector('.cc-sg-add');
  if (btn) toggleSectionAdd(btn);
}

/* Keep `addedCourses` (course-level) in sync with `addedSectionKeys`
   (section-level). A course counts as added iff ANY of its section
   keys is present. The schedule rail / count read from addedCourses. */
function _syncAddedCourse(courseId) {
  const prefix = courseId + ':';
  let hasAny = false;
  for (const k of addedSectionKeys) {
    if (k.startsWith(prefix)) { hasAny = true; break; }
  }
  if (hasAny) addedCourses.add(courseId);
  else        addedCourses.delete(courseId);
  updateScheduleCount();
}

/* Source-of-truth rebuild from the backend's scheduleEvents.
   Every /schedule/add or /schedule/remove response gives us the
   full list of materialized events; rebuilding local state from
   it prevents the count-vs-grid desync the user hit ("0 courses"
   but Math still appears). Called after every successful add/
   remove response. */
function _hydrateScheduleState() {
  const keys = new Set();
  const courses = new Set();
  for (const ev of scheduleEvents) {
    const sec = ev.section_num || ev.section || '';
    if (ev.course_id) {
      courses.add(ev.course_id);
      if (sec) keys.add(`${ev.course_id}:${sec}`);
    }
  }
  addedSectionKeys = keys;
  addedCourses = courses;
  // Reflect the new state on every visible + button so the picker
  // chevrons match the rail. Done by scanning the DOM rather than
  // tracking individually so re-renders don't drift.
  document.querySelectorAll('.cc-sg-add').forEach(el => {
    const cid = el.dataset.cid;
    const sec = el.dataset.sec;
    if (!cid || !sec) return;
    const isAdded = keys.has(`${cid}:${sec}`);
    el.classList.toggle('added', isAdded);
    el.setAttribute('aria-pressed', isAdded ? 'true' : 'false');
    el.setAttribute('aria-label',
      isAdded ? 'Remove section from schedule' : 'Add section to schedule');
  });
  updateScheduleCount();
}

/* Enrollment-restriction chips (SOC 'Rstr' column decoded).
   Class-level restrictions (E/F/G/H/I/J) are NEVER shown here —
   the dispatcher already hard-drops cards that violate those.
   What's left is non-blocking: auth code, major restriction, fee,
   forced grading basis. Hover for the registrar's full explanation. */
function _renderRestrictionChips(card) {
  const chips = card?.restriction_chips || [];
  if (chips.length === 0) return '';
  return `<div class="cc-rstr">
    ${chips.map(c => `<span class="cc-rstr-pill"
                            title="${escHTML(c.tooltip || '')}">
      <span class="cc-rstr-code">${escHTML(c.code)}</span>
      ${escHTML(c.label)}
    </span>`).join('')}
  </div>`;
}

function _renderPrereqChips(card) {
  const missing  = card?.prereq_missing || [];
  const unknown  = card?.prereq_unknown || [];
  const required = card?.prereq_required || [];
  const status   = card?.prereq_status || (card?.prereq_met ? 'met' : 'not_met');
  // Met → single success "Prereqs met" chip.
  // Not met → one locked chip per missing prereq.
  // Unknown → one warning chip per unverifiable prerequisite reason.
  // No prereqs known → empty column.
  if (status === 'met' && required.length > 0) {
    return `<span class="cc-chip cc-chip-met">
      <span class="material-symbols-outlined">check_circle</span>
      Prereqs met
    </span>`;
  }
  if (status === 'unknown' && unknown.length > 0) {
    return unknown.map(p => `<span class="cc-chip">
      <span class="material-symbols-outlined">help</span>
      ${escHTML(p)}
    </span>`).join('');
  }
  if (missing.length === 0) return '';
  return missing.map(p => `<span class="cc-chip">
    <span class="material-symbols-outlined">lock</span>
    ${escHTML(p)}
  </span>`).join('');
}

function renderCard(card) {
  const cid       = card.course_id || '';
  const isAdded   = addedCourses.has(cid);
  const cat       = _catSlug(card);
  const classes   = [
    'course-card',
    `cat-${cat}`,
    isAdded ? 'added' : '',
    card.found === false ? 'not-found' : '',
  ].filter(Boolean).join(' ');

  // Pull the registrar enrollment code(s) — these are what the
  // student actually inputs to register. Per the propose_recommendation
  // contract the dispatcher only stages cards that HAVE a primary code,
  // so card.primary_code is reliable here; the older `sections[0]`
  // fallback stays for the legacy non-agent rec path.
  const sec        = _firstSection(card);
  const primaryCode = card.primary_code
                      || sec?.section_code
                      || null;
  const addIcon    = isAdded ? 'check' : 'add';
  const title      = card.title || (card.found === false
                       ? 'Not in catalog' : '');
  const reason     = card.reason || '';
  const unitsTxt   = card.units != null ? card.units : '—';

  const groupCount = (card.section_groups || []).length;
  const showSectionsBtn = groupCount > 0 ? `
    <button class="cc-sg-toggle"
            type="button"
            aria-expanded="false"
            onclick="toggleSectionGroups(this)">
      <span>${groupCount === 1 ? '1 section' : groupCount + ' Lec groups'}</span>
      <span class="material-symbols-outlined">expand_more</span>
    </button>` : '';

  return `<div class="${classes}"
              id="card-${escHTML(cid)}"
              onclick="cardClickHandler(event)">
    <div class="cc-id-block">
      <div class="cc-id">${escHTML(cid)}</div>
      ${title ? `<div class="cc-title">${escHTML(title)}</div>` : ''}
      ${_renderCodeRow(card)}
    </div>
    <div class="cc-body">
      <div class="cc-badges">${_renderCardBadges(card, cat)}${_renderCardSourceBadges(card)}</div>
      ${reason ? `<p class="cc-reason">${escHTML(reason)}</p>` : ''}
      ${_renderRestrictionChips(card)}
      ${_renderDeadlineRow(card)}
      ${showSectionsBtn}
    </div>
    <div class="cc-prereqs">${_renderPrereqChips(card)}</div>
    <div class="cc-units-block">
      <span class="cc-units">${escHTML(String(unitsTxt))}</span>
    </div>
    ${_renderSectionGroups(card)}
  </div>`;
}

/* One-button toggle (Stitch UX: the + button stays in place, just
   changes icon). Routes to existing add/remove endpoints. */
function toggleCard(courseId, section, btnEl) {
  if (addedCourses.has(courseId)) {
    removeCourse(courseId, btnEl);
  } else {
    addCourse(courseId, section, btnEl);
  }
}

/* Header strip + N cards, returned as one HTML string. The header
   shows "N INTELLIGENCE MODULES" and the live category legend
   computed from what's actually present in the batch — no point
   showing "Practical" if none of the cards are practical. */
function renderCardsBlock(cards) {
  if (!cards || cards.length === 0) return '';
  const cats = new Set(cards.map(_catSlug));
  const legend = ['core', 'practical', 'career', 'advanced', 'elective']
    .filter(c => cats.has(c))
    .map(c => `<span class="cards-legend-dot" style="--legend-color: var(--cat-${c});">${CATEGORY_LABELS[c]}</span>`)
    .join('');
  const noun = cards.length === 1 ? 'module' : 'modules';
  const head = `<div class="cards-header">
    <span class="cards-header-title">${cards.length} ${noun} recommended</span>
    <div class="cards-legend">${legend}</div>
  </div>`;
  const body = cards.map(renderCard).join('');
  return `<div class="cards-row">${head}${body}</div>`;
}
