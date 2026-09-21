/* Offline integration check: serve the repo on localhost:8765 first.
 * All API data is synthetic. No real account or external service is accessed. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const base = process.env.SOLON_BASE_URL || 'http://127.0.0.1:8765';
const output = process.env.SOLON_ARTIFACT_DIR || '/tmp/solon-profile-check';
fs.mkdirSync(output, {recursive:true});
const groups = [
  ['AC ENG','Academic English',[['20B','Academic Writing'],['20C','Essentials of Academic Writing'],['22A','Academic English Reading and Vocabulary']]],
  ['ART','Art',[['9B','Visual Culture: A Culture Divided'],['12A','Art, Design, and Electronic Culture']]],
  ['BME','Biomedical Engineering',[['3','Engineering Innovations in Treating Diabetes']]],
  ['DRAMA','Drama',[['15','Performance Now']]],
  ['EECS','Electrical Engineering & Computer Science',[['12','Introduction to Programming'],['31','Introduction to Digital Logic Design']]],
  ['I&C SCI','Information & Computer Sciences',[['6B','Boolean Logic and Discrete Structures'],['6D','Discrete Mathematics for Computer Science'],['31','Introduction to Programming'],['32','Programming with Software Libraries'],['33','Intermediate Programming'],['45C','Programming in C/C++ as a Second Language'],['46','Data Structure Implementation and Analysis'],['51','Introductory Computer Organization'],['53','Principles in System Design'],['90','New Students Seminar']]],
  ['MATH','Mathematics',[['2A','Single-Variable Calculus I'],['2B','Single-Variable Calculus II'],['3A','Introduction to Linear Algebra']]],
  ['PHYSICS','Physics',[['7C','Classical Physics'],['7D','Classical Physics'],['7LC','Classical Physics Laboratory']]],
  ['STATS','Statistics',[['7','Basic Statistics'],['67','Introduction to Probability and Statistics for Computer Science']]],
  ['UNI STU','University Studies',[['87','Navigating Your UCI Journey: Foundations for Success']]],
  ['UPPP','Urban Planning & Public Policy',[['5','Introduction to Planning and Policy']]],
  ['WRITING','Writing',[['45','Intensive Writing'],['60','Argument and Research']]],
];
const completed = groups.flatMap(([dept,,courses]) => courses.map(([number,title]) => ({course_id:dept+' '+number,title})));
const originalProfile = {major:'Computer Science and Engineering',year:'Junior',graduating_class:2027,school_slug:'ics',program_id:'cse',catalog_year:'2024-2025',completed_courses:completed.map(c=>c.course_id),selected_courses:[],waitlisted_courses:[]};
(async()=>{
  const browser = await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000},deviceScaleFactor:1});
    const errors=[], writes=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.route('https://**',route=>route.abort());
    let profile = structuredClone(originalProfile), courses = structuredClone(completed);
    let academicFails=false, memoryFails=false, gpaFails=false, gpaDelay=0, gpaReads=0, hasImport=true;
    const user={id:'profile_fixture',email:'profile-fixture@uci.edu'};
    await page.route('**/api/**',async route=>{
      const request=route.request(), url=new URL(request.url());
      const send=data=>route.fulfill({json:data});
      const failure=()=>route.fulfill({status:503,json:{detail:'Fixture unavailable'}});
      if(request.method()!=='GET') writes.push({path:url.pathname,body:request.postDataJSON()});
      if(url.pathname==='/api/auth/me') return send({user});
      if(url.pathname==='/api/memory/me') return memoryFails ? failure() : send({user_id:user.id,profile,preferences:[],facts:[]});
      if(url.pathname==='/api/memory/me/profile') {Object.assign(profile,request.postDataJSON());return send({ok:true,profile});}
      if(url.pathname==='/api/term-state') return send({default_term:'Fall 2026',automatic_term:'Fall 2026'});
      if(url.pathname==='/api/sessions/me') return send({sessions:[{session_id:'sess_profile',title:'Saved conversation',created_at:new Date().toISOString()}]});
      if(url.pathname==='/api/sessions/me/sess_profile') return send({session_id:'sess_profile',title:'Saved conversation',turns:[{role:'user',content:'Recommend ICS courses'},{role:'assistant',content:'Your saved answer.'}]});
      if(url.pathname==='/api/schedule') return send({ok:true,pending_schedule:[],events:[]});
      if(url.pathname==='/api/schedule/add') {
        await new Promise(resolve=>setTimeout(resolve,400));
        return send({ok:true,pending_schedule:[{course_id:'ICS33',section:'A',term:'2026 Fall'}],events:[]});
      }
      if(url.pathname==='/api/academic/profile') {
        if(academicFails) return failure();
        const data={ok:true,completed_courses:courses,units_completed:hasImport?118:null,gpa_available:hasImport,last_import:hasImport?{imported_at:'2026-09-18T15:23:00Z'}:null};
        if(url.searchParams.has('include_gpa')) {
          gpaReads++;
          if(gpaDelay) await new Promise(resolve=>setTimeout(resolve,gpaDelay));
          if(gpaFails) return failure();
          data.official_uc_gpa=3.67;
        }
        return send(data);
      }
      if(url.pathname==='/api/onboarding/departments') return send({departments:groups.map(([deptCode,deptName])=>({deptCode,deptName}))});
      if(url.pathname==='/api/onboarding/schools') return send({schools:[{slug:'ics',name:'Donald Bren School of Information and Computer Sciences'}]});
      if(url.pathname==='/api/onboarding/years') return send({years:['Freshman','Sophomore','Junior','Senior']});
      if(url.pathname==='/api/onboarding/majors') return send({majors:[{id:'cse',name:'Major in Computer Science and Engineering'}]});
      if(url.pathname==='/api/onboarding/courses/all') return send({courses:groups.flatMap(([department,,items])=>items.map(([number,title])=>({id:`${department} ${number}`,department,courseNumeric:parseInt(number),minUnits:4,title})))});
      if(url.pathname==='/api/academic/transcript/import') {
        courses=[...courses,{course_id:'COMPSCI 161',title:'Design and Analysis of Algorithms'}];
        profile.completed_courses=courses.map(course=>course.course_id);
        return send({ok:true,read:1,accepted:1,added:1,updated:0,skipped:0,completed_course_ids:profile.completed_courses});
      }
      return send({});
    });
    const loaded=()=>page.locator('#profileMajor').waitFor();
    const open=async()=>{await page.locator('#profileNav').click();await loaded();};
    const noOverflow=async()=>assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.goto(`${base}/static/index.html`);
    await page.getByRole('button',{name:'Saved conversation',exact:true}).waitFor();
    assert.deepEqual(await page.locator('.sidebar-nav .nav-item').allTextContents(),['Ask','Student Profile','Schedule','New thread']);
    await page.getByRole('button',{name:'Saved conversation',exact:true}).click();
    await page.getByText('Your saved answer.',{exact:true}).waitFor();
    await page.locator('#userInput').fill('My unsent question');
    // Start a real add request, then navigate before the response arrives.
    await page.evaluate(()=>{window.pendingProfileTestAdd=addCourse('ICS33','A',null,'2026 Fall');});
    await page.locator('#toggleScheduleBtn').click();
    await open();
    await page.evaluate(()=>window.pendingProfileTestAdd);
    assert.equal(await page.evaluate(()=>pendingScheduleEntries[0]?.course_id),'ICS33');
    assert.equal(await page.locator('#chatPanel').isVisible(),false);
    assert.equal(await page.evaluate(()=>scheduleOpen),false);
    assert.equal(await page.locator('#profileNav').getAttribute('aria-current'),'page');
    assert.equal(await page.locator('#profileCourseCount').textContent(),'31');
    assert.equal(await page.locator('#profileUnits').textContent(),'118');
    await page.getByText('AC ENG · Academic English',{exact:true}).waitFor();
    assert.equal(await page.locator('.profile-subject').count(),12);
    assert.equal(gpaReads,0);
    assert(!(await page.locator('#profileBody').innerHTML()).includes('3.67'));
    await noOverflow();
    const height=await page.locator('#profileBody').evaluate(el=>el.offsetHeight);
    await page.setViewportSize({width:1440,height:Math.ceil(height+60)});
    await page.screenshot({path:path.join(output,'01-profile-desktop.png'),animations:'disabled'});
    await page.setViewportSize({width:1440,height:1000});
    await page.getByRole('button',{name:'Show GPA',exact:true}).click();
    await page.getByText('3.67',{exact:true}).waitFor();
    assert.equal(gpaReads,1);
    await page.getByRole('button',{name:'Hide GPA',exact:true}).click();
    assert(!(await page.locator('#profileBody').innerHTML()).includes('3.67'));
    gpaFails=true;
    await page.getByRole('button',{name:'Show GPA',exact:true}).click();
    await page.getByText('Couldn’t load GPA · try again',{exact:true}).waitFor();
    gpaFails=false;gpaDelay=400;
    await page.getByRole('button',{name:'Show GPA',exact:true}).click();
    await page.locator('#askNav').click();
    await page.waitForTimeout(500);
    assert(!(await page.locator('#profileBody').innerHTML()).includes('3.67'));
    assert.equal(await page.locator('#userInput').inputValue(),'My unsent question');
    assert(await page.getByText('Your saved answer.',{exact:true}).isVisible());
    gpaDelay=0;
    await open();
    await page.locator('#profileCourseSearch').fill('ics32');
    assert.equal(await page.locator('.profile-course-row').count(),1);
    assert.equal(await page.locator('.profile-course-code').textContent(),'ICS 32');
    await page.locator('#profileCourseSearch').fill('nonexistent');
    assert(await page.getByText('No courses match your search.').isVisible());
    await page.locator('#profileCourseSearch').fill('');
    await page.locator('#profileSortAZ').click();
    assert.equal(await page.locator('.profile-subject').count(),0);
    assert.equal(await page.locator('.profile-course-row').count(),31);
    await page.locator('#profileSortSubject').click();
    // Existing editor opens prefilled and refreshes the page after saving.
    await page.locator('#profileEditButton').click();
    await page.locator('#wizardOverlay.open').waitFor();
    assert.equal(await page.evaluate(()=>wizardState.year),'Junior');
    assert.equal(await page.evaluate(()=>wizardState.completed_courses.size),31);
    await page.locator('#wizardNextBtn').click();
    await page.locator('#wizardYearGrid .wizard-card').filter({hasText:'Senior'}).click();
    await page.locator('#wizardNextBtn').click();
    await page.locator('#wizardNextBtn').click();
    await page.locator('#wizardCourseGrid .wizard-card').first().waitFor({state:'attached'});
    // Regress the clipped final step: real wheel input must scroll the page,
    // and the last course must remain fully visible and selectable above Save.
    for (const [width,height,label] of [[1440,780,'desktop'],[1280,600,'short'],[390,844,'mobile']]) {
      await page.setViewportSize({width,height});
      await page.emulateMedia({reducedMotion:'reduce'});
      await page.locator('.wizard-main').evaluate(el=>el.scrollTop=0);
      await page.mouse.move(width/2,150);
      await page.mouse.wheel(0,800);
      await page.waitForFunction(()=>document.querySelector('.wizard-main').scrollTop>100,{},{timeout:2500});
      const lastCourse=page.locator('#wizardCourseGrid [data-cid="WRITING 60"]');
      await lastCourse.scrollIntoViewIfNeeded();
      const bounds=await page.evaluate(()=>{
        const last=document.querySelector('#wizardCourseGrid [data-cid="WRITING 60"]').getBoundingClientRect();
        const main=document.querySelector('.wizard-main');
        const footer=document.querySelector('.wizard-footer').getBoundingClientRect();
        return {top:last.top,bottom:last.bottom,footer:footer.top,footerRight:footer.right,width:innerWidth,client:main.clientWidth,scroll:main.scrollWidth};
      });
      assert(bounds.top>=0 && bounds.bottom<=bounds.footer,JSON.stringify(bounds));
      assert(bounds.footerRight<=bounds.width && bounds.scroll<=bounds.client,JSON.stringify(bounds));
      await lastCourse.click();
      assert.equal(await page.evaluate(()=>wizardState.completed_courses.has('WRITING 60')),false);
      await lastCourse.click();
      assert.equal(await page.evaluate(()=>wizardState.completed_courses.has('WRITING 60')),true);
      await page.screenshot({path:path.join(output,`04-edit-scroll-${label}.png`),animations:'disabled'});
      await page.locator('#wizardLetterNav [data-letter-target="A"]').click();
      await page.waitForFunction(()=>{
        const header=document.querySelector('[data-dept-anchor="AC ENG"]').getBoundingClientRect();
        return header.top>=0 && header.bottom<innerHeight;
      });
      await page.locator('#wizardCourseSearch').fill('WRITING60');
      assert.equal(await page.locator('#wizardCourseGrid .wizard-card:not(.is-hidden)').count(),1);
      await lastCourse.click();await lastCourse.click();
      await page.locator('#wizardCourseSearch').fill('');
    }
    await page.setViewportSize({width:1440,height:1000});
    await page.locator('#wizardNextBtn').click();
    await page.locator('#wizardOverlay.open').waitFor({state:'hidden'});
    await loaded();
    assert((await page.locator('.profile-identity-meta').textContent()).includes('Senior'));
    assert(writes.some(write=>write.path==='/api/memory/me/profile' && write.body.year==='Senior'));
    // The real file-input/import controller is used; only PDF extraction is stubbed.
    await page.evaluate(()=>{extractTranscriptPayload=async()=>({payload:{parser_version:'fixture',courses:[]},localIssues:[]});});
    const chooserPromise=page.waitForEvent('filechooser');
    await page.locator('.profile-import-button').click();
    await (await chooserPromise).setFiles({name:'fixture.pdf',mimeType:'application/pdf',buffer:Buffer.from('%PDF-fixture')});
    await page.locator('#profileTranscriptStatus').getByText('Read 1 · Accepted 1 · 0 need attention',{exact:true}).waitFor();
    assert.equal(await page.locator('#profileCourseCount').textContent(),'32');
    assert.equal(await page.locator('.profile-import-button').isDisabled(),false);
    assert(writes.some(write=>write.path==='/api/academic/transcript/import'));
    await page.locator('.profile-delete-button').click();
    await page.locator('#deleteAccountModal.open').waitFor();
    await page.locator('#deleteAccountModal').getByRole('button',{name:'Cancel',exact:true}).click();
    assert(await page.locator('#studentProfilePanel').isVisible());
    await page.locator('.profile-plan-link').click();
    assert.equal(await page.locator('#userInput').inputValue(),'My unsent question');
    await page.locator('#userInput').fill('');
    await open();await page.locator('.profile-plan-link').click();
    assert.equal(await page.locator('#userInput').inputValue(),'Help me plan Fall 2026');
    assert.equal(await page.locator('#composerTermLabel').textContent(),'Fall 2026');
    await open();await page.getByRole('button',{name:'Saved conversation',exact:true}).click();
    assert(await page.locator('#chatPanel').isVisible());
    await open();await page.locator('#scheduleNav').click();
    assert.equal(await page.evaluate(()=>scheduleOpen),true);
    await open();await page.getByRole('button',{name:'New thread',exact:true}).click();
    assert(await page.locator('#chatPanel').isVisible());
    // Empty, partial failure, and hostile text never fabricate academic values.
    profile={major:'<img src=x onerror=alert(1)>',year:'Junior',completed_courses:['ICS99']};
    courses=[];hasImport=false;
    await open();
    assert.equal(await page.locator('#profileMajor img').count(),0);
    assert.equal(await page.locator('#profileCourseCount').textContent(),'0');
    assert.equal(await page.locator('#profileUnits').textContent(),'—');
    assert.equal(await page.locator('.profile-graduation').textContent(),'—');
    assert.equal(await page.locator('#profileGpaToggle').isVisible(),false);
    academicFails=true;await open();
    await page.locator('.profile-data-warning').waitFor();
    assert.equal(await page.locator('#profileCourseCount').textContent(),'1');
    memoryFails=true;await page.locator('#profileNav').click();
    await page.getByText('Your profile couldn’t be loaded. Please try again.').waitFor();
    assert.equal(await page.locator('#profileEditButton').isDisabled(),true);
    memoryFails=false;academicFails=false;hasImport=true;profile=structuredClone(originalProfile);courses=structuredClone(completed);
    await page.getByRole('button',{name:'Try again',exact:true}).click();await loaded();
    await page.setViewportSize({width:390,height:844});
    await page.locator('#studentProfileScroll').evaluate(el=>el.scrollTop=0);
    await noOverflow();
    await page.screenshot({path:path.join(output,'02-profile-mobile.png'),animations:'disabled'});
    await page.locator('#profileCourseSearch').fill('ICS32');
    assert.equal(await page.locator('.profile-course-row').count(),1);
    await page.locator('#profileCourseSearch').fill('');
    await page.locator('#studentProfilePanel .mobile-sidebar-toggle').click();
    assert.equal(await page.locator('#studentProfilePanel .mobile-sidebar-toggle').getAttribute('aria-expanded'),'true');
    await page.locator('#askNav').click();
    await page.locator('#chatPanel .mobile-sidebar-toggle').click();
    await open();
    assert.equal(await page.locator('#studentProfilePanel .mobile-sidebar-toggle').getAttribute('aria-expanded'),'false');
    await page.setViewportSize({width:320,height:740});await noOverflow();
    await page.screenshot({path:path.join(output,'03-profile-small.png'),animations:'disabled'});
    assert(writes.every(write=>['/api/memory/me/profile','/api/academic/transcript/import','/api/schedule/add'].includes(write.path)),JSON.stringify(writes));
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,courses:completed.length,screenshots:output,checks:['navigation and unsent draft retention','GPA opt-in, hide, error and stale-response privacy','course search and sorting','prefilled editor save refresh','coursework wheel scrolling on desktop, short and mobile viewports','last-course selection, A–Z jump and search','transcript import refresh','delete dialog cancel','planning prompt and read-only term','empty and failed APIs','escaped profile text','mobile navigation and overflow'],pageErrors:errors},null,2));
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
