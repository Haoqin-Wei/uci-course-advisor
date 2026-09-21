/* Offline browser check with synthetic API responses and a local static server.
 * No real accounts, emails, model requests, or repository data are modified. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const output = process.env.SOLON_ARTIFACT_DIR || '/tmp/solon-registration-check';
const contentTypes = {'.html':'text/html', '.js':'text/javascript', '.mjs':'text/javascript', '.css':'text/css', '.svg':'image/svg+xml'};

(async () => {
  fs.mkdirSync(output, {recursive:true});
  const server = http.createServer((req, res) => {
    const pathname = new URL(req.url, 'http://localhost').pathname;
    const file = path.resolve(root, '.' + pathname);
    if (!file.startsWith(root + path.sep + 'static' + path.sep)) {
      res.writeHead(404).end();
      return;
    }
    fs.readFile(file, (error, bytes) => {
      if (error) {res.writeHead(404).end(); return;}
      res.writeHead(200, {'Content-Type':contentTypes[path.extname(file)] || 'application/octet-stream'}).end(bytes);
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({headless:true, executablePath:process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
    const page = await browser.newPage({viewport:{width:1498,height:934}});
    const errors = [], writes = [];
    const user = {id:'registration_fixture', email:'student@uci.edu', verified_at:null};
    let authenticated = false, mode = 'duplicate', releaseRegistration;
    page.on('pageerror', error => errors.push(error.message));
    await page.route('https://**', route => route.abort());
    await page.route('**/api/**', async route => {
      const request = route.request(), url = new URL(request.url());
      const send = data => route.fulfill({json:data});
      if (request.method() !== 'GET') writes.push({path:url.pathname, body:request.postDataJSON()});
      if (url.pathname === '/api/auth/me') return authenticated ? send({user}) : route.fulfill({status:401,json:{detail:'Not authenticated'}});
      if (url.pathname === '/api/auth/register') {
        if (mode === 'duplicate') return route.fulfill({status:409,json:{detail:'Email is already registered. Use /login instead.'}});
        if (mode === 'network') return route.abort();
        await new Promise(resolve => {releaseRegistration = resolve;});
        authenticated = true;
        return send({ok:true,user_id:user.id,email:user.email});
      }
      if (url.pathname === '/api/auth/login') {
        authenticated = true;
        return send({ok:true,user_id:user.id,email:user.email});
      }
      if (url.pathname === '/api/memory/me') return send({user_id:user.id,profile:{},facts:[],preferences:[]});
      if (url.pathname === '/api/sessions/me') return send({sessions:[]});
      if (url.pathname === '/api/term-state') return send({default_term:'Fall 2026',automatic_term:'Fall 2026'});
      if (url.pathname === '/api/system_prompt') return send({prompt:''});
      throw new Error(`Unexpected API request: ${request.method()} ${url.pathname}`);
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/static/index.html`);
    await page.locator('#authModal.open').waitFor();
    assert.equal(await page.locator('#authModalTitle').textContent(), 'Sign in to Solon');
    assert(await page.locator('#loginSubmit').isDisabled());
    assert(await page.locator('.app-body').evaluate(element => element.inert));
    assert.equal(await page.locator('.app-body').isVisible(), false);
    assert.equal(await page.getByText(/Continue with|Forgot password|Need help/).count(), 0);
    assert.equal(await page.locator('#authModal').getByRole('link').count(), 2);
    await page.screenshot({path:path.join(output,'signin-desktop.png')});
    await page.locator('#authTabLogin').focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.locator('#authTabRegister').getAttribute('aria-selected'), 'true');
    assert.equal(await page.locator('#authModalTitle').textContent(), 'Create your Solon account');
    await page.keyboard.press('Home');
    assert.equal(await page.locator('#authTabLogin').getAttribute('aria-selected'), 'true');
    await page.locator('#authTabRegister').click();
    assert(await page.locator('#regSubmit').isDisabled());
    await page.screenshot({path:path.join(output,'registration-desktop.png')});
    assert.equal(await page.locator('#authPaneRegister input:not([type=checkbox])').count(), 3);
    assert.equal(await page.getByText('发送验证码', {exact:true}).count(), 0);
    for (const id of ['regEmail', 'regPassword', 'regPasswordConfirm']) assert(await page.locator('#' + id).isVisible());
    await page.locator('#regEmail').fill('student@gmail.com');
    await page.locator('#regPassword').fill('study-plan-123');
    await page.locator('#regPasswordConfirm').fill('different-password');
    await page.locator('#regConsent').check();
    await page.locator('#regSubmit').click();
    assert.match(await page.locator('#authError').textContent(), /@uci.edu/);
    assert.equal(writes.length, 0);
    await page.locator('#regEmail').fill('student@uci.edu');
    await page.locator('#regSubmit').click();
    assert.match(await page.locator('#authError').textContent(), /passwords don’t match/);
    assert.equal(writes.length, 0);

    await page.locator('#regPasswordConfirm').fill('study-plan-123');
    await page.locator('#regPasswordConfirm').press('Enter');
    await page.getByText('Email is already registered. Use /login instead.', {exact:true}).waitFor();
    assert(await page.locator('#regSubmit').isEnabled());
    mode = 'network';
    await page.locator('#regSubmit').click();
    await page.getByText('Unable to connect. Please try again.', {exact:true}).waitFor();
    assert(await page.locator('#regSubmit').isEnabled());

    await page.locator('#authTabLogin').click();
    await page.locator('#authTabRegister').click();
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert(await page.locator('#authModal').evaluate(element => element.scrollWidth <= element.clientWidth));
    assert.equal(await page.locator('.auth-story').isVisible(), false);
    assert(await page.locator('.auth-mobile-brand').isVisible());
    await page.screenshot({path:path.join(output,'registration-mobile.png')});
    await page.setViewportSize({width:320,height:568});
    await page.locator('#regSubmit').scrollIntoViewIfNeeded();
    assert(await page.locator('#authModal').evaluate(element => element.scrollWidth <= element.clientWidth));
    assert(await page.locator('#regSubmit').isVisible());
    await page.setViewportSize({width:390,height:844});
    mode = 'success';
    const registrationRequest = page.waitForRequest(request => request.url().endsWith('/api/auth/register'));
    await page.locator('#regPasswordConfirm').press('Enter');
    await registrationRequest;
    await page.waitForFunction(() => document.getElementById('regSubmit').disabled);
    const requestCount = writes.length;
    await page.evaluate(() => doRegister());
    assert.equal(writes.length, requestCount);
    assert(releaseRegistration);
    releaseRegistration();
    await page.locator('#wizardOverlay.open').waitFor();
    assert.equal(await page.locator('#authModal').isVisible(), false);
    assert.equal(await page.locator('.app-body').evaluate(element => element.inert), false);
    assert(await page.locator('.app-body').isVisible());
    assert.equal(await page.locator('#regPassword').inputValue(), '');
    assert.equal(await page.locator('#regPasswordConfirm').inputValue(), '');
    assert.equal(await page.evaluate(() => USER_ID), user.id);
    await page.locator('#wizardOverlay').getByRole('button', {name:/Skip/}).click();
    assert.equal(await page.locator('#wizardOverlay').isVisible(), false);
    const registration = writes.filter(write => write.path === '/api/auth/register');
    assert.equal(registration.length, 3);
    assert.equal(registration.at(-1).body.password_confirmation, 'study-plan-123');
    assert(!Object.hasOwn(registration.at(-1).body, 'code'));

    authenticated = false;
    await page.reload();
    await page.locator('#authModal.open').waitFor();
    await page.locator('#loginEmail').fill(user.email);
    await page.locator('#loginPassword').fill('study-plan-123');
    await page.locator('#loginPassword').press('Enter');
    await page.waitForFunction(() => !document.getElementById('authModal').classList.contains('open'));
    assert.equal(writes.at(-1).path, '/api/auth/login');
    await page.reload();
    await page.waitForFunction(() => !document.body.classList.contains('auth-pending'));
    assert.equal(await page.locator('#authModal').isVisible(), false);
    assert(await page.locator('.app-body').isVisible());
    assert.deepEqual(errors, []);
    console.log(`PASS: split account page, keyboard tabs, workspace isolation, registration validation, errors, Enter, duplicate-submit guard, auto-login, optional onboarding, login, authenticated reload, desktop/mobile/narrow layouts. Screenshots: ${output}`);
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
