// Pure contracts: node --test harness/ui.regressions.test.cjs
// Real DOM fixtures: app/node_modules/.bin/electron harness/ui.regressions.test.cjs
// The Electron mode uses data URLs and synthetic markup; no host port is used.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, 'ui.js'), 'utf8');
const exposed = source.replace(/\}\)\(\);\s*$/, `
  window.__uiRegression = { screen, closeScreen, profileForRow, baseMinecraftVersion,
    injectOfflineAccountButton, injectOnlineSignInButton, injectModpackButton,
    injectLoaderPicker, openModpackDialog, scan };
})();`);

function scope(profiles, latest = {}) {
  const document = { readyState: 'loading', addEventListener() {},
    documentElement: {}, querySelector() { return null; } };
  const window = { addEventListener() {}, __host: { appData: {
    configurationData: { profiles }, gameVersions: { latest, versions: {
      '1.20.1': {}, '1.21.1': {}, '26.3': {}, '26.3-snapshot-1': {},
    } },
  } } };
  const sandbox = { document, window, MutationObserver: class { observe() {} },
    setTimeout() {}, console };
  vm.createContext(sandbox);
  vm.runInContext(exposed, sandbox);
  return window.__uiRegression;
}

function row(name, version, id = null) {
  return {
    textContent: name + ' ' + version + ' Jouer',
    getAttribute(key) { return key === 'data-configuration-id' ? id : null; },
    querySelector() { return null; },
    querySelectorAll(selector) {
      return selector === 'div' ? [{ children: [], textContent: name },
        { children: [], textContent: version }] : [];
    },
  };
}

if (!process.versions.electron) {
  const test = require('node:test');
  test('same-name installation is selected by its resolved version', () => {
    const api = scope({ first: { name: 'Shared', lastVersionId: '1.20.1' },
      second: { name: 'Shared', lastVersionId: '1.21.1' } });
    assert.equal(api.profileForRow(row('Shared', '1.21.1')).id, 'second');
  });
  test('indistinguishable installation rows refuse an ambiguous selection', () => {
    const api = scope({ first: { name: 'Shared', lastVersionId: '1.21.1' },
      second: { name: 'Shared', lastVersionId: '1.21.1' } });
    assert.equal(api.profileForRow(row('Shared', '1.21.1')), null);
  });
  test('explicit row metadata identifies an otherwise identical installation', () => {
    const api = scope({ first: { name: 'Shared', lastVersionId: '1.21.1' },
      second: { name: 'Shared', lastVersionId: '1.21.1' } });
    assert.equal(api.profileForRow(row('Shared', '1.21.1', 'second')).id, 'second');
  });
  test('stale explicit row metadata cannot fall back to a different installation', () => {
    const api = scope({ first: { name: 'Shared', lastVersionId: '1.21.1' } });
    assert.equal(api.profileForRow(row('Shared', '1.21.1', 'deleted')), null);
  });
  test('a localized default installation resolves through latest-version metadata', () => {
    const api = scope({ stable: { type: 'latest-release', lastVersionId: 'latest-release' },
      preview: { type: 'latest-snapshot', lastVersionId: 'latest-snapshot' } },
    { release: '26.3', snapshot: '26.3-snapshot-1' });
    const profile = api.profileForRow(row('Dernière version', '26.3'));
    assert.ok(profile, 'localized default installation was not identified');
    assert.equal(profile.id, 'stable');
  });
  test('localized selected-version copy resolves to the concrete version id', () => {
    const api = scope({}, { release: '26.3' });
    const dialog = { querySelector() { return { placeholder: 'Dernière version (26.3)' }; } };
    assert.equal(api.baseMinecraftVersion(dialog), '26.3');
  });
} else {
  const { app, BrowserWindow } = require('electron');
  const os = require('node:os');
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'launcher-ui-regressions-'));
  app.setPath('userData', folder);
  app.commandLine.appendSwitch('disable-renderer-backgrounding');
  let win;
  const results = [];
  const failures = [];
  const deadline = setTimeout(() => app.exit(2), 30000);
  async function fixture(html) {
    await win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html));
    await win.webContents.executeJavaScript(`
      window.__entrySettled = true;
      window.__host = {appData: {configurationData: {profiles: {}}}};
      window.__harness = {accountAddOffline: async () => ({})};
      ${exposed}
    `);
  }
  async function check(name, fn) {
    try { await fn(); results.push({ name, ok: true }); }
    catch (error) { results.push({ name, ok: false, error: String(error.stack || error) }); failures.push(name); }
  }
  const accountMarkup = `
    <section id="microsoftAccountSection"><img alt="Microsoft logotype">
    <ul role="listbox"><li><button>Supprimer le compte</button></li></ul>
    <div class="native-action"><button type="button"><span>Ajouter un compte Microsoft</span></button></div></section>`;
  const installationMarkup = `<main><div role="region"><div class="native-column">
    <div class="native-filter"><form><div class="native-search"><label id="versions_search_label">Recherche</label><input aria-label="Rechercher une installation"></div>
    <div class="native-checkbox"><input type="checkbox" id="filter-release" aria-label="Versions publiées"><label for="filter-release">Versions publiées</label></div>
    <input type="checkbox" id="filter-snapshot"></form></div>
    <li class="native-toolbar"><div><button type="button"><span>Nouvelle configuration</span></button></div></li>
    <ul role="list"><li><div>Installation test</div><div>26.3</div><div>
    <button type="button"><span>Jouer</span></button><button type="button">Ouvrir</button>
    <button type="button" id="MoreOptionsDropdown" aria-label="Plus d’options">...</button></div></li></ul></div></div></main>`;
  const loaderMarkup = `<div role="dialog"><form><div id="modal-dialog"><div class="native-icon"></div>
    <div class="native-fields"><div><label for="configuration_name">Nom</label><input id="configuration_name"></div>
    <div><div><label id="version-select-label">Version du jeu</label></div>
    <div role="presentation"><div role="button" tabindex="0"><input data-testid="toggle-list-search" aria-labelledby="version-select-label" placeholder="Dernière version (26.3)"></div></div></div>
    <div><label for="configuration_game_dir">Répertoire du jeu</label><div><input id="configuration_game_dir"><button type="button" aria-label="Parcourir"><span>Parcourir</span></button></div></div></div>
    <footer><button type="button">Annuler</button><button type="submit">Installer</button><input type="checkbox" id="toggleLaunchAfterInstallGlyph"></footer>
    </div></form></div>`;
  app.whenReady().then(async () => {
    win = new BrowserWindow({ show: false, webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true } });
    await check('French account actions clone the native action rather than account-row buttons', async () => {
      await fixture(accountMarkup);
      const out = await win.webContents.executeJavaScript(`(() => {
        __uiRegression.injectOfflineAccountButton(); __uiRegression.injectOnlineSignInButton();
        __uiRegression.injectOfflineAccountButton(); __uiRegression.injectOnlineSignInButton();
        return {offline: document.querySelectorAll('#__hx-add-offline').length,
          online: document.querySelectorAll('#__hx-add-online').length,
          native: document.querySelector('.native-action button').textContent};
      })()`);
      assert.deepEqual(out, { offline: 1, online: 1, native: 'Ajouter un compte Microsoft' });
    });
    await check('French installation toolbar keeps Import modpack', async () => {
      await fixture(installationMarkup);
      const out = await win.webContents.executeJavaScript(`(() => {
        __uiRegression.injectModpackButton(); __uiRegression.injectModpackButton();
        return {count: document.querySelectorAll('#__hx-modpacks').length,
          toolbar: !!document.querySelector('.native-toolbar #__hx-modpacks')};
      })()`);
      assert.deepEqual(out, { count: 1, toolbar: true });
    });
    await check('French import dialog renders catalog hits with native search and source controls', async () => {
      await fixture(installationMarkup);
      const out = await win.webContents.executeJavaScript(`(async () => {
        window.fetch=async url => ({json: async () => String(url).startsWith('/__modpacks/search')
          ? {hits:[{id:'fixture-pack',title:'Fixture modpack',author:'Fixture',downloads:42}],total:1}
          : {job:null}});
        __uiRegression.openModpackDialog();
        await new Promise(resolve => setTimeout(resolve, 20));
        return {results: document.querySelectorAll('#__hx-mod-results > li').length,
          source: !!document.querySelector('#__hx-src-modrinth'),
          nativeSearch: !!document.querySelector('.native-search #__hx-modpack-search'),
          action: document.querySelector('#__hx-mod-results button')?.textContent || null};
      })()`);
      assert.deepEqual(out, {results: 1, source: true, nativeSearch: true, action: 'Import'});
    });
    await check('French installation form receives loader choices without English labels', async () => {
      await fixture(loaderMarkup);
      const out = await win.webContents.executeJavaScript(`(() => {
        __uiRegression.injectLoaderPicker();
        const loader=document.querySelector('#__hx-loader-version input');
        const label=loader && loader.getAttribute('aria-labelledby');
        return {choices: [...document.querySelectorAll('#__hx-loader-row [data-loader]')].map(e => e.getAttribute('aria-label') || e.textContent),
          loaderName: label ? document.getElementById(label).textContent : loader && loader.getAttribute('aria-label')};
      })()`);
      assert.deepEqual(out, {choices: ['Vanilla', 'Fabric', 'Quilt', 'Forge', 'NeoForge'], loaderName: 'Loader version'});
    });
    await check('first-run native sign-in surface exposes Continue offline', async () => {
      await fixture(`<div id="first-run"><button type="button"><span>Se connecter avec Microsoft</span></button>
        <a href="https://sisu.xboxlive.com/connect/XboxLive/?state=signup&amp;signup=1">Inscription</a></div>`);
      await new Promise(resolve => setTimeout(resolve, 300));
      const out = await win.webContents.executeJavaScript(`(() => {
        const button=document.querySelector('#__hx-first-offline');
        if(button) button.click();
        return {present: !!button, text: button && button.textContent,
          dialog: !!document.querySelector('#__hx-screen')};
      })()`);
      assert.deepEqual(out, { present: true, text: 'Continue offline', dialog: true });
    });
    await check('custom dialog has an accessible name and focuses its content', async () => {
      await fixture('<button id="trigger">Open</button>');
      const out = await win.webContents.executeJavaScript(`(async () => {
        document.querySelector('#trigger').focus();
        const screen = __uiRegression.screen('Fixture dialog');
        screen.add('<label for="fixture-input">Fixture input</label><input id="fixture-input">');
        screen.button('Cancel', false, screen.close); screen.button('Save', true, () => {});
        await Promise.resolve();
        const labelledby=screen.el.getAttribute('aria-labelledby');
        return {name: labelledby ? document.getElementById(labelledby).textContent : screen.el.getAttribute('aria-label'),
          focus: document.activeElement.id};
      })()`);
      assert.deepEqual(out, { name: 'Fixture dialog', focus: 'fixture-input' });
    });
    await check('dialog traps Tab and Shift+Tab while excluding hidden and disabled controls', async () => {
      const out = await win.webContents.executeJavaScript(`(() => {
        const dialog=document.querySelector('#__hx-screen');
        const hidden=document.createElement('button'); hidden.style.display='none'; dialog.appendChild(hidden);
        const disabled=document.createElement('button'); disabled.disabled=true; dialog.appendChild(disabled);
        const first=dialog.querySelector('.__hx-close'), last=dialog.querySelector('.__hx-primary');
        last.focus(); const tab=new KeyboardEvent('keydown',{key:'Tab',bubbles:true,cancelable:true});
        last.dispatchEvent(tab); const forward=document.activeElement===first && tab.defaultPrevented;
        first.focus(); const back=new KeyboardEvent('keydown',{key:'Tab',shiftKey:true,bubbles:true,cancelable:true});
        first.dispatchEvent(back); const reverse=document.activeElement===last && back.defaultPrevented;
        return {forward,reverse};
      })()`);
      assert.deepEqual(out, { forward: true, reverse: true });
    });
    await check('Escape closes the dialog and restores focus and background interactivity', async () => {
      const out = await win.webContents.executeJavaScript(`(() => {
        const trigger=document.querySelector('#trigger'), locked=trigger.inert;
        const escape=new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true});
        document.activeElement.dispatchEvent(escape);
        return {locked, closed: !document.querySelector('#__hx-screen'),
          restored: document.activeElement===trigger, inert: trigger.inert, prevented: escape.defaultPrevented};
      })()`);
      assert.deepEqual(out, { locked: true, closed: true, restored: true, inert: false, prevented: true });
    });
    await check('a removed dialog cannot close a later replacement', async () => {
      await fixture('<button id="trigger">Open</button>');
      const out = await win.webContents.executeJavaScript(`(() => {
        const old=__uiRegression.screen('Old dialog'); old.close();
        const current=__uiRegression.screen('Current dialog'); old.close();
        return current.el.isConnected;
      })()`);
      assert.equal(out, true);
    });
    console.log(JSON.stringify({ mode: 'electron-dom', tests: results.length, failures, results }, null, 2));
    clearTimeout(deadline); win.destroy(); app.exit(failures.length ? 1 : 0);
  }).catch(error => { console.error(error.stack || error); clearTimeout(deadline); app.exit(2); });
}
