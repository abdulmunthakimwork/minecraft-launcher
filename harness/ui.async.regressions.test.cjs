// Deferred responses against the official UI in one disposable offscreen window.
// Imports, exports, account writes, downloads and operating-system actions are intercepted.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');

if(!process.versions.electron){
  const {test}=require('node:test'),{execFileSync}=require('node:child_process');
  test('native custom dialogs reject obsolete responses and preserve active work',()=>{
    process.stdout.write(execFileSync(process.execPath,
      [path.join(__dirname,'../app/node_modules/electron/cli.js'),__filename],
      {encoding:'utf8',timeout:120000}));
  });
}else{
  const {app,BrowserWindow}=require('electron'),{spawn}=require('node:child_process');
  const os=require('node:os'),http=require('node:http'),net=require('node:net');
  const root=path.dirname(__dirname),temporary=fs.mkdtempSync(path.join(os.tmpdir(),'launcher-ui-async-'));
  const output=process.env.ASYNC_TEST_OUTPUT||temporary;
  fs.mkdirSync(output,{recursive:true});app.setPath('userData',path.join(temporary,'electron'));
  app.disableHardwareAcceleration();
  for(const flag of ['disable-renderer-backgrounding','disable-background-timer-throttling'])app.commandLine.appendSwitch(flag);
  let host,win,origin,hasLoaded=false,guardNavigation=false;
  const network=new Set(),report={checks:[],consoleErrors:[],blockedEffects:[],unexpectedNavigation:[],output,temporary};
  const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const run=code=>win.webContents.executeJavaScript(code);
  const evaluate=(fn,...args)=>run('('+fn.toString()+')('+args.map(value=>JSON.stringify(value)).join(',')+');');
  const deadline=setTimeout(()=>{report.fatal='async fixture deadline';finish(2);},110000);

  function request(route,body){return new Promise((resolve,reject)=>{
    const req=http.request(origin+route,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:{}},response=>{
      let raw='';response.on('data',chunk=>raw+=chunk);response.on('end',()=>{
        try{const value=JSON.parse(raw);response.statusCode>=400?reject(Error(raw)):resolve(value);}catch(error){reject(error);}
      });
    });req.on('error',reject);req.end(body?JSON.stringify(body):null);
  });}
  async function ready(code){
    for(let attempt=0;attempt<100;attempt++){if(await run(code))return;await pause(100);}
    throw Error('Native async fixture did not settle: '+code);
  }
  async function networkSettled(){
    let idle=null;
    for(let attempt=0;attempt<150;attempt++){
      if(network.size)idle=null;else if(idle===null)idle=Date.now();else if(Date.now()-idle>=300)return;
      await pause(100);
    }
    throw Error('Native page requests did not settle');
  }
  async function cleanup(){
    if(!hasLoaded)return;
    await evaluate(()=>{
      const close=document.querySelector('#__hx-screen .__hx-close');if(close)close.click();
      if(window.__asyncFixture)for(const item of window.__asyncFixture.requests){
        if(!item.settled){
          if(item.kind==='offline'||item.kind==='msa')item.reject('Fixture cleaned up');
          else item.respond(item.kind==='search'?{hits:[],total:0}:{job:null,phase:'idle'});
        }
      }
    });
    await pause(30);
  }
  async function prepare(route='/games/java/configurations'){
    guardNavigation=false;await cleanup();
    const marker=route.includes('configurations')?'#__hx-modpacks':'#__hx-add-offline';
    const same=hasLoaded&&await run('location.pathname==='+JSON.stringify(route)+' && !!document.querySelector('+JSON.stringify(marker)+')');
    if(!same){
      if(hasLoaded)await networkSettled();
      await win.loadURL(origin+route);hasLoaded=true;
    }
    await ready('window.__entrySettled && location.pathname==='+JSON.stringify(route)+' && !!document.querySelector('+JSON.stringify(marker)+')');
    if(route.includes('configurations')){
      await ready('!!document.querySelector("main li[data-configuration-id=\\"async-fixture\\"] button[id=\\"MoreOptionsDropdown\\"]")');
    }
    await evaluate(()=>{
      const nativeFetch=window.__asyncNativeFetch||(window.__asyncNativeFetch=window.fetch.bind(window));
      const state={requests:[],effects:[],next:1};window.__asyncFixture=state;
      function deferred(kind,url,body,response=true){
        return new Promise((resolve,reject)=>{
          const item={id:state.next++,kind,url,body,settled:false};
          item.respond=payload=>{
            if(item.settled)return;item.settled=true;
            resolve(response?{ok:true,json:async()=>payload}:payload);
          };
          item.reject=message=>{if(item.settled)return;item.settled=true;reject(Error(message));};
          state.requests.push(item);
        });
      }
      window.fetch=(input,options)=>{
        const url=typeof input==='string'?input:(input&&input.url)||String(input);
        const pathname=new URL(url,location.origin).pathname;
        // These cases defer authentication completion, after a configured app
        // has enabled sign-in. Configuration itself is a read-only fixture.
        if(pathname==='/__msa/config')return Promise.resolve({ok:true,json:async()=>({
          configured:true,clientId:'11111111-2222-4333-8444-555555555555',source:'file',environmentOverride:false})});
        if(pathname==='/__modpacks/search')return deferred('search',url,null);
        if(pathname==='/__modpacks/status')return deferred('status',url,null);
        if(pathname==='/__modpacks/import'||pathname==='/__modpacks/export'){
          state.effects.push({path:pathname,body:JSON.parse(options.body)});
          return Promise.resolve({ok:true,json:async()=>({ok:true,started:true})});
        }
        return nativeFetch(input,options);
      };
      window.__harness.accountAction=(action,extra)=>deferred('offline','accountAction',{action,extra},false);
      window.__harness.microsoftSignIn=()=>deferred('msa','microsoftSignIn',null,false);
    });
  }
  async function queued(kind,pending=false){
    return evaluate((kind,pending)=>window.__asyncFixture.requests
      .filter(item=>item.kind===kind&&(!pending||!item.settled))
      .map(item=>({id:item.id,kind:item.kind,url:item.url,settled:item.settled})),kind,pending);
  }
  async function waitQueued(kind,count=1,pending=true){
    await ready('window.__asyncFixture.requests.filter(item=>item.kind==='+JSON.stringify(kind)+(pending?' && !item.settled':'')+').length>='+count);
    return queued(kind,pending);
  }
  async function respond(id,payload){
    await evaluate((id,payload)=>window.__asyncFixture.requests.find(item=>item.id===id).respond(payload),id,payload);
    await pause(20);
  }
  async function reject(id,message){
    await evaluate((id,message)=>window.__asyncFixture.requests.find(item=>item.id===id).reject(message),id,message);
    await pause(20);
  }
  const hits=[
    {id:'pack-a',title:'Pack A',author:'Fixture Author',loader:'fabric-loader',downloads:1},
    {id:'pack-b',title:'Pack B',author:'Fixture Author',loader:'fabric-loader',downloads:2}
  ];
  const imported={job:null,phase:'done',result:{name:'Pack A',files:1,bytes:10}};
  const exported={job:null,phase:'done',result:{path:'C:\\fixture\\export-a.mrpack',size:10,files:1,bundled:0}};
  async function openImport(total=2,resolveResume=true){
    await prepare();
    await evaluate(()=>document.querySelector('#__hx-modpacks').click());
    const searches=await waitQueued('search'),statuses=await waitQueued('status');
    await respond(searches[0].id,{hits,total});
    if(resolveResume)await respond(statuses[0].id,{job:null,phase:'idle'});
    await ready('document.querySelectorAll("#__hx-mod-results li").length===2');
    return {resume:statuses[0].id};
  }
  async function startImport(){
    await evaluate(()=>document.querySelector('#__hx-mod-results [data-hx-modpack-id="pack-a"] button').click());
    await waitQueued('status');
  }
  async function openExport(){
    await prepare();
    await evaluate(()=>document.querySelector('main li[data-configuration-id="async-fixture"] button[id="MoreOptionsDropdown"]').click());
    await ready('!!document.querySelector("#__hx-export-item")');
    await evaluate(()=>document.querySelector('#__hx-export-item').click());
    await ready('!!document.querySelector("#__hx-export-status")');
    const recovery=(await waitQueued('status'))[0];
    await respond(recovery.id,{job:null,phase:'idle'});
    await evaluate(()=>document.querySelector('#__hx-screen .__hx-primary').click());
    await waitQueued('status');
  }
  async function status(selector){return evaluate(selector=>document.querySelector(selector).textContent,selector);}
  async function terminalVisible(kind){
    const selector=kind==='import'?'#__hx-mod-status':'#__hx-export-status';
    await ready('document.querySelector('+JSON.stringify(selector)+').textContent.includes('+JSON.stringify(kind==='import'?'Imported Pack A':'Saved C:\\fixture\\export-a.mrpack')+')');
  }
  async function check(name,action){
    if(process.env.ASYNC_TEST_FILTER&&!name.includes(process.env.ASYNC_TEST_FILTER))return;
    try{const details=await action();report.checks.push({name,passed:true,...(details?{details}:{})});}
    catch(error){report.checks.push({name,passed:false,error:String(error.stack||error)});}
  }
  async function openAccount(kind,name){
    await evaluate(kind=>document.querySelector(kind==='offline'?'#__hx-add-offline':'[data-hx-online-bound]').click(),kind);
    await ready('!!document.querySelector("#__hx-screen .__hx-primary")');
    if(kind==='msa')await ready('!document.querySelector("#__hx-online-go").disabled');
    if(kind==='offline')await evaluate(name=>{const input=document.querySelector('#__hx_offline_name');input.value=name;input.dispatchEvent(new Event('input',{bubbles:true}));},name);
    await evaluate(()=>document.querySelector('#__hx-screen .__hx-primary').click());
    return (await waitQueued(kind))[0].id;
  }
  async function replacement(){
    await evaluate(()=>{
      const close=document.querySelector('#__hx-screen .__hx-close');if(close)close.click();
      document.querySelector('#__hx-add-offline').click();
      const input=document.querySelector('#__hx_offline_name');input.value='ReplacementUser';
      input.dispatchEvent(new Event('input',{bubbles:true}));
      window.__asyncReplacement=document.querySelector('#__hx-screen');
    });
  }
  async function assertReplacement(before){
    await pause(750);
    assert.equal(report.unexpectedNavigation.length,before,'Obsolete account completion attempted a document reload');
    const state=await evaluate(()=>({
      same:document.querySelector('#__hx-screen')===window.__asyncReplacement,
      name:document.querySelector('#__hx_offline_name')&&document.querySelector('#__hx_offline_name').value,
      heading:document.querySelector('#__hx-screen h2')&&document.querySelector('#__hx-screen h2').textContent
    }));
    assert.deepEqual(state,{same:true,name:'ReplacementUser',heading:'Add offline account'});
  }
  function finish(code){
    clearTimeout(deadline);report.finishedAt=new Date().toISOString();
    fs.writeFileSync(path.join(output,'async-report.json'),JSON.stringify(report,null,2));
    if(win&&!win.isDestroyed())win.destroy();if(host)host.kill();
    console.log(JSON.stringify(report,null,2));app.exit(code);
  }

  app.whenReady().then(async()=>{
    try{
      const state=path.join(temporary,'state'),game=path.join(temporary,'game'),data=path.join(temporary,'data');
      for(const folder of [state,game,data,path.join(state,'versions/26.3')])fs.mkdirSync(folder,{recursive:true});
      fs.writeFileSync(path.join(data,'state.json'),JSON.stringify({UiSettings:JSON.stringify({lastVisitedPage:'java'}),UiEvents:'{}'}));
      fs.writeFileSync(path.join(state,'launcher_profiles.json'),JSON.stringify({profiles:{
        'async-fixture':{name:'Async Native Fixture',type:'custom',lastVersionId:'26.3',lastUsed:'2026-11-11T00:00:00.000Z'}
      }}));
      fs.writeFileSync(path.join(state,'versions/26.3/26.3.json'),JSON.stringify({id:'26.3',type:'release',mainClass:'NeverExecuteFixture',libraries:[],downloads:{client:{url:'https://example.invalid/never-download.jar',size:1}}}));
      fs.writeFileSync(path.join(state,'versions/version_manifest_v2.json'),JSON.stringify({latest:{release:'26.3',snapshot:'26.3'},versions:[]}));
      const port=await new Promise(resolve=>{const socket=net.createServer();socket.listen(0,'127.0.0.1',()=>{const port=socket.address().port;socket.close(()=>resolve(port));});});
      origin='http://127.0.0.1:'+port;
      host=spawn('python',['-B',path.join(__dirname,'server.py'),String(port)],{cwd:root,windowsHide:true,
        env:{...process.env,LAUNCHER_ROOT:root,LAUNCHER_STATE_DIR:state,LAUNCHER_GAME_DIR:game,LAUNCHER_DATA_DIR:data,LAUNCHER_NO_RELOAD:'1',LAUNCHER_MSA_CLIENT_ID:''},stdio:['ignore','pipe','pipe']});
      const log=fs.createWriteStream(path.join(output,'host.log'));host.stdout.pipe(log);host.stderr.pipe(log);
      for(let attempt=0;attempt<80;attempt++){try{await request('/__accounts');break;}catch(error){if(attempt===79)throw error;await pause(100);}}
      await request('/__accounts',{action:'login',name:'AsyncFixture'});
      await request('/__launcher_settings',{locale:'en-US',useMagnifiedMode:false});
      win=new BrowserWindow({width:1280,height:800,show:false,webPreferences:{
        contextIsolation:true,nodeIntegration:false,sandbox:true,offscreen:true,backgroundThrottling:false,partition:'async-fixture-memory'
      }});
      win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
      win.webContents.session.setPermissionRequestHandler((_contents,_permission,callback)=>callback(false));
      win.webContents.session.on('will-download',event=>event.preventDefault());
      win.webContents.session.webRequest.onBeforeRequest((details,callback)=>{
        let allowed=false;try{
          const url=new URL(details.url);allowed=url.origin===origin||['data:','blob:','file:','devtools:','chrome:'].includes(url.protocol);
          if(url.origin===origin&&details.resourceType==='mainFrame'&&guardNavigation){
            report.unexpectedNavigation.push({url:details.url});allowed=false;
          }
          if(url.origin===origin&&(/^\/__(?:launch|install|uninstall|open|dialog|msa\/signin|loaders\/materialize|modpacks\/(?:import|export))(?:\/|$)/.test(url.pathname)||url.pathname==='/__accounts'&&details.method==='POST')){
            report.blockedEffects.push({path:url.pathname,method:details.method});allowed=false;
          }
        }catch{}
        if(allowed&&details.resourceType==='xhr')network.add(details.id);
        callback({cancel:!allowed});
      });
      win.webContents.session.webRequest.onCompleted(details=>network.delete(details.id));
      win.webContents.session.webRequest.onErrorOccurred(details=>network.delete(details.id));
      win.webContents.on('console-message',details=>{if(details.level==='error'||details.level===3)report.consoleErrors.push(details.message);});

      for(const kind of ['import','export'])await check(kind+' permits one outstanding poll and cannot regress terminal success',async()=>{
        if(kind==='import'){await openImport();await startImport();}else await openExport();
        await pause(900); // More than two native 400ms polling periods.
        const pending=await queued('status',true);assert.ok(pending.length,'No status poll was started');
        const latest=pending[pending.length-1];
        await respond(latest.id,kind==='import'?imported:exported);await terminalVisible(kind);
        for(const item of pending.slice(0,-1))await respond(item.id,{job:kind,message:'OBSOLETE '+kind.toUpperCase()+' PROGRESS',done:1,total:99});
        assert.equal(pending.length,1,'More than one unresolved '+kind+' status request');
        assert.match(await status(kind==='import'?'#__hx-mod-status':'#__hx-export-status'),kind==='import'?/^Imported Pack A/:/^Saved C:\\fixture\\export-a\.mrpack/);
        return {outstanding:pending.length};
      });

      await check('an obsolete initial resume response cannot revive a completed user import',async()=>{
        const opened=await openImport(2,false);await startImport();await pause(50);
        let active=(await queued('status',true)).filter(item=>item.id!==opened.resume);
        const sharedFlight=!active.length;
        if(sharedFlight){
          await respond(opened.resume,{job:'import',message:'OBSOLETE RESUME',done:1,total:99});
          await ready('window.__asyncFixture.requests.some(item=>item.kind==="status" && !item.settled && item.id!=='+opened.resume+')');
          active=(await queued('status',true)).filter(item=>item.id!==opened.resume);
        }
        await respond(active[active.length-1].id,imported);await terminalVisible('import');
        if(!sharedFlight)await respond(opened.resume,{job:'import',message:'OBSOLETE RESUME',done:1,total:99});
        await pause(50);
        assert.match(await status('#__hx-mod-status'),/^Imported Pack A/);
        assert.equal((await queued('status',true)).length,0,'Obsolete resume started new polling after completion');
        assert.equal(await evaluate(()=>document.querySelector('#__hx-import-background').hidden),true);
        return {sharedFlight};
      });

      await check('an obsolete search failure cannot unlock pagination during the current query',async()=>{
        await prepare();await evaluate(()=>document.querySelector('#__hx-modpacks').click());
        const old=(await waitQueued('search'))[0];const resume=(await waitQueued('status'))[0];await respond(resume.id,{job:null,phase:'idle'});
        await evaluate(()=>{
          const input=document.querySelector('#__hx-modpack-search');input.value='Current query';
          input.dispatchEvent(new Event('input',{bubbles:true}));
          input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));
        });
        await waitQueued('search',2,false);await reject(old.id,'Obsolete query failure');
        await evaluate(()=>document.querySelector('#__hx-screen .__hx-body').dispatchEvent(new Event('scroll')));
        await pause(50);const searches=await queued('search');
        for(const item of searches.filter(item=>!item.settled))await respond(item.id,{hits:[hits[1]],total:1});
        assert.equal(searches.length,2,'A stale failure cleared the active query loading guard');
        await ready('!!document.querySelector("#__hx-mod-results [data-hx-modpack-id=\\"pack-b\\"]")');
        assert.doesNotMatch(await status('#__hx-mod-status'),/Obsolete query failure/);
      });

      await check('late pagination cannot overwrite an import completion or append stale rows',async()=>{
        await openImport(100);const page=(await waitQueued('search'))[0];await startImport();
        const poll=(await queued('status',true))[0];await respond(poll.id,imported);await terminalVisible('import');
        await respond(page.id,{hits:[{id:'late-result',title:'Late stale result',author:'Fixture'}],total:3});
        assert.match(await status('#__hx-mod-status'),/^Imported Pack A/);
        assert.equal(await evaluate(()=>!!document.querySelector('#__hx-mod-results [data-hx-modpack-id="late-result"]')),false);
      });

      await check('active import preserves its progress and rejects a new search',async()=>{
        await openImport();await startImport();const before=(await queued('search')).length;
        const frozen=await evaluate(()=>{
          const input=document.querySelector('#__hx-modpack-search');
          if(input.disabled||input.readOnly)return true;
          input.value='Another query';input.dispatchEvent(new Event('input',{bubbles:true}));
          input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));return false;
        });
        await pause(50);const count=(await queued('search')).length;
        const poll=(await queued('status',true))[0];await respond(poll.id,imported);await terminalVisible('import');
        assert.equal(count,before,'A new search replaced the active import status and result rows');
        return {frozen};
      });

      await check('sequential completed imports retain disabled rows and cannot submit again',async()=>{
        await openImport();await startImport();
        await respond((await queued('status',true))[0].id,imported);await terminalVisible('import');
        await evaluate(()=>document.querySelector('#__hx-mod-results [data-hx-modpack-id="pack-b"] button').click());
        const second=(await waitQueued('status'))[0];
        await respond(second.id,{job:null,phase:'done',result:{name:'Pack B',files:1,bytes:10}});
        await ready('document.querySelector("#__hx-mod-status").textContent.startsWith("Imported Pack B")');
        const rows=await evaluate(()=>Array.from(document.querySelectorAll('#__hx-mod-results li'))
          .map(row=>({id:row.dataset.hxModpackId,label:row.querySelector('button').innerText.trim(),disabled:row.querySelector('button').disabled}))
          .sort((a,b)=>a.id.localeCompare(b.id)));
        await evaluate(()=>document.querySelector('#__hx-mod-results [data-hx-modpack-id="pack-a"] button').click());
        await pause(20);
        const imports=await evaluate(()=>window.__asyncFixture.effects.filter(item=>item.path==='/__modpacks/import').length);
        assert.deepEqual({rows,imports},{rows:[
          {id:'pack-a',label:'Imported',disabled:true},
          {id:'pack-b',label:'Imported',disabled:true}
        ],imports:2},'Completing B must preserve the completed state of A and prevent another request');
      });

      for(const kind of ['offline','msa'])for(const timing of ['closed-before-completion','replacement-after-success']){
        await check(kind+' ignores reload work from '+timing,async()=>{
          await prepare('/settings/accounts');guardNavigation=true;
          const before=report.unexpectedNavigation.length,name=kind==='offline'?'ObsoleteOffline':'ObsoleteMicrosoft';
          const id=await openAccount(kind,name);
          if(timing==='closed-before-completion')await replacement();
          await respond(id,{id:'obsolete-account',javaProfileName:name,username:name});
          if(timing==='replacement-after-success'){
            await ready('!document.querySelector("#__hx-screen")');await replacement();
          }
          await assertReplacement(before);
        });
      }
      guardNavigation=false;await cleanup();await networkSettled();
      assert.deepEqual(report.blockedEffects,[],'Unexpected physical effects');
      assert.deepEqual(report.consoleErrors,[],'Unexpected renderer errors');
      assert.deepEqual(report.unexpectedNavigation,[],'Obsolete dialog work attempted navigation');
    }catch(error){report.fatal=String(error.stack||error);}
    finish(report.fatal||report.checks.some(item=>!item.passed)?1:0);
  });
}
