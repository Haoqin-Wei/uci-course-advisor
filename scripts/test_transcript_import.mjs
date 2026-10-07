/* Offline regressions; all transcript fields below are synthetic. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {parseUciTranscriptLines, reconstructPdfLines} from '../static/js/transcript-parser.mjs';

const headers = ['THIS IS NOT AN OFFICIAL TRANSCRIPT', 'University Requirements'];
const parse = lines => parseUciTranscriptLines([...headers, ...lines]).summary;
assert.deepEqual(parse([
  'UNITS COMPLETED: 112.5', 'UC GPA: 3.4',
  'TOTAL UNITS PASSED 100', 'GRADE UNITS ATTEMPTED 80',
]), {official_uc_gpa: 3.4, grade_units_attempted: 80, total_units_passed: 100, units_completed: 112.5});
assert.equal(parse(['UC GPA 0', 'UNITS COMPLETED 0']).official_uc_gpa, 0);
assert.equal(parse(['UC GPA 0', 'UNITS COMPLETED 0']).units_completed, 0);
assert.equal(parse(['UC GPA 3.5']).units_completed, null);
assert.equal(parse(['Term Totals GPA: 3.9']).official_uc_gpa, null);
const item = (str, x, y) => ({str, transform: [1, 0, 0, 1, x, y], width: str.length * 4});
const lines = reconstructPdfLines([[
  item('UC GPA', 10, 50), item('3.4', 80, 50),
  item('UNITS COMPLETED', 120, 50), item('112.5', 210, 50),
]]);
assert.equal(parse(lines).official_uc_gpa, 3.4);
assert.equal(parse(lines).units_completed, 112.5);

// Synthetic coordinates reproduce the UCI summary's stacked labels and
// vertically centered values without retaining any real transcript data.
const stacked = [
  item('GRADE UNITS', 79, 620), item('ATTEMPTED', 79, 608),
  item('80.0', 214, 614), item('GRADE POINTS', 245, 614), item('272.0', 345, 614),
  item('UC', 382, 620), item('GPA', 382, 608), item('3.400', 428, 614),
  item('BALANCE', 465, 614), item('112.0', 522, 614),
  item('TOTAL UNITS PASSED', 79, 588), item('80.0', 214, 588),
  item('UNITS', 245, 594), item('COMPLETED', 245, 582), item('112.5', 345, 588),
];
const expectedStacked = {official_uc_gpa: 3.4, grade_units_attempted: 80, total_units_passed: 80, units_completed: 112.5};
assert.deepEqual(parse(reconstructPdfLines([stacked])), expectedStacked);
assert.deepEqual(parse(reconstructPdfLines([[...stacked].reverse()])), expectedStacked);
// Summary merging must not collapse normal course row baselines.
const courses = parseUciTranscriptLines([
  ...headers, '2025 Fall Quarter',
  ...reconstructPdfLines([[
    item('Calculus   MATH   2B 4.0 A 16.0', 79, 700),
    item('Physics   PHYSICS   7C 4.0 B 12.0', 79, 688),
  ]]),
  'UC GPA 3.4',
]);
assert.deepEqual(courses.courses.map(course => course.course_id), ['MATH 2B', 'PHYSICS 7C']);

const controller = fs.readFileSync(new URL('../static/js/transcript-import.js', import.meta.url), 'utf8');
for (const cryptoApi of [undefined, {}, {getRandomValues: bytes => bytes.fill(42)}]) {
  const context = vm.createContext({crypto: cryptoApi, document: {getElementById: () => null, addEventListener() {}}, window: {}});
  vm.runInContext(controller, context);
  const id = vm.runInContext('transcriptRequestId()', context);
  assert.equal(typeof id, 'string');
  assert.ok(id.length >= 8 && id.length <= 64);
}

// Exercise the shipped PDF reader and worker without APIs absent on older iOS.
delete Promise.withResolvers;
delete Map.prototype.getOrInsertComputed;
delete Map.prototype.getOrInsert;
// Text extraction does not use canvas matrices; Node has no browser DOM.
globalThis.DOMMatrix = class DOMMatrix {};
await import('../static/vendor/pdfjs/compat.mjs');
const pdfjs = await import('../static/vendor/pdfjs/pdf.min.mjs');
pdfjs.GlobalWorkerOptions.workerSrc = new URL('../static/vendor/pdfjs/worker-entry.mjs', import.meta.url).href;
const content = 'BT /F1 12 Tf 30 700 Td (UC GPA 3.4) Tj ET';
const objects = [
  '<< /Type /Catalog /Pages 2 0 R >>',
  '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
  '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
  '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
  `<< /Length ${content.length} >>\nstream\n${content}\nendstream`,
];
let pdf = '%PDF-1.4\n';
const offsets = [0];
objects.forEach((object, index) => { offsets.push(pdf.length); pdf += `${index + 1} 0 obj\n${object}\nendobj\n`; });
const start = pdf.length;
pdf += `xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map(offset => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')}trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${start}\n%%EOF`;
const task = pdfjs.getDocument({data: new TextEncoder().encode(pdf), isEvalSupported: false});
try {
  const doc = await task.promise;
  const page = await doc.getPage(1);
  const text = await page.getTextContent();
  assert.equal(parse(reconstructPdfLines([text.items])).official_uc_gpa, 3.4);
} finally { await task.destroy(); }
console.log('Transcript compatibility and summary regressions passed.');
