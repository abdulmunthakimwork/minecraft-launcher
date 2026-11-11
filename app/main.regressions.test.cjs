const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const os = require('node:os');
const {EventEmitter} = require('node:events');
const source = fs.readFileSync(__dirname + '/main.js', 'utf8');
function extract(begin, end, name, scope) {
  vm.createContext(scope);
  const start = source.indexOf(begin), finish = source.indexOf(end, start);
  assert.ok(start >= 0 && finish > start);
  vm.runInContext(source.slice(start, finish) + '\nglobalThis.tested = ' + name, scope);
  return scope.tested;
}
test('hardware acceleration preference merges saved overlay before desktop startup', () => {
  const files = {
    'state/launcher_settings.json': JSON.stringify({disableGPU: false, locale: 'en-US'}),
    'data/state.json': JSON.stringify({__launcher_settings: {disableGPU: true}}),
  };
  const scope = {fs: {readFileSync(file) {
    if (!(file in files)) throw new Error('missing');
    return files[file];
  }}, path: path.posix, DATA_DIR: 'data', stateDir: () => 'state'};
  const read = extract('function readLauncherSettings(', '// --- python / bundled host', 'readLauncherSettings', scope);
  assert.equal(read().disableGPU, true);
  assert.equal(read().locale, 'en-US');
  files['data/state.json'] = '{broken';
  assert.equal(read().disableGPU, false);
  delete files['state/launcher_settings.json'];
  assert.equal(read().disableGPU, undefined);
  const disable = source.indexOf('if (readLauncherSettings().disableGPU === true) app.disableHardwareAcceleration();');
  assert.ok(disable >= 0 && disable < source.indexOf('app.whenReady()'), 'GPU preference applies before ready');
});
test('packaged missing frozen host fails before Python fallback', () => {
  const scope = {isPackaged: true, HOST_EXE: 'missing.exe', RESOURCE_ROOT: 'resources', __dirname,
    fs: {existsSync: () => false}, path, findPython: () => ({cmd: 'python', args: []})};
  const fn = extract('function hostCommand(', 'async function startHost(', 'hostCommand', scope);
  assert.throws(() => fn(10000), /host.*missing|reinstall/i);
});
test('host readiness rejects an early clean exit promptly', async () => {
  const child = new EventEmitter();
  child.exitCode = null;
  const scope = {http: {get() {const request = new EventEmitter(); request.setTimeout = () => {};
    request.destroy = () => {}; setTimeout(() => request.emit('error', new Error('unavailable')), 5);
    return request;}}, setTimeout, clearTimeout, Date};
  const fn = extract('function waitForServer(', 'function logTail(', 'waitForServer', scope);
  const pending = fn('http://127.0.0.1:10000', 300, child);
  child.emit('exit', 0, null);
  await assert.rejects(pending, /exited|stopped/i);
});
test('flow validation fails when a required click was skipped', () => {
  const flows = fs.readFileSync(__dirname + '/../harness/flows.js', 'utf8');
  const start = flows.indexOf('  const crashes = report.filter');
  const scope = {report: [{crash: false, leak: false, clickedAll: false, errors: []}], console,
    app: {exit(code) {scope.result = code;}}};
  vm.createContext(scope);
  vm.runInContext(flows.slice(start, flows.lastIndexOf('});')), scope);
  assert.equal(scope.result, 1);
});

for (const [code, signal] of [[0, null], [null, 'SIGTERM']]) {
  test('unexpected host exit is reported: ' + (signal || code), () => {
    const child = new EventEmitter();
    const scope = {serverProcess: child, spawnedHost: child, hostReady: true,
      app: {isQuitting: false}, logTail: () => 'fixture log',
      dialog: {showErrorBox(title, message) {scope.message = message;}}};
    vm.createContext(scope);
    const start = source.indexOf('  serverProcess.on("exit",');
    const end = source.indexOf('  await waitForServer', start);
    vm.runInContext(source.slice(start, end), scope);
    child.emit('exit', code, signal);
    assert.match(scope.message, /exited/);
  });
}

function windowFixture(openExternal = async () => {}) {
  const scope = {URL, path, __dirname, isPackaged: false, REPO_ROOT: 'fixture', APP_NAME: 'Fixture',
    readWindowState: () => ({width: 1280, height: 720}), buildMenu() {}, saveWindowState() {},
    logTail: () => '', mainWindow: null, lifecycle: null, app: {isQuitting: false}, links: [], errors: [],
    shell: {openExternal(url) {scope.links.push(url); return openExternal(url);}},
    dialog: {showErrorBox(title, message) {scope.errors.push({title, message});}},
    BrowserWindow: class extends EventEmitter {
      constructor(options) {
        super();
        this.options = options;
        this.webContents = new EventEmitter();
        this.webContents.setWindowOpenHandler = (handler) => {this.openHandler = handler;};
      }
      loadURL(url) {this.url = url;}
      show() {this.visible = true;}
      hide() {this.visible = false;}
    }};
  const begin = source.includes('function openExternalUrl(') ? 'function openExternalUrl(' : 'function createWindow(';
  const create = extract(begin, '// --- lifecycle', 'createWindow', scope);
  create('http://127.0.0.1:4312');
  return scope;
}

test('desktop launcher navigation trusts the exact origin and rejects prefix lookalikes', () => {
  const scope = windowFixture();
  for (const target of ['http://127.0.0.1:43123/', 'http://127.0.0.1:4312@remote.invalid/',
                        'http://user@127.0.0.1:4312/', 'not a URL']) {
    let prevented = false;
    scope.mainWindow.webContents.emit('will-navigate', {preventDefault() {prevented = true;}}, target);
    assert.equal(prevented, true, target + ' replaced the launcher page');
  }
  let prevented = false;
  scope.mainWindow.webContents.emit('will-navigate', {preventDefault() {prevented = true;}},
                                    'http://127.0.0.1:4312/games/java/configurations?editor=one');
  assert.equal(prevented, false, 'A normal local launcher route was blocked');
});

test('desktop window loads the update facade through a sandboxed isolated preload', () => {
  const scope = windowFixture();
  const preferences = scope.mainWindow.options.webPreferences;
  assert.equal(preferences.preload, path.join(__dirname, 'preload.js'));
  assert.equal(preferences.contextIsolation, true);
  assert.equal(preferences.nodeIntegration, false);
  assert.equal(preferences.sandbox, true);
});

test('desktop external-link failure is handled and reported', async () => {
  const scope = windowFixture(() => Promise.reject(new Error('fixture browser unavailable')));
  assert.equal(scope.mainWindow.openHandler({url: 'https://example.invalid/help'}).action, 'deny');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(scope.errors.length, 1);
  assert.match(scope.errors[0].message, /fixture browser unavailable/);
});

test('desktop handles synchronous browser-opening failure too', () => {
  const scope = windowFixture(() => {throw new Error('fixture browser failed');});
  assert.doesNotThrow(() => scope.mainWindow.openHandler({url: 'https://example.invalid/help'}));
  assert.equal(scope.errors.length, 1);
});

test('desktop network filter limits loopback requests to the launcher origin', () => {
  let requestHandler;
  const scope = {URL};
  const lock = extract('function lockToLocalContent(', '// --- lifecycle', 'lockToLocalContent', scope);
  lock({webRequest: {onBeforeRequest(handler) {requestHandler = handler;}}}, 'http://127.0.0.1:4312');
  for (const [url, expected] of [
    ['http://127.0.0.1:4312/__bridge.js', false],
    ['data:image/png;base64,fixture', false],
    ['http://127.0.0.1:43123/', true],
    ['http://user@127.0.0.1:4312/', true],
    ['https://example.invalid/', true],
    ['not a URL', true],
  ]) {
    let result;
    requestHandler({url}, value => {result = value;});
    assert.equal(result.cancel, expected, url);
  }
});

test('desktop launcher redirects cannot replace the page with another local service', () => {
  const scope = windowFixture();
  let prevented = false;
  scope.mainWindow.webContents.emit('will-redirect', {preventDefault() {prevented = true;}},
                                    'http://127.0.0.1:43123/');
  assert.equal(prevented, true);
});

// Catches allowing native close to destroy the hidden window and its drafts.
test('desktop close keeps the running game session window available', async () => {
  const scope = windowFixture();
  const {LauncherLifecycle} = require('./lifecycle.js');
  scope.lifecycle = new LauncherLifecycle({
    readStatus: async () => ({gameRunning: true, activeWork: false, keepLauncherOpen: true}),
    reserveShutdown: async () => false, resumeHost: async () => {},
    hideWindow: () => scope.mainWindow.hide(), showWindow: () => scope.mainWindow.show(), quit: () => {},
  });
  scope.mainWindow.visible = true;
  let prevented = false;
  scope.mainWindow.emit('close', {preventDefault() {prevented = true;}});
  await scope.lifecycle.check();
  assert.equal(prevented, true, 'Native close would destroy the session window');
  assert.equal(scope.mainWindow.visible, false);
  assert.equal(scope.lifecycle.hidden, true);
});

function shellEventFixture(options = {}) {
  const app = new EventEmitter();
  const nativeUpdater = new EventEmitter();
  const paths = {userData: options.userData || __dirname};
  const startupOrder = options.startupOrder || [];
  Object.assign(app, {isPackaged: options.isPackaged || false, paths,
    getPath(name) {startupOrder.push('read:' + name); return paths[name] || __dirname;},
    setPath(name, value) {startupOrder.push('set:' + name); paths[name] = value;},
    setAppUserModelId() {}, setName() {}, disableHardwareAcceleration() {},
    requestSingleInstanceLock() {startupOrder.push('lock'); return true;}, whenReady: () => new Promise(() => {}),
    completedQuits: 0,
    quit() {
      const event = {prevented: false, preventDefault() {this.prevented = true;}};
      app.emit('before-quit', event);
      if (!event.prevented) {app.completedQuits++; app.emit('will-quit');}
    },
  });
  const scope = {process: Object.assign(new EventEmitter(), {env: {}, argv: options.argv || [], resourcesPath: '', platform: process.platform}),
    console, __dirname, URL, setTimeout, clearTimeout,
    require(name) {
      if (name === 'electron') return {app, autoUpdater: nativeUpdater,
        Tray: class extends EventEmitter {
          constructor() {super(); scope.trayObject = this;}
          setContextMenu(menu) {this.menu = menu;}
          setToolTip(value) {this.tooltip = value;}
          destroy() {this.destroyed = true;}
        },
        nativeImage: {createFromPath: () => ({isEmpty: () => false})},
        Menu: {buildFromTemplate: value => value},
      };
      if (name === 'fs') return {mkdirSync: options.mkdirSync || fs.mkdirSync,
        readFileSync() {throw new Error('disposable settings unavailable');}};
      if (name === 'path') return options.pathModule || path;
      return require(name);
    },
  };
  vm.createContext(scope);
  vm.runInContext(source, scope);
  return {app, scope, nativeUpdater};
}

test('smoke user data isolates packaged paths before any user data read or instance lock', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'launcher-smoke-option-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const userData = path.join(root, 'isolated shell data');
  const startupOrder = [];
  const {app, scope} = shellEventFixture({isPackaged: true, userData: path.join(root, 'native'), startupOrder,
    argv: ['--smoke-test', '--smoke-user-data=' + userData]});
  assert.equal(app.paths.userData, userData);
  assert.equal(fs.statSync(userData).isDirectory(), true);
  assert.equal(vm.runInContext('DATA_DIR', scope), path.join(userData, 'data'));
  assert.equal(vm.runInContext('WINDOW_STATE', scope), path.join(userData, 'window-state.json'));
  assert.equal(vm.runInContext('SERVER_LOG', scope), path.join(userData, 'host-server.log'));
  assert.ok(startupOrder.indexOf('set:userData') < startupOrder.indexOf('read:userData'));
  assert.ok(startupOrder.indexOf('set:userData') < startupOrder.indexOf('lock'));
});

test('ordinary startup ignores the smoke user data option and keeps native paths', () => {
  const nativePath = path.resolve(__dirname, 'native-fixture');
  for (const directory of [path.resolve(__dirname, 'ignored-fixture'), '', 'relative']) {
    const created = [], startupOrder = [];
    const {app, scope} = shellEventFixture({userData: nativePath, isPackaged: true, startupOrder,
      argv: ['--smoke-user-data=' + directory], mkdirSync: value => created.push(value)});
    assert.equal(app.paths.userData, nativePath);
    assert.equal(vm.runInContext('DATA_DIR', scope), path.join(nativePath, 'data'));
    assert.deepEqual(created, []);
    assert.equal(startupOrder.includes('set:userData'), false);
  }
});

test('smoke user data rejects relative and malformed paths before writes or instance lock', () => {
  for (const directory of ['', 'relative', 'C:drive-relative', '\\drive-rooted', 'C:\\tmp\\invalid\0path']) {
    const created = [], startupOrder = [];
    assert.throws(() => shellEventFixture({pathModule: path.win32, startupOrder,
      argv: ['--smoke-test', '--smoke-user-data=' + directory], mkdirSync: value => created.push(value)}),
      /smoke.*absolute.*path/i, JSON.stringify(directory));
    assert.deepEqual(created, []);
    assert.deepEqual(startupOrder, []);
  }
});

test('smoke user data creation failure stops before accessing native data or instance lock', () => {
  const startupOrder = [];
  assert.throws(() => shellEventFixture({startupOrder,
    argv: ['--smoke-test', '--smoke-user-data=' + path.resolve(__dirname, 'unwritable-fixture')],
    mkdirSync() {throw new Error('synthetic directory creation failure');}}), /synthetic directory creation failure/);
  assert.deepEqual(startupOrder, []);
});

test('ordinary app quit keeps a busy game in the background', async () => {
  const {app, scope} = shellEventFixture();
  const {LauncherLifecycle} = require('./lifecycle.js');
  scope.controller = new LauncherLifecycle({
    readStatus: async () => ({gameRunning: true, activeWork: false, keepLauncherOpen: true}),
    reserveShutdown: async () => false, resumeHost: async () => {},
    hideWindow() {}, showWindow() {}, quit: () => app.quit(),
  });
  // The controller binding is established by desktop startup in production.
  vm.runInContext('lifecycle = controller;', scope);
  app.quit();
  await scope.controller.check();
  assert.equal(app.completedQuits, 0, 'Ordinary quit exited while Minecraft was busy');
  assert.equal(scope.controller.hidden, true);
});

test('desktop shortcut second instance shows the existing hidden session', async () => {
  const {app, scope} = shellEventFixture();
  const window = {visible: false, show() {this.visible = true;}, isDestroyed: () => false,
    isMinimized: () => false, focus() {this.focused = true;}};
  scope.window = window;
  vm.runInContext('mainWindow = window;', scope);
  app.emit('second-instance');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(window.visible, true, 'Existing hidden session was not shown');
  assert.equal(window.focused, true);
});

test('tray actions reopen the existing window and defer quitting a busy game', async () => {
  const {app, scope} = shellEventFixture();
  const {LauncherLifecycle} = require('./lifecycle.js');
  const window = {visible: false, show() {this.visible = true;}, hide() {this.visible = false;},
    isDestroyed: () => false, isMinimized: () => false, focus() {}};
  scope.window = window;
  scope.controller = new LauncherLifecycle({
    readStatus: async () => ({gameRunning: true, activeWork: false, keepLauncherOpen: true}),
    reserveShutdown: async () => false, resumeHost: async () => {},
    hideWindow: () => window.hide(), showWindow: () => window.show(), quit: () => app.quit(),
  });
  vm.runInContext('mainWindow = window; lifecycle = controller;', scope);
  assert.equal(vm.runInContext('typeof createTray', scope), 'function', 'Background session has no tray actions');
  vm.runInContext('createTray();', scope);
  scope.trayObject.menu[0].click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(window.visible, true);
  scope.trayObject.menu.at(-1).click();
  await scope.controller.check();
  assert.equal(window.visible, false);
  assert.equal(app.completedQuits, 0);
});

test('explicit update quit bypasses ordinary-close deferral only after shutdown admission', () => {
  const {app, scope} = shellEventFixture();
  scope.controller = {stopped: false, updating: true, stop() {this.stopped = true;}, closeWindow() {}, check() {}};
  scope.updateController = {reserved: true, installLaunchConfirmed: true, getState: () => ({status: 'installing'}), stop() {}};
  vm.runInContext('lifecycle = controller; updaterController = updateController;', scope);
  assert.equal(vm.runInContext('quitForUpdate();', scope), true);
  assert.equal(app.completedQuits, 1, 'The reserved explicit install was stranded by ordinary window-close handling');
});

test('pending or failed installer cannot grant quit permission or hide the recovery window', () => {
  const {app, scope, nativeUpdater} = shellEventFixture();
  const window = {visible: true, isDestroyed: () => false, getNormalBounds: () => ({width: 1280, height: 720}),
    hide() {this.visible = false;}};
  scope.window = window;
  scope.controller = {stopped: false, updating: true, stop() {this.stopped = true;}, closeWindow() {window.hide();}, check() {}};
  scope.updateController = {reserved: true, installLaunchConfirmed: false,
    getState: () => ({status: 'installing'}), stop() {}};
  vm.runInContext('mainWindow = window; lifecycle = controller; updaterController = updateController;', scope);
  nativeUpdater.emit('before-quit-for-update');
  assert.equal(vm.runInContext('quitForUpdate();', scope), false);
  assert.equal(app.completedQuits, 0);
  assert.equal(window.visible, true);
  scope.updateController.installLaunchConfirmed = true;
  scope.updateController.reserved = false;
  assert.equal(vm.runInContext('quitForUpdate();', scope), false);
  assert.equal(app.completedQuits, 0);
  assert.equal(window.visible, true);
});

test('unsolicited native update quit cannot bypass a running game', async () => {
  const {app, scope, nativeUpdater} = shellEventFixture();
  const {LauncherLifecycle} = require('./lifecycle');
  scope.controller = new LauncherLifecycle({readStatus: async () => ({gameRunning: true, activeWork: false, keepLauncherOpen: true}),
    reserveShutdown: async () => false, hideWindow() {}, quit() {}});
  vm.runInContext('lifecycle = controller;', scope);
  nativeUpdater.emit('before-quit-for-update');
  app.quit();
  await scope.controller.check();
  assert.equal(app.completedQuits, 0);
});

test('updater initialization binds a dedicated secure session before any configured automatic check', () => {
  const {EventEmitter} = require('node:events');
  const order = [];
  let networkGuard;
  const scope = {USER_DATA: 'disposable', isPackaged: true, app: {getVersion: () => '3.41.30'},
    mainWindow: null, launcherUrl: 'http://127.0.0.1:4312', updaterController: null, disposeDesktopIpc: null,
    process: {argv: []}, isUpdateInstallSafe: async () => true, prepareUpdateRestart: async () => true, abortUpdateRestart: async () => {},
    ipcMain: {}, isPermittedUpdateUrl: require('./updater').isPermittedUpdateUrl,
    GITHUB_UPDATE_SOURCE: {url: 'https://github.com/abdulmunthakimwork/minecraft-launcher/'},
    require(name) {assert.equal(name, 'electron-updater'); return {NsisUpdater: EventEmitter};},
    UpdaterController: class {
      constructor(options) {this.options = options;}
      getState() {return {feedUrl: 'https://updates.example.test/launcher/'};}
      start() {order.push('start');}
    },
    registerDesktopIpc(options) {scope.ipcOptions = options; order.push('ipc'); return () => {};},
    session: {fromPartition(name, options) {
      assert.equal(name, 'electron-updater');
      assert.equal(options.cache, false);
      return {webRequest: {onBeforeRequest(handler) {networkGuard = handler; order.push('guard');}}};
    }},
  };
  scope.launchNsisInstaller = () => {};
  scope.quitForUpdate = () => {};
  const begin = extract('function beginUpdater(', 'function quitForUpdate(', 'beginUpdater', scope);
  begin();
  assert.deepEqual(order, ['guard', 'ipc', 'start']);
  assert.equal(scope.updaterController.options.defaultFeedUrl,
    'https://github.com/abdulmunthakimwork/minecraft-launcher/',
    'Installed launchers must receive the default GitHub source before automatic checks start');
  for (const [url, expected] of [
    ['https://updates.example.test/launcher/latest.yml', false],
    ['http://updates.example.test/launcher/latest.yml', true],
    ['https://another.example.test/launcher/latest.yml', true],
    ['https://updates.example.test/outside.exe', true],
  ]) {
    let result;
    networkGuard({url}, value => {result = value;});
    assert.equal(result.cancel, expected, url);
  }
  assert.equal(scope.ipcOptions.getAppInfo().version, '3.41.30');
  assert.equal(scope.ipcOptions.getAppInfo().packaged, true);
});
