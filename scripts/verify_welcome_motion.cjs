/* Local-only browser checks. Serve the repo on :8765; all APIs use fixtures. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const base = process.env.SOLON_BASE_URL || 'http://127.0.0.1:8765';
const output = process.env.SOLON_ARTIFACT_DIR || '/tmp/solon-welcome-check';
fs.mkdirSync(output,{recursive:true});
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:900}});
    const errors=[], sends=[];
    let fail=false, delay=850;
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('https://**',route=>route.abort());
    await page.route('**/api/**',async route=>{
      const request=route.request(), url=new URL(request.url());
      const send=data=>route.fulfill({json:data});
      if(url.pathname==='/api/auth/me') return send({user:{id:'welcome_fixture',email:'welcome-fixture@uci.edu'}});
      if(url.pathname==='/api/memory/me') return send({profile:{major:'Computer Science',year:'Junior'}});
      if(url.pathname==='/api/academic/profile') return send({completed_courses:[],gpa_available:false});
      if(url.pathname==='/api/term-state') return send({default_term:'Fall 2026',automatic_term:'Fall 2026'});
      if(url.pathname==='/api/sessions/me') return send({sessions:[{session_id:'sess_history',title:'Saved course question'}]});
      if(url.pathname==='/api/sessions/me/sess_history') return send({session_id:'sess_history',title:'Saved course question',turns:[{role:'user',content:'A previous question'},{role:'assistant',content:'A previous answer'}]});
      if(url.pathname==='/api/schedule') return send({pending_schedule:[],events:[]});
      if(url.pathname==='/api/chat/stream') {
        const body=request.postDataJSON();sends.push(body);
        await new Promise(resolve=>setTimeout(resolve,delay));
        if(fail) return route.fulfill({status:503,json:{detail:'Fixture unavailable'}});
        return route.fulfill({contentType:'text/event-stream',body:[
          {type:'token',text:'Let’s find a plan that works for you.'},
          {type:'meta',session_id:'sess_new',final_answer:'Let’s find a plan that works for you.',followups:[]},
          {type:'done'},
        ].map(event=>'data: '+JSON.stringify(event)+'\n\n').join('')});
      }
      return send({});
    });
    const welcome=()=>page.locator('#chatPanel.is-welcome').waitFor();
    const done=()=>page.waitForFunction(()=>!currentAbortController && !welcomeTransition);
    const center=async()=>{
      await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
      const boxes=await page.evaluate(()=>{
        const panel=document.getElementById('chatPanel').getBoundingClientRect();
        const row=document.querySelector('.input-row').getBoundingClientRect();
        const title=document.getElementById('welcomeGreeting').getBoundingClientRect();
        const shortcuts=document.getElementById('welcomeFollowups').getBoundingClientRect();
        return {panel:{top:panel.top,height:panel.height,left:panel.left,width:panel.width},row:{top:row.top,bottom:row.bottom,left:row.left,right:row.right},title:{top:title.top,bottom:title.bottom},shortcuts:{top:shortcuts.top,bottom:shortcuts.bottom},width:innerWidth,scroll:document.documentElement.scrollWidth};
      });
      assert(boxes.row.top>boxes.panel.height*.3 && boxes.row.bottom<boxes.panel.height*.7,JSON.stringify(boxes));
      assert(boxes.title.bottom<boxes.row.top && boxes.shortcuts.top>boxes.row.bottom,JSON.stringify(boxes));
      assert(boxes.row.left>=boxes.panel.left && boxes.row.right<=boxes.width && boxes.scroll<=boxes.width,JSON.stringify(boxes));
      assert.equal(await page.locator('#userInput').count(),1);
      return boxes;
    };
    await page.goto(`${base}/static/index.html`);
    await page.getByRole('button',{name:'Saved course question',exact:true}).waitFor();
    await welcome();
    const initial=await center();
    const greeting=await page.locator('#welcomeGreeting').textContent();
    await page.screenshot({path:path.join(output,'01-centered-welcome.png'),animations:'disabled'});
    await page.locator('#userInput').fill('Help me plan my quarter');
    await page.evaluate(()=>loadSidebar(true));
    assert.equal(await page.locator('#welcomeGreeting').textContent(),greeting);
    // The same input node, term label and request payload survive the transition.
    await page.evaluate(()=>{window.originalComposer=document.getElementById('userInput');});
    await page.locator('#sendBtn').click();
    await page.waitForFunction(()=>welcomeTransition?.animations.length>=4);
    assert.equal(sends.length,1);
    assert.deepEqual(sends[0],{message:'Help me plan my quarter',session_id:''});
    await page.evaluate(()=>{for(const animation of welcomeTransition.animations){animation.pause();animation.currentTime=160;}});
    const middle=await page.locator('.input-row').boundingBox();
    assert(middle.y>initial.row.top && middle.y<820,JSON.stringify(middle));
    await page.screenshot({path:path.join(output,'02-first-send-motion.png')});
    await page.evaluate(()=>{for(const animation of welcomeTransition.animations) animation.play();});
    await done();
    assert.equal(await page.evaluate(()=>window.originalComposer===document.getElementById('userInput')),true);
    assert.equal(await page.locator('.welcome-exit').count(),0);
    assert.equal(await page.locator('#welcomeState').isVisible(),false);
    assert.equal(await page.locator('#composerTermLabel').textContent(),'Fall 2026');
    assert((await page.locator('.input-row').boundingBox()).y>800);
    await page.screenshot({path:path.join(output,'03-conversation.png')});
    await page.locator('#userInput').fill('And ICS 33?');await page.locator('#userInput').press('Enter');
    assert.equal(await page.evaluate(()=>welcomeTransition),null);
    await done();assert.equal(sends.length,2);
    // New chats get a fresh phrase, never overwritten by async sidebar refreshes.
    await page.getByRole('button',{name:'New thread',exact:true}).click();await welcome();
    assert.notEqual(await page.locator('#welcomeGreeting').textContent(),greeting);
    await center();
    await page.getByRole('button',{name:'Saved course question',exact:true}).click();
    await page.getByText('A previous answer',{exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>welcomeTransition),null);
    assert.equal(await page.locator('#welcomeState').isVisible(),false);
    // Navigation during launch cancels only visual effects, without stale cleanup.
    await page.getByRole('button',{name:'New thread',exact:true}).click();
    await page.getByRole('button',{name:'Recommend courses',exact:true}).click();
    await page.getByRole('button',{name:'New thread',exact:true}).click();
    await page.waitForTimeout(950);await welcome();await center();
    assert.equal(await page.locator('.msg').count(),0);
    assert.equal(await page.locator('.welcome-exit').count(),0);
    // Failure still leaves a usable bottom composer; no delayed/repeated request.
    fail=true;delay=100;
    await page.locator('#userInput').fill('Try this request');await page.locator('#userInput').press('Enter');await done();
    assert(await page.getByText('Unable to connect. Please try again.',{exact:true}).isVisible());
    assert.equal(await page.locator('#userInput').isEditable(),true);
    fail=false;
    await page.getByRole('button',{name:'New thread',exact:true}).click();
    await page.locator('#toggleScheduleBtn').click();await page.waitForTimeout(700);
    await center();await page.locator('#toggleScheduleBtn').click();await page.waitForTimeout(700);
    // System motion preferences skip the flight entirely.
    await page.emulateMedia({reducedMotion:'reduce'});
    await page.locator('#userInput').fill('A quiet start');await page.locator('#userInput').press('Enter');
    assert.equal(await page.evaluate(()=>welcomeTransition),null);
    await done();
    await page.getByRole('button',{name:'New thread',exact:true}).click();
    for(const [width,height] of [[390,844],[320,640],[844,390]]) {
      await page.setViewportSize({width,height});await center();
      await page.screenshot({path:path.join(output,`04-welcome-${width}.png`),animations:'disabled'});
    }
    await page.setViewportSize({width:390,height:844});
    await page.emulateMedia({reducedMotion:'no-preference'});
    await page.locator('#userInput').fill('推荐课程');await page.locator('#userInput').press('Enter');await done();
    assert((await page.locator('.input-row').boundingBox()).y>700);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,screenshots:output,checks:['centered desktop, split and mobile layouts','stable random greetings','first-send flight with immediate request','single composer retained','normal follow-up and history','interrupted animation cleanup','failed request recovery','reduced motion'],pageErrors:errors},null,2));
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
