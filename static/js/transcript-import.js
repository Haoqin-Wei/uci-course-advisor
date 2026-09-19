/* Transcript import controller. The file bytes and extracted text stay in this
 * browser process; only the parser's allow-listed academic object is POSTed.
 */

const TRANSCRIPT_MAX_BYTES = 5 * 1024 * 1024;
const TRANSCRIPT_MAX_PAGES = 20;
let transcriptImportSource = 'profile';
let transcriptImportBusy = false;

function triggerTranscriptPicker(source = 'profile') {
  if (transcriptImportBusy) return;
  transcriptImportSource = source;
  const input = document.getElementById('transcriptFileInput');
  input.value = '';
  input.click();
}

function transcriptSetStatus(message, tone = '') {
  const targets = [
    document.getElementById('wizardTranscriptStatus'),
    document.getElementById('profileTranscriptStatus'),
  ];
  for (const target of targets) {
    if (!target) continue;
    target.replaceChildren();
    target.textContent = message || '';
    target.dataset.tone = tone;
    target.hidden = !message;
  }
}

function transcriptRenderImportResult(result) {
  const read = Number(result.read || 0);
  const accepted = Number(result.accepted || 0);
  const skipped = Number(result.skipped || 0);
  const unchanged = Number(result.unchanged || 0);
  const olderIgnored = Number(result.older_ignored || 0);
  const issues = Array.isArray(result.issues) ? result.issues : [];
  const targets = [
    document.getElementById('wizardTranscriptStatus'),
    document.getElementById('profileTranscriptStatus'),
  ];

  for (const target of targets) {
    if (!target) continue;
    target.replaceChildren();
    target.hidden = false;
    target.dataset.tone = skipped > 0 ? 'warning' : 'success';

    const summary = document.createElement('div');
    summary.className = 'transcript-import-summary';
    summary.textContent = `Read ${read} · Accepted ${accepted} · ${skipped} need attention`;
    target.appendChild(summary);

    const secondaryParts = [
      `${Number(result.added || 0)} added`,
      `${Number(result.updated || 0)} updated`,
    ];
    if (unchanged) secondaryParts.push(`${unchanged} unchanged`);
    if (olderIgnored) secondaryParts.push(`${olderIgnored} older record(s) ignored`);
    const secondary = document.createElement('div');
    secondary.className = 'transcript-import-secondary';
    secondary.textContent = secondaryParts.join(' · ');
    target.appendChild(secondary);

    if (!issues.length) continue;
    const details = document.createElement('details');
    details.className = 'transcript-import-details';
    const summaryToggle = document.createElement('summary');
    summaryToggle.textContent = `View details (${issues.length})`;
    details.appendChild(summaryToggle);
    const list = document.createElement('ul');
    for (const issue of issues) {
      const item = document.createElement('li');
      const prefix = issue.course_id ? `${issue.course_id}: ` : '';
      item.textContent = `${prefix}${issue.message || issue.reason_code || 'Not imported'}`;
      if (issue.level === 'info') item.dataset.level = 'info';
      list.appendChild(item);
    }
    details.appendChild(list);
    target.appendChild(details);
  }
}

async function extractTranscriptPayload(file) {
  if (!file || !/\.pdf$/i.test(file.name || '') || file.type && file.type !== 'application/pdf') {
    throw new Error('Choose a PDF file.');
  }
  if (file.size > TRANSCRIPT_MAX_BYTES) {
    throw new Error('The PDF is larger than 5 MB.');
  }

  transcriptSetStatus('Reading locally…');
  const arrayBuffer = await file.arrayBuffer();
  const bytes = new Uint8Array(arrayBuffer);
  const signature = new TextDecoder('ascii').decode(bytes.slice(0, 5));
  if (signature !== '%PDF-') {
    if (bytes.byteLength) bytes.fill(0);
    throw new Error('The selected file is not a valid PDF.');
  }

  let pdfDocument = null;
  let loadingTask = null;
  try {
    const [pdfjs, parser] = await Promise.all([
      import('/static/vendor/pdfjs/pdf.min.mjs'),
      import('/static/js/transcript-parser.mjs'),
    ]);
    pdfjs.GlobalWorkerOptions.workerSrc = '/static/vendor/pdfjs/pdf.worker.min.mjs';
    loadingTask = pdfjs.getDocument({data: bytes, isEvalSupported: false});
    pdfDocument = await loadingTask.promise;
    if (pdfDocument.numPages > TRANSCRIPT_MAX_PAGES) {
      throw new Error('The PDF has more than 20 pages.');
    }

    transcriptSetStatus('Validating UCI format…');
    const pages = [];
    for (let pageNumber = 1; pageNumber <= pdfDocument.numPages; pageNumber += 1) {
      const page = await pdfDocument.getPage(pageNumber);
      const content = await page.getTextContent();
      pages.push(content.items.map(item => ({
        str: item.str,
        transform: item.transform,
        width: item.width,
      })));
      page.cleanup();
    }
    const lines = parser.reconstructPdfLines(pages);
    const payload = parser.parseUciTranscriptLines(lines);
    const localIssues = Array.isArray(payload.local_issues) ? payload.local_issues : [];
    delete payload.local_issues;
    pages.length = 0;
    lines.fill('');
    payload.client_request_id = crypto.randomUUID();
    return {payload, localIssues};
  } catch (error) {
    if (error?.name === 'PasswordException') {
      throw new Error('Encrypted PDFs are not supported.');
    }
    throw error;
  } finally {
    if (loadingTask && typeof loadingTask.destroy === 'function') {
      await loadingTask.destroy().catch(() => {});
    }
    if (bytes.byteLength) bytes.fill(0);
  }
}

async function importTranscriptFile(file, source = 'profile') {
  if (!file || transcriptImportBusy) return;
  transcriptImportSource = source;
  transcriptImportBusy = true;
  for (const button of document.querySelectorAll('[data-transcript-button]')) button.disabled = true;
  try {
    const {payload, localIssues} = await extractTranscriptPayload(file);
    transcriptSetStatus('Saving structured academic data…');
    const response = await fetch(`${API}/api/academic/transcript/import`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || `Import failed (${response.status})`);
    }
    const result = await response.json();
    const serverIssues = Array.isArray(result.issues) ? result.issues : [];
    result.issues = [
      ...localIssues,
      ...serverIssues.filter(issue => issue.reason_code !== 'parser_unrecognized' || !localIssues.length),
    ];
    if (typeof wizardState !== 'undefined') {
      for (const courseId of result.completed_course_ids || []) {
        wizardState.completed_courses.add(courseId);
      }
      if (typeof wizardRenderMatrix === 'function' && wizardState.step === 4) wizardRenderMatrix();
      if (typeof wizardSyncFooterCount === 'function') wizardSyncFooterCount();
    }
    if (transcriptImportSource === 'profile' && typeof loadProfile === 'function') {
      await loadProfile();
    }
    transcriptRenderImportResult(result);
    if (typeof loadSidebar === 'function') loadSidebar(true);
  } catch (error) {
    transcriptSetStatus(error?.message || 'Unable to import this transcript.', 'error');
  } finally {
    transcriptImportBusy = false;
    for (const button of document.querySelectorAll('[data-transcript-button]')) button.disabled = false;
  }
}

async function handleTranscriptFile(event) {
  const file = event.target.files?.[0];
  try {
    await importTranscriptFile(file, transcriptImportSource);
  } finally {
    event.target.value = '';
  }
}

document.getElementById('transcriptFileInput')?.addEventListener('change', handleTranscriptFile);
document.addEventListener('dragover', event => {
  const zone = event.target.closest?.('[data-transcript-dropzone]');
  if (!zone) return;
  event.preventDefault();
  zone.classList.add('is-dragover');
});
document.addEventListener('dragleave', event => {
  event.target.closest?.('[data-transcript-dropzone]')?.classList.remove('is-dragover');
});
document.addEventListener('drop', event => {
  const zone = event.target.closest?.('[data-transcript-dropzone]');
  if (!zone) return;
  event.preventDefault();
  zone.classList.remove('is-dragover');
  importTranscriptFile(event.dataTransfer?.files?.[0], 'wizard');
});

window.triggerTranscriptPicker = triggerTranscriptPicker;
