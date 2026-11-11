// Disposable Chromium fixtures. Every download, update and external action is intercepted.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
if(!process.versions.electron){
  const {test}=require('node:test'),{execFileSync}=require('node:child_process');
  test('five-upgrade dialogs keep controls honest and preserve drafts',()=>{
    try{process.stdout.write(execFileSync(process.execPath,[path.join(__dirname,'../app/node_modules/electron/cli.js'),__filename],{encoding:'utf8',timeout:60000}));}
    catch(error){if(error.stdout)process.stdout.write(error.stdout);throw error;}
  });
}else{
  const {app,BrowserWindow}=require('electron');app.disableHardwareAcceleration();
  const output=process.env.FIVE_UPGRADES_UI_OUTPUT||fs.mkdtempSync(path.join(require('node:os').tmpdir(),'launcher-five-ui-'));
  fs.mkdirSync(output,{recursive:true});app.setPath('userData',path.join(output,'electron'));
  const source=fs.readFileSync(path.join(__dirname,'ui.js'),'utf8').replace(/\}\)\(\);\s*$/,`
    window.__fiveUI={openModpackDialog,openExportDialog,injectUnavailableSettings,
      openUpdatesDialog:typeof openUpdatesDialog==='function'?openUpdatesDialog:()=>{},
      injectUpdatesButton:typeof injectUpdatesButton==='function'?injectUpdatesButton:()=>{},
      injectUnsupportedServices:typeof injectUnsupportedServices==='function'?injectUnsupportedServices:()=>{}};
  })();`);
  const markup=`<main><h1>Installations</h1><div><label id="versions_search_label">Search</label><div><input type="text"></div></div>
    <ul><li><div>Fixture installation</div><div>1.20.1</div><div><button><span>Play</span></button><button id="MoreOptionsDropdown">More</button></div></li></ul></main>`;
  let win;const results=[];const run=code=>win.webContents.executeJavaScript(code);
  const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  async function ready(code){for(let n=0;n<60;n++){if(await run(code))return;await pause(30);}assert.fail('UI did not settle: '+code);}
  async function fixture(html=markup){
    await win.loadURL('data:text/html;charset=utf-8,'+encodeURIComponent(html));
    await run(`window.__entrySettled=true;window.__requests=[];window.__pushed=[];
      window.__host={appData:{launcherSettings:{},capabilities:{localJava:true,realms:false,purchases:false},configurationData:{profiles:{}}},push:key=>__pushed.push(key)};
      window.__job={job:null,phase:'idle'};
      window.fetch=(url,opts)=>{
        if(String(url)==='/__modpacks/status'&&!window.__deferStatus)return Promise.resolve({ok:true,json:async()=>({...__job})});
        if(String(url).startsWith('/__modpacks/search'))return Promise.resolve({ok:true,json:async()=>({total:0,hits:[]})});
        return new Promise((resolve,reject)=>__requests.push({url,opts,resolve,reject}));};
      ${source}`);
  }
  async function respond(route,body){await ready(`__requests.some(r=>r.url===${JSON.stringify(route)}&&!r.done)`);await run(`(() => {const r=__requests.find(r=>r.url===${JSON.stringify(route)}&&!r.done);r.done=true;r.resolve({ok:true,json:async()=>(${JSON.stringify(body)})});})()`);await pause(30);}
  async function setJob(value){await run(`__job=${JSON.stringify(value)};`);await pause(450);}
  async function check(name,fn){try{await fn();results.push({name,ok:true});}catch(error){results.push({name,ok:false,error:String(error.stack||error)});}}
  const active=(extra={})=>({job:'import',jobId:'import-owned',phase:'downloading',controlStatus:'running',message:'Downloading fixture',done:1,total:3,canPause:true,canResume:false,canCancel:true,...extra});
  async function importJob(){await fixture();await run(`__fiveUI.openModpackDialog();document.querySelector('#__hx-modpack-search').value='keep this query';document.querySelector('[data-hx-local-pack-import]').click();`);
    await run(`__job=${JSON.stringify(active())}`);await respond('/__modpacks/import-file',{ok:true,started:true,jobId:'import-owned'});await pause(30);}
  async function updater(initial={}){await fixture(`<main><h1>Settings</h1><section><div><input type="checkbox" id="keepLauncherOpen"><label for="keepLauncherOpen">Keep launcher open</label></div></section></main>`);
    await run(`window.__updates={status:'setup',currentVersion:'3.41.30',feedUrl:'',availableVersion:null,progress:null,error:null,installAllowed:false,lastCheckedAt:null,...${JSON.stringify(initial)}};window.__updateCalls=[];window.__updateListeners=[];window.__updateRequests=[];window.__stateRequests=[];window.__updateStateReads=0;
      window.launcherDesktop=Object.freeze({getAppInfo:async()=>({version:'3.41.30',packaged:true}),getUpdateState:async()=>{__updateStateReads++;const initial={...__updates};if(window.__deferState)await new Promise(resolve=>__stateRequests.push(resolve));return initial;},
        configureUpdateFeed:async url=>{__updateCalls.push(['configure',url]);if(window.__deferConfigure)await new Promise(resolve=>__updateRequests.push(resolve));return {...__updates,feedUrl:url,status:url?'idle':'setup',lastCheckedAt:null};},
        checkForUpdates:async()=>{__updateCalls.push(['check']);return {...__updates,status:'checking'};},
        restartAndInstall:async()=>{__updateCalls.push(['install']);return {...__updates,status:'installing'};},
        onUpdateState:callback=>{__updateListeners.push(callback);return ()=>{__updateListeners=__updateListeners.filter(c=>c!==callback);};}});
      __fiveUI.openUpdatesDialog();`);await pause(30);}
  app.whenReady().then(async()=>{
    win=new BrowserWindow({width:1024,height:640,useContentSize:true,show:false,webPreferences:{contextIsolation:true,nodeIntegration:false,sandbox:true,offscreen:true,backgroundThrottling:false}});
    await check('working settings retain choices and explain background play and local-only crash help',async()=>{
      await fixture(`<main><section><div><input type="checkbox" id="keepLauncherOpen" checked disabled aria-describedby="old-help"><label for="keepLauncherOpen">Keep the launcher open</label></div><p id="old-help">Existing help</p><div><input type="checkbox" id="crashAssistance" disabled><label for="crashAssistance">Send crash reports</label></div></section></main>`);
      await run(`__fiveUI.injectUnavailableSettings();__fiveUI.injectUnavailableSettings();`);
      assert.deepEqual(await run(`[document.querySelector('#keepLauncherOpen').disabled,document.querySelector('#crashAssistance').disabled]`),[false,false]);
      assert.deepEqual(await run(`[document.querySelector('#keepLauncherOpen').checked,document.querySelector('#crashAssistance').checked]`),[true,false]);
      assert.match(await run(`document.querySelector('#__hx-setting-keepLauncherOpen-note').textContent`),/closing.*keeps.*game.*tray/i);
      assert.match(await run(`document.querySelector('label[for="crashAssistance"]').textContent`),/local crash explanations/i);
      assert.equal(await run(`document.querySelectorAll('[data-hx-setting-note]').length`),2);
    });
    await check('pause and resume target the owned import and retain the search draft',async()=>{
      await importJob();await ready(`!!document.querySelector('[data-hx-job-action="pause"]')&&!document.querySelector('[data-hx-job-action="pause"]').disabled`);
      await run(`document.querySelector('[data-hx-job-action="pause"]').click();`);
      assert.deepEqual(await run(`JSON.parse(__requests.find(r=>r.url==='/__modpacks/control').opts.body)`),{jobId:'import-owned',action:'pause'});
      await respond('/__modpacks/control',active({paused:true,controlStatus:'paused',canPause:false,canResume:true}));
      await run(`__job=${JSON.stringify(active({paused:true,controlStatus:'paused',canPause:false,canResume:true}))}`);
      await ready(`!document.querySelector('[data-hx-job-action="resume"]').hidden&&!document.querySelector('[data-hx-job-action="resume"]').disabled`);
      await run(`document.querySelector('[data-hx-job-action="resume"]').click();`);
      assert.deepEqual(await run(`JSON.parse(__requests.filter(r=>r.url==='/__modpacks/control')[1].opts.body)`),{jobId:'import-owned',action:'resume'});
      assert.equal(await run(`document.querySelector('#__hx-modpack-search').value`),'keep this query');
    });
    await check('commit restrictions are visible and cancel cannot produce a successful import',async()=>{
      await importJob();await setJob(active({phase:'committing',canPause:false,canCancel:false,controlReason:'Finishing the installation; controls are unavailable during commit.'}));
      assert.equal(await run(`document.querySelector('[data-hx-job-action="cancel"]').disabled`),true);
      assert.match(await run(`document.querySelector('[data-hx-job-control-reason]').textContent`),/during commit/);
      await setJob(active({job:null,phase:'cancelled',cancelRequested:true,canPause:false,canCancel:false,result:{name:'Never committed',profiles:{bad:{name:'bad'}}}}));
      assert.match(await run(`document.querySelector('#__hx-mod-status').textContent`),/cancelled/i);
      assert.deepEqual(await run(`__pushed`),[]);assert.equal(await run(`document.querySelector('[data-hx-local-pack-import]').disabled`),false);
    });
    await check('reopening a paused import recovers Resume and rejects another job result',async()=>{
      await fixture();await run(`__job=${JSON.stringify(active({paused:true,controlStatus:'paused',canPause:false,canResume:true}))};__fiveUI.openModpackDialog();`);
      await ready(`!!document.querySelector('[data-hx-job-action="resume"]')&&!document.querySelector('[data-hx-job-action="resume"]').disabled`);
      await setJob({job:null,jobId:'other-job',phase:'done',result:{name:'Different job',profiles:{bad:{name:'bad'}}}});
      assert.doesNotMatch(await run(`document.querySelector('#__hx-mod-status').textContent`),/Imported Different job/);assert.deepEqual(await run(`__pushed`),[]);
    });
    await check('a cancelled export never exposes Open folder and can be retried',async()=>{
      await fixture();await run(`__fiveUI.openExportDialog({id:'fixture',name:'Export fixture'});document.querySelector('.__hx-primary').click();__job=${JSON.stringify(active({job:'export',jobId:'export-owned'}))};`);
      await respond('/__modpacks/export',{ok:true,started:true,jobId:'export-owned'});await pause(30);
      await ready(`!!document.querySelector('[data-hx-job-action="cancel"]')&&!document.querySelector('[data-hx-job-action="cancel"]').disabled`);
      await run(`document.querySelector('[data-hx-job-action="cancel"]').click();`);assert.deepEqual(await run(`JSON.parse(__requests.find(r=>r.url==='/__modpacks/control').opts.body)`),{jobId:'export-owned',action:'cancel'});
      const cancelled={job:null,jobId:'export-owned',phase:'cancelled',controlStatus:'cancelled',cancelRequested:true,result:{path:'C:/never-created/file.mrpack'}};
      await respond('/__modpacks/control',cancelled);await setJob(cancelled);
      assert.match(await run(`document.querySelector('#__hx-export-status').textContent`),/cancelled/i);
      assert.equal(await run(`[...document.querySelectorAll('.__hx-foot button')].find(e=>e.textContent==='Open folder').style.display`),'none');
      assert.equal(await run(`document.querySelector('.__hx-primary').disabled`),false);
    });
    await check('an outstanding export recovery request cannot overlap polling or complete a newer export',async()=>{
      await fixture();await run(`__deferStatus=true;__fiveUI.openExportDialog({id:'fixture',name:'Export fixture'});document.querySelector('.__hx-primary').click();`);
      await respond('/__modpacks/export',{ok:true,started:true,jobId:'export-owned'});await pause(450);
      assert.equal(await run(`__requests.filter(r=>r.url==='/__modpacks/status'&&!r.done).length`),1);
      await respond('/__modpacks/status',{job:null,jobId:'obsolete',phase:'done',result:{path:'C:/obsolete/file.mrpack'}});
      await ready(`__requests.filter(r=>r.url==='/__modpacks/status'&&!r.done).length===1`);
      assert.doesNotMatch(await run(`document.querySelector('#__hx-export-status').textContent`),/Saved C:\/obsolete/);
      await respond('/__modpacks/status',active({job:'export',jobId:'export-owned'}));
      assert.equal(await run(`document.querySelector('.__hx-primary').disabled`),true);
    });
    await check('update source draft survives progress events while busy updates block editing',async()=>{
      await updater();await ready(`!!document.querySelector('#__hx-update-feed')`);
      assert.match(await run(`document.querySelector('#__hx-screen').textContent`),/3\.41\.30/);
      await run(`const feed=document.querySelector('#__hx-update-feed');feed.value='https://releases.example.invalid/my-draft/';feed.dispatchEvent(new Event('input',{bubbles:true}));feed.focus();feed.setSelectionRange(8,16);__updates={...__updates,status:'downloading',feedUrl:'https://saved.example.invalid/',availableVersion:'3.41.31',progress:{percent:42,transferred:42,total:100,bytesPerSecond:2}};__updateListeners.forEach(c=>c(__updates));__updates={...__updates,progress:{...__updates.progress,percent:43}};__updateListeners.forEach(c=>c(__updates));`);
      assert.deepEqual(await run(`(() => {const f=document.querySelector('#__hx-update-feed');return {value:f.value,disabled:f.disabled,start:f.selectionStart,end:f.selectionEnd};})()`),{value:'https://releases.example.invalid/my-draft/',disabled:true,start:8,end:16});
      assert.match(await run(`document.querySelector('#__hx-update-status').textContent`),/43%/);
    });
    await check('Use GitHub replaces the draft and saves the repository through the facade once',async()=>{
      await updater({status:'idle',feedUrl:'https://custom.example.invalid/releases/'});
      assert.equal(await run(`!!document.querySelector('[data-hx-update-github]')`),true,'Use GitHub action should be available');
      await run(`window.__deferConfigure=true;const f=document.querySelector('#__hx-update-feed');f.value='https://draft.example.invalid/';f.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('[data-hx-update-github]').click();`);
      await ready(`__updateRequests.length===1`);
      assert.deepEqual(await run(`__updateCalls`),[['configure','https://github.com/abdulmunthakimwork/minecraft-launcher/']]);
      assert.equal(await run(`document.querySelector('#__hx-update-feed').value`),'https://github.com/abdulmunthakimwork/minecraft-launcher/');
      assert.deepEqual(await run(`['#__hx-update-feed','[data-hx-update-save]','[data-hx-update-github]','[data-hx-update-check]','[data-hx-update-install]'].map(q=>document.querySelector(q).disabled)`),[true,true,true,true,true]);
      await run(`document.querySelector('[data-hx-update-github]').dispatchEvent(new Event('click',{bubbles:true}));document.querySelector('[data-hx-update-save]').dispatchEvent(new Event('click',{bubbles:true}));__updates={...__updates,feedUrl:'https://saved.example.invalid/'};__updateListeners.forEach(c=>c(__updates));`);
      assert.equal(await run(`document.querySelector('#__hx-update-feed').value`),'https://github.com/abdulmunthakimwork/minecraft-launcher/');
      assert.equal(await run(`__updateCalls.length`),1);
      await run(`__updateRequests.shift()();`);await ready(`!document.querySelector('[data-hx-update-save]').disabled`);
      assert.equal(await run(`document.querySelector('#__hx-update-feed').value`),'https://github.com/abdulmunthakimwork/minecraft-launcher/');
    });
    await check('checking downloading and installing reject source actions and keep a typed custom draft',async()=>{
      await updater({status:'idle',feedUrl:'https://saved.example.invalid/'});
      assert.equal(await run(`!!document.querySelector('[data-hx-update-github]')`),true,'Use GitHub action should be available');
      await run(`const f=document.querySelector('#__hx-update-feed');f.value='https://custom.example.invalid/keep-this/';f.dispatchEvent(new Event('input',{bubbles:true}));f.blur();`);
      for(const phase of ['checking','downloading','installing']){
        await run(`__updates={...__updates,status:${JSON.stringify(phase)},feedUrl:'https://github.com/abdulmunthakimwork/minecraft-launcher/'};__updateListeners.forEach(c=>c(__updates));`);
        assert.deepEqual(await run(`['#__hx-update-feed','[data-hx-update-save]','[data-hx-update-github]','[data-hx-update-check]'].map(q=>document.querySelector(q).disabled)`),[true,true,true,true],phase+' should prevent source changes');
        await run(`document.querySelector('[data-hx-update-github]').dispatchEvent(new Event('click',{bubbles:true}));document.querySelector('[data-hx-update-save]').dispatchEvent(new Event('click',{bubbles:true}));`);
        assert.deepEqual(await run(`__updateCalls`),[],phase+' should not call configure');
        assert.equal(await run(`document.querySelector('#__hx-update-feed').value`),'https://custom.example.invalid/keep-this/');
      }
      await run(`__updates={...__updates,status:'idle'};__updateListeners.forEach(c=>c(__updates));`);
      assert.deepEqual(await run(`['#__hx-update-feed','[data-hx-update-save]','[data-hx-update-github]'].map(q=>document.querySelector(q).disabled)`),[false,false,false]);
      assert.equal(await run(`document.querySelector('#__hx-update-feed').value`),'https://custom.example.invalid/keep-this/');
    });
    await check('the browser view and closed dialog cannot invoke desktop update actions',async()=>{
      await fixture();await run(`__fiveUI.openUpdatesDialog();`);await pause(30);
      assert.equal(await run(`!!document.querySelector('[data-hx-update-github]')`),true,'Use GitHub action should remain visible in a browser view');
      assert.deepEqual(await run(`['#__hx-update-feed','[data-hx-update-save]','[data-hx-update-github]','[data-hx-update-check]','[data-hx-update-install]'].map(q=>document.querySelector(q).disabled)`),[true,true,true,true,true]);
      assert.match(await run(`document.querySelector('#__hx-update-status').textContent`),/desktop app.*browser view/i);
      await updater({status:'idle',feedUrl:'https://saved.example.invalid/'});
      await run(`window.__closedUpdateControls=['[data-hx-update-save]','[data-hx-update-github]','[data-hx-update-check]'].map(q=>document.querySelector(q));document.querySelector('.__hx-close').click();__closedUpdateControls.forEach(b=>b.dispatchEvent(new Event('click',{bubbles:true})));`);await pause(30);
      assert.deepEqual(await run(`__updateCalls`),[]);assert.equal(await run(`__updateListeners.length`),0);
    });
    await check('an unchecked idle source never claims Up to date until a successful check is reported',async()=>{
      await updater({status:'idle',feedUrl:'https://github.com/abdulmunthakimwork/minecraft-launcher/'});
      assert.doesNotMatch(await run(`document.querySelector('#__hx-update-status').textContent`),/up to date/i);
      await run(`__updates={...__updates,status:'idle',lastCheckedAt:'2026-11-11T12:00:00.000Z'};__updateListeners.forEach(c=>c(__updates));`);
      assert.match(await run(`document.querySelector('#__hx-update-status').textContent`),/up to date/i);
    });
    await check('the Settings entry announces a verified ready update and opens the existing review dialog',async()=>{
      await updater({status:'ready',feedUrl:'https://github.com/abdulmunthakimwork/minecraft-launcher/',availableVersion:'3.41.31',installAllowed:false});
      await run(`document.querySelector('.__hx-close').click();__fiveUI.injectUpdatesButton();`);await pause(30);
      assert.equal(await run(`document.querySelector('#__hx-updates button span').textContent`),'Launcher update ready');
      assert.match(await run(`document.querySelector('#__hx-updates button').getAttribute('aria-label')`),/3\.41\.31/);
      await run(`document.querySelector('#__hx-updates button').click();`);await pause(30);
      assert.match(await run(`document.querySelector('#__hx-update-status').textContent`),/downloaded and verified/i);
      assert.deepEqual(await run(`__updateCalls`),[]);
      await run(`document.querySelector('.__hx-close').click();__updates={...__updates,status:'checking',availableVersion:null};__updateListeners.forEach(c=>c(__updates));`);
      assert.equal(await run(`document.querySelector('#__hx-updates button span').textContent`),'Launcher updates');
      assert.equal(await run(`document.querySelector('#__hx-updates button').hasAttribute('aria-label')`),false);
    });
    await check('Settings remounts reuse one update listener and a late read cannot erase a ready indication',async()=>{
      await updater({status:'idle',feedUrl:'https://github.com/abdulmunthakimwork/minecraft-launcher/'});
      await run(`document.querySelector('.__hx-close').click();window.__deferState=true;__fiveUI.injectUpdatesButton();`);await pause(30);
      assert.equal(await run(`__updateListeners.length`),1,'Settings entry should subscribe once');
      assert.equal(await run(`__stateRequests.length`),1);
      await run(`__updates={...__updates,status:'ready',availableVersion:'3.41.31'};__updateListeners.forEach(c=>c(__updates));__stateRequests.shift()();`);await pause(30);
      assert.equal(await run(`document.querySelector('#__hx-updates button span').textContent`),'Launcher update ready');
      await run(`document.querySelector('main').innerHTML='<h1>Settings</h1>';__fiveUI.injectUpdatesButton();__fiveUI.injectUpdatesButton();`);await pause(30);
      assert.equal(await run(`document.querySelectorAll('#__hx-updates').length`),1);
      assert.equal(await run(`__updateListeners.length`),1);
      assert.equal(await run(`__updateStateReads`),2,'Initial dialog and entry read should each run once');
      assert.equal(await run(`document.querySelector('#__hx-updates button span').textContent`),'Launcher update ready');
      await run(`__updates={...__updates,status:'error',error:'Disconnected'};__updateListeners.forEach(c=>c(__updates));`);
      assert.equal(await run(`document.querySelector('#__hx-updates button span').textContent`),'Launcher updates');
    });
    await check('restart and install is blocked during work and requires an explicit click when ready',async()=>{
      await updater();await run(`__updates={...__updates,status:'ready',availableVersion:'3.41.31',installAllowed:false};__updateListeners.forEach(c=>c(__updates));`);
      await ready(`!!document.querySelector('[data-hx-update-install]')`);assert.equal(await run(`document.querySelector('[data-hx-update-install]').disabled`),true);
      assert.match(await run(`document.querySelector('#__hx-update-install-reason').textContent`),/game|download|work/i);
      await run(`__updates={...__updates,installAllowed:true};__updateListeners.forEach(c=>c(__updates));`);assert.deepEqual(await run(`__updateCalls`),[]);
      await run(`document.querySelector('[data-hx-update-install]').click();`);await pause(30);assert.deepEqual(await run(`__updateCalls`),[['install']]);
    });
    await check('updates validates HTTPS, saves the exact draft, and unsubscribes when closed',async()=>{
      await updater();await ready(`!!document.querySelector('[data-hx-update-save]')`);
      await run(`document.querySelector('#__hx-update-feed').value='http://bad.example.invalid/';document.querySelector('[data-hx-update-save]').click();`);await pause(30);assert.deepEqual(await run(`__updateCalls`),[]);
      assert.match(await run(`document.querySelector('#__hx-update-status').textContent`),/https/i);
      await run(`document.querySelector('#__hx-update-feed').value='https://releases.example.invalid/feed/';document.querySelector('[data-hx-update-save]').click();`);await pause(30);
      assert.deepEqual(await run(`__updateCalls`),[['configure','https://releases.example.invalid/feed/']]);
      await run(`document.querySelector('.__hx-close').click();`);await pause(30);assert.equal(await run(`__updateListeners.length`),0);
    });
    await check('unsupported controls offer official destinations while read-only content and native links stay usable',async()=>{
      await fixture(`<main><h1>Marketplace</h1><p id="readonly">Read-only pack details</p><button aria-label="Subscribe now">Subscribe now</button><a id="faq" href="https://www.minecraft.net/en-us/marketplace">FAQ</a><a id="java" href="/games/java/play">Java Edition</a></main>`);
      await run(`window.__nativeCalls=0;document.querySelector('button').addEventListener('click',()=>__nativeCalls++);__fiveUI.injectUnsupportedServices('/games/bedrock/marketplace_pass');document.querySelector('[data-hx-unsupported-action]').click();`);
      await ready(`!!document.querySelector('#__hx-screen')`);assert.equal(await run(`__nativeCalls`),0);
      assert.match(await run(`document.querySelector('#__hx-screen').textContent`),/unavailable|not supported/i);
      assert.equal(await run(`document.querySelector('#readonly').textContent`),'Read-only pack details');assert.equal(await run(`document.querySelector('#faq').getAttribute('href')`),'https://www.minecraft.net/en-us/marketplace');
      assert.ok(await run(`!!document.querySelector('#__hx-screen a[href="https://www.minecraft.net/en-us/marketplace"]')`));assert.equal(await run(`document.querySelector('#java').getAttribute('href')`),'/games/java/play');
    });
    await check('purchase buttons on Java offer the official store while the supported Play action stays native',async()=>{
      await fixture(`<main><button id="java-play" aria-label="Play">Play</button><button id="buy" aria-label="Buy now">Buy now</button><a href="/games/java/play">Java Edition</a></main>`);
      await run(`window.__nativeBuys=0;window.__nativePlay=0;document.querySelector('#buy').addEventListener('click',()=>__nativeBuys++);document.querySelector('#java-play').addEventListener('click',()=>__nativePlay++);__fiveUI.injectUnsupportedServices('/games/java/play');document.querySelector('#java-play').click();document.querySelector('#buy').click();`);
      await ready(`!!document.querySelector('#__hx-screen')`);assert.deepEqual(await run(`[__nativeBuys,__nativePlay]`),[0,1]);
      assert.ok(await run(`!!document.querySelector('#__hx-screen a[href="https://www.minecraft.net/en-us/store/minecraft-java-bedrock-edition-pc"]')`));
    });
    await check('larger update text fits the minimum window and keeps footer actions reachable',async()=>{
      await updater({status:'ready',feedUrl:'https://github.com/abdulmunthakimwork/minecraft-launcher/',availableVersion:'3.41.31',installAllowed:true,lastCheckedAt:'2026-11-11T12:00:00.000Z'});await run(`__host.appData.launcherSettings.useMagnifiedMode=true;__fiveUI.openUpdatesDialog();`);await pause(30);
      const metrics=await run(`(() => {const s=document.querySelector('#__hx-screen'),f=s.querySelector('.__hx-foot');return {width:innerWidth,scroll:s.scrollWidth,footer:f.getBoundingClientRect().bottom,height:innerHeight,font:getComputedStyle(document.querySelector('#__hx-update-feed')).fontSize,buttons:[...f.querySelectorAll('button:not([hidden])')].map(b=>{const r=b.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom};})};})()`);
      assert.ok(metrics.scroll<=metrics.width+1);assert.ok(metrics.footer<=metrics.height+1);assert.equal(metrics.font,'20px');assert.equal(metrics.buttons.length,5);
      for(const b of metrics.buttons){assert.ok(b.left>=0&&b.right<=metrics.width&&b.bottom<=metrics.height);}
      for(let i=1;i<metrics.buttons.length;i++){const before=metrics.buttons[i-1],after=metrics.buttons[i];assert.ok(after.top>=before.bottom||after.left>=before.right+7,'Footer buttons need room between targets');}
      fs.writeFileSync(path.join(output,'updates-large-minimum.png'),(await win.webContents.capturePage()).toPNG());
    });
    fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({output,results},null,2));console.log(JSON.stringify({output,results},null,2));win.destroy();app.exit(results.some(r=>!r.ok)?1:0);
  }).catch(error=>{console.error(error.stack||error);app.exit(2);});
}
