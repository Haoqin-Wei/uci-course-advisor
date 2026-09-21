/* Uses a temporary real FastAPI server started by the integration test.
 * Only ancillary auth/profile UI calls are fixtures; schedule requests,
 * persistence, section resolution, and calendar events use production code. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');
const fs=require('node:fs');
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:900}});
    page.setDefaultTimeout(12000);
    const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('https://**',route=>route.abort());
    await page.route('**/api/auth/me',route=>route.fulfill({json:{user:{id:'demo_001',email:'schedule-fixture@uci.edu'}}}));
    await page.route('**/api/memory/me',route=>route.fulfill({json:{profile:{major:'Computer Science',year:'Junior'},facts:[],preferences:[]}}));
    await page.route('**/api/term-state',route=>route.fulfill({json:{default_term:'Fall 2026',automatic_term:'Fall 2026'}}));
    await page.goto(`${process.env.SOLON_BASE_URL}/static/index.html`);
    await page.getByRole('button',{name:'Registrar schedule integration',exact:true}).click();
    const detail=page.locator('.plan-details-toggle');
    await detail.click();
    await page.locator('.cc-sg-toggle').click();
    const plus=page.locator('.cc-sg-add[data-cid="I&C SCI 45C"][data-sec="A"]');
    const before=Date.now();
    const responseWait=page.waitForResponse(r=>r.url().endsWith('/api/schedule/add')&&r.request().method()==='POST');
    await plus.click();
    const response=await responseWait;
    const payload=await response.json();
    assert.equal(response.status(),200);
    assert.deepEqual(payload.events.map(e=>[e.day,e.start,e.end]),[['Tue','12:30','13:50'],['Thu','12:30','13:50']]);
    await page.waitForFunction(()=>document.querySelectorAll('.sg-event[data-cid="I&C SCI 45C"]').length===2);
    await page.waitForFunction(()=>document.querySelector('#schedulePanel').getBoundingClientRect().width>=819);
    const addVisibleMs=Date.now()-before;
    assert.equal(await plus.getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('.schedule-untimed-item').count(),0);
    assert(!/suggested/i.test(await page.locator('#scheduleBody').textContent()));
    assert.match(await page.locator('.sg-event').first().textContent(),/12:30 PM–1:50 PM/);
    // The actual block geometry must start at 12:30 and span 80 minutes.
    const geometry=await page.locator('.sg-event').first().evaluate(el=>({top:parseFloat(el.style.top),height:parseFloat(el.style.height),left:el.getBoundingClientRect().left}));
    assert.equal(geometry.top,297);assert(Math.abs(geometry.height-(80/60*58-4))<.01);
    const out=process.env.SOLON_ARTIFACT_DIR||'/tmp/solon-schedule-api-check';
    fs.mkdirSync(out,{recursive:true});
    await page.locator('#scheduleBody').evaluate(el=>{el.scrollTop=0;});
    await page.screenshot({path:path.join(out,'45c-real-api.png')});
    // Saved old-format cards still work after a full reload.
    await page.reload();
    await page.getByRole('button',{name:'Registrar schedule integration',exact:true}).click();
    await page.waitForFunction(()=>document.querySelectorAll('.sg-event').length===2);
    await page.locator('#toggleScheduleBtn').click();
    await page.locator('.sg-event').first().click();
    const removedWait=page.waitForResponse(r=>r.url().endsWith('/api/schedule/remove'));
    await page.getByRole('button',{name:'Remove I&C SCI 45C',exact:true}).click();
    const removed=await (await removedWait).json();
    assert.deepEqual(removed.events,[]);
    await page.waitForFunction(()=>document.querySelectorAll('.sg-event').length===0);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,realScheduleAPI:true,addVisibleMs,geometry,screenshot:path.join(out,'45c-real-api.png'),pageErrors:errors}));
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
