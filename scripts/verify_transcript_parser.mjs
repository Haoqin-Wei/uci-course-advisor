#!/usr/bin/env node

/* Local, read-only smoke check for real transcript samples. It prints only
 * aggregate parser results and never prints PDF text or identity values.
 */

import fs from 'node:fs/promises';
import process from 'node:process';
import * as pdfjs from 'pdfjs-dist/legacy/build/pdf.mjs';
import {
  parseUciTranscriptLines,
  reconstructPdfLines,
} from '../static/js/transcript-parser.mjs';


async function parseFile(path) {
  const source = new Uint8Array(await fs.readFile(path));
  let pdfDocument = null;
  let loadingTask = null;
  try {
    loadingTask = pdfjs.getDocument({data: source, isEvalSupported: false});
    pdfDocument = await loadingTask.promise;
    const pages = [];
    for (let pageNumber = 1; pageNumber <= pdfDocument.numPages; pageNumber += 1) {
      const page = await pdfDocument.getPage(pageNumber);
      const content = await page.getTextContent();
      pages.push(content.items.map(item => ({
        str: item.str,
        transform: item.transform,
        width: item.width,
      })));
    }
    const lines = reconstructPdfLines(pages);
    const parsed = parseUciTranscriptLines(lines);
    lines.fill('');
    const serialized = JSON.stringify(parsed).toLowerCase();
    return {
      pages: pdfDocument.numPages,
      courses: parsed.courses.length,
      examCredits: parsed.exam_credits.length,
      transferCredits: parsed.transfer_credits.length,
      requirements: parsed.university_requirements.length,
      skipped: parsed.skipped_count,
      structuredCourseFieldsPresent: parsed.courses.every(course => (
        Boolean(course.department) && Boolean(course.course_number) &&
        course.course_id === `${course.department} ${course.course_number}`
      )),
      hasOfficialGpa: parsed.summary.official_uc_gpa !== null,
      hasPrintTimestamp: Boolean(parsed.printed_at),
      repeatResolved: parsed.courses.filter(course => course.course_id === 'I&C SCI 32').length <= 1,
      identityFieldsAbsent: !/(student_id|studentid|raw_text|filename|file_name|source_url)/.test(serialized),
    };
  } finally {
    if (loadingTask && typeof loadingTask.destroy === 'function') await loadingTask.destroy();
    if (source.byteLength) source.fill(0);
  }
}


if (process.argv.length < 3) {
  console.error('Usage: node scripts/verify_transcript_parser.mjs <sample.pdf> [sample.pdf]');
  process.exit(2);
}

const results = [];
for (const path of process.argv.slice(2)) results.push(await parseFile(path));
console.log(JSON.stringify(results, null, 2));
