"""Opt-in dynamic prefilter metadata uses the existing production renderer."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess

import pytest

from tests.test_zotistics_cards import NODE, ROOT, history, render


def card():
    return {
        "course_id": "ICS33", "term": "Fall 2026", "reason": "Fixture recommendation",
        "primary_code": "36200", "grade_distribution": {"avg_gpa": 3.2},
        "ranking": {"final_score": 82.25}, "zotistics_grades": history(),
        "section_groups": [{"letter": "A", "secondaries": [], "primary": {
            "code": "36200", "num": "A", "instructors": ["TESTER, A."],
            "ranking": {"final_score": 75},
        }}],
    }


def render_card(payload):
    return render(f"renderCard({json.dumps(payload)})")


@pytest.mark.parametrize("metadata", [None, {}, {"enabled": False}, {"enabled": "true"},
                                        {"enabled": False, "status": "needs_verification",
                                         "warnings": [{"message": "Do not display this"}]}])
def test_absent_or_disabled_feature_preserves_legacy_card_html(metadata):
    baseline = card()
    candidate = deepcopy(baseline)
    candidate["prefilter"] = metadata
    assert render_card(candidate) == render_card(baseline)
    assert "Recommendation score" not in render_card(candidate)
    assert "Lecturer score" not in render_card(candidate)


def test_enabled_feature_reads_backend_score_without_changing_raw_chart_or_legacy_badges():
    payload = card()
    payload["prefilter"] = {"enabled": True, "status": "keep", "warnings": []}
    html = render_card(payload)
    assert "Recommendation score 82.3/100" in html
    assert "Lecturer score 75.0/100" in html
    assert "Avg GPA 3.2" in html
    assert 'data-grade="A" data-count="34"' in html
    assert 'data-percentage="34"' in html
    assert "Raw A rate for ranking (A ÷ A–F)</dt><dd>42.5%" in html
    assert "This is not an expected grade or A rate" in html
    assert 'data-term="Fall 2026"' in html
    assert 'data-sec="A"' in html


@pytest.mark.parametrize("score", [None, -1, 101, "82.3", True, {"value": 82}])
def test_missing_or_invalid_score_is_not_zero(score):
    payload = card()
    payload["prefilter"] = {"enabled": True, "status": "keep", "warnings": []}
    payload["ranking"]["final_score"] = score
    payload["section_groups"][0]["primary"].pop("ranking")
    html = render_card(payload)
    assert "Recommendation score" not in html
    assert "Lecturer score" not in html
    assert "0.0/100" not in html


def test_true_zero_score_is_preserved():
    payload = card()
    payload["prefilter"] = {"enabled": True, "status": "keep", "warnings": []}
    payload["ranking"]["final_score"] = 0
    assert "Recommendation score 0.0/100" in render_card(payload)


def test_unknown_conditions_are_not_a_claim_of_eligibility_and_do_not_need_ranking():
    payload = card()
    payload.pop("ranking")
    payload["section_groups"][0]["primary"].pop("ranking")
    payload["prefilter"] = {"enabled": True, "status": "needs_verification", "warnings": []}
    html = render_card(payload)
    assert "Enrollment conditions still need verification." in html
    assert "Recommendation score" not in html
    assert "Lecturer score" not in html
    assert "Eligible" not in html


def test_warning_messages_are_escaped_and_deduplicated():
    payload = card()
    payload["prefilter"] = {"enabled": True, "status": "needs_verification", "warnings": [
        {"code": "unknown_time", "message": '<img src=x onerror="alert(1)"> & time unknown'},
        {"code": "unknown_time", "message": '<img src=x onerror="alert(1)"> & time unknown'},
        {"code": "unknown_ge", "message": "GE status needs verification"},
        {"code": "empty", "message": "   "}, None,
    ]}
    html = render_card(payload)
    assert "<img" not in html
    assert html.count("&lt;img") == 1
    assert "&amp; time unknown" in html
    assert "GE status needs verification" in html
    assert "Enrollment conditions still need verification." not in html


def test_primary_uses_its_own_score_and_warnings_without_copying_the_course_score():
    payload = card()
    payload["prefilter"] = {"enabled": True, "status": "needs_verification",
                            "warnings": [{"message": "Course prerequisite unknown"}]}
    primary = payload["section_groups"][0]["primary"]
    primary["prefilter"] = {"enabled": True, "status": "needs_verification",
                             "warnings": [{"message": "Lecture meeting time unknown"}]}
    html = render_card(payload)
    assert "Recommendation score 82.3/100" in html
    assert "Lecturer score 75.0/100" in html
    assert html.count("Course prerequisite unknown") == 1
    assert html.count("Lecture meeting time unknown") == 1
    primary["prefilter"]["enabled"] = False
    assert "Lecturer score" not in render_card(payload)


def test_primary_opt_in_can_be_present_without_course_opt_in():
    payload = card()
    payload["section_groups"][0]["primary"]["prefilter"] = {"enabled": True, "status": "keep"}
    html = render_card(payload)
    assert "Lecturer score 75.0/100" in html
    assert "Recommendation score" not in html


def test_nonfinite_score_is_not_rendered_and_chinese_labels_use_existing_language():
    expression = """(() => {
      globalThis.currentResponseLanguage = 'zh';
      const visible = _renderDynamicPrefilterMetadata({prefilter:{enabled:true,status:'needs_verification'},ranking:{final_score:0}});
      const hidden = _renderDynamicPrefilterMetadata({prefilter:{enabled:true,status:'keep'},ranking:{final_score:Infinity}});
      return {visible, hidden};
    })()"""
    result = render(expression)
    assert "推荐分 0.0/100" in result["visible"]
    assert "选课条件仍需核实" in result["visible"]
    assert result["hidden"] == ""


@pytest.mark.skipif(os.environ.get("SOLON_BROWSER_CHECK") != "1", reason="Optional real Chrome dynamic card verification")
def test_real_browser_dynamic_card_preserves_grade_chart_and_captures_responsive_ui():
    """Only synthetic data; all external browser requests are blocked."""
    if not NODE:
        pytest.skip("Node is required for browser verification")
    payload = card()
    payload["prefilter"] = {"enabled": True, "status": "needs_verification",
                            "warnings": [{"code": "prerequisite_unknown",
                                          "message": "Prerequisite eligibility still needs verification."}]}
    primary = payload["section_groups"][0]["primary"]
    primary["prefilter"] = {"enabled": True, "status": "needs_verification",
                             "warnings": [{"code": "time_unknown",
                                           "message": "Lecture meeting times are still unverified."}]}
    primary["grade_history"] = [{"instructor": "TESTER, A.", **history(), "scope": "course_instructor"}]
    artifacts = Path("/tmp/solon-dynamic-prefilter-ui")
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
    await page.setContent('<main id="fixture"><p class="fixture-label">Synthetic fixture · dynamic prefilter enabled</p><div class="plan-details" id="cardFixture"></div></main>');
    for (const style of ['tokens','components','solon']) {
      await page.addStyleTag({content:fs.readFileSync(`static/styles/${style}.css`, 'utf8')});
    }
    await page.addStyleTag({content:'html{height:auto;overflow:visible}body{height:auto;display:block;margin:0;padding:16px;overflow:visible}#fixture{width:860px;max-width:100%;margin:auto}.fixture-label{margin:0 0 12px;font-size:12px;color:var(--text-secondary)}'});
    await page.addScriptTag({content:`
      let currentResponseLanguage='en'; let currentTermContext={term:'Fall 2026'};
      const addedCourses=new Set(); const addedSectionKeys=new Set();
      function scheduleCourseKey(term,cid){return term+'|'+cid;}
      function scheduleEntryKey(term,cid,sec){return term+'|'+cid+':'+sec;}
    `});
    const ui = fs.readFileSync('static/js/ui.js','utf8');
    await page.addScriptTag({content:ui.slice(0,ui.indexOf("document.querySelectorAll('[data-icon]')"))});
    const api = fs.readFileSync('static/js/api-client.js','utf8');
    await page.addScriptTag({content:api.match(/function escAttr\(s\) \{[\s\S]*?\n\}/)[0]});
    for (const name of ['chat','cards']) {
      await page.addScriptTag({content:fs.readFileSync(`static/js/${name}.js`,'utf8')});
    }
    await page.evaluate(payload => {
      document.getElementById('cardFixture').innerHTML=renderCard(payload);
    }, JSON.parse(process.argv[1]));
    const card = page.locator('.course-card');
    assert.match(await card.locator('.cc-body > .cc-prefilter-metadata').textContent(),/Recommendation score 82\.3\/100/);
    assert.match(await card.locator('.cc-body > .cc-prefilter-metadata').textContent(),/Prerequisite eligibility still needs verification/);
    const courseHistory = card.locator('.cc-body > .grade-history-details');
    assert.equal(await courseHistory.getAttribute('open'),null);
    await courseHistory.locator('summary').click();
    assert.equal(await card.locator('.cc-sg-wrap').evaluate(el=>el.classList.contains('open')),false);
    await card.locator('.cc-sg-toggle').click();
    const primaryMetadata = card.locator('.cc-sg-card > .cc-prefilter-metadata');
    assert.match(await primaryMetadata.textContent(),/Lecturer score 75\.0\/100/);
    assert.match(await primaryMetadata.textContent(),/Lecture meeting times are still unverified/);
    await card.locator('.cc-sg-prof-grade > .grade-history-details > summary').click();
    assert.equal(await card.locator('.grade-chart-bars').count(),2);
    assert.equal(await courseHistory.locator('[data-grade="A"]').getAttribute('data-count'),'34');
    assert.equal(await courseHistory.locator('[data-grade="A"]').getAttribute('data-percentage'),'34');
    assert.match(await courseHistory.textContent(),/Raw A rate for ranking \(A ÷ A–F\).*42\.5%/s);
    assert.match(await courseHistory.textContent(),/Zotistics reported GPA.*3\.180/s);
    assert.equal(await card.locator('.cc-sg-add').getAttribute('aria-pressed'),'false');
    fs.mkdirSync(process.argv[2],{recursive:true});
    await page.locator('#fixture').screenshot({path:process.argv[2]+'/desktop.png'});
    await page.setViewportSize({width:390,height:1100});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=390));
    assert.equal(await courseHistory.locator('[data-grade="A"]').getAttribute('data-percentage'),'34');
    await page.locator('#fixture').screenshot({path:process.argv[2]+'/mobile.png'});
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({scores:true,warnings:true,rawGradeChart:true,mobileNoOverflow:true,errors}));
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([NODE, "-e", script, json.dumps(payload), str(artifacts)], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
