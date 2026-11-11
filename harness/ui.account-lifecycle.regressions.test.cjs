// The real first-run dialog must update the native account slice without a reload.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');

if (!process.versions.electron) {
  const {test} = require('node:test');
  const {execFileSync} = require('node:child_process');
  test('native accounts support first-run setup, selection, logout, and reload', () => {
    const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'launcher-account-lifecycle-'));
    try {
      let result;
      try {
        result = execFileSync(process.execPath, [path.join(__dirname, '../app/node_modules/electron/cli.js'), __filename], {
          encoding: 'utf8', timeout: 90000, env: {...process.env, ACCOUNT_LIFECYCLE_TEMP: temporary},
        });
      } catch (error) {
        process.stdout.write(error.stdout || '');
        throw new Error('Account lifecycle UI regression failed; see .cache/account-login-fix-20261111/report.json');
      }
      assert.equal(JSON.parse(result.trim().split(/\r?\n/).at(-1)).finished, true, 'The renderer must finish the lifecycle checks.');
      process.stdout.write(result);
    } finally {
      assert.equal(path.dirname(path.resolve(temporary)), path.resolve(os.tmpdir()));
      fs.rmSync(temporary, {recursive: true, force: true, maxRetries: 5, retryDelay: 100});
    }
  });
} else {
  const {app, BrowserWindow} = require('electron');
  const {spawn} = require('node:child_process');
  const http = require('node:http');
  const net = require('node:net');
  const root = path.dirname(__dirname);
  const temporary = process.env.ACCOUNT_LIFECYCLE_TEMP;
  assert.ok(temporary);
  const output = path.join(root, '.cache/account-login-fix-20261111');
  fs.mkdirSync(output, {recursive: true});
  app.setPath('userData', path.join(temporary, 'electron'));
  app.on('window-all-closed', () => {});
  app.disableHardwareAcceleration();
  app.commandLine.appendSwitch('disable-renderer-backgrounding');
  app.commandLine.appendSwitch('disable-background-timer-throttling');
  const report = {checks: [], findings: [], errors: [], deniedEffects: [], snapshots: []};
  let host, win, origin;
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const run = source => win.webContents.executeJavaScript(source);
  function request(route) {
    return new Promise((resolve, reject) => {
      http.get(origin + route, response => {
        let data = '';
        response.on('data', chunk => data += chunk);
        response.on('end', () => {
          try {assert.equal(response.statusCode, 200); resolve(JSON.parse(data));}
          catch (error) {reject(error);}
        });
      }).on('error', reject);
    });
  }
  async function ready(expression, message) {
    for (let attempt = 0; attempt < 60; attempt++) {
      if (await run(expression)) return;
      await sleep(100);
    }
    assert.fail(message);
  }
  async function review(name, action) {
    try {await action(); report.checks.push(name);}
    catch (error) {report.findings.push({name, error: error.message});}
  }
  async function keyboard(keyCode) {
    win.webContents.sendInputEvent({type: 'keyDown', keyCode});
    win.webContents.sendInputEvent({type: 'keyUp', keyCode});
    await sleep(150);
  }
  async function closeDialog() {
    await run(`document.querySelector('#__hx-screen .__hx-close').click()`);
    await ready(`!document.querySelector('#__hx-screen')`, 'The account dialog must close.');
  }
  async function buttonVisibility() {
    return run(`(()=>{const visible=e=>!!e&&e.getClientRects().length>0&&getComputedStyle(e).display!=='none'&&getComputedStyle(e).visibility!=='hidden';
      const retry=[...document.querySelectorAll('#__hx-screen button')].find(e=>e.innerText.trim()==='Retry');
      return {retry:visible(retry),save:visible(document.querySelector('#__hx-msa-save')),go:visible(document.querySelector('#__hx-online-go')),
        goDisabled:document.querySelector('#__hx-online-go').disabled,setupOpen:document.querySelector('#__hx-online-setup').open};})()`);
  }
  async function openChooser() {
    await run(`document.querySelector('#AccountSettingsDropdown').click()`);
    await ready(`Array.from(document.querySelectorAll('*')).some(e=>!e.childElementCount&&e.getClientRects().length&&e.textContent.trim()==='LOG OUT')`, 'The native account chooser must open.');
  }
  async function nativeLogout() {
    await openChooser();
    await run(`[...document.querySelectorAll('*')].find(e=>!e.childElementCount&&e.getClientRects().length&&e.textContent.trim()==='LOG OUT').click()`);
  }
  async function snapshot(name) {
    report.snapshots.push(await run(`({name:${JSON.stringify(name)},path:location.pathname,
      text:document.body.innerText,gate:!!document.querySelector('#__hx-first-offline'),
      dialog:!!document.querySelector('#__hx-screen'),
      username:document.querySelector('[data-testid="user-dropdown-username"]')?.innerText,
      activeName:__harness.activeAccount()?.javaProfileName,calls:__calls.slice(-15),
      events:window.__accountLifecycleEvents||[],controls:[...document.querySelectorAll('#__hx-screen button')].map(e=>({
        text:e.innerText,hidden:e.hidden,disabled:e.disabled,display:getComputedStyle(e).display,visible:e.getClientRects().length>0}))})`));
    const capture = await win.webContents.capturePage();
    assert.deepEqual(capture.getSize(), {width: 1266, height: 714});
    fs.writeFileSync(path.join(output, name + '.png'), capture.toPNG());
  }
  app.whenReady().then(async () => {
    let failure;
    try {
      const state = path.join(temporary, 'state'), game = path.join(temporary, 'game'), data = path.join(temporary, 'data');
      for (const folder of [state, game, data]) fs.mkdirSync(folder, {recursive: true});
      fs.writeFileSync(path.join(state, 'launcher_profiles.json'), JSON.stringify({profiles: {}}));
      fs.writeFileSync(path.join(data, 'state.json'), JSON.stringify({UiSettings: '{}', UiEvents: '{}'}));
      const port = await new Promise(resolve => {
        const reservation = net.createServer();
        reservation.listen(0, '127.0.0.1', () => {const port = reservation.address().port; reservation.close(() => resolve(port));});
      });
      origin = 'http://127.0.0.1:' + port;
      host = spawn('python', ['-B', path.join(root, 'harness/server.py'), String(port)], {
        cwd: root, windowsHide: true,
        env: {...process.env, LAUNCHER_ROOT: root, LAUNCHER_STATE_DIR: state, LAUNCHER_GAME_DIR: game,
          LAUNCHER_DATA_DIR: data, LAUNCHER_NO_RELOAD: '1', LAUNCHER_MSA_CLIENT_ID: ''},
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      const log = fs.createWriteStream(path.join(output, 'host.log'));
      host.stdout.pipe(log); host.stderr.pipe(log);
      for (let attempt = 0; attempt < 80; attempt++) {
        try {assert.equal((await request('/__accounts')).accounts.length, 0); break;}
        catch (error) {if (attempt === 79) throw error; await sleep(100);}
      }
      win = new BrowserWindow({width: 1266, height: 714, useContentSize: true, show: false, webPreferences: {
        contextIsolation: true, nodeIntegration: false, sandbox: true, offscreen: true,
        backgroundThrottling: false, partition: 'account-lifecycle-memory',
      }});
      win.webContents.setWindowOpenHandler(() => ({action: 'deny'}));
      win.webContents.session.webRequest.onBeforeRequest((details, callback) => {
        let allowed = false;
        try {
          const url = new URL(details.url);
          const effect = /^\/__(?:launch|install|uninstall|open|dialog|read_file|msa|loaders|modpacks|instances)(?:\/|$)/.test(url.pathname);
          allowed = ['data:', 'blob:', 'file:', 'devtools:', 'chrome:'].includes(url.protocol) || url.origin === origin && (!effect || url.pathname === '/__msa/config');
          if (!allowed) report.deniedEffects.push(url.pathname);
        } catch {}
        callback({cancel: !allowed});
      });
      win.webContents.on('console-message', details => {
        if (details.level === 'error' || details.level === 3) report.errors.push(details.message);
      });
      await win.loadURL(origin + '/');
      await ready(`!!document.querySelector('#__hx-first-offline')`, 'The empty store must show Continue offline.');
      await snapshot('cold-sign-in');
      for (const keyCode of ['Enter', 'Space']) {
        await run(`document.querySelector('[data-hx-online-bound]:not(#__hx-first-offline)').focus()`);
        await keyboard(keyCode);
        await ready(`!!document.querySelector('#__hx-online-setup')&&document.querySelector('#__hx-online-status').innerText.includes('Set up')`, 'Native Microsoft sign-in must open hosted setup without remote authentication.');
        await review('Microsoft setup opens by ' + keyCode + ' with accurate visible buttons.', async () => {
          assert.deepEqual(await buttonVisibility(), {retry: false, save: true, go: true, goDisabled: true, setupOpen: true});
        });
        await snapshot('microsoft-setup-' + keyCode.toLowerCase());
        await run(`document.querySelector('#__hx-online-setup summary').click()`);
        await sleep(100);
        await review('Collapsed setup hides Save for ' + keyCode + '.', async () => {
          assert.deepEqual(await buttonVisibility(), {retry: false, save: false, go: true, goDisabled: true, setupOpen: false});
        });
        await closeDialog();
      }
      await run(`document.querySelector('[data-hx-online-bound]:not(#__hx-first-offline)').click()`);
      await ready(`document.querySelector('#__hx-online-status')?.innerText.includes('Set up')`, 'Microsoft setup must load before saving the public application ID.');
      await run(`document.querySelector('#__hx_msa_client_id').value='11111111-1111-1111-1111-111111111111';document.querySelector('#__hx-msa-save').click()`);
      await ready(`document.querySelector('#__hx-online-status').innerText.includes('Application ID configured')`, 'The public application ID must save to the isolated host.');
      await review('Saved application ID enables sign-in and hides setup-only actions.', async () => {
        assert.deepEqual(await buttonVisibility(), {retry: false, save: false, go: true, goDisabled: false, setupOpen: false});
      });
      await snapshot('microsoft-configured');
      await closeDialog();
      await run(`const originalFetch=window.fetch;window.fetch=(url,options)=>{
        if(String(url)==='/__msa/config'){window.fetch=originalFetch;return Promise.reject(new Error('Fixture configuration unavailable'));}
        return originalFetch(url,options);};document.querySelector('[data-hx-online-bound]:not(#__hx-first-offline)').click()`);
      await ready(`document.querySelector('#__hx_online_error')?.innerText==='Fixture configuration unavailable'`, 'Configuration failure must offer Retry.');
      await review('Failed configuration shows only Retry and disabled sign-in.', async () => {
        assert.deepEqual(await buttonVisibility(), {retry: true, save: false, go: true, goDisabled: true, setupOpen: false});
      });
      await run(`[...document.querySelectorAll('#__hx-screen button')].find(e=>e.innerText.trim()==='Retry').click()`);
      await ready(`document.querySelector('#__hx-online-status').innerText.includes('Application ID configured')`, 'Retry must recover using the real local configuration endpoint.');
      await review('Recovered configuration hides Retry and restores sign-in.', async () => {
        assert.deepEqual(await buttonVisibility(), {retry: false, save: false, go: true, goDisabled: false, setupOpen: false});
      });
      await snapshot('microsoft-retry-recovered');
      await closeDialog();
      await run(`window.__accountLifecycleEvents=[];
        for(const name of ['ACCOUNT_DATA_EVENT',__host.eventNames.users].filter(Boolean))window.addEventListener(name,e=>__accountLifecycleEvents.push({name,detailType:typeof e.detail}));
        document.querySelector('#__hx-first-offline').click();`);
      await ready(`!!document.querySelector('#__hx_offline_name')`, 'Continue offline must open the account dialog.');
      await run(`document.querySelector('#__hx_offline_name').value='FirstRunFixture';
        [...document.querySelectorAll('#__hx-screen button')].find(e=>e.innerText.trim()==='Add account').click();`);
      await ready(`!document.querySelector('#__hx-screen')`, 'The successful offline-account dialog must close.');
      assert.equal((await request('/__accounts')).accounts[0].javaProfileName, 'FirstRunFixture');
      await snapshot('after-account-submit');
      await ready(`!document.querySelector('#__hx-first-offline')&&document.querySelector('[data-testid="user-dropdown-username"]')?.innerText==='FirstRunFixture'`,
        'Creating the first offline account must replace the native sign-in gate with its launcher sidebar.');
      report.checks.push('First-run account submission updates the native launcher.');
      await snapshot('signed-in');
      await win.reload();
      await ready(`document.querySelector('[data-testid="user-dropdown-username"]')?.innerText==='FirstRunFixture'&&!document.querySelector('#__hx-first-offline')`,
        'Reload must retain the selected offline account.');
      await snapshot('signed-in-reload');
      report.checks.push('Reload retains the selected offline account.');
      await win.loadURL(origin + '/settings/accounts');
      await ready(`!!document.querySelector('#__hx-add-offline')`, 'Settings must expose Add offline account.');
      await run(`document.querySelector('#__hx-add-offline').click()`);
      await ready(`!!document.querySelector('#__hx_offline_name')`, 'Settings must open the offline dialog.');
      await run(`document.querySelector('#__hx_offline_name').value='SecondFixture';
        [...document.querySelectorAll('#__hx-screen button')].find(e=>e.innerText.trim()==='Add account').click();`);
      await ready(`document.querySelector('[data-testid="user-dropdown-username"]')?.innerText==='SecondFixture'`, 'The new account must become selected.');
      await openChooser();
      await snapshot('native-account-chooser');
      await run(`[...document.querySelectorAll('*')].filter(e=>!e.childElementCount&&e.getClientRects().length&&e.textContent.trim()==='FirstRunFixture').at(-1).click()`);
      await ready(`document.querySelector('[data-testid="user-dropdown-username"]')?.innerText==='FirstRunFixture'`, 'The native chooser must switch to the saved account.');
      await review('Native chooser persists its selected saved account.', async () => {
        const value = await request('/__accounts');
        assert.equal(value.accounts.find(e=>e.id===value.activeAccount).javaProfileName, 'FirstRunFixture');
      });
      await win.reload();
      await ready(`document.querySelector('[data-testid="user-dropdown-username"]')?.innerText==='FirstRunFixture'`, 'Reload must retain the native chooser selection.');
      await nativeLogout();
      await review('Logging out the selected account falls back to the saved account.', async () => {
        await ready(`document.querySelector('[data-testid="user-dropdown-username"]')?.innerText==='SecondFixture'`, 'Logging out must select the remaining saved account.');
        const value = await request('/__accounts');
        assert.equal(value.accounts.length, 1);
        assert.equal(value.accounts[0].javaProfileName, 'SecondFixture');
      });
      await snapshot('logout-fallback');
      await nativeLogout();
      await ready(`!!document.querySelector('#__hx-first-offline')`, 'Logging out the last account must restore the sign-in gate.');
      assert.equal((await request('/__accounts')).accounts.length, 0);
      report.checks.push('Last-account logout restores the native sign-in gate.');
      await snapshot('last-account-logout');
      assert.deepEqual(report.deniedEffects, []);
      assert.equal(report.findings.length, 0, report.findings.map(e => e.name).join('\n'));
    } catch (error) {
      failure = error;
      report.failure = String(error.stack || error);
      if (win && !win.isDestroyed()) await snapshot('failure').catch(() => {});
    } finally {
      fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
      if (win && !win.isDestroyed()) win.destroy();
      if (host && host.exitCode === null) await new Promise(resolve => {
        host.once('exit', resolve); host.kill(); setTimeout(resolve, 2000);
      });
      process.stdout.write(JSON.stringify({finished: true, checks: report.checks, findings: report.findings.map(e=>e.name), failure: report.failure, output}) + '\n');
      app.exit(failure ? 1 : 0);
    }
  });
}
