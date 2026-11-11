const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {EventEmitter} = require('node:events');
const {registerDesktopIpc, IPC_CHANNELS, UPDATE_CHANNEL} = require('./updater');

function desktopFixture() {
  const frame = {url: 'http://127.0.0.1:4312/games/java'};
  const sent = [];
  const wc = {mainFrame: frame, send(channel, state) {sent.push({channel, state});}};
  const window = {webContents: wc, isDestroyed: () => false};
  const handlers = new Map();
  const ipcMain = {handle: (channel, handler) => handlers.set(channel, handler), removeHandler: channel => handlers.delete(channel)};
  const controller = new EventEmitter();
  Object.assign(controller, {refreshInstallAllowed: async () => ({status: 'ready', installAllowed: false}),
    configureFeed: async url => ({status: 'idle', feedUrl: url}),
    checkForUpdates: async () => ({status: 'checking'}), restartAndInstall: async () => ({status: 'installing'})});
  const dispose = registerDesktopIpc({ipcMain, controller, getWindow: () => window,
    getLauncherUrl: () => 'http://127.0.0.1:4312/', getAppInfo: () => ({version: '3.41.30', packaged: true})});
  return {frame, wc, window, controller, sent, handlers, dispose, event: {sender: wc, senderFrame: frame}};
}

test('desktop IPC accepts only actual launcher webContents exact main frame and local origin', async () => {
  const {event, handlers, frame} = desktopFixture();
  const handler = handlers.get(IPC_CHANNELS.info);
  assert.deepEqual(await handler(event), {version: '3.41.30', packaged: true});
  for (const value of [{sender: {}, senderFrame: frame}, {sender: event.sender, senderFrame: {...frame}},
    {sender: event.sender, senderFrame: null}]) assert.throws(() => handler(value), /main frame/);
  for (const url of ['http://127.0.0.1:43123/', 'https://example.test/', 'http://user@127.0.0.1:4312/']) {
    frame.url = url;
    assert.throws(() => handler(event), /main frame/);
  }
});

test('desktop IPC validates arguments and stops publishing when page leaves launcher origin', async () => {
  const {event, handlers, frame, controller, sent, dispose} = desktopFixture();
  assert.throws(() => handlers.get(IPC_CHANNELS.check)(event, 'extra'), /arguments/);
  assert.throws(() => handlers.get(IPC_CHANNELS.configure)(event), /arguments/);
  assert.deepEqual(await handlers.get(IPC_CHANNELS.configure)(event, 'https://updates.example.test/'),
    {status: 'idle', feedUrl: 'https://updates.example.test/'});
  controller.emit('state', {status: 'ready'});
  assert.deepEqual(sent, [{channel: UPDATE_CHANNEL, state: {status: 'ready'}}]);
  frame.url = 'https://remote.example.test/';
  controller.emit('state', {status: 'error'});
  assert.equal(sent.length, 1);
  dispose();
  assert.equal(handlers.size, 0);
});

test('sandboxed preload exposes fixed desktop operations and strips IPC event from subscriptions', async () => {
  const filename = path.join(__dirname, 'preload.js');
  assert.ok(fs.existsSync(filename), 'The desktop facade has no preload');
  const ipc = new EventEmitter();
  const calls = [];
  ipc.invoke = async (channel, ...args) => {calls.push({channel, args}); return {status: 'setup'};};
  let facade;
  const scope = {require(name) {
    assert.equal(name, 'electron', 'Sandboxed preload used an unavailable Node module');
    return {ipcRenderer: ipc, contextBridge: {exposeInMainWorld(name, api) {
      assert.equal(name, 'launcherDesktop'); facade = api;
    }}};
  }};
  vm.runInNewContext(fs.readFileSync(filename, 'utf8'), scope);
  assert.equal(Object.isFrozen(facade), true);
  assert.equal(facade.send, undefined);
  assert.equal(facade.invoke, undefined);
  await facade.getAppInfo();
  await facade.getUpdateState();
  await facade.configureUpdateFeed('https://updates.example.test/');
  await facade.checkForUpdates();
  await facade.restartAndInstall();
  assert.deepEqual(calls.map(value => value.channel), [IPC_CHANNELS.info, IPC_CHANNELS.state,
    IPC_CHANNELS.configure, IPC_CHANNELS.check, IPC_CHANNELS.install]);
  assert.deepEqual(calls[2].args, ['https://updates.example.test/']);
  let received;
  const unsubscribe = facade.onUpdateState((...args) => {received = args;});
  ipc.emit(UPDATE_CHANNEL, {sender: 'private IPC event'}, {status: 'ready'});
  assert.deepEqual(received, [{status: 'ready'}]);
  unsubscribe();
  assert.equal(ipc.listenerCount(UPDATE_CHANNEL), 0);
});
