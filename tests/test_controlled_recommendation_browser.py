"""Opt-in real Chrome check of controlled Continue replacement, fully offline."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from app.recommendations.presentation import normalize_recommendation_card, render_recommendation_answer
from tests.test_controlled_recommendation_output import BAD, _card
from tests.test_zotistics_cards import NODE


@pytest.mark.skipif(os.environ.get("SOLON_BROWSER_CHECK") != "1", reason="Optional controlled Continue Chrome verification")
def test_real_browser_controlled_continue_replaces_old_primary_text_and_cards():
    if not NODE:
        pytest.skip("Node is required for Chrome verification")
    old = normalize_recommendation_card(_card(), "en")
    fresh = normalize_recommendation_card(_card(code="36210", num="B", time="11:00–12:20"), "en")
    fixture = {"old": old, "fresh": fresh,
               "old_text": render_recommendation_answer([old], "en"),
               "fresh_text": render_recommendation_answer([fresh], "en"), "bad": BAD}
    script = r"""
const {chromium}=require('playwright');
const fs=require('node:fs');
const assert=require('node:assert/strict');
(async()=>{
  const browser=await chromium.launch({headless:true,
    executablePath:process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  try {
    const page=await browser.newPage(); const errors=[];
    page.on('pageerror', error=>errors.push(error.message));
    await page.route('https://**', route=>route.abort());
    await page.setContent('<main id="chatScroll"></main>');
    await page.addScriptTag({content:`
      const API=''; let currentSessionId='fixture'; let currentResponseLanguage='en';
      let currentTermContext={term:'2026 Fall'};
      const addedCourses=new Set(), addedSectionKeys=new Set();
      function solonIcon(){return '';}
      function scheduleCourseKey(term,cid){return term+'|'+cid;}
      function scheduleEntryKey(term,cid,sec){return term+'|'+cid+':'+sec;}
    `});
    const ui=fs.readFileSync('static/js/ui.js','utf8');
    await page.addScriptTag({content:ui.slice(0,ui.indexOf("document.querySelectorAll('[data-icon]')"))});
    await page.addScriptTag({content:ui.slice(ui.indexOf('function displayTerm(term)'))});
    const api=fs.readFileSync('static/js/api-client.js','utf8');
    await page.addScriptTag({content:api.match(/function escAttr\(s\) \{[\s\S]*?\n\}/)[0]});
    for(const name of ['chat','cards','schedule','sessions','plan']) await page.addScriptTag({content:fs.readFileSync(`static/js/${name}.js`,'utf8')});
    const result=await page.evaluate(async fixture=>{
      window.fetch=async()=>({ok:true});
      const wrap=document.createElement('div'); wrap._responseLanguage='en';
      wrap.innerHTML='<div class="msg-ai-body"></div>';
      wrap.firstElementChild.innerHTML=formatMarkdown(fixture.old_text);
      document.getElementById('chatScroll').appendChild(wrap);
      replaceControlledRecommendationCards(wrap,[fixture.old]);
      let immediate=false;
      window.consumeSSE=async(_res,handlers)=>{
        handlers.cards_proposed({controlled_recommendation:true,replace:true,cards:[fixture.fresh]});
        immediate=wrap.querySelectorAll('.course-card').length===1 && wrap.textContent.includes('36210');
        handlers.token({text:fixture.fresh_text});
        handlers.meta({controlled_recommendation:true,cards:[fixture.fresh],final_answer:fixture.fresh_text,response_language:'en'});
        handlers.done();
      };
      await continueAgent(wrap,'fixture-continuation',document.createElement('button'));
      await continueAgent(wrap,'fixture-continuation-2',document.createElement('button'));
      return {immediate,cardCount:wrap.querySelectorAll('.course-card').length,
              bodyCount:wrap.querySelectorAll(':scope > .msg-ai-body').length,
              text:wrap.textContent,reason:wrap.querySelector('.cc-reason').textContent};
    },JSON.parse(process.argv[1]));
    assert.equal(result.immediate,true); assert.equal(result.cardCount,1); assert.equal(result.bodyCount,1);
    assert(result.text.includes('36210') && result.text.includes('11:00'));
    assert(!result.text.includes('36200') && !result.text.includes('09:30'));
    assert(!result.text.includes(JSON.parse(process.argv[1]).bad));
    assert(!result.reason.includes('GE-III')); assert.deepEqual(errors,[]);
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([NODE, "-e", script, json.dumps(fixture)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
