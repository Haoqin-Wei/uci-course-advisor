"""Execute production grade chart renderers without browser dependencies."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
NODE_HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const context = vm.createContext({
  document: {createElement() {
    return {set textContent(value) {
      this.innerHTML = String(value).replace(/&/g, '&amp;')
        .replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }};
  }},
  currentTermContext: {term: 'Fall 2026'},
  addedCourses: new Set(), addedSectionKeys: new Set(),
  scheduleCourseKey: (term, cid) => `${term}|${cid}`,
  scheduleEntryKey: (term, cid, sec) => `${term}|${cid}:${sec}`,
  solonIcon: () => '',
});
for (const [file, name] of [['static/js/chat.js', 'escHTML'],
                            ['static/js/api-client.js', 'escAttr']]) {
  const source = fs.readFileSync(file, 'utf8');
  const fn = source.match(new RegExp(`function ${name}\\(s\\) \\{[\\s\\S]*?\\n\\}`));
  if (!fn) throw new Error(`Missing production helper ${name}`);
  vm.runInContext(fn[0], context);
}
vm.runInContext(fs.readFileSync('static/js/cards.js', 'utf8'), context);
Promise.resolve(vm.runInContext(process.argv[1], context))
  .then(value => process.stdout.write(JSON.stringify(value)))
  .catch(error => {console.error(error); process.exitCode=1;});
"""


def render(expression: str):
    if not NODE:
        pytest.skip("Node is required to execute the frontend renderer")
    result = subprocess.run([NODE, "-e", NODE_HARNESS, expression], cwd=ROOT,
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def history(**overrides):
    grades = {
        "avg_gpa": 3.12, "source_reported_gpa": 3.18, "pct_A": 42.5,
        "graded": 80, "samples": 100,
        "letter_counts": {"A": 34, "B": 20, "C": 16, "D": 5, "F": 5, "P": 12, "NP": 8},
        "term_range": {"first": "2021 Fall", "last": "2023 Spring"},
    }
    grades.update(overrides)
    return {"source": "zotistics", "found": True, "status": "ok", "grades": grades,
            "source_url": "javascript:alert('source')", "scope": "course",
            "coverage": {"last_grade_update": "2023 Spring"}}


def chart(evidence):
    return render(f"_renderZotisticsHistory({json.dumps(evidence)})")


def test_course_history_is_collapsed_and_keeps_denominators_and_reported_gpa_separate():
    card = {"course_id": "I&C SCI 33", "grade_distribution": {"avg_gpa": 3.8},
            "zotistics_grades": history()}
    html = render(f"renderCard({json.dumps(card)})")
    assert "Avg GPA 3.8" in html
    assert '<details class="grade-history-details">' in html
    assert '<details class="grade-history-details" open' not in html
    assert 'data-grade="A" data-count="34"' in html
    assert 'data-percentage="34"' in html  # all 100 records, including P/NP
    assert "42.5%" in html  # separate A/(A-F) rate
    assert "including P/NP" in html
    assert "Total grade records</dt><dd>100" in html
    assert "A–F grade records</dt><dd>80" in html
    assert "Zotistics reported GPA</dt><dd>3.180" in html
    assert "Actual historical range: 2021 Fall – 2023 Spring" in html
    assert "Source grades updated through: 2023 Spring" in html
    assert 'href="https://zotistics.com/"' in html
    assert 'rel="noopener noreferrer"' in html
    assert "javascript:" not in html


@pytest.mark.parametrize("evidence", [
    None,
    {"found": False, "source": "zotistics", "status": "unavailable", "grades": history()["grades"]},
    {"found": False, "source": "zotistics", "status": "no_data"},
    {"found": True, "source": "another_source", "grades": history()["grades"]},
    {"found": True, "source": "zotistics", "grades": {}},
    history(letter_counts={}, graded=0, samples=0), history(samples=101), history(graded=81),
    history(letter_counts={**history()["grades"]["letter_counts"], "A": -1}),
    history(letter_counts={**history()["grades"]["letter_counts"], "A": True}),
])
def test_unavailable_invalid_and_absent_data_never_draw_fake_zero_bars(evidence):
    html = chart(evidence)
    assert 'class="grade-chart-bars"' not in html
    assert 'data-grade="A"' not in html


def test_real_zero_rate_and_source_gpa_are_preserved():
    html = chart(history(letter_counts={"A": 0, "B": 0, "C": 0, "D": 0, "F": 80, "P": 12, "NP": 8},
                         avg_gpa=0, source_reported_gpa=0, pct_A=0))
    assert 'data-grade="A" data-count="0"' in html
    assert 'style="height:0%"' in html
    assert "Raw A rate for ranking (A ÷ A–F)</dt><dd>0.0%" in html
    assert "Zotistics reported GPA</dt><dd>0.000" in html


def test_pnp_only_has_raw_bars_without_a_rate_or_estimated_gpa():
    html = chart(history(letter_counts={"A": 0, "B": 0, "C": 0, "D": 0, "F": 0, "P": 55, "NP": 45},
                         avg_gpa=None, source_reported_gpa=None, pct_A=None, graded=0))
    assert 'data-grade="P" data-count="55"' in html
    assert "55.0%" in html
    assert "No A–F records; A rate unavailable." in html
    assert "Raw A rate for ranking (A ÷ A–F)</dt><dd>Unavailable" in html
    assert "Bucket GPA estimate" not in html


def test_w_only_appears_when_source_supplies_it():
    assert 'data-grade="W"' not in chart(history())
    with_w = chart(history(letter_counts={**history()["grades"]["letter_counts"], "W": 5}, samples=105))
    assert 'data-grade="W" data-count="5"' in with_w
    assert "Total grade records</dt><dd>105" in with_w
    assert "A–F grade records</dt><dd>80" in with_w
    assert "W included." in with_w


def test_ranking_adjustments_and_supplied_percentages_do_not_replace_raw_counts():
    html = chart(history(pct_A=95, letter_pct={"A": 95}, adjusted_a_rate=90, ranking={"score": 97}))
    assert 'data-percentage="34"' in html
    assert "Raw A rate for ranking (A ÷ A–F)</dt><dd>42.5%" in html
    for incorrect in ("95.0%", "90.0%", "97.0%"):
        assert incorrect not in html


def test_bucket_estimate_is_labelled_when_source_reported_gpa_is_missing():
    html = chart(history(source_reported_gpa=None))
    assert "Zotistics reported GPA</dt><dd>Unavailable" in html
    assert "Bucket GPA estimate ≈ 3.120" in html
    assert "no +/- or P/NP" in html


def test_professor_course_history_matches_exact_name_and_rejects_course_wide_pool():
    lecturer = {"instructor": "TESTER, A.", **history(), "scope": "course_instructor"}
    primary = {"code": "36200", "num": "A", "instructors": ["TESTER, A.", "BUILDER, B."],
               "grade_history": [lecturer, {"instructor": "BUILDER", **history()}]}
    card = {"course_id": "I&C SCI 33", "zotistics_grades": history(),
            "section_groups": [{"letter": "A", "primary": primary, "secondaries": []}]}
    html = render(f"renderCard({json.dumps(card)})")
    assert html.count('class="grade-chart-bars"') == 2
    assert "TESTER, A. · I&amp;C SCI 33" in html
    assert "cc-sg-prof-grade" not in html.split("BUILDER, B.", 1)[1]
    pooled = [{"instructor": "TESTER, A.", **history()}]
    assert 'class="grade-chart-bars"' not in render(f"_renderProfRow([], ['TESTER, A.'], {json.dumps(pooled)}, 'ICS33')")


def test_instructor_wide_fallback_is_labelled_and_no_data_pair_is_not_zero():
    fallback = {"instructor": "TESTER, A.", **history(), "scope": "instructor"}
    missing = {"instructor": "BUILDER, B.", "source": "zotistics", "scope": "course_instructor",
               "status": "no_data", "found": False, "grades": None}
    html = render(f"_renderProfRow([], ['TESTER, A.', 'BUILDER, B.'], {json.dumps([fallback, missing])}, 'ICS33')")
    assert "All courses by this instructor" in html
    assert "this is not a distribution for this course" in html
    assert "No matching historical grade records" in html
    assert html.count('class="grade-chart-bars"') == 1


def test_comparison_is_open_and_unavailable_entries_do_not_have_bars():
    first = {"instructor": "TESTER, A.", "course_id": "ICS33", **history(), "scope": "course_instructor"}
    second = {"instructor": "BUILDER, B.", "course_id": "ICS33", "source": "zotistics",
              "scope": "course_instructor", "found": False, "status": "unavailable", "grades": None}
    html = render(f"renderGradeChartsBlock({json.dumps([first, second])})")
    assert 'class="grade-comparison-grid"' in html
    assert html.count('class="grade-comparison-item"') == 2
    assert html.count('class="grade-chart-bars"') == 1
    assert "Historical grades temporarily unavailable" in html
    assert "<details" not in html
    empty = render(f"renderGradeChartsBlock({json.dumps([second])})")
    assert 'class="grade-chart-bars"' not in empty
    assert 'data-count="0"' not in empty


def test_external_text_is_escaped_and_source_link_is_fixed():
    instructor = 'TESTER, "A"><img src=x onerror="alert(1)">'
    evidence = {"instructor": instructor, "course_id": '<script>alert(2)</script>', **history(), "scope": "course_instructor"}
    evidence["grades"]["term_range"] = {"first": '"><script>alert(3)</script>', "last": "2023 & Spring"}
    evidence["grades"]["coverage"] = {"last_grade_update": '\" onclick="alert(4)"'}
    html = render(f"renderGradeChartsBlock({json.dumps([evidence])})")
    assert "<img" not in html
    assert "<script" not in html
    assert "&lt;img" in html
    assert "&lt;script&gt;" in html
    assert '&quot;A&quot;&gt;&lt;img' in html
    assert "2023 &amp; Spring" in html
    assert "javascript:" not in html


def test_live_and_restored_turns_share_chart_renderer():
    chat_source = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
    sessions = (ROOT / "static/js/sessions.js").read_text(encoding="utf-8")
    css = (ROOT / "static/styles/components.css").read_text(encoding="utf-8")
    assert "renderGradeChartsForMessage(wrap, meta.grade_charts)" in chat_source
    assert "renderGradeChartsForMessage(wrap, extras.grade_charts)" in sessions
    assert "grade_charts: t.grade_charts" in sessions
    assert ".grade-comparison-grid { grid-template-columns: minmax(0, 1fr); }" in css


def test_chinese_turn_labels_do_not_change_chart_counts():
    html = render(f"renderGradeChartsBlock({json.dumps([history()])}, 'zh')")
    assert "历史给分比较" in html
    assert "成绩记录总数" in html
    assert 'data-grade="A" data-count="34"' in html


@pytest.mark.parametrize("chart_payload", [None, [], [history()]])
def test_continue_meta_updates_the_real_wrap_only_when_new_charts_exist(chart_payload):
    """Exercise the production Continue SSE callback with a minimal DOM."""
    meta = {"final_answer": "Continued answer", "response_language": "en"}
    if chart_payload is not None:
        meta["grade_charts"] = chart_payload
    expression = r"""
(async () => {
  const source = REPLACE_SOURCE;
  // Include the actual full Continue function, whose closing brace is on
  // its own line; nested callback braces are indented.
  eval(source.match(/async function continueAgent\(wrap, continuationId, btn\) \{[\s\S]*?\n\}/)[0]);
  globalThis.API = '';
  globalThis.currentSessionId = 'fixture-session';
  globalThis.continuationCopy = () => ({loading:'Loading', error:'Error'});
  globalThis.fetch = async () => ({ok:true});
  globalThis.scrollChat = () => {};
  globalThis.formatMarkdown = value => value;
  globalThis.consumeSSE = async (_response, handlers) => handlers.meta(REPLACE_META);
  const originalCreate = document.createElement;
  document.createElement = () => ({className:'', style:{}, textContent:'',
    classList:{remove(){}}, innerHTML:''});
  const originalChart = {id:'old'};
  const wrap = {_responseLanguage:'en', chart:originalChart,
    querySelector:() => null, insertBefore:() => {}};
  let actualWrap = null;
  let calls = 0;
  globalThis.renderGradeChartsForMessage = (target, entries) => {
    actualWrap = target; calls++; target.chart = entries;
  };
  await continueAgent(wrap, 'fixture-continuation', {});
  document.createElement = originalCreate;
  return {calls, onRealWrap:actualWrap === wrap, oldPreserved:wrap.chart === originalChart};
})()
"""
    source = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
    result = render(expression.replace("REPLACE_SOURCE", json.dumps(source)).replace("REPLACE_META", json.dumps(meta)))
    has_new_charts = bool(chart_payload)
    assert result["calls"] == int(has_new_charts)
    assert result["onRealWrap"] is has_new_charts
    assert result["oldPreserved"] is not has_new_charts


@pytest.mark.skipif(os.environ.get("SOLON_BROWSER_CHECK") != "1", reason="Optional real Chrome chart verification")
def test_real_browser_chart_interaction_comparison_and_restore(tmp_path):
    """Run production live/restored renderers in Chrome; no remote calls."""
    if not NODE:
        pytest.skip("Node is required for browser verification")
    evidence = history()
    pair = {"instructor": "TESTER, A.", **evidence, "scope": "course_instructor"}
    fallback = {"instructor": "BUILDER, B.", "course_id": "ICS33", **evidence, "scope": "instructor"}
    card = {
        "course_id": "ICS33", "title": "Fixture course", "units": 4,
        "term": "Fall 2026", "primary_code": "36200", "zotistics_grades": evidence,
        "section_groups": [{"letter": "A", "secondaries": [], "primary": {
            "code": "36200", "num": "A", "instructors": ["TESTER, A."],
            "grade_history": [pair], "days": "MWF", "time": "10:00–10:50",
        }}],
    }
    payload = {"cards": [card], "grade_charts": [{"course_id": "ICS33", **pair}, fallback]}
    script = r"""
const {chromium} = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless:true,
    executablePath:process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1100}});
    page.setDefaultTimeout(5000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('https://**', route => route.abort());
    await page.setContent('<html><body><main id="chatScroll"></main></body></html>');
    for (const css of ['tokens','components','solon']) {
      await page.addStyleTag({content:fs.readFileSync(`static/styles/${css}.css`, 'utf8')});
    }
    await page.addStyleTag({content:'html{height:auto;overflow:visible}body{height:auto;display:block;margin:0;padding:16px;overflow:visible}#chatScroll{width:820px;max-width:100%;height:auto;overflow:visible;margin:auto}.msg{max-width:100%}'});
    await page.addScriptTag({content:`
      let currentResponseLanguage = 'en';
      let currentTermContext = {term:'Fall 2026'};
      let addedCourses = new Set(); let addedSectionKeys = new Set();
      function scheduleCourseKey(term, cid){return term+'|'+cid;}
      function scheduleEntryKey(term, cid, sec){return term+'|'+cid+':'+sec;}
      function canonicalTerm(term){return term;}
      function displayTerm(term){return term;}
      function solonIcon(){return '<svg class="solon-icon" viewBox="0 0 24 24"></svg>';}
      function setComposerSuggestions(){} function renderScheduleGrid(){}
    `});
    const api = fs.readFileSync('static/js/api-client.js','utf8');
    await page.addScriptTag({content:api.match(/function escAttr\(s\) \{[\s\S]*?\n\}/)[0]});
    for (const moduleName of ['chat','cards','plan','sessions']) {
      await page.addScriptTag({content:fs.readFileSync(`static/js/${moduleName}.js`,'utf8')});
    }
    const payload = JSON.parse(process.argv[1]);
    await page.evaluate(payload => {
      const live = startAiMessage('en');
      finalizeAiMessage(live, 'Live chart comparison.', payload, false);
      appendAssistantStatic('Restored chart comparison.', {...payload, response_language:'en'});
    }, payload);
    const messages = page.locator('.msg-ai');
    assert.equal(await messages.count(), 2);
    assert.equal(await messages.nth(0).locator('.grade-comparison').innerHTML(),
                 await messages.nth(1).locator('.grade-comparison').innerHTML());
    const comparison = messages.nth(0).locator('.grade-comparison-item');
    const desktop = await comparison.evaluateAll(items => items.map(el => el.getBoundingClientRect().toJSON()));
    assert(Math.abs(desktop[0].top-desktop[1].top)<1);
    assert(desktop[1].left>desktop[0].left);
    await messages.nth(0).locator('.plan-details-toggle').click();
    const card = messages.nth(0).locator('.course-card');
    const courseHistory = card.locator('.cc-body > .grade-history-details');
    assert.equal(await courseHistory.getAttribute('open'),null);
    await courseHistory.locator('summary').click();
    assert.equal(await courseHistory.getAttribute('open'),'');
    assert.equal(await card.locator('.cc-sg-wrap').evaluate(el => el.classList.contains('open')),false);
    await card.locator('.cc-sg-toggle').click();
    assert.equal(await card.locator('.cc-sg-wrap').evaluate(el => el.classList.contains('open')),true);
    const professorHistory = card.locator('.cc-sg-prof-grade > .grade-history-details');
    await professorHistory.locator('summary').click();
    assert.equal(await card.locator('.cc-sg-wrap').evaluate(el => el.classList.contains('open')),true);
    assert.equal(await card.locator('.cc-sg-add').getAttribute('aria-pressed'),'false');
    assert.equal(await card.locator('.cc-sg-add').getAttribute('data-sec'),'A');
    assert.equal(await professorHistory.locator('[data-grade="A"]').getAttribute('data-percentage'),'34');
    await messages.nth(0).locator('.grade-comparison').screenshot({path:process.argv[2]+'/desktop.png'});
    await page.setViewportSize({width:390,height:1100});
    const mobile = await comparison.evaluateAll(items => items.map(el => el.getBoundingClientRect().toJSON()));
    assert(Math.abs(mobile[0].left-mobile[1].left)<1);
    assert(mobile[1].top>mobile[0].top);
    assert(await page.evaluate(() => document.documentElement.scrollWidth<=390));
    await messages.nth(0).locator('.grade-comparison').screenshot({path:process.argv[2]+'/mobile.png'});
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({desktopSideBySide:true,mobileStacked:true,restoredIdentically:true,errors}));
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode=1;});
"""
    result = subprocess.run([NODE, "-e", script, json.dumps(payload), str(tmp_path)], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.environ.get("SOLON_BROWSER_CHECK") != "1", reason="Optional real Chrome Continue verification")
def test_real_browser_continue_chart_meta_preserves_and_replaces_without_duplicates():
    if not NODE:
        pytest.skip("Node is required for browser verification")
    old = {"instructor": "ORIGINAL, A.", "course_id": "ICS33", **history(), "scope": "course_instructor"}
    fresh = {"instructor": "CONTINUED, B.", "course_id": "ICS33", **history(), "scope": "course_instructor"}
    script = r"""
const {chromium} = require('playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless:true,
    executablePath:process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('https://**', route => route.abort());
    await page.setContent('<main id="chatScroll"></main>');
    await page.addScriptTag({content:`
      const API=''; let currentSessionId='fixture'; let currentResponseLanguage='en';
      function solonIcon(){return '';}
    `});
    const api = fs.readFileSync('static/js/api-client.js','utf8');
    await page.addScriptTag({content:api.match(/function escAttr\(s\) \{[\s\S]*?\n\}/)[0]});
    for (const name of ['chat','cards']) {
      await page.addScriptTag({content:fs.readFileSync(`static/js/${name}.js`,'utf8')});
    }
    const result = await page.evaluate(async fixtures => {
      window.fetch = async () => ({ok:true});
      let meta;
      window.consumeSSE = async (_response, handlers) => handlers.meta(meta);
      const wrap = document.createElement('div');
      wrap._responseLanguage='en';
      wrap.innerHTML='<div class="msg-ai-body">Original answer</div>';
      document.getElementById('chatScroll').appendChild(wrap);
      renderGradeChartsForMessage(wrap,[fixtures.old]);
      const originalChart=wrap.querySelector('.grade-comparison');
      async function resume(event) {
        meta=event;
        await continueAgent(wrap,'fixture-continuation',document.createElement('button'));
      }
      await resume({final_answer:'Continue without grade payload'});
      const missingPreserved=wrap.querySelector('.grade-comparison')===originalChart;
      await resume({final_answer:'Continue with empty grade payload',grade_charts:[]});
      const emptyPreserved=wrap.querySelector('.grade-comparison')===originalChart;
      await resume({final_answer:'Continue with fresh grade evidence',grade_charts:[fixtures.fresh]});
      const replaced=wrap.querySelector('.grade-comparison')!==originalChart;
      const freshShown=wrap.querySelector('.grade-comparison').textContent.includes('CONTINUED, B.');
      await resume({final_answer:'Repeated meta',grade_charts:[fixtures.fresh]});
      return {missingPreserved,emptyPreserved,replaced,freshShown,
              chartCount:wrap.querySelectorAll('.grade-comparison').length,
              originalBody:wrap.querySelector('.msg-ai-body').textContent};
    }, JSON.parse(process.argv[1]));
    assert.deepEqual(result,{missingPreserved:true,emptyPreserved:true,replaced:true,
      freshShown:true,chartCount:1,originalBody:'Original answer'});
    assert.deepEqual(errors,[]);
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode=1;});
"""
    result = subprocess.run([NODE, "-e", script, json.dumps({"old": old, "fresh": fresh})], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
