// Isolated Electron DOM regressions. No host port, account store or downloads.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

if (!process.versions.electron) {
  const { test } = require('node:test');
  const { execFileSync } = require('node:child_process');
  test('custom controls survive races, keyboard use and localized native markup', () => {
    const cli = path.join(__dirname, '..', 'app', 'node_modules', 'electron', 'cli.js');
    const output = execFileSync(process.execPath, [cli, __filename], {
      encoding: 'utf8', timeout: 45000,
    });
    process.stdout.write(output);
  });
} else {
  const { app, BrowserWindow } = require('electron');
  const os = require('node:os');
  app.setPath('userData', fs.mkdtempSync(path.join(os.tmpdir(), 'launcher-controls-regressions-')));
  const source = fs.readFileSync(path.join(__dirname, 'ui.js'), 'utf8').replace(/\}\)\(\);\s*$/, `
    window.__controls = { injectLoaderPicker, injectFirstRunOfflineButton,
      openOnlineSignIn, injectSkinCapeUpload, openModpackDialog,
      injectUnavailableSettings: typeof injectUnavailableSettings === 'function' ? injectUnavailableSettings : () => {} };
  })();`);
  const loaderMarkup = `<div role="dialog"><form><div id="modal-dialog"><div></div>
    <div class="native-fields"><div><label for="configuration_name">Name</label><input id="configuration_name"></div>
    <div><div><label id="version-select-label">Version</label></div><div role="presentation">
      <div role="button" tabindex="0"><input data-testid="toggle-list-search" aria-labelledby="version-select-label" placeholder="release 1.20.1"></div></div></div>
    <div><label for="configuration_game_dir">Directory</label><div><input id="configuration_game_dir">
      <button type="button"><span>Browse</span></button></div></div></div>
    <footer><button type="button">Cancel</button><button type="submit">Install</button>
      <input type="checkbox" id="toggleLaunchAfterInstallGlyph"></footer></div></form></div>`;
  const capeMarkup = (browse = 'Parcourir') => `<div id="modal-dialog"><form><button id="unrelated">Other action</button>
    <div class="native-fields"><div><label for="skin_name">Nom</label><input name="name" id="skin_name"></div>
    <div><div><label for="skin_drop">Fichier de skin</label><a href="#open-skin-file-info">Help</a></div>
      <div><button class="native-browse" type="button" aria-label="${browse}"><span><span>${browse}</span></span></button></div></div>
    <div><div><label id="cape_label" class="native-label">Cape</label><a href="#open-capes-info">Help</a></div>
      <div role="radiogroup" aria-labelledby="cape_label"></div></div></div></form></div>`;
  const modpackMarkup = `<main><div><form><div><label id="versions_search_label">Search</label><div><input type="text"></div></div></form></div>
    <ul role="list"><li><div>Installation</div><div>1.20.1</div><div><button type="button"><span>Play</span></button>
      <button type="button" id="MoreOptionsDropdown">More</button></div></li></ul></main>`;
  let win;
  const results = [];
  const failures = [];
  const deadline = setTimeout(() => app.exit(2), 35000);
  const run = js => win.webContents.executeJavaScript(js);
  const tick = () => run('new Promise(resolve => setTimeout(resolve, 0))');
  async function fixture(html = loaderMarkup) {
    await win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html));
    await run(`window.__entrySettled = true;
      window.__host = {appData:{gameVersions:{versions:{'1.20.1':{},'1.21.1':{}}},configurationData:{profiles:{}}}};
      window.__requests = [];
      window.fetch = (url, opts) => new Promise((resolve, reject) => __requests.push({url, opts, resolve, reject}));
      ${source}`);
  }
  async function resolve(index, body) {
    await run(`__requests[${index}].resolve({ok:true,json:async()=>(${JSON.stringify(body)})});`);
    await tick();
  }
  async function selectFabric() {
    await run(`__controls.injectLoaderPicker(); document.querySelector('[data-loader="fabric-loader"]').click();`);
    await resolve(0, { versions: ['0.16.5'], recommended: '0.16.5' });
  }
  async function check(name, fn) {
    try { await fn(); results.push({ name, ok: true }); }
    catch (error) { results.push({ name, ok: false, error: String(error.stack || error) }); failures.push(name); }
  }
  app.whenReady().then(async () => {
    win = new BrowserWindow({ show: false, webPreferences: {
      contextIsolation: true, nodeIntegration: false, sandbox: true, backgroundThrottling: false,
    } });
    await check('the latest loader choice wins when earlier versions finish last', async () => {
      await fixture();
      await run(`__controls.injectLoaderPicker(); document.querySelector('[data-loader="fabric-loader"]').click();
        document.querySelector('[data-loader="forge"]').click();`);
      await resolve(1, { versions: ['47.0.1'], recommended: '47.0.1' });
      await resolve(0, { versions: ['0.16.5'], recommended: '0.16.5' });
      assert.deepEqual(await run(`({choice:__hxLoaderChoice,
        versions:Array.from(document.querySelectorAll('[data-loader-version]')).map(n=>n.getAttribute('data-loader-version'))})`),
      { choice: 'forge', versions: ['47.0.1'] });
    });
    await check('an earlier loader failure cannot erase the current loader versions', async () => {
      await fixture();
      await run(`__controls.injectLoaderPicker(); document.querySelector('[data-loader="fabric-loader"]').click();
        document.querySelector('[data-loader="forge"]').click();`);
      await resolve(1, { versions: ['47.0.1'], recommended: '47.0.1' });
      await run(`__requests[0].reject(new Error('old Fabric failure'));`); await tick();
      assert.deepEqual(await run(`({count:document.querySelectorAll('[data-loader-version]').length,
        staleError:document.querySelector('#__hx-loader-version').textContent.includes('old Fabric failure')})`),
      { count: 1, staleError: false });
    });
    await check('changing Minecraft refreshes the active loader and clears its old build', async () => {
      await fixture(); await selectFabric();
      await run(`document.querySelector('[data-testid="toggle-list-search"]').placeholder='release 1.21.1';`); await tick();
      assert.deepEqual(await run(`({count:__requests.length,url:__requests[1]?.url,
        rows:document.querySelectorAll('[data-loader-version]').length})`),
      { count: 2, url: '/__loaders/versions?minecraft=1.21.1&loader=fabric-loader', rows: 0 });
      await resolve(1, { versions: ['0.17.0'], recommended: '0.17.0' });
      assert.equal(await run(`document.querySelector('#__hx-loader-version input').placeholder`), '0.17.0 (recommended)');
    });
    await check('a completed preparation cannot submit a changed loader choice', async () => {
      await fixture(); await selectFabric();
      await run(`window.__submitted=0; document.querySelector('button[type="submit"]').addEventListener('click',()=>__submitted++);
        document.querySelector('button[type="submit"]').click(); document.querySelector('[data-loader="forge"]').click();`);
      await resolve(1, { versionId: 'fabric-loader-0.16.5-1.20.1' });
      assert.deepEqual(await run(`({armed:__hxArmedLoader,clicks:__submitted,disabled:document.querySelector('button[type="submit"]').disabled})`),
      { armed: null, clicks: 0, disabled: false });
    });
    await check('a completed preparation cannot alter a replacement form', async () => {
      await fixture(); await selectFabric();
      await run(`window.__submitted=0; document.querySelector('button[type="submit"]').addEventListener('click',()=>__submitted++);
        document.querySelector('button[type="submit"]').click(); document.querySelector('[role="dialog"]').remove();
        document.body.insertAdjacentHTML('beforeend',${JSON.stringify(loaderMarkup)}); __controls.injectLoaderPicker();`);
      await resolve(1, { versionId: 'fabric-loader-0.16.5-1.20.1' });
      assert.deepEqual(await run(`({armed:__hxArmedLoader,choice:__hxLoaderChoice,clicks:__submitted})`),
      { armed: null, choice: null, clicks: 0 });
    });
    await check('repeated submits share one active loader preparation', async () => {
      await fixture(); await selectFabric();
      await run(`const form=document.querySelector('form');
        form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));
        form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));`);
      assert.equal(await run(`__requests.filter(r=>r.url==='/__loaders/materialize').length`), 1);
    });
    await check('saving during loader version loading waits without starting preparation', async () => {
      await fixture();
      await run(`__controls.injectLoaderPicker(); document.querySelector('[data-loader="fabric-loader"]').click();
        document.querySelector('button[type="submit"]').click();`);
      assert.deepEqual(await run(`({requests:__requests.length,disabled:document.querySelector('button[type="submit"]').disabled})`),
      { requests: 1, disabled: false });
      await resolve(0, { versions: ['0.16.5'], recommended: '0.16.5' });
      await run(`document.querySelector('button[type="submit"]').click();`);
      assert.equal(await run(`__requests[1]?.url`), '/__loaders/materialize');
    });
    await check('an empty compatible-version list cannot submit an unavailable loader', async () => {
      await fixture();
      await run(`__controls.injectLoaderPicker(); document.querySelector('[data-loader="forge"]').click();`);
      await resolve(0, { versions: [], recommended: null });
      await run(`document.querySelector('button[type="submit"]').click();`);
      assert.deepEqual(await run(`({requests:__requests.length,disabled:document.querySelector('button[type="submit"]').disabled})`),
      { requests: 1, disabled: false });
    });
    await check('editing an installed Forge profile loads its base Minecraft and saves unchanged', async () => {
      await fixture(`<ul><li data-configuration-id="forge-profile"><button id="MoreOptionsDropdown">More</button></li></ul>
        <ul id="MoreOptionsDropdownMenu"><li role="menuitem">Edit</li></ul>${loaderMarkup}`);
      await run(`__host.appData.configurationData.profiles={'forge-profile':{name:'Forge',lastVersionId:'forge-1.20.1-47.0.1'}};
        document.querySelector('#MoreOptionsDropdown').click(); document.querySelector('[role="menuitem"]').click();
        document.querySelector('#toggleLaunchAfterInstallGlyph').remove();
        document.querySelector('[data-testid="toggle-list-search"]').placeholder='forge-1.20.1-47.0.1';
        __controls.injectLoaderPicker();`);
      await resolve(0, { versions: ['47.0.1'], recommended: '47.0.1' });
      assert.equal(await run(`document.querySelector('#__hx-loader-version input').placeholder`), '47.0.1 (recommended)');
      await run(`document.querySelector('button[type="submit"]').click();`);
      assert.equal(await run(`__requests.length`), 1);
    });
    for (const [name, metadata, versionMetadata] of [
      ['imported NeoForge uses pack Minecraft metadata before the installed-version fallback',
        {minecraft:'1.21.1'}, {minecraftVersion:'1.20.1'}],
      ['manual NeoForge uses its installed version Minecraft metadata', {}, {minecraftVersion:'1.21.1'}],
    ]) {
      await check(name, async () => {
        await fixture(`<ul><li data-configuration-id="neo-profile"><button id="MoreOptionsDropdown">More</button></li></ul>
          <ul id="MoreOptionsDropdownMenu"><li role="menuitem">Edit</li></ul>${loaderMarkup}`);
        await run(`__host.appData.configurationData.profiles={'neo-profile':{name:'NeoForge',lastVersionId:'neoforge-21.1.2',modpack:${JSON.stringify(metadata)}}};
          __host.appData.gameVersions.versions['neoforge-21.1.2']=${JSON.stringify(versionMetadata)};
          document.querySelector('#MoreOptionsDropdown').click(); document.querySelector('[role="menuitem"]').click();
          document.querySelector('#toggleLaunchAfterInstallGlyph').remove();
          document.querySelector('[data-testid="toggle-list-search"]').placeholder='neoforge-21.1.2';
          __controls.injectLoaderPicker();`);
        assert.equal(await run(`__requests[0]?.url`), '/__loaders/versions?minecraft=1.21.1&loader=neoforge');
        await resolve(0, {versions:['21.1.2'],recommended:'21.1.2'});
        assert.equal(await run(`document.querySelector('#__hx-loader-version input').placeholder`), '21.1.2 (recommended)');
        await run(`document.querySelector('button[type="submit"]').click();`);
        assert.equal(await run(`__requests.length`), 1);
      });
    }
    await check('keyboard opens the loader menu, moves options and dismisses only the menu', async () => {
      await fixture(); await selectFabric();
      const out = await run(`(() => {
        const trigger=document.querySelector('#__hx-loader-version [role="button"]'); trigger.focus();
        trigger.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));
        const opened=trigger.getAttribute('aria-expanded')==='true';
        trigger.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true,cancelable:true}));
        const optionFocus=document.activeElement===document.querySelector('[data-loader-version] button');
        document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
        return {opened,optionFocus,closed:trigger.getAttribute('aria-expanded')==='false',
          restored:document.activeElement===trigger,formPresent:!!document.querySelector('#modal-dialog')};
      })()`);
      assert.deepEqual(out, { opened: true, optionFocus: true, closed: true, restored: true, formPresent: true });
    });
    await check('the first loader menu keeps native spacing and keyboard focus before template capture', async () => {
      await fixture(loaderMarkup.replace('role="presentation"', 'role="presentation" style="width:280px;font-family:sans-serif"'));
      await selectFabric();
      win.webContents.focus();
      const out = await run(`(async () => {
        const trigger=document.querySelector('#__hx-loader-version [role="button"]');
        trigger.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));
        const list=document.querySelector('#__hx-loader-version ul');
        const panel=getComputedStyle(list.parentElement.parentElement);
        const listStyle=getComputedStyle(list), button=list.querySelector('button');
        const rowStyle=getComputedStyle(button.parentElement);
        const unfocused=getComputedStyle(button);
        const base={background:unfocused.backgroundColor,border:unfocused.borderTopColor,
          font:unfocused.fontFamily,fontSize:unfocused.fontSize};
        trigger.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true,cancelable:true}));
        // Wait for the native 120ms color transitions to finish painting.
        // A hidden window's timer can fire before its compositor advances.
        for(let attempt=0;attempt<80;attempt++) {
          const state=getComputedStyle(button);
          if(state.backgroundColor==='rgb(81, 77, 76)'&&state.borderTopColor==='rgb(30, 110, 234)')break;
          await new Promise(resolve=>setTimeout(resolve,25));
        }
        const focused=getComputedStyle(button);
        return {nativeTemplatePresent:!!document.querySelector('#toggle-list-menu'),
          width:panel.width,panelBorder:panel.borderTopColor,gap:listStyle.gap,
          direction:listStyle.flexDirection,row:{margin:rowStyle.margin,padding:rowStyle.padding,
            listStyle:rowStyle.listStyleType},base,focused:{background:focused.backgroundColor,
            border:focused.borderTopColor,outline:focused.outlineColor,offset:focused.outlineOffset}};
      })()`);
      assert.deepEqual(out, {nativeTemplatePresent:false,width:'274px',panelBorder:'rgb(60, 133, 39)',
        gap:'4px',direction:'column',row:{margin:'0px',padding:'0px',listStyle:'none'},
        base:{background:'rgb(49, 45, 44)',border:'rgb(49, 45, 44)',font:'Arial',fontSize:'13.3333px'},
        focused:{background:'rgb(81, 77, 76)',border:'rgb(30, 110, 234)',outline:'rgb(255, 255, 255)',offset:'1px'}});
    });
    await check('real Enter and Space presses open the first loader menu without submitting', async () => {
      for (const keyCode of ['Enter', 'Space']) {
        await fixture(); await selectFabric(); win.webContents.focus();
        await run(`window.__submitted=0; document.querySelector('form').addEventListener('submit',event=>{event.preventDefault();__submitted++});
          document.querySelector('#__hx-loader-version [role="button"]').focus();`);
        win.webContents.sendInputEvent({type:'keyDown',keyCode});
        win.webContents.sendInputEvent({type:'keyUp',keyCode});
        await tick();
        assert.deepEqual(await run(`({open:document.querySelector('#__hx-loader-version [role="button"]').getAttribute('aria-expanded'),
          submitted:__submitted,requests:__requests.length})`), {open:'true',submitted:0,requests:1});
      }
    });
    await check('first-run Microsoft action opens the hosted sign-in dialog', async () => {
      await fixture(`<div><button id="native-sign-in"><span>Sign in with Microsoft</span></button>
        <a href="https://sisu.xboxlive.com/connect/XboxLive/?state=signup">Sign up</a></div>`);
      await run(`window.__nativeSignIn=0; document.querySelector('#native-sign-in').addEventListener('click',()=>__nativeSignIn++);
        __controls.injectFirstRunOfflineButton(); document.querySelector('#native-sign-in').click();`);
      assert.deepEqual(await run(`({native:__nativeSignIn, dialog:!!document.querySelector('#__hx-online-go')})`),
        {native:0,dialog:true});
    });
    await check('missing Microsoft registration exposes public ID setup and saves before sign-in', async () => {
      await fixture('<button id="trigger">Sign in</button>');
      await run(`window.__signIns=0; window.__harness={microsoftSignIn:async()=>__signIns++}; __controls.openOnlineSignIn();`);
      assert.equal(await run(`__requests[0]?.url`), '/__msa/config');
      await resolve(0, {configured:false,clientId:null,source:null,environmentOverride:false});
      assert.deepEqual(await run(`({setup:!document.querySelector('#__hx-online-setup').hidden,
        disabled:document.querySelector('#__hx-online-go').disabled,signIns:__signIns})`),
        {setup:true,disabled:true,signIns:0});
      await run(`document.querySelector('#__hx_msa_client_id').value='11111111-2222-4333-8444-555555555555';
        document.querySelector('#__hx_msa_client_id').focus();`);
      win.webContents.sendInputEvent({type:'keyDown',keyCode:'Enter'});
      win.webContents.sendInputEvent({type:'keyUp',keyCode:'Enter'});
      await tick();
      assert.equal(await run(`__requests.length`), 2, 'Enter saves the application ID before sign-in');
      assert.deepEqual(await run(`({url:__requests[1].url,method:__requests[1].opts.method,
        body:JSON.parse(__requests[1].opts.body)})`),
        {url:'/__msa/config',method:'POST',body:{clientId:'11111111-2222-4333-8444-555555555555'}});
      await resolve(1, {configured:true,clientId:'11111111-2222-4333-8444-555555555555',source:'file',environmentOverride:false});
      assert.equal(await run(`document.querySelector('#__hx-online-go').disabled`), false);
    });
    await check('Microsoft setup failure preserves input and permits retry', async () => {
      await fixture('<button id="trigger">Sign in</button>');
      await run(`__controls.openOnlineSignIn();`);
      assert.equal(await run(`__requests[0]?.url`), '/__msa/config');
      await resolve(0, {configured:false,clientId:null,source:null,environmentOverride:false});
      await run(`document.querySelector('#__hx_msa_client_id').value='11111111-2222-4333-8444-555555555555';
        document.querySelector('#__hx-msa-save').click();
        __requests[1].resolve({ok:false,json:async()=>({error:'Could not save configuration'})});`);
      await tick();
      assert.deepEqual(await run(`({value:document.querySelector('#__hx_msa_client_id').value,
        retry:!document.querySelector('#__hx-msa-save').disabled,signIn:!document.querySelector('#__hx-online-go').disabled,
        error:document.querySelector('#__hx_online_error').textContent})`),
        {value:'11111111-2222-4333-8444-555555555555',retry:true,signIn:false,error:'Could not save configuration'});
    });
    await check('sign-in keeps its raised label through waiting and failure', async () => {
      await fixture('<button id="trigger">Sign in</button>');
      await run(`window.__harness={microsoftSignIn:()=>new Promise((resolve,reject)=>__requests.push({resolve,reject}))};
        __controls.openOnlineSignIn();`);
      await resolve(0, {configured:true,clientId:'11111111-2222-4333-8444-555555555555',source:'file',environmentOverride:false});
      await run(`document.querySelector('#__hx-online-go').click();`);
      assert.equal(await run(`document.querySelector('#__hx-online-go > span')?.textContent`), 'Waiting for your browser…');
      await run(`__requests[1].reject(new Error('fixture failure'));`); await tick();
      assert.deepEqual(await run(`({label:document.querySelector('#__hx-online-go > span')?.textContent,
        enabled:!document.querySelector('#__hx-online-go').disabled,
        description:document.querySelector('#__hx-online-go').getAttribute('aria-describedby'),
        errorRole:document.querySelector('#__hx_online_error').getAttribute('role'),
        error:document.querySelector('#__hx_online_error').textContent})`),
      { label: 'Sign in with Microsoft', enabled: true, description:'__hx_online_error',
        errorRole:'alert', error:'fixture failure' });
    });
    await check('Continue offline retains the native CTA slabs, unique IDs and native label styling', async () => {
      await fixture(`<style>.native-cta{display:flex;color:black;background:transparent}.native-cta .container{color:white;background:#3c8527;font:700 18px Arial}.native-cta:focus .container{background:#2a641c}</style>
        <div><button class="native-cta" type="button"><div id="cta-container" class="container">Sign in with Microsoft</div><div id="cta-overlap" class="overlap"></div></button>
        <a href="https://sisu.xboxlive.com/connect/XboxLive/?state=signup">Sign up</a></div>`);
      const out = await run(`(() => {
        __controls.injectFirstRunOfflineButton(); const button=document.querySelector('#__hx-first-offline');
        const face=button.querySelector('.container');
        return {label:face?.textContent,overlap:!!button.querySelector('.overlap'),
          duplicateIds:document.querySelectorAll('#cta-container').length,
          color:face && getComputedStyle(face).color,background:face && getComputedStyle(face).backgroundColor};
      })()`);
      assert.deepEqual(out, { label: 'Continue offline', overlap: true, duplicateIds: 1,
        color: 'rgb(255, 255, 255)', background: 'rgb(60, 133, 39)' });
    });
    await check('French cape upload uses its native file action and retains the styled heading', async () => {
      await fixture(capeMarkup());
      const out = await run(`(() => {__controls.injectSkinCapeUpload();
        return {present:!!document.querySelector('#__hx-cape-upload'),
          heading:document.querySelector('#__hx-cape-upload > div .native-label')?.textContent,
          nativeAction:document.querySelector('#__hx-cape-browse')?.classList.contains('native-browse'),
          saveName:document.querySelector('#__hx-cape-save')?.getAttribute('aria-label'),
          labels:document.querySelectorAll('#cape_label').length};})()`);
      assert.deepEqual(out, { present: true, heading: 'Upload a cape', nativeAction: true, saveName: 'Save cape', labels: 1 });
    });
    await check('cape browse failures reset the success tone', async () => {
      await fixture(capeMarkup('Browse'));
      await run(`__controls.injectSkinCapeUpload(); const status=document.querySelector('#__hx-cape-upload .__hx-error');
        status.style.color='#86d562'; status.textContent='Previous success'; document.querySelector('#__hx-cape-browse').click();`);
      await run(`__requests[0].reject(new Error('fixture read failure'));`); await tick();
      assert.deepEqual(await run(`({text:document.querySelector('#__hx-cape-upload .__hx-error').textContent,
        color:document.querySelector('#__hx-cape-upload .__hx-error').style.color})`),
      { text: 'fixture read failure', color: '' });
    });
    await check('an active modpack import says Close and resumes visible progress when reopened', async () => {
      await fixture(modpackMarkup);
      await run(`window.__modpackStatus={job:null}; window.fetch=async url=>({json:async()=>{
        if(String(url).startsWith('/__modpacks/search')) return {hits:[{id:'fixture',title:'Fixture',author:'Fixture'}],total:1};
        if(url==='/__modpacks/import') {__modpackStatus={job:'import',message:'Importing fixture',done:1,total:3};return {ok:true};}
        return __modpackStatus;
      }}); __controls.openModpackDialog();`); await tick();
      await run(`document.querySelector('#__hx-mod-results button').click();`); await tick();
      assert.deepEqual(await run(`({label:document.querySelector('.__hx-foot button span').textContent,
        note:document.querySelector('#__hx-import-background')?.textContent,
        visible:document.querySelector('#__hx-import-background')?.hidden===false})`),
      { label: 'Close', note: 'Import continues if you close this window.', visible: true });
      await run(`document.querySelector('.__hx-close').click(); __controls.openModpackDialog();`); await tick();
      assert.deepEqual(await run(`({label:document.querySelector('.__hx-foot button span').textContent,
        progress:document.querySelector('#__hx-mod-status').textContent,
        disabled:document.querySelector('#__hx-mod-results button').disabled})`),
      { label: 'Close', progress: 'Importing fixture (1/3)', disabled: true });
      await run(`__modpackStatus={job:null,phase:'done',result:{name:'Fixture',files:1,bytes:10}};
        new Promise(resolve=>setTimeout(resolve,450));`);
      assert.deepEqual(await run(`({label:document.querySelector('.__hx-foot button span').textContent,
        hidden:document.querySelector('#__hx-import-background').hidden})`),
      { label: 'Cancel', hidden: true });
    });
    await check('working local settings preserve native changes and explain background play and local crash help', async () => {
      await fixture(`<main><section><div class="native-checkbox"><input type="checkbox" id="keepLauncherOpen" checked aria-describedby="existing-help">
        <label for="keepLauncherOpen">Garder le launcher ouvert</label></div><p id="existing-help">Existing help</p>
        <div class="native-checkbox"><input type="checkbox" id="crashAssistance"><label for="crashAssistance">Envoyer les rapports</label></div>
        <div class="native-checkbox"><input type="checkbox" id="disableHardwareRendering"><label for="disableHardwareRendering">Accélération matérielle</label></div></section></main>`);
      const out = await run(`(() => {
        window.__settingChanges=0; document.querySelectorAll('input').forEach(input=>input.addEventListener('change',()=>__settingChanges++));
        __controls.injectUnavailableSettings(); __controls.injectUnavailableSettings();
        const keep=document.querySelector('#keepLauncherOpen'), crash=document.querySelector('#crashAssistance'); keep.click(); crash.click();
        const describe=input=>(input.getAttribute('aria-describedby')||'').split(/\\s+/).map(id=>document.getElementById(id)?.textContent);
        return {checked:[keep.checked,crash.checked],disabled:[keep.disabled,crash.disabled],changes:__settingChanges,
          keepHelp:describe(keep),crashHelp:describe(crash),notes:document.querySelectorAll('[data-hx-setting-note]').length,
          hardwareEnabled:!document.querySelector('#disableHardwareRendering').disabled};
      })()`);
      assert.deepEqual(out, {checked:[false,true],disabled:[false,false],changes:2,
        keepHelp:['Existing help','Closing the launcher keeps your game running in the background. Use the tray icon to reopen it. This setting keeps the window visible after launch.'],
        crashHelp:['Read explanations from this installation’s logs in Logs. Analysis stays on this computer; crash reports are never uploaded.'],notes:2,hardwareEnabled:true});
    });
    console.log(JSON.stringify({ mode: 'electron-controls', tests: results.length, failures, results }, null, 2));
    clearTimeout(deadline); win.destroy(); app.exit(failures.length ? 1 : 0);
  }).catch(error => { console.error(error.stack || error); clearTimeout(deadline); app.exit(2); });
}
