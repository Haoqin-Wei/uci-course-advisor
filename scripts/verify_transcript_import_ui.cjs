#!/usr/bin/env node

/* Offline browser regression for the shipped PDF reader, worker, import
 * controller, and Student Profile renderer. All API persistence is synthetic.
 * Optional local PDFs are read only; no PDF bytes, text, or identity values
 * are sent to a server, written to the repository, or printed.
 *
 * NODE_PATH=/path/to/bundled/node_modules node scripts/verify_transcript_import_ui.cjs [sample.pdf]
 * Optional: CHROME_PATH. This script starts its own ephemeral local server.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const http = require('node:http');
const path = require('node:path');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const staticRoot = path.join(root, 'static');
const fixtureSummary = {
  official_uc_gpa: 3.4,
  grade_units_attempted: 80,
  total_units_passed: 100,
  units_completed: 112.5,
};
const replacementSummary = {
  official_uc_gpa: 3.8,
  grade_units_attempted: 48,
  total_units_passed: 48,
  units_completed: 52,
};
const replacementCourse = {title: 'CALCULUS II', department: 'MATH', number: '2B'};

function syntheticPdf({summary = fixtureSummary, course = {title: 'GENERAL CHEMISTRY', department: 'CHEM', number: '1A'}} = {}) {
  const text = (value, x, y) => `BT /F1 9 Tf 1 0 0 1 ${x} ${y} Tm (${value}) Tj ET`;
  // The real layout has two label baselines with the number halfway between
  // them. Nearby unrelated headings must not be absorbed into these labels.
  const items = [
    text('THIS IS NOT AN OFFICIAL TRANSCRIPT', 35, 750),
    text('University Requirements', 35, 715),
    text('2026 Winter Quarter', 35, 670),
    text(course.title, 35, 646), text(course.department, 260, 646),
    text(course.number, 340, 646), text('4.0', 380, 646),
    text('A', 420, 646), text('16.0', 455, 646),
    text('Term Totals GPA: 4.0', 35, 620),
    text('GRADE POINTS', 35, 520), text('200.0', 115, 520),
    text('BALANCE', 170, 520), text('0.0', 223, 520),
  ];
  if (summary) items.push(
    text('GRADE UNITS', 35, 480), text('ATTEMPTED', 35, 468), text(String(summary.grade_units_attempted), 112, 474),
    text('TOTAL UNITS PASSED', 170, 474), text(String(summary.total_units_passed), 282, 474),
    text('UC', 340, 480), text('GPA', 340, 468), text(String(summary.official_uc_gpa), 368, 474),
    text('UNITS', 430, 480), text('COMPLETED', 430, 468), text(String(summary.units_completed), 501, 474),
  );
  const content = items.join('\n');
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    `<< /Length ${Buffer.byteLength(content)} >>\nstream\n${content}\nendstream`,
  ];
  let pdf = '%PDF-1.4\n';
  const offsets = [0];
  objects.forEach((object, index) => {
    offsets.push(Buffer.byteLength(pdf));
    pdf += `${index + 1} 0 obj\n${object}\nendobj\n`;
  });
  const start = Buffer.byteLength(pdf);
  pdf += `xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map(offset => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')}trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${start}\n%%EOF`;
  return Buffer.from(pdf);
}

const harness = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Offline transcript import check</title></head>
<body class="profile-open"><button id="profileEditButton">Edit</button><div id="profileBody"></div>
<input id="transcriptFileInput" type="file" accept="application/pdf,.pdf" hidden>
<script>
const API = '';
const currentAuthUser = {id: 'synthetic-profile'};
const currentTermContext = {term: ''};
const wizardState = {step: 4, completed_courses: new Set(['MATH 2A'])};
window.__wizardMatrixRenders = 0;
window.__wizardFooterSyncs = 0;
function wizardRenderMatrix() {window.__wizardMatrixRenders++;}
function wizardSyncFooterCount() {window.__wizardFooterSyncs++;}
function displayTerm(value) {return value;}
function solonIcon() {return '';}
function escHTML(value) {return String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));}
function escAttr(value) {return escHTML(value);}
</script>
<script src="/static/js/student-profile.js"></script><script src="/static/js/transcript-import.js"></script>
<script>loadProfile();</script></body></html>`;

async function main() {
  if (process.argv.length > 3) throw new Error('Usage: node scripts/verify_transcript_import_ui.cjs [sample.pdf]');
  const providedSample = process.argv[2];
  const buffer = providedSample ? await fs.readFile(providedSample) : syntheticPdf();
  const requestedStaticPaths = new Set();
  const server = http.createServer(async (request, response) => {
    try {
      const pathname = new URL(request.url, 'http://localhost').pathname;
      if (pathname === '/') {
        response.writeHead(200, {'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store'});
        response.end(harness);
        return;
      }
      if (!pathname.startsWith('/static/')) {
        response.writeHead(404).end();
        return;
      }
      const target = path.resolve(staticRoot, decodeURIComponent(pathname.slice('/static/'.length)));
      if (!target.startsWith(staticRoot + path.sep)) {
        response.writeHead(403).end();
        return;
      }
      const bytes = await fs.readFile(target);
      requestedStaticPaths.add(pathname);
      const mime = /\.(?:mjs|js)$/.test(target) ? 'text/javascript; charset=utf-8' : 'application/octet-stream';
      response.writeHead(200, {'Content-Type': mime, 'Cache-Control': 'no-store'});
      response.end(bytes);
    } catch {
      response.writeHead(404).end();
    }
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  let browser;
  try {
    const base = `http://127.0.0.1:${server.address().port}`;
    browser = await chromium.launch({
      headless: true,
      executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    });
    const page = await browser.newPage({viewport: {width: 390, height: 844}});
    const pageErrors = [];
    const externalRequests = [];
    page.on('pageerror', () => pageErrors.push('Browser script error'));
    await page.route('**/*', route => {
      if (new URL(route.request().url()).origin === base) return route.continue();
      externalRequests.push('Blocked nonlocal request');
      return route.abort();
    });
    // API simulation only accepts the allow-listed parser object. The selected
    // PDF never reaches this route or the static server.
    let persisted = null;
    let importCount = 0;
    let gpaReads = 0;
    await page.route('**/api/**', async route => {
      const request = route.request();
      const url = new URL(request.url());
      const send = data => route.fulfill({json: data, headers: {'Cache-Control': 'no-store'}});
      if (url.pathname === '/api/memory/me') {
        return send({user_id: 'synthetic-profile', profile: {completed_courses: []}});
      }
      if (url.pathname === '/api/onboarding/departments') return send({departments: []});
      if (url.pathname === '/api/academic/transcript/import') {
        assert.equal(request.method(), 'POST');
        persisted = request.postDataJSON();
        assert.ok(persisted.summary);
        assert.ok(persisted.client_request_id.length >= 8 && persisted.client_request_id.length <= 64);
        assert.ok(!/(student_id|studentid|raw_text|filename|file_name|source_url)/i.test(JSON.stringify(persisted)));
        importCount++;
        const courses = persisted.courses || [];
        return send({ok: true, read: courses.length, accepted: courses.length, added: courses.length,
          updated: 0, skipped: persisted.skipped_count || 0, issues: [],
          completed_course_ids: courses.map(course => course.course_id)});
      }
      if (url.pathname === '/api/academic/profile') {
        const summary = persisted?.summary || {};
        const data = {ok: true, completed_courses: persisted?.courses || [],
          units_completed: summary.units_completed ?? null,
          gpa_available: summary.official_uc_gpa !== null && summary.official_uc_gpa !== undefined,
          last_import: persisted ? {imported_at: '2026-10-07T05:10:00Z'} : null};
        if (url.searchParams.get('include_gpa') === 'true') {
          gpaReads++;
          data.official_uc_gpa = summary.official_uc_gpa ?? null;
        }
        return send(data);
      }
      return route.fulfill({status: 404, json: {detail: 'Unknown synthetic route'}});
    });
    await page.addInitScript(() => {
      delete Promise.withResolvers;
      delete Map.prototype.getOrInsert;
      delete Map.prototype.getOrInsertComputed;
      Object.defineProperty(Crypto.prototype, 'randomUUID', {configurable: true, value: undefined});
      window.__transcriptCompatibilityBefore = {
        randomUUID: typeof crypto.randomUUID,
        withResolvers: typeof Promise.withResolvers,
        getOrInsert: typeof Map.prototype.getOrInsert,
        getOrInsertComputed: typeof Map.prototype.getOrInsertComputed,
      };
      window.__transcriptWorkerChecks = [];
      const NativeWorker = window.Worker;
      // The wrapper removes newer APIs in the actual worker realm before it
      // dynamically imports the unmodified, shipped worker entry and modules.
      window.Worker = class extends NativeWorker {
        constructor(url, options) {
          const source = new URL(String(url), location.href).href;
          const code = `delete Promise.withResolvers;
            delete Map.prototype.getOrInsert;
            delete Map.prototype.getOrInsertComputed;
            self.postMessage({__transcriptHarness: {stage: 'before',
              withResolvers: typeof Promise.withResolvers,
              getOrInsert: typeof Map.prototype.getOrInsert,
              getOrInsertComputed: typeof Map.prototype.getOrInsertComputed}});
            await import(${JSON.stringify(source)});
            self.postMessage({__transcriptHarness: {stage: 'after',
              withResolvers: typeof Promise.withResolvers,
              getOrInsert: typeof Map.prototype.getOrInsert,
              getOrInsertComputed: typeof Map.prototype.getOrInsertComputed}});`;
          const blobUrl = URL.createObjectURL(new Blob([code], {type: 'text/javascript'}));
          super(blobUrl, {...options, type: 'module'});
          this.addEventListener('message', event => {
            if (!event.data?.__transcriptHarness) return;
            event.stopImmediatePropagation();
            window.__transcriptWorkerChecks.push(event.data.__transcriptHarness);
          });
          this.addEventListener('error', () => URL.revokeObjectURL(blobUrl), {once: true});
        }
      };
    });
    await page.goto(base);
    await page.locator('#profileMajor').waitFor();
    assert.equal(await page.locator('#profileUnits').textContent(), '—');
    assert.equal(await page.locator('#profileGpaHint').textContent(), 'No GPA imported');
    const importFile = async (bytes, source = 'profile') => {
      const previousCount = importCount;
      await page.evaluate(value => {transcriptImportSource = value;}, source);
      await page.locator('#transcriptFileInput').setInputFiles({
        name: 'local-transcript.pdf', mimeType: 'application/pdf', buffer: bytes,
      });
      await page.waitForFunction(() => {
        const status = document.getElementById('profileTranscriptStatus');
        return !transcriptImportBusy && status && !status.hidden && status.dataset.tone !== '';
      }, null, {timeout: 30000});
      assert.equal(importCount, previousCount + 1, 'The shipped PDF reader must complete an import');
      assert.deepEqual(await page.evaluate(() => Array.from(wizardState.completed_courses).sort()),
        persisted.courses.map(course => course.course_id).sort(), 'Import must replace the wizard course selection');
    };
    await importFile(buffer);
    for (const name of Object.keys(fixtureSummary)) {
      assert.equal(typeof persisted.summary[name], 'number', `Missing parsed ${name}`);
      assert.ok(Number.isFinite(persisted.summary[name]), `Invalid parsed ${name}`);
    }
    if (!providedSample) assert.deepEqual(persisted.summary, fixtureSummary);
    assert.equal(await page.locator('#profileUnits').textContent(),
      persisted.summary.units_completed.toLocaleString('en-US', {maximumFractionDigits: 2}));
    assert.equal(await page.locator('#profileGpaHint').textContent(), 'Hidden · click to show');
    assert.equal(await page.locator('#profileGpaValue').textContent(), '••••');
    assert.equal(gpaReads, 0);
    await page.getByRole('button', {name: 'Show GPA', exact: true}).click();
    await page.waitForFunction(() => document.getElementById('profileGpaToggle')?.getAttribute('aria-pressed') === 'true');
    assert.equal(await page.locator('#profileGpaValue').textContent(), persisted.summary.official_uc_gpa.toFixed(2));
    assert.equal(gpaReads, 1);
    const previousCourse = persisted.courses.find(course => course.course_id !== 'MATH 2B')?.course_id;
    assert.ok(previousCourse, 'First transcript must contain a course omitted by the replacement');
    const replacement = syntheticPdf({summary: replacementSummary, course: replacementCourse});
    try {
      await importFile(replacement);
    } finally {
      replacement.fill(0);
    }
    assert.deepEqual(persisted.summary, replacementSummary);
    assert.deepEqual(persisted.courses.map(course => course.course_id), ['MATH 2B']);
    assert.deepEqual(await page.locator('.profile-course-code').allTextContents(), ['MATH 2B']);
    assert.equal(await page.locator('#profileCourseCount').textContent(), '1');
    assert.equal(await page.locator('#profileUnits').textContent(), '52');
    assert.equal(await page.locator('#profileGpaValue').textContent(), '••••', 'Replacement must reset the previously visible GPA');
    assert.equal(await page.locator('#profileGpaHint').textContent(), 'Hidden · click to show');
    assert.ok(!(await page.evaluate(() => Array.from(wizardState.completed_courses))).includes(previousCourse));
    await page.getByRole('button', {name: 'Show GPA', exact: true}).click();
    await page.waitForFunction(() => document.getElementById('profileGpaToggle')?.getAttribute('aria-pressed') === 'true');
    assert.equal(await page.locator('#profileGpaValue').textContent(), '3.80');
    assert.equal(gpaReads, 2);
    // A later import without a summary must clear the earlier GPA and units,
    // including when initiated from the editing wizard over an open Profile.
    const missingSummary = syntheticPdf({summary: null, course: replacementCourse});
    await page.evaluate(() => wizardState.completed_courses.add('ANTHRO 2A'));
    try {
      await importFile(missingSummary, 'wizard');
    } finally {
      missingSummary.fill(0);
    }
    assert.deepEqual(persisted.summary, {
      official_uc_gpa: null, grade_units_attempted: null,
      total_units_passed: null, units_completed: null,
    });
    assert.deepEqual(await page.locator('.profile-course-code').allTextContents(), ['MATH 2B']);
    assert.equal(await page.locator('#profileUnits').textContent(), '—');
    assert.equal(await page.locator('#profileGpaValue').textContent(), '—');
    assert.equal(await page.locator('#profileGpaHint').textContent(), 'No GPA imported');
    assert.equal(await page.locator('#profileGpaToggle').isVisible(), false);
    assert.equal(gpaReads, 2);
    assert.deepEqual(await page.evaluate(() => [window.__wizardMatrixRenders, window.__wizardFooterSyncs]), [3, 3]);
    const compatibility = await page.evaluate(() => ({
      before: window.__transcriptCompatibilityBefore,
      workers: window.__transcriptWorkerChecks,
      requestIdFallback: typeof crypto.randomUUID === 'undefined',
      readerCompat: typeof Promise.withResolvers,
    }));
    assert.ok(Object.values(compatibility.before).every(value => value === 'undefined'));
    assert.ok(compatibility.requestIdFallback);
    assert.equal(compatibility.readerCompat, 'function');
    assert.ok(compatibility.workers.some(check => check.stage === 'before' && check.withResolvers === 'undefined'
      && check.getOrInsert === 'undefined' && check.getOrInsertComputed === 'undefined'), 'Real PDF worker must remove newer APIs');
    assert.ok(compatibility.workers.some(check => check.stage === 'after' && check.withResolvers === 'function'
      && check.getOrInsert === 'function' && check.getOrInsertComputed === 'function'), 'Shipped worker compatibility must restore APIs');
    for (const file of ['worker-entry.mjs', 'compat.mjs', 'pdf.worker.min.mjs']) {
      assert.ok(requestedStaticPaths.has(`/static/vendor/pdfjs/${file}`), `Actual worker asset not requested: ${file}`);
    }
    assert.deepEqual(pageErrors, []);
    assert.deepEqual(externalRequests, []);
    console.log(`Offline transcript import UI regression passed (${providedSample ? 'provided local PDF' : 'synthetic stacked-label PDF'}; successive imports replace profile and wizard data, missing summaries clear prior values; actual worker and older-browser API simulation).`);
  } finally {
    buffer.fill(0);
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}

main().catch(error => {
  // Avoid printing browser details, extracted text, or a sample path on failure.
  console.error(process.argv[2]
    ? `Offline transcript import UI regression failed: ${error.name}.`
    : `Offline transcript import UI regression failed: ${error.stack}`);
  process.exitCode = 1;
});
