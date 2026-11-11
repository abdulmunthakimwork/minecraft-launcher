// The native Java Play bar, disposable files and hidden offscreen Electron.
// Menus use real pointer/keyboard input; games, downloads and OS actions are blocked.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
if(!process.versions.electron){
  const {test}=require('node:test'),{execFileSync}=require('node:child_process');
  test('native installation selector stays clear of Play and keeps working across supported layouts',()=>{
    process.stdout.write(execFileSync(process.execPath,[path.join(__dirname,'../app/node_modules/electron/cli.js'),__filename],{encoding:'utf8',timeout:95000}));
  });
}else{
  const {app,BrowserWindow}=require('electron'),{spawn}=require('node:child_process');
  const os=require('node:os'),http=require('node:http'),net=require('node:net');
  const root=path.dirname(__dirname),temporary=fs.mkdtempSync(path.join(os.tmpdir(),'launcher-playbar-'));
  const output=process.env.PLAYBAR_TEST_OUTPUT||temporary;fs.mkdirSync(output,{recursive:true});
  app.setPath('userData',path.join(temporary,'electron'));app.disableHardwareAcceleration();
  app.on('window-all-closed',()=>{});
  for(const flag of ['disable-renderer-backgrounding','disable-background-timer-throttling','disable-backgrounding-occluded-windows'])app.commandLine.appendSwitch(flag);
  const report={checks:[],layouts:[],errors:[],effects:[],output,temporary};
  const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const pending=new Set();let host,win,origin,loaded=false;
  const run=code=>win.webContents.executeJavaScript(code);
  function request(route,body){return new Promise((resolve,reject)=>{
    const req=http.request(origin+route,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:{}},res=>{
      let raw='';res.on('data',chunk=>raw+=chunk);res.on('end',()=>{try{res.statusCode>=400?reject(Error(raw)):resolve(JSON.parse(raw));}catch(error){reject(error);}});
    });req.on('error',reject);req.end(body?JSON.stringify(body):null);
  });}
  async function ready(code){for(let i=0;i<100;i++){if(await run(code))return;await pause(75);}throw Error('Native Play bar did not settle: '+code);}
  async function idle(){let since;for(let i=0;i<150;i++){if(pending.size)since=null;else if(since==null)since=Date.now();else if(Date.now()-since>=300)return;await pause(75);}throw Error('Native requests did not settle');}
  async function load(magnified,quickPlay,width,height){
    if(loaded)await idle();await request('/__launcher_settings',{locale:'en-US',useMagnifiedMode:magnified,quickPlayEnabled:quickPlay});
    win.setContentSize(width,height);await win.loadURL(origin+'/games/java/play');loaded=true;
    await ready(`location.pathname==='/games/java/play'&&!!document.querySelector('[role="tab"][aria-selected="true"][href$="/play"]')&&!!document.querySelector('#installation-select-label')&&!!document.querySelector('#bigCTAButton')&&${quickPlay?'!!':'!'}document.querySelector('[aria-label="Edit quick play list"]')`);
    await idle();await run(`Promise.all([...document.images].filter(e=>e.getClientRects().length&&e.getAttribute('src')).map(e=>e.decode().catch(()=>{})))`);
    await pause(180);
  }
  function inspect(){
    const label=document.querySelector('#installation-select-label'),trigger=label.parentElement.querySelector('[role="button"][aria-controls="toggle-list-menu"]'),wrapper=trigger.parentElement,cta=document.querySelector('#bigCTAButton');
    let bar=wrapper;while(bar&&!bar.contains(cta))bar=bar.parentElement;
    const read=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height,font:s.fontSize,line:s.lineHeight,fontFamily:s.fontFamily,padding:s.padding,border:s.border,background:s.backgroundColor,inline:{width:e.style.width,minWidth:e.style.minWidth,maxWidth:e.style.maxWidth}};};
    const points=[8,cta.getBoundingClientRect().width/2,cta.getBoundingClientRect().width-8].map(offset=>{const r=cta.getBoundingClientRect(),x=r.left+offset,y=r.top+r.height/2,hit=document.elementFromPoint(x,y);return {x,y,cta:hit===cta||cta.contains(hit),tag:hit?.tagName,text:hit?.textContent?.slice(0,60)};});
    return {path:location.pathname,selectedTab:document.querySelector('[role="tab"][aria-selected="true"][href$="/play"]')?.id,viewport:[innerWidth,innerHeight],trigger:read(trigger),wrapper:read(wrapper),cta:read(cta),bar:read(bar),points,profile:trigger.innerText,wrapperMarkup:wrapper.outerHTML.slice(0,7000),quickPlay:!!document.querySelector('[aria-label="Edit quick play list"]'),documentWidth:document.documentElement.scrollWidth};
  }
  async function capture(name){await run('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');fs.writeFileSync(path.join(output,name+'.png'),(await win.webContents.capturePage()).toPNG());}
  async function pointer(selector){
    const point=await run(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});if(!e)return null;e.scrollIntoView({block:'nearest',inline:'nearest'});return true;})()`);assert.ok(point,'Missing '+selector);await pause(150);
    const hit=await run(`(()=>{const e=document.querySelector(${JSON.stringify(selector)}),r=e.getBoundingClientRect(),x=Math.round(r.left+r.width/2),y=Math.round(r.top+r.height/2),hit=document.elementFromPoint(x,y);return {x,y,hit:hit===e||e.contains(hit)};})()`);assert.equal(hit.hit,true,'Pointer target is covered: '+selector);
    win.webContents.sendInputEvent({type:'mouseMove',x:hit.x,y:hit.y});win.webContents.sendInputEvent({type:'mouseDown',button:'left',x:hit.x,y:hit.y,clickCount:1});win.webContents.sendInputEvent({type:'mouseUp',button:'left',x:hit.x,y:hit.y,clickCount:1});await pause(200);
  }
  async function press(keyCode){for(const type of ['keyDown','keyUp'])win.webContents.sendInputEvent({type,keyCode});await pause(180);}
  async function check(name,work){if(process.env.PLAYBAR_TEST_FILTER&&!name.includes(process.env.PLAYBAR_TEST_FILTER))return;try{await work();report.checks.push({name,passed:true});}catch(error){report.checks.push({name,passed:false,error:String(error.stack||error)});}}
  function assertLayout(layout){
    const vertical=layout.trigger.top<layout.cta.bottom&&layout.trigger.bottom>layout.cta.top;
    if(vertical)assert.ok(layout.trigger.right+8<=layout.cta.left+1,`Selector ${layout.trigger.right} overlaps Play ${layout.cta.left}`);
    assert.equal(layout.trigger.height,48);assert.equal(layout.cta.width,230);assert.ok(layout.documentWidth<=layout.viewport[0]+1);
    for(const point of layout.points)assert.equal(point.cta,true,'Play is covered at '+point.x+','+point.y);
  }
  app.whenReady().then(async()=>{try{
    const state=path.join(temporary,'state'),game=path.join(temporary,'game'),data=path.join(temporary,'data');
    for(const dir of [state,game,data,path.join(state,'versions/26.3'),path.join(game,'versions/26.3'),path.join(game,'runtime/jre-legacy/bin')])fs.mkdirSync(dir,{recursive:true});
    fs.writeFileSync(path.join(data,'state.json'),JSON.stringify({UiSettings:'{}',UiEvents:'{}'}));
    fs.writeFileSync(path.join(state,'launcher_settings.json'),JSON.stringify({locale:'en-US',quickPlayEnabled:true}));
    fs.writeFileSync(path.join(state,'launcher_profiles.json'),JSON.stringify({profiles:{
      'fixture-custom':{name:'Native Playbar Fixture With A Long Installation Name',type:'custom',lastVersionId:'26.3',lastUsed:'2026-11-11T00:00:00Z'},
      'fixture-release':{name:'Latest release',type:'latest-release',lastVersionId:'latest-release'}
    }}));
    fs.writeFileSync(path.join(state,'versions/26.3/26.3.json'),JSON.stringify({id:'26.3',type:'release',mainClass:'NeverExecuteFixture',libraries:[],downloads:{client:{url:'https://example.invalid/never-download.jar',size:1}}}));
    fs.writeFileSync(path.join(state,'versions/version_manifest_v2.json'),JSON.stringify({latest:{release:'26.3',snapshot:'26.3'},versions:[]}));
    fs.writeFileSync(path.join(game,'versions/26.3/26.3.jar'),'x');fs.writeFileSync(path.join(game,'runtime/jre-legacy/bin/java.exe'),'never-executed');
    fs.writeFileSync(path.join(game,'runtime/jre-legacy/manifest.json'),JSON.stringify({files:{'bin/java.exe':{type:'file',size:14}}}));
    const port=await new Promise(resolve=>{const reserve=net.createServer();reserve.listen(0,'127.0.0.1',()=>{const port=reserve.address().port;reserve.close(()=>resolve(port));});});origin='http://127.0.0.1:'+port;
    host=spawn('python',['-B',path.join(root,'harness/server.py'),String(port)],{cwd:root,windowsHide:true,env:{...process.env,LAUNCHER_ROOT:root,LAUNCHER_STATE_DIR:state,LAUNCHER_GAME_DIR:game,LAUNCHER_DATA_DIR:data,LAUNCHER_NO_RELOAD:'1',LAUNCHER_MSA_CLIENT_ID:''},stdio:['ignore','pipe','pipe']});
    const log=fs.createWriteStream(path.join(output,'host.log'));host.stdout.pipe(log);host.stderr.pipe(log);
    for(let i=0;i<80;i++){try{await request('/__accounts');break;}catch(error){if(i===79)throw error;await pause(100);}}await request('/__accounts',{action:'login',name:'PlaybarFixture'});
    win=new BrowserWindow({width:1280,height:720,useContentSize:true,frame:false,show:false,webPreferences:{contextIsolation:true,nodeIntegration:false,sandbox:true,offscreen:true,backgroundThrottling:false,partition:'native-playbar-memory'}});
    win.webContents.setWindowOpenHandler(()=>({action:'deny'}));win.webContents.session.setPermissionRequestHandler((_wc,_permission,cb)=>cb(false));win.webContents.session.on('will-download',event=>event.preventDefault());
    win.webContents.session.webRequest.onBeforeRequest((d,cb)=>{let allowed=false;try{const u=new URL(d.url);allowed=u.origin===origin||['data:','blob:','file:','devtools:','chrome:'].includes(u.protocol);if(u.origin===origin&&/^\/(?:__(?:open|dialog|read_file|launch|install|uninstall|pick_path|msa\/signin|loaders\/materialize)|__modpacks\/(?:import|export))(\/|$)/.test(u.pathname)){report.effects.push({path:u.pathname,method:d.method});allowed=false;}}catch{}if(allowed&&d.resourceType==='xhr')pending.add(d.id);cb({cancel:!allowed});});
    win.webContents.session.webRequest.onCompleted(d=>pending.delete(d.id));win.webContents.session.webRequest.onErrorOccurred(d=>pending.delete(d.id));win.webContents.on('console-message',d=>{if(d.level==='error'||d.level===3)report.errors.push(d.message);});
    for(const magnified of [false,true])for(const quickPlay of [false,true])for(const [width,height] of [[1280,720],[1024,640]])await check(`native Play controls ${magnified?'magnified':'normal'} ${quickPlay?'Quick Play on':'Quick Play off'} ${width}x${height}`,async()=>{
      await load(magnified,quickPlay,width,height);const layout=await run('('+inspect.toString()+')()');report.layouts.push({magnified,quickPlay,width,height,...layout});await capture((magnified?'magnified':'normal')+'-'+(quickPlay?'quick-on':'quick-off')+'-'+width+'x'+height);assertLayout(layout);
      const trigger='[role="button"][aria-controls="toggle-list-menu"]';await pointer(trigger);await ready(`!!document.querySelector('#toggle-list-menu')`);
      report.layouts.at(-1).menu=await run(`(()=>{const list=document.querySelector('#toggle-list-menu'),scroll=list.closest('.scrollbars-default');return {width:scroll.getBoundingClientRect().width,options:[...list.querySelectorAll('button')].map(e=>e.getAttribute('aria-label'))};})()`);await capture((magnified?'magnified':'normal')+'-'+(quickPlay?'quick-on':'quick-off')+'-'+width+'x'+height+'-menu');
      assert.equal(report.layouts.at(-1).menu.width,280,'Native installation menu width changed');
      await pointer('#toggle-list-menu button[aria-label="Latest release"]');await ready(`!document.querySelector('#toggle-list-menu')&&document.querySelector('${trigger}').innerText.includes('Latest release')`);await idle();assert.match(await run(`document.querySelector('${trigger}').innerText`),/Latest release/);
      await run(`document.querySelector('${trigger}').focus()`);await press('Space');await ready(`!!document.querySelector('#toggle-list-menu')`);await press('Escape');await ready(`!document.querySelector('#toggle-list-menu')`);assert.equal(await run(`document.activeElement===document.querySelector('${trigger}')`),true);
    });
    await check('resizing a live magnified Quick Play bar restores exact native inline widths without reflow loops',async()=>{
      await load(true,true,1280,720);const wide=await run('('+inspect.toString()+')()');assert.equal(wide.wrapper.width,280);assert.deepEqual(wide.wrapper.inline,{width:'',minWidth:'',maxWidth:''});
      await run(`window.__playbarMutations=0;window.__playbarMutationObserver=new MutationObserver(entries=>__playbarMutations+=entries.length);__playbarMutationObserver.observe(document.querySelector('[role="button"][aria-controls="toggle-list-menu"]').parentElement,{attributes:true,attributeFilter:['style']});`);
      win.setContentSize(1024,640);await pause(500);const narrow=await run('('+inspect.toString()+')()');assertLayout(narrow);assert.ok(narrow.wrapper.width<280);
      win.setContentSize(1280,720);await pause(500);const restored=await run('('+inspect.toString()+')()');assert.equal(restored.wrapper.width,280);assert.deepEqual(restored.wrapper.inline,wide.wrapper.inline);
      const before=await run('__playbarMutations');await pause(700);const after=await run('__playbarMutations');assert.equal(after,before,'Responsive width keeps mutating after geometry settles');report.resize={wide,narrow,restored,mutations:after};
    });
    await check('live Quick Play toggles restore and reapply selector width without a page reload',async()=>{
      await load(false,true,1024,640);const on=await run('('+inspect.toString()+')()');assertLayout(on);assert.ok(on.wrapper.width<280);
      await run(`window.__livePlayWrapper=document.querySelector('[role="button"][aria-controls="toggle-list-menu"]').parentElement;MCLauncher.setQuickPlayEnabled(false)`);
      await ready(`!document.querySelector('[aria-label="Edit quick play list"]')`);await pause(300);const off=await run('('+inspect.toString()+')()');assertLayout(off);assert.equal(off.wrapper.width,280);assert.deepEqual(off.wrapper.inline,{width:'',minWidth:'',maxWidth:''});
      await run(`MCLauncher.setQuickPlayEnabled(true)`);await ready(`!!document.querySelector('[aria-label="Edit quick play list"]')`);await pause(300);const again=await run('('+inspect.toString()+')()');assertLayout(again);assert.equal(again.wrapper.width,on.wrapper.width);assert.equal(await run(`__livePlayWrapper===document.querySelector('[role="button"][aria-controls="toggle-list-menu"]').parentElement`),true);report.liveQuickPlay={on,off,again};
    });
    await idle();assert.deepEqual(report.errors,[]);assert.deepEqual(report.effects,[]);
  }catch(error){report.fatal=String(error.stack||error);}finally{
    fs.writeFileSync(path.join(output,'playbar-report.json'),JSON.stringify(report,null,2));if(win)win.destroy();if(host)host.kill();console.log(JSON.stringify({checks:report.checks,errors:report.errors,effects:report.effects,fatal:report.fatal,output},null,2));app.exit(report.fatal||report.checks.some(check=>!check.passed)?1:0);
  }});
}
