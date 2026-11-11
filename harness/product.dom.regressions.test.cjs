// Runs the official UI against an isolated host; no downloads or game process.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

if (!process.versions.electron) {
  const {test} = require('node:test');
  const {execFileSync} = require('node:child_process');
  test('native product UI separates local capability from ownership', () => {
    const cli = path.join(__dirname, '../app/node_modules/electron/cli.js');
    process.stdout.write(execFileSync(process.execPath, [cli, __filename], {encoding:'utf8', timeout:180000}));
  });
} else {
  const {app, BrowserWindow} = require('electron');
  const {spawn} = require('node:child_process');
  const http = require('node:http');
  const net = require('node:net');
  const root = path.dirname(__dirname);
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'launcher-product-dom-'));
  const output = process.env.PRODUCT_TEST_OUTPUT || temporary;
  fs.mkdirSync(output, {recursive:true});
  app.setPath('userData', path.join(temporary, 'electron'));
  app.disableHardwareAcceleration();
  app.commandLine.appendSwitch('disable-renderer-backgrounding');
  app.commandLine.appendSwitch('disable-background-timer-throttling');
  let host, win, origin;
  const report = {checks:[], errors:[], snapshots:[], realmRequests:[]};
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const run = source => win.webContents.executeJavaScript(source);
  function request(route, body) {
    return new Promise((resolve,reject) => {
      const encoded = body ? JSON.stringify(body) : null;
      const req = http.request(origin + route, {method:body?'POST':'GET', headers:body?{'Content-Type':'application/json'}:{}}, response => {
        let data='';response.on('data',chunk=>data+=chunk);response.on('end',()=> {
          if(response.statusCode>=400)reject(new Error(data));else resolve(JSON.parse(data));
        });
      });req.on('error',reject);req.end(encoded);
    });
  }
  async function check(name, action) {
    try {await action();report.checks.push({name,passed:true});}
    catch(error) {report.checks.push({name,passed:false,error:String(error.stack||error)});}
  }
  async function load(route) {
    await win.loadURL(origin+route);await sleep(3000);
    const snapshot=await run(`({path:location.pathname,text:document.body.innerText,
      controls:[...document.querySelectorAll('button,[role=button]')].filter(e=>e.getClientRects().length).map(e=>({id:e.id,text:e.innerText,disabled:e.disabled||e.getAttribute('aria-disabled')==='true'})),
      alerts:[...document.querySelectorAll('[id$="Alert"]')].map(e=>({id:e.id,text:e.innerText,html:e.outerHTML})),
      launcherLinks:[...document.querySelectorAll('a[href*="DownloadNewLauncher"]')].map(e=>e.parentElement.outerHTML),
      brokenImages:[...document.images].filter(e=>e.getAttribute('src')&&e.complete&&!e.naturalWidth).map(e=>e.src)})`);
    report.snapshots.push(snapshot);
    assert.equal(snapshot.brokenImages.length,0);
    assert.ok(!/Whoops!|PAGE NOT FOUND/.test(snapshot.text),snapshot.text.slice(-1500));
    const capture=await Promise.race([win.webContents.capturePage(),sleep(4000).then(()=>null)]);
    if(capture)fs.writeFileSync(path.join(output,route.replace(/[^a-z0-9]+/gi,'_')+'.png'),capture.toPNG());
    return snapshot;
  }
  app.whenReady().then(async () => {
    try {
      const port=await new Promise(resolve=>{const reservation=net.createServer();reservation.listen(0,'127.0.0.1',()=>{const port=reservation.address().port;reservation.close(()=>resolve(port));});});
      origin='http://127.0.0.1:'+port;
      const state=path.join(temporary,'state'),game=path.join(temporary,'game'),data=path.join(temporary,'data');
      for(const folder of [state,game,data,path.join(state,'versions/26.3'),path.join(game,'versions/26.3'),path.join(game,'runtime/jre-legacy/bin')])fs.mkdirSync(folder,{recursive:true});
      fs.writeFileSync(path.join(state,'versions/26.3/26.3.json'),JSON.stringify({id:'26.3',type:'release',mainClass:'Fixture',libraries:[],downloads:{client:{url:'https://example.invalid/fixture.jar',size:1}}}));
      fs.writeFileSync(path.join(state,'versions/version_manifest_v2.json'),JSON.stringify({latest:{release:'26.3',snapshot:'26.3'},versions:[]}));
      fs.writeFileSync(path.join(state,'launcher_profiles.json'),JSON.stringify({profiles:{
        'fixture-release':{name:'Latest release',type:'latest-release',lastVersionId:'latest-release',lastUsed:'2026-11-10T00:00:00.000Z'},
        'fixture-alternate':{name:'Fixture Alternate',type:'custom',lastVersionId:'26.3',lastUsed:'2020-01-01T00:00:00.000Z'},
        'fixture-deleted':{name:'Fixture Removed',type:'custom',lastVersionId:'26.3'}
      }}).replace(/^/,'\ufeff'));
      const instance=path.join(temporary,'saved-instance'),moddedVersion='fabric-loader-0.16-26.3';
      fs.mkdirSync(path.join(instance,'versions',moddedVersion),{recursive:true});
      fs.writeFileSync(path.join(instance,'versions',moddedVersion,moddedVersion+'.json'),'\ufeff'+JSON.stringify({
        id:moddedVersion,type:'release',inheritsFrom:'26.3',libraries:[],mainClass:'FixtureLoader'
      }));
      // Preseed the fixture overlay so the host cannot copy the user's saved overlay.
      fs.writeFileSync(path.join(data,'state.json'),'\ufeff'+JSON.stringify({
        __configurations:{'fixture-modded':{name:'Fixture Modded',type:'custom',lastVersionId:moddedVersion,
          gameDir:instance,lastUsed:'2020-01-02T00:00:00.000Z'}},
        __deleted_configurations:['fixture-deleted']
      }));
      fs.writeFileSync(path.join(game,'versions/26.3/26.3.jar'),'x');
      fs.writeFileSync(path.join(game,'runtime/jre-legacy/bin/java.exe'),'fixture-never-executed');
      fs.writeFileSync(path.join(game,'runtime/jre-legacy/manifest.json'),JSON.stringify({files:{'bin/java.exe':{type:'file',size:22}}}));
      host=spawn('python',['-B',path.join(__dirname,'server.py'),String(port)],{cwd:root,windowsHide:true,
        env:{...process.env,LAUNCHER_ROOT:root,LAUNCHER_STATE_DIR:state,LAUNCHER_GAME_DIR:game,LAUNCHER_DATA_DIR:data,LAUNCHER_NO_RELOAD:'1',LAUNCHER_MSA_CLIENT_ID:''},stdio:['ignore','pipe','pipe']});
      const stream=fs.createWriteStream(path.join(output,'host.log'));host.stdout.pipe(stream);host.stderr.pipe(stream);
      for(let attempt=0;attempt<60;attempt++){try{await request('/__accounts');break;}catch(error){if(attempt===59)throw error;await sleep(200);}}
      const first=await request('/__accounts',{action:'login',name:'FirstFixture'});
      const second=await request('/__accounts',{action:'login',name:'SecondFixture'});
      await request('/__accounts',{action:'switch',id:first.activeAccount});
      win=new BrowserWindow({width:1280,height:800,show:false,webPreferences:{contextIsolation:true,nodeIntegration:false,sandbox:true,offscreen:true,backgroundThrottling:false,partition:'product-test-memory'}});
      win.webContents.setWindowOpenHandler(()=>({action:'deny'}));
      win.webContents.session.webRequest.onBeforeRequest((details,callback)=>{let allowed=false;try{const u=new URL(details.url);if(u.pathname.startsWith('/api/realms/'))report.realmRequests.push(u.pathname);allowed=['data:','blob:','file:','devtools:','chrome:'].includes(u.protocol)||u.origin===origin;}catch{}callback({cancel:!allowed});});
      win.webContents.on('console-message',details=>{if(details.level==='error'||details.level===3)report.errors.push(details.message);});
      await check('saved BOM profiles render exact native installation rows after reload',async()=>{
        await load('/games/java/play');
        for(let reload=0;reload<2;reload++){
          const installations=await load('/games/java/configurations');
          assert.equal(installations.path,'/games/java/configurations');
          assert.equal(await run(`!!document.querySelector('#versions_search_label')`),true);
          const rows=await run(`Array.from(document.querySelectorAll('main li[data-configuration-id]'))
            .filter(e=>e.getClientRects().length).map(e=>({id:e.getAttribute('data-configuration-id'),text:e.innerText}))`);
          assert.deepEqual(rows.map(e=>e.id).sort(),['fixture-alternate','fixture-modded','fixture-release']);
          const expected={'fixture-release':'Latest release','fixture-alternate':'Fixture Alternate','fixture-modded':'Fixture Modded'};
          for(const row of rows)assert.ok(row.text.includes(expected[row.id]),JSON.stringify(row));
          assert.equal(installations.text.includes('Fixture Removed'),false);
          assert.equal(await run(`__host.appData.gameVersions.versions[${JSON.stringify(moddedVersion)}].minecraftVersion`),'26.3');
        }
      });
      await check('installed offline Java keeps native Play without ownership',async()=>{
        await load('/games/java/play');
        const state=await run(`({play:!!document.querySelector('#bigCTAButton[data-testid="game-play-button"]'),
          disabled:document.querySelector('#bigCTAButton')?.disabled,
          entitlements:__host.appData.__accounts.find(a=>a.id===__host.appData.__activeAccountId).entitlements,
          offlineReady:__host.appData.productInfo.products.java.versionData[0].isPlayableOffline,
          profileSelector:!!document.querySelector('#installation-select-label'),
          purchaseSection:!!document.querySelector('[aria-label="Upsell product section"]'),
          demoButton:[...document.querySelectorAll('button')].some(e=>e.innerText==='Play Demo')})`);
        assert.deepEqual(state,{play:true,disabled:false,entitlements:[],offlineReady:true,profileSelector:true,purchaseSection:false,demoButton:false});
        await run(`document.querySelector('[role="button"][aria-controls="toggle-list-menu"]').click()`);await sleep(200);
        await run(`document.querySelector('#toggle-list-menu button[aria-label="Fixture Alternate"]').click()`);await sleep(300);
        assert.match(await run(`document.querySelector('[role="button"][aria-controls="toggle-list-menu"]').innerText`),/Fixture Alternate/);
        await run(`document.querySelector('[role="button"][aria-controls="toggle-list-menu"]').click()`);await sleep(200);
        await run(`document.querySelector('#toggle-list-menu button[aria-label="Latest release"]').click()`);await sleep(300);
        assert.match(await run(`document.querySelector('[role="button"][aria-controls="toggle-list-menu"]').innerText`),/Latest release/);
        await run(`window.__launchRequests=[];window.__productRequests=[];const previousFetch=window.fetch;window.fetch=(url,opts)=>{
          if(String(url)==='/__install'){window.__productRequests.push('install');return Promise.resolve({ok:true,json:async()=>({ok:true})});}
          if(String(url)==='/__install_status')return Promise.resolve({ok:true,json:async()=>({active:false,isInstalled:true,phase:'done',bytesDone:1,bytesTotal:1})});
          if(String(url)==='/__launch'){window.__launchRequests.push(JSON.parse(opts.body));return Promise.resolve({ok:true,json:async()=>({pid:1})});}
          return previousFetch(url,opts);};
          document.querySelector('#bigCTAButton[data-testid="game-play-button"]').click();`);
        await sleep(700);
        const launches=await run('__launchRequests');
        report.playDiagnostics=await run(`({calls:__calls.slice(-15),requests:__productRequests,text:document.body.innerText.slice(-1600)})`);
        assert.equal(launches.length,1,JSON.stringify(report.playDiagnostics));assert.equal(launches[0].productId,'java');
      });
      await check('account switch and reload retain local Java and Skins',async()=>{
        await run(`MCLauncher.switchAccount(${JSON.stringify(second.activeAccount)})`);await sleep(300);
        await load('/games/java/play');
        assert.equal(await run(`!!document.querySelector('#bigCTAButton[data-testid="game-play-button"]')`),true);
        assert.equal(await run(`!!document.querySelector('#installation-select-label')`),true);
        assert.equal(await run(`!!document.querySelector('[aria-label="Upsell product section"]')`),false);
        assert.equal(await run(`document.body.innerText.includes('Try Minecraft for free!')`),false);
        assert.equal(await run('__host.appData.__activeAccountId'),second.activeAccount);
        const skins=await load('/games/java/skins');assert.match(skins.text,/New skin/);
        assert.equal(await run(`!!document.querySelector('canvas')`),true);
      });
      await check('Bedrock browsing shows build limitation and intact hero art',async()=>{
        const bedrock=await load('/games/bedrock/play');assert.equal(bedrock.path,'/games/bedrock/play');
        assert.match(bedrock.text,/unavailable in this build|cannot be launched with this version of the Launcher/i);
        assert.equal(await run(`Array.from(document.images).some(e=>e.naturalWidth>500)`),true);
        assert.equal(await run(`Array.from(document.querySelectorAll('[data-testid="game-play-button"]')).some(e=>e.getClientRects().length&&!e.disabled)`),false);
      });
      await check('Marketplace Pass contains native cards and art at the smaller desktop size',async()=>{
        async function awaitMarketplaceImages(){
          const images=await run(`(async()=>{
            const images=[...document.querySelectorAll('main img[src*="marketplacepass/featureCard_"],main img[src*="marketplacepass/followTheLight."]')];
            await Promise.all(images.map(image=>image.decode()));
            return images.map(image=>({complete:image.complete,naturalWidth:image.naturalWidth,naturalHeight:image.naturalHeight}));
          })()`);
          assert.equal(images.length,4,'The native Marketplace page must render all feature and closing artwork.');
          assert.ok(images.every(image=>image.complete&&image.naturalWidth>0&&image.naturalHeight>0),JSON.stringify(images));
          return images;
        }
        const measure=`(()=>{
          const first=document.querySelector('img[src*="marketplacepass/featureCard_1."]');
          const group=first.parentElement.parentElement.parentElement;
          const art=document.querySelector('img[src*="marketplacepass/followTheLight."]');
          let scroll=group;while(scroll&&!['auto','scroll'].includes(getComputedStyle(scroll).overflowX))scroll=scroll.parentElement;
          const rect=e=>{const r=e.getBoundingClientRect();return {left:r.left,right:r.right,width:r.width,top:r.top};};
          return {viewport:{left:scroll.getBoundingClientRect().left,width:scroll.clientWidth,scrollWidth:scroll.scrollWidth},
            cards:[...group.children].map(rect),images:[...group.querySelectorAll('img')].map(rect),gap:getComputedStyle(group).columnGap,
            art:{...rect(art),height:art.getBoundingClientRect().height,naturalWidth:art.naturalWidth,naturalHeight:art.naturalHeight}};
        })()`;
        try{
          win.setSize(1280,720);await load('/games/bedrock/marketplace_pass');
          const wideImages=await awaitMarketplaceImages();
          const wide=await run(measure);
          const nativeWide=await run(`(()=>{
            const first=document.querySelector('img[src*="marketplacepass/featureCard_1."]');
            const group=first.parentElement.parentElement.parentElement,holder=group.parentElement;
            const art=document.querySelector('img[src*="marketplacepass/followTheLight."]');
            const saved=[group.style.cssText,holder.style.cssText,art.style.cssText];
            for(const property of ['max-width','box-sizing','padding-inline','flex-wrap','justify-content'])group.style.removeProperty(property);
            holder.style.removeProperty('max-width');art.style.removeProperty('max-width');art.style.removeProperty('height');
            const result=${measure};[group,holder,art].forEach((e,i)=>e.style.cssText=saved[i]);return result;
          })()`);
          assert.deepEqual(wide.cards,nativeWide.cards);assert.equal(wide.gap,'72px');
          assert.equal(wide.art.width,nativeWide.art.width);assert.equal(wide.art.width,wide.art.naturalWidth);
          win.setSize(1024,640);await load('/games/bedrock/marketplace_pass');
          const smallImages=await awaitMarketplaceImages();
          const small=await run(measure);report.marketplaceLayout={wide,small,wideImages,smallImages};
          assert.equal(small.viewport.scrollWidth,small.viewport.width,JSON.stringify(small));
          assert.equal(small.cards.length,3);assert.ok(small.cards.every(e=>e.width===240));
          assert.equal(small.cards[0].top,small.cards[1].top);assert.ok(small.cards[2].top>small.cards[0].top);
          assert.ok(small.images.every(e=>e.left>=small.viewport.left&&e.right<=small.viewport.left+small.viewport.width));
          assert.ok(small.art.width<=small.viewport.width);assert.equal(small.art.naturalWidth,wide.art.naturalWidth);
          assert.ok(Math.abs(small.art.width/small.art.height-small.art.naturalWidth/small.art.naturalHeight)<0.001);
          fs.writeFileSync(path.join(output,'marketplace-contained-1024.png'),(await win.webContents.capturePage()).toPNG());
        }finally{win.setSize(1280,800);}
      });
      await check('Realms explains unavailability before an ownership upsell',async()=>{
        const realms=await load('/games/java/realms');assert.match(realms.text,/Realms is unavailable in this build/i);
        assert.ok(!realms.controls.some(e=>/Buy now|Subscribe|Start trial/i.test(e.text)&&!e.disabled));
      });
      await check('Realms keeps native artwork and readable copy at both desktop text sizes',async()=>{
        const measure=`(()=>{
          const image=document.querySelector('main img[src*="realms/javaRealmsUpsell."],main img[src*="realms/bedrockRealmsUpsell."]');
          const row=image.parentElement,copy=image.nextElementSibling,panel=row.parentElement;
          const rect=e=>{const r=e.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height};};
          const textRects=e=>{const range=document.createRange();range.selectNodeContents(e);return [...range.getClientRects()].map(r=>({left:r.left,right:r.right}));};
          return {path:location.pathname,magnified:__host.appData.launcherSettings.useMagnifiedMode,
            selected:!!document.querySelector('[role="tab"][aria-selected="true"][href$="/realms"]'),
            panel:{...rect(panel),clientWidth:panel.clientWidth,scrollWidth:panel.scrollWidth},
            image:{...rect(image),asset:image.getAttribute('src').split('/').pop(),naturalWidth:image.naturalWidth,naturalHeight:image.naturalHeight},
            copy:rect(copy),parts:[...copy.children].map(e=>({...rect(e),font:getComputedStyle(e).font,textRects:textRects(e)})),
            links:[...copy.querySelectorAll('a[href]')].map(e=>({...rect(e),text:e.innerText,href:e.getAttribute('href')}))};
        })()`;
        report.realmsLayouts=[];
        const assertContained=layout=>{
          assert.equal(layout.selected,true);assert.equal(layout.panel.scrollWidth,layout.panel.clientWidth,JSON.stringify(layout));
          for(const part of [layout.image,layout.copy,...layout.links]){
            assert.ok(part.left>=layout.panel.left-1&&part.right<=layout.panel.right+1,JSON.stringify(layout));
          }
          for(const part of layout.parts)for(const text of part.textRects){
            assert.ok(text.left>=layout.panel.left-1&&text.right<=layout.panel.right+1,JSON.stringify(layout));
          }
          assert.equal(layout.links.length,1);assert.match(layout.links[0].text,/Learn more/i);
          assert.ok(Math.abs(layout.image.width/layout.image.height-layout.image.naturalWidth/layout.image.naturalHeight)<0.002);
        };
        try{
          for(const magnified of [false,true]){
            await request('/__launcher_settings',{locale:'en-US',useMagnifiedMode:magnified});
            for(const product of ['java','bedrock']){
              win.setSize(1280,720);await load('/games/'+product+'/realms');
              const wide=await run(measure);assert.equal(wide.magnified,magnified);assertContained(wide);
              const nativeWide=await run(`(()=>{
                const image=document.querySelector('main img[src*="realms/javaRealmsUpsell."],main img[src*="realms/bedrockRealmsUpsell."]');
                const saved=image.style.cssText;image.style.removeProperty('width');image.style.removeProperty('height');
                const result=${measure};image.style.cssText=saved;return result;
              })()`);
              assert.deepEqual(wide,nativeWide);
              win.setSize(1024,640);await sleep(700);
              const small=await run(measure);assertContained(small);
              assert.equal(small.image.naturalWidth,wide.image.naturalWidth);assert.equal(small.image.naturalHeight,wide.image.naturalHeight);
              assert.equal(small.copy.width,420);assert.equal(small.parts[0].font,wide.parts[0].font);assert.equal(small.parts[1].font,wide.parts[1].font);
              report.realmsLayouts.push({product,magnified,wide,small});
              fs.writeFileSync(path.join(output,'realms-contained-'+(magnified?'magnified':'normal')+'-'+product+'-1024.png'),(await win.webContents.capturePage()).toPNG());
              win.setSize(1280,720);await sleep(700);assert.deepEqual(await run(measure),wide);
            }
          }
        }finally{await request('/__launcher_settings',{locale:'en-US',useMagnifiedMode:false});win.setSize(1280,800);}
      });
      await check('licensed fixture does not dispatch unsupported Realms requests',async()=>{
        await run(`(async()=>{window.__realmCalls=[];
          for(const target of [MCLauncher,MCLauncher.realms])for(const name of ['resetRealmsData','getRealmsInvites','getRealmsActivePlayerCounts','getRealmsWorlds','getRealmsWorldData','getRealmsTrialEligibility']){
            const previous=target[name];target[name]=(...args)=>{window.__realmCalls.push(name);return previous(...args)};
          }
          const current=__harness.activeAccount();const licensed={...current,offline:false,type:'Xbox',requiresInteractiveLogin:false,
          entitlements:['game_minecraft'],entitlementsSource:{game_minecraft:'RETAIL'}};const previousFetch=window.fetch;
          window.fetch=(url,opts)=>String(url).startsWith('/__accounts')?Promise.resolve({ok:true,json:async()=>({accounts:[licensed],activeAccount:licensed.id,errors:[],hasSecureStorageAccess:true})}):previousFetch(url,opts);
          await MCLauncher.switchAccount(licensed.id);})()`);
        await sleep(500);
        assert.match(await run('document.body.innerText'),/Realms is unavailable in this build/i);
        assert.equal(await run('__harness.activeAccount().offline'),false);
        assert.equal(await run('__harness.isServiceSupported("realms")'),false);
        assert.deepEqual(await run('__realmCalls'),[]);
        assert.deepEqual(report.realmRequests,[]);
      });
      await check('direct unsupported host actions reject clearly',async()=>{
        const results=await run(`Promise.all([()=>MCLauncher.purchaseStoreProduct(),()=>MCLauncher.products.purchase(),()=>MCLauncher.products.play('bedrock','retail'),()=>MCLauncher.installProduct('bedrock','retail')].map(async invoke=>{try{await invoke();return 'unexpected success'}catch(error){return error.message||String(error)}}))`);
        assert.ok(results.every(message=>/unavailable in this build/i.test(message)),results.join('\n'));
      });
      assert.equal(report.errors.length,0,report.errors.join('\n'));
    } catch(error) {report.fatal=String(error.stack||error);}
    finally {
      fs.writeFileSync(path.join(output,'product-dom-report.json'),JSON.stringify(report,null,2));
      if(win)win.destroy();if(host)host.kill();
      const failed=report.checks.filter(check=>!check.passed);
      console.log(JSON.stringify({checks:report.checks,errors:report.errors,fatal:report.fatal,output},null,2));
      app.exit(failed.length||report.fatal?1:0);
    }
  });
}
