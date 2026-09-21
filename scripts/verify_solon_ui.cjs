/* Offline browser regression check. Starts a local static server; run:
   NODE_PATH=/path/to/playwright/node_modules node scripts/verify_solon_ui.cjs
   Optional: SOLON_BASE_URL, CHROME_PATH, SOLON_ARTIFACT_DIR.
   All API responses are isolated fixtures; no account, LLM or live course data is used. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
let base = process.env.SOLON_BASE_URL;
const output = process.env.SOLON_ARTIFACT_DIR || '/tmp/solon-ui-check';
fs.mkdirSync(output, {recursive: true});
const today = new Date().toISOString();
const earlier = '2026-09-12T14:41:00Z';
const courses = [
  ['ICS6B', 'Boolean Algebra and Logic', 'MWF', '10:00', '10:50', '30001'],
  ['STATS67', 'Introduction to Probability and Statistics', 'TuTh', '11:00', '12:20', '30002'],
  ['PSYCH7A', 'Introduction to Psychology', 'TuTh', '14:00', '15:20', '30003'],
  ['ICS33', 'Intermediate Programming', 'MWF', '11:00', '11:50', '30004'],
].map(([course_id,title,days,start_time,end_time,code]) => {
  const section = {section_num:'A', section_code:code, section_type:'Lec', days, start_time, end_time, status:'OPEN', instructors:['EXAMPLE, A.']};
  return {course_id,title,term:'2026 Fall',units:'4',primary_code:code,found:true,reason:'Example structured recommendation for visual verification.',sections:[section],section_groups:[{letter:'A',primary:{...section,num:'A',code,type:'Lec'},secondaries:[]}]};
});
const primarySession = 'sess_abc123';
const earlierSession = 'sess_def456';
const metas = [
  {session_id:primarySession,title:'Plan my fall quarter',created_at:today,last_active_at:today},
  {session_id:earlierSession,title:'CSE选课推荐',created_at:earlier,last_active_at:earlier},
];
const turns = [
  {role:'user',content:'Plan my fall — I need 16 units, nothing before 10 AM, and I want Friday afternoons free.',timestamp:today},
  {role:'assistant',content:'Here’s a 16-unit fall that meets all three of your constraints. I kept your three courses and added **ICS 33**, the one core course you’re now eligible for.',cards:courses,followups:['Why ICS 33?']},
];
function entry(card, section = 'A') {
  const s = card.sections.find(s => s.section_num === section);
  return {course_id:card.course_id,term:card.term,section,units:card.units,materialization_status:s.start_time ? 'resolved' : 'tba',materialized_section:s};
}
function eventsFor(entries) {
  return entries.flatMap(e => {
    const card = courses.find(c=>c.course_id===e.course_id);
    if (!card) return [];
    const s=card.sections.find(s=>s.section_num===e.section);
    if (!s?.start_time || !s.end_time) return [];
    return (s.days.match(/Tu|Th|[MWF]/g) || []).map(day=>({course_id:e.course_id,term:e.term,section_num:s.section_num,section_code:s.section_code,section_type:s.section_type,day:({M:'Mon',Tu:'Tue',W:'Wed',Th:'Thu',F:'Fri'})[day],start:s.start_time,end:s.end_time,title:card.title}));
  });
}
(async()=>{
  const root = path.resolve(__dirname, '..');
  const types = {'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml'};
  const server = !base && http.createServer((request, response) => {
    const file = path.resolve(root, '.' + new URL(request.url, 'http://localhost').pathname);
    if (!file.startsWith(path.join(root, 'static') + path.sep)) return response.writeHead(404).end();
    fs.readFile(file, (error, data) => {
      if (error) return response.writeHead(404).end();
      response.writeHead(200, {'Content-Type':types[path.extname(file)] || 'application/octet-stream'}).end(data);
    });
  });
  if (server) {
    await new Promise(resolve=>server.listen(0, '127.0.0.1', resolve));
    base = `http://127.0.0.1:${server.address().port}`;
  }
  const browser = await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:900},deviceScaleFactor:1});
    page.setDefaultTimeout(15000);
    const errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.route('https://**',route=>route.abort());
    let saved=courses.slice(0,3).map(c=>entry(c));
    const writes=[];
    let failAdd=false;
    let addDelay=0;
    let scheduleDelay=0;
    let chatDelay=0;
    let lastChat='';
    let languageFixture=false;
    let fixtureLanguage='en';
    let fixtureLimit=false;
    const schedule = () => ({ok:true,pending_schedule:saved,events:eventsFor(saved),schedule_validation:{valid:true,warnings:[],conflicts:[],unknowns:[]}});
    await page.route('**/api/**', async route=>{
      const req=route.request(), url=new URL(req.url());
      const send=data=>route.fulfill({json:data});
      if(url.pathname==='/api/auth/me') return send({user:{id:'visual_fixture',email:'fixture@uci.edu'}});
      if(url.pathname==='/api/memory/me') return send({profile:{major:'Computer Science',year:'Sophomore',display_name:'Example Student'},preferences:[],facts:[]});
      if(url.pathname==='/api/term-state') return send({default_term:'Fall 2026',automatic_term:'Fall 2026'});
      if(url.pathname==='/api/sessions/me') return send({sessions:metas});
      if(url.pathname===`/api/sessions/me/${primarySession}`) return send({...metas[0],default_term:'Fall 2026',turns});
      if(url.pathname===`/api/sessions/me/${earlierSession}` && languageFixture) return send({...metas[1],turns:[
        {role:'user',content:'推荐课程',query_terms:['2026 Fall']},
        {role:'assistant',content:'历史回答。'},
        {role:'user',content:'CS161',query_terms:['2026 Fall']},
        {role:'assistant',content:'This old answer used the wrong language.'},
        {role:'user',content:'yes',query_terms:['2027 Winter']},
        {role:'assistant',content:'English follow-up.'},
      ]});
      if(url.pathname===`/api/sessions/me/${earlierSession}`) return send({...metas[1],default_term:'Fall 2026',turns:[{role:'user',content:'CSE选课推荐',timestamp:earlier},{role:'assistant',content:'Here is your earlier conversation, restored from its saved history.'}]});
      if(url.pathname==='/api/schedule') {
        const data = url.searchParams.get('session_id')===primarySession ? structuredClone(schedule()) : {ok:true,pending_schedule:[],events:[],schedule_validation:{valid:true,warnings:[],conflicts:[],unknowns:[]}};
        if(scheduleDelay) await new Promise(r=>setTimeout(r,scheduleDelay));
        return send(data);
      }
      if(url.pathname==='/api/schedule/add') {
        if(failAdd) return route.fulfill({status:503,json:{detail:'Offline test failure'}});
        const data=req.postDataJSON();writes.push(data);
        const card=courses.find(c=>c.course_id===data.course_id);
        if(addDelay) await new Promise(r=>setTimeout(r,addDelay));
        if(!saved.some(e=>e.course_id===data.course_id && e.section===data.section)) saved.push(entry(card, data.section));
        return send(schedule());
      }
      if(url.pathname==='/api/schedule/remove') {
        const data=req.postDataJSON();saved=saved.filter(e=>e.course_id!==data.course_id || e.section!==data.section);return send(schedule());
      }
      if(url.pathname==='/api/schedule/refresh') return send(schedule());
      if(url.pathname==='/api/chat/stream') {
        const data=req.postDataJSON();lastChat=data.message;
        if(chatDelay) await new Promise(r=>setTimeout(r,chatDelay));
        let chunks=[{type:'token',text:'I can help you plan.'},{type:'meta',session_id:'sess_aaaaaa',default_term:'Fall 2026',final_answer:'I can help you plan.',cards:[],followups:[]},{type:'done'}];
        if(languageFixture) {
          const zh=fixtureLanguage==='zh';
          const label=zh?'查询 ICS33 排课 · 2026年秋季':'Checking ICS33 sections · 2026 Fall';
          const answer=zh?'已找到课程信息。':'Course details are ready.';
          chunks=[
            {type:'tool_call_start',name:'get_sections',label,response_language:fixtureLanguage},
            {type:'tool_call_done',name:'get_sections',ok:true,section_count:2,fetch_summary:[{ok:true,source_role:'registrar_websoc_course_results',provides_evidence:true,url:'https://reg.uci.edu',depth:1}]},
            ...(fixtureLimit?[{type:'limit_reached',continuation_id:'fixture',response_language:fixtureLanguage}]:[]),
            {type:'token',text:answer},
            {type:'meta',session_id:'sess_aaaaaa',response_language:fixtureLanguage,query_terms:['2026 Fall'],query_term_source:'inferred',final_answer:answer},
            {type:'done'},
          ];
        }
        return route.fulfill({contentType:'text/event-stream',body:chunks.map(c=>'data: '+JSON.stringify(c)+'\n\n').join('')});
      }
      if(url.pathname==='/api/chat/continue' && languageFixture) {
        const chunks=[
          {type:'tool_call_start',name:'get_sections',label:'查询 ICS33 排课 · 2026年秋季',response_language:'zh'},
          {type:'tool_call_done',name:'get_sections',ok:true,section_count:0,offering_status:'not_offered',authoritative:true},
          {type:'token',text:'续查完成。'}, {type:'meta',final_answer:'续查完成。'}, {type:'done'},
        ];
        return route.fulfill({contentType:'text/event-stream',body:chunks.map(c=>'data: '+JSON.stringify(c)+'\n\n').join('')});
      }
      return send({});
    });
    const settle=()=>page.waitForTimeout(700);
    const noOverflow=async()=>{
      const size=await page.evaluate(()=>({width:innerWidth,doc:document.documentElement.scrollWidth,body:document.body.scrollWidth,chat:document.querySelector('.chat-panel').getBoundingClientRect().width}));
      assert(size.doc<=size.width && size.body<=size.width,JSON.stringify(size));
      return size;
    };
    await page.goto(`${base}/static/index.html`);
    await page.getByRole('button',{name:'Plan my fall quarter',exact:true}).waitFor();
    assert.equal(await page.locator('.nav-item').count(),4);
    assert.equal(await page.locator('#sendBtn').isDisabled(),true);
    assert.equal(await page.locator('#schedulePanel').evaluate(e=>e.inert),true);
    await page.screenshot({path:path.join(output,'00-empty.png')});
    await page.getByRole('button',{name:'Plan my fall quarter',exact:true}).click();
    await page.locator('.plan-pick').first().waitFor();
    await settle();
    assert.equal(await page.locator('#threadTitle').textContent(),'Plan my fall quarter');
    assert.equal(await page.locator('.plan-tag.is-kept').count(),3);
    assert.equal(await page.locator('.delivered').count(),1);
    assert.equal(await page.locator('.sessions-label').allTextContents().then(x=>x.join(',')),'Today,Earlier');
    await noOverflow();
    await page.screenshot({path:path.join(output,'01-chat-default.png')});
    await page.locator('#toggleScheduleBtn').click();await settle();
    assert.equal(await page.locator('.left-panel').evaluate(e=>Math.round(e.getBoundingClientRect().width)),60);
    assert.equal(await page.locator('#schedulePanel').evaluate(e=>Math.round(e.getBoundingClientRect().width)),820);
    assert.equal(await page.locator('.sg-event.is-suggested').count(),0);
    assert.deepEqual((await page.locator('.metric-value').allTextContents()).slice(0,3),['12','3','10 AM']);
    const freeFriday = await page.locator('.metric-value').nth(3).textContent();
    assert.match(freeFriday,/^Free (🎉|🥳|😊|😄)$/u);
    assert.equal(await page.locator('.sg-time-label').first().textContent(),'8 AM');
    assert.equal(await page.locator('.sg-time-label').last().textContent(),'10 PM');
    assert.equal(await page.locator('.sg-cell[data-day="Mon"]').count(),14);
    assert.equal(await page.locator('#scheduleSync').textContent(),'In sync');
    await page.locator('.plan-row').last().hover();
    assert.equal(await page.locator('.sg-event.is-highlighted').count(),0);
    assert(!/suggested/i.test(await page.locator('#scheduleBody').textContent()));
    await page.mouse.move(400,60);await settle();
    await noOverflow();await page.screenshot({path:path.join(output,'02-split-open.png')});
    await page.setViewportSize({width:1280,height:800});await settle();
    assert.equal(await page.locator('#schedulePanel').evaluate(e=>Math.round(e.getBoundingClientRect().width)),720);
    const layout=await noOverflow();assert(layout.chat>=490,JSON.stringify(layout));
    await page.screenshot({path:path.join(output,'03-split-1280.png')});
    // Unadded recommendations never appear in Schedule, even after a failed add.
    failAdd=true;await page.locator('.plan-add').click();
    await page.locator('.schedule-toast').waitFor();
    assert.equal(await page.locator('.plan-add').isDisabled(),false);
    assert.equal(await page.locator('.sg-event.is-suggested').count(),0);
    failAdd=false;await page.locator('.plan-add').click();
    await page.waitForFunction(()=>document.querySelectorAll('.sg-event[data-cid="ICS33"]').length===3);
    assert.equal(writes.length,1);assert.equal(writes[0].course_id,'ICS33');assert.equal(writes[0].section,'A');
    assert.deepEqual(await page.locator('.metric-value').allTextContents(),['16','4','10 AM',freeFriday]);
    assert.equal(await page.locator('.plan-add').isDisabled(),true);
    // Details still provide the original section controls and server-backed removal.
    await page.locator('.plan-details-toggle').last().click();
    await page.locator('.plan-details').last().getByRole('button',{name:'1 section',exact:false}).click();
    assert.equal(await page.locator('.plan-details').last().locator('.cc-sg-add.added').count(),1);
    await page.locator('.sg-event[data-cid="ICS33"]').first().click();
    await page.getByRole('button',{name:'Remove ICS33',exact:true}).click();
    await page.waitForFunction(()=>document.querySelectorAll('.sg-event[data-cid="ICS33"]').length===0);
    assert.equal(await page.locator('.plan-add').isDisabled(),false);
    // A card's + immediately adds every actual meeting. Repeated clicks while
    // saving submit once, and an older GET cannot overwrite the new meetings.
    const sectionPlus = page.locator('.plan-details').last().locator('.cc-sg-add');
    failAdd=true;
    await sectionPlus.click();
    await page.waitForFunction(()=>[...document.querySelectorAll('.cc-sg-add[data-cid="ICS33"]')].every(b=>!b.disabled && b.getAttribute('aria-pressed')==='false'));
    assert.equal(await sectionPlus.getAttribute('aria-pressed'),'false');
    assert.equal(await sectionPlus.getAttribute('aria-label'),'Add section to schedule');
    failAdd=false; scheduleDelay=600; addDelay=120;
    await page.evaluate(()=>{ void loadScheduleForSession(currentSessionId); });
    const writesBefore=writes.length;
    await sectionPlus.evaluate(button=>{button.click();button.click();});
    await page.waitForFunction(()=>document.querySelectorAll('.sg-event[data-cid="ICS33"]').length===3);
    await page.waitForTimeout(750);
    assert.equal(writes.length,writesBefore+1);
    assert.equal(await page.locator('.sg-event[data-cid="ICS33"]').count(),3);
    assert.equal(await sectionPlus.getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('.schedule-untimed-item').count(),0);
    scheduleDelay=0;addDelay=0;
    await sectionPlus.click();
    await page.waitForFunction(()=>document.querySelectorAll('.sg-event[data-cid="ICS33"]').length===0);
    await page.getByRole('button',{name:'Close weekly schedule',exact:true}).click();await settle();
    await page.getByRole('button',{name:'CSE选课推荐',exact:true}).click();await settle();
    assert.equal(await page.locator('#threadTitle').textContent(),'CSE选课推荐');
    assert.equal(await page.locator('.plan-object').count(),0);
    assert.equal(await page.locator('.msg-ai-body').textContent(),'Here is your earlier conversation, restored from its saved history.');
    await page.screenshot({path:path.join(output,'04-thread-switch.png')});
    // A slow previous schedule response must never repopulate another conversation.
    scheduleDelay=600;
    await page.getByRole('button',{name:'Plan my fall quarter',exact:true}).click();
    await page.locator('.plan-object').waitFor();
    await page.getByRole('button',{name:'New thread',exact:true}).click();await settle();
    assert.equal(await page.locator('#threadTitle').textContent(),'New conversation');
    assert.equal(await page.locator('.sg-event').count(),0);
    scheduleDelay=0;
    await page.locator('#userInput').fill('Plan a quiet quarter');
    assert.equal(await page.locator('#sendBtn').isDisabled(),false);
    await page.locator('#userInput').press('Enter');
    await page.waitForFunction(()=>document.querySelector('.delivered')?.textContent==='Delivered');
    assert.equal(lastChat,'Plan a quiet quarter');
    assert.equal(await page.locator('#threadTitle').textContent(),'Plan a quiet quarter');
    assert.equal(await page.locator('#sendBtn').isDisabled(),true);
    // Switching away during streaming must discard the old response.
    chatDelay=700;
    await page.locator('#userInput').fill('This response should be discarded');
    await page.locator('#userInput').press('Enter');
    await page.locator('.thinking').waitFor();
    await page.getByRole('button',{name:'New thread',exact:true}).click();await page.waitForTimeout(850);
    assert.equal(await page.locator('.msg').count(),0);
    assert.equal(await page.locator('#threadTitle').textContent(),'New conversation');
    chatDelay=0;
    // Mobile drawer, schedule overlay, and keyboard entry.
    await page.setViewportSize({width:390,height:844});await settle();
    await noOverflow();
    await page.getByRole('button',{name:'Show conversations',exact:true}).click();
    await page.getByRole('button',{name:'Plan my fall quarter',exact:true}).click();await settle();
    await page.screenshot({path:path.join(output,'05-mobile-chat.png')});
    await page.locator('#toggleScheduleBtn').click();await settle();
    assert.equal(await page.locator('#schedulePanel').evaluate(e=>Math.round(e.getBoundingClientRect().top)),0);
    await noOverflow();await page.screenshot({path:path.join(output,'06-mobile-schedule.png')});
    await page.getByRole('button',{name:'Close weekly schedule',exact:true}).click();await settle();
    await page.setViewportSize({width:1440,height:900});await settle();
    await page.emulateMedia({reducedMotion:'reduce'});
    await page.locator('#toggleScheduleBtn').click();
    assert.equal(await page.locator('#schedulePanel').evaluate(e=>getComputedStyle(e).transitionDuration),'1e-05s');
    // Unknown data stays unknown; units count a lecture/discussion course once.
    const edges = await page.evaluate(() => {
      const unknown = scheduleMetrics([{course_id:'TBA101',term:'2026 Fall',section:'A'}], []);
      const duplicate = scheduleMetrics([
        {course_id:'ICS33',term:'2026 Fall',section:'A',units:'4'},
        {course_id:'ICS33',term:'2026 Fall',section:'A1',units:'4'},
      ], [{course_id:'ICS33',term:'2026 Fall',section_num:'A',day:'Mon',start:'11:00',end:'11:50'}]);
      const fridayEntries = [{course_id:'FRI101',term:'2026 Fall',section:'A'}];
      const fridayMeeting = {course_id:'FRI101',term:'2026 Fall',section_num:'A',day:'Fri',start:'11:00',end:'12:00'};
      const endsAtNoon = scheduleMetrics(fridayEntries,[fridayMeeting]).friday;
      const crossesNoon = scheduleMetrics(fridayEntries,[{...fridayMeeting,end:'12:01'}]).friday;
      const ambiguous = {course_id:'ICS99',sections:[
        {section_num:'A',section_type:'Lec'}, {section_num:'B',section_type:'Lec'},
      ]};
      return {unknown,duplicate,endsAtNoon,crossesNoon,range:exactUnits('1-4'),invalid:parseTimeToMinutes('25:99'),noChosenSection:planPrimary(ambiguous)};
    });
    assert.equal(edges.endsAtNoon,'Free');assert.equal(edges.crossesNoon,'Busy');
    assert.equal(edges.unknown.units,null);assert.equal(edges.unknown.friday,'Unknown');
    assert.equal(edges.duplicate.count,1);assert.equal(edges.duplicate.units,4);
    assert.equal(edges.range,null);assert.equal(edges.invalid,-1);assert.equal(edges.noChosenSection,null);
    // The screenshot's online TBA lecture/discussion are visible in Schedule
    // without invented weekly blocks, and each section can be removed.
    const onlineSections=[
      {section_num:'A',section_code:'36250',section_type:'Lec'},
      {section_num:'A1',section_code:'36251',section_type:'Dis'},
    ].map(s=>({...s,days:'TBA',start_time:'',end_time:'',time_is_tba:true,location:'ON LINE',status:'OPEN',instructors:['EXAMPLE, N.']}));
    const compact=s=>({...s,num:s.section_num,code:s.section_code,type:s.section_type,time:'TBA'});
    const onlineCard={course_id:'I&C SCI 139W',title:'Critical Writing',term:'2026 Fall',units:'4',found:true,primary_code:'36250',requires_secondary:true,secondary_type:'Dis',sections:onlineSections,
      section_groups:[{letter:'A',primary:compact(onlineSections[0]),secondaries:[compact(onlineSections[1])]}]};
    courses.push(onlineCard);
    // Load this fixture through the normal saved-conversation renderer.
    await page.reload();
    await page.getByRole('button',{name:'Plan my fall quarter',exact:true}).click();
    await page.locator('.plan-object').waitFor();await settle();
    await page.locator('#toggleScheduleBtn').click();await settle();
    await page.locator('.plan-add').click();
    await page.waitForFunction(()=>document.querySelector('.course-card[id="card-I&C SCI 139W"] .cc-sg-wrap')?.classList.contains('open'));
    await settle();
    for (const sec of ['A','A1']) {
      const plus=page.locator(`.cc-sg-add[data-cid="I&C SCI 139W"][data-sec="${sec}"]`);
      if (sec === 'A1') await plus.locator('xpath=ancestor::tr').hover();
      await plus.click();
      await page.locator(`.schedule-untimed-item[data-sec="${sec}"]`).waitFor();
    }
    assert.equal(await page.locator('.sg-event[data-cid="I&C SCI 139W"]').count(),0);
    assert.equal(await page.locator('.schedule-untimed-item').count(),2);
    assert.match(await page.locator('.schedule-untimed').textContent(),/Time TBA/);
    assert.match(await page.locator('.schedule-untimed').textContent(),/ON LINE/);
    assert.equal(await page.locator('.metric-value').nth(1).textContent(),'4');
    assert.equal(await page.locator('.metric-value').nth(3).textContent(),'Unknown');
    assert(!/suggested/i.test(await page.locator('#scheduleBody').textContent()));
    await page.locator('#scheduleBody').evaluate(el=>{el.scrollTop=0;});
    await page.screenshot({path:path.join(output,'07-tba-schedule.png')});
    await page.setViewportSize({width:390,height:844});await settle();
    await noOverflow();
    await page.screenshot({path:path.join(output,'08-tba-mobile.png')});
    await page.setViewportSize({width:1440,height:900});await settle();
    await page.evaluate(()=>loadScheduleForSession(currentSessionId));
    assert.equal(await page.locator('.schedule-untimed-item').count(),2);
    await page.locator('.schedule-untimed-item').first().click();
    await page.locator('.schedule-entry[data-cid="I&C SCI 139W"][data-sec="A1"] button').click();
    await page.waitForFunction(()=>document.querySelectorAll('.schedule-untimed-item').length===1);
    assert.equal(await page.locator('.schedule-untimed-item').getAttribute('data-sec'),'A');
    assert.equal(await page.locator('.cc-sg-add[data-cid="I&C SCI 139W"][data-sec="A1"]').getAttribute('aria-pressed'),'false');
    // Changing terms selects a separate grid; early, evening and weekend meetings stay visible.
    await page.evaluate(() => {
      resetPlanObjects();
      pendingScheduleEntries = [
        {course_id:'EDGE101',term:'2026 Fall',section:'A',units:'4'},
        {course_id:'EDGE101',term:'2027 Winter',section:'A',units:'4'},
      ];
      scheduleEvents = [
        {course_id:'EDGE101',term:'2026 Fall',section_num:'A',day:'Sat',start:'07:00',end:'08:20'},
        {course_id:'EDGE101',term:'2027 Winter',section_num:'A',day:'Mon',start:'20:00',end:'21:20'},
      ];
      _hydrateScheduleState();renderScheduleGrid();
    });
    assert.equal(await page.locator('.sg-event').count(),1);
    assert((await page.locator('.sg-day-header').allTextContents()).includes('Sat'));
    assert.equal(await page.locator('.sg-time-label').first().textContent(),'7 AM');
    await page.getByRole('button',{name:'Winter 2027',exact:true}).click();
    assert.equal(await page.locator('.sg-event').count(),1);
    assert.equal(await page.locator('.sg-event').getAttribute('data-term'),'2027 Winter');
    assert.equal(await page.locator('.sg-time-label').last().textContent(),'10 PM');
    assert.equal(await page.locator('.metric-value').nth(1).textContent(),'1');
    // Account tools remain reachable from the collapsed sidebar.
    await page.getByRole('button',{name:'Open user menu',exact:true}).click();
    await page.getByRole('menuitem',{name:'Settings',exact:true}).click();
    assert.equal(await page.locator('#settingsModal').evaluate(e=>e.classList.contains('open')),true);
    await page.getByRole('button',{name:'Close settings',exact:true}).click();
    // Every current user turn owns its response language, including progress and restore.
    languageFixture=true;chatDelay=150;
    await page.getByRole('button',{name:'New thread',exact:true}).click();
    for (const [message,language] of [['推荐课程','zh'],['CS161','zh'],['yes','en'],['继续','zh']]) {
      fixtureLanguage=language;fixtureLimit=message==='继续';
      await page.locator('#userInput').fill(message);await page.locator('#userInput').press('Enter');
      await page.locator('.thinking').waitFor();
      assert.equal(await page.locator('.thinking').getAttribute('aria-label'),language==='zh'?'Solon 正在思考':'Solon is thinking');
      await page.waitForFunction(()=>!currentAbortController);
      const response=page.locator('.msg-ai').last();
      const progress=await response.locator('.tool-label').textContent();
      const audit=await response.locator('.tool-fetch-details summary').textContent();
      const term=await response.locator('.query-term-badge').textContent();
      if(language==='zh') {
        assert(progress.includes('找到 2 个教学班'));assert(audit.includes('实际抓取'));assert(term.includes('2026年秋季（年份自动推断）'));
        assert.equal(await page.locator('#composerSuggestions .followup-chip').count(),0);
      } else {
        assert(progress.includes('2 sections found'));assert.equal(audit,'1 web request');assert(term.includes('Querying: 2026 Fall (year inferred)'));
        assert(!/[\u3400-\u9fff]/u.test(await response.textContent()));
        await page.screenshot({path:path.join(output,'07-english-progress.png')});
      }
    }
    await page.locator('.continue-btn').click();
    await page.locator('.limit-notice').waitFor({state:'detached'});
    assert((await page.locator('.msg-ai').last().textContent()).includes('续查完成。'));
    assert((await page.locator('.tool-label').last().textContent()).includes('官方无匹配'));
    await page.screenshot({path:path.join(output,'08-chinese-progress.png')});
    await page.getByRole('button',{name:'CSE选课推荐',exact:true}).click();
    await page.waitForFunction(()=>document.querySelectorAll('.query-term-badge').length===3);
    assert.deepEqual(await page.locator('.query-term-badge').allTextContents(),['本次查询：2026年秋季','本次查询：2026年秋季','Querying: 2027 Winter']);
    assert.equal(await page.evaluate(()=>currentResponseLanguage),'en');
    // Section failure and audit failure copy also follows each response's own language.
    const copy=await page.evaluate(()=>{
      const samples={};
      for(const language of ['en','zh']) {
        const wrap=startAiMessage(language);
        for(const status of ['unavailable','not_offered']) {
          startToolChip(wrap,'ICS33','get_sections');
          finishToolChip(wrap,false,[],{offering_status:status});
        }
        renderWebFetchSummary(wrap,[{ok:false,error:'Internal English error',source_role:'policy',parent_url:'https://uci.edu'}]);
        samples[language]=wrap.textContent;wrap.remove();
      }
      return samples;
    });
    assert(copy.en.includes('Data unavailable'));assert(copy.en.includes('No sections found'));assert(copy.en.includes('Failed'));
    assert(!/[\u3400-\u9fff]/u.test(copy.en));
    assert(copy.zh.includes('数据不可用'));assert(copy.zh.includes('未找到教学班'));assert(copy.zh.includes('失败'));
    assert(!copy.zh.includes('Internal English error'));
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,viewports:['1440×900','1280×800','390×844'],screenshots:output,checks:['real renderer fixtures','split layout','plan hover','add failure and retry','add/remove API payloads','section picker','thread switching','stale response isolation','streaming','composer','mobile navigation','reduced motion','TBA and unknown units','no arbitrary section selection','cross-term grid','weekend and evening classes','account settings','per-turn language switching','neutral course code language','localized progress and source audit','localized continuation','mixed-language history restore'],pageErrors:errors},null,2));
  } finally { await browser.close(); if (server) {server.closeAllConnections(); await new Promise(resolve=>server.close(resolve));} }
})().catch(e=>{console.error(e);process.exitCode=1;});
