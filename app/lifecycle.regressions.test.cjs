const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {spawn, spawnSync} = require('node:child_process');
const http = require('node:http');

const source = () => fs.readFileSync(path.join(__dirname, 'main.js'), 'utf8');
const alive = pid => {try {process.kill(pid, 0); return true;} catch {return false;}};

function lifecycleFixture(initial = {gameRunning: true, activeWork: false, keepLauncherOpen: true}) {
  const filename = path.join(__dirname, 'lifecycle.js');
  assert.ok(fs.existsSync(filename), 'Background lifecycle controller is not implemented');
  const {LauncherLifecycle} = require(filename);
  let snapshot = initial, clock = 0, reservation = true;
  const events = [];
  const lifecycle = new LauncherLifecycle({
    readStatus: async () => {if (snapshot instanceof Error) throw snapshot; return snapshot;},
    reserveShutdown: async () => reservation,
    resumeHost: async () => {events.push('resume');},
    hideWindow: () => {events.push('hide');},
    showWindow: () => {events.push('show');},
    quit: () => {events.push('quit');},
    now: () => clock,
  });
  return {lifecycle, events, snapshot(value) {snapshot = value;},
    clock(value) {clock = value;}, reservation(value) {reservation = value;}};
}

// Catches quitting during a running game and losing the same hidden session.
test('closing during a game retains the session and reopening shows it', async () => {
  const f = lifecycleFixture();
  f.lifecycle.closeWindow();
  await f.lifecycle.check();
  assert.deepEqual(f.events, ['hide']);
  await f.lifecycle.reopen();
  assert.deepEqual(f.events, ['hide', 'resume', 'show']);
  assert.equal(f.lifecycle.hidden, false);
});

// Catches leaving the background host alive after its game has actually ended.
test('a hidden launcher releases resources after a confirmed game exit', async () => {
  const f = lifecycleFixture();
  f.lifecycle.closeWindow();
  await f.lifecycle.check();
  f.snapshot({gameRunning: false, activeWork: false, keepLauncherOpen: true});
  f.clock(100);
  await f.lifecycle.check();
  assert.equal(f.events.includes('quit'), false);
  f.clock(2000);
  await f.lifecycle.check();
  assert.equal(f.events.filter(value => value === 'quit').length, 1);
});

// Catches a launch accepted after an idle snapshot being interrupted by close.
test('shutdown admission protects a launch that races a window close', async () => {
  const f = lifecycleFixture({gameRunning: false, activeWork: false, keepLauncherOpen: true});
  f.reservation(false);
  f.lifecycle.closeWindow();
  await f.lifecycle.check();
  f.clock(2000);
  await f.lifecycle.check();
  assert.equal(f.events.includes('quit'), false);
  f.snapshot({gameRunning: false, activeWork: true, keepLauncherOpen: true});
  await f.lifecycle.check();
  assert.equal(f.events.includes('quit'), false);
});

// Catches periodic polls rehiding a window the user explicitly reopened.
test('keepLauncherOpen false hides once on game start and permits reopening', async () => {
  const f = lifecycleFixture({gameRunning: false, activeWork: false, keepLauncherOpen: false});
  await f.lifecycle.check();
  f.snapshot({gameRunning: true, activeWork: false, keepLauncherOpen: false});
  await f.lifecycle.check();
  assert.deepEqual(f.events, ['hide']);
  await f.lifecycle.reopen();
  await f.lifecycle.check();
  assert.deepEqual(f.events, ['hide', 'resume', 'show']);
});

// Catches host preparation being mistaken for a live game and closing recovery.
test('real held launch preparation and failure leave the recovery window visible', async () => {
  const script = `
import contextlib, io, json, sys
sys.path.insert(0, sys.argv[1])
from test_lifecycle_http import LifecycleHttpTests
fixture = LifecycleHttpTests('test_held_preparation_is_active_work_then_failed_launch_returns_idle')
with contextlib.redirect_stdout(io.StringIO()):
    fixture.setUp()
    try:
        snapshots = fixture.preparation_failure_snapshots()
    finally:
        fixture.doCleanups()
print(json.dumps(snapshots))
`;
  const result = spawnSync(process.env.PYTHON || 'python', ['-B', '-c', script, path.join(__dirname, '..', 'harness')],
    {encoding: 'utf8', timeout: 10000, windowsHide: true});
  assert.equal(result.error, undefined, result.error && result.error.message);
  assert.equal(result.status, 0, result.stderr);
  const snapshots = JSON.parse(result.stdout);
  assert.equal(snapshots.pid, null, 'Synthetic fixture unexpectedly spawned a game');
  assert.deepEqual(snapshots.response, [409, {error: 'Synthetic Java preparation failure'}]);
  const f = lifecycleFixture(snapshots.idle);
  await f.lifecycle.check();
  f.snapshot(snapshots.preparing);
  f.clock(100);
  await f.lifecycle.check();
  assert.equal(await f.lifecycle.isUpdateInstallSafe(), false, 'Preparation permitted an update restart');
  f.snapshot(snapshots.failed);
  f.clock(200);
  await f.lifecycle.check();
  f.clock(2200);
  await f.lifecycle.check();
  assert.deepEqual(f.events, [], 'Failed startup hid or quit the error/retry window');
  assert.equal(f.lifecycle.hidden, false);
  assert.equal(f.lifecycle.stopped, false);
});

test('keepLauncherOpen true retains the visible window on game start', async () => {
  const f = lifecycleFixture();
  await f.lifecycle.check();
  assert.deepEqual(f.events, []);
});

// Catches treating unavailable or incomplete host status as idle.
test('status failure or malformed status cannot permit shutdown or updates', async () => {
  const f = lifecycleFixture();
  f.lifecycle.closeWindow();
  for (const snapshot of [new Error('host unavailable'), {},
                          {gameRunning: false, activeWork: false, keepLauncherOpen: 'false'}]) {
    f.snapshot(snapshot);
    f.clock(5000);
    await f.lifecycle.check();
    assert.equal(await f.lifecycle.prepareUpdateRestart(), false);
    assert.equal(f.events.includes('quit'), false);
  }
});

// Catches using a stale idle snapshot or allowing paused/commit work to restart.
test('update restart requires fresh idle status and shutdown admission', async () => {
  const f = lifecycleFixture({gameRunning: false, activeWork: false, keepLauncherOpen: true});
  await f.lifecycle.check();
  for (const snapshot of [
    {gameRunning: true, activeWork: false, keepLauncherOpen: true},
    {gameRunning: false, activeWork: true, keepLauncherOpen: true},
  ]) {
    f.snapshot(snapshot);
    assert.equal(await f.lifecycle.prepareUpdateRestart(), false);
  }
  f.snapshot({gameRunning: false, activeWork: false, keepLauncherOpen: true});
  f.reservation(false);
  assert.equal(await f.lifecycle.prepareUpdateRestart(), false);
  f.reservation(true);
  assert.equal(await f.lifecycle.prepareUpdateRestart(), true);
  await f.lifecycle.abortUpdateRestart();
  assert.equal(f.events.at(-1), 'resume');
});

// Catches an old close decision winning after a shortcut/tray reopen.
test('reopen during shutdown admission waits then resumes without quitting', async () => {
  const f = lifecycleFixture({gameRunning: false, activeWork: false, keepLauncherOpen: true});
  let grant;
  f.lifecycle.options.reserveShutdown = () => new Promise(resolve => {grant = resolve;});
  f.lifecycle.closeWindow();
  await f.lifecycle.check();
  f.clock(2000);
  const check = f.lifecycle.check();
  await new Promise(resolve => setImmediate(resolve));
  const reopen = f.lifecycle.reopen();
  grant(true);
  await Promise.all([check, reopen]);
  assert.deepEqual(f.events, ['hide', 'resume', 'show']);
});

test('a hidden launcher recovers from status failure and exits after idle returns', async () => {
  const f = lifecycleFixture(new Error('temporary host status failure'));
  f.lifecycle.closeWindow();
  await f.lifecycle.check();
  f.snapshot({gameRunning: false, activeWork: false, keepLauncherOpen: true});
  f.clock(100);
  await f.lifecycle.check();
  f.clock(2000);
  await f.lifecycle.check();
  assert.equal(f.events.at(-1), 'quit');
});

// Catches leaving the host admission latch set if the acknowledgment is lost.
test('failed update admission resumes the host and allows another attempt', async () => {
  const f = lifecycleFixture({gameRunning: false, activeWork: false, keepLauncherOpen: true});
  f.lifecycle.options.reserveShutdown = async () => {throw new Error('response lost');};
  assert.equal(await f.lifecycle.prepareUpdateRestart(), false);
  assert.equal(f.events.at(-1), 'resume');
  f.lifecycle.options.reserveShutdown = async () => true;
  assert.equal(await f.lifecycle.prepareUpdateRestart(), true);
});

// Catches a failed abort response permanently blocking tray/shortcut recovery.
test('an updater abort status failure still permits reopening and retrying resume', async () => {
  const f = lifecycleFixture({gameRunning: false, activeWork: false, keepLauncherOpen: true});
  assert.equal(await f.lifecycle.prepareUpdateRestart(), true);
  f.lifecycle.options.resumeHost = async () => {throw new Error('resume temporarily unavailable');};
  await assert.rejects(f.lifecycle.abortUpdateRestart(), /temporarily unavailable/);
  f.lifecycle.options.resumeHost = async () => {f.events.push('resume');};
  await f.lifecycle.reopen();
  assert.deepEqual(f.events, ['resume', 'show']);
});

async function hostFixture(t, handler) {
  const server = http.createServer(handler);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const {createLifecycleHost} = require('./lifecycle.js');
  assert.equal(typeof createLifecycleHost, 'function', 'Bounded loopback lifecycle transport is not implemented');
  return createLifecycleHost('http://127.0.0.1:' + server.address().port);
}

// Catches incorrect route/method/header integration, against real loopback HTTP.
test('lifecycle transport reads status and sends shutdown/resume to its local host', async t => {
  const host = await hostFixture(t, (req, res) => {
    res.setHeader('Content-Type', 'application/json');
    if (req.method === 'GET' && req.url === '/__lifecycle') {
      return res.end(JSON.stringify({gameRunning: true, activeWork: false, keepLauncherOpen: false}));
    }
    if (req.method !== 'POST' || req.headers.origin !== 'http://127.0.0.1:' + req.socket.localPort) {
      res.writeHead(400); return res.end('{}');
    }
    if (req.url === '/__lifecycle/prepare-shutdown') return res.end('{"safeToStop":false}');
    if (req.url === '/__lifecycle/resume') return res.end('{"ok":true}');
    res.writeHead(404); res.end('{}');
  });
  assert.deepEqual(await host.readStatus(), {gameRunning: true, activeWork: false, keepLauncherOpen: false});
  assert.equal(await host.reserveShutdown(), false);
  await host.resumeHost();
});

test('lifecycle transport refuses redirects and malformed status', async t => {
  let redirect = true;
  const host = await hostFixture(t, (req, res) => {
    if (redirect) {res.writeHead(302, {Location: 'https://example.invalid'}); return res.end();}
    res.end('{"gameRunning":false}');
  });
  await assert.rejects(host.readStatus(), /status|HTTP|302/i);
  redirect = false;
  await assert.rejects(host.readStatus(), /incomplete|status/i);
});

test('lifecycle transport bounds response size and rejects foreign host URLs', async t => {
  const host = await hostFixture(t, (req, res) => res.end('x'.repeat(17000)));
  await assert.rejects(host.readStatus(), /large|size/i);
  const {createLifecycleHost} = require('./lifecycle.js');
  for (const url of ['https://example.invalid', 'http://127.0.0.1:80@remote.invalid', 'http://user@127.0.0.1:80']) {
    assert.throws(() => createLifecycleHost(url), /local|loopback|host/i);
  }
});

// Catches an idle-only request timeout that permits a trickling host to retain
// a hidden launcher forever; the deadline covers the entire response.
test('lifecycle transport times out a continuously trickling response', async t => {
  const host = await hostFixture(t, (req, res) => {
    const timer = setInterval(() => res.write(' '), 40);
    res.on('close', () => clearInterval(timer));
  });
  const began = Date.now();
  await assert.rejects(host.readStatus(), /timed out/i);
  assert.ok(Date.now() - began < 3500, 'Lifecycle request had no bounded completion deadline');
});

test('lifecycle transport rejects incomplete shutdown and resume acknowledgments', async t => {
  const host = await hostFixture(t, (req, res) => res.end('{"ok":false}'));
  await assert.rejects(host.reserveShutdown(), /incomplete|admission/i);
  await assert.rejects(host.resumeHost(), /resume/i);
});

test('host exit releases a hidden session even if its last status was unavailable', async () => {
  const f = lifecycleFixture(new Error('host unavailable'));
  f.lifecycle.closeWindow();
  await f.lifecycle.check();
  f.lifecycle.hostStopped();
  assert.equal(f.lifecycle.stopped, true);
  assert.equal(f.events.at(-1), 'quit');
});

// Catches allowing an update readiness check to retain a previously idle result.
test('update readiness rejects newly active work and unavailable host status', async () => {
  const f = lifecycleFixture({gameRunning: false, activeWork: false, keepLauncherOpen: true});
  assert.equal(await f.lifecycle.isUpdateInstallSafe(), true);
  f.snapshot({gameRunning: false, activeWork: true, keepLauncherOpen: true});
  assert.equal(await f.lifecycle.isUpdateInstallSafe(), false);
  f.snapshot(new Error('status lost'));
  assert.equal(await f.lifecycle.isUpdateInstallSafe(), false);
});

// Catches restoring process-tree shutdown: a harmless independently running
// descendant must survive the launcher-owned host's shutdown.
test('stopping the host preserves its running descendant', async () => {
  const host = spawn(process.execPath, ['-e', `
    const {spawn} = require('node:child_process');
    const game = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'],
                       {detached: true, stdio: 'ignore', windowsHide: true});
    game.unref();
    console.log(game.pid);
    setInterval(() => {}, 1000);
  `], {stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true});
  let descendant;
  try {
    descendant = await new Promise((resolve, reject) => {
      host.stdout.once('data', value => resolve(Number(value.toString().trim())));
      host.once('error', reject);
      host.once('exit', () => reject(new Error('owned fixture exited before reporting PID')));
    });
    assert.ok(Number.isSafeInteger(descendant) && descendant > 0);
    const script = source();
    const begin = script.indexOf('function stopHost('), end = script.indexOf('// --- window', begin);
    const scope = {serverProcess: host, process, spawnSync};
    vm.createContext(scope);
    vm.runInContext(script.slice(begin, end) + '\nglobalThis.stop = stopHost;', scope);
    scope.stop();
    await new Promise(resolve => setTimeout(resolve, 180));
    assert.equal(alive(descendant), true, 'Host shutdown terminated its descendant');
  } finally {
    if (host.pid && alive(host.pid)) host.kill();
    if (descendant && alive(descendant)) process.kill(descendant);
  }
});
