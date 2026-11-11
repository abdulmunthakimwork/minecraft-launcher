// Exercises real keyboard events against the official bundle and an isolated host.
// Loader metadata is a fixture; no downloads, game process or user state.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

if (!process.versions.electron) {
  const {test} = require('node:test');
  const {execFileSync} = require('node:child_process');
  test('native loader picker handles a whole keyboard press once', () => {
    const cli = path.join(__dirname, '../app/node_modules/electron/cli.js');
    process.stdout.write(execFileSync(process.execPath, [cli, __filename], {encoding:'utf8', timeout:60000}));
  });
} else {
  const {app, BrowserWindow} = require('electron');
  const {spawn} = require('node:child_process');
  const http = require('node:http');
  const net = require('node:net');
  const root = path.dirname(__dirname);
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'launcher-native-menu-'));
  app.setPath('userData', path.join(temporary, 'electron'));
  app.disableHardwareAcceleration();
  app.commandLine.appendSwitch('disable-renderer-backgrounding');
  app.commandLine.appendSwitch('disable-background-timer-throttling');
  let host, win, origin;
  const report = {checks:[], events:[]};
  const sleep = ms => new Promise(resolve=>setTimeout(resolve,ms));
  const run = source => win.webContents.executeJavaScript(source);
  function request(route, body) {
    return new Promise((resolve,reject)=>{
      const encoded=body?JSON.stringify(body):null;
      const req=http.request(origin+route,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:{}},response=>{
        let data='';response.on('data',chunk=>data+=chunk);response.on('end',()=>{
          if(response.statusCode>=400)reject(new Error(data));else resolve(JSON.parse(data));
        });
      });req.on('error',reject);req.end(encoded);
    });
  }
  async function ready(source) {
    for(let i=0;i<60;i++){if(await run(source))return;await sleep(100);}
    throw new Error('Native menu fixture did not settle: '+source);
  }
  async function press(keyCode) {
    keyCode=({ArrowDown:'Down',ArrowUp:'Up'})[keyCode]||keyCode;
    win.webContents.sendInputEvent({type:'keyDown',keyCode});
    if(keyCode==='Enter'||keyCode==='Space')win.webContents.sendInputEvent({type:'char',keyCode});
    await sleep(40);
    win.webContents.sendInputEvent({type:'keyUp',keyCode});
    await sleep(200);
  }
  async function expanded() {
    return run(`document.querySelector('#__hx-loader-version [role="button"]').getAttribute('aria-expanded')`);
  }
  app.whenReady().then(async()=>{
    try {
      const port=await new Promise(resolve=>{const server=net.createServer();server.listen(0,'127.0.0.1',()=>{
        const port=server.address().port;server.close(()=>resolve(port));
      });});
      origin='http://127.0.0.1:'+port;
      const state=path.join(temporary,'state'),game=path.join(temporary,'game'),data=path.join(temporary,'data');
      for(const folder of [state,game,data,path.join(state,'versions/26.3')])fs.mkdirSync(folder,{recursive:true});
      // Keep bootstrap routing independent of the user's saved overlay.
      fs.writeFileSync(path.join(data,'state.json'),JSON.stringify({UiSettings:JSON.stringify({lastVisitedPage:'java'}),UiEvents:'{}'}));
      fs.writeFileSync(path.join(state,'versions/26.3/26.3.json'),JSON.stringify({id:'26.3',type:'release',mainClass:'Fixture',libraries:[],downloads:{client:{url:'https://example.invalid/fixture.jar',size:1}}}));
      fs.writeFileSync(path.join(state,'versions/version_manifest_v2.json'),JSON.stringify({latest:{release:'26.3',snapshot:'26.3'},versions:[]}));
      fs.writeFileSync(path.join(state,'launcher_profiles.json'),JSON.stringify({profiles:{
        'fixture-release':{name:'Latest release',type:'latest-release',lastVersionId:'latest-release'}
      }}));
      host=spawn('python',['-B',path.join(__dirname,'server.py'),String(port)],{cwd:root,windowsHide:true,
        env:{...process.env,LAUNCHER_ROOT:root,LAUNCHER_STATE_DIR:state,LAUNCHER_GAME_DIR:game,LAUNCHER_DATA_DIR:data,LAUNCHER_NO_RELOAD:'1',LAUNCHER_MSA_CLIENT_ID:''},stdio:['ignore','pipe','pipe']});
      const stream=fs.createWriteStream(path.join(temporary,'host.log'));host.stdout.pipe(stream);host.stderr.pipe(stream);
      for(let i=0;i<60;i++){try{await request('/__accounts');break;}catch(error){if(i===59)throw error;await sleep(100);}}
      await request('/__accounts',{action:'login',name:'MenuFixture'});
      win=new BrowserWindow({width:1280,height:800,show:false,webPreferences:{contextIsolation:true,nodeIntegration:false,sandbox:true,backgroundThrottling:false,partition:'native-menu-fixture'}});
      win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
      win.webContents.session.webRequest.onBeforeRequest((details,callback)=>{
        let allowed=false;try{const u=new URL(details.url);allowed=['data:','blob:','file:','devtools:','chrome:'].includes(u.protocol)||u.origin===origin;}catch{}
        callback({cancel:!allowed});
      });
      await win.loadURL(origin+'/games/java/configurations');
      await ready(`window.__entrySettled && location.pathname==='/games/java/configurations' && !!document.querySelector('#__hx-modpacks') && Array.from(document.querySelectorAll('button')).some(e=>/New installation/i.test(e.innerText))`);
      await run(`window.__menuSideEffects=[];const previousFetch=window.fetch;window.fetch=(url,opts)=>{
        if(String(url).startsWith('/__loaders/versions'))return Promise.resolve({ok:true,json:async()=>({versions:['0.16.5','0.16.4'],recommended:'0.16.5'})});
        if(['/__install','/__launch','/__loaders/materialize'].includes(String(url))){__menuSideEffects.push(String(url));return Promise.reject(new Error('Unexpected menu side effect'));}
        return previousFetch(url,opts);
      };Array.from(document.querySelectorAll('button')).find(e=>/New installation/i.test(e.innerText)).click();`);
      await ready(`!!document.querySelector('[data-loader="fabric-loader"]')`);
      await run(`document.querySelector('[data-loader="fabric-loader"]').click()`);
      await ready(`document.querySelectorAll('[data-loader-version]').length===2`);
      win.webContents.focus();
      await run(`window.__menuEvents=[];for(const type of ['keydown','keyup','keypress','click','focusin']){
        for(const capture of [true,false])document.addEventListener(type,event=>__menuEvents.push({type,key:event.key,capture,
          target:event.target.tagName,id:event.target.id,role:event.target.getAttribute('role'),prevented:event.defaultPrevented,
          expanded:document.querySelector('#__hx-loader-version [role="button"]').getAttribute('aria-expanded'),
          stack:type==='click'?new Error().stack:undefined}),capture);
      }document.querySelector('#__hx-loader-version [role="button"]').focus();`);
      await press('Enter');
      report.firstEnter=await expanded();report.events=await run('__menuEvents');
      assert.equal(report.firstEnter,'true',JSON.stringify(report.events));
      report.checks.push('Parent role button opens with real Enter before native menu capture');
      await press('Escape');assert.equal(await expanded(),'false');
      assert.equal(await run(`document.activeElement===document.querySelector('#__hx-loader-version [role="button"]')`),true);
      assert.equal(await run(`!!document.querySelector('#configuration_name')`),true);
      await press('Space');assert.equal(await expanded(),'true');
      report.checks.push('Parent role button opens with real Space after Escape');
      await press('Escape');
      await run(`document.querySelector('#__hx-loader-version input').focus()`);
      await press('Enter');assert.equal(await expanded(),'true');
      report.checks.push('Readonly text field opens with real Enter');
      await press('ArrowDown');
      report.firstArrow=await run(`({active:document.activeElement.tagName,version:document.activeElement.closest('[data-loader-version]')?.getAttribute('data-loader-version')})`);
      await press('ArrowDown');
      report.secondArrow=await run(`({active:document.activeElement.tagName,version:document.activeElement.closest('[data-loader-version]')?.getAttribute('data-loader-version')})`);
      assert.deepEqual(report.firstArrow,{active:'BUTTON',version:'0.16.5'});
      assert.deepEqual(report.secondArrow,{active:'BUTTON',version:'0.16.4'});
      await press('Home');
      assert.equal(await run(`document.activeElement.closest('[data-loader-version]')?.getAttribute('data-loader-version')`),'0.16.5');
      await press('End');
      assert.equal(await run(`document.activeElement.closest('[data-loader-version]')?.getAttribute('data-loader-version')`),'0.16.4');
      await press('Enter');assert.equal(await expanded(),'false');
      assert.equal(await run(`document.querySelector('#__hx-loader-version input').placeholder`),'0.16.4');
      report.checks.push('Real arrows and Enter choose a loader version');
      assert.deepEqual(await run('__menuSideEffects'),[]);
    }catch(error){report.error=String(error.stack||error);}
    finally{
      if(win){report.events=await run('__menuEvents').catch(()=>report.events);win.destroy();}if(host)host.kill();
      console.log(JSON.stringify({...report,events:report.error?report.events:undefined,output:temporary},null,2));
      app.exit(report.error?1:0);
    }
  });
}
