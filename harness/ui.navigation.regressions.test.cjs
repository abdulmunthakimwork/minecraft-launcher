// Real Chromium layout checks against an isolated host and the official UI.
// No desktop, game, download, file-picker or real account state is touched.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

if (!process.versions.electron) {
  const {test} = require('node:test');
  const {execFileSync} = require('node:child_process');
  test('native notices and tabs stay readable and reachable in larger text', () => {
    const cli = path.join(__dirname, '../app/node_modules/electron/cli.js');
    process.stdout.write(execFileSync(process.execPath, [cli, __filename], {encoding:'utf8', timeout:120000}));
  });
} else {
  const {app, BrowserWindow} = require('electron');
  const {spawn} = require('node:child_process');
  const http = require('node:http');
  const net = require('node:net');
  const root = path.dirname(__dirname);
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'launcher-navigation-'));
  const output = process.env.LAUNCHER_LAYOUT_OUTPUT || temporary;
  fs.mkdirSync(output, {recursive:true});
  app.setPath('userData', path.join(temporary, 'electron'));
  app.disableHardwareAcceleration();
  app.commandLine.appendSwitch('disable-renderer-backgrounding');
  app.commandLine.appendSwitch('disable-background-timer-throttling');
  let host, win, origin, hasLoaded=false;
  const pendingRequests = new Set();
  const report = {startedAt:new Date().toISOString(), checks:[], native:[], consoleErrors:[], requestErrors:[], blockedEffects:[], output};
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
  async function requestsSettled() {
    let idleSince = null;
    for(let i=0;i<150;i++) {
      if(pendingRequests.size)idleSince=null;
      else if(idleSince===null)idleSince=Date.now();
      else if(Date.now()-idleSince>=300)return;
      await pause(100);
    }
    throw new Error('Page requests did not settle before fixture navigation');
  }
  async function load(route) {
    if(hasLoaded&&await run(`location.pathname===${JSON.stringify(route)} && window.__entrySettled`))return;
    await requestsSettled();
    await win.loadURL(origin+route);
    hasLoaded=true;
    await ready(`window.__entrySettled && location.pathname===${JSON.stringify(route)}`);
    await pause(250);
  }
  async function check(name, fn) {
    try {await fn();report.checks.push({name,ok:true});}
    catch(error){failures.push(name);report.checks.push({name,ok:false,error:String(error.stack||error)});}
  }
  async function capture(label) {
    await run(`Promise.all([...document.querySelectorAll('img')].filter(e=>e.getClientRects().length&&e.complete&&e.naturalWidth).map(e=>e.decode().catch(()=>{})))`);
    await run('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
    // Hidden offscreen captures need a fresh paint after image decode.
    win.webContents.invalidate();await pause(150);
    const img=await win.webContents.capturePage();
    fs.writeFileSync(path.join(output,label+'.png'),img.toPNG());
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
      // The bridge seeds direct routes from an existing native UiSettings
      // blob. Empty fixture state cannot exercise that route seed on first run.
      fs.writeFileSync(path.join(data,'state.json'),JSON.stringify({
        UiSettings:JSON.stringify({lastVisitedPage:'bedrock'}),UiEvents:'{}'
      }));
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
      win.webContents.session.webRequest.onErrorOccurred(details=>{pendingRequests.delete(details.id);report.requestErrors.push({url:details.url,error:details.error});});
      win.webContents.on('console-message',details=>{if(details.level==='error'||details.level===3)report.consoleErrors.push({page:win.webContents.getURL(),message:details.message});});
      for(const magnified of [false,true]) {
        // Reset only the native gift-hint dismissal. Keep UiSettings, whose
        // lastVisitedPage is needed for the host's native route seed.
        // Start directly on the page under test. Loading Home first could
        // abort its delayed UiEvents write when a cold renderer was still
        // mounting. Require a quiet request interval before later reloads.
        await requestsSettled();
        await request('/__state/UiEvents',{value:'{}'});
        await request('/__launcher_settings',{locale:'en-US',useMagnifiedMode:magnified});
        win.setContentSize(1024,640);
        await load('/games/bedrock/play');await ready(`!!document.querySelector('a[aria-label="Download new Minecraft Launcher"]')`);
        await check((magnified?'large':'normal')+' compatibility notice uses readable full-width prose',async()=>{
          const metrics=await run(`(() => {
            const link=document.querySelector('a[aria-label="Download new Minecraft Launcher"]'),notice=link.closest('[role="alert"]');
            const range=document.createRange();range.selectNodeContents(notice.firstChild);
            const lines=[...range.getClientRects()];const s=getComputedStyle(notice);
            return {lineCount:lines.length,messageWidth:Math.max(...lines.map(r=>r.width)),available:notice.clientWidth-32,font:s.fontSize,line:s.lineHeight};
          })()`);
          report.native.push({magnified,notice:metrics});await capture('bedrock-notice-'+(magnified?'large':'normal'));
          assert.ok(metrics.lineCount<=3,'Message is squeezed into '+metrics.lineCount+' short lines');
          assert.ok(metrics.messageWidth>metrics.available*0.6,'Message is squeezed beside its link');
          assert.equal(metrics.font,magnified?'24px':'12px');
        });
        await check((magnified?'large':'normal')+' native tab strip fits the header and exposes horizontal scrolling',async()=>{
          const metrics=await run(`(() => {
            const list=document.querySelector('[role="tablist"][id$="-linkwrap"]');
            let header=list;while(header&&getComputedStyle(header).position!=='sticky')header=header.parentElement;
            const r=list.getBoundingClientRect(),h=header.getBoundingClientRect();
            return {bottom:r.bottom,headerBottom:h.bottom,width:[list.clientWidth,list.scrollWidth],scrollbar:getComputedStyle(list,'::-webkit-scrollbar').display};
          })()`);
          assert.ok(metrics.bottom<=metrics.headerBottom+1,'Tabs extend below their header');
          if(metrics.width[1]>metrics.width[0])assert.notEqual(metrics.scrollbar,'none','Hidden tabs need a visible scroll affordance');
        });
        await check((magnified?'large':'normal')+' notification Close receives a real pointer click',async()=>{
          await ready(`[...document.querySelectorAll('[aria-label="Close notification"]')].some(e=>e.getClientRects().length)`);
          const point=await run(`(() => {
            const e=[...document.querySelectorAll('[aria-label="Close notification"]')].find(e=>e.getClientRects().length);
            const r=e.getBoundingClientRect(),x=Math.round(r.left+r.width/2),y=Math.round(r.top+r.height/2),top=document.elementFromPoint(x,y);
            e.setAttribute('data-navigation-close','true');return {x,y,hit:top===e||e.contains(top)};
          })()`);
          assert.equal(point.hit,true,'Notification Close is covered');
          win.webContents.sendInputEvent({type:'mouseDown',button:'left',x:point.x,y:point.y,clickCount:1});
          win.webContents.sendInputEvent({type:'mouseUp',button:'left',x:point.x,y:point.y,clickCount:1});
          await ready(`!document.querySelector('[data-navigation-close]')`);
        });
        await check((magnified?'large':'normal')+' manual tab scroll survives scans and last tab receives a pointer click',async()=>{
          const before=await run(`(() => {
            const tabs=document.querySelector('[role="tablist"][id$="-linkwrap"]');tabs.scrollLeft=tabs.scrollWidth;
            const marker=document.createElement('span');marker.hidden=true;document.body.appendChild(marker);marker.remove();return tabs.scrollLeft;
          })()`);
          await pause(250);
          assert.equal(await run(`document.querySelector('[role="tablist"][id$="-linkwrap"]').scrollLeft`),before,'Scan reset manual scrolling');
          const point=await run(`(() => {
            const e=document.querySelector('[role="tab"][href="/games/bedrock/patch_notes"]');
            const r=e.getBoundingClientRect(),x=Math.round(r.left+r.width/2),y=Math.round(r.top+r.height/2),top=document.elementFromPoint(x,y);
            return {x,y,hit:top===e||e.contains(top)};
          })()`);
          assert.equal(point.hit,true,'Last tab is covered');
          win.webContents.sendInputEvent({type:'mouseDown',button:'left',x:point.x,y:point.y,clickCount:1});
          win.webContents.sendInputEvent({type:'mouseUp',button:'left',x:point.x,y:point.y,clickCount:1});
          await ready(`location.pathname==='/games/bedrock/patch_notes'`);
        });
        await check((magnified?'large':'normal')+' selected last Bedrock tab is fully visible',async()=>{
          await load('/games/bedrock/patch_notes');
          await pause(1000);
          const metrics=await run(`(() => {
            const list=document.querySelector('[role="tablist"][id$="-linkwrap"]'),active=list.querySelector('[aria-selected="true"]');
            const a=active.getBoundingClientRect(),b=list.getBoundingClientRect();
            return {left:a.left,right:a.right,listLeft:b.left,listRight:b.right,scroll:list.scrollLeft};
          })()`);
          assert.ok(metrics.left>=metrics.listLeft-1&&metrics.right<=metrics.listRight+1,'Selected tab is outside its strip');
          report.native.push({magnified,images:await run(`[...document.querySelectorAll('main img')].slice(0,6).map(e=>{const s=getComputedStyle(e);return {src:e.currentSrc,naturalWidth:e.naturalWidth,complete:e.complete,opacity:s.opacity,visibility:s.visibility,display:s.display};})`)});
          await capture('bedrock-selected-last-tab-'+(magnified?'large':'normal'));
        });
      }
      await check('selected tab stays visible through an in-place native text-size change',async()=>{
        await load('/games/bedrock/patch_notes');
        await run(`window.__navigationMountedTabs=document.querySelector('[role="tablist"][id$="-linkwrap"]');window.MCLauncher.setUseMagnifiedMode(false)`);
        await ready(`getComputedStyle(document.querySelector('[role="tab"][aria-selected="true"]')).fontSize==='16px'`);
        await pause(300);
        await run(`window.MCLauncher.setUseMagnifiedMode(true)`);
        await ready(`getComputedStyle(document.querySelector('[role="tab"][aria-selected="true"]')).fontSize==='24px'`);
        await pause(300);
        const metrics=await run(`(() => {
          const list=document.querySelector('[role="tablist"][id$="-linkwrap"]'),active=list.querySelector('[aria-selected="true"]');
          const a=active.getBoundingClientRect(),b=list.getBoundingClientRect();
          return {sameList:list===window.__navigationMountedTabs,left:a.left,right:a.right,listLeft:b.left,listRight:b.right};
        })()`);
        assert.equal(metrics.sameList,true,'Native text-size update must keep the existing strip mounted');
        assert.ok(metrics.left>=metrics.listLeft-1&&metrics.right<=metrics.listRight+1,'Text-size update hides the selected tab');
      });
      await check('selected tab stays visible when a mounted tab label becomes longer',async()=>{
        await run(`(() => {
          const list=document.querySelector('[role="tablist"][id$="-linkwrap"]');
          const label=list.querySelector('[role="tab"] [id$="-notification-text"]'),text=label.firstChild;
          window.__navigationOriginalLabel=text.nodeValue;text.replaceWith(document.createTextNode(text.nodeValue+' for Windows'));
        })()`);
        await pause(300);
        const metrics=await run(`(() => {
          const list=document.querySelector('[role="tablist"][id$="-linkwrap"]'),active=list.querySelector('[aria-selected="true"]');
          const a=active.getBoundingClientRect(),b=list.getBoundingClientRect();
          return {left:a.left,right:a.right,listLeft:b.left,listRight:b.right};
        })()`);
        assert.ok(metrics.left>=metrics.listLeft-1&&metrics.right<=metrics.listRight+1,'Changed label width hides the selected tab');
        await run(`document.querySelector('[role="tablist"][id$="-linkwrap"] [role="tab"] [id$="-notification-text"]').firstChild.replaceWith(document.createTextNode(window.__navigationOriginalLabel))`);
      });
      assert.deepEqual(report.consoleErrors,[],'Unexpected renderer errors');
      assert.deepEqual(report.blockedEffects,[],'Fixture triggered an external action');
    }catch(error){report.fatal=String(error.stack||error);
      if(win&&!win.isDestroyed())report.fatalPage=await run(`({path:location.pathname,text:document.body.innerText.slice(0,3000)})`).catch(()=>null);
    }
    finish(report.fatal||failures.length?1:0);
  });
}
