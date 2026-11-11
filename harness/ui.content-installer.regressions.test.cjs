// Real Chromium controls with deterministic service responses and disposable state.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
if (!process.versions.electron) {
  const {test} = require('node:test');
  const {execFileSync} = require('node:child_process');
  test('content logos and installation flow remain understandable and recoverable', () => {
    try { process.stdout.write(execFileSync(process.execPath, [path.join(__dirname, '../app/node_modules/electron/cli.js'), __filename], {encoding:'utf8',timeout:90000})); }
    catch(error) { if(error.stdout)process.stdout.write(error.stdout);throw error; }
  });
} else {
  const {app, BrowserWindow} = require('electron');
  app.disableHardwareAcceleration();
  app.setPath('userData', fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'launcher-content-ui-')));
  const shell = fs.readFileSync(path.join(__dirname,'ui.js'),'utf8').replace(/\}\)\(\);\s*$/, 'window.__launcherUI={screen,field,esc,closeScreen,profileForRow};})();');
  const artwork = 'data:image/png;base64,'+fs.readFileSync(path.join(__dirname,'../minecraft-launcher-re/src/ui/assets/icons/javaIcons/Furnace.png')).toString('base64');
  let win;
  const results=[];
  const run = code => win.webContents.executeJavaScript(code);
  const tick = () => new Promise(resolve=>setTimeout(resolve,25));
  async function ready(expression) {for(let i=0;i<70;i++){if(await run(expression))return;await tick();}assert.fail('UI did not settle: '+expression);}
  async function fixture(extra={}) {
    await win.loadURL('data:text/html;charset=utf-8,'+encodeURIComponent('<button id="opener">Edit</button><main><ul><li><img alt="Native installation" src="'+artwork+'"><div data-row-title>Native installation</div><div data-row-detail>1.20.1</div><button><span>Play</span></button><button id="MoreOptionsDropdown">More</button></li></ul></main>'));
    await run(`window.__entrySettled=true;window.__host={appData:{launcherSettings:{},configurationData:{profiles:{}}}};window.MCLauncher={openFileDialog:async()=>null};window.__requests=[];window.fetch=(url,opts={})=>url.startsWith('/__loaders/versions')?Promise.resolve({ok:true,json:async()=>({versions:['0.16.10']})}):new Promise((resolve,reject)=>__requests.push({url,opts,resolve,reject}));${shell}`);
    await run(fs.readFileSync(path.join(__dirname,'installation_ui.js'),'utf8'));
    await run(`__installationWorkspace.open('one',document.querySelector('#opener'));void 0`);
    await respond(0,{ok:true,id:'one',profile:{name:'My home',lastVersionId:'1.20.1',gameDir:'C:/fixture/one',icon:'Grass'},context:{minecraft:'1.20.1',loader:'fabric',loaderVersion:'0.16.10'},ownership:{root:'C:/fixture/one',private:true},versions:[],running:false,...extra});
  }
  async function respond(index,body) {await run(`__requests[${index}].resolve({ok:${!body.error},json:async()=>(${JSON.stringify(body)})})`);await tick();}
  async function click(selector) {
    const point=await run(`(()=>{const n=document.querySelector(${JSON.stringify(selector)});n.scrollIntoView({block:'center'});const r=n.getBoundingClientRect();return {x:Math.round(r.left+r.width/2),y:Math.round(r.top+r.height/2)}})()`);
    win.webContents.sendInputEvent({type:'mouseDown',button:'left',clickCount:1,...point});win.webContents.sendInputEvent({type:'mouseUp',button:'left',clickCount:1,...point});await tick();
  }
  async function inventory(items=[]) {await click('[data-category="mods"]');await respond(1,{ok:true,revision:'r1',items,problems:[]});}
  async function review(plan) {
    await inventory();await click('[data-iw-browse]');await respond(2,{ok:true,hits:[{project_id:'main',title:'Lovely Mod'}],total_hits:1});
    await click('[data-project-id="main"] button');await respond(3,{ok:true,versions:[{id:'vmain',version_number:'1.2',version_type:'release'}]});
    await click('[data-iw-plan]');assert.match(await run(`document.querySelector('[data-iw-status]').textContent`),/Checking compatibility and dependencies/);await respond(4,{ok:true,planId:'plan1',files:[],reuse:[],enable:[],remove:[],conflicts:[],warnings:[],...plan});
  }
  async function check(name,fn) {try {await fixture();await fn();results.push({name,ok:true});}catch(e){results.push({name,ok:false,error:String(e.stack||e)});}}
  app.whenReady().then(async()=>{
    win=new BrowserWindow({show:false,width:1024,height:640,webPreferences:{contextIsolation:true,sandbox:true,nodeIntegration:false,backgroundThrottling:false}});
    win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
    await check('catalog artwork uses original project logo and missing artwork has an honest fallback',async()=>{
      await inventory();await click('[data-iw-browse]');await respond(2,{ok:true,hits:[{project_id:'main',title:'Lovely Mod',icon_url:'https://cdn.modrinth.com/data/main/icon.png'},{project_id:'missing',title:'No artwork'},{project_id:'unsafe',title:'Unsafe artwork',icon_url:'https://example.invalid/icon.svg'}],total_hits:3});
      assert.equal(await run(`document.querySelector('[data-project-id="main"] [data-iw-logo] img')?.getAttribute('src') || ''`),'/__content_icon?url='+encodeURIComponent('https://cdn.modrinth.com/data/main/icon.png'));
      for(const id of ['missing','unsafe'])assert.equal(await run(`document.querySelector('[data-project-id="${id}"] [data-iw-logo] img')?.getAttribute('src') || ''`),'');
      await run(`document.querySelector('[data-project-id="main"] [data-iw-logo] img').dispatchEvent(new Event('error'))`);
      assert.equal(await run(`document.querySelector('[data-project-id="main"] [data-iw-logo-fallback]').hidden`),false);
    });
    await check('installed embedded logo decodes and detail uses human dependency names',async()=>{
      await inventory([{id:'mod1',kind:'mods',name:'Lovely Mod',filename:'lovely.jar',enabled:true,version:'1.2',iconData:artwork,requiredBy:['parent']},{id:'parent',kind:'mods',name:'My Modpack',filename:'parent.jar',enabled:true}]);
      await ready(`!!document.querySelector('[data-content-id="mod1"] [data-iw-logo] img')?.naturalWidth`);
      await click('[data-content-id="mod1"] button');
      assert.equal(await run(`document.querySelector('[data-iw-project-header] img').src`),artwork);
      assert.match(await run(`document.querySelector('[data-iw-pane]').textContent`),/Required by: My Modpack/);
    });
    await check('review explains target, download size and required dependency relationships',async()=>{
      await review({files:[{id:'vmain',versionId:'vmain',projectId:'main',name:'Lovely Mod',filename:'lovely.jar',version:'1.2',size:1048576,userRequested:true},{id:'vhelper',name:'Helper API',filename:'helper.jar',size:524288,userRequested:false,requiredBy:['vmain']}]});
      const text=await run(`document.querySelector('[data-iw-pane]').textContent`);
      assert.match(text,/My home/);assert.match(text,/Minecraft 1\.20\.1/);assert.match(text,/Fabric/);assert.match(text,/1\.5 MiB/);assert.match(text,/Required by: Lovely Mod/);
      assert.equal(await run(`document.querySelector('[data-iw-apply]').disabled`),false);
    });
    await check('already installed no-op cannot submit while provenance adoption remains available',async()=>{
      await review({reuse:['same'],reuseMetadata:{}});
      assert.equal(await run(`document.querySelector('[data-iw-apply]').disabled`),true);
      assert.match(await run(`document.querySelector('[data-iw-pane]').textContent`),/already installed|No changes needed/i);
      await click('[data-iw-close]');await fixture();
      await review({reuse:['same'],reuseMetadata:{same:{provider:'modrinth',projectId:'main'}}});
      assert.equal(await run(`document.querySelector('[data-iw-apply]').disabled`),false);
      assert.match(await run(`document.querySelector('[data-iw-pane]').textContent`),/existing files|existing content/i);
    });
    await check('install remains reachable without scrolling through a large compatibility report',async()=>{
      await review({files:[{name:'Lovely Mod',filename:'lovely.jar',size:1048576}],warnings:Array.from({length:40},(_,index)=>({message:'Optional compatibility note '+index,itemId:'existing'+index}))});
      assert.equal(await run(`(()=>{let b=document.querySelector('[data-iw-apply]'),r=b.getBoundingClientRect();return r.top>=0 && r.bottom<=innerHeight && !!b.closest('.__hx-foot')})()`),true);
    });
    await check('local import dependency error shows missing module and recovery',async()=>{
      await inventory();await run(`window.MCLauncher.openFileDialog=async()=> 'C:/fixture/needs-helper.jar';void 0`);await click('[data-iw-native-import]');await click('[data-iw-native-import-review]');
      await respond(2,{error:'Local files have missing dependencies',code:'dependency_conflict',details:[{code:'missing_dependency',message:'A required module is missing.',dependency:'helper_mod',requirements:['>=1.0.0']}]});
      const text=await run(`document.querySelector('[data-iw-pane]').textContent`);assert.match(text,/helper_mod/);assert.match(text,/>=1\.0\.0/);assert.ok(await run(`!!document.querySelector('[data-iw-browse-required]')`));
    });
    await check('progress uses verified counts and completion gives actual installed results once',async()=>{
      await review({files:[{name:'Lovely Mod',filename:'lovely.jar',size:1,userRequested:true}]});await click('[data-iw-apply]');await respond(5,{ok:true,jobId:'job1'});
      await respond(6,{ok:true,job:{id:'job1',status:'running',phase:'downloading',message:'Downloading helper.jar',completed:1,total:2}});
      assert.deepEqual(await run(`(()=>{let p=document.querySelector('[data-iw-job-progress]');return p?{value:p.value,max:p.max}:null})()`),{value:1,max:2});
      await ready(`__requests.length===8`);await respond(7,{ok:true,job:{id:'job1',status:'succeeded',phase:'complete',result:{summary:{installed:[{name:'Lovely Mod',filename:'lovely.jar'}],reused:[{name:'Helper API',filename:'helper.jar'}],enabled:[],removed:[]}}}});
      const text=await run(`document.querySelector('[data-iw-pane]').textContent`);assert.match(text,/Installation complete/);assert.match(text,/Lovely Mod/);assert.match(text,/Helper API/);assert.doesNotMatch(text,/installation continues/i);assert.equal(await run(`document.querySelectorAll('[data-iw-job-installed]').length`),1);
    });
    await check('failed installations stop announcing continued work and retain dependency details',async()=>{
      await review({files:[{name:'Lovely Mod',filename:'lovely.jar',size:1}]});await click('[data-iw-apply]');await respond(5,{ok:true,jobId:'job1'});
      await respond(6,{ok:true,job:{id:'job1',status:'failed',phase:'failed',error:'The verified mod needs another module.',details:[{code:'missing_dependency',message:'Required module missing.',dependency:'helper_mod',requirements:['>=1.0.0']}]}});
      const text=await run(`document.querySelector('[data-iw-pane]').textContent`);assert.match(text,/Installation failed/);assert.match(text,/helper_mod/);assert.doesNotMatch(text,/installation continues/i);
    });
    await check('finished background result is reachable when the editor reopens',async()=>{
      await fixture({latestJob:{id:'done',status:'succeeded',message:'Content updated',result:{summary:{installed:[{name:'Lovely Mod',filename:'lovely.jar'}],reused:[],enabled:[],removed:[]}}}});
      assert.equal(await run(`!!document.querySelector('[data-iw-last-operation]')`),true);await click('[data-iw-last-operation]');
      const text=await run(`document.querySelector('[data-iw-pane]').textContent`);assert.match(text,/Installation complete/);assert.match(text,/Lovely Mod/);assert.doesNotMatch(text,/installation continues/i);
      assert.equal(await run(`document.querySelector('[data-category][aria-selected="true"]').dataset.category`),'mods');await click('[data-iw-job-installed]');await respond(1,{ok:true,revision:'r2',items:[{id:'mod1',kind:'mods',name:'Lovely Mod',filename:'lovely.jar',enabled:true}],problems:[]});assert.equal(await run(`!!document.querySelector('[data-content-id="mod1"]')`),true);
    });
    await check('reopened pack job returns to its own files and catalog category',async()=>{
      await fixture({latestJob:{id:'packdone',kind:'resourcepacks',status:'succeeded',message:'Content updated',result:{summary:{installed:[{name:'Home Pack',filename:'home.zip'}],reused:[],enabled:[],removed:[]}}}});await click('[data-iw-last-operation]');
      assert.equal(await run(`document.querySelector('[data-category][aria-selected="true"]').dataset.category`),'resourcepacks');await click('[data-iw-job-installed]');await respond(1,{ok:true,revision:'r2',items:[{id:'pack1',kind:'resourcepacks',name:'Home Pack',filename:'home.zip',enabled:true}],problems:[]});assert.equal(await run(`!!document.querySelector('[data-content-id="pack1"]')`),true);await click('[data-iw-browse]');assert.match(await run(`__requests[2].url`),/kind=resourcepacks/);
    });
    await check('reopened failed mod job still offers the required-mod recovery',async()=>{
      await fixture({latestJob:{id:'failed',kind:'mods',status:'failed',error:'Dependency verification failed',details:[{code:'missing_dependency',message:'Required module missing.',dependency:'helper_mod'}]}});await click('[data-iw-last-operation]');
      assert.equal(await run(`!!document.querySelector('[data-iw-browse-required]')`),true);await click('[data-iw-browse-required]');assert.equal(await run(`__requests.filter(r=>r.url.endsWith('/content')).length`),1);assert.match(await run(`__requests.find(r=>r.url.includes('/catalog?')).url`),/kind=mods/);
    });
    await check('reopening progress through content refresh cannot create concurrent polling chains',async()=>{
      await review({files:[{name:'Lovely Mod',filename:'lovely.jar',size:1}]});await click('[data-iw-apply]');await respond(5,{ok:true,jobId:'job1'});
      await click('[data-category="mods"]');await respond(7,{ok:true,revision:'r1',items:[],problems:[]});
      assert.equal(await run(`__requests.filter(r=>r.url.includes('/job?')).length`),1);
      await respond(6,{ok:true,job:{id:'job1',status:'succeeded',phase:'complete',result:{summary:{installed:[],reused:[],enabled:[],removed:[]}}}});
      await new Promise(resolve=>setTimeout(resolve,850));assert.equal(await run(`__requests.filter(r=>r.url.includes('/job?')).length`),1);
    });
    process.stdout.write(JSON.stringify(results,null,2)+'\n');app.exit(results.some(r=>!r.ok)?1:0);
  }).catch(e=>{process.stderr.write(String(e.stack||e));app.exit(1)});
}
