/* Stitch-style recommendation card. Consumes the schema produced by
   the `propose_recommendation` agent tool (app/agent/tools.py):
       {course_id, title, units, category, priority, reason,
        prereq_met, prereq_missing[], prereq_required[],
        sections: [...], grade_distribution: {avg_gpa, ...},
        ge_category, found}
   Falls through gracefully on missing fields — found=false renders
   a faded stub, missing sections/grade just hide their badges. */
const CATEGORY_LABELS = {
  core:      'Core',
  practical: 'Practical',
  career:    'Career',
  advanced:  'Advanced',
  elective:  'Elective',
};

const SOURCE_BADGE_LABELS = {
  db_verified: 'DB Verified',
  uci_catalog: 'UCI Catalog',
  uci_websoc: 'UCI WebSoc',
  registrar_websoc: 'UCI WebSoc',
  official_uci: 'Official UCI',
  official_web: 'Official Web',
  live_anteater_websoc: 'Live WebSoc',
  last_known_live: 'Cached WebSoc',
  anteater_courses: 'Anteater Courses',
  anteater_programs: 'Anteater Programs',
  student_declaration: 'Your profile',
  websoc_comments: 'WebSoc Comments',
  official_department_link: 'Official Department Link',
  local_not_live: 'Not Live',
  external_web: 'External Web',
  anecdotal: 'Anecdotal',
  unverified: 'Unverified',
};

function _catSlug(card) {
  const c = (card?.category || 'elective').toLowerCase();
  return CATEGORY_LABELS[c] ? c : 'elective';
}

function _renderCardBadges(card, cat) {
  const out = [];
  out.push(
    `<span class="cc-badge cc-badge-cat-${cat}">${CATEGORY_LABELS[cat]}</span>`
  );
  if ((card?.priority || '').toLowerCase() === 'high') {
    out.push(`<span class="cc-badge cc-badge-priority-high">High Priority</span>`);
  }
  if (card?.ge_category) {
    out.push(`<span class="cc-badge">${escHTML(card.ge_category)}</span>`);
  }
  const gpa = card?.grade_distribution?.avg_gpa;
  if (typeof gpa === 'number' && gpa > 0) {
    out.push(`<span class="cc-badge cc-badge-success">Avg GPA ${gpa.toFixed(1)}</span>`);
  }
  return out.join('');
}

/* Dynamic prefilter metadata is opt-in on each backend payload. Ranking
   remains a backend calculation; these scores never alter raw grade charts. */
function _renderDynamicPrefilterMetadata(payload, options = {}) {
  const metadata = payload?.prefilter;
  const enabled = metadata?.enabled === true
    || (metadata == null && options.enabled === true);
  if (!enabled) return '';
  const zh = typeof currentResponseLanguage === 'string' && /^zh/i.test(currentResponseLanguage);
  const score = payload?.ranking?.final_score;
  const hasScore = typeof score === 'number' && Number.isFinite(score) && score >= 0 && score <= 100;
  const scoreLabel = options.primary
    ? (zh ? '主讲分' : 'Lecturer score') : (zh ? '推荐分' : 'Recommendation score');
  const explanation = zh
    ? '按可用历史成绩与教授评价、样本量计算的推荐分，不是预计成绩或 A 率。修读条件另行核实。'
    : 'Based on available grade and professor-rating evidence, adjusted for sample size. This is not an expected grade or A rate; enrollment conditions are assessed separately.';
  const warnings = [];
  const seen = new Set();
  for (const warning of (Array.isArray(metadata?.warnings) ? metadata.warnings : [])) {
    const message = typeof warning?.message === 'string' ? warning.message.trim() : '';
    const scope = [payload?.course_id, payload?.term, warning?.section_code || warning?.primary_code || '',
      warning?.code || message].join('|');
    if (!message || seen.has(scope)) continue;
    seen.add(scope);
    warnings.push(message);
  }
  if (metadata?.status === 'needs_verification' && warnings.length === 0) {
    warnings.push(zh ? '选课条件仍需核实。' : 'Enrollment conditions still need verification.');
  }
  if (!hasScore && warnings.length === 0) return '';
  return `<div class="cc-prefilter-metadata">
    ${hasScore ? `<span class="cc-badge cc-prefilter-score" title="${_gradeAttr(explanation)}">${scoreLabel} ${score.toFixed(1)}/100</span>` : ''}
    ${warnings.length ? `<ul class="cc-prefilter-warnings">${warnings.map(message => `<li>${escHTML(message)}</li>`).join('')}</ul>` : ''}
  </div>`;
}

/* Raw historical outcomes share one renderer for recommendations and
   comparison turns. Ranking adjustments never enter this chart. */
function _gradeText(value) {
  return typeof value === 'string' ? value : '';
}

function _gradeAttr(value) {
  return escAttr(value).replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function _gradeLabels(language) {
  const lang = language || (typeof currentResponseLanguage === 'string' ? currentResponseLanguage : 'en');
  return /^zh/i.test(lang) ? {
    history: '历史给分', comparison: '历史给分比较', allCourses: '教授全部课程',
    allInstructors: '课程总体历史', fallback: '教授全部课程参考，不代表本课程的给分分布。',
    total: '成绩记录总数', graded: 'A–F 成绩记录', aRate: '排名使用的原始 A 率（A ÷ A–F）',
    gpa: 'Zotistics 报告 GPA', noGpa: '未提供', estimate: '分档估算 GPA',
    range: '实际历史范围', noRange: '范围未知', updated: '源成绩更新至',
    raw: '柱高以全部成绩记录为分母（含 P/NP）；数字为原始记录数。纵轴为 0–100%。', wIncluded: ' 含 W 记录。',
    noAF: '没有 A–F 成绩记录，A 率不可用。', approximate: '按 A=4、B=3、C=2、D=1、F=0 估算，不含 +/- 或 P/NP。',
    unavailable: '历史成绩暂时不可用', noData: '没有匹配的历史成绩记录',
    ambiguous: '教授姓名尚未明确', invalid: '历史查询无法确认', source: '查看 Zotistics',
  } : {
    history: 'Historical grades', comparison: 'Historical grade comparison', allCourses: 'All courses by this instructor',
    allInstructors: 'Course-wide history', fallback: 'Instructor history across all courses; this is not a distribution for this course.',
    total: 'Total grade records', graded: 'A–F grade records', aRate: 'Raw A rate for ranking (A ÷ A–F)',
    gpa: 'Zotistics reported GPA', noGpa: 'Unavailable', estimate: 'Bucket GPA estimate',
    range: 'Actual historical range', noRange: 'Range unavailable', updated: 'Source grades updated through',
    raw: 'Bars use all grade records, including P/NP; counts are shown below. Scale: 0–100%.', wIncluded: ' W included.',
    noAF: 'No A–F records; A rate unavailable.', approximate: 'Estimated with A=4, B=3, C=2, D=1, F=0; no +/- or P/NP.',
    unavailable: 'Historical grades temporarily unavailable', noData: 'No matching historical grade records',
    ambiguous: 'Instructor name needs clarification', invalid: 'Historical query could not be verified', source: 'View Zotistics',
  };
}

function _isZotisticsHistory(evidence) {
  return _gradeText(evidence?.source).toLowerCase() === 'zotistics';
}

function _gradeChartData(evidence) {
  if (!_isZotisticsHistory(evidence) || evidence.found !== true) return null;
  const grades = evidence.grades;
  const counts = grades?.letter_counts;
  if (!counts || typeof counts !== 'object' || Array.isArray(counts)) return null;
  const letters = ['A', 'B', 'C', 'D', 'F', 'P', 'NP'];
  if (Object.prototype.hasOwnProperty.call(counts, 'W')) letters.push('W');
  if (letters.some(l => !Number.isSafeInteger(counts[l]) || counts[l] < 0)) return null;
  const samples = letters.reduce((total, l) => total + counts[l], 0);
  const graded = letters.slice(0, 5).reduce((total, l) => total + counts[l], 0);
  if (!Number.isSafeInteger(samples) || samples <= 0) return null;
  // A contradictory envelope cannot supply a meaningful denominator.
  if (grades.samples != null && grades.samples !== samples) return null;
  if (grades.graded != null && grades.graded !== graded) return null;
  return {
    letters, counts, samples, graded,
    aRate: graded > 0 ? counts.A / graded * 100 : null,
    percentages: Object.fromEntries(letters.map(l => [l, counts[l] / samples * 100])),
  };
}

function _gradeHistoryContext(evidence, options, labels) {
  const grades = evidence?.grades || {};
  const instructor = _gradeText(options.instructor) || _gradeText(evidence.instructor)
    || _gradeText(evidence.instructor_name) || _gradeText(grades.instructor_name);
  const course = _gradeText(options.courseId) || _gradeText(evidence.course_id)
    || _gradeText(evidence.query?.course_id) || _gradeText(grades.course_id);
  const scope = _gradeText(evidence.scope) || _gradeText(grades.scope);
  const instructorWide = scope === 'instructor' || scope === 'instructor_all_courses';
  const context = instructorWide ? [instructor, labels.allCourses]
    : instructor ? [instructor, course] : [course, labels.allInstructors];
  return {label: context.filter(Boolean).join(' · '), instructorWide, course};
}

function _renderGradeChartBody(evidence, options = {}) {
  const labels = _gradeLabels(options.language);
  const data = _gradeChartData(evidence);
  const context = _gradeHistoryContext(evidence, options, labels);
  const source = `<a class="grade-chart-source" href="https://zotistics.com/" target="_blank" rel="noopener noreferrer">${labels.source}</a>`;
  if (!data) {
    const status = evidence?.status;
    const message = status === 'no_data' || status === 'not_found' ? labels.noData
      : status === 'ambiguous_instructor' ? labels.ambiguous
      : status === 'invalid_query' ? labels.invalid : labels.unavailable;
    return `<div class="grade-history-unavailable"><p>${message}</p>${source}</div>`;
  }
  const grades = evidence.grades;
  const coverage = {...(evidence.coverage || {}), ...(grades.coverage || {})};
  const range = grades.term_range || evidence.term_range || coverage.term_range || {};
  const first = _gradeText(range.first);
  const last = _gradeText(range.last);
  const rangeLabel = [first, last !== first ? last : ''].filter(Boolean).join(' – ') || labels.noRange;
  const validGpa = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 4;
  const sourceGpa = validGpa(grades.source_reported_gpa) ? grades.source_reported_gpa.toFixed(3) : labels.noGpa;
  const estimate = validGpa(grades.avg_gpa) ? grades.avg_gpa.toFixed(3) : null;
  const aLabel = data.aRate !== null ? `${data.aRate.toFixed(1)}%` : labels.noGpa;
  const accessible = [context.label, ...data.letters.map(l => `${l}: ${data.counts[l]} (${data.percentages[l].toFixed(1)}%)`)].join(' · ');
  const columns = data.letters.map(l => {
    const percentage = data.percentages[l];
    return `<div class="grade-chart-column" data-grade="${l}" data-count="${data.counts[l]}"
                 data-percentage="${percentage}" title="${l}: ${data.counts[l]} (${percentage.toFixed(1)}%)">
      <span class="grade-chart-percent">${percentage.toFixed(1)}%</span>
      <div class="grade-chart-track"><span class="grade-chart-bar" style="height:${percentage}%"></span></div>
      <span class="grade-chart-letter">${l}</span><span class="grade-chart-count">${data.counts[l]}</span>
    </div>`;
  }).join('');
  return `<figure class="grade-chart">
    <div class="grade-chart-bars" role="img" aria-label="${_gradeAttr(accessible)}"
         style="--grade-columns:${data.letters.length}"><div class="grade-chart-grid" aria-hidden="true"></div>
      <div class="grade-chart-columns" aria-hidden="true">${columns}</div>
    </div>
    <figcaption>
      <p class="grade-chart-denominator">${labels.raw}${data.letters.includes('W') ? labels.wIncluded : ''}</p>
      ${context.instructorWide && context.course ? `<p class="grade-chart-scope-note">${labels.fallback}</p>` : ''}
      <dl class="grade-chart-metrics">
        <div><dt>${labels.total}</dt><dd>${data.samples}</dd></div>
        <div><dt>${labels.graded}</dt><dd>${data.graded}</dd></div>
        <div><dt>${labels.aRate}</dt><dd>${aLabel}</dd></div>
        <div><dt>${labels.gpa}</dt><dd>${sourceGpa}</dd></div>
      </dl>
      ${data.graded === 0 ? `<p class="grade-chart-note">${labels.noAF}</p>` : ''}
      ${sourceGpa === labels.noGpa && estimate !== null && data.graded > 0
        ? `<p class="grade-chart-note">${labels.estimate} ≈ ${estimate}. ${labels.approximate}</p>` : ''}
      <p class="grade-chart-range">${labels.range}: ${escHTML(rangeLabel)}</p>
      ${_gradeText(coverage.last_grade_update) ? `<p class="grade-chart-note">${labels.updated}: ${escHTML(coverage.last_grade_update)}</p>` : ''}
      ${source}
    </figcaption>
  </figure>`;
}

function _renderZotisticsHistory(evidence, instructor, courseId, language) {
  if (!_isZotisticsHistory(evidence)) return '';
  const labels = _gradeLabels(language);
  const options = {instructor, courseId, language};
  const context = _gradeHistoryContext(evidence, options, labels);
  return `<details class="grade-history-details">
    <summary>${labels.history}<span class="grade-history-source-label">Zotistics</span></summary>
    <div class="grade-history-content">
      <p class="grade-history-context">${escHTML(context.label)}</p>
      ${_renderGradeChartBody(evidence, options)}
    </div>
  </details>`;
}

/* Comparison charts are native structured UI, open on first render. */
function renderGradeChartsBlock(entries, language) {
  if (!Array.isArray(entries)) return '';
  const histories = entries.filter(e => e && typeof e === 'object' && _isZotisticsHistory(e));
  if (!histories.length) return '';
  const labels = _gradeLabels(language);
  return `<section class="grade-comparison" aria-label="${_gradeAttr(labels.comparison)}">
    <h3>${labels.comparison}</h3>
    <div class="grade-comparison-grid">${histories.map(e => {
      const options = {instructor: e.instructor, courseId: e.course_id, language};
      const context = _gradeHistoryContext(e, options, labels);
      return `<article class="grade-comparison-item">
        <h4>${escHTML(context.label)}</h4>${_renderGradeChartBody(e, options)}
      </article>`;
    }).join('')}</div>
  </section>`;
}

function renderGradeChartsForMessage(wrap, entries) {
  const html = renderGradeChartsBlock(entries, wrap._responseLanguage);
  const existing = wrap.querySelector('.grade-comparison');
  if (existing) existing.remove();
  if (!html) return;
  const block = document.createElement('div');
  block.innerHTML = html;
  if (block.firstElementChild) wrap.appendChild(block.firstElementChild);
}

function _renderCardSourceBadges(card) {
  // Field-level provenance stays separate from core DB fields
  // (`course_source`, `course_provenance`, `section_source`) so web
  // evidence never masquerades as DB verified. Section-level live
  // sources can also surface here when a card includes live sections.
  const explicit = [
    ...(Array.isArray(card?.field_source_badges) ? card.field_source_badges : []),
    ...(Array.isArray(card?.source_badges) ? card.source_badges : []),
  ];
  const sectionSources = [];
  if (card?.section_source) sectionSources.push(card.section_source);
  for (const section of (card?.sections || [])) {
    if (section?.source) sectionSources.push(section.source);
  }
  const badges = [...explicit, ...sectionSources];
  const seen = new Set();
  const sourceHTML = badges.map((raw) => {
    const source = (typeof raw === 'string' ? raw : raw?.kind || raw?.source_class || '')
      .toLowerCase();
    if (!source || source === 'prefilter_metadata') return '';
    const key = source === 'registrar_websoc' ? 'uci_websoc' : source;
    const canonical = SOURCE_BADGE_LABELS[key] ? key : 'unknown';
    if (seen.has(canonical)) return '';
    seen.add(canonical);
    const zh = typeof currentResponseLanguage === 'string' && /^zh/i.test(currentResponseLanguage);
    const label = SOURCE_BADGE_LABELS[key] || (zh ? '来源待识别' : 'Source unknown');
    const timestamps = [...new Set([card?.section_retrieved_at,
      ...(card?.sections || []).filter(section => {
        const actual = section?.source === 'registrar_websoc' ? 'uci_websoc' : section?.source;
        return actual === key;
      }).map(section => section?.retrieved_at)].filter(value => typeof value === 'string' && value))];
    const freshness = timestamps.length ? `${zh ? '获取于' : 'Retrieved'} ${timestamps.join(', ')}` : '';
    return `<span class="cc-badge cc-source-badge cc-source-${escAttr(canonical)}"${freshness ? ` title="${_gradeAttr(freshness)}"` : ''}>${escHTML(label)}</span>`
      + (freshness ? `<span class="cc-source-freshness">${escHTML(freshness)}</span>` : '');
  }).join('');
  const ge = card?.ge_evidence;
  const geSource = ge?.source;
  const url = geSource?.source_url;
  const officialGE = ge?.status === 'verified' && geSource?.source === 'uci_catalogue'
    && /^\d{4}-\d{2}$/.test(geSource?.catalogue_year || '')
    && typeof url === 'string' && /^https:\/\/catalogue\.uci\.edu\//.test(url);
  return sourceHTML + (officialGE ? `<a class="cc-badge cc-source-badge cc-source-uci_catalog" href="${_gradeAttr(url)}" target="_blank" rel="noopener noreferrer">UCI GE ${escHTML(geSource.catalogue_year)}</a>` : '');
}

/* Section enrollment row: the primary (Lec) code as the headline
   chip, and — when the course requires a paired Discussion / Lab /
   Studio — a smaller "+ Dis required" reminder pill. WebReg rejects
   schedules that have only the Lec without a paired secondary, so
   this isn't a hint, it's a hard rule (policies.ENROLLMENT_RULES).
   The pill's tooltip lists the bookable secondary codes. */
function _renderCodeRow(card) {
  const codes = card?.enrollment_codes || [];
  // Primary first (dispatcher orders enrollment_codes[0] = primary).
  const primary = (card.primary_code ? codes.find(section => String(section.code) === String(card.primary_code)) : codes[0]) || null;
  // Legacy non-agent path: fall back to first raw section.
  const fallback = primary ? null : (card.primary_code
    ? (card?.sections || []).find(section => String(section.section_code || section.code) === String(card.primary_code))
    : (card?.sections || [])[0]);
  const code = primary?.code || fallback?.section_code;
  if (!code) return '';
  const fullSections = [...(card?.sections || []), ...(card?.section_groups || []).flatMap(
    group => [group.primary, ...(group.secondaries || [])])].filter(Boolean);
  const fullPrimary = fullSections.find(section => String(section.code || section.section_code) === String(code));
  const meta = {...(primary || fallback || {}), ...(fullPrimary || {})};
  const tip = [meta.type || meta.section_type,
               _sectionDaysTimeText(meta),
               (meta.instructors || [])[0]]
              .filter(Boolean).join(' · ');

  let secondaryPill = '';
  const pairing = _cardPairing(card);
  if (pairing) {
    const verified = pairing.evidence?.status === 'verified';
    const requirement = verified ? pairing.requirement : 'unknown';
    const zh = typeof currentResponseLanguage === 'string' && /^zh/i.test(currentResponseLanguage);
    const label = requirement === 'required'
      ? (zh ? `需配 ${_pairingComponentLabel(pairing, zh)}` : `+ ${_pairingComponentLabel(pairing, zh)} required`)
      : requirement === 'not_required'
        ? (_usesDiscussionPolicy(pairing) ? (zh ? '无需选 Dis' : 'No discussion required') : (zh ? '无需附加配对' : 'No secondary required'))
        : (zh ? '配对待核实' : 'Pairing unverified');
    const codes = (pairing.allowed_bundles || []).flatMap(bundle => Array.isArray(bundle) ? bundle : bundle.secondary_codes || []);
    const allowed = [...new Set(codes)];
    const details = requirement === 'required' && allowed.length
      ? `${zh ? '可配班次' : 'Allowed sections'}: ${allowed.join(', ')}` : '';
    const missing = requirement === 'required' && card.selection_verification?.status !== 'satisfied';
    secondaryPill = `<span class="cc-badge cc-pairing-status" data-pairing="${escHTML(requirement)}" title="${_gradeAttr(details)}">${escHTML(label)}</span>`
      + (missing ? `<span class="cc-pairing-selection">${zh ? '尚未选择完整配套班次' : 'Complete secondary selection needed'}</span>` : '');
  } else if (card?.requires_secondary) {
    const secs = card?.secondary_codes || [];
    const type = card?.secondary_type || 'Dis';
    // Tooltip lists each bookable secondary so the student can see
    // their options without rendering 10 chips on the card itself.
    const secTip = secs.slice(0, 12).map(s => {
      const inst = (s.instructors || [])[0];
      const full = fullSections.find(section => String(section.code || section.section_code) === String(s.code));
      return [s.code, s.type, _sectionDaysTimeText({...s, ...(full || {})}), inst].filter(Boolean).join(' · ');
    }).join('\n');
    const overflow = secs.length > 12 ? `\n…and ${secs.length - 12} more` : '';
    secondaryPill = `<span class="cc-code-secondary"
                           title="Must also enroll in a ${escAttr(type)}:\n${escAttr(secTip + overflow)}">
      + ${escHTML(type)} required
    </span>`;
  }

  return `<div class="cc-codes">
    <span class="cc-code cc-code-primary"
          title="${escAttr(tip || '')}">${escHTML(code)}</span>
    ${secondaryPill}
  </div>`;
}

function _cardPairing(card) {
  const current = _savedCardPairing(card);
  const pair = current?.pairing || card?.pairing;
  if (pair?.version === 1) return pair;
  if (pair?.version !== 2) return null;
  const raw = pair.per_primary?.[String(card.primary_code)];
  if (!raw) return {requirement: 'unknown', evidence: {status: 'unverified'}};
  const types = raw.required_types || [...new Set((raw.required_components || []).flatMap(component => component.types || []))];
  return {...raw, required_types: types};
}

function _usesDiscussionPolicy(pairing) {
  return pairing?.evidence?.policy_id === 'user_confirmed_discussion_presence'
    && pairing.evidence.policy_version === 1 && pairing.evidence.user_confirmed === true;
}

function _savedCardPairing(card) {
  if (typeof scheduleValidation === 'undefined' || !Array.isArray(scheduleValidation?.pairing)) return null;
  if (!addedCourses.has(scheduleCourseKey(card.term, card.course_id))) return null;
  const termKey = value => typeof canonicalTerm === 'function' ? canonicalTerm(value) : value;
  return scheduleValidation.pairing.find(row => row.course_id === card.course_id && termKey(row.term) === termKey(card.term)) || null;
}

function _renderCoursePairingSummary(card) {
  if (card?.pairing?.version !== 2) return '';
  const proof = _cardPairing(card);
  const zh = typeof currentResponseLanguage === 'string' && /^zh/i.test(currentResponseLanguage);
  const current = _savedCardPairing(card);
  const selection = current ? current.selection_verification : card.selection_verification;
  const samePrimary = !current || String(selection?.primary_code) === String(card.primary_code);
  const satisfied = samePrimary && selection?.status === 'satisfied';
  let text;
  if (proof?.evidence?.status !== 'verified' || proof.requirement === 'unknown') {
    text = zh ? '此主班的配对要求待核实。' : 'Pairing requirements for this primary remain unverified.';
  } else if (proof.requirement === 'not_required') {
    text = _usesDiscussionPolicy(proof)
      ? (zh ? '讨论课：无需选 Dis；其他修读条件分开判断。' : 'No discussion required; other enrollment conditions are assessed separately.')
      : (zh ? '已确认此主班无需附加配对；其他修读条件另行核实。' : 'No secondary required for this primary; other enrollment conditions remain separate.');
  } else {
    text = (zh ? `配套要求：${_pairingComponentLabel(proof, zh)}。` : `Required components: ${_pairingComponentLabel(proof, zh)}. `)
      + (satisfied ? (zh ? '当前选择已满足配对要求。' : 'The current selection satisfies pairing requirements.')
        : (zh ? '尚未选择完整配套班次。' : 'Complete secondary selection needed.'));
    if (proof.relationship === 'unknown') text += zh ? '可配班次待核实。' : 'Compatible sections remain unverified.';
    const linked = (proof.required_course_components || []).map(item => item?.course_id).filter(Boolean);
    if (linked.length) text += (zh ? '关联 co-class：' : ' Linked co-class: ') + [...new Set(linked)].join(' / ');
  }
  return `<p class="cc-pairing-summary">${escHTML(text)}</p>`;
}

function _pairingComponentLabel(pairing, zh) {
  const components = pairing.required_components;
  if (!Array.isArray(components) || !components.length) return (pairing.required_types || []).join(' + ');
  return components.map(component => {
    const types = (component.types || []).join(zh ? ' 或 ' : ' or ');
    return (component.min === 1 && component.max === 1) ? types : `${types} (${component.min}–${component.max})`;
  }).join(' + ');
}

/* Per-term deadline footer.
   Renders a small monospace strip beneath the badges/reason showing
   the term's free-window end (last day of WebReg self-service) and
   the late add/drop end (last day to drop with dean approval — i.e.
   the practical "drop without W" deadline). Hidden if the card
   doesn't carry term_deadlines (legacy non-agent path). */
function _renderDeadlineRow(card) {
  const d = card?.term_deadlines || null;
  if (!d) return '';
  const fmt = (iso) => {
    if (!iso) return null;
    // 2026-10-09 → "Oct 9". Plain Date parsing is reliable for
    // YYYY-MM-DD; just guard against future schema drift.
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
    if (!m) return iso;
    const months = ['Jan','Feb','Mar','Apr','May','Jun',
                    'Jul','Aug','Sep','Oct','Nov','Dec'];
    return `${months[parseInt(m[2],10)-1]} ${parseInt(m[3],10)}`;
  };
  const pieces = [];
  if (d.free_window_end) {
    pieces.push(`<span title="Last day to add/drop/change without dean approval (WebReg)">
      Free add/drop · <b>${escHTML(fmt(d.free_window_end))}</b>
    </span>`);
  }
  if (d.late_add_drop_end) {
    pieces.push(`<span title="Last day to drop with dean approval (via Enrollment Exceptions in Student Access). After this date a drop becomes a W on the transcript.">
      Drop w/o W · <b>${escHTML(fmt(d.late_add_drop_end))}</b>
    </span>`);
  }
  if (!pieces.length) return '';
  return `<div class="cc-deadlines">${pieces.join('<span class="cc-deadlines-sep">·</span>')}</div>`;
}

/* Section-groups sub-cards — one sub-card per Lec letter group,
   laid out side-by-side (wraps on narrow screens). Hidden by
   default; toggleable via the "Show sections" chevron on the card.

   Each sub-card carries:
     · Lec letter badge + 5-digit registrar code
     · Professor name + RMP rating chip (star + score)
     · Days + meeting time
     · Classroom
     · Final-exam time + room
     · Paired Dis/Lab chip strip (codes + status badges)

   Each card holds its own collapse state so multiple recommendation
   cards can be expanded independently. */
function _renderSectionGroups(card) {
  const groups = card?.section_groups || [];
  if (groups.length === 0) return '';
  // Collapsed by default — `.open` is added by toggleSectionGroups()
  // or by cardClickHandler() when the user taps the card body.
  // No `hidden` attribute so CSS can animate the open/close transition.
  const cid = card?.course_id || '';
  const term = card?.term || currentTermContext?.term || '';
  return `<div class="cc-sg-wrap">
    <div class="cc-sg-cards">
      ${groups.map(g => _renderOneSectionGroup(g, cid, term, card?.prefilter?.enabled === true)).join('')}
    </div>
  </div>`;
}

function _renderOneSectionGroup(g, courseId, term, prefilterEnabled = false) {
  const p = g.primary;
  if (!p) {
    const zh = typeof currentResponseLanguage === 'string' && /^zh/i.test(currentResponseLanguage);
    const orphanLabel = g.relationship === 'unknown'
      ? (zh ? '附属班次，配对关系待核实' : 'Unassigned sections · pairing unverified') : 'Lec unavailable';
    return `<div class="cc-sg-card cc-sg-orphan">
      <div class="cc-sg-card-head">
        <span class="cc-sg-letter">${escHTML(g.letter || '?')}</span>
        <em>${escHTML(orphanLabel)}</em>
      </div>
      <div class="cc-sg-secs-block">
        ${_renderSecondaryChips(g.secondaries, courseId, term)}
      </div>
    </div>`;
  }

  const code     = escHTML(p.code || '');
  const letter   = escHTML(p.num || p.type || g.letter);
  const time     = _fmtSectionDaysTime(p);
  const room     = _fmtSectionLocations(p);
  const status   = _renderSeatBadge(p);
  const profs    = _renderProfRow(p.ratings, p.instructors, p.grade_history, courseId);
  const finalExam = _renderFinalExam(p.final_exam);

  const lecNum = p.num || g.letter || '?';
  const addBtn = courseId ? _renderAddBtn(courseId, lecNum, {term}) : '';

  return `<div class="cc-sg-card">
    <div class="cc-sg-card-head">
      <span class="cc-sg-letter">${letter}</span>
      <span class="cc-sg-code">${code}</span>
      ${status}
      <span class="cc-sg-spacer"></span>
      ${addBtn}
    </div>
    ${profs}${_renderDynamicPrefilterMetadata(p, {primary: true, enabled: prefilterEnabled})}
    ${p.pairing_selected ? _renderPrimaryPairing(p.pairing_selected, g.secondaries) : ''}
    <dl class="cc-sg-facts">
      <div class="cc-sg-fact">
        <span class="material-symbols-outlined">${solonIcon('schedule')}</span>
        <dd>${time}</dd>
      </div>
      <div class="cc-sg-fact">
        <span class="material-symbols-outlined">${solonIcon('location_on')}</span>
        <dd>${room}</dd>
      </div>
      ${finalExam}
    </dl>
    ${g.secondaries.length === 0
      ? ''
      : `<div class="cc-sg-secs-block">
           <div class="cc-sg-secs-label">${escHTML(g.secondaries[0]?.type || 'Dis')} sections</div>
           ${_renderSecondaryChips(g.secondaries, courseId, term)}
         </div>`}
  </div>`;
}

function _renderPrimaryPairing(pairing, sections) {
  const zh = typeof currentResponseLanguage === 'string' && /^zh/i.test(currentResponseLanguage);
  const requirement = pairing.evidence?.status === 'verified' ? pairing.requirement : 'unknown';
  const label = requirement === 'required'
    ? (zh ? `需配 ${_pairingComponentLabel(pairing, zh)}` : `Required: ${_pairingComponentLabel(pairing, zh)}`)
    : requirement === 'not_required' ? (_usesDiscussionPolicy(pairing)
      ? (zh ? '无需选 Dis' : 'No discussion required') : (zh ? '无需附加配对' : 'No secondary required'))
      : (zh ? '配对条件待核实' : 'Pairing unverified');
  const choices = requirement === 'required' ? (sections || []).map(section => section.num || section.code).join(', ') : '';
  return `<div class="cc-primary-pairing"><span class="cc-badge">${escHTML(label)}</span>`
    + (choices ? `<span>${zh ? '可选' : 'Options'}: ${escHTML(choices)}</span>` : '') + '</div>';
}

function _sectionClockText(value) {
  if (value && typeof value === 'object') {
    const {hour, minute} = value;
    return Number.isInteger(hour) && Number.isInteger(minute) && hour >= 0 && hour <= 23 && minute >= 0 && minute <= 59
      ? `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}` : null;
  }
  return typeof value === 'string' ? value.trim() : null;
}

function _sectionClockMinutes(value) {
  const match = /^(\d{1,2}):(\d{2})\s*([AP]M?)?$/i.exec(value || '');
  if (!match) return null;
  let hour = Number(match[1]);
  const minute = Number(match[2]);
  if (minute > 59 || hour > 23 || match[3] && (hour < 1 || hour > 12)) return null;
  if (match[3]) hour = hour % 12 + (/^p/i.test(match[3]) ? 12 : 0);
  return hour * 60 + minute;
}

/* Shared by cards and the calendar. An explicit empty/broken meetings array
   is unknown evidence; only absent/None arrays may use legacy flat fields. */
function sectionMeetingsForDisplay(section) {
  const raw = section?.meetings;
  const rows = raw == null ? [section || {}] : Array.isArray(raw) && raw.length ? raw : [{}];
  return rows.map(value => {
    const row = value && typeof value === 'object' && !Array.isArray(value) ? value : {};
    const dayText = typeof row.days === 'string' ? row.days.replace(/[ ,/\-\s]/g, '') : '';
    const dayTokens = dayText.match(/Tu|Th|Sa|Su|M|W|F/g) || [];
    const days = dayTokens.length && [...new Set(dayTokens)].join('') === dayText ? dayText : null;
    const startText = _sectionClockText(Object.hasOwn(row, 'start_time') ? row.start_time : row.startTime);
    const endText = _sectionClockText(Object.hasOwn(row, 'end_time') ? row.end_time : row.endTime);
    let start = _sectionClockMinutes(startText);
    const end = _sectionClockMinutes(endText);
    // Registrar ranges share the end's AM/PM suffix, e.g. 3:30–4:50p.
    if (start !== null && end !== null && /[ap](?:m)?$/i.test(endText || '') && !/[ap](?:m)?$/i.test(startText || '')) {
      const hour = Number(startText.split(':')[0]);
      if (hour >= 1 && hour <= 12) {
        const base = start % 720;
        const candidates = [base, base + 720].filter(minute => minute < end);
        start = candidates.length ? Math.max(...candidates) : null;
      }
    }
    const validWindow = start !== null && end !== null && start < end;
    const clock = minute => minute === null ? null : `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`;
    const rawLocation = Object.hasOwn(row, 'location') ? row.location : row.bldg;
    const location = Array.isArray(rawLocation) ? rawLocation.filter(value => typeof value === 'string').join(', ')
      : typeof rawLocation === 'string' ? rawLocation : null;
    const tba = /^(true|1|yes|y)$/i.test(String(row.time_is_tba)) || /^(true|1|yes|y)$/i.test(String(row.timeIsTBA))
      || [row.days, row.start_time, row.end_time, row.time_display].some(value => /^(TBA|TBD|UNKNOWN)$/i.test(String(value || '').trim()));
    return {days, start_time: validWindow ? clock(start) : null, end_time: validWindow ? clock(end) : null,
      location, time_is_tba: tba || !days || !validWindow};
  });
}

function _sectionDaysTimeLines(section) {
  if (section?.meetings == null) {
    const text = section?.time || section?.time_display;
    if (text) return [String(text)];
  }
  return sectionMeetingsForDisplay(section).map(meeting => [meeting.days,
    meeting.time_is_tba ? 'TBA' : `${meeting.start_time}-${meeting.end_time}`].filter(Boolean).join(' '));
}

function _sectionDaysTimeText(section) {
  return _sectionDaysTimeLines(section).join('; ');
}

function _fmtSectionDaysTime(section) {
  return _sectionDaysTimeLines(section).map(text => escHTML(text)).join('<br>');
}

function _fmtSectionLocations(section) {
  return sectionMeetingsForDisplay(section).map(meeting => meeting.location
    ? escHTML(meeting.location) : '<em>Room TBA</em>').join('<br>');
}

function _renderSeatBadge(s) {
  if (s.status === 'FULL')    return `<span class="cc-sg-badge danger">FULL</span>`;
  if (s.status === 'Waitl')   return `<span class="cc-sg-badge warning">Waitlist</span>`;
  if (s.status === 'NewOnly') return `<span class="cc-sg-badge muted">New only</span>`;
  if (typeof s.seats_open === 'number' && s.seats_open <= 5 && s.seats_open > 0) {
    return `<span class="cc-sg-badge warning">${s.seats_open} left</span>`;
  }
  return '';
}

function _renderProfRow(ratings, fallback_instructors, grade_history, courseId) {
  // ratings: [{instructor, avg_rating, num_ratings, tier, tier_label}]
  // fallback_instructors: raw strings if ratings array is missing
  const list = (ratings && ratings.length) ? ratings
                : (fallback_instructors || []).map(n => ({instructor: n}));
  if (!list.length) return '';
  return `<div class="cc-sg-profs">
    ${list.filter(p => p.instructor && p.instructor.toLowerCase() !== 'staff')
          .map(p => {
      const hasScore = typeof p.avg_rating === 'number' && p.avg_rating > 0;
      const tierClass = _rmpTierClass(p.tier, p.num_ratings);
      const star = hasScore
        ? `<span class="cc-sg-rmp ${tierClass}"
                 title="${escHTML(p.tier_label || 'RMP')} · avg ${p.avg_rating} from ${p.num_ratings || 0} ratings">
             <span class="material-symbols-outlined">${solonIcon('star')}</span>
             ${p.avg_rating.toFixed(1)}
             <span class="cc-sg-rmp-n">(${p.num_ratings || 0})</span>
           </span>`
        : `<span class="cc-sg-rmp cc-sg-rmp-na" title="No RateMyProfessor data on file">no rmp</span>`;
      // Only evidence explicitly matched to this lecturer belongs beside
      // their name. Course-wide grades never fill a professor's history.
      const history = (Array.isArray(grade_history) ? grade_history : [])
        .find(h => h?.instructor === p.instructor && h.scope !== 'course');
      // Legacy saved rows had only {instructor, grades}; new rows carry
      // the full envelope, including no-data and explicit fallback scope.
      const evidence = history && history.source == null && history.found == null
        ? {found: true, source: 'zotistics', ...history} : history;
      const gradeChart = evidence ? _renderZotisticsHistory(evidence, p.instructor, courseId) : '';
      return `<div class="cc-sg-prof">
        <span class="cc-sg-prof-name">${escHTML(p.instructor)}</span>
        ${star}
      </div>${gradeChart ? `<div class="cc-sg-prof-grade">${gradeChart}</div>` : ''}`;
    }).join('')
      || `<div class="cc-sg-prof"><span class="cc-sg-prof-name">STAFF</span></div>`}
  </div>`;
}

/* Map the prof's RMP tier slug → CSS color class.
   The slug comes from db.get_professor_rating's `tier.tier` field
   (mostly_positive / mostly_negative / mixed / insufficient_data /
   unrated). We treat insufficient sample (< 10 ratings) the same as
   the explicit insufficient_data tier so a 1-review prof doesn't
   show as bright green. Order: positive → green; negative → red;
   mixed → yellow; else gray. */
function _rmpTierClass(tier, numRatings) {
  if (numRatings != null && numRatings < 10) return 'cc-sg-rmp-insufficient';
  switch ((tier || '').toLowerCase()) {
    case 'mostly_positive': return 'cc-sg-rmp-positive';
    case 'mostly_negative': return 'cc-sg-rmp-negative';
    case 'mixed':           return 'cc-sg-rmp-mixed';
    case 'insufficient_data':
    case 'unrated':
                            return 'cc-sg-rmp-insufficient';
    default:                return 'cc-sg-rmp-mixed';   // fallback when tier missing but score present
  }
}

function _renderFinalExam(fx) {
  if (!fx) return '';
  if (fx.status === 'none') {
    return `<div class="cc-sg-fact">
      <span class="material-symbols-outlined">${solonIcon('event_busy')}</span>
      <dd>No final exam</dd>
    </div>`;
  }
  if (fx.status === 'tba') {
    return `<div class="cc-sg-fact">
      <span class="material-symbols-outlined">${solonIcon('event')}</span>
      <dd>Final: TBA</dd>
    </div>`;
  }
  const loc = fx.location ? ` · ${escHTML(fx.location)}` : '';
  return `<div class="cc-sg-fact">
    <span class="material-symbols-outlined">${solonIcon('event')}</span>
    <dd>Final: ${escHTML(fx.label || '')}${loc}</dd>
  </div>`;
}

/* Secondary (Dis / Lab / Stu) rows below each Lec.
   Each row carries the full enrollment-decision payload because
   the student picks a Dis based on time / room / instructor — a
   bare "A1 36251" doesn't help.

   Columns (mobile collapses to two flex rows):
     · section num (A1) — small letter badge
     · 5-digit code — mono
     · instructor — name only when it isn't STAFF; if the rare
       Dis-taught-by-prof case applies AND we have an RMP score,
       a tier-colored star pill rides alongside
     · meeting time — schedule icon + "F 10:00-10:50"
     · room — pin icon + location
     · status badge — FULL / Waitlist / N left when applicable */
/* Apple Store–style + → ✓ button.
   Args:
     courseId    — canonical course_id ("CS161")
     sectionNum  — section letter ("A") for Lec, letter+digit ("A1") for Dis
     opts.hoverOnly — true to apply `.cc-sg-add-hover` (Dis rows: shown only on row hover).
   The button reads its initial added state from addedSectionKeys
   so re-renders after a render-from-history don't lose the picker
   state. */
function _renderAddBtn(courseId, sectionNum, opts) {
  opts = opts || {};
  const term = opts.term || currentTermContext?.term || '';
  const key = scheduleEntryKey(term, courseId, sectionNum);
  const isAdded = addedSectionKeys.has(key);
  const classes = ['cc-sg-add'];
  if (opts.hoverOnly) classes.push('cc-sg-add-hover');
  if (isAdded)        classes.push('added');
  const aria = isAdded ? 'Remove section from schedule' : 'Add section to schedule';
  return `<button type="button" class="${classes.join(' ')}"
                  data-cid="${escHTML(courseId)}"
                  data-sec="${escHTML(sectionNum)}"
                  data-term="${escAttr(term)}"
                  onclick="event.stopPropagation(); toggleSectionAdd(this)"
                  aria-label="${aria}"
                  aria-pressed="${isAdded ? 'true' : 'false'}">
    <span class="material-symbols-outlined icon-add">${solonIcon('add')}</span>
    <span class="material-symbols-outlined icon-check">${solonIcon('check')}</span>
  </button>`;
}

function _renderSecondaryChips(secondaries, courseId, term) {
  if (!secondaries || secondaries.length === 0) return '';
  return `<table class="cc-sg-sec-table">
    <tbody>
      ${secondaries.map(s => _renderOneSecondaryRow(s, courseId, term)).join('')}
    </tbody>
  </table>`;
}

function _renderOneSecondaryRow(s, courseId, term) {
  const num   = escHTML(s.num || s.type || '?');
  const code  = escHTML(s.code || '—');
  const time  = _fmtSectionDaysTime(s);          // already escaped
  const room  = _fmtSectionLocations(s);
  const status = _renderSeatBadge(s);
  // Instructor: drop STAFF / TBA / empty; show the first real name
  // and a tier-colored RMP pill if we have a rating for them.
  const real = (s.instructors || []).filter(
    n => n && !/^(staff|tba)$/i.test(n.trim())
  );
  let instCell = '<span class="cc-sg-row-staff">TA</span>';
  if (real.length > 0) {
    const ratings = (s.ratings || []).filter(r => r.instructor === real[0]);
    const rt = ratings[0] || {};
    const hasScore = typeof rt.avg_rating === 'number' && rt.avg_rating > 0;
    const star = hasScore
      ? `<span class="cc-sg-rmp ${_rmpTierClass(rt.tier, rt.num_ratings)}"
               title="${escHTML(rt.tier_label || 'RMP')} · avg ${rt.avg_rating} from ${rt.num_ratings || 0} ratings">
           <span class="material-symbols-outlined">${solonIcon('star')}</span>
           ${rt.avg_rating.toFixed(1)}
         </span>` : '';
    instCell = `<span class="cc-sg-row-inst">${escHTML(real[0])}</span>${star}`;
  }
  const sNum = s.num || s.type || '?';
  const addBtn = courseId ? _renderAddBtn(courseId, sNum, {hoverOnly: true, term}) : '';
  return `<tr class="cc-sg-row"
              data-cid="${escHTML(courseId || '')}"
              data-sec="${escHTML(sNum)}"
              data-term="${escAttr(term || '')}"
              ondblclick="event.stopPropagation(); _toggleSectionAddByRow(this)">
    <td><span class="cc-sg-rownum">${num}</span></td>
    <td class="cc-sg-row-code">${code}</td>
    <td class="cc-sg-row-instcell">${instCell}</td>
    <td class="cc-sg-row-meta">
      <span class="material-symbols-outlined">${solonIcon('schedule')}</span>${time}
    </td>
    <td class="cc-sg-row-meta">
      <span class="material-symbols-outlined">${solonIcon('location_on')}</span>${room}
    </td>
    <td class="cc-sg-row-status">${status}</td>
    <td class="cc-sg-row-add">${addBtn}</td>
  </tr>`;
}

/* Toggle the section-groups panel for a card. Accepts either the
   toggle button (from its inline onclick) or the .course-card
   element itself (from cardClickHandler). Animates open/close via
   the .open class; the CSS does the actual transition. */
function toggleSectionGroups(originEl) {
  const card = originEl.classList?.contains('course-card')
               ? originEl
               : originEl.closest('.course-card');
  if (!card) return;
  const panel = card.querySelector('.cc-sg-wrap');
  if (!panel) return;
  const willOpen = !panel.classList.contains('open');
  panel.classList.toggle('open', willOpen);
  // Sync the explicit toggle button's chevron + aria — the button
  // may not be the element that triggered this call (could be the
  // card body), but we still want the chevron in the right state.
  const btn = card.querySelector('.cc-sg-toggle');
  if (btn) {
    btn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
    const icon = btn.querySelector('.material-symbols-outlined');
    if (icon) icon.innerHTML = solonIcon(willOpen ? 'expand_less' : 'expand_more');
  }
}

/* Card-body click → toggle the section panel. Skip clicks that
   land on a control with its own meaning so we don't fight the
   user's actual intent:
     · the floating + Add button
     · the explicit "Show sections" toggle (it routes through the
       same toggleSectionGroups; clicking either still works)
     · anything INSIDE the open section panel (selecting a section
       chip / hovering a Dis row shouldn't collapse the card)
     · standard interactive elements (button / a / input / select). */
function cardClickHandler(e) {
  // closest("...") returns the nearest matching ancestor or null.
  // If any of these match, we leave the click alone.
  // .cc-sg-add covers both Lec and Dis +/✓ buttons even though they
  // also stopPropagation in their own handler — belt and suspenders.
  const skip = e.target.closest(
    '.cc-sg-add, .cc-sg-toggle, .cc-sg-wrap, .grade-history-details, a, button, input, select, textarea'
  );
  if (skip) return;
  toggleSectionGroups(e.currentTarget);
}

/* Section-level add/remove toggle (Phase E4 picker).
   Called from each Lec or Dis + button. Optimistically flips the
   button's `.added` class so the App-Store-style + → ✓ animation
   fires immediately, then hits the backend. If the backend
   rejects, we revert the visual change. */
async function toggleSectionAdd(btn) {
  if (btn.disabled) return;
  const cid = btn.dataset.cid;
  const sec = btn.dataset.sec;
  const term = btn.dataset.term || currentTermContext?.term || '';
  if (!cid || !sec) return;
  const epoch = conversationEpoch;
  const peers = [...document.querySelectorAll('.cc-sg-add')].filter(el =>
    el.dataset.cid === cid && el.dataset.sec === sec && canonicalTerm(el.dataset.term) === canonicalTerm(term));
  peers.forEach(el => { el.disabled = true; });
  const key = scheduleEntryKey(term, cid, sec);
  const wasAdded = addedSectionKeys.has(key);
  // Optimistic flip — animation runs from CSS via the .added class.
  if (wasAdded) {
    addedSectionKeys.delete(key);
    btn.classList.remove('added');
    btn.setAttribute('aria-pressed', 'false');
    btn.setAttribute('aria-label', 'Add section to schedule');
  } else {
    addedSectionKeys.add(key);
    btn.classList.add('added');
    btn.setAttribute('aria-pressed', 'true');
    btn.setAttribute('aria-label', 'Remove section from schedule');
  }
  _syncAddedCourse(cid, term);
  try {
    if (wasAdded) {
      await removeCourse(cid, btn, sec, term);
    } else {
      await addCourse(cid, sec, btn, term);
    }
  } catch (err) {
    // Revert visual on failure
    console.error('section toggle failed:', err);
    if (epoch === conversationEpoch) {
      if (wasAdded) addedSectionKeys.add(key); else addedSectionKeys.delete(key);
      _syncAddedCourse(cid, term);
      notifySchedule('Could not update your schedule. Please try again.');
    }
  } finally {
    peers.forEach(el => { el.disabled = false; });
  }
  if (epoch !== conversationEpoch) return;
  // Mirror state on every other button bound to the same key (e.g.
  // both a Lec head + and a Dis row + can target the same key in
  // edge cases; or after a remove via the schedule rail we want all
  // displayed cards to re-sync).
  document.querySelectorAll(
    `.cc-sg-add[data-cid="${CSS.escape(cid)}"][data-sec="${CSS.escape(sec)}"][data-term="${CSS.escape(term)}"]`
  ).forEach(el => {
    const added = addedSectionKeys.has(key);
    el.classList.toggle('added', added);
    el.setAttribute('aria-pressed', String(added));
    el.setAttribute('aria-label', added ? 'Remove section from schedule' : 'Add section to schedule');
  });
}

/* Double-click on a Dis row delegates to the row's own + button so
   the animation runs in the same place — keeps one source of truth. */
function _toggleSectionAddByRow(rowEl) {
  const btn = rowEl.querySelector('.cc-sg-add');
  if (btn) toggleSectionAdd(btn);
}

/* Keep `addedCourses` (course-level) in sync with `addedSectionKeys`
   (section-level). A course counts as added iff ANY of its section
   keys is present. The schedule rail / count read from addedCourses. */
function _syncAddedCourse(courseId, term) {
  const prefix = `${canonicalTerm(term) || 'unknown'}|${courseId}:`;
  let hasAny = false;
  for (const k of addedSectionKeys) {
    if (k.startsWith(prefix)) { hasAny = true; break; }
  }
  const courseKey = scheduleCourseKey(term, courseId);
  if (hasAny) addedCourses.add(courseKey);
  else        addedCourses.delete(courseKey);
  updateScheduleCount();
  syncPlanObjects();
}

/* Source-of-truth rebuild from pending_schedule, including TBA and
   unknown-term legacy entries that have no calendar event. */
function _hydrateScheduleState() {
  const keys = new Set();
  const courses = new Set();
  for (const entry of pendingScheduleEntries) {
    const sec = entry.section || '';
    if (entry.course_id) {
      courses.add(scheduleCourseKey(entry.term, entry.course_id));
      if (sec) keys.add(scheduleEntryKey(entry.term, entry.course_id, sec));
    }
  }
  addedSectionKeys = keys;
  addedCourses = courses;
  // Reflect the new state on every visible + button so the picker
  // chevrons match the rail. Done by scanning the DOM rather than
  // tracking individually so re-renders don't drift.
  document.querySelectorAll('.cc-sg-add').forEach(el => {
    const cid = el.dataset.cid;
    const sec = el.dataset.sec;
    const term = el.dataset.term || currentTermContext?.term || '';
    if (!cid || !sec) return;
    const isAdded = keys.has(scheduleEntryKey(term, cid, sec));
    el.classList.toggle('added', isAdded);
    el.setAttribute('aria-pressed', isAdded ? 'true' : 'false');
    el.setAttribute('aria-label',
      isAdded ? 'Remove section from schedule' : 'Add section to schedule');
  });
  updateScheduleCount();
  syncPlanObjects();
}

/* Enrollment-restriction chips (SOC 'Rstr' column decoded).
   Class-level restrictions (E/F/G/H/I/J) are NEVER shown here —
   the dispatcher already hard-drops cards that violate those.
   What's left is non-blocking: auth code, major restriction, fee,
   forced grading basis. Hover for the registrar's full explanation. */
function _renderRestrictionChips(card) {
  const chips = card?.restriction_chips || [];
  if (chips.length === 0) return '';
  return `<div class="cc-rstr">
    ${chips.map(c => `<span class="cc-rstr-pill"
                            title="${escHTML(c.tooltip || '')}">
      <span class="cc-rstr-code">${escHTML(c.code)}</span>
      ${escHTML(c.label)}
    </span>`).join('')}
  </div>`;
}

function _renderPrereqChips(card) {
  const missing  = card?.prereq_missing || [];
  const unknown  = card?.prereq_unknown || [];
  const required = card?.prereq_required || [];
  const status   = card?.prereq_status || (card?.prereq_met ? 'met' : 'not_met');
  // Met → single success "Prereqs met" chip.
  // Not met → one locked chip per missing prereq.
  // Unknown → one warning chip per unverifiable prerequisite reason.
  // No prereqs known → empty column.
  if (status === 'met' && required.length > 0) {
    return `<span class="cc-chip cc-chip-met">
      <span class="material-symbols-outlined">${solonIcon('check_circle')}</span>
      Prereqs met
    </span>`;
  }
  if (status === 'unknown' && unknown.length > 0) {
    return unknown.map(p => `<span class="cc-chip">
      <span class="material-symbols-outlined">${solonIcon('help')}</span>
      ${escHTML(p)}
    </span>`).join('');
  }
  if (missing.length === 0) return '';
  return missing.map(p => `<span class="cc-chip">
    <span class="material-symbols-outlined">${solonIcon('lock')}</span>
    ${escHTML(p)}
  </span>`).join('');
}

function renderCard(card) {
  const cid       = card.course_id || '';
  const term      = card.term || currentTermContext?.term || '';
  const isAdded   = addedCourses.has(scheduleCourseKey(term, cid));
  const cat       = _catSlug(card);
  const classes   = [
    'course-card',
    `cat-${cat}`,
    isAdded ? 'added' : '',
    card.found === false ? 'not-found' : '',
  ].filter(Boolean).join(' ');

  const title      = card.title || (card.found === false
                       ? 'Not in catalog' : '');
  const reason     = card.reason || '';
  const unitsTxt   = card.units != null ? card.units : '—';
  const groupCount = (card.section_groups || []).length;
  const showSectionsBtn = groupCount > 0 ? `
    <button class="cc-sg-toggle"
            type="button"
            aria-expanded="false"
            onclick="event.stopPropagation(); toggleSectionGroups(this)">
      <span>${groupCount === 1 ? '1 section' : groupCount + ' Lec groups'}</span>
      <span class="material-symbols-outlined">${solonIcon('expand_more')}</span>
    </button>` : '';

  return `<div class="${classes}"
              id="card-${escHTML(cid)}"
              onclick="cardClickHandler(event)">
    <div class="cc-id-block">
      <div class="cc-id">${escHTML(cid)}</div>
      ${title ? `<div class="cc-title">${escHTML(title)}</div>` : ''}
      ${_renderCodeRow(card)}
    </div>
    <div class="cc-body">
      <div class="cc-badges">${_renderCardBadges(card, cat)}${_renderCardSourceBadges(card)}</div>
      ${reason ? `<p class="cc-reason">${escHTML(reason)}</p>` : ''}${_renderDynamicPrefilterMetadata(card)}
      ${_renderCoursePairingSummary(card)}
      ${_renderZotisticsHistory(card.zotistics_grades, null, cid)}
      ${_renderRestrictionChips(card)}
      ${_renderDeadlineRow(card)}
      ${showSectionsBtn}
    </div>
    <div class="cc-prereqs">${_renderPrereqChips(card)}</div>
    <div class="cc-units-block">
      <span class="cc-units">${escHTML(String(unitsTxt))}</span>
    </div>
    ${_renderSectionGroups(card)}
  </div>`;
}

/* Plan summaries retain the existing detailed card/section renderers. */
function renderCardsBlock(cards) {
  if (!cards || cards.length === 0) return '';
  return renderPlanObject(cards);
}
