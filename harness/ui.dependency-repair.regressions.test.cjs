// Native launcher controls, controlled HTTP boundaries and disposable host state.
// These checks catch broadened selection payloads, duplicate plans, lost drafts,
// stale rendering, false healthy diagnostics and unreachable review actions.
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict');
if(!process.versions.electron){
  const {test}=require('node:test'),{execFileSync}=require('node:child_process');
  test('native dependency repair keeps enabled scopes, recovery and reviewed transactions safe',()=>{
    try{process.stdout.write(execFileSync(process.execPath,[path.join(__dirname,'../app/node_modules/electron/cli.js'),__filename],{encoding:'utf8',timeout:120000}));}
    catch(error){if(error.stdout)process.stdout.write(error.stdout);throw error;}
  });
}else{
  const {app,BrowserWindow}=require('electron'),http=require('node:http'),net=require('node:net'),{spawn}=require('node:child_process');
  app.disableHardwareAcceleration();app.commandLine.appendSwitch('disable-renderer-backgrounding');app.commandLine.appendSwitch('disable-background-timer-throttling');
  const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'launcher-dependency-repair-')),root=path.dirname(__dirname),output=process.env.DEPENDENCY_REPAIR_TEST_OUTPUT||temporary;
  app.setPath('userData',path.join(temporary,'electron'));fs.mkdirSync(output,{recursive:true});
  const report={checks:[],captures:[],blockedEffects:[],consoleErrors:[],output};
  const artwork='data:image/png;base64,'+fs.readFileSync(path.join(root,'minecraft-launcher-re/src/ui/assets/icons/javaIcons/Furnace.png')).toString('base64');
  const items=[
    {id:'alpha',kind:'mods',name:'Alpha Mod',filename:'alpha.jar',path:'mods/alpha.jar',enabled:true,version:'1.0',projectId:'alpha-project',versionId:'alpha-v1',userRequested:true,iconData:artwork},
    {id:'beta',kind:'mods',name:'Beta Mod',filename:'beta.jar',path:'mods/beta.jar.disabled',enabled:false,version:'1.0',iconData:artwork},
    {id:'gamma',kind:'mods',name:'Gamma Mod',filename:'gamma.jar',path:'mods/gamma.jar',enabled:true,version:'1.0',projectId:'gamma-project',versionId:'gamma-v1',iconData:artwork},
    {id:'unknown',kind:'mods',name:'Unrecognized local archive',filename:'a very long unrecognized local archive which must remain visible.jar',path:'mods/a very long unrecognized local archive which must remain visible.jar',enabled:true,version:'Unknown',iconData:artwork}
  ];
  const plan=(extra={})=>({ok:true,planId:'repair-plan',kind:'mods',revision:'content-r1',files:[{id:'helper-v1',projectId:'helper',versionId:'helper-v1',name:'Helper API',filename:'helper.jar',size:1024,userRequested:false,requiredBy:['alpha-v1'],iconData:artwork}],reuse:['alpha','gamma'],enable:[],remove:[],reuseMetadata:{},conflicts:[],warnings:[],dependencyRepair:{checked:3,unidentified:[items[3]]},...extra});
  let win,host,origin,deadline;
  const run=code=>win.webContents.executeJavaScript(code),tick=()=>new Promise(resolve=>setTimeout(resolve,25));
  async function ready(expression){for(let i=0;i<120;i++){if(await run(expression))return;await new Promise(resolve=>setTimeout(resolve,50));}assert.fail('UI did not settle: '+expression);}
  function hostRequest(route,body){return new Promise((resolve,reject)=>{const req=http.request(origin+route,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:{}},res=>{let value='';res.on('data',c=>value+=c);res.on('end',()=>{try{if(res.statusCode>=400)reject(new Error(value));else resolve(JSON.parse(value));}catch(e){reject(e);}});});req.on('error',reject);req.end(body?JSON.stringify(body):undefined);});}
  async function pending(fragment,exact=false){const expr=`__requests.findIndex(r=>!r.done&&${exact?'r.url===':'r.url.includes('}${JSON.stringify(fragment)}${exact?'':')'})`;await ready(expr+'>=0');return run(expr);}
  async function response(fragment,body,exact=false){const index=await pending(fragment,exact);await run(`__requests[${index}].done=true;__requests[${index}].resolve({ok:${!body.error},json:async()=>(${JSON.stringify(body)})});void 0`);await tick();return index;}
  async function payload(fragment){return run(`JSON.parse(__requests[${await pending(fragment)}].opts.body||'{}')`);}
  async function click(selector,times=1){
    const point=await run(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e)return null;e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return{x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)}})()`);
    assert.ok(point,'Required native control is missing: '+selector);
    for(let i=0;i<times;i++){win.webContents.sendInputEvent({type:'mouseDown',button:'left',clickCount:1,...point});win.webContents.sendInputEvent({type:'mouseUp',button:'left',clickCount:1,...point});}await tick();
  }
  async function clickText(text){const selector=await run(`(()=>{const e=[...document.querySelectorAll('[data-iw-pane] button')].find(e=>e.textContent.trim()===${JSON.stringify(text)});if(!e)return null;e.dataset.fixtureTarget='';return '[data-fixture-target]';})()`);assert.ok(selector,'Missing '+text);await click(selector);await run(`document.querySelector('[data-fixture-target]')?.removeAttribute('data-fixture-target');void 0`);}
  async function fill(selector,value){await run(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});e.focus();e.value=${JSON.stringify(value)};e.dispatchEvent(new Event('input',{bubbles:true}));})()`);await tick();}
  async function fixture(extra={},magnified=false,width=1280,height=720){
    win.setContentSize(width,height);await hostRequest('/__launcher_settings',{locale:'en-US',useMagnifiedMode:magnified});
    await win.loadURL(origin+'/games/java/configurations');await ready(`window.__entrySettled&&!!window.__installationWorkspace&&!!document.querySelector('[data-configuration-id="one"]')`);
    await run(`window.__requests=[];const fixtureFetch=window.fetch.bind(window);window.fetch=(url,opts={})=>typeof url==='string'&&url.startsWith('/__loaders/versions')?Promise.resolve({ok:true,json:async()=>({ok:true,versions:['0.16.5']})}):typeof url==='string'&&url.startsWith('/__instances/one')?new Promise((resolve,reject)=>__requests.push({url,opts,resolve,reject,done:false})):fixtureFetch(url,opts);void 0`);
    await click('[data-configuration-id="one"] button[id="MoreOptionsDropdown"]');await ready(`!!document.querySelector('[data-hx-action="edit-installation"]')`);await click('[data-hx-action="edit-installation"]');
    await response('/__instances/one',{ok:true,id:'one',profile:{name:'Repair fixture',type:'custom',icon:'Grass',lastVersionId:'1.20.1',gameDir:'C:/fixture/one',javaArgs:'',javaDir:'',resolution:{width:854,height:480}},context:{minecraft:'1.20.1',loader:'fabric',loaderVersion:'0.16.5',javaMajor:17},ownership:{root:'C:/fixture/one',private:true,sharedWith:[],reason:''},versions:[{id:'1.20.1',type:'release'}],running:false,settings:{},...extra},true);
  }
  async function inventory(available=items){await click('[data-category="mods"]');await response('/content',{ok:true,revision:'content-r1',items:available,problems:[]});}
  async function selected(){await inventory();await click('[data-iw-select-id="alpha"]');await click('[data-iw-select-id="beta"]');await click('[data-iw-select-id="gamma"]');await click('[data-iw-review-selected]');}
  const text=()=>run(`document.querySelector('[data-iw-pane]').textContent`);
  async function footerVisible(selector){return run(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e)return false;const r=e.getBoundingClientRect();return !e.hidden&&!e.disabled&&!!e.closest('.__hx-foot')&&r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight;})()`);}
  async function capture(name){await run('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');win.webContents.invalidate();await new Promise(resolve=>setTimeout(resolve,100));fs.writeFileSync(path.join(output,name+'.png'),(await win.webContents.capturePage()).toPNG());report.captures.push(name+'.png');}
  async function check(name,fn){if(process.env.DEPENDENCY_REPAIR_TEST_FILTER&&!name.includes(process.env.DEPENDENCY_REPAIR_TEST_FILTER))return;try{await fixture();await fn();report.checks.push({name,ok:true});}catch(error){report.checks.push({name,ok:false,error:String(error.stack||error)});}}
  function finish(code,error){clearTimeout(deadline);if(error)report.error=String(error.stack||error);report.sourceSha256=require('node:crypto').createHash('sha256').update(fs.readFileSync(path.join(__dirname,'installation_ui.js'))).digest('hex');fs.writeFileSync(path.join(output,'ui-dependency-repair-report.json'),JSON.stringify(report,null,2));process.stdout.write(JSON.stringify(report,null,2)+'\n');if(win&&!win.isDestroyed())win.destroy();if(host)host.kill();app.exit(code);}
  app.whenReady().then(async()=>{deadline=setTimeout(()=>finish(2,'Native dependency repair fixture timed out'),115000);try{
    const port=await new Promise(resolve=>{const socket=net.createServer();socket.listen(0,'127.0.0.1',()=>{const value=socket.address().port;socket.close(()=>resolve(value));});});origin='http://127.0.0.1:'+port;
    const state=path.join(temporary,'state'),game=path.join(temporary,'game'),data=path.join(temporary,'data'),one=path.join(temporary,'one');
    [state,game,data,one,path.join(state,'versions/1.20.1')].forEach(folder=>fs.mkdirSync(folder,{recursive:true}));
    fs.writeFileSync(path.join(data,'state.json'),JSON.stringify({UiSettings:JSON.stringify({lastVisitedPage:'java'}),UiEvents:'{}'}));
    fs.writeFileSync(path.join(state,'launcher_profiles.json'),JSON.stringify({profiles:{one:{name:'Repair fixture',type:'custom',icon:'Grass',lastVersionId:'1.20.1',gameDir:one}}}));
    fs.writeFileSync(path.join(state,'versions/1.20.1/1.20.1.json'),JSON.stringify({id:'1.20.1',type:'release',mainClass:'Fixture',libraries:[],downloads:{client:{url:'https://example.invalid/fixture.jar',size:1}}}));
    fs.writeFileSync(path.join(state,'versions/version_manifest_v2.json'),JSON.stringify({latest:{release:'1.20.1',snapshot:'1.20.1'},versions:[]}));
    host=spawn('python',['-B',path.join(__dirname,'server.py'),String(port)],{cwd:root,windowsHide:true,env:{...process.env,LAUNCHER_ROOT:root,LAUNCHER_STATE_DIR:state,LAUNCHER_GAME_DIR:game,LAUNCHER_DATA_DIR:data,LAUNCHER_NO_RELOAD:'1',LAUNCHER_MSA_CLIENT_ID:''},stdio:['ignore','pipe','pipe']});const stream=fs.createWriteStream(path.join(output,'ui-dependency-repair-host.log'));host.stdout.pipe(stream);host.stderr.pipe(stream);
    for(let i=0;i<100;i++){try{await hostRequest('/__accounts');break;}catch(error){if(i===99)throw error;await new Promise(resolve=>setTimeout(resolve,100));}}await hostRequest('/__accounts',{action:'login',name:'DependencyRepairFixture'});
    win=new BrowserWindow({show:false,width:1280,height:720,useContentSize:true,frame:false,webPreferences:{contextIsolation:true,sandbox:true,nodeIntegration:false,offscreen:true,backgroundThrottling:false,partition:'dependency-repair-native'}});
    win.webContents.setWindowOpenHandler(()=>({action:'deny'}));win.webContents.session.setPermissionRequestHandler((_wc,_permission,callback)=>callback(false));win.webContents.session.on('will-download',event=>event.preventDefault());
    win.webContents.session.webRequest.onBeforeRequest((details,callback)=>{let allow=false;try{const u=new URL(details.url);allow=u.origin===origin||['data:','blob:','file:','devtools:'].includes(u.protocol);if(u.origin===origin&&/^\/__(open|dialog|launch|install|msa\/signin|loaders\/materialize)(\/|$)/.test(u.pathname)){report.blockedEffects.push(u.pathname);allow=false;}}catch{}callback({cancel:!allow});});
    win.webContents.on('console-message',event=>{if(event.level==='error'||event.level===3)report.consoleErrors.push(event.message);});
    await check('repair all sends current revision without broadening selected or disabled roots',async()=>{
      await inventory();await click('[data-iw-select-id="beta"]');await click('[data-iw-repair-dependencies]',2);
      assert.deepEqual(await payload('/plan'),{kind:'mods',repairDependencies:true,revision:'content-r1'});assert.equal(await run(`__requests.filter(r=>r.url.endsWith('/plan')).length`),1);assert.match(await text(),/Checking.*dependencies/i);
      await response('/plan',plan());assert.match(await text(),/3 enabled.*installed/i);assert.match(await text(),/unrecognized local archive/i);assert.match(await text(),/could not.*identif|unidentified/i);assert.match(await text(),/Helper API/);
      await clickText('Back');assert.equal(await run(`document.querySelector('[data-iw-select-id="beta"]').checked`),true);assert.equal(await run(`document.querySelector('[data-iw-select-id="beta"]').closest('li').textContent.includes('Disabled')`),true);
    });
    await check('selected repair sends enabled original IDs and applies only the reviewed plan once',async()=>{
      await selected();await click('[data-iw-repair-selected]',2);assert.deepEqual(await payload('/plan'),{kind:'mods',repairDependencies:true,revision:'content-r1',ids:['alpha','gamma']});assert.equal(await run(`__requests.filter(r=>r.url.endsWith('/plan')).length`),1);
      await response('/plan',plan({dependencyRepair:{checked:2,unidentified:[]}}));assert.match(await text(),/2 enabled.*selected/i);assert.match(await run(`document.querySelector('[data-iw-content-context]').textContent`),/Fabric 0\.16\.5/);assert.equal(await footerVisible('[data-iw-apply]'),true);await click('[data-iw-apply]',2);assert.deepEqual(await payload('/apply'),{planId:'repair-plan'});assert.equal(await run(`__requests.filter(r=>r.url.endsWith('/apply')).length`),1);
      await response('/apply',{ok:true,jobId:'repair-job'});await response('/job?jobId=repair-job',{ok:true,job:{id:'repair-job',kind:'mods',status:'succeeded',phase:'complete',result:{summary:{installed:[{name:'Helper API',filename:'helper.jar'}],reused:[items[0],items[2]],enabled:[],removed:[]}}}});assert.match(await text(),/Installation complete/);assert.match(await text(),/Helper API/);
    });
    await check('disabled-only selected repair is unavailable and never emits an empty broadening scope',async()=>{
      await inventory([items[1]]);await click('[data-iw-select-id="beta"]');await click('[data-iw-review-selected]');assert.equal(await run(`document.querySelector('[data-iw-repair-selected]')?.disabled`),true);await click('[data-iw-repair-selected]');assert.equal(await run(`__requests.filter(r=>r.url.endsWith('/plan')).length`),0);assert.match(await text(),/enabled mod|disabled.*select/i);
    });
    await check('failed repair retries once and Back preserves hidden selections and installed filters',async()=>{
      await inventory();await click('[data-iw-select-id="alpha"]');await click('[data-iw-select-id="beta"]');await fill('#iw-installed-search','Alpha');await click('#iw-installed-filter');await clickText('Enabled');await click('[data-iw-review-selected]');await click('[data-iw-repair-selected]');
      await response('/plan',{error:'Provider lookup unavailable',code:'provider_unavailable'});assert.doesNotMatch(await text(),/Checking.*dependencies/i);await click('[data-iw-read-retry]',2);assert.equal(await run(`__requests.filter(r=>r.url.endsWith('/plan')).length`),2);assert.deepEqual(await payload('/plan'),{kind:'mods',repairDependencies:true,revision:'content-r1',ids:['alpha']});
      await response('/plan',{error:'Provider lookup still unavailable'});await click('[data-iw-read-back]');assert.match(await text(),/Alpha Mod/);assert.match(await text(),/Beta Mod/);await clickText('Back');assert.equal(await run(`document.querySelector('#iw-installed-search').value`),'Alpha');assert.match(await run(`document.querySelector('#iw-installed-filter').textContent`),/Enabled/);assert.equal(await run(`document.querySelector('[data-iw-select-id="alpha"]').checked`),true);assert.match(await text(),/2 selected.*outside/i);
    });
    await check('late repair success after category navigation cannot overwrite an unsaved note draft',async()=>{
      await inventory();await click('[data-iw-repair-dependencies]');await pending('/plan');await click('[data-category="notes"]');await response('/tools?kind=notes',{ok:true,revision:'note-r1',capabilities:['save'],text:'Saved note'});await fill('#iw-notes','Unsaved note survives repair');await response('/plan',plan());assert.equal(await run(`document.querySelector('#iw-notes').value`),'Unsaved note survives repair');assert.equal(await run(`document.querySelector('[data-category][aria-selected="true"]').dataset.category`),'notes');await clickText('Save');assert.deepEqual(await payload('/tools'),{kind:'notes',action:'save',revision:'note-r1',text:'Unsaved note survives repair'});await response('/__instances/one/tools',{ok:true,revision:'note-r2'},true);
    });
    await check('server drafts remain preserved through repair review and Back',async()=>{
      await click('[data-category="servers"]');await response('/tools?kind=servers',{ok:true,revision:'servers-r1',capabilities:['save'],items:[{name:'Fixture server',address:'saved.test',resourcePolicy:'disabled'}]});await fill('#iw-server-address-0','unsaved.test');await inventory();await click('[data-iw-repair-dependencies]');await response('/plan',plan());await clickText('Back');await click('[data-category="servers"]');await response('/tools?kind=servers',{ok:true,revision:'servers-r1',capabilities:['save'],items:[{name:'Fixture server',address:'saved.test',resourcePolicy:'disabled'}]});assert.equal(await run(`document.querySelector('#iw-server-address-0').value`),'unsaved.test');assert.match(await run(`document.querySelector('.iw-footer-note').textContent`),/Unsaved servers/);
    });
    await check('running and shared installations cannot plan repairs',async()=>{
      for(const extra of [{running:true},{ownership:{root:'C:/fixture/one',private:false,reason:'Shared fixture directory'}}]){await fixture(extra);await inventory();assert.equal(await run(`document.querySelector('[data-iw-repair-dependencies]')?.disabled`),true);await click('[data-iw-select-id="alpha"]');await click('[data-iw-review-selected]');assert.equal(await run(`document.querySelector('[data-iw-repair-selected]')?.disabled`),true);assert.equal(await run(`__requests.filter(r=>r.url.endsWith('/plan')).length`),0);}
    });
    await check('dirty General context keeps repair unavailable until the existing draft gate resolves',async()=>{
      await fill('#iw-directory','C:/fixture/new unsaved');await click('[data-category="mods"]');assert.match(await text(),/Unsaved General/);assert.equal(await run(`!!document.querySelector('[data-iw-repair-dependencies]')`),false);assert.equal(await run(`__requests.filter(r=>r.url.endsWith('/plan')).length`),0);await clickText('Back to General');assert.equal(await run(`document.querySelector('#iw-directory').value`),'C:/fixture/new unsaved');
    });
    await check('unidentified no-change repair does not claim every installed mod is healthy',async()=>{
      await inventory();await click('[data-iw-repair-dependencies]');await response('/plan',plan({files:[],reuse:[],dependencyRepair:{checked:3,unidentified:[items[3]]}}));assert.equal(await run(`document.querySelector('[data-iw-apply]').disabled`),true);assert.match(await text(),/unrecognized local archive/i);assert.match(await text(),/could not|unidentified|unable/i);assert.doesNotMatch(await text(),/This content is already installed|No changes needed|all.*healthy/i);
    });
    await check('conflicting no-change repair shows blocking diagnostics instead of a healthy action',async()=>{
      await inventory();await click('[data-iw-repair-dependencies]');await response('/plan',plan({files:[],conflicts:[{code:'installed_dependency_conflict',message:'Helper API has an incompatible installed version.',dependency:'helper'}],dependencyRepair:{checked:3,unidentified:[]}}));assert.equal(await run(`document.querySelector('[data-iw-apply]').disabled`),true);assert.match(await text(),/incompatible installed version/);assert.doesNotMatch(await text(),/No changes needed|This content is already installed|all.*healthy/i);
    });
    await check('native normal and magnified repair reviews retain fixed footer reachability',async()=>{
      for(const [magnified,width,height] of [[false,1280,720],[true,1024,640]]){
        await fixture({},magnified,width,height);await inventory();await click('[data-iw-select-id="alpha"]');await click('[data-iw-select-id="beta"]');await click('[data-iw-select-id="unknown"]');assert.equal(await footerVisible('[data-iw-review-selected]'),true);
        await run(`document.querySelector('[data-iw-pane]').scrollTop=0;void 0`);assert.equal(await run(`(()=>{const e=document.querySelector('[data-iw-repair-dependencies]'),r=e.getBoundingClientRect();return !e.disabled&&r.left>=0&&r.right<=innerWidth&&r.top>=60&&r.bottom<document.querySelector('.__hx-foot').getBoundingClientRect().top;})()`),true);await capture('repair-installed-'+width+'x'+height+(magnified?'-magnified':''));
        await click('[data-iw-review-selected]');await capture('repair-selected-'+width+'x'+height+(magnified?'-magnified':''));await click('[data-iw-repair-selected]');assert.deepEqual(await payload('/plan'),{kind:'mods',repairDependencies:true,revision:'content-r1',ids:['alpha','unknown']});
        await response('/plan',plan({reuse:['alpha'],dependencyRepair:{checked:2,unidentified:[items[3]]},warnings:Array.from({length:30},(_,i)=>({message:'Optional fixture compatibility note '+i,dependency:'optional-'+i}))}));assert.match(await run(`document.querySelector('[data-iw-content-context]').textContent`),/Fabric 0\.16\.5/);assert.equal(await footerVisible('[data-iw-apply]'),true);assert.equal(await run(`document.querySelector('#__hx-screen').scrollWidth>document.querySelector('#__hx-screen').clientWidth`),false);assert.equal(await run(`getComputedStyle(document.querySelector('[data-iw-apply]')).fontSize`),magnified?'20px':'12px');await capture('repair-review-'+width+'x'+height+(magnified?'-magnified':''));
      }
    });
    assert.deepEqual(report.blockedEffects,[]);assert.deepEqual(report.consoleErrors,[]);finish(report.checks.some(check=>!check.ok)?1:0);
  }catch(error){finish(1,error);}});
}
