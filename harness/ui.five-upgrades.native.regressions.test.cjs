// Official native Settings with pointer input and a disposable host overlay.
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict');
if(!process.versions.electron){
  const {test}=require('node:test'),{execFileSync}=require('node:child_process');
  test('native upgrade settings persist and Updates remains reachable',()=>{
    try{process.stdout.write(execFileSync(process.execPath,[path.join(__dirname,'../app/node_modules/electron/cli.js'),__filename],{encoding:'utf8',timeout:70000}));}
    catch(error){if(error.stdout)process.stdout.write(error.stdout);throw error;}
  });
}else{
  const {app,BrowserWindow}=require('electron'),{spawn}=require('node:child_process'),http=require('node:http'),net=require('node:net');
  const root=path.dirname(__dirname),temporary=fs.mkdtempSync(path.join(os.tmpdir(),'launcher-five-native-'));
  const output=process.env.FIVE_UPGRADES_NATIVE_OUTPUT||temporary;fs.mkdirSync(output,{recursive:true});app.setPath('userData',path.join(temporary,'electron'));
  app.disableHardwareAcceleration();for(const flag of ['disable-renderer-backgrounding','disable-background-timer-throttling'])app.commandLine.appendSwitch(flag);
  const state=path.join(temporary,'state'),game=path.join(temporary,'game'),data=path.join(temporary,'data');
  let win,host,origin;const pending=new Set(),report={temporary,output,checks:[],errors:[],blocked:[]},pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const run=code=>win.webContents.executeJavaScript(code);
  const deadline=setTimeout(()=>{report.fatal='native fixture deadline';finish(2);},65000);
  function request(route,body){return new Promise((resolve,reject)=>{const req=http.request(origin+route,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:{}},res=>{let raw='';res.on('data',chunk=>raw+=chunk);res.on('end',()=>{try{res.statusCode>=400?reject(Error(raw)):resolve(JSON.parse(raw));}catch(error){reject(error);}});});req.on('error',reject);req.end(body?JSON.stringify(body):null);});}
  async function ready(expr){for(let n=0;n<100;n++){if(await run(expr))return;await pause(60);}assert.fail('Native UI did not settle: '+expr);}
  async function quiet(){let since=null;for(let n=0;n<100;n++){if(pending.size)since=null;else if(since===null)since=Date.now();else if(Date.now()-since>=250)return;await pause(60);}assert.fail('Native requests did not settle');}
  async function load(){await quiet();await win.loadURL(origin+'/settings/general');await ready(`window.__entrySettled&&location.pathname==='/settings/general'&&!!document.querySelector('#__hx-updates')&&!!document.querySelector('#__hx-setting-crashAssistance-note')`);}
  async function pointer(selector){
    await run(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'center'});`);await pause(30);
    const point=await run(`(() => {const e=document.querySelector(${JSON.stringify(selector)}),r=e.getBoundingClientRect(),x=Math.round(r.left+Math.min(16,r.width/2)),y=Math.round(r.top+r.height/2),hit=document.elementFromPoint(x,y);return {x,y,hit:hit===e||e.contains(hit)};})()`);
    assert.equal(point.hit,true,'Native target is covered: '+selector);
    win.webContents.sendInputEvent({type:'mouseDown',button:'left',x:point.x,y:point.y,clickCount:1});win.webContents.sendInputEvent({type:'mouseUp',button:'left',x:point.x,y:point.y,clickCount:1});
  }
  async function check(name,action){try{await action();report.checks.push({name,ok:true});}catch(error){report.checks.push({name,ok:false,error:String(error.stack||error)});}}
  function finish(code){clearTimeout(deadline);fs.writeFileSync(path.join(output,'native-report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));if(win&&!win.isDestroyed())win.destroy();if(host)host.kill();app.exit(code);}
  app.whenReady().then(async()=>{try{
    for(const folder of [state,game,data,path.join(state,'versions/1.20.1')])fs.mkdirSync(folder,{recursive:true});
    fs.writeFileSync(path.join(state,'launcher_settings.json'),JSON.stringify({locale:'en-US',keepLauncherOpen:false,crashAssistance:false}));
    fs.writeFileSync(path.join(state,'launcher_profiles.json'),JSON.stringify({profiles:{fixture:{name:'Five upgrades native fixture',type:'custom',lastVersionId:'1.20.1'}}}));
    fs.writeFileSync(path.join(state,'versions/1.20.1/1.20.1.json'),JSON.stringify({id:'1.20.1',type:'release',mainClass:'NeverRunFixture',libraries:[],downloads:{client:{url:'https://example.invalid/never.jar',size:1}}}));
    fs.writeFileSync(path.join(state,'versions/version_manifest_v2.json'),JSON.stringify({latest:{release:'1.20.1',snapshot:'1.20.1'},versions:[]}));
    fs.writeFileSync(path.join(data,'state.json'),JSON.stringify({UiSettings:'{}',UiEvents:'{}'}));
    const port=await new Promise(resolve=>{const reserve=net.createServer();reserve.listen(0,'127.0.0.1',()=>{const value=reserve.address().port;reserve.close(()=>resolve(value));});});origin='http://127.0.0.1:'+port;
    host=spawn('python',['-B',path.join(__dirname,'server.py'),String(port)],{cwd:root,windowsHide:true,env:{...process.env,LAUNCHER_ROOT:root,LAUNCHER_STATE_DIR:state,LAUNCHER_GAME_DIR:game,LAUNCHER_DATA_DIR:data,LAUNCHER_NO_RELOAD:'1',LAUNCHER_MSA_CLIENT_ID:''},stdio:['ignore','pipe','pipe']});
    const log=fs.createWriteStream(path.join(output,'host.log'));host.stdout.pipe(log);host.stderr.pipe(log);
    for(let n=0;n<80;n++){try{await request('/__accounts');break;}catch(error){if(n===79)throw error;await pause(100);}}
    await request('/__accounts',{action:'login',name:'NativeUpgradeFixture'});
    win=new BrowserWindow({width:1024,height:640,useContentSize:true,show:false,webPreferences:{contextIsolation:true,nodeIntegration:false,sandbox:true,offscreen:true,backgroundThrottling:false,partition:'five-upgrades-native-memory'}});
    win.webContents.setWindowOpenHandler(()=>({action:'deny'}));win.webContents.session.setPermissionRequestHandler((_wc,_permission,callback)=>callback(false));win.webContents.session.on('will-download',event=>event.preventDefault());
    win.webContents.session.webRequest.onBeforeRequest((details,callback)=>{let allowed=false;try{const url=new URL(details.url);allowed=url.origin===origin||['data:','blob:','file:','devtools:','chrome:'].includes(url.protocol);if(url.origin===origin&&/^\/__(open|dialog|launch|install|msa\/signin|loaders\/materialize|modpacks\/(import|export))(\/|$)/.test(url.pathname)){report.blocked.push(url.pathname);allowed=false;}}catch{}if(allowed&&details.resourceType==='xhr')pending.add(details.id);callback({cancel:!allowed});});
    win.webContents.session.webRequest.onCompleted(details=>pending.delete(details.id));win.webContents.session.webRequest.onErrorOccurred(details=>pending.delete(details.id));win.webContents.on('console-message',details=>{if(details.level==='error'||details.level===3)report.errors.push(details.message);});
    await check('both native upgrade preferences save to configuration settings and survive reload',async()=>{
      await load();assert.deepEqual(await run(`[document.querySelector('#keepLauncherOpen').checked,document.querySelector('#crashAssistance').checked]`),[false,false]);
      for(const id of ['keepLauncherOpen','crashAssistance']){assert.equal(await run(`document.querySelector('#${id}').disabled`),false);await pointer(`label[for="${id}"]`);await ready(`__host.appData.configurationData.settings.${id}===true`);}
      await quiet();const overlay=JSON.parse(fs.readFileSync(path.join(data,'state.json'),'utf8'));assert.equal(overlay.__configuration_settings.keepLauncherOpen,true);assert.equal(overlay.__configuration_settings.crashAssistance,true);
      await load();assert.deepEqual(await run(`[document.querySelector('#keepLauncherOpen').checked,document.querySelector('#crashAssistance').checked]`),[true,true]);
      report.persisted=overlay.__configuration_settings;
    });
    await check('magnified preferences remain native pointer targets and can be disabled persistently',async()=>{
      await quiet();await request('/__launcher_settings',{useMagnifiedMode:true});await load();
      const layout=await run(`(() => {const note=document.querySelector('#__hx-setting-keepLauncherOpen-note'),entry=document.querySelector('#__hx-updates button');return {noteRight:note.getBoundingClientRect().right,width:innerWidth,color:getComputedStyle(entry).backgroundColor,font:getComputedStyle(entry).fontSize};})()`);
      assert.ok(layout.noteRight<=layout.width-8,'Background-game explanation extends past the viewport');
      assert.equal(layout.color,'rgb(208, 197, 192)','Updates needs the native secondary action color');assert.equal(layout.font,'20px');
      for(const id of ['keepLauncherOpen','crashAssistance']){await pointer(`label[for="${id}"]`);await ready(`__host.appData.configurationData.settings.${id}===false`);}
      await quiet();await load();assert.deepEqual(await run(`[document.querySelector('#keepLauncherOpen').checked,document.querySelector('#crashAssistance').checked]`),[false,false]);
      assert.match(await run(`document.querySelector('label[for="crashAssistance"]').textContent`),/local crash explanations/i);
      fs.writeFileSync(path.join(output,'native-settings-large-minimum.png'),(await win.webContents.capturePage()).toPNG());
    });
    await check('native Settings Updates entry opens the facade version and restores its keyboard focus',async()=>{
      await run(`window.launcherDesktop=Object.freeze({getAppInfo:async()=>({version:'3.41.30',packaged:true}),getUpdateState:async()=>({status:'setup',currentVersion:'3.41.30',feedUrl:'',installAllowed:false}),onUpdateState:()=>()=>{},configureUpdateFeed:async()=>({}),checkForUpdates:async()=>({}),restartAndInstall:async()=>({})});document.querySelector('#__hx-updates button').focus();`);
      await pointer('#__hx-updates button');await ready(`!!document.querySelector('#__hx-update-feed')&&document.querySelector('#__hx-update-version').textContent.includes('3.41.30')`);
      assert.equal(await run(`document.querySelector('#__hx-screen').scrollWidth<=innerWidth+1`),true);
      fs.writeFileSync(path.join(output,'native-updates-large-minimum.png'),(await win.webContents.capturePage()).toPNG());
      await pointer('#__hx-screen .__hx-close');await ready(`!document.querySelector('#__hx-screen')`);assert.equal(await run(`document.activeElement.closest('#__hx-updates')?.id`),'__hx-updates');
    });
    await check('native Java progress exposes pause and resume from owned download capabilities',async()=>{
      await quiet();await win.loadURL(origin+'/games/java/play');await ready(`window.__entrySettled&&!!window.__host`);
      await run(`webpackChunklauncher.push([[991122],{},r=>window.__nativeRequire=r]);void 0;`);
      await run(`window.__host.appData.downloads={current:{id:'fixture-download',jobId:'fixture-download',productId:'java',intent:'Install',state:'Downloading',progress:0.5,currentBytes:5,totalBytes:10,canPause:true,canResume:false,canCancel:true},pool:{}};__host.appData.downloads.pool['fixture-download']=__host.appData.downloads.current;window.__host.appData.gameInstances={instances:[{id:'fixture-download',productId:'java',downloadId:'fixture-download',state:'Downloading',localInstaller:true}]};window.__host.push('gameInstances');window.__host.push('downloads');`);
      try{await ready(`!!document.querySelector('button[aria-label="Pause download"]')`);}catch(error){report.downloadDebug=await run(`({instances:MCLauncher.getInstances(),nativeInstances:__nativeRequire(37688).Ay.common.getInstances(),enum:__nativeRequire(37688).er,eventNames:__host.eventNames,current:__host.appData.downloads.current,text:document.body.innerText})`);throw error;}
      assert.equal(await run(`document.querySelector('button[aria-label="Pause download"]').disabled`),false);
      await run(`window.__originalFetch=fetch;window.fetch=(url,options)=>String(url)==='/__install_control'?Promise.resolve({ok:true,json:async()=>({ok:false,error:'Cannot pause during commit'})}):__originalFetch(url,options);void 0;`);
      await pointer('button[aria-label="Pause download"]');await ready(`document.body.innerText.includes('Unable to pause download')`);
      assert.equal(await run(`document.body.innerText.includes('Successfully paused download')`),false);
      await run(`Object.assign(__host.appData.downloads.current,{state:'Paused',canPause:false,canResume:true});__host.push('downloads');`);
      await ready(`!!document.querySelector('button[aria-label="Resume download"]')`);
      assert.equal(await run(`document.querySelector('button[aria-label="Resume download"]').disabled`),false);
      await run(`Object.assign(__host.appData.downloads.current,{state:'Finalizing',canPause:false,canResume:false,canCancel:false,controlReason:'Applying verified files'});__host.push('downloads');`);
      await ready(`document.body.innerText.includes('Applying verified files')`);
      assert.equal(await run(`document.querySelector('button[aria-label="Pause download"]')`),null);
      const cancel=await run(`document.querySelector('button[aria-label="Cancel download"]')?.disabled`);assert.ok(cancel===true||cancel===undefined);
      await run(`__host.appData.gameInstances={instances:[]};__host.push('gameInstances');`);
    });
    assert.equal(report.errors.length,0,report.errors.join('\n'));assert.deepEqual(report.blocked,[]);finish(report.checks.some(c=>!c.ok)?1:0);
  }catch(error){report.fatal=String(error.stack||error);finish(2);}});
}
