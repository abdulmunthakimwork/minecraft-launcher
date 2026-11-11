// Real Chromium layout checks against an isolated host and the official UI.
// No desktop, game, download, file-picker or real account state is touched.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

if (!process.versions.electron) {
  const {test} = require('node:test');
  const {execFileSync} = require('node:child_process');
  test('added dialogs preserve native text sizes, contain long paths and explain validation', () => {
    const cli = path.join(__dirname, '../app/node_modules/electron/cli.js');
    process.stdout.write(execFileSync(process.execPath, [cli, __filename], {encoding:'utf8', timeout:120000}));
  });
} else {
  const {app, BrowserWindow} = require('electron');
  const {spawn} = require('node:child_process');
  const http = require('node:http');
  const net = require('node:net');
  const root = path.dirname(__dirname);
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'launcher-layout-'));
  const output = process.env.LAUNCHER_LAYOUT_OUTPUT || temporary;
  fs.mkdirSync(output, {recursive:true});
  app.setPath('userData', path.join(temporary, 'electron'));
  app.disableHardwareAcceleration();
  app.commandLine.appendSwitch('disable-renderer-backgrounding');
  app.commandLine.appendSwitch('disable-background-timer-throttling');
  let host, win, origin, hasLoaded=false;
  const pendingRequests=new Set();
  const report = {startedAt:new Date().toISOString(), checks:[], native:[], consoleErrors:[], blockedEffects:[], output};
  const pause = ms => new Promise(resolve=>setTimeout(resolve,ms));
  const run = code => win.webContents.executeJavaScript(code);
  const deadline = setTimeout(()=>{report.fatal='layout deadline';finish(2);},110000);
  const failures = [];

  function request(route, body) {
    return new Promise((resolve,reject)=>{
      const encoded=body?JSON.stringify(body):null;
      const req=http.request(origin+route,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:{}},res=>{
        let data='';res.on('data',chunk=>data+=chunk);res.on('end',()=>{
          if(res.statusCode>=400)reject(new Error(data));else resolve(JSON.parse(data));
        });
      });req.on('error',reject);req.end(encoded);
    });
  }
  async function ready(code) {
    for(let i=0;i<80;i++){if(await run(code))return;await pause(100);}
    throw new Error('Page did not settle: '+code);
  }
  async function load(route) {
    if(hasLoaded)await requestsSettled();
    await win.loadURL(origin+route);
    hasLoaded=true;
    const marker=route.includes('configurations')?'#__hx-modpacks':route.endsWith('/accounts')?'#__hx-add-offline':'[data-hx-setting-note]';
    await ready(`window.__entrySettled && location.pathname===${JSON.stringify(route)} && !!document.querySelector(${JSON.stringify(marker)})`);
  }
  async function requestsSettled() {
    let idleSince=null;
    for(let attempt=0;attempt<150;attempt++) {
      if(pendingRequests.size)idleSince=null;
      else if(idleSince===null)idleSince=Date.now();
      else if(Date.now()-idleSince>=300)return;
      await pause(100);
    }
    throw new Error('Page requests did not settle before fixture navigation');
  }
  async function clickExact(label) {
    const clicked=await run(`(() => {
      const label=${JSON.stringify(label)};
      const el=[...document.querySelectorAll('button,a,[role="button"]')].find(e=>
        e.getClientRects().length && ((e.getAttribute('aria-label')||e.innerText||'').replace(/\\s+/g,' ').trim()===label));
      if(!el) return false;el.click();return true;
    })()`);
    assert.equal(clicked,true,'Missing action '+label);
  }
  async function check(name, fn) {
    try {await fn();report.checks.push({name,ok:true});}
    catch(error){failures.push(name);report.checks.push({name,ok:false,error:String(error.stack||error)});}
  }
  async function capture(label) {
    await run('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
    const img=await win.webContents.capturePage();
    if(/^(custom-offline|long-path)/.test(label)) {
      const dimensions=img.getSize(),pixels=img.toBitmap(),offset=(100*dimensions.width+10)*4;
      assert.deepEqual([...pixels.subarray(offset,offset+3)].sort((a,b)=>a-b),[21,22,23],
        'Capture must paint the full-window dialog surface rather than a stale page');
    }
    fs.writeFileSync(path.join(output,label+'.png'),img.toPNG());
  }
  async function nativeSizes(magnified) {
    await request('/__launcher_settings',{locale:'en-US',useMagnifiedMode:magnified});
    await load('/games/java/configurations');
    await ready(`!!document.querySelector('#__hx-modpacks')`);
    await clickExact('New installation');
    await ready(`!!document.querySelector('#modal-dialog #configuration_name')`);
    const sizes=await run(`(() => {
      const input=document.querySelector('#configuration_name');
      const dialog=input.closest('#modal-dialog');
      const button=[...document.querySelectorAll('button')].find(e=>e.getClientRects().length&&/^Cancel$/i.test(e.innerText.trim()));
      const label=document.querySelector('[for="configuration_name"]') || document.getElementById(input.getAttribute('aria-labelledby')) || input.parentElement.previousElementSibling;
      const read=e=>{const s=getComputedStyle(e);return {font:s.fontSize,line:s.lineHeight,height:e.getBoundingClientRect().height};};
      return {input:read(input),button:read(button),label:read(label),viewport:[innerWidth,innerHeight]};
    })()`);
    report.native.push({magnified,...sizes});
    await capture('native-installation-'+(magnified?'large':'normal'));
    return sizes;
  }
  async function openOffline() {
    await load('/settings/accounts');
    await ready(`!!document.querySelector('#__hx-add-offline')`);
    await clickExact('Add offline account');
    await ready(`!!document.querySelector('#__hx-screen #__hx_offline_name')`);
    assert.equal(await run(`document.activeElement.id`),'__hx_offline_name');
  }
  function finish(code) {
    clearTimeout(deadline);report.finishedAt=new Date().toISOString();
    fs.writeFileSync(path.join(output,'layout-report.json'),JSON.stringify(report,null,2));
    process.stdout.write(JSON.stringify(report)+'\n');
    if(win&&!win.isDestroyed())win.destroy();if(host)host.kill();app.exit(code);
  }

  app.whenReady().then(async()=>{
    try {
      const port=await new Promise(resolve=>{const socket=net.createServer();socket.listen(0,'127.0.0.1',()=>{
        const port=socket.address().port;socket.close(()=>resolve(port));
      });});
      origin='http://127.0.0.1:'+port;
      const state=path.join(temporary,'state'),game=path.join(temporary,'game'),data=path.join(temporary,'data');
      for(const folder of [state,game,data,path.join(state,'versions/1.20.1')])fs.mkdirSync(folder,{recursive:true});
      // Supply a disposable native route store before the first document load.
      fs.writeFileSync(path.join(data,'state.json'),JSON.stringify({UiSettings:JSON.stringify({lastVisitedPage:'java'}),UiEvents:'{}'}));
      fs.writeFileSync(path.join(state,'launcher_profiles.json'),JSON.stringify({profiles:{fixture:{name:'Layout fixture',type:'custom',lastVersionId:'1.20.1'}}}));
      fs.writeFileSync(path.join(state,'versions/1.20.1/1.20.1.json'),JSON.stringify({id:'1.20.1',type:'release',mainClass:'Fixture',libraries:[],downloads:{client:{url:'https://example.invalid/fixture.jar',size:1}}}));
      fs.writeFileSync(path.join(state,'versions/version_manifest_v2.json'),JSON.stringify({latest:{release:'1.20.1',snapshot:'1.20.1'},versions:[]}));
      host=spawn('python',['-B',path.join(__dirname,'server.py'),String(port)],{cwd:root,windowsHide:true,
        env:{...process.env,LAUNCHER_ROOT:root,LAUNCHER_STATE_DIR:state,LAUNCHER_GAME_DIR:game,LAUNCHER_DATA_DIR:data,LAUNCHER_NO_RELOAD:'1',LAUNCHER_MSA_CLIENT_ID:''},stdio:['ignore','pipe','pipe']});
      const stream=fs.createWriteStream(path.join(output,'host.log'));host.stdout.pipe(stream);host.stderr.pipe(stream);
      for(let i=0;i<80;i++){try{await request('/__accounts');break;}catch(error){if(i===79)throw error;await pause(100);}}
      await request('/__accounts',{action:'login',name:'LayoutFixture'});
      win=new BrowserWindow({width:1280,height:720,useContentSize:true,show:false,frame:false,
        webPreferences:{contextIsolation:true,nodeIntegration:false,sandbox:true,offscreen:true,backgroundThrottling:false,partition:'layout-fixture'}});
      win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
      win.webContents.session.setPermissionRequestHandler((_wc,_permission,callback)=>callback(false));
      win.webContents.session.on('will-download',event=>event.preventDefault());
      win.webContents.session.webRequest.onBeforeRequest((details,callback)=>{
        let allowed=false;try{const u=new URL(details.url);allowed=['data:','blob:','file:','devtools:','chrome:'].includes(u.protocol)||u.origin===origin;
          if(u.origin===origin&&/^\/__(open|dialog|launch|install|msa\/signin|loaders\/materialize)(\/|$)/.test(u.pathname)){
            report.blockedEffects.push({path:u.pathname,method:details.method});allowed=false;
          }
        }catch{}
        if(allowed&&details.resourceType==='xhr')pendingRequests.add(details.id);
        callback({cancel:!allowed});
      });
      win.webContents.session.webRequest.onCompleted(details=>pendingRequests.delete(details.id));
      win.webContents.session.webRequest.onErrorOccurred(details=>pendingRequests.delete(details.id));
      win.webContents.on('console-message',details=>{if(details.level==='error'||details.level===3)report.consoleErrors.push({page:win.webContents.getURL(),message:details.message});});

      for(const magnified of [false,true]) {
        const native=await nativeSizes(magnified);
        await openOffline();
        const custom=await run(`(() => {
          const read=e=>{const s=getComputedStyle(e);return {font:s.fontSize,line:s.lineHeight,height:e.getBoundingClientRect().height};};
          return {input:read(document.querySelector('#__hx_offline_name')),button:read(document.querySelector('#__hx-screen .__hx-btn')),label:read(document.querySelector('#__hx-screen label'))};
        })()`);
        await capture('custom-offline-'+(magnified?'large':'normal'));
        await check('custom '+(magnified?'magnified':'normal')+' controls use native metrics',async()=>{
          assert.deepEqual(custom.input,native.input);assert.deepEqual(custom.button,native.button);
          assert.equal(custom.label.font,native.label.font);assert.equal(custom.label.line,native.label.line);
        });
        await check('long Windows path wraps at '+(magnified?'magnified':'normal')+' minimum window',async()=>{
          win.setContentSize(1024,640);await pause(100);
          const longPath='Saved C:\\Users\\fixture\\'+('VeryLongUnbrokenFolderName'.repeat(50))+'.mrpack';
          const metrics=await run(`(() => {
            const error=document.querySelector('#__hx_offline_error');
            error.textContent=${JSON.stringify(longPath)};
            const note=document.querySelector('#__hx-screen .__hx-note');note.textContent='Installation '+('LongUnbrokenName'.repeat(40));
            const form=document.querySelector('#__hx-screen form'),body=document.querySelector('#__hx-screen .__hx-body');
            return {body:[body.clientWidth,body.scrollWidth],form:[form.clientWidth,form.scrollWidth],error:[error.clientWidth,error.scrollWidth],note:[note.clientWidth,note.scrollWidth]};
          })()`);
          await capture('long-path-'+(magnified?'large':'normal'));
          for(const [name,[client,scroll]] of Object.entries(metrics))assert.ok(scroll<=client+1,name+' overflows: '+scroll+' > '+client);
          win.setContentSize(1280,720);
        });
      }
      await openOffline();
      await check('invalid offline name is associated with its announced explanation',async()=>{
        const result=await run(`(() => {
          const input=document.querySelector('#__hx_offline_name');input.value='a';
          document.querySelector('#__hx-screen .__hx-primary').click();
          const error=document.querySelector('#__hx_offline_error');
          return {invalid:input.getAttribute('aria-invalid'),description:input.getAttribute('aria-describedby'),role:error.getAttribute('role'),text:error.textContent,focus:document.activeElement.id};
        })()`);
        assert.equal(result.invalid,'true');assert.ok(result.description.split(/\s+/).includes('__hx_offline_error'));
        assert.equal(result.role,'alert');assert.match(result.text,/3.?16/);assert.equal(result.focus,'__hx_offline_name');
      });
      await check('correcting the offline name clears stale field validation',async()=>{
        const result=await run(`(() => {
          const input=document.querySelector('#__hx_offline_name');input.value='ValidName';input.dispatchEvent(new Event('input',{bubbles:true}));
          return {invalid:input.getAttribute('aria-invalid'),text:document.querySelector('#__hx_offline_error').textContent};
        })()`);
        assert.notEqual(result.invalid,'true');assert.equal(result.text,'');
      });
      await check('magnified injected settings notes use the native body text ramp',async()=>{
        await load('/settings/general');
        await ready(`!!document.querySelector('[data-hx-setting-note]')`);
        const metrics=await run(`(() => {
          const note=document.querySelector('[data-hx-setting-note]');
          const label=document.querySelector('label[for="keepLauncherOpen"]');
          return {note:getComputedStyle(note).fontSize,native:getComputedStyle(label).fontSize,line:getComputedStyle(note).lineHeight};
        })()`);
        assert.equal(metrics.note,metrics.native);assert.equal(metrics.line,'24px');
      });
      await check('magnified loader fallback and status match native text',async()=>{
        await load('/games/java/configurations');
        await run(`const loaderFetch=window.fetch;window.fetch=(url,opts)=>String(url).startsWith('/__loaders/versions')
          ?Promise.resolve({ok:true,json:async()=>({versions:['0.16.5'],recommended:'0.16.5'})}):loaderFetch(url,opts);void 0;`);
        await clickExact('New installation');
        await ready(`!!document.querySelector('[data-loader="fabric-loader"]')`);
        await run(`document.querySelector('[data-loader="fabric-loader"]').click()`);
        await ready(`!!document.querySelector('#__hx-loader-version [data-loader-version]')`);
        const metrics=await run(`(() => {
          const read=e=>({font:getComputedStyle(e).fontSize,line:getComputedStyle(e).lineHeight});
          const option=document.querySelector('#__hx-loader-version .__hx-loader-fallback-option');
          const note=document.querySelector('#__hx-loader-version > p');
          return {fallback:read(option),note:read(note),status:read(document.querySelector('#__hx-loader-status'))};
        })()`);
        await run(`document.querySelector('#modal-dialog [data-testid="toggle-list-search"]').closest('[role="button"]').click()`);
        await ready(`!!document.querySelector('#toggle-list-menu li button')`);
        const native=await run(`(() => {const e=document.querySelector('#toggle-list-menu li button');const s=getComputedStyle(e);return {font:s.fontSize,line:s.lineHeight};})()`);
        report.native.push({magnified:true,loaderOption:native,injected:metrics});
        assert.deepEqual(metrics.fallback,native);assert.equal(metrics.note.font,'20px');assert.equal(metrics.note.line,'24px');
        assert.equal(metrics.status.font,'20px');assert.equal(metrics.status.line,'24px');
      });
      await requestsSettled();
      assert.deepEqual(report.consoleErrors,[],'Unexpected renderer errors');
      assert.deepEqual(report.blockedEffects,[],'Fixture triggered an external action');
    }catch(error){report.fatal=String(error.stack||error);
      if(win&&!win.isDestroyed())report.fatalPage=await run(`({path:location.pathname,text:document.body.innerText.slice(0,3000)})`).catch(()=>null);
    }
    finish(report.fatal||failures.length?1:0);
  });
}
