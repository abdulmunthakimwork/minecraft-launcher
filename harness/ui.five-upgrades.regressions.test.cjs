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
  async function updater(){await fixture(`<main><h1>Settings</h1><section><div><input type="checkbox" id="keepLauncherOpen"><label for="keepLauncherOpen">Keep launcher open</label></div></section></main>`);
    await run(`window.__updates={status:'setup',currentVersion:'3.41.30',feedUrl:'',availableVersion:null,progress:null,error:null,installAllowed:false};window.__updateCalls=[];window.__updateListeners=[];
      window.launcherDesktop=Object.freeze({getAppInfo:async()=>({version:'3.41.30',packaged:true}),getUpdateState:async()=>({...__updates}),
        configureUpdateFeed:async url=>{__updateCalls.push(['configure',url]);return {...__updates,feedUrl:url,status:url?'idle':'setup'};},
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
    await check('update setup uses the facade version and preserves feed draft and focus across progress events',async()=>{
      await updater();await ready(`!!document.querySelector('#__hx-update-feed')`);
      assert.match(await run(`document.querySelector('#__hx-screen').textContent`),/3\.41\.30/);assert.match(await run(`document.querySelector('#__hx-screen').textContent`),/host.*later|hosted release/i);
      await run(`const feed=document.querySelector('#__hx-update-feed');feed.value='https://releases.example.invalid/my-draft/';feed.focus();feed.setSelectionRange(8,16);__updates={...__updates,status:'downloading',feedUrl:'https://saved.example.invalid/',availableVersion:'3.41.31',progress:{percent:42,transferred:42,total:100,bytesPerSecond:2}};__updateListeners.forEach(c=>c(__updates));`);
      assert.deepEqual(await run(`({value:document.querySelector('#__hx-update-feed').value,focus:document.activeElement.id,start:document.activeElement.selectionStart,end:document.activeElement.selectionEnd})`),{value:'https://releases.example.invalid/my-draft/',focus:'__hx-update-feed',start:8,end:16});
      assert.match(await run(`document.querySelector('#__hx-update-status').textContent`),/42%/);
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
      await updater();await run(`__host.appData.launcherSettings.useMagnifiedMode=true;__fiveUI.openUpdatesDialog();`);await pause(30);
      const metrics=await run(`(() => {const s=document.querySelector('#__hx-screen'),f=s.querySelector('.__hx-foot');return {width:innerWidth,scroll:s.scrollWidth,footer:f.getBoundingClientRect().bottom,height:innerHeight,font:getComputedStyle(document.querySelector('#__hx-update-feed')).fontSize};})()`);
      assert.ok(metrics.scroll<=metrics.width+1);assert.ok(metrics.footer<=metrics.height+1);assert.equal(metrics.font,'20px');
      fs.writeFileSync(path.join(output,'updates-large-minimum.png'),(await win.webContents.capturePage()).toPNG());
    });
    fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({output,results},null,2));console.log(JSON.stringify({output,results},null,2));win.destroy();app.exit(results.some(r=>!r.ok)?1:0);
  }).catch(error=>{console.error(error.stack||error);app.exit(2);});
}
