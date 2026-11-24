/* Student Profile uses the existing memory, academic import, and editing flows.
 * Schedule drafts are intentionally separate from saved enrollment records. */
let profileLoadVersion = 0;
let profileCourses = [];
let profileSearch = '';
let profileSort = 'subject';
let profileDepartments = new Map();
let profileDepartmentsPromise = null;
let profileGpaAvailable = false;
let profileGpaVisible = false;
let profileGpaRequest = null;
const profileCollator = new Intl.Collator('en', {numeric: true, sensitivity: 'base'});

function openProfile() {
  closeUserPopover();
  if (scheduleOpen) toggleSchedule();
  setWorkspaceView('profile');
  document.getElementById('studentProfileTitle').focus({preventScroll: true});
  loadProfile();
}

async function profileGetJSON(path) {
  const response = await fetch(`${API}${path}`, {cache: 'no-store'});
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function loadProfile() {
  const version = ++profileLoadVersion;
  resetProfilePrivacy();
  const body = document.getElementById('profileBody');
  const edit = document.getElementById('profileEditButton');
  body.setAttribute('aria-busy', 'true');
  edit.disabled = true;
  body.innerHTML = '<div class="profile-empty-state" role="status">Loading your profile…</div>';
  const [memory, academic] = await Promise.allSettled([
    profileGetJSON('/api/memory/me'), profileGetJSON('/api/academic/profile'),
  ]);
  if (version !== profileLoadVersion) return;
  body.setAttribute('aria-busy', 'false');
  if (memory.status !== 'fulfilled') {
    body.innerHTML = `<div class="profile-empty-state" role="alert">
      <p>Your profile couldn’t be loaded. Please try again.</p>
      <button class="quiet-button" onclick="loadProfile()">Try again</button></div>`;
    return;
  }
  edit.disabled = false;
  renderProfile({...memory.value, academic: academic.status === 'fulfilled' ? academic.value : null});
  // Department names enrich the list, but an unavailable catalog never blocks it.
  if (!profileDepartmentsPromise) {
    profileDepartmentsPromise = profileGetJSON('/api/onboarding/departments').then(data => {
      for (const dept of data.departments || []) {
        profileDepartments.set(profileSubjectKey(dept.deptCode), dept);
      }
    }).catch(() => { profileDepartmentsPromise = null; });
  }
  profileDepartmentsPromise.then(() => {
    if (version === profileLoadVersion) renderProfileCourses();
  });
}

function profileSubjectKey(value) {
  const key = String(value || '').toUpperCase().replace(/[^A-Z]/g, '');
  return key === 'ICSCI' ? 'ICS' : key;
}

function profileCourseParts(course) {
  const id = String(typeof course === 'string' ? course : course.course_id || '').trim();
  const match = id.match(/^(.+?)\s*(\d.*)$/);
  const subject = match ? match[1].trim().toUpperCase() : id;
  const key = profileSubjectKey(subject);
  const dept = profileDepartments.get(key);
  const displaySubject = key === 'ICS' ? 'ICS' : dept?.deptCode || subject;
  return {
    key, subject: displaySubject, name: dept?.deptName || '',
    code: match ? `${displaySubject} ${match[2]}` : id,
    title: typeof course === 'string' ? '' : course.title || '',
  };
}

function profileNumber(value) {
  return value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value))
    ? Number(value).toLocaleString('en-US', {maximumFractionDigits: 2}) : '—';
}

function renderProfile(data) {
  const p = data.profile || {};
  const academic = data.academic || {};
  const manualCourses = Array.isArray(p.completed_courses) ? p.completed_courses : [];
  profileCourses = Array.isArray(academic.completed_courses) ? academic.completed_courses : manualCourses;
  profileGpaAvailable = academic.gpa_available === true;
  const subjects = new Set(profileCourses.map(course => profileCourseParts(course).key));
  const graduation = p.graduating_class || p.graduation_term || '';
  const accountId = String(data.user_id || currentAuthUser?.id || '');
  const standing = p.year || 'Not set';
  const undergraduate = /^(freshman|sophomore|junior|senior)$/i.test(p.year || '');
  const metadata = [p.year, graduation ? `Expected graduation ${graduation}` : 'Expected graduation not set'];
  const importDate = academic.last_import?.imported_at;
  const units = profileNumber(academic.units_completed);
  document.getElementById('profileBody').innerHTML = `
    <section class="profile-identity" aria-labelledby="profileMajor">
      <div class="profile-avatar" aria-hidden="true">${solonIcon('user')}</div>
      <div class="profile-identity-copy">
        <p class="profile-eyebrow">UC Irvine${undergraduate ? ' · Undergraduate' : ''}</p>
        <h2 id="profileMajor">${escHTML(p.major || 'Your student profile')}</h2>
        <div class="profile-identity-meta">${metadata.filter(Boolean).map(value => `<span>${escHTML(value)}</span>`).join('')}
          ${accountId ? `<span class="profile-account-id" title="Account ID: ${escAttr(accountId)}">ID ${escHTML(accountId)}</span>` : ''}</div>
      </div>
    </section>
    <section class="profile-stats" aria-label="Academic summary">
      <div class="profile-stat"><h3>GPA</h3>
        <div class="profile-stat-value profile-gpa-value"><span id="profileGpaValue" aria-live="polite"></span>
          <button class="profile-eye-button" id="profileGpaToggle" onclick="toggleProfileGpa()" aria-label="Show GPA" aria-pressed="false">${solonIcon('eye')}</button></div>
        <p id="profileGpaHint" aria-live="polite"></p>
      </div>
      <div class="profile-stat"><h3>Units completed</h3><div class="profile-stat-value" id="profileUnits">${units}</div>
        <p>${units === '—' ? 'Import a transcript to add units' : 'From your imported transcript'}</p></div>
      <div class="profile-stat"><h3>Courses completed</h3><div class="profile-stat-value" id="profileCourseCount">${profileCourses.length}</div>
        <p>across ${subjects.size} ${subjects.size === 1 ? 'subject' : 'subjects'}</p></div>
      <div class="profile-stat"><h3>Expected graduation</h3><div class="profile-stat-value profile-graduation">${escHTML(graduation || '—')}</div>
        <p>Class standing: ${escHTML(standing)}</p></div>
    </section>
    ${data.academic === null ? '<div class="profile-data-warning" role="status">Transcript data couldn’t be loaded. Showing your saved courses. <button onclick="loadProfile()">Retry</button></div>' : ''}
    <section class="profile-transcript" aria-label="Unofficial transcript">
      <span class="profile-file-icon" aria-hidden="true">${solonIcon('file')}</span>
      <div class="profile-transcript-copy"><h3>Unofficial transcript</h3>
        <p>${importDate ? `Last imported ${escHTML(_formatProfileDate(importDate))}` : 'Import your transcript to update your academic record'}</p></div>
      <button class="primary-button profile-import-button" type="button" data-transcript-button onclick="triggerTranscriptPicker('profile')" ${transcriptImportBusy ? 'disabled' : ''}>${importDate ? 'Import Updated Transcript…' : 'Import Transcript…'}</button>
    </section>
    <div class="transcript-import-status" id="profileTranscriptStatus" hidden aria-live="polite"></div>
    ${profileDetailsSection(academic, p)}
    <p class="profile-source-note">User-provided and not verified by UCI. Solon is not an official degree audit.</p>
    <section class="profile-completed" aria-labelledby="profileCompletedTitle">
      <div class="profile-courses-toolbar">
        <div><h2 id="profileCompletedTitle">Completed courses</h2><p id="profileCoursesSummary" aria-live="polite"></p></div>
        <div class="profile-course-controls">
          <label class="profile-course-search">${solonIcon('search')}<input id="profileCourseSearch" type="search" placeholder="Search courses" aria-label="Search completed courses" autocomplete="off"></label>
          <div class="profile-sort" role="group" aria-label="Course display">
            <button id="profileSortSubject" onclick="setProfileSort('subject')">By subject</button>
            <button id="profileSortAZ" onclick="setProfileSort('az')">A–Z</button>
          </div>
        </div>
      </div>
      <div id="profileCourseList"></div>
    </section>
    <section class="profile-quarter" aria-labelledby="profileQuarterTitle">
      <h2 id="profileQuarterTitle">This quarter</h2>
      <div class="profile-quarter-grid">
        ${profileEnrollmentCard('Enrolled', p.selected_courses, true)}
        ${profileEnrollmentCard('Waitlisted', p.waitlisted_courses, false)}
      </div>
    </section>
    <footer class="student-profile-danger">
      <div><h3>Delete account</h3><p>Remove your profile, academic data, and conversations.</p></div>
      <button class="profile-delete-button" onclick="openDeleteAccount()">Delete account</button>
    </footer>`;
  const input = document.getElementById('profileCourseSearch');
  input.value = profileSearch;
  input.addEventListener('input', () => { profileSearch = input.value; renderProfileCourses(); });
  resetProfilePrivacy();
  renderProfileCourses();
}

/* Details a transcript cannot show. The student states them here; Solon uses
   them for prerequisites, GE progress and enrollment restrictions. */
const PROFILE_GE_CATEGORIES = [
  ['IA', 'Ia Lower-division writing'], ['IB', 'Ib Upper-division writing'], ['II', 'II Science & technology'],
  ['III', 'III Social & behavioral'], ['IV', 'IV Arts & humanities'], ['VA', 'Va Quantitative literacy'],
  ['VB', 'Vb Formal reasoning'], ['VI', 'VI Language other than English'], ['VII', 'VII Multicultural studies'],
  ['VIII', 'VIII International/global'],
];
let profileAcademicDetails = null;

function profileExamCreditText(credit) {
  const courses = Array.isArray(credit.credited_courses) ? credit.credited_courses : [];
  const direct = Object.keys(credit.ge_direct || {});
  if (!credit.matched) return 'Not matched to the UCI chart; units only';
  const parts = [];
  if (courses.length) parts.push(`counts as ${courses.join(' / ')}`);
  if (direct.length) parts.push(`GE ${direct.join(', ')}`);
  return parts.length ? parts.join('; ') : (credit.note || 'elective units only');
}

function profileDetailsSection(academic, profile) {
  const details = academic.details || {};
  profileAcademicDetails = {...details, target_major: academic.target_major || profile.major || ''};
  const exams = Array.isArray(academic.exam_credits) ? academic.exam_credits : [];
  const transfers = Array.isArray(academic.transfer_credits) ? academic.transfer_credits : [];
  const list = values => (Array.isArray(values) && values.length ? values.map(escHTML).join(', ') : '—');
  const majorSource = details.official_major_source === 'transcript'
    ? `from your transcript${details.official_major_as_of ? ` printed ${escHTML(_formatProfileDate(details.official_major_as_of))}` : ''}`
    : details.official_major_source === 'student' ? 'entered by you' : 'not set';
  return `<section class="profile-details" aria-labelledby="profileDetailsTitle">
      <div class="profile-details-head"><div><h2 id="profileDetailsTitle">Academic details</h2>
        <p>Used for prerequisites, GE progress and enrollment restrictions. A transcript does not show these.</p></div>
        <button class="quiet-button" type="button" onclick="openProfileDetailsEditor()">Edit details</button></div>
      <dl class="profile-details-list">
        <div><dt>Major on record</dt><dd>${escHTML(details.official_major || '—')}<span>${majorSource}; decides which restricted courses you can enroll in</span></dd></div>
        <div><dt>Target major</dt><dd>${escHTML(profileAcademicDetails.target_major || '—')}<span>decides your degree path</span></dd></div>
        <div><dt>In progress${details.in_progress_term ? ` · ${escHTML(displayTerm(details.in_progress_term))}` : ''}</dt><dd>${list(details.in_progress_courses)}<span>count as met prerequisites and are not recommended again</span></dd></div>
        <div><dt>GE marked complete</dt><dd>${list(details.ge_confirmed)}<span>for categories met outside coursework, such as language</span></dd></div>
        <div><dt>Exam credit</dt><dd>${exams.length ? exams.map(credit => `${escHTML(credit.exam_type)} ${escHTML(credit.subject)} (${escHTML(credit.score)}): ${escHTML(profileExamCreditText(credit))}`).join('<br>') : '—'}<span>from the UC Irvine AP credit chart</span></dd></div>
        <div><dt>Transfer credit</dt><dd>${transfers.length ? transfers.map(row => `${escHTML(row.institution_name || 'Transfer')} · ${escHTML(row.units)} units`).join('<br>') : '—'}<span>counts as: ${list(details.transfer_courses)}</span></dd></div>
      </dl>
      <form class="profile-details-form" id="profileDetailsForm" hidden onsubmit="saveProfileDetails(event)"></form>
    </section>`;
}

function openProfileDetailsEditor() {
  const form = document.getElementById('profileDetailsForm');
  const details = profileAcademicDetails || {};
  const confirmed = new Set(details.ge_confirmed || []);
  form.innerHTML = `
    <label>Major on record<input name="official_major" maxlength="120" value="${escAttr(details.official_major || '')}" placeholder="As shown in StudentAccess, e.g. Computer Science and Engineering"></label>
    <label>In-progress term<input name="in_progress_term" maxlength="24" value="${escAttr(details.in_progress_term || '')}" placeholder="2026 Fall"></label>
    <label>Courses in progress<input name="in_progress_courses" value="${escAttr((details.in_progress_courses || []).join(', '))}" placeholder="SWE 43, UNI STU 10"></label>
    <label>Transfer credit counts as<input name="transfer_courses" value="${escAttr((details.transfer_courses || []).join(', '))}" placeholder="UCI course IDs, comma separated"></label>
    <fieldset><legend>GE categories already complete</legend>
      ${PROFILE_GE_CATEGORIES.map(([code, label]) => `<label class="profile-details-check"><input type="checkbox" name="ge_confirmed" value="${code}" ${confirmed.has(code) ? 'checked' : ''}>${escHTML(label)}</label>`).join('')}
    </fieldset>
    <p class="profile-details-status" id="profileDetailsStatus" aria-live="polite"></p>
    <div class="profile-details-actions"><button class="primary-button" type="submit">Save details</button>
      <button class="quiet-button" type="button" onclick="document.getElementById('profileDetailsForm').hidden = true">Cancel</button></div>`;
  form.hidden = false;
  form.querySelector('input').focus();
}

async function saveProfileDetails(event) {
  event.preventDefault();
  const form = event.target;
  const status = document.getElementById('profileDetailsStatus');
  const split = value => String(value || '').split(/[,;\n]/).map(item => item.trim()).filter(Boolean);
  const payload = {
    official_major: form.official_major.value.trim(),
    in_progress_term: form.in_progress_term.value.trim(),
    in_progress_courses: split(form.in_progress_courses.value),
    transfer_courses: split(form.transfer_courses.value),
    ge_confirmed: [...form.querySelectorAll('input[name="ge_confirmed"]:checked')].map(input => input.value),
  };
  status.textContent = 'Saving…';
  try {
    const response = await fetch(`${API}/api/academic/details`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const rejected = (await response.json()).details?.rejected || [];
    if (rejected.length) {
      status.textContent = `Saved. Not recognized and left out: ${rejected.join(', ')}`;
      setTimeout(loadProfile, 2500);
    } else {
      loadProfile();
    }
  } catch (error) {
    status.textContent = 'Your details could not be saved. Please try again.';
  }
}

function profileEnrollmentCard(label, courses, canPlan) {
  const list = Array.isArray(courses) ? courses : [];
  const term = displayTerm(currentTermContext?.term || '');
  return `<div class="profile-enrollment-card"><h3>${label}<span>${list.length}</span></h3>
    ${list.length ? `<ul>${list.map(course => `<li>${escHTML(profileCourseParts(course).code)}</li>`).join('')}</ul><p class="profile-small-note">From your saved profile</p>` : `<p>No ${label.toLowerCase()} courses.</p>`}
    ${canPlan ? `<button class="profile-plan-link" onclick="planFromProfile()">Plan ${escHTML(term || 'your quarter')} with Solon <span aria-hidden="true">›</span></button>` : '<p class="profile-small-note">Waitlisted courses you share with Solon appear here.</p>'}</div>`;
}

function planFromProfile() {
  const term = displayTerm(currentTermContext?.term || '');
  showAsk();
  // Keep the prompt reviewable and don't interrupt an in-flight conversation.
  const input = document.getElementById('userInput');
  if (!input.value.trim()) input.value = `Help me plan ${term || 'my quarter'}`;
  syncComposer();
  input.focus();
}

function setProfileSort(sort) {
  profileSort = sort === 'az' ? 'az' : 'subject';
  renderProfileCourses();
}

function renderProfileCourses() {
  const container = document.getElementById('profileCourseList');
  if (!container) return;
  const query = profileSearch.trim().toLowerCase();
  const compact = query.replace(/\s/g, '');
  const allCourses = profileCourses.map(profileCourseParts).sort((a, b) => profileCollator.compare(a.code, b.code));
  const courses = allCourses.filter(course => !query || course.code.toLowerCase().replace(/\s/g, '').includes(compact) || course.title.toLowerCase().includes(query) || course.name.toLowerCase().includes(query));
  const subjects = new Set(allCourses.map(course => course.key));
  document.getElementById('profileCoursesSummary').textContent = query
    ? `${courses.length} of ${allCourses.length} courses`
    : `${allCourses.length} ${allCourses.length === 1 ? 'course' : 'courses'} · ${subjects.size} ${subjects.size === 1 ? 'subject' : 'subjects'}`;
  document.getElementById('profileSortSubject').setAttribute('aria-pressed', String(profileSort === 'subject'));
  document.getElementById('profileSortAZ').setAttribute('aria-pressed', String(profileSort === 'az'));
  if (!courses.length) {
    container.className = 'profile-empty-state';
    container.innerHTML = query ? '<p>No courses match your search.</p>' : '<p>No completed courses yet.</p><p class="profile-small-note">Import a transcript or use Edit to add courses you’ve completed.</p>';
    return;
  }
  const row = course => `<li class="profile-course-row"><span class="profile-course-code">${escHTML(course.code)}</span><span class="profile-course-title">${escHTML(course.title || 'Title unavailable')}</span><span class="profile-course-check" role="img" aria-label="Completed">${solonIcon('check')}</span></li>`;
  container.className = profileSort === 'subject' ? 'profile-course-columns' : 'profile-course-alphabetical';
  if (profileSort === 'az') {
    container.innerHTML = `<ul class="profile-course-flat">${courses.map(row).join('')}</ul>`;
    return;
  }
  const groups = new Map();
  for (const course of courses) {
    if (!groups.has(course.key)) groups.set(course.key, []);
    groups.get(course.key).push(course);
  }
  container.innerHTML = [...groups.values()].map(group => `<section class="profile-subject">
    <h3><span>${escHTML(group[0].subject)}${group[0].name ? ` · ${escHTML(group[0].name)}` : ''}</span><span class="profile-subject-count">${group.length}</span></h3>
    <ul>${group.map(row).join('')}</ul></section>`).join('');
}

function resetProfilePrivacy() {
  profileGpaRequest?.abort();
  profileGpaRequest = null;
  profileGpaVisible = false;
  updateProfileGpa();
}

function updateProfileGpa(value = null, error = '') {
  const label = document.getElementById('profileGpaValue');
  const toggle = document.getElementById('profileGpaToggle');
  const hint = document.getElementById('profileGpaHint');
  if (!label || !toggle || !hint) return;
  label.textContent = profileGpaVisible ? Number(value).toFixed(2) : profileGpaAvailable ? '••••' : '—';
  label.setAttribute('aria-label', profileGpaVisible ? `GPA ${label.textContent}` : profileGpaAvailable ? 'GPA hidden' : 'GPA not available');
  label.classList.toggle('is-hidden', profileGpaAvailable && !profileGpaVisible);
  toggle.hidden = !profileGpaAvailable;
  toggle.disabled = !!profileGpaRequest;
  toggle.setAttribute('aria-pressed', String(profileGpaVisible));
  toggle.setAttribute('aria-label', profileGpaVisible ? 'Hide GPA' : 'Show GPA');
  hint.textContent = error || (profileGpaRequest ? 'Loading GPA…' : profileGpaVisible ? 'Official UC GPA · click to hide' : profileGpaAvailable ? 'Hidden · click to show' : 'No GPA imported');
}

async function toggleProfileGpa() {
  if (profileGpaVisible) { resetProfilePrivacy(); return; }
  if (!profileGpaAvailable || profileGpaRequest) return;
  const request = new AbortController();
  profileGpaRequest = request;
  updateProfileGpa();
  try {
    const response = await fetch(`${API}/api/academic/profile?include_gpa=true`, {signal: request.signal, cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (request !== profileGpaRequest || !document.body.classList.contains('profile-open')) return;
    profileGpaRequest = null;
    const value = data.official_uc_gpa;
    profileGpaVisible = value !== null && value !== undefined && Number.isFinite(Number(value));
    profileGpaAvailable = profileGpaVisible;
    updateProfileGpa(value);
  } catch (error) {
    if (request !== profileGpaRequest) return;
    profileGpaRequest = null;
    updateProfileGpa(null, 'Couldn’t load GPA · try again');
  }
}

function _formatProfileDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value || '');
  return date.toLocaleString('en-US', {
    year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  });
}
