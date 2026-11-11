// Uses the official bundle and disposable files in hidden Electron windows.
// Catalog and authentication actions are intercepted; no download or browser opens.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

if (!process.versions.electron) {
  const {test} = require('node:test');
  const {execFileSync} = require('node:child_process');
  test('native modpack rows and account entry points remain usable without installations', () => {
    process.stdout.write(execFileSync(process.execPath,
      [path.join(__dirname, '../app/node_modules/electron/cli.js'), __filename],
      {encoding:'utf8', timeout:65000}));
  });
} else {
  const {app, BrowserWindow} = require('electron');
  const {spawn} = require('node:child_process');
  const net = require('node:net');
  const http = require('node:http');
  const root = path.dirname(__dirname);
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'launcher-modpack-dom-'));
  const output = process.env.MODPACK_TEST_OUTPUT || temporary;
  fs.mkdirSync(output, {recursive:true});
  app.setPath('userData', path.join(temporary, 'electron'));
  app.disableHardwareAcceleration();
  app.commandLine.appendSwitch('disable-renderer-backgrounding');
  app.commandLine.appendSwitch('disable-background-timer-throttling');
  let win, host, origin, hasLoaded=false;
  const pendingRequests=new Set();
  const report = {checks:[], errors:[]};
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const run = source => win.webContents.executeJavaScript(source);
  const state = path.join(temporary, 'state');
  const game = path.join(temporary, 'game');
  const data = path.join(temporary, 'data');
  const profile = {name:'Native Fixture', type:'custom', lastVersionId:'26.3',
    lastUsed:'2026-11-10T00:00:00.000Z'};
  function profiles(value) {
    fs.writeFileSync(path.join(state, 'launcher_profiles.json'), JSON.stringify({profiles:value}));
  }
  function request(route, body) {
    return new Promise((resolve,reject) => {
      const req = http.request(origin + route, {method:body?'POST':'GET',
        headers:body?{'Content-Type':'application/json'}:{}}, response => {
        let raw=''; response.on('data', chunk=>raw+=chunk);
        response.on('end',()=>{try{resolve(JSON.parse(raw));}catch(error){reject(error);}});
      }); req.on('error',reject); req.end(body?JSON.stringify(body):null);
    });
  }
  async function load(route) {
    if(hasLoaded)await requestsSettled();
    await win.loadURL(origin + route);hasLoaded=true;
    const marker=route.includes('configurations')?'#__hx-modpacks':'#__hx-add-offline';
    await ready(`window.__entrySettled && location.pathname===${JSON.stringify(route)} && !!document.querySelector(${JSON.stringify(marker)})`);
    (report.loadStates ||= []).push(await run(`({path:location.pathname,text:document.body.innerText})`));
    assert.ok(!/Whoops!|PAGE NOT FOUND/.test(await run('document.body.innerText')));
  }
  async function ready(source) {
    for(let attempt=0;attempt<100;attempt++) {
      if(await run(source))return;
      await sleep(100);
    }
    throw new Error('Modpack fixture did not settle: '+source);
  }
  async function requestsSettled() {
    let idleSince=null;
    for(let attempt=0;attempt<150;attempt++) {
      if(pendingRequests.size)idleSince=null;
      else if(idleSince===null)idleSince=Date.now();
      else if(Date.now()-idleSince>=300)return;
      await sleep(100);
    }
    throw new Error('Page requests did not settle before fixture navigation');
  }
  async function check(name, action) {
    try {await action(); report.checks.push({name, passed:true});}
    catch(error) {report.checks.push({name, passed:false, error:String(error.stack||error)});}
  }
  async function capture(name) {
    const picture=await win.webContents.capturePage();
    fs.writeFileSync(path.join(output,name+'.png'),picture.toPNG());
  }
  async function openCatalog() {
    await run(`window.__catalogCalls=[];window.__importCalls=[];
      const previousFetch=window.fetch;window.fetch=(url,opts)=>{
        if(String(url).startsWith('/__modpacks/search')){__catalogCalls.push(String(url));
          return Promise.resolve({ok:true,json:async()=>({total:1,hits:[{
            id:'fixture-pack',title:'Native Modpack Fixture',author:'Fixture Author',
            loader:'fabric-loader',downloads:1234}]})});}
        if(String(url)==='/__modpacks/import'){__importCalls.push(JSON.parse(opts.body));
          return Promise.resolve({ok:true,json:async()=>({error:'Fixture import intercepted'})});}
        return previousFetch(url,opts);};document.querySelector('#__hx-modpacks').click();`);
    await ready(`document.querySelectorAll('#__hx-screen li').length===1`);
  }
  async function watchAuthentication() {
    await run(`window.__authCalls=[];const previousFetch=window.fetch;window.fetch=(url,opts)=>{
      if(String(url)==='/__msa/config')return Promise.resolve({ok:true,json:async()=>({
        configured:false,clientId:null,source:null,environmentOverride:false})});
      if(String(url).startsWith('/__msa')){__authCalls.push(String(url));
        return Promise.resolve({ok:false,json:async()=>({error:'Fixture authentication intercepted'})});}
      return previousFetch(url,opts);};void 0;`);
  }
  async function catalogWorks(name) {
    await openCatalog();
    report[name]=await run(`({text:document.querySelector('#__hx-screen').innerText,
      calls:__catalogCalls,rows:[...document.querySelectorAll('#__hx-screen li')].map(e=>e.outerHTML)})`);
    await capture(name);
    assert.equal(await run(`document.querySelectorAll('#__hx-screen li').length`),1);
    assert.match(await run(`document.querySelector('#__hx-screen li').innerText`),/Native Modpack Fixture/);
    assert.match(await run(`document.querySelector('#__hx-screen li').innerText`),/Fixture Author/);
    assert.equal(await run(`document.querySelectorAll('#__hx-screen li button').length`),1);
    assert.equal(await run(`document.querySelector('#__hx-screen li button').innerText.trim()`),'Import');
    assert.match(await run(`document.querySelector('#__hx-screen li img').getAttribute('src')`),/Grass\.png$/);
    assert.equal(await run(`document.querySelector('#__hx-screen li').hasAttribute('data-configuration-id')`),false);
    await run(`document.querySelector('#__hx-screen li button').click()`); await sleep(100);
    assert.deepEqual(await run('__importCalls'),[{projectId:'fixture-pack'}]);
    assert.equal(await run('__catalogCalls.length'),1);
  }
  app.whenReady().then(async()=>{
    try {
      for(const folder of [state,game,data,path.join(state,'versions/26.3'),
        path.join(game,'versions/26.3'),path.join(game,'runtime/jre-legacy/bin')])fs.mkdirSync(folder,{recursive:true});
      fs.writeFileSync(path.join(state,'versions/26.3/26.3.json'),JSON.stringify({id:'26.3',type:'release',mainClass:'Fixture',
        libraries:[],downloads:{client:{url:'https://example.invalid/never-download.jar',size:1}}}));
      fs.writeFileSync(path.join(state,'versions/version_manifest_v2.json'),JSON.stringify({latest:{release:'26.3',snapshot:'26.3'},versions:[]}));
      fs.writeFileSync(path.join(data,'state.json'),JSON.stringify({UiSettings:JSON.stringify({lastVisitedPage:'java'}),UiEvents:'{}'}));
      profiles({'native-fixture':profile});
      fs.writeFileSync(path.join(game,'versions/26.3/26.3.jar'),'x');
      fs.writeFileSync(path.join(game,'runtime/jre-legacy/bin/java.exe'),'fixture-never-executed');
      fs.writeFileSync(path.join(game,'runtime/jre-legacy/manifest.json'),JSON.stringify({files:{'bin/java.exe':{type:'file',size:22}}}));
      const port=await new Promise(resolve=>{const reserve=net.createServer();reserve.listen(0,'127.0.0.1',()=>{
        const number=reserve.address().port;reserve.close(()=>resolve(number));});});
      origin='http://127.0.0.1:'+port;
      host=spawn('python',['-B',path.join(__dirname,'server.py'),String(port)],{cwd:root,windowsHide:true,
        env:{...process.env,LAUNCHER_ROOT:root,LAUNCHER_STATE_DIR:state,LAUNCHER_GAME_DIR:game,LAUNCHER_DATA_DIR:data,
          LAUNCHER_NO_RELOAD:'1',LAUNCHER_MSA_CLIENT_ID:''},stdio:['ignore','pipe','pipe']});
      const log=fs.createWriteStream(path.join(output,'host.log'));host.stdout.pipe(log);host.stderr.pipe(log);
      for(let attempt=0;attempt<60;attempt++){try{await request('/__accounts');break;}catch(error){
        if(attempt===59)throw error;await sleep(200);}}
      await request('/__accounts',{action:'login',name:'FixturePlayer'});
      win=new BrowserWindow({width:1280,height:720,show:false,webPreferences:{contextIsolation:true,nodeIntegration:false,
        sandbox:true,offscreen:true,backgroundThrottling:false,partition:'modpack-fixture-memory'}});
      win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
      win.webContents.session.webRequest.onBeforeRequest((details,callback)=>{
        let allowed=false;try{const url=new URL(details.url);allowed=url.origin===origin||
          ['data:','blob:','file:','devtools:','chrome:'].includes(url.protocol);}catch{}
        if(allowed&&details.resourceType==='xhr')pendingRequests.add(details.id);
        callback({cancel:!allowed});});
      win.webContents.session.webRequest.onCompleted(details=>pendingRequests.delete(details.id));
      win.webContents.session.webRequest.onErrorOccurred(details=>pendingRequests.delete(details.id));
      win.webContents.on('console-message',details=>{if(details.level==='error'||details.level===3)report.errors.push(details.message);});
      await check('search hits remain native rows when installation search matches nothing',async()=>{
        await load('/games/java/configurations');
        assert.equal(await run('location.pathname'),'/games/java/configurations');
        report.nativeRow=await run(`[...document.querySelectorAll('main li[data-configuration-id]')].find(e=>e.getClientRects().length)?.outerHTML`);
        await run(`const input=document.querySelector('#versions_search_label').parentElement.querySelector('input');
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'does-not-match-any-installation');
          input.dispatchEvent(new Event('input',{bubbles:true}));`);await sleep(350);
        assert.equal(await run(`Array.from(document.querySelectorAll('main li[data-configuration-id]')).filter(e=>e.getClientRects().length).length`),0);
        await catalogWorks('filtered-catalog');
      });
      await check('a first load with no saved installations still offers import',async()=>{
        profiles({});await load('/games/java/configurations');
        assert.equal(await run(`Object.keys(__host.appData.configurationData.profiles).length`),0);
        assert.equal(await run(`Array.from(document.querySelectorAll('main li[data-configuration-id]')).filter(e=>e.getClientRects().length).length`),0);
        await catalogWorks('empty-catalog');
      });
      await check('modpack search clears native and fallback fields without stale badges or IDs',async()=>{
        await load('/games/java/configurations'); await openCatalog();
        await run(`const field=document.querySelector('#__hx-modpack-search');field.value='home pack';field.dispatchEvent(new Event('input',{bubbles:true}));`);
        assert.equal(await run(`!!document.querySelector('[data-hx-modpack-clear]')`),true,'a Clear action exists even when the original search was empty');
        await run(`document.querySelector('[data-hx-modpack-clear]').click()`);
        await ready(`document.querySelector('#__hx-modpack-search').value==='' && __catalogCalls.at(-1).includes('query=&')`);
        assert.equal(await run(`document.querySelectorAll('#__hx-screen .results-tag').length`),0);
        assert.equal(await run(`document.querySelectorAll('#__hx-modpack-search').length`),1);
        assert.equal(await run(`document.activeElement.id`),'__hx-modpack-search');
      });
      await check('local pack picker cancellation restores the catalog and import action once',async()=>{
        await load('/games/java/configurations'); await openCatalog();
        assert.equal(await run(`!!document.querySelector('[data-hx-local-pack-import]')`),true,'local .mrpack entry is visible');
        await run(`window.__localCalls=[];const localFetch=window.fetch;window.fetch=(url,opts)=>{
          if(String(url)==='/__modpacks/import-file'){__localCalls.push(JSON.parse(opts.body));return Promise.resolve({ok:true,json:async()=>({ok:true,cancelled:true,started:false})});}
          return localFetch(url,opts);};document.querySelector('[data-hx-local-pack-import]').click();`);
        await ready(`document.querySelector('#__hx-screen').innerText.includes('File selection was cancelled.')`);
        assert.deepEqual(await run('__localCalls'),[{}]);
        assert.equal(await run(`document.querySelector('[data-hx-local-pack-import]').disabled`),false);
        assert.equal(await run(`document.querySelector('#__hx-modpack-search').disabled`),false);
        assert.equal(await run(`document.querySelectorAll('#__hx-screen li').length`),1);
        assert.equal(await run(`document.querySelector('[data-hx-local-pack-import]').innerText.trim()`),'Import from file…');
        await capture('local-import-cancelled');
      });
      await check('empty import retains native larger-text controls at the minimum window',async()=>{
        await request('/__launcher_settings',{useMagnifiedMode:true});
        win.setContentSize(1024,640);
        await load('/games/java/configurations');await openCatalog();
        const metrics=await run(`(() => {
          const read=e=>{const s=getComputedStyle(e);return {font:s.fontSize,line:s.lineHeight,height:s.height};};
          return {native:read(document.querySelector('[data-hx-installation-template] button')),
            imported:read(document.querySelector('#__hx-screen li button')),
            width:innerWidth,documentWidth:document.documentElement.scrollWidth,
            formWidth:document.querySelector('#__hx-screen form').scrollWidth};
        })()`);
        assert.deepEqual(metrics.imported,metrics.native);
        assert.equal(metrics.imported.font,'20px');
        assert.ok(metrics.documentWidth<=metrics.width+1);
        assert.ok(metrics.formWidth<=metrics.width+1);
        await capture('empty-catalog-large-minimum');
        await request('/__launcher_settings',{useMagnifiedMode:false});win.setContentSize(1280,720);
      });
      await check('the original Microsoft action opens the working host dialog once',async()=>{
        await load('/settings/accounts');
        await watchAuthentication();
        report.accountBefore=await run(`document.querySelector('#microsoftAccountSection').innerText`);
        assert.equal(await run(`document.querySelectorAll('#microsoftAccountSection [data-hx-account-action="online"]').length`),0);
        await run(`[...document.querySelectorAll('#microsoftAccountSection button')].find(e=>e.innerText.trim()==='Add Microsoft account').click();`);
        await ready(`!!document.querySelector('#__hx-online-go')`);
        report.accountAfter=await run(`({text:document.body.innerText,dialogs:document.querySelectorAll('#__hx-screen').length})`);
        await capture('native-account-action');
        assert.equal(report.accountAfter.dialogs,1,report.accountAfter.text);
        assert.match(report.accountAfter.text,/Sign in with Microsoft/);
        assert.deepEqual(await run('__authCalls'),[]);
      });
      await check('native Microsoft Enter and Space activation each open exactly one dialog',async()=>{
        for(const keyCode of ['Enter','Space']) {
          await load('/settings/accounts');
          await watchAuthentication();
          await run(`document.querySelector('[data-hx-online-bound]').focus()`);
          win.webContents.sendInputEvent({type:'keyDown',keyCode});
          win.webContents.sendInputEvent({type:'char',keyCode});
          win.webContents.sendInputEvent({type:'keyUp',keyCode});
          await ready(`!!document.querySelector('#__hx-online-go')`);
          assert.equal(await run(`document.querySelectorAll('#__hx-screen').length`),1);
          assert.equal(await run(`document.querySelectorAll('#__hx-online-go').length`),1);
          assert.ok(!/Sorry! Something unexpected/.test(await run('document.body.innerText')));
          assert.deepEqual(await run('__authCalls'),[]);
        }
      });
      await requestsSettled();
      assert.equal(report.errors.length,0,report.errors.join('\n'));
    }catch(error){report.fatal=String(error.stack||error);}
    finally{
      fs.writeFileSync(path.join(output,'ui-modpack-report.json'),JSON.stringify(report,null,2));
      if(win)win.destroy();if(host)host.kill();
      console.log(JSON.stringify({checks:report.checks,errors:report.errors,fatal:report.fatal,output},null,2));
      app.exit(report.fatal||report.checks.some(e=>!e.passed)?1:0);
    }
  });
}
