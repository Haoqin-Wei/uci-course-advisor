/* ─────────────────────────────────────────────────────
   Phase C — onboarding wizard
   ─────────────────────────────────────────────────────
   4-step state machine. Each step is its own DOM panel; the active
   panel is toggled via display:block. The user advances by clicking
   Continue (gated until the step is "complete" — e.g. a school is
   picked). Save (Step 4) POSTs the accumulated wizardState to
   /api/memory/{user_id}/profile, which the backend merge-writes.

   Triggered from the boot init when:
     - The user is authenticated (currentAuthUser != null)
     - AND their profile has neither major NOR year (= first run)
     - AND they haven't skipped in this session
*/

const WIZARD_SKIPPED_KEY = 'zotadvisor.wizardSkipped';

const wizardState = {
  step: 1,
  school: null,        // {slug, name}
  year:   null,        // 'Freshman' | ... | 'Graduate'
  major:  null,        // {id, name}
  completed_courses: new Set(),   // course IDs
  // Caches so Back navigation doesn't refetch
  _schools: null,
  _majorsBySchool: {},
  _courses: null,      // [{id, title, category}]
};

const WIZARD_STEP_META = {
  1: {name: 'Pick your school',  headline: 'Which school are you in?',
      sub: 'Your school sets the major options on the next screen. Pick whichever feels closest — you can refine later in your profile.'},
  2: {name: 'Your year',         headline: 'What year are you in?',
      sub: 'I use this to weight recommendations — freshman should not get senior-only seminars.'},
  3: {name: 'Your major',        headline: 'Which major are you pursuing?',
      sub: 'I will pull the required-course list for this major so you can mark off what you have already taken.'},
  4: {name: 'Coursework taken',  headline: 'Which courses have you already completed?',
      sub: 'Click a card to mark it as done. The list is grouped alphabetically — scroll through. You can refine this later in your profile editor.'},
};

const WIZARD_YEARS = [
  {value: 'Freshman',  desc: 'Year 1 — 0–44 units completed.'},
  {value: 'Sophomore', desc: 'Year 2 — 45–89 units completed.'},
  {value: 'Junior',    desc: 'Year 3 — 90–134 units completed.'},
  {value: 'Senior',    desc: '135+ units completed; planning the home stretch.'},
  {value: 'Graduate',  desc: 'M.S. / M.A. / Ph.D. — different recommendation logic.'},
];

async function maybeShowWizard() {
  if (!currentAuthUser) return;                 // guests skip
  if (sessionStorage.getItem(WIZARD_SKIPPED_KEY) === '1') return;
  try {
    const r = await fetch(`${API}/api/memory/${USER_ID}`);
    if (!r.ok) return;
    const data = await r.json();
    const profile = data.profile || {};
    if (!profile.major && !profile.year) {
      openWizard();
    }
  } catch (err) {
    console.warn('wizard check failed:', err);
  }
}

// Default entry — first-time auto-trigger. Shows the intro (step 0)
// first, then transitions to the 4-step picker on "Begin".
function openWizard() {
  wizardState.step = 0;     // intro screen
  wizardState.school = null;
  wizardState.year = null;
  wizardState.major = null;
  wizardState.completed_courses.clear();
  document.getElementById('wizardOverlay').classList.add('open');
  // Restart entrance animations every time the intro opens. Without
  // this, re-opening after a Skip would show a static hero (animations
  // only fire once per element insertion).
  _wizardReplayIntroAnimations();
  wizardRenderStep();
  _wizardInstallParallax();
  // We DON'T pre-load schools here — that happens when the user
  // clicks Begin and we enter step 1. Keeps the intro snappy.
}

// Mouse-tracking glow behind the hero. Sets --mx / --my on the
// overlay element so a radial-gradient ::before follows the cursor.
// Throttled by requestAnimationFrame so 60Hz mousemove doesn't trash
// the CSS transition queue.
let _wizardMouseRaf = null;
function _wizardInstallParallax() {
  if (window._wizardParallaxInstalled) return;
  window._wizardParallaxInstalled = true;
  document.addEventListener('mousemove', (e) => {
    const overlay = document.getElementById('wizardOverlay');
    if (!overlay || !overlay.classList.contains('is-intro')) return;
    if (_wizardMouseRaf) return;
    _wizardMouseRaf = requestAnimationFrame(() => {
      _wizardMouseRaf = null;
      const x = (e.clientX / window.innerWidth) * 100;
      const y = (e.clientY / window.innerHeight) * 100;
      overlay.style.setProperty('--mx', `${x}%`);
      overlay.style.setProperty('--my', `${y}%`);
    });
  }, {passive: true});
}

// Force-restart CSS animations on the intro pieces. We do this by
// removing then re-inserting the wizard-intro element; cloneNode +
// replaceChild keeps event listeners off (intro has none) but resets
// every animation-fill-mode to its from-state. Then kick off the
// JS-driven typewriter for the subtitle paragraph.
function _wizardReplayIntroAnimations() {
  const step0 = document.getElementById('wizardStep0');
  if (!step0) return;
  const intro = step0.querySelector('.wizard-intro');
  if (!intro) return;
  const clone = intro.cloneNode(true);
  step0.replaceChild(clone, intro);
  // Cancel any prior typewriter pass before starting a new one
  if (_wizardTypeTimer) { clearTimeout(_wizardTypeTimer); _wizardTypeTimer = null; }
  _wizardTypeIntroSubtitle();
}

const WIZARD_INTRO_SUB =
  "ZotAdvisor is your course-planning copilot. For accurate recommendations, " +
  "the AI needs your school, year, major, and the courses you've already taken.\n\n" +
  "The more honest your profile, the more personal the advice. " +
  "This intro shows once — you can revise everything later from Profile.";

// Per-char typing speed in ms. Slow enough that you can read characters
// land, fast enough to keep total under ~5s with the punctuation pauses
// below. Cursor finishes ~5.4s, then value blocks fade in.
const WIZARD_TYPE_MS_PER_CHAR = 10;
const WIZARD_TYPE_START_DELAY = 900;

// Pauses added AFTER specific characters, to mimic the cadence of a
// human typing rather than a perfectly-metronomic robot. Periods get
// the longest pause (sentence break); commas a small breath; em-dash
// a mid-sentence beat; newlines a paragraph reset.
const WIZARD_TYPE_PAUSE_AFTER = {
  ',':   90,
  '.':  230,
  ';':  160,
  ':':  140,
  '—':  170,
  '\n': 180,
};

let _wizardTypeTimer = null;

function _wizardTypeIntroSubtitle() {
  const el = document.getElementById('wizardIntroTyped');
  if (!el) return;

  // Build the static structure: [text node][cursor span]
  el.textContent = '';
  const textNode = document.createTextNode('');
  const cursor = document.createElement('span');
  cursor.className = 'wizard-cursor';
  el.appendChild(textNode);
  el.appendChild(cursor);

  // Honor user motion preferences — dump the full text immediately
  if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
    textNode.data = WIZARD_INTRO_SUB;
    cursor.classList.add('is-blinking');
    return;
  }

  let i = 0;
  function tick() {
    if (i >= WIZARD_INTRO_SUB.length) {
      cursor.classList.add('is-blinking');
      _wizardTypeTimer = null;
      return;
    }
    const ch = WIZARD_INTRO_SUB[i++];
    textNode.data += ch;

    // Base per-character delay
    let nextDelay = WIZARD_TYPE_MS_PER_CHAR;
    // Punctuation pause — but only when the next char is a space/newline
    // or we hit the end. Otherwise a comma inside a token (rare here)
    // would needlessly slow things down.
    const nextCh = WIZARD_INTRO_SUB[i] || ' ';
    const pause = WIZARD_TYPE_PAUSE_AFTER[ch];
    if (pause && (nextCh === ' ' || nextCh === '\n' || i === WIZARD_INTRO_SUB.length)) {
      nextDelay += pause;
    }
    _wizardTypeTimer = setTimeout(tick, nextDelay);
  }
  _wizardTypeTimer = setTimeout(tick, WIZARD_TYPE_START_DELAY);
}

// Re-opening from the Profile modal: pre-fill state from the saved
// profile so the user sees their existing picks already marked rather
// than starting from scratch. The school/major slugs are resolved
// against the static schools list and the (lazy-loaded) majors list
// — for major specifically, we stash a stub so the render code can
// mark the correct card once /api/onboarding/majors resolves.
async function reopenWizardFromProfile() {
  // Clear the skip flag so the wizard truly re-opens.
  sessionStorage.removeItem(WIZARD_SKIPPED_KEY);
  closeProfile();

  // Re-fetch the freshest profile (sidebar cache could be stale)
  let profile = {};
  try {
    const r = await fetch(`${API}/api/memory/${USER_ID}`);
    if (r.ok) profile = (await r.json()).profile || {};
  } catch (err) {
    console.warn('reopen: profile fetch failed:', err);
  }

  // Hydrate wizard state from profile fields
  wizardState.step = 1;
  wizardState.school = null;
  wizardState.year = profile.year || null;
  wizardState.major = null;
  wizardState.completed_courses.clear();
  for (const cid of (profile.completed_courses || [])) {
    wizardState.completed_courses.add(cid);
  }

  // Resolve school slug → full school object (so card highlighting works)
  if (profile.school_slug) {
    if (!wizardState._schools) {
      try {
        const r = await fetch(`${API}/api/onboarding/schools`);
        wizardState._schools = (await r.json()).schools || [];
      } catch (err) { /* leave _schools null; the load step retries */ }
    }
    const match = (wizardState._schools || []).find(s => s.slug === profile.school_slug);
    if (match) wizardState.school = match;
  }

  // For major: stash an id+name stub. wizardRenderMajors uses .id for
  // the selected-card comparison, so this is enough to highlight it
  // once the majors list lazy-loads on step 3.
  if (profile.program_id) {
    wizardState.major = {
      id:   profile.program_id,
      name: profile.major ? `Major in ${profile.major}` : profile.program_id,
    };
  }

  document.getElementById('wizardOverlay').classList.add('open');
  wizardRenderStep();
  wizardLoadSchools();
}

function closeWizard() {
  document.getElementById('wizardOverlay').classList.remove('open');
}

function wizardRenderStep() {
  // Toggle step panels (0 = intro, 1..4 = real steps)
  for (let i = 0; i <= 4; i++) {
    const el = document.getElementById(`wizardStep${i}`);
    if (el) el.style.display = (i === wizardState.step) ? '' : 'none';
  }

  // Intro page uses its own layout — hide the standard header row.
  const overlay = document.getElementById('wizardOverlay');
  overlay.classList.toggle('is-intro', wizardState.step === 0);

  // Header text (only matters for steps 1-4)
  if (wizardState.step >= 1) {
    const meta = WIZARD_STEP_META[wizardState.step];
    document.getElementById('wizardStepNum').textContent =
      `Step 0${wizardState.step} / 04`;
    document.getElementById('wizardStepName').textContent = meta.name;
    document.getElementById('wizardHeadline').textContent = meta.headline;
    document.getElementById('wizardSub').textContent = meta.sub;
  }

  // Footer state
  document.getElementById('wizardBackBtn').style.display =
    (wizardState.step > 1) ? '' : 'none';
  const nextBtn = document.getElementById('wizardNextBtn');
  if (wizardState.step === 0) {
    nextBtn.textContent = 'Begin';
  } else if (wizardState.step === 4) {
    nextBtn.textContent = 'Save & Finish';
  } else {
    nextBtn.textContent = 'Save & Continue';
  }
  wizardSyncFooterCount();
  wizardSyncNextEnabled();
}

function wizardSyncFooterCount() {
  const c = document.getElementById('wizardCount');
  const label = document.getElementById('wizardCountLabel');
  if (wizardState.step === 0) {
    c.textContent = '·';
    label.textContent = 'about 2 minutes';
  } else if (wizardState.step === 4) {
    c.textContent = wizardState.completed_courses.size;
    label.textContent = (wizardState.completed_courses.size === 1)
      ? 'course marked complete' : 'courses marked complete';
  } else {
    c.textContent = wizardState.step;
    label.textContent = `of 4 steps`;
  }
}

function wizardSyncNextEnabled() {
  const ok = (
    (wizardState.step === 0) ||   // intro CTA always enabled
    (wizardState.step === 1 && wizardState.school) ||
    (wizardState.step === 2 && wizardState.year) ||
    (wizardState.step === 3 && wizardState.major) ||
    (wizardState.step === 4)
  );
  document.getElementById('wizardNextBtn').disabled = !ok;
}

// Persist whatever fields are currently populated in wizardState to
// the profile endpoint. Called on every step transition (Continue /
// Back / Skip) so the user never loses their picks even if they back
// out mid-wizard. The endpoint is a merge-write — empty fields don't
// overwrite anything.
async function wizardSavePartial() {
  const payload = {};
  if (wizardState.school) {
    payload.college = wizardState.school.name;
    payload.school_slug = wizardState.school.slug;
  }
  if (wizardState.year) payload.year = wizardState.year;
  if (wizardState.major) {
    payload.major = wizardState.major.name?.replace(/^Major in\s+/i, '') || wizardState.major.id;
    payload.program_id = wizardState.major.id;
  }
  if (wizardState.completed_courses.size > 0) {
    payload.completed_courses = Array.from(wizardState.completed_courses);
  }
  if (Object.keys(payload).length === 0) return;   // nothing to save yet

  try {
    await fetch(`${API}/api/memory/${USER_ID}/profile`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    // Refresh sidebar so the partial profile shows up immediately
    loadSidebar(true);
  } catch (err) {
    // Non-blocking — the user can still proceed even if persistence
    // hiccups. Their in-memory wizardState is intact for this session.
    console.warn('wizardSavePartial failed:', err);
  }
}

async function wizardBack() {
  if (wizardState.step <= 1) return;
  await wizardSavePartial();   // save whatever we picked at this step before retreating
  wizardState.step -= 1;
  wizardRenderStep();
  if (wizardState.step === 3 && wizardState.school) wizardLoadMajors();
}

async function wizardNext() {
  // Intro CTA → enter Step 1 (no partial save — nothing picked yet)
  if (wizardState.step === 0) {
    wizardState.step = 1;
    wizardRenderStep();
    wizardLoadSchools();
    return;
  }
  if (wizardState.step === 4) {
    await wizardSave();   // final save + close + refresh
    return;
  }
  // Save partial before advancing so a future Skip / close doesn't lose this step
  const btn = document.getElementById('wizardNextBtn');
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Saving…';
  try {
    await wizardSavePartial();
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }

  if (wizardState.step === 1) {
    wizardState.step = 2;
    wizardRenderStep();
    wizardLoadYears();
  } else if (wizardState.step === 2) {
    wizardState.step = 3;
    wizardRenderStep();
    wizardLoadMajors();
  } else if (wizardState.step === 3) {
    wizardState.step = 4;
    wizardRenderStep();
    wizardLoadCourses();
  }
}

async function wizardSkip() {
  // Save whatever the user already picked before closing — that's
  // the whole point of this change. A skipped wizard with school+year
  // chosen still leaves school+year persisted in the profile.
  await wizardSavePartial();
  sessionStorage.setItem(WIZARD_SKIPPED_KEY, '1');
  closeWizard();
}

// ── Step 1: schools ──────────────────────────────────────
async function wizardLoadSchools() {
  if (wizardState._schools) {
    wizardRenderSchools(wizardState._schools);
    return;
  }
  try {
    const r = await fetch(`${API}/api/onboarding/schools`);
    const data = await r.json();
    wizardState._schools = data.schools || [];
    wizardRenderSchools(wizardState._schools);
  } catch (err) {
    document.getElementById('wizardSchoolGrid').innerHTML =
      `<div class="wizard-empty">Couldn't load schools — ${err}</div>`;
  }
}

function wizardRenderSchools(schools) {
  const grid = document.getElementById('wizardSchoolGrid');
  grid.innerHTML = '';
  schools.forEach((s, i) => {
    const card = document.createElement('div');
    card.className = 'wizard-card' + (wizardState.school?.slug === s.slug ? ' selected' : '');
    card.style.animationDelay = `${i * 30}ms`;
    card.innerHTML = `
      <div class="cat">School</div>
      <div class="code">${escHTML(s.name.replace(/^School of |^Donald Bren School of |^Henry Samueli School of |^Claire Trevor School of |^Paul Merage School of |^Susan & Henry Samueli College of /, ''))}</div>
      <div class="title">${escHTML(s.name)}</div>
      <div class="check">✓</div>
    `;
    card.onclick = () => {
      wizardState.school = s;
      wizardState._majorsBySchool[s.slug] = null;  // invalidate cache if reselected
      wizardRenderSchools(schools);                // refresh selected state
      wizardSyncNextEnabled();
    };
    grid.appendChild(card);
  });
}

// ── Step 2: year ─────────────────────────────────────────
function wizardLoadYears() {
  const grid = document.getElementById('wizardYearGrid');
  grid.innerHTML = '';
  WIZARD_YEARS.forEach((y, i) => {
    const card = document.createElement('div');
    card.className = 'wizard-card' + (wizardState.year === y.value ? ' selected' : '');
    card.style.animationDelay = `${i * 30}ms`;
    card.innerHTML = `
      <div class="cat">Year</div>
      <div class="code">${escHTML(y.value)}</div>
      <div class="title">${escHTML(y.desc)}</div>
      <div class="check">✓</div>
    `;
    card.onclick = () => {
      wizardState.year = y.value;
      wizardLoadYears();
      wizardSyncNextEnabled();
    };
    grid.appendChild(card);
  });
}

// ── Step 3: major ────────────────────────────────────────
async function wizardLoadMajors() {
  const grid = document.getElementById('wizardMajorGrid');
  const school = wizardState.school;
  if (!school) {
    grid.innerHTML = `<div class="wizard-empty">Pick a school first.</div>`;
    return;
  }
  if (wizardState._majorsBySchool[school.slug]) {
    wizardRenderMajors(wizardState._majorsBySchool[school.slug]);
    return;
  }
  grid.innerHTML = `<div class="wizard-loading">Loading majors for ${escHTML(school.name)}…</div>`;
  try {
    const r = await fetch(`${API}/api/onboarding/majors?school=${encodeURIComponent(school.slug)}&type=B.S.`);
    const data = await r.json();
    let majors = data.majors || [];
    // If B.S. filter yields nothing, retry without type to cover B.A. schools
    if (!majors.length) {
      const r2 = await fetch(`${API}/api/onboarding/majors?school=${encodeURIComponent(school.slug)}`);
      majors = (await r2.json()).majors || [];
    }
    wizardState._majorsBySchool[school.slug] = majors;
    wizardRenderMajors(majors);
  } catch (err) {
    grid.innerHTML = `<div class="wizard-empty">Couldn't load majors — ${err}</div>`;
  }
}

function wizardRenderMajors(majors) {
  const grid = document.getElementById('wizardMajorGrid');
  grid.innerHTML = '';
  if (!majors.length) {
    grid.innerHTML = `<div class="wizard-empty">No undergraduate majors found in this school's catalog.</div>`;
    return;
  }
  majors.forEach((m, i) => {
    const card = document.createElement('div');
    card.className = 'wizard-card' + (wizardState.major?.id === m.id ? ' selected' : '');
    card.style.animationDelay = `${i * 25}ms`;
    // Strip "Major in " prefix — UCI catalog uses it on every entry
    const display = m.name.replace(/^Major in\s+/i, '');
    card.innerHTML = `
      <div class="cat">${escHTML(m.type || '')}</div>
      <div class="code">${escHTML(display)}</div>
      <div class="title">${escHTML(m.id)}</div>
      <div class="check">✓</div>
    `;
    card.onclick = () => {
      wizardState.major = m;
      wizardRenderMajors(majors);
      wizardSyncNextEnabled();
    };
    grid.appendChild(card);
  });
}

// ── Step 4: full UCI catalog, single column + A-Z nav ────
//
// Backed by GET /api/onboarding/courses/all (~9000 rows, server-side
// cached + warmed at startup). Cards are grouped by the first letter
// of the course ID with a sticky A-Z navigation strip on the right.
// Search filters in place via a class toggle so 9000 cards × keystrokes
// stays smooth.

function _wizardNorm(s) {
  return (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

let _wizardLetterObserver = null;

async function wizardLoadCourses() {
  // Reset search on re-entry
  const search = document.getElementById('wizardCourseSearch');
  if (search) search.value = '';
  const empty = document.getElementById('wizardSearchEmpty');
  if (empty) empty.style.display = 'none';

  if (wizardState._allCourses && wizardState._deptNameMap) {
    wizardRenderCourses(wizardState._allCourses);
    return;
  }
  const grid = document.getElementById('wizardCourseGrid');
  try {
    // Parallel fetch — both endpoints are cheap once warm
    const [coursesRes, deptsRes] = await Promise.all([
      fetch(`${API}/api/onboarding/courses/all`),
      fetch(`${API}/api/onboarding/departments`),
    ]);
    const coursesData = await coursesRes.json();
    const deptsData = await deptsRes.json();
    wizardState._allCourses = coursesData.courses || [];
    wizardState._deptNameMap = {};
    for (const d of (deptsData.departments || [])) {
      wizardState._deptNameMap[d.deptCode] = d.deptName;
    }
    wizardRenderCourses(wizardState._allCourses);
  } catch (err) {
    grid.innerHTML = `<div class="wizard-empty">Couldn't load courses — ${err}</div>`;
  }
}

// Group courses by department (matches UCI's WebSoc index style). Each
// dept becomes a section with its own header showing CODE · Name (N
// courses). The right-side A-Z strip remains useful as a coarse
// navigator: click "C" → jump to the first dept whose code starts
// with C. Scrolling auto-highlights the letter of the topmost visible
// dept header.
function wizardRenderCourses(courses) {
  const grid = document.getElementById('wizardCourseGrid');
  const nav  = document.getElementById('wizardLetterNav');
  grid.innerHTML = '';
  nav.innerHTML = '';
  if (_wizardLetterObserver) { _wizardLetterObserver.disconnect(); _wizardLetterObserver = null; }

  if (!courses || !courses.length) {
    grid.innerHTML = `<div class="wizard-empty">No courses available.</div>`;
    return;
  }

  // Group by department code (already lower-cased in payload — preserve
  // original case for display). Within a dept, sort by courseNumeric
  // then by id so ICS 6B < ICS 31 < ICS 122A reads naturally.
  const groups = new Map();   // deptCode → courses[]
  for (const c of courses) {
    const dept = c.department || '???';
    if (!groups.has(dept)) groups.set(dept, []);
    groups.get(dept).push(c);
  }
  // Sort dept codes alphabetically (the UCI index way)
  const deptCodes = [...groups.keys()].sort((a, b) => a.localeCompare(b));
  for (const code of deptCodes) {
    groups.get(code).sort((a, b) => {
      const na = a.courseNumeric || 0, nb = b.courseNumeric || 0;
      if (na !== nb) return na - nb;
      return (a.id || '').localeCompare(b.id || '');
    });
  }

  // Letter → first-dept-header-element map, for A-Z nav click-jump
  const letterFirstHeader = new Map();
  const headerEls = [];

  // Build in a DocumentFragment so we only repaint once
  const frag = document.createDocumentFragment();
  const deptNameMap = wizardState._deptNameMap || {};
  let cardIndex = 0;

  for (const deptCode of deptCodes) {
    const items = groups.get(deptCode);
    const firstLetter = (deptCode[0] || '#').toUpperCase();
    const deptName = deptNameMap[deptCode] || '';

    const header = document.createElement('div');
    header.className = 'wizard-dept-header';
    header.setAttribute('data-letter-anchor', firstLetter);
    header.setAttribute('data-dept-anchor', deptCode);
    header.innerHTML = `
      <span class="dept-code">${escHTML(deptCode)}</span>
      ${deptName ? `<span class="dept-sep">·</span><span class="dept-name">${escHTML(deptName)}</span>` : ''}
      <span class="count">${items.length} course${items.length === 1 ? '' : 's'}</span>
    `;
    frag.appendChild(header);
    headerEls.push(header);
    if (!letterFirstHeader.has(firstLetter)) letterFirstHeader.set(firstLetter, header);

    for (const c of items) {
      const card = document.createElement('div');
      const sel = wizardState.completed_courses.has(c.id);
      card.className = 'wizard-card' + (sel ? ' selected' : '');
      card.style.animationDelay = `${Math.min(cardIndex++, 20) * 10}ms`;
      card.setAttribute('data-cid', c.id);
      card.setAttribute('data-dept', deptCode);
      const levelTag = (c.courseLevel || '').replace(/Division/g, '').trim();
      const units = (c.minUnits != null)
        ? `${c.minUnits}${c.maxUnits && c.maxUnits !== c.minUnits ? `–${c.maxUnits}` : ''} units`
        : '';
      card.innerHTML = `
        <div class="cat">${escHTML(levelTag || 'Course')}${units ? ' · ' + escHTML(units) : ''}</div>
        <div class="code">${escHTML(c.id)}</div>
        <div class="title">${escHTML(c.title || c.id)}</div>
        <div class="check">✓</div>
      `;
      card.onclick = () => {
        if (wizardState.completed_courses.has(c.id)) {
          wizardState.completed_courses.delete(c.id);
        } else {
          wizardState.completed_courses.add(c.id);
        }
        card.classList.toggle('selected');
        wizardRenderMatrix();
        wizardSyncFooterCount();
      };
      frag.appendChild(card);
    }
  }
  grid.appendChild(frag);

  // A-Z navigation strip — one button per letter that appears
  for (const letter of letterFirstHeader.keys()) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = letter;
    btn.setAttribute('data-letter-target', letter);
    btn.onclick = () => {
      const target = letterFirstHeader.get(letter);
      if (target) target.scrollIntoView({behavior: 'smooth', block: 'start'});
    };
    nav.appendChild(btn);
  }

  // Active-letter highlighting: when scrolling, mark the letter button
  // whose first-dept-header is the topmost visible one.
  const scrollRoot = document.getElementById('wizardCourseGridWrap');
  _wizardLetterObserver = new IntersectionObserver((entries) => {
    let topVisible = null;
    for (const e of entries) {
      if (e.isIntersecting) {
        if (!topVisible || e.boundingClientRect.top < topVisible.boundingClientRect.top) {
          topVisible = e;
        }
      }
    }
    if (!topVisible) return;
    const letter = topVisible.target.getAttribute('data-letter-anchor');
    document.querySelectorAll('#wizardLetterNav button.is-active')
      .forEach(el => el.classList.remove('is-active'));
    const btn = document.querySelector(
      `#wizardLetterNav button[data-letter-target="${CSS.escape(letter)}"]`);
    if (btn) btn.classList.add('is-active');
  }, {
    root: scrollRoot,
    rootMargin: '0px 0px -70% 0px',
    threshold: 0,
  });
  for (const h of headerEls) _wizardLetterObserver.observe(h);

  const firstBtn = nav.querySelector('button');
  if (firstBtn) firstBtn.classList.add('is-active');

  wizardRenderMatrix();
}

// Split a normalized course code into its letter-prefix and its
// number-part. The number-part starts at the first digit and includes
// any trailing letters (suffix like "A" in "MATH2A" or "W" in
// "WRITING39W"). Examples:
//   "icsci33"   → ["icsci", "33"]
//   "math2a"    → ["math",  "2a"]
//   "writing39b" → ["writing", "39b"]
//   "stats"     → ["stats", ""]      (no number)
//   "33"        → ["",      "33"]
function _wizardSplitCode(s) {
  const m = (s || '').match(/^([^0-9]*)(.*)$/);
  return m ? [m[1], m[2]] : [s, ''];
}

// Tight match — course ID only (NOT title or category). Three tiers,
// cheapest first; ordered so each tier only catches what the previous
// didn't and so BIOSCI is never matched by a query of "ics":
//   1. raw substring on the id (handles exact paste of a real id)
//   2. normalized substring on the id (handles I&CSCI ↔ ICS by
//      stripping & / space / punctuation)
//   3. split match — query's letter-prefix must be a substring of the
//      id's letter-prefix AND query's digit-prefix must start the
//      id's number-part. This catches "ICS33" → "I&CSCI33" where the
//      contiguous normalized form ("icsci33") does NOT contain the
//      contiguous query ("ics33") because of the extra "ci" between.
//      Tier 3 only activates when the query actually has BOTH letter
//      and digit parts so plain text queries (no digits) keep behaving
//      exactly like tier 2.
function _wizardMatchesQuery(card, qRaw, qNorm) {
  const cid = (card.getAttribute('data-cid') || '').toLowerCase();
  if (cid.includes(qRaw)) return true;
  const cidNorm = _wizardNorm(cid);
  if (cidNorm.includes(qNorm)) return true;

  const [qLetters, qDigits] = _wizardSplitCode(qNorm);
  // Tier 3 needs both halves — a pure-letter query would otherwise
  // match too broadly via the empty-digits short-circuit below.
  if (!qLetters || !qDigits) return false;
  const [cLetters, cDigits] = _wizardSplitCode(cidNorm);
  return cLetters.includes(qLetters) && cDigits.startsWith(qDigits);
}

function wizardFilterCourses() {
  const input = document.getElementById('wizardCourseSearch');
  const qRaw  = (input?.value || '').trim().toLowerCase();
  const qNorm = _wizardNorm(qRaw);
  const grid  = document.getElementById('wizardCourseGrid');
  const empty = document.getElementById('wizardSearchEmpty');

  if (!qRaw) {
    grid.querySelectorAll('.is-hidden').forEach(el => el.classList.remove('is-hidden'));
    document.querySelectorAll('#wizardLetterNav .is-hidden')
      .forEach(el => el.classList.remove('is-hidden'));
    if (empty) empty.style.display = 'none';
    return;
  }

  // Track which depts still have visible cards
  const visibleDepts = new Set();
  const visibleLetters = new Set();
  let matchCount = 0;
  grid.querySelectorAll('.wizard-card').forEach(card => {
    const hit = _wizardMatchesQuery(card, qRaw, qNorm);
    card.classList.toggle('is-hidden', !hit);
    if (hit) {
      matchCount++;
      visibleDepts.add(card.getAttribute('data-dept') || '');
      const cid = card.getAttribute('data-cid') || '';
      visibleLetters.add((cid[0] || '').toUpperCase());
    }
  });

  // Hide dept headers that have no visible children
  grid.querySelectorAll('.wizard-dept-header').forEach(h => {
    const dept = h.getAttribute('data-dept-anchor');
    h.classList.toggle('is-hidden', !visibleDepts.has(dept));
  });

  // Hide A-Z buttons whose letter has no visible dept
  const visibleDeptLetters = new Set();
  visibleDepts.forEach(d => visibleDeptLetters.add((d[0] || '').toUpperCase()));
  document.querySelectorAll('#wizardLetterNav button').forEach(b => {
    const letter = b.getAttribute('data-letter-target');
    b.classList.toggle('is-hidden', !visibleDeptLetters.has(letter));
  });

  if (empty) {
    empty.style.display = (matchCount === 0) ? '' : 'none';
    empty.textContent = `No courses match "${qRaw}".`;
  }
}

function wizardRenderMatrix() {
  const matrix = document.getElementById('wizardMatrix');
  const clear = document.getElementById('wizardClearAll');
  if (wizardState.completed_courses.size === 0) {
    matrix.innerHTML = '<span class="wizard-matrix-empty">No courses selected yet.</span>';
    clear.classList.remove('is-visible');
    return;
  }
  matrix.innerHTML = '';
  clear.classList.add('is-visible');
  Array.from(wizardState.completed_courses).forEach(id => {
    const chip = document.createElement('span');
    chip.className = 'wizard-chip';
    chip.innerHTML = `${escHTML(id)} <button title="Remove">×</button>`;
    chip.querySelector('button').onclick = () => {
      wizardState.completed_courses.delete(id);
      const card = document.querySelector(
        `#wizardCourseGrid .wizard-card[data-cid="${CSS.escape(id)}"]`);
      if (card) card.classList.remove('selected');
      wizardRenderMatrix();
      wizardSyncFooterCount();
    };
    matrix.appendChild(chip);
  });
}

function wizardClearCourses() {
  wizardState.completed_courses.clear();
  document.querySelectorAll('#wizardCourseGrid .wizard-card.selected')
    .forEach(el => el.classList.remove('selected'));
  wizardRenderMatrix();
  wizardSyncFooterCount();
}

// ── Save (step 4 done) ───────────────────────────────────
async function wizardSave() {
  const btn = document.getElementById('wizardNextBtn');
  btn.disabled = true;
  btn.textContent = 'Saving…';

  const payload = {
    major: wizardState.major?.name?.replace(/^Major in\s+/i, '') || null,
    year:  wizardState.year || null,
    college: wizardState.school?.name || null,
    school_slug: wizardState.school?.slug || null,
    program_id: wizardState.major?.id || null,
    completed_courses: Array.from(wizardState.completed_courses),
  };
  // Strip nulls so the merge endpoint doesn't get partial garbage
  Object.keys(payload).forEach(k => (payload[k] == null) && delete payload[k]);

  try {
    const r = await fetch(`${API}/api/memory/${USER_ID}/profile`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    closeWizard();
    // Re-paint sidebar with the new profile
    await loadSidebar(true);
  } catch (err) {
    console.error('wizard save failed:', err);
    alert(`Save failed: ${err.message || err}`);
    btn.disabled = false;
    btn.textContent = 'Save & Finish';
  }
}


/* ── Boot ──────────────────────────────────────────── */
(async function init() {
  // Auth gate FIRST so we don't load demo data for an authenticated
  // user, then have to re-fetch. bootCheckAuth resolves quickly:
  // either the cookie is valid (one GET /me) or we just show the
  // modal (no network call beyond /me itself).
  await bootCheckAuth();
  paintAuthChrome();
  await Promise.all([
    loadTermState(),
    loadSidebar(),
    loadSessionList(),
  ]);
  // Phase C — first-time wizard for authenticated users with empty profile.
  // Runs after the main UI is in place so the user can see the chat layout
  // behind the modal-style overlay if the wizard ever errors out.
  await maybeShowWizard();
})();
