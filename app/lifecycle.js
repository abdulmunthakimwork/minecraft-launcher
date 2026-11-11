const http = require('node:http');

function trustedStatus(value) {
  if (!value || ['gameRunning', 'activeWork', 'keepLauncherOpen'].some(key => typeof value[key] !== 'boolean')) {
    throw new Error('The launcher host returned an incomplete lifecycle status.');
  }
  return value;
}

// Owns the background session, not Minecraft. Shutdown admission in the host
// closes the gap between observing idle and a concurrent launch being accepted.
class LauncherLifecycle {
  constructor(options) {
    this.options = options;
    this.now = options.now || Date.now;
    this.hidden = false;
    this.status = null;
    this.lastGameRunning = false;
    this.idleSince = null;
    this.epoch = 0;
    this.updating = false;
    this.stopped = false;
    this.inFlight = null;
  }

  closeWindow() {
    if (this.stopped) return;
    this.epoch++;
    this.hidden = true;
    this.idleSince = null;
    this.options.hideWindow();
  }

  async reopen() {
    if (this.stopped || this.updating) return;
    const epoch = ++this.epoch;
    this.hidden = false;
    this.idleSince = null;
    // A prepare-shutdown request already sent must settle before resuming;
    // otherwise the host could process resume first and acquire the gate later.
    if (this.inFlight) await this.inFlight;
    try {
      await this.options.resumeHost();
    } finally {
      if (!this.stopped && epoch === this.epoch && !this.hidden) this.options.showWindow();
    }
  }

  check() {
    if (this.stopped || this.updating) return Promise.resolve();
    if (this.inFlight) return this.inFlight;
    this.inFlight = this._check().finally(() => {this.inFlight = null;});
    return this.inFlight;
  }

  async _readStatus() {
    try {
      this.status = trustedStatus(await this.options.readStatus());
    } catch (error) {
      this.status = null;
      this.idleSince = null;
      if (this.options.statusError) this.options.statusError(error);
    }
    if (this.options.statusChanged) this.options.statusChanged(this.status);
    return this.status;
  }

  async _check() {
    const epoch = this.epoch;
    const status = await this._readStatus();
    if (!status || this.stopped || this.updating) return;
    const gameStarted = status.gameRunning && !this.lastGameRunning;
    this.lastGameRunning = status.gameRunning;
    if (epoch !== this.epoch) return;
    if (gameStarted && !status.keepLauncherOpen && !this.hidden) this.closeWindow();
    if (!this.hidden || status.gameRunning || status.activeWork) {
      this.idleSince = null;
      return;
    }
    if (this.idleSince === null) this.idleSince = this.now();
    // Let already dispatched UI/host launch requests become visible before the
    // authoritative shutdown gate, without leaving an idle host indefinitely.
    if (this.now() - this.idleSince < 1500) return;
    const closeEpoch = this.epoch;
    let safe = false;
    try {safe = await this.options.reserveShutdown();} catch {this.idleSince = null;}
    if (safe !== true || closeEpoch !== this.epoch || !this.hidden || this.updating || this.stopped) return;
    this.stop();
    this.options.quit();
  }

  async isUpdateInstallSafe() {
    const status = await this._readStatus();
    return !!status && !status.gameRunning && !status.activeWork && !this.stopped && !this.updating;
  }

  async prepareUpdateRestart() {
    if (this.stopped || this.updating) return false;
    this.updating = true;
    const epoch = this.epoch;
    let prepared = false;
    let attempted = false;
    try {
      if (this.inFlight) await this.inFlight;
      const status = await this._readStatus();
      if (!status || status.gameRunning || status.activeWork || epoch !== this.epoch) return false;
      attempted = true;
      if (await this.options.reserveShutdown() !== true) return false;
      prepared = true;
      return prepared;
    } catch {
      return false;
    } finally {
      // On success hold admission closed until the updater quits or aborts.
      if (!prepared) {
        if (attempted) {
          try {await this.options.resumeHost();} catch (error) {
            if (this.options.statusError) this.options.statusError(error);
          }
        }
        this.updating = false;
      }
    }
  }

  async abortUpdateRestart() {
    try {
      await this.options.resumeHost();
    } finally {
      // A temporarily unavailable host must not leave the shell permanently
      // in update mode; tray/shortcut reopening can retry the resume request.
      this.updating = false;
      this.idleSince = null;
    }
  }

  start() {
    if (this.timer || this.stopped) return;
    this.timer = setInterval(() => this.check(), 1000);
    this.check();
  }

  stop() {
    this.stopped = true;
    clearInterval(this.timer);
    this.timer = null;
  }

  hostStopped() {
    this.stop();
    if (this.hidden) this.options.quit();
  }
}

function createLifecycleHost(baseUrl) {
  const base = new URL(baseUrl);
  if (base.protocol !== 'http:' || base.hostname !== '127.0.0.1' || base.username || base.password ||
      base.pathname !== '/' || base.search || base.hash) {
    throw new Error('Lifecycle requests require the launcher loopback host.');
  }
  function request(route, method = 'GET') {
    return new Promise((resolve, reject) => {
      let finished = false;
      const complete = (error, value) => {
        if (finished) return;
        finished = true;
        clearTimeout(deadline);
        if (error) {req.destroy(); reject(error);} else resolve(value);
      };
      const headers = {Accept: 'application/json', Origin: base.origin};
      if (method === 'POST') Object.assign(headers, {'Content-Type': 'application/json', 'Content-Length': '2'});
      const req = http.request(new URL(route, base), {method, headers}, res => {
        if (res.statusCode !== 200) {
          res.resume();
          return complete(new Error('Lifecycle host HTTP status ' + res.statusCode));
        }
        const chunks = [];
        let bytes = 0;
        res.on('data', chunk => {
          bytes += chunk.length;
          if (bytes > 16384) return complete(new Error('Lifecycle host response is too large.'));
          chunks.push(chunk);
        });
        res.on('error', complete);
        res.on('aborted', () => complete(new Error('Lifecycle host response was interrupted.')));
        res.on('end', () => {
          try {complete(null, JSON.parse(Buffer.concat(chunks).toString('utf8')));}
          catch (error) {complete(error);}
        });
      });
      const deadline = setTimeout(() => complete(new Error('Lifecycle host status timed out.')), 2000);
      req.on('error', complete);
      req.end(method === 'POST' ? '{}' : undefined);
    });
  }
  return {
    readStatus: async () => trustedStatus(await request('/__lifecycle')),
    reserveShutdown: async () => {
      const result = await request('/__lifecycle/prepare-shutdown', 'POST');
      if (!result || typeof result.safeToStop !== 'boolean') throw new Error('Incomplete shutdown admission status.');
      return result.safeToStop;
    },
    resumeHost: async () => {
      const result = await request('/__lifecycle/resume', 'POST');
      if (!result || result.ok !== true) throw new Error('The launcher host could not resume its session.');
    },
  };
}

module.exports = {LauncherLifecycle, trustedStatus, createLifecycleHost};
