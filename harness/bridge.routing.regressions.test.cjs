// Native route bootstrap with disposable state and no operating-system actions.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'bridge.js'),'utf8');

if(!process.versions.electron){
  const {test}=require('node:test'),vm=require('node:vm');
  const block=source.slice(source.indexOf('  const PAGES ='),source.indexOf('  // Real download:'));
  function entry(pathname){
    const scope={location:{pathname},normalisePath:p=>p.replace(/\/+$/,'')||'/',window:{},setTimeout(){},console};
    vm.createContext(scope);vm.runInContext(block+'\nglobalThis.seedEntry=applyEntryPage;',scope);
    return scope;
  }
  test('fresh absent native preferences seed the requested product on first read',()=>{
    const scope=entry('/games/java/configurations');
    assert.deepEqual(JSON.parse(scope.seedEntry('UiSettings',{value:null}).value),{lastVisitedPage:'java'});
  });
  test('entry seeding preserves existing preferences and later navigation choices',()=>{
    const scope=entry('/games/java/configurations');
    const events={value:'{}'};assert.equal(scope.seedEntry('UiEvents',events),events);
    const original={value:JSON.stringify({lastVisitedPage:'bedrock',filters:{release:false},customPreference:'retained'})};
    const first=scope.seedEntry('UiSettings',original);
    assert.deepEqual(JSON.parse(first.value),{lastVisitedPage:'java',filters:{release:false},customPreference:'retained'});
    assert.equal(JSON.parse(original.value).lastVisitedPage,'bedrock');
    const later={value:JSON.stringify({lastVisitedPage:'bedrock'})};
    assert.equal(scope.seedEntry('UiSettings',later),later);
  });
  test('fresh native configurations deep link renders the exact saved rows',()=>{
    const {execFileSync}=require('node:child_process');
    process.stdout.write(execFileSync(process.execPath,[path.join(__dirname,'../app/node_modules/electron/cli.js'),__filename],{encoding:'utf8',timeout:60000}));
  });
}else{
  const {app,BrowserWindow}=require('electron'),{spawn}=require('node:child_process');
  const os=require('node:os'),http=require('node:http'),net=require('node:net');
  const root=path.dirname(__dirname),temporary=fs.mkdtempSync(path.join(os.tmpdir(),'launcher-cold-routing-'));
  const output=process.env.ROUTING_TEST_OUTPUT||temporary;
  fs.mkdirSync(output,{recursive:true});app.setPath('userData',path.join(temporary,'electron'));
  app.disableHardwareAcceleration();
  for(const flag of ['disable-renderer-backgrounding','disable-background-timer-throttling'])app.commandLine.appendSwitch(flag);
  let host,win,origin;const report={checks:[],errors:[],temporary,blockedEffects:[]};
  const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const run=code=>win.webContents.executeJavaScript(code);
  function request(route,body){return new Promise((resolve,reject)=>{
    const req=http.request(origin+route,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:{}},response=>{
      let text='';response.on('data',chunk=>text+=chunk);response.on('end',()=>{try{const value=JSON.parse(text);response.statusCode>=400?reject(Error(text)):resolve(value);}catch(error){reject(error);}});
    });req.on('error',reject);req.end(body?JSON.stringify(body):null);
  });}
  async function ready(code){for(let attempt=0;attempt<80;attempt++){if(await run(code))return;await sleep(100);}throw Error('Native route did not settle: '+code);}
  async function snapshot(){return run(`({path:location.pathname,entrySettled:__entrySettled,
    selected:[...document.querySelectorAll('[role="tab"][aria-selected="true"][href]')].map(e=>e.getAttribute('href')),
    installations:!!document.querySelector('#versions_search_label'),
    rows:[...document.querySelectorAll('main li[data-configuration-id]')].filter(e=>e.getClientRects().length).map(e=>({id:e.dataset.configurationId,text:e.innerText})),
    brokenImages:[...document.images].filter(e=>e.getAttribute('src')&&e.complete&&!e.naturalWidth).map(e=>e.src)})`);}
  app.whenReady().then(async()=>{
    try{
      const state=path.join(temporary,'state'),game=path.join(temporary,'game'),data=path.join(temporary,'data');
      for(const folder of [state,game,data,path.join(state,'versions/26.3')])fs.mkdirSync(folder,{recursive:true});
      fs.writeFileSync(path.join(data,'state.json'),'{}');
      fs.writeFileSync(path.join(state,'launcher_settings.json'),'{}');
      fs.writeFileSync(path.join(state,'launcher_profiles.json'),JSON.stringify({profiles:{
        'cold-a':{name:'Cold Alpha',type:'custom',lastVersionId:'26.3'},
        'cold-b':{name:'Cold Beta',type:'custom',lastVersionId:'26.3'},
        'cold-c':{name:'Cold Gamma',type:'custom',lastVersionId:'26.3'},
        'cold-release':{name:'',type:'latest-release',lastVersionId:'latest-release'}
      }}));
      fs.writeFileSync(path.join(state,'versions/26.3/26.3.json'),JSON.stringify({id:'26.3',type:'release',mainClass:'NeverExecuteFixture',libraries:[]}));
      fs.writeFileSync(path.join(state,'versions/version_manifest_v2.json'),JSON.stringify({latest:{release:'26.3',snapshot:'26.3'},versions:[]}));
      const port=await new Promise(resolve=>{const server=net.createServer();server.listen(0,'127.0.0.1',()=>{const port=server.address().port;server.close(()=>resolve(port));});});
      origin='http://127.0.0.1:'+port;
      host=spawn('python',['-B',path.join(__dirname,'server.py'),String(port)],{cwd:root,windowsHide:true,
        env:{...process.env,LAUNCHER_ROOT:root,LAUNCHER_STATE_DIR:state,LAUNCHER_GAME_DIR:game,LAUNCHER_DATA_DIR:data,LAUNCHER_NO_RELOAD:'1',LAUNCHER_MSA_CLIENT_ID:''},stdio:['ignore','pipe','pipe']});
      const log=fs.createWriteStream(path.join(output,'host.log'));host.stdout.pipe(log);host.stderr.pipe(log);
      for(let attempt=0;attempt<80;attempt++){try{await request('/__accounts');break;}catch(error){if(attempt===79)throw error;await sleep(100);}}
      await request('/__accounts',{action:'login',name:'ColdRouteFixture'});
      report.initialUiSettings=await request('/__state/UiSettings');assert.equal(report.initialUiSettings.value,null);
      win=new BrowserWindow({width:1280,height:800,show:false,webPreferences:{contextIsolation:true,nodeIntegration:false,sandbox:true,offscreen:true,backgroundThrottling:false,partition:'cold-route-memory'}});
      win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
      win.webContents.session.webRequest.onBeforeRequest((details,callback)=>{
        let allowed=false;try{const u=new URL(details.url);allowed=u.origin===origin||['data:','blob:','file:','devtools:','chrome:'].includes(u.protocol);
          if(u.origin===origin&&/^\/(?:__(?:launch|install|uninstall|open|dialog)|__loaders\/materialize)$/.test(u.pathname)){allowed=false;report.blockedEffects.push(u.pathname);}
        }catch{}callback({cancel:!allowed});
      });
      win.webContents.on('console-message',details=>{if(details.level==='error'||details.level===3)report.errors.push(details.message);});
      await win.loadURL(origin+'/games/java/configurations');
      await ready(`window.__entrySettled===true && location.pathname==='/games/java/configurations' &&
        !!document.querySelector('a[role="tab"][aria-selected="true"][href="/games/java/configurations"]') &&
        !!document.querySelector('#versions_search_label') &&
        [...document.querySelectorAll('main li[data-configuration-id]')].filter(e=>e.getClientRects().length).length===4`);
      report.first=await snapshot();
      fs.writeFileSync(path.join(output,'cold-configurations.png'),(await win.webContents.capturePage()).toPNG());
      assert.equal(report.first.path,'/games/java/configurations',JSON.stringify(report.first));
      assert.equal(report.first.installations,true);assert.ok(report.first.selected.includes('/games/java/configurations'));
      assert.deepEqual(report.first.rows.map(row=>row.id).sort(),['cold-a','cold-b','cold-c','cold-release']);
      assert.deepEqual(report.first.brokenImages,[]);
      report.checks.push('Cold empty store honors the direct native Installations route with all4 saved rows');
      await run(`document.querySelector('a[href="/games/bedrock/play"]').click()`);
      await ready(`!!document.querySelector('a[role="tab"][aria-selected="true"][href="/games/bedrock/play"]')`);await sleep(500);
      assert.equal(await run('location.pathname'),'/games/bedrock/play');
      report.checks.push('Normal navigation after cold initialization retains the user choice');
      assert.deepEqual(report.blockedEffects,[]);assert.deepEqual(report.errors,[]);
    }catch(error){report.error=String(error.stack||error);}
    finally{
      fs.writeFileSync(path.join(output,'cold-route-report.json'),JSON.stringify(report,null,2));
      if(win)win.destroy();if(host)host.kill();
      console.log(JSON.stringify({...report,output},null,2));app.exit(report.error?1:0);
    }
  });
}
