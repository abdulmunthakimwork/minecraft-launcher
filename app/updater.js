const {EventEmitter} = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {spawn} = require('node:child_process');

const GITHUB_UPDATE_SOURCE = Object.freeze({url: 'https://github.com/abdulmunthakimwork/minecraft-launcher/',
  owner: 'abdulmunthakimwork', repo: 'minecraft-launcher', repositoryId: '1453904618'});
const UPDATE_CHANNEL = 'launcher-desktop:update-state';
const IPC_CHANNELS = Object.freeze({info: 'launcher-desktop:info', state: 'launcher-desktop:update-state:get',
  configure: 'launcher-desktop:update-feed:set', check: 'launcher-desktop:update-check',
  install: 'launcher-desktop:update-install'});

function validateFeed(value) {
  if (typeof value !== 'string' || value.length > 2048) throw new Error('The update source must be an HTTPS directory URL.');
  if (!value.trim()) return '';
  let url;
  try {url = new URL(value.trim());} catch {throw new Error('The update source must be an HTTPS directory URL.');}
  if (url.protocol !== 'https:' || !url.hostname || url.username || url.password || url.search || url.hash ||
      /\.(?:ya?ml|json|exe)$/i.test(url.pathname) || /%2f|%5c|%00/i.test(url.pathname)) {
    throw new Error('The update source must be an HTTPS directory without credentials, query, fragment or metadata filename.');
  }
  if (!url.pathname.endsWith('/')) url.pathname += '/';
  return url.href;
}

function isPermittedUpdateUrl(target, feedUrl) {
  if (!feedUrl) return false;
  try {
    const url = new URL(target), feed = new URL(feedUrl);
    if (feed.href === GITHUB_UPDATE_SOURCE.url) {
      // Check the original path as well as URL's normalized path: URL parsing
      // removes dot segments, which must not disguise a disallowed request.
      const rawPath = /^https:\/\/[^/?#]+([^?#]*)/i.exec(String(target))?.[1];
      if (url.protocol !== 'https:' || url.port || url.username || url.password || url.hash ||
          rawPath !== url.pathname || /%|\\/.test(rawPath)) return false;
      if (url.hostname === 'release-assets.githubusercontent.com') {
        return new RegExp(`^/github-production-release-asset/${GITHUB_UPDATE_SOURCE.repositoryId}/` +
          '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', 'i').test(url.pathname);
      }
      if (url.hostname !== 'github.com' || url.search) return false;
      const releases = `/${GITHUB_UPDATE_SOURCE.owner}/${GITHUB_UPDATE_SOURCE.repo}/releases`;
      if (url.pathname === releases + '.atom' || url.pathname === releases + '/latest') return true;
      if (!url.pathname.startsWith(releases + '/')) return false;
      const relative = url.pathname.slice(releases.length);
      const match = /^\/(?:tag|download)\/v([^/]+)(?:\/([^/]+))?$/.exec(relative);
      if (!match) return false;
      versionParts(match[1]);
      if (relative.startsWith('/tag/')) return match[2] === undefined;
      return match[2] === 'latest.yml' || match[2] === `Minecraft-Launcher-Setup-${match[1]}.exe`;
    }
    return url.protocol === 'https:' && url.origin === feed.origin && !url.username && !url.password && !url.hash &&
      url.pathname.startsWith(feed.pathname) && !/%2f|%5c|%00/i.test(url.pathname);
  } catch {return false;}
}

function versionParts(version) {
  if (typeof version !== 'string' || !/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(version)) {
    throw new Error('The update feed contains an invalid stable release version.');
  }
  const parts = version.split('.').map(Number);
  if (parts.some(part => !Number.isSafeInteger(part))) throw new Error('The update version is too large.');
  return parts;
}

function isNewerVersion(candidate, current) {
  const next = versionParts(candidate), previous = versionParts(current);
  for (let i = 0; i < 3; i++) {
    if (next[i] !== previous[i]) return next[i] > previous[i];
  }
  return false;
}

function validateUpdateInfo(info, currentVersion) {
  if (!info || !isNewerVersion(info.version, currentVersion)) throw new Error('The update must be newer than the installed launcher.');
  const filename = `Minecraft-Launcher-Setup-${info.version}.exe`;
  if (!Array.isArray(info.files) || info.files.length !== 1 || info.packages || info.files[0].url !== filename) {
    throw new Error('The feed must identify one relative Minecraft Launcher setup filename.');
  }
  const file = info.files[0];
  if (!Number.isSafeInteger(file.size) || file.size <= 0 || file.size > 2 * 1024 * 1024 * 1024 ||
      typeof file.sha512 !== 'string' || !/^[A-Za-z0-9+/]{86}==$/.test(file.sha512) ||
      Buffer.from(file.sha512, 'base64').length !== 64) {
    throw new Error('The update metadata needs a valid installer size and SHA-512 verification hash.');
  }
  if (file.isAdminRightsRequired !== undefined && typeof file.isAdminRightsRequired !== 'boolean') {
    throw new Error('The update metadata contains an invalid administrator permission flag.');
  }
  return {version: info.version, filename, size: file.size, sha512: file.sha512,
    requiresElevation: file.isAdminRightsRequired === true};
}

async function verifyInstaller(filename, expected) {
  if (typeof filename !== 'string' || path.basename(filename) !== expected.filename) {
    throw new Error('The staged installer filename does not match the verified release.');
  }
  const stat = await fs.promises.lstat(filename);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size !== expected.size) {
    throw new Error('Installer size verification failed. Check the uploaded installer and release metadata.');
  }
  const hash = crypto.createHash('sha512');
  for await (const chunk of fs.createReadStream(filename)) hash.update(chunk);
  if (hash.digest('base64') !== expected.sha512) {
    throw new Error('Installer checksum verification failed. Check the uploaded installer and release metadata.');
  }
}

function spawnInstallerProcess(command, args, spawnProcess, waitForExit = false) {
  return new Promise((resolve, reject) => {
    let settled = false, child;
    const complete = error => {
      if (settled) return;
      settled = true;
      if (child) {
        child.removeListener('spawn', spawned);
        child.removeListener('exit', exited);
        // Leave the error listener until its first event; a later process
        // error must not become an unhandled EventEmitter error after launch.
      }
      if (error) reject(error);
      else {child.unref(); resolve(true);}
    };
    const spawned = () => {if (!waitForExit) complete();};
    const exited = (code, signal) => {
      if (waitForExit && code === 0 && !signal) complete();
      else complete(new Error('The update installer could not be elevated or started. Approve the Windows permission request and retry.'));
    };
    try {
      child = spawnProcess(command, args, {detached: true, stdio: 'ignore', windowsHide: true, shell: false});
      child.once('error', complete);
      child.once('spawn', spawned);
      child.once('exit', exited);
    } catch (error) {complete(error);}
  });
}

function launchNsisInstaller(installer, options = {}) {
  if (typeof installer !== 'string' || !path.isAbsolute(installer) || !installer.toLowerCase().endsWith('.exe')) {
    return Promise.reject(new Error('The update installer path is invalid.'));
  }
  const spawnProcess = options.spawnProcess || spawn;
  const args = ['--updated', '--force-run'];
  const elevated = () => {
    try {
      const helper = path.join(options.resourcesPath, 'elevate.exe');
      const stat = fs.lstatSync(helper);
      if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('Invalid elevation helper.');
      // The bundled helper exits after ShellExecuteEx launches the target;
      // omitting its -wait option avoids waiting for the installer to finish.
      // Its successful exit confirms UAC acceptance, unlike helper spawn.
      return spawnInstallerProcess(helper, [installer, ...args], spawnProcess, true);
    } catch {
      return Promise.reject(new Error('The update installer needs administrator permission, but its elevation helper is missing. Reinstall the launcher or retry with sufficient permission.'));
    }
  };
  if (options.requiresElevation === true) return elevated();
  return spawnInstallerProcess(installer, args, spawnProcess).catch(error => {
    if (['EACCES', 'EPERM', 'UNKNOWN'].includes(error && error.code)) return elevated();
    throw error;
  });
}

function publicError(error, github = false) {
  const message = String(error && (error.code || error.message) || error || 'Unknown update error');
  if (/CHECKSUM|checksum|verification|SHA-512|size/i.test(message)) {
    return 'The installer failed size or checksum verification. Check the hosted release files and try again.';
  }
  if (/CHANNEL_FILE_NOT_FOUND|404/.test(message)) return github
    ? 'latest.yml was not found in the latest stable GitHub release. Check the published release files and try again.'
    : 'latest.yml was not found in the configured update directory. Check the hosted release files.';
  if (github && /LATEST_VERSION_NOT_FOUND|NO_PUBLISHED_VERSIONS|INVALID_RELEASE_FEED/.test(message)) {
    return 'The latest stable GitHub release could not be found. Check the published release and connection, then try again.';
  }
  if (/ENOTFOUND|ECONN|CERT|SSL|TIMEDOUT|ERR_FAILED|network/i.test(message)) {
    return 'The HTTPS update source could not be reached securely. Check the feed address and connection, then try again.';
  }
  // Do not expose server-controlled payloads, URLs with tokens, or local paths.
  if (/^The (?:update|feed|staged)|^Installer/.test(message)) return message.slice(0, 300);
  return 'The launcher update could not complete. Check the update source and try again.';
}

class UpdaterController extends EventEmitter {
  constructor(options) {
    super();
    this.options = options;
    this.updater = options.updater;
    this.configFile = path.join(options.userData, 'update-feed.json');
    this.inFlight = null;
    this.installInFlight = null;
    this.staged = null;
    this.reserved = false;
    this.installLaunchConfirmed = false;
    this.started = false;
    this.stopped = false;
    let feedUrl = '';
    try {
      if (fs.statSync(this.configFile).size <= 8192) {
        const value = JSON.parse(fs.readFileSync(this.configFile, 'utf8'));
        if (value.schemaVersion === 1) feedUrl = validateFeed(value.feedUrl);
      }
    } catch (error) {
      // Only a missing choice inherits the injected default. Invalid saved
      // configuration stays disabled, and an explicitly saved empty URL wins.
      if (error.code === 'ENOENT' && options.defaultFeedUrl !== undefined) feedUrl = validateFeed(options.defaultFeedUrl);
    }
    this.state = {status: feedUrl ? 'idle' : 'setup', currentVersion: options.currentVersion,
      feedUrl, availableVersion: null, progress: null, error: null, installAllowed: false, lastCheckedAt: null};
    this.updater.autoDownload = false; // Validate metadata before permitting acquisition.
    this.updater.autoInstallOnAppQuit = false;
    this.updater.autoRunAppAfterInstall = true;
    this.updater.allowDowngrade = false;
    this.updater.allowPrerelease = false;
    this.updater.disableWebInstaller = true;
    this.updater.disableDifferentialDownload = true;
    if (feedUrl && this.updater.setFeedURL) this._applyFeed();
    this.progressListener = progress => {
      if (this.state.status !== 'downloading') return;
      const finite = value => Number.isFinite(value) && value >= 0 ? value : 0;
      this._set({progress: {percent: Math.min(100, finite(progress.percent)),
        transferred: finite(progress.transferred), total: finite(progress.total), bytesPerSecond: finite(progress.bytesPerSecond)}});
    };
    this.errorListener = error => {
      if (['checking', 'downloading', 'installing'].includes(this.state.status)) this._fail(error);
    };
    this.updater.on('download-progress', this.progressListener);
    this.updater.on('error', this.errorListener);
  }

  getState() {return JSON.parse(JSON.stringify(this.state));}

  _set(value) {
    Object.assign(this.state, value);
    this.emit('state', this.getState());
  }

  _applyFeed() {
    this.updater.setFeedURL(this.state.feedUrl === GITHUB_UPDATE_SOURCE.url
      ? {provider: 'github', owner: GITHUB_UPDATE_SOURCE.owner, repo: GITHUB_UPDATE_SOURCE.repo, channel: 'latest'}
      : {provider: 'generic', url: this.state.feedUrl, channel: 'latest', useMultipleRangeRequest: false});
  }

  async configureFeed(value) {
    if (this.inFlight || this.installInFlight || this.reserved) throw new Error('An update is in progress. Wait before changing the source.');
    const feedUrl = validateFeed(value);
    fs.mkdirSync(path.dirname(this.configFile), {recursive: true});
    const temporary = this.configFile + '.' + crypto.randomBytes(6).toString('hex') + '.tmp';
    try {
      fs.writeFileSync(temporary, JSON.stringify({schemaVersion: 1, feedUrl}) + '\n', {flag: 'wx'});
      fs.renameSync(temporary, this.configFile);
    } finally {try {fs.unlinkSync(temporary);} catch {}}
    this.staged = null;
    this._set({feedUrl, status: feedUrl ? 'idle' : 'setup', availableVersion: null, progress: null, error: null, installAllowed: false, lastCheckedAt: null});
    if (feedUrl) this._applyFeed();
    if (this.started && feedUrl) setImmediate(() => this.checkForUpdates());
    return this.getState();
  }

  checkForUpdates() {
    if (!this.state.feedUrl || this.stopped) return Promise.resolve(this.getState());
    if (this.installInFlight || this.state.status === 'installing') return Promise.resolve(this.getState());
    if (this.inFlight) return this.inFlight;
    this.inFlight = this._check().finally(() => {this.inFlight = null;});
    return this.inFlight;
  }

  async _check() {
    this.staged = null;
    this._set({status: 'checking', availableVersion: null, progress: null, error: null, installAllowed: false});
    try {
      if (!this.options.isPackaged) throw new Error('The update checker requires an installed launcher.');
      const result = await this.updater.checkForUpdates();
      if (!result) throw new Error('The update checker did not return release metadata.');
      if (this.state.feedUrl === GITHUB_UPDATE_SOURCE.url) {
        const info = result.updateInfo;
        versionParts(info && info.version);
        if (info.tag !== `v${info.version}`) throw new Error('The update release tag does not match its stable metadata version.');
      }
      if (result.isUpdateAvailable === false) {
        versionParts(result.updateInfo && result.updateInfo.version);
        this._set({status: 'idle', lastCheckedAt: new Date().toISOString()});
        return this.getState();
      }
      const expected = validateUpdateInfo(result.updateInfo, this.options.currentVersion);
      this._set({status: 'downloading', availableVersion: expected.version,
        progress: {percent: 0, transferred: 0, total: expected.size, bytesPerSecond: 0}});
      const paths = await this.updater.downloadUpdate(result.cancellationToken);
      if (!Array.isArray(paths) || paths.length !== 1) throw new Error('The staged update did not contain one installer.');
      await verifyInstaller(paths[0], expected);
      this.staged = {filename: paths[0], expected};
      this._set({status: 'ready', progress: {percent: 100, transferred: expected.size, total: expected.size, bytesPerSecond: 0}, error: null, lastCheckedAt: new Date().toISOString()});
      await this.refreshInstallAllowed();
    } catch (error) {await this._fail(error);}
    return this.getState();
  }

  async refreshInstallAllowed() {
    if (this.state.status !== 'ready') return this.getState();
    let safe = false;
    try {safe = await this.options.isUpdateInstallSafe() === true;} catch {}
    if (this.state.status === 'ready' && safe !== this.state.installAllowed) this._set({installAllowed: safe});
    return this.getState();
  }

  restartAndInstall() {
    if (this.installInFlight) return this.installInFlight;
    this.installInFlight = this._install().finally(() => {this.installInFlight = null;});
    return this.installInFlight;
  }

  async _install() {
    if (this.state.status !== 'ready' || !this.staged) return this.getState();
    try {
      await verifyInstaller(this.staged.filename, this.staged.expected);
      await this.refreshInstallAllowed();
      if (!this.state.installAllowed) return this.getState();
      if (await this.options.prepareUpdateRestart() !== true) {
        this._set({installAllowed: false});
        return this.getState();
      }
      this.reserved = true;
      this.installLaunchConfirmed = false;
      this._set({status: 'installing', installAllowed: false, error: null});
      // BaseUpdater.quitAndInstall queues app.quit before asynchronous process
      // launch can fail. Own and await that boundary instead; no upstream quit
      // callback or install latch is created, so recovery can safely retry.
      const launched = await this.options.launchInstaller(this.staged.filename,
        {requiresElevation: this.staged.expected.requiresElevation});
      if (launched !== true) throw new Error('The update installer could not be started.');
      if (this.state.status !== 'installing' || !this.reserved) return this.getState();
      this.installLaunchConfirmed = true;
      this.options.quitForUpdate();
    } catch (error) {await this._fail(error);}
    return this.getState();
  }

  async _fail(error) {
    this.installLaunchConfirmed = false;
    this.staged = null;
    this._set({status: 'error', error: publicError(error, this.state.feedUrl === GITHUB_UPDATE_SOURCE.url), installAllowed: false, progress: null});
    if (this.reserved) {
      this.reserved = false;
      try {await this.options.abortUpdateRestart();} catch {
        this._set({error: 'The update failed and the host could not resume. Reopen the launcher to retry its connection.'});
      }
    }
  }

  start() {
    if (this.started || this.stopped) return;
    this.started = true;
    if (this.state.feedUrl) this.checkForUpdates();
    this.checkTimer = setInterval(() => {if (this.state.feedUrl) this.checkForUpdates();}, 6 * 60 * 60 * 1000);
    this.statusTimer = setInterval(() => this.refreshInstallAllowed(), 1000);
    this.checkTimer.unref?.();
    this.statusTimer.unref?.();
  }

  stop() {
    this.stopped = true;
    clearInterval(this.checkTimer);
    clearInterval(this.statusTimer);
    this.updater.removeListener('download-progress', this.progressListener);
    this.updater.removeListener('error', this.errorListener);
  }
}

function trustedDesktopSender(event, window, launcherUrl) {
  try {
    if (!window || window.isDestroyed() || event.sender !== window.webContents ||
        !event.senderFrame || event.senderFrame !== window.webContents.mainFrame) return false;
    const actual = new URL(event.senderFrame.url), allowed = new URL(launcherUrl);
    return allowed.protocol === 'http:' && allowed.hostname === '127.0.0.1' &&
      actual.origin === allowed.origin && !actual.username && !actual.password;
  } catch {return false;}
}

function registerDesktopIpc({ipcMain, controller, getWindow, getLauncherUrl, getAppInfo}) {
  const handlers = {
    [IPC_CHANNELS.info]: () => getAppInfo(),
    [IPC_CHANNELS.state]: () => controller.refreshInstallAllowed(),
    [IPC_CHANNELS.configure]: value => controller.configureFeed(value),
    [IPC_CHANNELS.check]: () => controller.checkForUpdates(),
    [IPC_CHANNELS.install]: () => controller.restartAndInstall(),
  };
  for (const [channel, handler] of Object.entries(handlers)) ipcMain.handle(channel, (event, ...args) => {
    if (!trustedDesktopSender(event, getWindow(), getLauncherUrl())) throw new Error('Desktop requests require the launcher main frame.');
    if (args.length !== (channel === IPC_CHANNELS.configure ? 1 : 0)) throw new Error('Invalid desktop request arguments.');
    return handler(...args);
  });
  const publish = state => {
    const window = getWindow();
    if (!window || window.isDestroyed() || !trustedDesktopSender({sender: window.webContents,
      senderFrame: window.webContents.mainFrame}, window, getLauncherUrl())) return;
    window.webContents.send(UPDATE_CHANNEL, state);
  };
  controller.on('state', publish);
  return () => {
    controller.removeListener('state', publish);
    for (const channel of Object.keys(handlers)) ipcMain.removeHandler(channel);
  };
}

module.exports = {UpdaterController, validateFeed, validateUpdateInfo, verifyInstaller, launchNsisInstaller, isPermittedUpdateUrl,
  GITHUB_UPDATE_SOURCE, IPC_CHANNELS, UPDATE_CHANNEL, registerDesktopIpc, trustedDesktopSender};
