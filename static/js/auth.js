/* ── Auth modal handlers ───────────────────────────────
   Boot flow:
     1. Try /api/auth/me. 200 → set currentAuthUser, skip modal.
     2. 401 → show #authModal. Private testing requires a verified
        @uci.edu account; there is no guest entry point.
   After a successful login/register, we close the modal and refresh
   the sidebar/sessions so the UI reflects the new user. */

async function bootCheckAuth() {
  try {
    const r = await fetch(`${API}/api/auth/me`);
    if (r.ok) {
      const data = await r.json();
      currentAuthUser = data.user;
      USER_ID = currentAuthUser.id;
      return true;  // logged in
    }
  } catch (err) {
    console.warn('auth check failed (network?):', err);
  }
  // 401 or network err → show login modal
  document.getElementById('authModal').classList.add('open');
  return false;
}

function switchAuthTab(tab) {
  for (const t of ['login', 'register']) {
    document.getElementById(`authTab${t === 'login' ? 'Login' : 'Register'}`)
      .classList.toggle('is-active', t === tab);
    document.getElementById(`authPane${t === 'login' ? 'Login' : 'Register'}`)
      .classList.toggle('is-active', t === tab);
  }
  setAuthError('');  // clear any prior error when switching tabs
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
  setAuthError('');
  const email = document.getElementById('loginEmail').value.trim();
  const password = document.getElementById('loginPassword').value;
  if (!email || !password) {
    setAuthError('请输入邮箱和密码');
    return;
  }
  const btn = document.getElementById('loginSubmit');
  btn.disabled = true; btn.textContent = '登录中…';
  try {
    const r = await fetch(`${API}/api/auth/login`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email, password}),
    });
    if (!r.ok) {
      const detail = (await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`;
      setAuthError(detail);
      return;
    }
    await onAuthSuccess(await r.json());
  } catch (err) {
    setAuthError(`网络错误：${err.message || err}`);
  } finally {
    btn.disabled = false; btn.textContent = '登录';
  }
}

const TERMS_VERSION = '2026-09-03';
const PRIVACY_VERSION = '2026-09-03';

function isUciEmail(value) {
  return /^[^@\s]+@uci\.edu$/i.test(String(value || '').trim());
}

async function doRequestCode() {
  setAuthError('');
  const email = document.getElementById('regEmail').value.trim();
  if (!isUciEmail(email)) {
    setAuthError('内测仅支持已验证的 @uci.edu 邮箱');
    return;
  }
  const btn = document.getElementById('regSendBtn');
  btn.disabled = true; btn.textContent = '发送中…';
  try {
    const r = await fetch(`${API}/api/auth/request_code`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email}),
    });
    if (!r.ok) {
      const detail = (await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`;
      setAuthError(detail);
      return;
    }
    // Reveal step 2 and focus the code field
    document.getElementById('regCodeHint').style.display = '';
    document.getElementById('authRegisterStep2').classList.add('is-visible');
    document.getElementById('regCode').focus();
    btn.textContent = '重新发送验证码';
  } catch (err) {
    setAuthError(`网络错误：${err.message || err}`);
  } finally {
    btn.disabled = false;
  }
}

async function doVerify() {
  setAuthError('');
  const email = document.getElementById('regEmail').value.trim();
  const code = document.getElementById('regCode').value.trim();
  const password = document.getElementById('regPassword').value;
  const accepted = document.getElementById('regConsent').checked;
  if (!email || !code || !password) {
    setAuthError('请填写完整：邮箱、验证码、密码');
    return;
  }
  if (password.length < 8) {
    setAuthError('密码至少 8 位');
    return;
  }
  if (!accepted) {
    setAuthError('请确认年满 18 岁并接受服务条款与隐私声明');
    return;
  }
  const btn = document.getElementById('regVerifyBtn');
  btn.disabled = true; btn.textContent = '创建中…';
  try {
    const r = await fetch(`${API}/api/auth/verify`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        email,
        code,
        password,
        age_18_confirmed: true,
        terms_accepted: true,
        terms_version: TERMS_VERSION,
        privacy_version: PRIVACY_VERSION,
      }),
    });
    if (!r.ok) {
      const detail = (await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`;
      setAuthError(detail);
      return;
    }
    await onAuthSuccess(await r.json());
  } catch (err) {
    setAuthError(`网络错误：${err.message || err}`);
  } finally {
    btn.disabled = false; btn.textContent = '创建账号并登录';
  }
}

async function onAuthSuccess(authBody) {
  // Server set the session cookie via Set-Cookie; we just refresh state.
  currentAuthUser = {id: authBody.user_id, email: authBody.email};
  USER_ID = authBody.user_id;
  document.getElementById('authModal').classList.remove('open');
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
  document.getElementById('profileModal')?.classList.remove('open');
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
  if (!window.confirm('Permanently delete your ZotAdvisor account and all primary data?')) return;
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

// Submit-on-Enter for the auth inputs.
document.getElementById('loginPassword')?.addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); doLogin(); }
});
document.getElementById('regEmail')?.addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); doRequestCode(); }
});
document.getElementById('regPassword')?.addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); doVerify(); }
});
