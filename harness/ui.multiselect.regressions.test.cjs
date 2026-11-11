// Multi-picking through real Chromium input, deterministic services and no user files.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
if(!process.versions.electron){
  const {test}=require('node:test'),{execFileSync}=require('node:child_process');
  test('installed and catalog multiselect preserve scope, explicit choices and reviewed changes',()=>{
    try{process.stdout.write(execFileSync(process.execPath,[path.join(__dirname,'../app/node_modules/electron/cli.js'),__filename],{encoding:'utf8',timeout:90000}));}
    catch(error){if(error.stdout)process.stdout.write(error.stdout);throw error;}
  });
}else{
  const {app,BrowserWindow}=require('electron');
  app.disableHardwareAcceleration();
  app.setPath('userData',fs.mkdtempSync(path.join(require('node:os').tmpdir(),'launcher-multiselect-ui-')));
  const shell=fs.readFileSync(path.join(__dirname,'ui.js'),'utf8').replace(/\}\)\(\);\s*$/,'window.__launcherUI={screen,field,esc,closeScreen,profileForRow};})();');
  const productSource=fs.readFileSync(path.join(__dirname,'installation_ui.js'),'utf8');
  const withoutAck=process.env.MULTISELECT_TEST_WITHOUT_ACK==='1';
  if(withoutAck)assert.equal(productSource.split('if(onApplied)onApplied();').length,2,'The disposable callback variant must remove exactly one acknowledgment call.');
  const fixtureSource=withoutAck?productSource.replace('if(onApplied)onApplied();',''):productSource;
  const artwork='data:image/png;base64,'+fs.readFileSync(path.join(__dirname,'../minecraft-launcher-re/src/ui/assets/icons/javaIcons/Furnace.png')).toString('base64');
  const items=[
    {id:'alpha',kind:'mods',name:'Alpha Mod',filename:'alpha.jar',path:'mods/alpha.jar',enabled:true,version:'1.0',iconData:artwork},
    {id:'beta',kind:'mods',name:'Beta Mod',filename:'beta.jar',path:'mods/beta.jar',enabled:false,version:'1.0',iconData:artwork},
    {id:'gamma',kind:'mods',name:'Gamma Mod',filename:'gamma.jar',path:'mods/gamma.jar',enabled:true,version:'1.0',iconData:artwork}
  ];
  const hits=[{project_id:'first',title:'First Project',description:'First home mod'},{project_id:'second',title:'Second Project',description:'Second home mod'}];
  const versions=id=>[{id:id+'-beta',version_number:'2-beta',version_type:'beta'},{id:id+'-release',version_number:'1.5',version_type:'release'}];
  let win;const checks=[],errors=[];
  const run=code=>win.webContents.executeJavaScript(code);
  const tick=()=>new Promise(resolve=>setTimeout(resolve,25));
  async function ready(expression){for(let i=0;i<80;i++){if(await run(expression))return;await tick();}assert.fail('UI did not settle: '+expression);}
  async function fixture(extra={}){
    await win.loadURL('data:text/html;charset=utf-8,'+encodeURIComponent('<button id="opener">Edit</button><main><ul><li><img src="'+artwork+'"><div data-row-title>Native installation</div><div data-row-detail>1.20.1</div><button><span>Play</span></button><button id="MoreOptionsDropdown">More</button></li></ul></main>'));
    await run(`window.__entrySettled=true;window.__host={appData:{launcherSettings:{},configurationData:{profiles:{}}}};window.MCLauncher={openFileDialog:async()=>null};window.__requests=[];window.fetch=(url,opts={})=>url.startsWith('/__loaders/versions')?Promise.resolve({ok:true,json:async()=>({versions:['0.16.10']})}):new Promise((resolve,reject)=>__requests.push({url,opts,resolve,reject,done:false}));${shell}`);
    await run(fixtureSource);
    await run(`__installationWorkspace.open('one',document.querySelector('#opener'));void 0`);
    await response('/__instances/one',{ok:true,id:'one',profile:{name:'My home',lastVersionId:'1.20.1',gameDir:'C:/fixture/one',icon:'Grass'},context:{minecraft:'1.20.1',loader:'fabric',loaderVersion:'0.16.10'},ownership:{root:'C:/fixture/one',private:true},versions:[],running:false,...extra},true);
  }
  async function pending(fragment,exact=false){const expr=`__requests.findIndex(r=>!r.done&&${exact?'r.url===':'r.url.includes('}${JSON.stringify(fragment)}${exact?'':')'})`;await ready(expr+'>=0');return run(expr);}
  async function response(fragment,body,exact=false){const index=await pending(fragment,exact);await run(`__requests[${index}].done=true;__requests[${index}].resolve({ok:${!body.error},json:async()=>(${JSON.stringify(body)})});void 0`);await tick();return index;}
  async function payload(fragment){const index=await pending(fragment);return run(`JSON.parse(__requests[${index}].opts.body||'{}')`);}
  async function click(selector,modifiers=[]){
    const point=await run(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e)return null;e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return {x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)}})()`);
    assert.ok(point,'Missing control '+selector);
    win.webContents.sendInputEvent({type:'mouseDown',button:'left',clickCount:1,...point,modifiers});win.webContents.sendInputEvent({type:'mouseUp',button:'left',clickCount:1,...point,modifiers});await tick();
  }
  async function clickText(text){const selector=await run(`(()=>{const b=[...document.querySelectorAll('[data-iw-pane] button')].find(b=>b.textContent.trim()===${JSON.stringify(text)});if(!b)return null;b.setAttribute('data-fixture-target','');return '[data-fixture-target]';})()`);assert.ok(selector,'Missing '+text);await click(selector);await run(`document.querySelector('[data-fixture-target]')?.removeAttribute('data-fixture-target');void 0`);}
  async function fill(selector,value){await run(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});e.focus();e.value=${JSON.stringify(value)};e.dispatchEvent(new Event('input',{bubbles:true}));})()`);await tick();}
  async function inventory(available=items){await click('[data-category="mods"]');await response('/content',{ok:true,revision:'r1',items:available,problems:[]});}
  async function catalog(available=hits){await inventory();await click('[data-iw-browse]');await response('/catalog?',{ok:true,hits:available,total_hits:available.length});}
  async function pickProject(id){await click('[data-iw-select-project="'+id+'"]');await response('/versions?projectId='+id,{ok:true,versions:versions(id)});}
  async function chooseVersion(id){await click('#iw-contentVersion');const selector=await run(`(()=>{const b=[...document.querySelectorAll('#iw-contentVersion-options button')].find(b=>b.textContent.includes(${JSON.stringify(id)}));if(!b)return null;b.setAttribute('data-fixture-version','');return '[data-fixture-version]';})()`);assert.ok(selector,'Missing version '+id);await click(selector);}
  async function check(name,fn){if(process.env.MULTISELECT_TEST_FILTER&&!name.includes(process.env.MULTISELECT_TEST_FILTER))return;try{await fixture();await fn();checks.push({name,ok:true});}catch(error){checks.push({name,ok:false,error:String(error.stack||error)});}}
  app.whenReady().then(async()=>{try{
    win=new BrowserWindow({show:false,width:1024,height:640,webPreferences:{contextIsolation:true,sandbox:true,nodeIntegration:false,backgroundThrottling:false}});
    win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
    win.webContents.on('console-message',event=>{if(event.level==='error'||event.level===3)errors.push(event.message);});
    await check('named installed checkboxes support keyboard and leave General draft clean',async()=>{
      await inventory();assert.ok(await run(`!!document.querySelector('[data-iw-select-id="alpha"]')`),'Installed selection checkbox is missing');assert.match(await run(`document.querySelector('[data-iw-select-id="alpha"]').getAttribute('aria-label')||document.querySelector('[data-iw-select-id="alpha"]').labels?.[0]?.textContent||''`),/Alpha Mod/);
      await run(`document.querySelector('[data-iw-select-id="alpha"]').focus();void 0`);win.webContents.sendInputEvent({type:'keyDown',keyCode:'Space'});win.webContents.sendInputEvent({type:'keyUp',keyCode:'Space'});await tick();
      assert.equal(await run(`document.querySelector('[data-iw-select-id="alpha"]').checked`),true);
      assert.equal(await run(`document.querySelector('[data-iw-save]').disabled`),true);
      assert.equal(await run(`(()=>{const b=document.querySelector('[data-iw-review-selected]');if(!b)return false;const r=b.getBoundingClientRect();return !b.disabled&&!!b.closest('.__hx-foot')&&r.top>=0&&r.bottom<=innerHeight;})()`),true);
    });
    await check('Shift picking selects the visible ordered range',async()=>{
      await inventory();await click('[data-iw-select-id="alpha"]');await click('[data-iw-select-id="gamma"]',['shift']);
      assert.deepEqual(await run(`[...document.querySelectorAll('[data-iw-select-id]')].filter(e=>e.checked).map(e=>e.dataset.iwSelectId)`),['alpha','beta','gamma']);
    });
    await check('Select visible retains hidden picks and manager names every selected file',async()=>{
      await inventory();await click('[data-iw-select-id="alpha"]');await fill('#iw-installed-search','beta');await ready(`document.querySelectorAll('[data-iw-select-id]').length===1`);await click('[data-iw-select-visible]');
      assert.match(await run(`document.querySelector('[data-iw-pane]').textContent`),/outside|hidden|not shown/i);
      await click('[data-iw-review-selected]');const text=await run(`document.querySelector('[data-iw-pane]').textContent`);assert.match(text,/Alpha Mod/);assert.match(text,/Beta Mod/);assert.doesNotMatch(text,/Gamma Mod/);
    });
    await check('status filter and Details Back preserve only intended picks',async()=>{
      await inventory();await click('[data-iw-select-id="alpha"]');await click('[data-content-id="alpha"] button');await clickText('Back');assert.equal(await run(`document.querySelector('[data-iw-select-id="alpha"]').checked`),true);
      await click('#iw-installed-filter');await clickText('Disabled');assert.deepEqual(await run(`[...document.querySelectorAll('[data-iw-select-id]')].map(e=>e.dataset.iwSelectId)`),['beta']);
      await click('[data-iw-clear-selection]');assert.equal(await run(`!!document.querySelector('[data-iw-review-selected]')&&!document.querySelector('[data-iw-review-selected]').hidden&&!document.querySelector('[data-iw-review-selected]').disabled`),false);
    });
    await check('bulk preview receives exact IDs and Apply uses the reviewed dependency expansion once',async()=>{
      await inventory();await click('[data-iw-select-id="alpha"]');await click('[data-iw-select-id="gamma"]');await click('[data-iw-review-selected]');await click('[data-iw-bulk-action="disable"]');
      const body=await payload('/preview-change');assert.deepEqual([...body.ids].sort(),['alpha','gamma']);assert.equal(body.kind,'mods');assert.equal(body.action,'toggle');assert.equal(body.enabled,false);assert.equal(body.revision,'r1');
      const applyBody={ids:['alpha','gamma'],kind:'mods',enabled:false,revision:'r1',disableDependents:true,reviewContext:{root:'C:/fixture/one'}};
      await response('/preview-change',{ok:true,action:'toggle',kind:'mods',revision:'r1',selected:[items[0],items[2]],affected:items,additional:[items[1]],unchanged:[],requiresExpansion:true,conflicts:[],warnings:[],applyBody});
      const text=await run(`document.querySelector('[data-iw-pane]').textContent`);assert.match(text,/Beta Mod/);assert.match(text,/depend|additional|affected/i);
      await click('[data-iw-bulk-apply]');assert.deepEqual(await payload('/toggle'),applyBody);assert.equal(await run(`__requests.filter(r=>r.url.endsWith('/toggle')).length`),1);
    });
    await check('conflicting bulk previews cannot apply and stale failures retain the selected files',async()=>{
      await inventory();await click('[data-iw-select-id="alpha"]');await click('[data-iw-review-selected]');await click('[data-iw-bulk-action="remove"]');await response('/preview-change',{ok:true,action:'remove',kind:'mods',revision:'r1',selected:[items[0]],affected:[items[0]],additional:[],unchanged:[],conflicts:[{message:'Helper Mod is still required',code:'missing_dependency',dependency:'helper'}],warnings:[],applyBody:{ids:['alpha'],kind:'mods',revision:'r1'}});
      assert.equal(await run(`document.querySelector('[data-iw-bulk-apply]').disabled`),true);assert.match(await run(`document.querySelector('[data-iw-pane]').textContent`),/Helper Mod|helper/);
      await clickText('Back');if(!await run(`!!document.querySelector('[data-iw-bulk-action="remove"]')`))await click('[data-iw-review-selected]');await click('[data-iw-bulk-action="remove"]');await response('/preview-change',{error:'Content changed. Refresh before changing these files.',code:'stale_revision'});assert.match(await run(`document.querySelector('[data-iw-pane]').textContent`),/Refresh|changed/);assert.equal(await run(`__requests.filter(r=>r.url.endsWith('/remove')).length`),0);
    });
    await check('catalog picks prefer releases and resolve every project in one shared plan',async()=>{
      await catalog();await pickProject('first');await pickProject('second');await click('[data-iw-review-selected]');
      const text=await run(`document.querySelector('[data-iw-pane]').textContent`);assert.match(text,/First Project/);assert.match(text,/Second Project/);assert.match(text,/1\.5/);
      await run(`(()=>{const label=[...document.querySelectorAll('[data-iw-pane] label')].find(e=>e.textContent.includes('Include optional dependencies'));label?.querySelector('input').setAttribute('data-fixture-optional','');})()`);await click('[data-fixture-optional]');await click('[data-iw-resolve-selection]');const body=await payload('/plan');assert.deepEqual(body.selections.map(s=>s.versionId).sort(),['first-release','second-release']);assert.equal(body.kind,'mods');assert.equal(body.revision,'r1');assert.equal(body.includeOptional,true);assert.equal(await run(`__requests.filter(r=>r.url.endsWith('/plan')).length`),1);
    });
    await check('catalog selections survive another search and explicit version edits replace a project pin',async()=>{
      await catalog();await pickProject('first');await fill('#__hx-modpack-search, #iw-search','second');await response('/catalog?',{ok:true,hits:[hits[1]],total_hits:1});await pickProject('second');await click('[data-iw-review-selected]');await click('[data-iw-edit-pick="first"]');await response('/versions?projectId=first',{ok:true,versions:versions('first')});await chooseVersion('2-beta');await click('[data-iw-add-selection]');
      if(await run(`!!document.querySelector('[data-iw-review-selected]')`))await click('[data-iw-review-selected]');await click('[data-iw-resolve-selection]');const body=await payload('/plan');assert.equal(body.selections.length,2);assert.deepEqual(body.selections.map(s=>s.versionId).sort(),['first-beta','second-release']);
    });
    await check('removing a pending catalog pick cannot be undone by its late version response',async()=>{
      await catalog();await click('[data-iw-select-project="first"]');const versionRequest=await pending('/versions?projectId=first');await click('[data-iw-select-project="first"]');await run(`__requests[${versionRequest}].done=true;__requests[${versionRequest}].resolve({ok:true,json:async()=>({versions:${JSON.stringify(versions('first'))}})});void 0`);await tick();
      assert.equal(await run(`document.querySelector('[data-iw-select-project="first"]').checked`),false);assert.equal(await run(`!!document.querySelector('[data-iw-review-selected]')&&!document.querySelector('[data-iw-review-selected]').hidden&&!document.querySelector('[data-iw-review-selected]').disabled`),false);
    });
    await check('missing compatible versions stay visible and retryable instead of silently dropping picks',async()=>{
      await catalog();await click('[data-iw-select-project="first"]');await response('/versions?projectId=first',{ok:true,versions:[]});await click('[data-iw-review-selected]');assert.match(await run(`document.querySelector('[data-iw-pane]').textContent`),/First Project/);assert.equal(await run(`document.querySelector('[data-iw-resolve-selection]').disabled`),true);assert.ok(await run(`!!document.querySelector('[data-iw-retry-pick="first"]')`));
    });
    await check('running installations cannot submit bulk changes or catalog resolution',async()=>{
      await fixture({running:true});await inventory();await click('[data-iw-select-id="alpha"]');const review=await run(`!!document.querySelector('[data-iw-review-selected]')`);if(review&&await run(`!document.querySelector('[data-iw-review-selected]').hidden&&!document.querySelector('[data-iw-review-selected]').disabled`)){await click('[data-iw-review-selected]');assert.equal(await run(`[...document.querySelectorAll('[data-iw-bulk-action]')].every(b=>b.disabled)`),true);}assert.equal(await run(`__requests.filter(r=>r.url.endsWith('/preview-change')||r.url.endsWith('/plan')||r.url.endsWith('/toggle')).length`),0);
    });
    await check('refresh removes missing selected IDs and preserves surviving selections',async()=>{
      await inventory();await click('[data-iw-select-id="alpha"]');await click('[data-iw-select-id="beta"]');await clickText('Refresh');await response('/content',{ok:true,revision:'r2',items:[items[1],items[2]],problems:[]});await click('[data-iw-review-selected]');const text=await run(`document.querySelector('[data-iw-pane]').textContent`);assert.match(text,/Beta Mod/);assert.doesNotMatch(text,/Alpha Mod/);
    });
    await check('saved installation context invalidates picks and rejects their late version responses',async()=>{
      await catalog();await click('[data-iw-select-project="first"]');const old=await pending('/versions?projectId=first');await click('[data-category="general"]');await fill('#iw-directory','C:/fixture/new');await click('[data-category="mods"]');await clickText('Save changes');await response('/general',{ok:true,id:'one',profile:{name:'My home',lastVersionId:'1.20.1',gameDir:'C:/fixture/new',icon:'Grass'},context:{minecraft:'1.20.1',loader:'fabric',loaderVersion:'0.16.10'},ownership:{root:'C:/fixture/new',private:true},versions:[],running:false});await response('/content',{ok:true,revision:'r2',items:[],problems:[],ownership:{root:'C:/fixture/new',private:true}});await run(`__requests[${old}].done=true;__requests[${old}].resolve({ok:true,json:async()=>({versions:${JSON.stringify(versions('first'))}})});void 0`);await tick();await click('[data-iw-browse]');await response('/catalog?',{ok:true,hits:[hits[0]],total_hits:1});assert.equal(await run(`document.querySelector('[data-iw-select-project="first"]').checked`),false);assert.equal(await run(`!!document.querySelector('[data-iw-review-selected]')&&!document.querySelector('[data-iw-review-selected]').hidden&&!document.querySelector('[data-iw-review-selected]').disabled`),false);
    });
    await check('accepted catalog Apply clears submitted mod picks after acknowledgment and preserves another category',async()=>{
      await inventory();await click('[data-category="resourcepacks"]');await response('/content',{ok:true,revision:'r1',items,problems:[]});await click('[data-iw-browse]');await response('/catalog?',{ok:true,hits:[{project_id:'pack',title:'Home Pack'}],total_hits:1});await pickProject('pack');
      await click('[data-category="mods"]');await response('/content',{ok:true,revision:'r1',items,problems:[]});await click('[data-iw-browse]');await response('/catalog?',{ok:true,hits,total_hits:2});await pickProject('first');await pickProject('second');await click('[data-iw-review-selected]');await click('[data-iw-resolve-selection]');await response('/plan',{ok:true,planId:'selected-plan',files:[{projectId:'first',versionId:'first-release',name:'First Project',filename:'first.jar',size:100},{projectId:'second',versionId:'second-release',name:'Second Project',filename:'second.jar',size:100}],reuse:[],enable:[],remove:[],conflicts:[],warnings:[]});
      await click('[data-iw-apply]');await pending('/apply');await clickText('Back');assert.equal(await run(`document.querySelectorAll('[data-iw-remove-pick]').length`),2,'Picks must stay until the host acknowledges the install');
      await response('/apply',{ok:true,jobId:'accepted-job'});await response('/job?',{ok:true,job:{id:'accepted-job',kind:'mods',status:'succeeded',phase:'complete',result:{summary:{installed:[],reused:[],enabled:[],removed:[]}}}});assert.equal(await run(`document.querySelectorAll('[data-iw-remove-pick]').length`),0,'The acknowledged selection must repaint immediately in the basket');await clickText('Back to Browse');await response('/catalog?',{ok:true,hits,total_hits:2});assert.deepEqual(await run(`[...document.querySelectorAll('[data-iw-select-project]')].filter(e=>e.checked).map(e=>e.dataset.iwSelectProject)`),[],'Submitted mod picks remain selected after an accepted install');
      await click('[data-category="resourcepacks"]');await response('/content',{ok:true,revision:'r2',items,problems:[]});await click('[data-iw-browse]');await response('/catalog?',{ok:true,hits:[{project_id:'pack',title:'Home Pack'}],total_hits:1});assert.equal(await run(`document.querySelector('[data-iw-select-project="pack"]').checked`),true,'An install must not clear picks from another category');
    });
    await check('pending shared plan locks basket edit remove clear and optional controls',async()=>{
      await catalog();await pickProject('first');await pickProject('second');await click('[data-iw-review-selected]');await click('[data-iw-resolve-selection]');const frozen=await payload('/plan');
      assert.deepEqual(await run(`[...document.querySelectorAll('[data-iw-edit-pick],[data-iw-remove-pick],[data-iw-clear-selection],[data-iw-basket-optional]')].map(e=>e.disabled)`),[true,true,true,true,true,true]);
      await click('[data-iw-remove-pick="first"]');await click('[data-iw-edit-pick="second"]');await click('[data-iw-basket-optional]');await click('[data-iw-clear-selection]');assert.equal(await run(`document.querySelectorAll('[data-iw-remove-pick]').length`),2);assert.equal(await run(`document.querySelector('[data-iw-basket-optional]').checked`),false);assert.deepEqual(await payload('/plan'),frozen);assert.equal(await run(`__requests.filter(r=>r.url.includes('/versions?')).length`),2);assert.equal(await run(`__requests.filter(r=>r.url.endsWith('/plan')).length`),1);
      await response('/plan',{ok:true,planId:'locked-plan',files:[],reuse:[],enable:[],remove:[],conflicts:[],warnings:[]});
    });
    await check('mixed-state bulk preview shows unchanged rows and disables Apply when no files change',async()=>{
      await inventory();await click('[data-iw-select-id="alpha"]');await click('[data-iw-select-id="beta"]');await click('[data-iw-review-selected]');await click('[data-iw-bulk-action="enable"]');const applyBody={ids:['alpha','beta'],kind:'mods',enabled:true,revision:'r1'};await response('/preview-change',{ok:true,action:'toggle',kind:'mods',revision:'r1',selected:[items[0],items[1]],affected:[items[0],items[1]],changed:[items[1]],additional:[],unchanged:[items[0]],conflicts:[],warnings:[],applyBody});assert.match(await run(`document.querySelector('[data-iw-pane]').textContent`),/Already in the requested state/);assert.equal(await run(`document.querySelector('[data-iw-bulk-apply]').disabled`),false);
      await clickText('Back');await click('[data-iw-bulk-action="enable"]');await response('/preview-change',{ok:true,action:'toggle',kind:'mods',revision:'r1',selected:[items[0],{...items[1],enabled:true}],affected:[items[0],{...items[1],enabled:true}],changed:[],additional:[],unchanged:[items[0],{...items[1],enabled:true}],conflicts:[],warnings:[],applyBody});assert.equal(await run(`document.querySelector('[data-iw-bulk-apply]').disabled`),true);assert.match(await run(`document.querySelector('[data-iw-bulk-apply]').textContent`),/No changes needed/);await click('[data-iw-bulk-apply]');assert.equal(await run(`__requests.filter(r=>r.url.endsWith('/toggle')).length`),0);
    });
    await check('pending version completion and Unselect preserve basket focus',async()=>{
      await catalog();await pickProject('first');await click('[data-iw-select-project="second"]');await pending('/versions?projectId=second');await click('[data-iw-review-selected]');await run(`document.querySelector('[data-iw-edit-pick="first"]').focus();void 0`);await response('/versions?projectId=second',{ok:true,versions:versions('second')});assert.equal(await run(`document.activeElement?.dataset.iwEditPick`),'first','A completed pending row must not detach the focused control in another row');
      await click('[data-iw-remove-pick="first"]');assert.equal(await run(`!!document.activeElement?.closest('[data-iw-basket-list]')&&!document.activeElement.disabled`),true,'Unselect must move focus to a remaining basket control');
    });
    assert.deepEqual(errors,[]);
  }catch(error){checks.push({name:'fixture fatal',ok:false,error:String(error.stack||error)});}finally{
    const result={checks,errors,sourceVariant:withoutAck?'without-onApplied-callback':'product',sourceSha256:require('node:crypto').createHash('sha256').update(fixtureSource).digest('hex')};const output=process.env.MULTISELECT_TEST_OUTPUT;if(output){fs.mkdirSync(output,{recursive:true});fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(result,null,2));}
    process.stdout.write(JSON.stringify(result,null,2)+'\n');if(win)win.destroy();app.exit(checks.some(c=>!c.ok)||errors.length?1:0);
  }});
}
