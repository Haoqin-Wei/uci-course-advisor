/* ── Account page handlers ─────────────────────────────
   Boot flow:
     1. Try /api/auth/me. 200 → set currentAuthUser, enter the workspace.
     2. 401 → show #authModal. Registration accepts @uci.edu emails
        without email verification; there is no guest entry point.
   After a successful login/register, we hide the account page and refresh
   the sidebar/sessions so the UI reflects the new user. */

const authRequests = {login: false, register: false};

function setAuthScreenVisible(visible) {
  const screen = document.getElementById('authModal');
  screen.hidden = !visible;
  screen.classList.toggle('open', visible);
  document.body.classList.remove('auth-pending');
  document.body.classList.toggle('auth-active', visible);
  const workspace = document.querySelector('.app-body');
  workspace.inert = visible;
  if (visible) {
    workspace.setAttribute('aria-hidden', 'true');
    document.getElementById('authModalTitle').focus({preventScroll: true});
  } else {
    workspace.removeAttribute('aria-hidden');
  }
}

function syncAuthControls() {
  const busy = authRequests.login || authRequests.register;
  const value = id => document.getElementById(id).value;
  const login = document.getElementById('loginSubmit');
  const register = document.getElementById('regSubmit');
  login.disabled = busy || !value('loginEmail').trim() || !value('loginPassword');
  register.disabled = busy || !value('regEmail').trim() || !value('regPassword')
    || !value('regPasswordConfirm') || !document.getElementById('regConsent').checked;
  login.textContent = authRequests.login ? 'Signing in…' : 'Sign in';
  register.textContent = authRequests.register ? 'Creating account…' : 'Create account';
  for (const name of ['Login', 'Register']) {
    document.getElementById(`authTab${name}`).disabled = busy;
    document.getElementById(`authPane${name}`).setAttribute('aria-busy', String(authRequests[name.toLowerCase()]));
  }
}

function authResponseError(data, fallback) {
  return typeof data.detail === 'string' ? data.detail : fallback;
}

async function bootCheckAuth() {
  try {
    const r = await fetch(`${API}/api/auth/me`);
    if (r.ok) {
      const data = await r.json();
      currentAuthUser = data.user;
      USER_ID = currentAuthUser.id;
      setAuthScreenVisible(false);
      return true;  // logged in
    }
  } catch (err) {
    console.warn('auth check failed (network?):', err);
  }
  // 401 or network error → show the account page.
  setAuthScreenVisible(true);
  return false;
}

function switchAuthTab(tab) {
  if (authRequests.login || authRequests.register) return;
  for (const t of ['login', 'register']) {
    const button = document.getElementById(`authTab${t === 'login' ? 'Login' : 'Register'}`);
    button.classList.toggle('is-active', t === tab);
    button.setAttribute('aria-selected', String(t === tab));
    button.tabIndex = t === tab ? 0 : -1;
    document.getElementById(`authPane${t === 'login' ? 'Login' : 'Register'}`)
      .classList.toggle('is-active', t === tab);
  }
  const register = tab === 'register';
  document.getElementById('authModal').classList.toggle('is-register', register);
  document.getElementById('authModalTitle').textContent = register ? 'Create your Solon account' : 'Sign in to Solon';
  document.getElementById('authSubtitle').textContent = register ? 'A clearer plan starts here.' : 'Pick up where your plan left off.';
  setAuthError('');  // clear any prior error when switching tabs
  syncAuthControls();
}

function setAuthError(msg) {
  const el = document.getElementById('authError');
  if (!msg) {
    el.classList.remove('is-visible');
    el.textContent = '';
    return;
  }
  el.textContent = msg;
  el.classList.add('is-visible');
}

async function doLogin() {
  if (authRequests.login || authRequests.register) return;
  setAuthError('');
  const email = document.getElementById('loginEmail').value.trim();
  const password = document.getElementById('loginPassword').value;
  if (!email || !password) {
    setAuthError('Enter your email and password.');
    return;
  }
  authRequests.login = true;
  syncAuthControls();
  try {
    const r = await fetch(`${API}/api/auth/login`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email, password}),
    });
    if (!r.ok) {
      const detail = authResponseError(await r.json().catch(() => ({})), 'Unable to sign in. Please try again.');
      setAuthError(detail);
      return;
    }
    document.getElementById('loginPassword').value = '';
    await onAuthSuccess(await r.json());
  } catch (err) {
    setAuthError('Unable to connect. Please try again.');
  } finally {
    authRequests.login = false;
    syncAuthControls();
  }
}

const TERMS_VERSION = '2026-09-21';
const PRIVACY_VERSION = '2026-09-21';

function isUciEmail(value) {
  return /^[^@\s]+@uci\.edu$/i.test(String(value || '').trim());
}

async function doRegister() {
  if (authRequests.login || authRequests.register) return;
  setAuthError('');
  const email = document.getElementById('regEmail').value.trim();
  const password = document.getElementById('regPassword').value;
  const passwordConfirmation = document.getElementById('regPasswordConfirm').value;
  const accepted = document.getElementById('regConsent').checked;
  if (!isUciEmail(email)) {
    setAuthError('Use your @uci.edu email address.');
    return;
  }
  if (!password || !passwordConfirmation) {
    setAuthError('Enter and confirm your password.');
    return;
  }
  if ([...password].length < 8) {
    setAuthError('Your password must have at least 8 characters.');
    return;
  }
  if (new TextEncoder().encode(password).length > 72) {
    setAuthError('Your password is too long. Please use a shorter one.');
    return;
  }
  if (password !== passwordConfirmation) {
    setAuthError('Your passwords don’t match. Please try again.');
    return;
  }
  if (!accepted) {
    setAuthError('Confirm you’re 18 or older and accept the Terms of Use and Privacy Notice.');
    return;
  }
  authRequests.register = true;
  syncAuthControls();
  try {
    const r = await fetch(`${API}/api/auth/register`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        email,
        password,
        password_confirmation: passwordConfirmation,
        age_18_confirmed: true,
        terms_accepted: true,
        terms_version: TERMS_VERSION,
        privacy_version: PRIVACY_VERSION,
      }),
    });
    if (!r.ok) {
      const detail = authResponseError(await r.json().catch(() => ({})), 'Unable to create your account. Please try again.');
      setAuthError(detail);
      return;
    }
    document.getElementById('regPassword').value = '';
    document.getElementById('regPasswordConfirm').value = '';
    await onAuthSuccess(await r.json());
  } catch (err) {
    setAuthError('Unable to connect. Please try again.');
  } finally {
    authRequests.register = false;
    syncAuthControls();
  }
}

async function onAuthSuccess(authBody) {
  // Server set the session cookie via Set-Cookie; we just refresh state.
  currentAuthUser = {id: authBody.user_id, email: authBody.email};
  USER_ID = authBody.user_id;
  setAuthScreenVisible(false);
  paintAuthChrome();
  // Reset any session UI that was loaded under demo_001 — the new user
  // starts with a clean slate. We re-run the boot data-fetches.
  currentSessionId = null;
  await Promise.all([loadSidebar(), loadSessionList()]);
  // Phase C — newly-registered (or first-time-logged-in) user. If their
  // profile is still empty, fire the onboarding wizard. maybeShowWizard
  // is idempotent (sessionStorage skip-flag + the empty-profile check),
  // so re-calling it later in the same tab is safe.
  await maybeShowWizard();
  if (!document.getElementById('wizardOverlay').classList.contains('open')) {
    document.getElementById('userInput').focus({preventScroll: true});
  }
}

// Sync the popover email line + show/hide the Sign-out item based on
// whether currentAuthUser is populated. Idempotent — safe to call any
// time auth state changes.
function paintAuthChrome() {
  const popEmail = document.getElementById('userPopoverEmail');
  const logoutSep = document.getElementById('logoutSep');
  const logoutItem = document.getElementById('logoutItem');
  if (currentAuthUser && currentAuthUser.email) {
    popEmail.textContent = currentAuthUser.email;
    logoutSep.style.display = '';
    logoutItem.style.display = '';
  } else {
    popEmail.textContent = 'Sign in required';
    logoutSep.style.display = 'none';
    logoutItem.style.display = 'none';
  }
}

async function doLogout() {
  closeUserPopover();
  try {
    await fetch(`${API}/api/auth/logout`, {method: 'POST'});
  } catch (err) {
    console.warn('logout request failed:', err);
  }
  // Hard reload — easiest way to flush all in-memory state (current
  // session, schedule, message history) so the next user doesn't see
  // anything from the previous session.
  window.location.reload();
}

function openDeleteAccount() {
  resetProfilePrivacy();
  document.getElementById('deleteAccountError').classList.remove('is-visible');
  document.getElementById('deleteAccountError').textContent = '';
  document.getElementById('deleteAccountPassword').value = '';
  document.getElementById('deleteAccountModal').classList.add('open');
  setTimeout(() => document.getElementById('deleteAccountPassword').focus(), 50);
}

function closeDeleteAccount(event) {
  if (event && event.type === 'click' && event.target.id !== 'deleteAccountModal') return;
  document.getElementById('deleteAccountModal').classList.remove('open');
}

async function deleteAccount() {
  const password = document.getElementById('deleteAccountPassword').value;
  const error = document.getElementById('deleteAccountError');
  if (!password) {
    error.textContent = 'Enter your password to continue.';
    error.classList.add('is-visible');
    return;
  }
  if (!window.confirm('Permanently delete your Solon account and all primary data?')) return;
  const button = document.getElementById('deleteAccountSubmit');
  button.disabled = true;
  button.textContent = 'Deleting…';
  try {
    const response = await fetch(`${API}/api/auth/account`, {
      method: 'DELETE',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password}),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || `Delete failed (${response.status})`);
    }
    window.location.reload();
  } catch (err) {
    error.textContent = err.message || String(err);
    error.classList.add('is-visible');
  } finally {
    button.disabled = false;
    button.textContent = 'Delete account';
  }
}

window.openDeleteAccount = openDeleteAccount;
window.closeDeleteAccount = closeDeleteAccount;
window.deleteAccount = deleteAccount;

// Both forms support native Enter submission and password-manager autofill.
for (const name of ['Login', 'Register']) {
  const form = document.getElementById(`authPane${name}`);
  form.addEventListener('input', syncAuthControls);
  form.addEventListener('change', syncAuthControls);
  form.addEventListener('focusin', syncAuthControls);
}
window.addEventListener('pageshow', syncAuthControls);
document.querySelector('.auth-tabs').addEventListener('keydown', event => {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  if (authRequests.login || authRequests.register) return;
  event.preventDefault();
  const current = document.getElementById('authTabRegister').getAttribute('aria-selected') === 'true';
  const register = event.key === 'End' || (event.key !== 'Home' && !current);
  switchAuthTab(register ? 'register' : 'login');
  document.getElementById(register ? 'authTabRegister' : 'authTabLogin').focus();
});
syncAuthControls();
