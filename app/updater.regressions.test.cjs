const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const {EventEmitter} = require('node:events');

function implementation() {
  const file = path.join(__dirname, 'updater.js');
  assert.ok(fs.existsSync(file), 'The launcher has no update controller');
  return require(file);
}

function fixture(t, options = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'launcher-updater-test-'));
  t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
  const bytes = Buffer.from('disposable test installer, never executable');
  const info = {version: '3.41.31', files: [{url: 'Minecraft-Launcher-Setup-3.41.31.exe',
    size: bytes.length, sha512: crypto.createHash('sha512').update(bytes).digest('base64')}]};
  const engine = new EventEmitter();
  Object.assign(engine, {autoInstallOnAppQuit: true, checkCalls: 0, installCalls: 0,
    setFeedURL(value) {this.feed = value;},
    async checkForUpdates() {this.checkCalls++; return {isUpdateAvailable: true, updateInfo: info};},
    async downloadUpdate() {
      engine.emit('download-progress', {percent: 45, transferred: 18, total: bytes.length, bytesPerSecond: 10});
      const installer = path.join(dir, info.files[0].url);
      fs.writeFileSync(installer, bytes);
      engine.emit('update-downloaded', {...info, downloadedFile: installer});
      return [installer];
    },
    quitAndInstall() {this.installCalls++; this.quitAndInstallCalled = true;},
  });
  let safe = true;
  const admissions = [];
  const controller = new (implementation().UpdaterController)({updater: engine, userData: dir,
    currentVersion: '3.41.30', isPackaged: true,
    launchInstaller: async () => {engine.installCalls++; return true;},
    quitForUpdate: () => {},
    isUpdateInstallSafe: async () => safe,
    prepareUpdateRestart: async () => {admissions.push('reserve'); return safe;},
    abortUpdateRestart: async () => {admissions.push('resume');},
    ...options});
  t.after(() => controller.stop());
  return {controller, engine, info, bytes, dir, admissions, setSafe: value => {safe = value;}};
}

const githubFeed = 'https://github.com/abdulmunthakimwork/minecraft-launcher/';
const githubReleases = githubFeed.slice(0, -1) + '/releases';
const githubCdn = 'https://release-assets.githubusercontent.com/github-production-release-asset/1453904618/9ba8181c-b438-4b5b-91c2-7c89a08c5314';

test('missing config uses the injected GitHub default without automatically starting a check', async t => {
  const {controller, engine, dir} = fixture(t, {defaultFeedUrl: githubFeed});
  assert.equal(controller.getState().status, 'idle');
  assert.equal(controller.getState().feedUrl, githubFeed);
  assert.equal(engine.checkCalls, 0);
  assert.equal(fs.existsSync(path.join(dir, 'update-feed.json')), false);
  assert.equal(controller.getState().lastCheckedAt, null);
  assert.deepEqual(engine.feed, {provider: 'github', owner: 'abdulmunthakimwork', repo: 'minecraft-launcher', channel: 'latest'});
  assert.deepEqual(implementation().GITHUB_UPDATE_SOURCE, {url: githubFeed,
    owner: 'abdulmunthakimwork', repo: 'minecraft-launcher', repositoryId: '1453904618'});
  assert.equal(Object.isFrozen(implementation().GITHUB_UPDATE_SOURCE), true);
});

test('GitHub default starts one automatic verified full download and records successful check time', async t => {
  const {controller, engine, info} = fixture(t, {defaultFeedUrl: githubFeed});
  assert.equal(controller.getState().feedUrl, githubFeed);
  info.tag = 'v3.41.31';
  const complete = new Promise(resolve => controller.on('state', value => {
    if (value.status === 'ready' || value.status === 'error') resolve(value);
  }));
  controller.start(); controller.start();
  const state = await complete;
  assert.equal(state.status, 'ready', state.error);
  assert.equal(engine.checkCalls, 1);
  assert.equal(engine.installCalls, 0);
  assert.equal(engine.autoDownload, false);
  assert.equal(engine.autoInstallOnAppQuit, false);
  assert.equal(engine.allowPrerelease, false);
  assert.equal(engine.disableDifferentialDownload, true);
  assert.equal(engine.disableWebInstaller, true);
  assert.ok(Number.isFinite(Date.parse(state.lastCheckedAt)));
});

test('explicitly disabled or custom sources survive a new GitHub default', async t => {
  const {controller, dir} = fixture(t);
  for (const saved of ['', 'https://updates.example.test/custom/']) {
    await controller.configureFeed(saved);
    const engine = new EventEmitter(); engine.checkCalls = 0;
    engine.setFeedURL = function(value) {this.feed = value;};
    engine.checkForUpdates = async () => {engine.checkCalls++; return {isUpdateAvailable: false, updateInfo: {version: '3.41.30'}};};
    const second = new (implementation().UpdaterController)({updater: engine, userData: dir,
      currentVersion: '3.41.30', isPackaged: true, defaultFeedUrl: githubFeed});
    t.after(() => second.stop());
    assert.equal(second.getState().feedUrl, saved);
    if (saved) assert.equal(engine.feed.provider, 'generic');
    else {second.start(); await new Promise(setImmediate); assert.equal(engine.checkCalls, 0);}
  }
});

test('invalid persisted config does not silently switch to the default source', async t => {
  const {controller, dir} = fixture(t);
  fs.writeFileSync(path.join(dir, 'update-feed.json'), '{invalid');
  const second = new (implementation().UpdaterController)({updater: new EventEmitter(), userData: dir,
    currentVersion: '3.41.30', isPackaged: true, defaultFeedUrl: githubFeed});
  t.after(() => second.stop());
  assert.equal(second.getState().status, 'setup');
  assert.equal(controller.getState().status, 'setup');
});

test('GitHub source canonicalizes the repository URL and preserves custom generic feeds', async t => {
  const {controller, engine} = fixture(t);
  await controller.configureFeed(githubFeed.slice(0, -1));
  assert.equal(controller.getState().feedUrl, githubFeed);
  assert.equal(engine.feed.provider, 'github');
  assert.equal(Object.hasOwn(engine.feed, 'private'), false);
  assert.equal(Object.hasOwn(engine.feed, 'token'), false);
  await controller.configureFeed('https://updates.example.test/custom');
  assert.equal(engine.feed.provider, 'generic');
  assert.equal(engine.feed.url, 'https://updates.example.test/custom/');
});

test('GitHub transport permits only stable discovery, matching setup files, and its repository CDN', () => {
  const {isPermittedUpdateUrl} = implementation();
  const allowed = [githubReleases + '.atom', githubReleases + '/latest', githubReleases + '/tag/v3.41.31',
    githubReleases + '/download/v3.41.31/latest.yml',
    githubReleases + '/download/v3.41.31/Minecraft-Launcher-Setup-3.41.31.exe',
    githubCdn + '?sp=r&sv=2018-11-09&sig=signed-value&response-content-disposition=attachment%3B%20filename%3DMinecraft-Launcher-Setup-3.41.31.exe'];
  for (const url of allowed) assert.equal(isPermittedUpdateUrl(url, githubFeed), true, url);
  const denied = [githubFeed, githubReleases, githubReleases + '/latest?token=secret',
    githubReleases + '/download/v3.41.31/Minecraft-Launcher-Setup-3.41.30.exe',
    githubReleases + '/download/v3.41.31/Minecraft-Launcher-3.41.31-Portable.exe',
    githubReleases + '/download/v3.41.31/Minecraft-Launcher-Setup-3.41.31.exe.blockmap',
    githubReleases + '/download/v3.41.31-beta.1/latest.yml', githubReleases + '/download/3.41.31/latest.yml',
    githubReleases + '/download/v03.41.31/latest.yml', githubReleases + '/download/v9007199254740992.1.1/latest.yml',
    githubReleases + '/tag/v3.41.31-beta.1', githubReleases + '/tag/v3.41.31/extra',
    githubReleases + '/download/v3.41.31/latest.yml?token=secret',
    githubReleases.replace('abdulmunthakimwork', 'someone') + '.atom',
    githubReleases.replace('minecraft-launcher', 'other-repo') + '/latest',
    githubReleases.replace('https:', 'http:') + '.atom', githubReleases.replace('github.com', 'github.com:444') + '.atom',
    githubReleases.replace('github.com', 'user:secret@github.com') + '.atom',
    githubReleases + '/download/v3.41.31/%6catest.yml', githubReleases + '/download/v3.41.31/%2f/latest.yml',
    githubReleases + '/download/v3.41.31/%2e%2e/v3.41.31/latest.yml',
    githubReleases + '/download/v3.41.31/../v3.41.31/latest.yml',
    githubReleases + '/download/v3.41.31/latest.yml#fragment',
    githubCdn.replace('1453904618', '1453904619') + '?sig=secret', githubCdn + '/extra?sig=secret',
    githubCdn.replace('9ba8181c-b438-4b5b-91c2-7c89a08c5314', 'not-a-uuid'),
    githubCdn.replace('release-assets.githubusercontent.com', 'objects.githubusercontent.com'),
    githubCdn.replace('https:', 'http:'), githubCdn.replace('release-assets.githubusercontent.com', 'user:secret@release-assets.githubusercontent.com'),
    'https://api.github.com/repos/abdulmunthakimwork/minecraft-launcher/releases/latest'];
  for (const url of denied) assert.equal(isPermittedUpdateUrl(url, githubFeed), false, url);
  assert.equal(isPermittedUpdateUrl(githubCdn, 'https://updates.example.test/launcher/'), false);
});

test('GitHub checks reject mismatched or missing tags before download including non-newer metadata', async t => {
  const {controller, engine, info} = fixture(t);
  await controller.configureFeed(githubFeed);
  let downloads = 0;
  const download = engine.downloadUpdate;
  engine.downloadUpdate = async () => {downloads++; return download();};
  for (const tag of [undefined, '3.41.31', 'v3.41.30', 'v3.41.31-beta.1']) {
    info.tag = tag;
    const state = await controller.checkForUpdates();
    assert.equal(state.status, 'error', String(tag));
    assert.equal(state.installAllowed, false);
    assert.equal(downloads, 0);
  }
  for (const updateInfo of [{version: '3.41.30', tag: 'v3.41.29'},
    {version: '3.41.30'}, {version: '3.41.30-beta.1', tag: 'v3.41.30-beta.1'}]) {
    engine.checkForUpdates = async () => ({isUpdateAvailable: false, updateInfo});
    assert.equal((await controller.checkForUpdates()).status, 'error', JSON.stringify(updateInfo));
  }
  engine.checkForUpdates = async () => ({isUpdateAvailable: false, updateInfo: {version: '3.41.30', tag: 'v3.41.30'}});
  const state = await controller.checkForUpdates();
  assert.equal(state.status, 'idle');
  assert.ok(Number.isFinite(Date.parse(state.lastCheckedAt)));
  await controller.configureFeed(githubFeed);
  assert.equal(controller.getState().lastCheckedAt, null);
});

test('GitHub missing release metadata gives a safe actionable public error without signed URL leakage', async t => {
  const {controller, engine} = fixture(t);
  await controller.configureFeed(githubFeed);
  for (const code of ['ERR_UPDATER_LATEST_VERSION_NOT_FOUND', 'ERR_UPDATER_NO_PUBLISHED_VERSIONS', 'ERR_UPDATER_CHANNEL_FILE_NOT_FOUND']) {
    engine.checkForUpdates = async () => {throw Object.assign(new Error(githubCdn + '?sig=secret'), {code});};
    const state = await controller.checkForUpdates();
    assert.equal(state.status, 'error');
    assert.match(state.error, /GitHub|latest.yml/);
    assert.doesNotMatch(state.error, /sig=|secret|github-production-release-asset|configured update directory/);
  }
});

test('missing update source is setup and performs no network check', async t => {
  const {controller, engine} = fixture(t);
  assert.equal(controller.getState().status, 'setup');
  assert.equal(engine.autoInstallOnAppQuit, false);
  const state = await controller.checkForUpdates();
  assert.equal(state.status, 'setup');
  assert.equal(engine.checkCalls, 0);
});

test('HTTPS feed is persisted across sessions and empty input clears setup', async t => {
  const {controller, engine, dir} = fixture(t);
  await controller.configureFeed('https://updates.example.test/launcher');
  assert.equal(controller.getState().feedUrl, 'https://updates.example.test/launcher/');
  const second = new (implementation().UpdaterController)({updater: new EventEmitter(), userData: dir,
    currentVersion: '3.41.30', isPackaged: true});
  t.after(() => second.stop());
  assert.equal(second.getState().feedUrl, 'https://updates.example.test/launcher/');
  assert.equal(engine.feed.provider, 'generic');
  await controller.configureFeed('');
  assert.equal(controller.getState().status, 'setup');
});

test('source validation rejects insecure credentials queries fragments and metadata filenames', async t => {
  const {controller} = fixture(t);
  for (const value of ['http://updates.example.test/', 'https://user:secret@updates.example.test/',
    'https://updates.example.test/?token=secret', 'https://updates.example.test/#feed',
    'https://updates.example.test/latest.yml', 12, 'file:///C:/installer']) {
    await assert.rejects(controller.configureFeed(value), /HTTPS|directory|source/i);
    assert.equal(controller.getState().status, 'setup');
  }
});

test('a configured check stages a hash and size verified installer and exposes progress', async t => {
  const {controller, engine} = fixture(t);
  const states = [];
  controller.on('state', value => states.push(value));
  await controller.configureFeed('https://updates.example.test/launcher/');
  const state = await controller.checkForUpdates();
  assert.equal(state.status, 'ready');
  assert.equal(state.availableVersion, '3.41.31');
  assert.equal(state.installAllowed, true);
  assert.ok(states.some(value => value.status === 'checking'));
  assert.ok(states.some(value => value.status === 'downloading' && value.progress.percent === 45));
  assert.equal(engine.installCalls, 0, 'Staging started the installer');
});

test('metadata rejects wrong app file traversal insecure URLs missing hashes and invalid sizes', async t => {
  const {controller, engine, info} = fixture(t);
  await controller.configureFeed('https://updates.example.test/launcher/');
  const original = {...info.files[0]};
  for (const change of [{url: '../../outside.exe'}, {url: 'Another-App.exe'},
    {url: 'http://updates.example.test/launcher/Minecraft-Launcher-Setup-3.41.31.exe'},
    {sha512: ''}, {size: 0}, {size: 2 ** 42}, {size: original.size + 1},
    {sha512: Buffer.alloc(64).toString('base64')}, {isAdminRightsRequired: 'yes'}]) {
    info.files[0] = {...original, ...change};
    const state = await controller.checkForUpdates();
    assert.equal(state.status, 'error', JSON.stringify(change));
    assert.equal(state.installAllowed, false);
    assert.equal(engine.installCalls, 0);
  }
});

test('old and malformed versions cannot become staged updates', async t => {
  const {controller, engine, info} = fixture(t);
  await controller.configureFeed('https://updates.example.test/');
  for (const version of ['3.41.29', '3.41.30', 'invalid', '3.41.31-beta.1']) {
    info.version = version;
    assert.equal((await controller.checkForUpdates()).status, 'error');
    assert.equal(engine.installCalls, 0);
  }
  engine.checkForUpdates = async () => ({isUpdateAvailable: false, updateInfo: {version: '3.41.30'}});
  assert.equal((await controller.checkForUpdates()).status, 'idle');
});

test('installer staging failures report an actionable error and stay un-installable', async t => {
  const {controller, engine} = fixture(t);
  await controller.configureFeed('https://updates.example.test/');
  engine.downloadUpdate = async () => {throw new Error('ERR_CHECKSUM_MISMATCH disposable fixture');};
  const state = await controller.checkForUpdates();
  assert.equal(state.status, 'error');
  assert.match(state.error, /verification|checksum/i);
  assert.equal(state.installAllowed, false);
});

test('restart requires fresh idle admission and retains ready state while game is running', async t => {
  const {controller, engine, setSafe, admissions} = fixture(t);
  await controller.configureFeed('https://updates.example.test/');
  await controller.checkForUpdates();
  setSafe(false);
  const state = await controller.restartAndInstall();
  assert.equal(state.status, 'ready');
  assert.equal(state.installAllowed, false);
  assert.equal(engine.installCalls, 0);
  assert.deepEqual(admissions, []);
  setSafe(true);
  assert.equal((await controller.restartAndInstall()).status, 'installing');
  assert.equal(engine.installCalls, 1);
  assert.deepEqual(admissions, ['reserve']);
});

test('changed installer is reverified before acquiring shutdown or executing', async t => {
  const {controller, engine, dir, info, admissions} = fixture(t);
  await controller.configureFeed('https://updates.example.test/');
  await controller.checkForUpdates();
  fs.appendFileSync(path.join(dir, info.files[0].url), 'tampered');
  const state = await controller.restartAndInstall();
  assert.equal(state.status, 'error');
  assert.equal(engine.installCalls, 0);
  assert.deepEqual(admissions, []);
});

test('failed installer launch releases shutdown admission and reports failure', async t => {
  const {controller, engine, admissions} = fixture(t);
  await controller.configureFeed('https://updates.example.test/');
  await controller.checkForUpdates();
  controller.options.launchInstaller = async () => {throw new Error('fixture installer cannot start');};
  const state = await controller.restartAndInstall();
  assert.equal(state.status, 'error');
  assert.deepEqual(admissions, ['reserve', 'resume']);
});

for (const [code, delay] of [['EIO', 0], ['EIO', 30], ['ENOENT', 0]]) {
  test(`real NSIS async ${code} failure (${delay}ms) keeps recovery visible and allows a fresh install retry`, async t => {
    const Module = require('node:module');
    const originalLoad = Module._load;
    const native = new EventEmitter();
    let appQuitCalls = 0, shellCalls = 0, nativeQuitEvents = 0, confirmedQuitCalls = 0, resumes = 0;
    native.on('before-quit-for-update', () => {nativeQuitEvents++;});
    Module._load = function(name) {
      if (name === 'electron') return {autoUpdater: native, shell: {openPath: async () => {
        shellCalls++; return 'Synthetic association failed';
      }}};
      return originalLoad.apply(this, arguments);
    };
    t.after(() => {Module._load = originalLoad;});
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'launcher-async-install-test-'));
    t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
    const {NsisUpdater} = require('electron-updater');
    const engine = new NsisUpdater(null, {version: '3.41.30', name: 'Minecraft Launcher', isPackaged: true,
      userDataPath: dir, baseCachePath: dir, appUpdateConfigPath: path.join(dir, 'app-update.yml'),
      whenReady: async () => {}, onQuit() {throw new Error('Automatic installation forbidden');},
      quit() {appQuitCalls++;}});
    engine.logger = null;
    const bytes = Buffer.from('synthetic non-executable installer');
    const filename = 'Minecraft-Launcher-Setup-3.41.31.exe';
    const info = {version: '3.41.31', files: [{url: filename, size: bytes.length,
      sha512: crypto.createHash('sha512').update(bytes).digest('base64')}]};
    engine.checkForUpdates = async () => ({isUpdateAvailable: true, updateInfo: info});
    engine.downloadUpdate = async () => {
      const target = path.join(dir, filename); fs.writeFileSync(target, bytes);
      engine.downloadedUpdateHelper = {file: target, downloadedFileInfo: {isAdminRightsRequired: false}, packageFile: null};
      return [target];
    };
    let failing = true;
    const failure = () => Object.assign(new Error('Synthetic installer failure'), {code});
    engine.spawnLog = async () => {await new Promise(resolve => setTimeout(resolve, delay)); throw failure();};
    const spawnProcess = () => {
      const child = new EventEmitter(); child.unref = () => {};
      setTimeout(() => {if (failing) child.emit('error', failure()); else child.emit('spawn');}, delay);
      return child;
    };
    const controller = new (implementation().UpdaterController)({updater: engine, userData: dir,
      currentVersion: '3.41.30', isPackaged: true,
      isUpdateInstallSafe: async () => true, prepareUpdateRestart: async () => true,
      abortUpdateRestart: async () => {resumes++;},
      launchInstaller: target => implementation().launchNsisInstaller(target, {resourcesPath: dir, spawnProcess}),
      quitForUpdate: () => {confirmedQuitCalls++;}});
    t.after(() => controller.stop());
    await controller.configureFeed('https://updates.example.test/launcher/');
    await controller.checkForUpdates();
    await controller.restartAndInstall();
    await new Promise(resolve => setTimeout(resolve, delay + 10));
    await new Promise(setImmediate);
    assert.equal(controller.getState().status, 'error');
    assert.equal(controller.reserved, false);
    assert.equal(resumes, 1);
    assert.equal(appQuitCalls, 0, 'The upstream queued quit escaped installer recovery');
    assert.equal(nativeQuitEvents, 0, 'Quit permission was published before confirmed process launch');
    assert.equal(shellCalls, 0, 'A resolved shell.openPath error bypassed recovery');
    assert.equal(confirmedQuitCalls, 0);
    assert.equal(engine.quitAndInstallCalled, false, 'The upstream install latch poisoned retry');
    failing = false;
    await controller.checkForUpdates();
    const retry = await controller.restartAndInstall();
    assert.equal(retry.status, 'installing');
    assert.equal(confirmedQuitCalls, 1);
    assert.equal(resumes, 1);
    assert.equal(appQuitCalls, 0);
    assert.equal(engine.quitAndInstallCalled, false);
  });
}

test('owned installer launcher waits for spawn and reports access elevation failure before quitting', async t => {
  const {launchNsisInstaller} = implementation();
  assert.equal(typeof launchNsisInstaller, 'function', 'The launcher has no awaited installer process boundary');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'launcher-elevation-test-'));
  t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
  const installer = path.join(dir, 'Minecraft-Launcher-Setup-3.41.31.exe');
  fs.writeFileSync(installer, 'synthetic installer');
  fs.writeFileSync(path.join(dir, 'elevate.exe'), 'synthetic elevation helper');
  const children = [], commands = [];
  const spawnProcess = (command, args, options) => {
    commands.push({command, args, options});
    const child = new EventEmitter(); child.unref = () => {child.unreferenced = true;};
    children.push(child); return child;
  };
  let completed = false;
  const pending = launchNsisInstaller(installer, {resourcesPath: dir, spawnProcess}).then(() => {completed = true;});
  assert.equal(completed, false);
  children[0].emit('error', Object.assign(new Error('Synthetic access denied'), {code: 'EACCES'}));
  await new Promise(setImmediate);
  assert.equal(commands.length, 2);
  assert.equal(commands[1].command, path.join(dir, 'elevate.exe'));
  assert.deepEqual(commands[1].args, [installer, '--updated', '--force-run']);
  children[1].emit('spawn');
  await new Promise(setImmediate);
  assert.equal(completed, false, 'Elevation helper spawn was mistaken for UAC acceptance');
  children[1].emit('exit', 1, null);
  await assert.rejects(pending, /elevat|permission|start/i);
  assert.equal(completed, false);
  assert.equal(commands[0].options.shell, false);
  assert.equal(commands[0].options.windowsHide, true);
});

test('owned elevation confirms helper success and handles explicit admin metadata', async t => {
  const {launchNsisInstaller} = implementation();
  assert.equal(typeof launchNsisInstaller, 'function');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'launcher-elevation-success-test-'));
  t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
  const installer = path.join(dir, 'Minecraft-Launcher-Setup-3.41.31.exe');
  fs.writeFileSync(path.join(dir, 'elevate.exe'), 'synthetic helper');
  const children = [], commands = [];
  const pending = launchNsisInstaller(installer, {requiresElevation: true, resourcesPath: dir,
    spawnProcess(command, args) {
      commands.push({command, args}); const child = new EventEmitter(); child.unref = () => {};
      children.push(child); return child;
    }});
  await new Promise(setImmediate);
  assert.equal(commands.length, 1);
  assert.equal(commands[0].command, path.join(dir, 'elevate.exe'));
  children[0].emit('spawn'); children[0].emit('exit', 0, null);
  assert.equal(await pending, true);
});

test('work that starts after readiness but before reservation prevents installation', async t => {
  const {controller, engine, admissions} = fixture(t, {prepareUpdateRestart: async () => false});
  await controller.configureFeed('https://updates.example.test/');
  await controller.checkForUpdates();
  assert.equal(controller.getState().installAllowed, true);
  const state = await controller.restartAndInstall();
  assert.equal(state.status, 'ready');
  assert.equal(state.installAllowed, false);
  assert.equal(engine.installCalls, 0);
  assert.deepEqual(admissions, []);
});

test('host status failures keep the staged release ready without authorizing installation', async t => {
  const {controller, engine} = fixture(t, {isUpdateInstallSafe: async () => {throw new Error('host unavailable');}});
  await controller.configureFeed('https://updates.example.test/');
  await controller.checkForUpdates();
  assert.equal(controller.getState().status, 'ready');
  assert.equal(controller.getState().installAllowed, false);
  await controller.restartAndInstall();
  assert.equal(engine.installCalls, 0);
});

test('automatic startup checks use a persisted configured source and repeat startup does not duplicate checks', async t => {
  const {controller, engine} = fixture(t);
  await controller.configureFeed('https://updates.example.test/');
  const ready = new Promise(resolve => controller.on('state', value => {if (value.status === 'ready') resolve();}));
  controller.start();
  controller.start();
  await ready;
  assert.equal(engine.checkCalls, 1);
  assert.equal(engine.installCalls, 0);
});

test('renderer gets an installed-app explanation instead of silently waiting on a checkout updater', async t => {
  const {controller, engine} = fixture(t, {isPackaged: false});
  await controller.configureFeed('https://updates.example.test/');
  assert.equal((await controller.checkForUpdates()).status, 'error');
  assert.match(controller.getState().error, /installed launcher/);
  assert.equal(engine.checkCalls, 0);
});

test('feed cannot change during acquisition and duplicate checks join the same job', async t => {
  const {controller, engine} = fixture(t);
  await controller.configureFeed('https://updates.example.test/');
  let complete;
  engine.checkForUpdates = () => {engine.checkCalls++; return new Promise(resolve => {complete = resolve;});};
  const first = controller.checkForUpdates();
  const second = controller.checkForUpdates();
  await assert.rejects(controller.configureFeed('https://elsewhere.example.test/'), /progress|active/i);
  complete({isUpdateAvailable: false, updateInfo: {version: '3.41.30'}});
  await Promise.all([first, second]);
  assert.equal(engine.checkCalls, 1);
  assert.equal(controller.getState().feedUrl, 'https://updates.example.test/');
});

test('updater transport rejects HTTPS downgrade external redirects and directory escapes', () => {
  const {isPermittedUpdateUrl} = implementation();
  const feed = 'https://updates.example.test/launcher/';
  for (const [url, expected] of [
    ['https://updates.example.test/launcher/latest.yml?noCache=123', true],
    ['https://updates.example.test/launcher/Minecraft-Launcher-Setup-3.41.31.exe', true],
    ['http://updates.example.test/launcher/latest.yml', false],
    ['https://cdn.example.test/launcher/latest.yml', false],
    ['https://updates.example.test/outside.exe', false],
    ['https://user:pass@updates.example.test/launcher/latest.yml', false],
    ['https://updates.example.test/launcher/%2e%2e/outside.exe', false],
  ]) assert.equal(isPermittedUpdateUrl(url, feed), expected, url);
});

test('real NSIS updater stages without bundled publish configuration or invented service URL', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'launcher-real-updater-test-'));
  t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
  const resource = require('./package.json').build.extraResources.find(value => value.to === 'app-update.yml');
  assert.ok(resource, 'Packaging omits the cache-only updater configuration');
  const config = fs.readFileSync(path.join(__dirname, resource.from), 'utf8');
  fs.writeFileSync(path.join(dir, 'app-update.yml'), config);
  const {NsisUpdater} = require('electron-updater');
  const engine = new NsisUpdater(null, {version: '3.41.30', name: 'Minecraft Launcher', isPackaged: true,
    appUpdateConfigPath: path.join(dir, 'app-update.yml'), userDataPath: dir, baseCachePath: path.join(dir, 'cache'),
    whenReady: async () => {}, onQuit() {throw new Error('Automatic install hook must never be registered');},
    quit() {throw new Error('A test must never invoke a real installer');}});
  engine.logger = null;
  const bytes = Buffer.from('synthetic full installer');
  const hash = crypto.createHash('sha512').update(bytes).digest('base64');
  const requests = [];
  engine.httpExecutor = {
    async request(options) {requests.push(options); return `version: 3.41.31\nfiles:\n  - url: Minecraft-Launcher-Setup-3.41.31.exe\n    size: ${bytes.length}\n    sha512: ${hash}\n`;},
    async download(_url, destination) {fs.writeFileSync(destination, bytes);},
  };
  const controller = new (implementation().UpdaterController)({updater: engine, userData: dir,
    currentVersion: '3.41.30', isPackaged: true, isUpdateInstallSafe: async () => false});
  t.after(() => controller.stop());
  await controller.configureFeed('https://updates.example.test/launcher/');
  const state = await controller.checkForUpdates();
  assert.equal(state.status, 'ready', state.error);
  assert.equal(state.installAllowed, false);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].hostname, 'updates.example.test');
  const cacheConfig = require('js-yaml').load(config);
  assert.equal(cacheConfig.provider, undefined);
  assert.equal(cacheConfig.url, undefined);
  assert.ok(fs.existsSync(path.join(dir, 'cache', 'minecraft-launcher-desktop-updater', 'pending',
    'Minecraft-Launcher-Setup-3.41.31.exe')));
});

test('real public GitHub provider discovers stable metadata and stages the exact full setup through the repository CDN', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'launcher-github-provider-test-'));
  t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
  fs.copyFileSync(path.join(__dirname, 'updater-cache.yml'), path.join(dir, 'app-update.yml'));
  const {NsisUpdater} = require('electron-updater');
  const engine = new NsisUpdater(null, {version: '3.41.30', name: 'Minecraft Launcher', isPackaged: true,
    appUpdateConfigPath: path.join(dir, 'app-update.yml'), userDataPath: dir, baseCachePath: path.join(dir, 'cache'),
    whenReady: async () => {}, onQuit() {throw new Error('Automatic install forbidden');},
    quit() {throw new Error('No synthetic fixture may launch an installer');}});
  engine.logger = null;
  const bytes = Buffer.from('synthetic public GitHub setup');
  const hash = crypto.createHash('sha512').update(bytes).digest('base64');
  const filename = 'Minecraft-Launcher-Setup-3.41.31.exe';
  const requestUrls = [], downloadUrls = [];
  const {isPermittedUpdateUrl} = implementation();
  engine.httpExecutor = {
    async request(options) {
      const url = `${options.protocol}//${options.hostname}${options.path}`;
      requestUrls.push(url);
      assert.equal(isPermittedUpdateUrl(url, githubFeed), true, url);
      if (url === githubReleases + '.atom') return `<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>v3.41.31</title><link href="${githubReleases}/tag/v3.41.31"/><content>Stable release</content></entry></feed>`;
      if (url === githubReleases + '/latest') return '{"tag_name":"v3.41.31"}';
      assert.equal(url, githubReleases + '/download/v3.41.31/latest.yml');
      return `version: 3.41.31\nfiles:\n  - url: ${filename}\n    size: ${bytes.length}\n    sha512: ${hash}\n`;
    },
    async download(url, destination) {
      downloadUrls.push(url.href);
      assert.equal(url.href, githubReleases + '/download/v3.41.31/' + filename);
      assert.equal(isPermittedUpdateUrl(url.href, githubFeed), true);
      assert.equal(isPermittedUpdateUrl(githubCdn + '?sig=fixture-signature', githubFeed), true);
      fs.writeFileSync(destination, bytes);
    },
  };
  const controller = new (implementation().UpdaterController)({updater: engine, userData: dir,
    currentVersion: '3.41.30', isPackaged: true, defaultFeedUrl: githubFeed, isUpdateInstallSafe: async () => false});
  t.after(() => controller.stop());
  const provider = await engine.clientPromise;
  assert.ok(provider, 'The default source did not construct a public GitHub provider');
  assert.equal(provider.constructor.name, 'GitHubProvider');
  assert.equal(provider.options.private, undefined);
  assert.equal(provider.options.token, undefined);
  const state = await controller.checkForUpdates();
  assert.equal(state.status, 'ready', state.error);
  assert.equal(state.installAllowed, false);
  assert.equal(state.availableVersion, '3.41.31');
  assert.deepEqual(requestUrls, [githubReleases + '.atom', githubReleases + '/latest', githubReleases + '/download/v3.41.31/latest.yml']);
  assert.deepEqual(downloadUrls, [githubReleases + '/download/v3.41.31/' + filename]);
  const metadata = {version: '3.41.31', tag: 'v3.41.31', files: [{url: filename, size: bytes.length, sha512: hash}]};
  const resolved = provider.resolveFiles(metadata);
  assert.equal(resolved[0].url.href, githubReleases + '/download/v3.41.31/' + filename);
  assert.equal(metadata.files[0].url, filename, 'Provider must preserve metadata used by strict validation');
  assert.equal(fs.readFileSync(controller.staged.filename).equals(bytes), true);
});
