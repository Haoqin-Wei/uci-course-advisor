/* Deterministic parser for the current UCI unofficial transcript layout.
 *
 * Input is page text reconstructed locally from PDF.js coordinates. Output is
 * an allow-listed academic payload: identity headers, footer URLs, and raw text
 * are never included.
 */

export const TRANSCRIPT_PARSER_VERSION = 'uci-current-v2';

const TERM_RE = /^(\d{4})\s+(Fall|Winter|Spring)\s+Quarter$|^(\d{4})\s+(First|Second|Ten-Week)\s+Summer\s+Session$/i;
const GRADE_RE = '(?:A\\+|A-|A|B\\+|B-|B|C\\+|C-|C|D\\+|D-|D|F|NP|P|W|I|IP|NR|S|U)';
const COURSE_RE = new RegExp(
  '^\\s*(.+?)\\s{2,}([A-Z][A-Z0-9&/]*(?:\\s+[A-Z][A-Z0-9&/]*)?)\\s{2,}' +
  '([A-Z]?\\d{1,3}[A-Z0-9/]{0,5})\\s+(\\d+(?:\\.\\d+)?)\\s+(' + GRADE_RE + ')' +
  '\\s+(\\d+(?:\\.\\d+)?)\\s*([A-Z][A-Z0-9]{0,2})?\\s*$',
  'i',
);

const NON_PASSING = new Set(['F', 'NP', 'U', 'W', 'I', 'IP', 'NR']);
const REPEAT_REPLACEMENT_CODES = new Set(['G0', 'G1', 'G2']);
const REPEAT_CODES = new Set(['RF', 'G0', 'G1', 'G2']);

function cleanLine(value) {
  return String(value || '').replace(/\u00a0/g, ' ').replace(/[ \t]+$/g, '');
}

function canonicalCourseId(department, number) {
  return `${String(department || '').trim().toUpperCase().replace(/\s+/g, ' ')} ${String(number || '').trim().toUpperCase()}`;
}

function sanitizedCandidateCourseId(line) {
  const match = String(line || '').match(
    /\s{2,}([A-Z][A-Z0-9&/]*(?:\s+[A-Z][A-Z0-9&/]*)?)\s{2,}([A-Z]?\d{1,3}[A-Z0-9/]{0,5})(?:\s|$)/i,
  );
  return match ? canonicalCourseId(match[1], match[2]) : null;
}

function parsePrintedAt(lines) {
  for (const line of lines) {
    const match = line.match(/\b(20\d{2})\/(\d{1,2})\/(\d{1,2})\s+(\d{1,2}):(\d{2})\b/);
    if (!match) continue;
    const [, year, month, day, hour, minute] = match;
    const value = new Date(
      Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), 0, 0,
    );
    if (!Number.isNaN(value.getTime())) return value.toISOString();
  }
  return null;
}

function parseRequirements(lines) {
  const output = [];
  let inSection = false;
  for (const raw of lines) {
    const line = raw.trim();
    if (/^University Requirements$/i.test(line)) {
      inSection = true;
      continue;
    }
    if (!inSection) continue;
    if (/^(?:AP|IB)\s+/i.test(line) || TERM_RE.test(line)) break;
    const match = line.match(/^(\d{2}\/\d{2}\/\d{2})\s+(.+?)\s+-\s+(.+)$/);
    if (!match) continue;
    output.push({
      requirement_code: match[2].trim(),
      status: match[3].trim(),
      status_date: match[1],
    });
  }
  return output;
}

function parseExamCredits(lines) {
  const output = [];
  for (const raw of lines) {
    const line = raw.trim();
    const match = line.match(
      /^(AP|IB|A[ -]?LEVEL)\s+(.+?)\s+\(Score\s+([0-9.]+),\s*Units\s+([0-9.]+)\)\s+(\d{2}\/\d{2})$/i,
    );
    if (!match) continue;
    output.push({
      exam_type: match[1].toUpperCase().replace(/[ -]/g, '_'),
      subject: match[2].trim(),
      score: Number(match[3]),
      units: Number(match[4]),
      exam_date: match[5],
      uci_equivalent_course: null,
      confidence: 1,
    });
  }
  return output;
}

function parseTransferCredits(lines) {
  const output = [];
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index].trim();
    const detailed = line.match(/^(.+?)\s+\(Units\s+([0-9.]+)\)\s+\d+\s+Terms?\s+to\s+(\d{2}\/\d{2})$/i);
    if (detailed && !/^(AP|IB|A[ -]?LEVEL)\s/i.test(line)) {
      output.push({
        institution_name: detailed[1].trim(),
        units: Number(detailed[2]),
        terms_through: detailed[3],
        uci_equivalent_course: null,
        confidence: 1,
      });
      continue;
    }
    const summary = line.match(/^Units Transferred\s+([0-9.]+)$/i);
    if (!summary) continue;
    const hasDetailedNeighbor = lines.slice(Math.max(0, index - 3), index)
      .some(candidate => /\(Units\s+[0-9.]+\)\s+\d+\s+Terms?\s+to/i.test(candidate));
    if (!hasDetailedNeighbor) {
      output.push({
        institution_name: null,
        units: Number(summary[1]),
        terms_through: null,
        uci_equivalent_course: null,
        confidence: 0.9,
      });
    }
  }
  return output;
}

function parseSummary(lines) {
  const joined = lines.join('\n');
  const gpa = joined.match(/GRADE UNITS ATTEMPTED\s+([0-9.]+).*?UC GPA\s+([0-9.]+)/s);
  const completed = joined.match(/TOTAL UNITS PASSED\s+([0-9.]+)\s+UNITS COMPLETED\s+([0-9.]+)/);
  return {
    official_uc_gpa: gpa ? Number(gpa[2]) : null,
    grade_units_attempted: gpa ? Number(gpa[1]) : null,
    total_units_passed: completed ? Number(completed[1]) : null,
    units_completed: completed ? Number(completed[2]) : null,
  };
}

function looksLikeUnparsedCourse(line) {
  if (!/\d+(?:\.\d+)?/.test(line)) return false;
  if (/^(?:Term|Cumulative) Totals/i.test(line.trim())) return false;
  if (/\(Units\s+[0-9.]+\).*Terms?\s+to|^\s*Units Transferred/i.test(line)) return false;
  if (/\b(?:ATTM|PSSD|GPTS|GPA|BAL)\s*:/i.test(line)) return false;
  return /[A-Z]{2,}/.test(line) && new RegExp(`\\b${GRADE_RE}\\b`).test(line);
}

function resolveEffectiveCourses(attempts) {
  const grouped = new Map();
  for (const attempt of attempts) {
    const rows = grouped.get(attempt.course_id) || [];
    rows.push(attempt);
    grouped.set(attempt.course_id, rows);
  }

  const output = [];
  for (const rows of grouped.values()) {
    const replacement = [...rows].reverse().find(row => REPEAT_REPLACEMENT_CODES.has(row.credit_code));
    const effective = replacement || rows[rows.length - 1];
    output.push({
      course_id: effective.course_id,
      department: effective.department,
      course_number: effective.course_number,
      title: effective.title,
      units: effective.units,
      grade: effective.grade,
      grade_points: effective.grade_points,
      credit_code: effective.credit_code,
      effective_term: effective.effective_term,
      confidence: effective.confidence,
      _repeat_applied: rows.some(row => REPEAT_CODES.has(row.credit_code)),
    });
  }
  return output;
}

export function parseUciTranscriptLines(inputLines) {
  const lines = (inputLines || []).map(cleanLine);
  const fullText = lines.join('\n');
  const hasUnofficialMarker = /THIS IS NOT AN OFFICIAL TRANSCRIPT/i.test(fullText);
  const hasRequirements = /University Requirements/i.test(fullText);
  const hasTotals = /(?:Term Totals|UC GPA|TOTAL UNITS PASSED)/i.test(fullText);
  if (!hasUnofficialMarker || !hasRequirements || !hasTotals) {
    throw new Error('This PDF does not match the supported current UCI unofficial transcript format.');
  }

  const attempts = [];
  const localIssues = [];
  let currentTerm = null;
  let skippedCount = 0;
  for (const raw of lines) {
    const line = raw.trimEnd();
    const trimmed = line.trim();
    if (TERM_RE.test(trimmed)) {
      currentTerm = trimmed;
      continue;
    }
    if (!currentTerm) continue;
    if (/^(?:Term|Cumulative) Totals/i.test(trimmed)) continue;
    if (/^(?:INCOMPLETE|NR|P\/NP|S\/U|W) GRADES:/i.test(trimmed)) {
      currentTerm = null;
      continue;
    }
    const match = line.match(COURSE_RE);
    if (!match) {
      if (looksLikeUnparsedCourse(line)) {
        skippedCount += 1;
        localIssues.push({
          course_id: sanitizedCandidateCourseId(line),
          reason_code: 'parser_unrecognized',
          message: 'A course-like transcript row could not be parsed locally.',
          count: 1,
          level: 'warning',
        });
      }
      continue;
    }
    const [, title, department, number, units, grade, points, creditCode] = match;
    attempts.push({
      course_id: canonicalCourseId(department, number),
      department: department.trim().toUpperCase().replace(/\s+/g, ' '),
      course_number: number.trim().toUpperCase(),
      title: title.trim(),
      units: Number(units),
      grade: grade.toUpperCase(),
      grade_points: Number(points),
      credit_code: creditCode ? creditCode.toUpperCase() : null,
      effective_term: trimmed ? currentTerm : null,
      confidence: 1,
    });
  }

  const courses = resolveEffectiveCourses(attempts).map(({_repeat_applied, ...course}) => course);
  return {
    parser_version: TRANSCRIPT_PARSER_VERSION,
    printed_at: parsePrintedAt(lines),
    courses,
    exam_credits: parseExamCredits(lines),
    transfer_credits: parseTransferCredits(lines),
    university_requirements: parseRequirements(lines),
    summary: parseSummary(lines),
    skipped_count: skippedCount,
    local_issues: localIssues,
  };
}

export function reconstructPdfLines(pages) {
  const lines = [];
  for (const pageItems of pages || []) {
    const groups = [];
    for (const item of pageItems || []) {
      const text = String(item.str || '');
      if (!text.trim()) continue;
      const x = Number(item.transform?.[4] || 0);
      const y = Number(item.transform?.[5] || 0);
      const width = Number(item.width || 0);
      let group = groups.find(candidate => Math.abs(candidate.y - y) <= 2.5);
      if (!group) {
        group = {y, items: []};
        groups.push(group);
      }
      group.items.push({text, x, width});
    }
    groups.sort((a, b) => b.y - a.y);
    for (const group of groups) {
      group.items.sort((a, b) => a.x - b.x);
      let text = '';
      let right = null;
      for (const item of group.items) {
        if (right !== null) {
          const gap = item.x - right;
          text += gap > 12 ? '   ' : ' ';
        }
        text += item.text;
        right = Math.max(item.x + item.width, item.x);
      }
      lines.push(cleanLine(text));
    }
  }
  return lines;
}

export function passedCourseIds(payload) {
  return (payload?.courses || [])
    .filter(course => !NON_PASSING.has(String(course.grade || '').toUpperCase()))
    .map(course => course.course_id);
}
