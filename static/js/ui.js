/* Shared presentation helpers. All course and session data stays in the existing API flow. */
const SOLON_ICONS = {
  sparkles: 'M12 3l2.2 6.8L21 12l-6.8 2.2L12 21l-2.2-6.8L3 12l6.8-2.2Z',
  calendar: 'M4 5h16v16H4zM4 10h16M8 3v4M16 3v4',
  compose: 'm14 5 5 5M4 20l5-1L21 7a2 2 0 0 0-5-5L4 14v6ZM12 21h9',
  panel: 'M4 5h16v14H4zM14 5v14',
  sidebar: 'M4 5h16v14H4zM10 5v14',
  'arrow-up': 'M12 19V5m-6 6 6-6 6 6',
  close: 'm6 6 12 12M6 18 18 6',
  refresh: 'M20 7v5h-5M4 17v-5h5M5.5 8a7 7 0 0 1 12-3L20 8M4 16l2.5 3a7 7 0 0 0 12-3',
  chevron: 'm9 5 7 7-7 7',
  check: 'm5 12 4 4L19 6',
  user: 'M5 21v-2a7 7 0 0 1 14 0v2M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z',
  eye: 'M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12ZM15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z',
  file: 'M6 3h9l4 4v14H6zM14 3v5h5M9 12h7M9 15h7M9 18h4',
  search: 'M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0Zm-2 5 6 6',
  add: 'M12 5v14M5 12h14',
  schedule: 'M12 8v4l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
  location_on: 'M19 10c0 5-7 11-7 11S5 15 5 10a7 7 0 1 1 14 0ZM15 10a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z',
  star: 'm12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9Z',
  event: 'M4 5h16v16H4zM4 10h16M8 3v4M16 3v4M8 14h3v3H8z',
  event_busy: 'M4 5h16v16H4zM4 10h16M8 3v4M16 3v4m-7 7 6 6m-6 0 6-6',
  check_circle: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM8 12l3 3 5-6',
  help: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM9 9a3 3 0 0 1 6 0c0 2-3 2-3 4M12 17h.01',
  lock: 'M5 10h14v11H5zM8 10V7a4 4 0 1 1 8 0v3M12 14v3',
  expand_more: 'm6 9 6 6 6-6',
  expand_less: 'm6 15 6-6 6 6',
};

function solonIcon(name) {
  return `<svg class="solon-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${SOLON_ICONS[name] || SOLON_ICONS.sparkles}"></path></svg>`;
}

document.querySelectorAll('[data-icon]').forEach(el => { el.innerHTML = solonIcon(el.dataset.icon); });

function setThreadTitle(title) {
  const el = document.getElementById('threadTitle');
  el.textContent = title || 'New conversation';
  el.title = el.textContent;
}

const WELCOME_GREETINGS = [
  'What should we plan?',
  'What’s next for your quarter?',
  'What would you like to explore?',
  'How can I help you plan?',
  'Where should we start?',
];
let lastWelcomeGreeting = '';
let welcomeTransition = null;
const welcomeReducedMotion = matchMedia('(prefers-reduced-motion: reduce)');

function randomizeWelcomeGreeting() {
  const choices = WELCOME_GREETINGS.filter(text => text !== lastWelcomeGreeting);
  lastWelcomeGreeting = choices[Math.floor(Math.random() * choices.length)];
  document.getElementById('welcomeGreeting').textContent = lastWelcomeGreeting;
}

function cancelWelcomeTransition() {
  const transition = welcomeTransition;
  welcomeTransition = null;
  if (!transition) return;
  for (const animation of transition.animations) animation.cancel();
  for (const ghost of transition.ghosts) ghost.remove();
  document.getElementById('chatPanel').classList.remove('is-launching');
}

function setWelcomePresentation(welcome) {
  cancelWelcomeTransition();
  document.getElementById('chatPanel').classList.toggle('is-welcome', welcome);
  document.getElementById('chatScroll').classList.toggle('is-empty', welcome);
  if (welcome) randomizeWelcomeGreeting();
}

function beginWelcomeSend() {
  const panel = document.getElementById('chatPanel');
  if (!panel.classList.contains('is-welcome')) return null;
  const origin = panel.querySelector('.input-row').getBoundingClientRect();
  const canAnimate = !welcomeReducedMotion.matches && origin.width > 0 && !panel.hidden;
  const panelRect = panel.getBoundingClientRect();
  const ghosts = [];
  // Copies only the decorative welcome content, never the live input or its
  // value. The original composer keeps its focus, listeners and term badge.
  if (canAnimate) {
    for (const id of ['welcomeState', 'welcomeFollowups']) {
      const source = document.getElementById(id);
      const rect = source.getBoundingClientRect();
      const ghost = source.cloneNode(true);
      for (const element of [ghost, ...ghost.querySelectorAll('*')]) {
        element.removeAttribute('id');
        element.removeAttribute('onclick');
      }
      ghost.classList.add('welcome-exit');
      ghost.setAttribute('aria-hidden', 'true');
      ghost.inert = true;
      Object.assign(ghost.style, {
        left: `${rect.left - panelRect.left}px`, top: `${rect.top - panelRect.top}px`,
        width: `${rect.width}px`, height: `${rect.height}px`,
      });
      ghosts.push(ghost);
    }
  }
  setWelcomePresentation(false);
  if (!canAnimate) return null;
  const transition = {origin, ghosts, animations: []};
  welcomeTransition = transition;
  panel.classList.add('is-launching');
  for (const ghost of ghosts) {
    panel.appendChild(ghost);
    const animation = ghost.animate([
      {opacity: 1, transform: 'translateY(0) scale(1)'},
      {opacity: 0, transform: 'translateY(-12px) scale(.98)'},
    ], {duration: 200, easing: 'cubic-bezier(.2,.7,.2,1)', fill: 'forwards'});
    transition.animations.push(animation);
  }
  return transition;
}

function animateWelcomeSend(transition, userMessage, aiMessage) {
  if (!transition || transition !== welcomeTransition) return;
  const row = document.querySelector('#chatPanel .input-row');
  const destination = row.getBoundingClientRect();
  const {origin} = transition;
  const timing = {duration: 680, easing: 'cubic-bezier(.22,1,.36,1)'};
  const composerMotion = row.animate([
    {transform: `translate(${origin.left - destination.left}px, ${origin.top - destination.top}px)`},
    {transform: 'translate(0, 0)'},
  ], timing);
  transition.animations.push(composerMotion);
  userMessage.classList.add('is-first-message');
  const bubble = userMessage.querySelector('.bubble-me');
  bubble.classList.remove('send');
  const target = bubble.getBoundingClientRect();
  const dx = origin.right - 52 - target.right;
  const dy = origin.top + origin.height / 2 - target.top - target.height / 2;
  const messageMotion = bubble.animate([
    {opacity: 0, transform: `translate(${dx}px, ${dy}px) scale(.96)`},
    {opacity: 1, transform: 'translate(0, 0) scale(1)'},
  ], timing);
  transition.animations.push(messageMotion);
  aiMessage.classList.add('is-first-reply');
  composerMotion.finished.then(() => {
    if (welcomeTransition === transition) cancelWelcomeTransition();
  }).catch(() => {});
}

randomizeWelcomeGreeting();
window.addEventListener('resize', cancelWelcomeTransition);
welcomeReducedMotion.addEventListener('change', cancelWelcomeTransition);

function syncComposer() {
  const input = document.getElementById('userInput');
  document.getElementById('sendBtn').disabled = !input.value.trim() || !!currentAbortController;
}

document.getElementById('userInput').addEventListener('input', syncComposer);

function setComposerSuggestions(suggestions = []) {
  const container = document.getElementById('composerSuggestions');
  container.replaceChildren();
  for (const prompt of suggestions) {
    const btn = document.createElement('button');
    btn.className = 'followup-chip';
    btn.innerHTML = solonIcon('sparkles');
    const label = document.createElement('span');
    label.textContent = prompt;
    btn.appendChild(label);
    btn.title = label.textContent;
    btn.addEventListener('click', () => sendFollowup(prompt));
    container.appendChild(btn);
  }
}

function toggleMobileSidebar(force) {
  const open = force ?? !document.body.classList.contains('sidebar-open');
  document.body.classList.toggle('sidebar-open', open);
  document.querySelectorAll('.mobile-sidebar-toggle').forEach(button => {
    button.setAttribute('aria-expanded', String(open));
  });
}

function setWorkspaceView(view) {
  cancelWelcomeTransition();
  const isProfile = view === 'profile';
  if (!isProfile && document.body.classList.contains('profile-open')) resetProfilePrivacy();
  document.body.classList.toggle('profile-open', isProfile);
  document.getElementById('chatPanel').hidden = isProfile;
  document.getElementById('studentProfilePanel').hidden = !isProfile;
  for (const [id, active] of [['askNav', !isProfile], ['profileNav', isProfile]]) {
    const button = document.getElementById(id);
    button.classList.toggle('is-active', active);
    if (active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }
  toggleMobileSidebar(false);
}

function showAsk() {
  setWorkspaceView('ask');
  if (scheduleOpen) toggleSchedule();
  toggleMobileSidebar(false);
  document.getElementById('userInput').focus();
}

document.addEventListener('keydown', event => {
  if (event.key === 'Escape') toggleMobileSidebar(false);
});

function displayTerm(term) {
  return String(term || '').replace(/^(\d{4})\s+(Spring|Summer[12]?|Fall|Winter)$/i, '$2 $1');
}

function canonicalTerm(term) {
  return String(term || '').replace(/^(Spring|Summer[12]?|Fall|Winter)\s+(\d{4})$/i, '$2 $1');
}

function displayTime(value) {
  const minutes = parseTimeToMinutes(value);
  if (minutes < 0) return value || 'TBA';
  const hour = Math.floor(minutes / 60);
  return `${hour % 12 || 12}${minutes % 60 ? ':' + String(minutes % 60).padStart(2, '0') : ''} ${hour >= 12 ? 'PM' : 'AM'}`;
}

function notifySchedule(message) {
  dismissScheduleToast();
  const region = document.getElementById('scheduleToastRegion');
  const toast = document.createElement('div');
  toast.className = 'schedule-toast';
  toast.textContent = message;
  region.appendChild(toast);
  scheduleToastTimer = setTimeout(dismissScheduleToast, 5000);
}
