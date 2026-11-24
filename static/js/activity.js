/* ── Thinking line and steps ───────────────────────────
 * One line above each answer says what Solon is doing right now
 * ("Reading the COMPSCI department notes · 6s", with a shimmer), and
 * becomes "Thought for 14s · ..." once the answer starts. Clicking it
 * opens the steps in place as plain text rows; a row with sources or
 * a result opens further.
 *
 * The first half of this file is plain functions (no DOM) so the
 * wording can be tested on its own; the second half draws.
 */

/* ── Wording ───────────────────────────────────────── */

function activityCopy(zh) {
  return zh ? {
    resolvingTerm: '正在确定查询学期',
    thinking: '正在思考',
    thought: (time) => `已思考 ${time}`,
    steps: '思考过程',
    termLead: '已确定查询学期',
    answerRow: '已整理答案',
    stoppedRow: '已停止生成',
    sourcesLead: '已读取来源',
    failedPrefix: '未完成：',
    interruptedPrefix: '已中断：',
    failedCount: (n) => `${n} 步未完成`,
    took: (time) => `用时 ${time}`,
    joiner: '，',
    more: '等',
    requests: (n) => `抓取了 ${n} 个网页`,
  } : {
    resolvingTerm: 'Working out which term to check',
    thinking: 'Thinking',
    thought: (time) => `Thought for ${time}`,
    steps: 'Steps',
    termLead: 'Settled the term',
    answerRow: 'Wrote the answer',
    stoppedRow: 'Stopped',
    sourcesLead: 'Read sources',
    failedPrefix: 'Failed: ',
    interruptedPrefix: 'Interrupted: ',
    failedCount: (n) => `${n} ${n === 1 ? 'step' : 'steps'} failed`,
    took: (time) => `took ${time}`,
    joiner: ', ',
    more: 'and more',
    requests: (n) => `fetched ${n} web ${n === 1 ? 'page' : 'pages'}`,
  };
}

function activityDuration(ms, zh) {
  const total = Math.max(1, Math.round((Number(ms) || 0) / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  if (zh) return minutes ? `${minutes} 分 ${seconds} 秒` : `${seconds} 秒`;
  return minutes ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

// Tool labels arrive as "读取 WebSoc 部门说明 · COMPSCI" / "Reading WebSoc
// department notes · COMPSCI": a verb phrase in the turn's language.
function activityRunningLabel(label, zh) {
  const text = String(label || '').trim();
  return zh ? `正在${text}` : text;
}

const ACTIVITY_PAST_TENSE = {
  'Looking up': 'Looked up', Checking: 'Checked', Reading: 'Read', Summarizing: 'Summarized',
  Searching: 'Searched', Listing: 'Listed', Preparing: 'Prepared', Saving: 'Saved', Calling: 'Called',
};

function activityDoneLabel(label, zh) {
  const text = String(label || '').trim();
  if (zh) return `已${text}`;
  for (const [doing, done] of Object.entries(ACTIVITY_PAST_TENSE)) {
    if (text.startsWith(`${doing} `) || text === doing) return done + text.slice(doing.length);
  }
  return text;
}

// "Read WebSoc department notes · COMPSCI · Winter 2027" reads as an action
// (shown quietly) and what it was done to (shown in full strength).
function activitySplitLabel(text) {
  const value = String(text || '');
  const at = value.indexOf(' · ');
  return at < 0 ? {lead: value, rest: ''} : {lead: value.slice(0, at), rest: value.slice(at + 3)};
}

function activityIsRequest(source) {
  return Boolean(source) && !source.local && !source.cached;
}

function activitySourceKey(source) {
  const address = String(source.final_url || source.url || '').split('#')[0].replace(/\/+$/u, '').toLowerCase();
  return `${address}|${source.source_role || ''}|${source.local ? 'local' : 'web'}`;
}

// Every source the turn read, once each. A page fetched by one step and
// reused by a later one was still requested this turn.
function activityAllSources(steps) {
  const merged = [];
  const index = new Map();
  for (const step of steps || []) {
    for (const source of (step && step.sources) || []) {
      if (!source || typeof source !== 'object') continue;
      const key = activitySourceKey(source);
      if (!index.has(key)) {
        index.set(key, merged.length);
        merged.push(source);
      } else if (activityIsRequest(source) && !activityIsRequest(merged[index.get(key)])) {
        merged[index.get(key)] = source;
      }
    }
  }
  return merged;
}

function activityRequestCount(steps) {
  return activityAllSources(steps).filter(activityIsRequest).length;
}

const ACTIVITY_KINDS = {
  get_department_restrictions: ['读取了选课限制说明', 'read the enrollment restriction notes'],
  get_sections: ['查了排课', 'checked sections'],
  get_course_offerings: ['查了排课', 'checked sections'],
  get_live_sections: ['实时查了 WebSoc', 'checked live WebSoc'],
  search_courses: ['搜索了课程', 'searched courses'],
  get_course_info: ['查了课程资料', 'looked up course details'],
  check_prerequisites_met: ['查了课程资料', 'looked up course details'],
  get_grade_distribution: ['查了历史给分', 'checked grade history'],
  get_zotistics_grades: ['查了历史给分', 'checked grade history'],
  get_professor_rating: ['查了教授评价', 'looked up professor reviews'],
  get_professor_reviews: ['查了教授评价', 'looked up professor reviews'],
  get_professor_tags: ['查了教授评价', 'looked up professor reviews'],
  summarize_professor_reviews: ['查了教授评价', 'looked up professor reviews'],
  check_section_conflict: ['检查了时间冲突', 'checked time conflicts'],
  get_student_profile: ['读取了你的资料', 'read your profile'],
  get_policy: ['查了学校政策', 'checked university policy'],
  web_search: ['搜索了网页', 'searched the web'],
  fetch_page: ['读了网页', 'read a web page'],
  propose_recommendation: ['整理了推荐', 'prepared recommendations'],
  sources: ['读取了来源', 'read sources'],
};
const ACTIVITY_MAX_KINDS = 3;

// The short account after the time: what kinds of things were done.
function activitySummary(steps, zh) {
  const copy = activityCopy(zh);
  const phrases = [];
  for (const step of steps || []) {
    // Only what was actually done: failed and interrupted steps are
    // counted separately, not described as work.
    if (!step || step.kind === 'term' || step.ok !== true) continue;
    const pair = ACTIVITY_KINDS[step.name] || ['调用了工具', 'used a tool'];
    const phrase = zh ? pair[0] : pair[1];
    if (!phrases.includes(phrase)) phrases.push(phrase);
  }
  const parts = [];
  if (phrases.length) {
    let kinds = phrases.slice(0, ACTIVITY_MAX_KINDS).join(copy.joiner);
    if (phrases.length > ACTIVITY_MAX_KINDS) kinds += zh ? copy.more : `${copy.joiner}${copy.more}`;
    parts.push(kinds);
  }
  const requests = activityRequestCount(steps);
  if (requests) parts.push(copy.requests(requests));
  return parts.join(copy.joiner);
}

// What a section lookup found, said next to its row.
function activityStepResult(step, zh) {
  if (!step || !['get_sections', 'get_live_sections'].includes(step.name)) return '';
  const count = Number(step.section_count || 0);
  if (count > 0) return zh ? `找到 ${count} 个教学班` : `${count} ${count === 1 ? 'section' : 'sections'} found`;
  if (step.offering_status === 'not_offered' && step.authoritative === true) return zh ? '官方无匹配' : 'No official match';
  if (step.offering_status === 'not_offered') return zh ? '未找到教学班' : 'No sections found';
  if (step.offering_status === 'unavailable') return zh ? '数据不可用' : 'Data unavailable';
  return '';
}

function activityStepState(step, live) {
  if (step.kind === 'term') return 'done';
  if (step.ok === false) return 'failed';
  if (step.ok === true) return 'done';
  return live ? 'running' : 'interrupted';
}

// One row: {lead, rest, state}. `termLabel` localizes a term name.
function activityRowText(step, zh, live, termLabel) {
  const copy = activityCopy(zh);
  if (step.kind === 'term') {
    const terms = (step.terms || []).map(term => (termLabel ? termLabel(term) : term));
    return {lead: copy.termLead, rest: terms.join(' · '), state: 'done'};
  }
  if (step.name === 'sources' && !step.label) {
    return {lead: copy.sourcesLead, rest: '', state: 'done'};
  }
  const state = activityStepState(step, live);
  const label = state === 'running' ? activityRunningLabel(step.label, zh) : activityDoneLabel(step.label, zh);
  const parts = activitySplitLabel(state === 'failed' ? copy.failedPrefix + String(step.label || '')
    : state === 'interrupted' ? copy.interruptedPrefix + String(step.label || '') : label);
  const result = activityStepResult(step, zh);
  if (result) parts.rest = parts.rest ? `${parts.rest} · ${result}` : result;
  return {...parts, state};
}

// The collapsed line once the answer has started.
function activityHeaderText(activity, zh) {
  const copy = activityCopy(zh);
  const steps = (activity && activity.steps) || [];
  const hasTime = Boolean(activity) && activity.thinking_ms != null && Number.isFinite(Number(activity.thinking_ms));
  let text = hasTime ? copy.thought(activityDuration(activity.thinking_ms, zh)) : copy.steps;
  const summary = activitySummary(steps, zh);
  if (summary) text += ` · ${summary}`;
  const failed = steps.filter(step => step && step.ok === false).length;
  if (failed) text += zh ? `（${copy.failedCount(failed)}）` : ` (${copy.failedCount(failed)})`;
  return text;
}

// Turns saved before steps were recorded only kept their sources.
function activityFromLegacy(webFetches) {
  if (!Array.isArray(webFetches) || !webFetches.length) return null;
  return {thinking_ms: null, steps: [{kind: 'tool', name: 'sources', ok: true, sources: webFetches}]};
}

/* ── Drawing ───────────────────────────────────────── */

const ACTIVITY_SOURCE_ROLES = {
  zh: {
    registrar_websoc_form: 'WebSoc 查询表单',
    registrar_websoc_results: 'WebSoc 部门结果',
    registrar_websoc_course_results: 'WebSoc 课程结果',
    catalog_store_snapshot: '本地课表快照中的院系公告',
    undergrad_restrictions: '本科限制官方页',
    graduate_restrictions: '研究生限制官方页',
    policy: '政策官方页',
    restriction_spreadsheet: '课程限制表',
    official_link: '官方链接',
  },
  en: {
    registrar_websoc_form: 'WebSoc query form',
    registrar_websoc_results: 'WebSoc department results',
    registrar_websoc_course_results: 'WebSoc course results',
    catalog_store_snapshot: 'Department notices from the synced timetable',
    undergrad_restrictions: 'Official undergraduate restrictions',
    graduate_restrictions: 'Official graduate restrictions',
    policy: 'Official policy page',
    restriction_spreadsheet: 'Course restrictions spreadsheet',
    official_link: 'Official link',
  },
};

// The facts shown under a source: what it is and how it was read.
function activitySourceFacts(source, zh) {
  const roles = zh ? ACTIVITY_SOURCE_ROLES.zh : ACTIVITY_SOURCE_ROLES.en;
  const facts = [roles[source.source_role] || (zh ? '网页来源' : 'Web source')];
  if (source.local || source.cached) {
    const readAt = source.local ? source.snapshot_synced_at : source.cached_at;
    const when = readAt ? new Date(readAt) : null;
    const stamp = when && !Number.isNaN(when.getTime()) ? when.toLocaleString() : null;
    const label = source.local
      ? (zh ? '未发请求，同步于' : 'No request; synced')
      : (zh ? '24 小时内已抓取，未重新请求；抓取于' : 'Fetched within 24 h, not requested again;');
    facts.push(stamp ? `${label} ${stamp}` : label);
  }
  if (source.status_code != null) facts.push(`HTTP ${source.status_code}`);
  if (source.bytes != null) facts.push(`${Number(source.bytes).toLocaleString()} B`);
  if (source.duration_ms != null && activityIsRequest(source)) facts.push(`${Math.round(source.duration_ms)} ms`);
  if (source.provides_evidence) facts.push(zh ? '用于最终事实' : 'Used as evidence');
  if (source.ok === false) facts.push(zh ? '抓取失败，未能读取此来源' : 'Request failed; this source could not be read');
  return facts;
}

function activityEl(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text != null) el.textContent = text;
  return el;
}

function buildActivitySources(sources, zh) {
  const list = activityEl('div', 'thinking-sources');
  for (const source of sources || []) {
    if (!source || typeof source !== 'object') continue;
    const row = activityEl('div', `thinking-source${source.ok === false ? ' is-error' : ''}`);
    const url = String(source.final_url || source.url || '');
    const name = String(source.host || url || (zh ? '未知地址' : 'Unknown address'));
    if (/^https?:\/\//iu.test(url) && !source.local) {
      const link = activityEl('a', 'thinking-source-name', name);
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.title = url;
      row.appendChild(link);
    } else {
      row.appendChild(activityEl('span', 'thinking-source-name', name));
    }
    row.appendChild(activityEl('span', 'thinking-source-facts', activitySourceFacts(source, zh).join(' · ')));
    list.appendChild(row);
  }
  return list;
}

function activityState(wrap) {
  if (!wrap._activity) {
    wrap._activity = {
      steps: [], live: false, expanded: false, openRows: new Set(),
      accumMs: 0, phaseStart: null, liveText: '', timer: null, finished: false, stopped: false,
    };
  }
  return wrap._activity;
}

function activityBlock(wrap) {
  let block = wrap.querySelector(':scope > .thinking-block');
  if (block) return block;
  block = activityEl('div', 'thinking-block');
  const line = activityEl('button', 'thinking-line');
  line.type = 'button';
  line.setAttribute('aria-expanded', 'false');
  line.appendChild(activityEl('span', 'thinking-text'));
  line.appendChild(activityEl('span', 'thinking-time'));
  line.appendChild(activityEl('span', 'thinking-chevron', '›'));
  line.addEventListener('click', () => {
    const state = activityState(wrap);
    state.expanded = !state.expanded;
    drawActivity(wrap);
  });
  const steps = activityEl('div', 'thinking-steps');
  steps.hidden = true;
  block.appendChild(line);
  block.appendChild(steps);
  const body = wrap.querySelector(':scope > .msg-ai-body');
  wrap.insertBefore(block, wrap.querySelector(':scope > .query-term-badge') || body);
  return block;
}

function activityElapsed(state) {
  return state.accumMs + (state.phaseStart == null ? 0 : performance.now() - state.phaseStart);
}

function activityClockText(state, zh) {
  const elapsed = activityElapsed(state);
  return elapsed >= 1000 ? ` · ${activityDuration(elapsed, zh)}` : '';
}

// Once a second while thinking. Only the clock is touched, so an open
// list keeps its focus and scroll position.
function activityTick(wrap) {
  const state = activityState(wrap);
  const time = wrap.querySelector(':scope > .thinking-block .thinking-time');
  if (time && state.live) time.textContent = activityClockText(state, wrap._responseLanguage === 'zh');
}

function drawActivity(wrap) {
  const state = activityState(wrap);
  const zh = wrap._responseLanguage === 'zh';
  const copy = activityCopy(zh);
  const block = activityBlock(wrap);
  const line = block.querySelector('.thinking-line');
  const text = line.querySelector('.thinking-text');
  const time = line.querySelector('.thinking-time');

  line.classList.toggle('is-live', state.live);
  line.classList.toggle('is-warning', state.steps.some(step => step.ok === false));
  line.setAttribute('aria-expanded', String(state.expanded));
  if (state.live) {
    text.textContent = state.liveKey ? copy[state.liveKey] : activityRunningLabel(state.liveText, zh);
    time.textContent = activityClockText(state, zh);
  } else {
    text.textContent = activityHeaderText({
      thinking_ms: state.noTime ? null : state.accumMs,
      steps: state.steps,
    }, zh);
    time.textContent = '';
  }

  const list = block.querySelector('.thinking-steps');
  list.hidden = !state.expanded;
  if (!state.expanded) return;
  list.replaceChildren();
  state.steps.forEach((step, index) => {
    const row = activityRowText(step, zh, state.live, term => responseTerm(term, wrap._responseLanguage));
    const sources = Array.isArray(step.sources) ? step.sources : [];
    const hasDetail = sources.length > 0;
    const item = activityEl('div', `thinking-step is-${row.state}`);
    const head = activityEl(hasDetail ? 'button' : 'div', 'thinking-step-head');
    head.appendChild(activityEl('span', 'thinking-step-lead', row.lead));
    if (row.rest) head.appendChild(activityEl('span', 'thinking-step-rest', row.rest));
    if (hasDetail) {
      const open = state.openRows.has(index);
      head.type = 'button';
      head.setAttribute('aria-expanded', String(open));
      head.appendChild(activityEl('span', 'thinking-chevron', '›'));
      head.addEventListener('click', () => {
        if (state.openRows.has(index)) state.openRows.delete(index); else state.openRows.add(index);
        drawActivity(wrap);
        const redrawn = block.querySelectorAll('.thinking-steps > .thinking-step')[index];
        if (redrawn) redrawn.querySelector('.thinking-step-head').focus();
      });
      item.appendChild(head);
      if (open) {
        const detail = activityEl('div', 'thinking-step-detail');
        if (Number(step.duration_ms) >= 100) {
          detail.appendChild(activityEl('div', 'thinking-step-took', copy.took(activityDuration(step.duration_ms, zh))));
        }
        detail.appendChild(buildActivitySources(sources, zh));
        item.appendChild(detail);
      }
    } else {
      item.appendChild(head);
    }
    list.appendChild(item);
  });
  if (state.finished) {
    const closing = activityEl('div', 'thinking-step is-done');
    const head = activityEl('div', 'thinking-step-head');
    head.appendChild(activityEl('span', 'thinking-step-lead', state.stopped ? copy.stoppedRow : copy.answerRow));
    closing.appendChild(head);
    list.appendChild(closing);
  }
}

function activityStopTimer(state) {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
}

// Thinking (again): the line shimmers and the clock runs.
function activityResume(wrap) {
  const state = activityState(wrap);
  state.finished = false;
  state.live = true;
  if (state.phaseStart == null) state.phaseStart = performance.now();
  if (!state.timer) state.timer = setInterval(() => activityTick(wrap), 1000);
}

function activityPause(state) {
  if (state.phaseStart != null) {
    state.accumMs += performance.now() - state.phaseStart;
    state.phaseStart = null;
  }
  state.live = false;
  activityStopTimer(state);
}

function startActivity(wrap) {
  const state = activityState(wrap);
  state.liveKey = 'resolvingTerm';
  activityResume(wrap);
  drawActivity(wrap);
}

function activityStage(wrap, event) {
  const state = activityState(wrap);
  if (event.response_language) wrap._responseLanguage = event.response_language;
  if (event.stage === 'term_resolved') {
    const terms = Array.isArray(event.query_terms) ? event.query_terms.filter(Boolean) : [];
    if (terms.length && !state.steps.some(step => step.kind === 'term')) {
      state.steps.unshift({kind: 'term', terms, ok: true});
    }
    state.liveKey = 'thinking';
  }
  drawActivity(wrap);
}

function activityToolStart(wrap, label, toolName, language) {
  if (language) wrap._responseLanguage = language;
  const state = activityState(wrap);
  state.steps.push({kind: 'tool', name: toolName || '', label: label || toolName || ''});
  state.liveKey = null;
  state.liveText = label || toolName || '';
  activityResume(wrap);
  drawActivity(wrap);
  scrollChat();
}

function activityToolDone(wrap, ok, fetchSummary, resultMeta = {}) {
  const state = activityState(wrap);
  const running = state.steps.filter(step => step.kind === 'tool' && step.ok === undefined);
  const step = running.find(item => item.name === resultMeta.name) || running[0];
  if (step) {
    step.ok = ok;
    if (Array.isArray(fetchSummary) && fetchSummary.length) step.sources = fetchSummary;
    for (const field of ['section_count', 'offering_status', 'authoritative']) {
      if (field in resultMeta) step[field] = resultMeta[field];
    }
  }
  const next = state.steps.find(item => item.kind === 'tool' && item.ok === undefined);
  state.liveKey = next ? null : 'thinking';
  if (next) state.liveText = next.label;
  drawActivity(wrap);
}

// The answer has started: the line settles into "Thought for Ns".
function activityAnswerStarted(wrap) {
  const state = activityState(wrap);
  if (!state.live) return;
  activityPause(state);
  drawActivity(wrap);
}

// Generation is over. `activity` is the server's record of the turn when
// there is one; it replaces the live copy so reload shows the same thing.
function finishActivity(wrap, activity, stopped) {
  const state = activityState(wrap);
  activityPause(state);
  if (activity && Array.isArray(activity.steps)) {
    state.steps = activity.steps.map(step => ({...step}));
    if (Number.isFinite(Number(activity.thinking_ms))) state.accumMs = Number(activity.thinking_ms);
  }
  state.finished = true;
  state.stopped = Boolean(stopped);
  if (!state.steps.length && state.accumMs < 1000) {
    // Nothing happened worth a line (an instant reply).
    const block = wrap.querySelector(':scope > .thinking-block');
    if (block) block.remove();
    return;
  }
  drawActivity(wrap);
}

// A saved turn, drawn in its settled form.
function renderStoredActivity(wrap, activity, legacyFetches) {
  const record = (activity && Array.isArray(activity.steps)) ? activity : activityFromLegacy(legacyFetches);
  if (!record || (!record.steps.length && !(Number(record.thinking_ms) >= 1000))) return;
  const state = activityState(wrap);
  state.steps = record.steps.map(step => ({...step}));
  state.noTime = record.thinking_ms == null;
  state.accumMs = Number(record.thinking_ms) || 0;
  state.finished = true;
  drawActivity(wrap);
}

if (typeof module !== 'undefined') {
  module.exports = {
    activityCopy, activityDuration, activityRunningLabel, activityDoneLabel, activitySplitLabel,
    activityAllSources, activityRequestCount, activitySummary, activityStepResult, activityRowText,
    activityHeaderText, activityFromLegacy, activitySourceFacts,
  };
}
