// Desktop shell for the reconstructed Minecraft Launcher.
//
// The web app is unmodified. This process supplies what launcher.dll supplies
// in the real product: a window, the local host server (appData + MCLauncher +
// content + franchise services), and a place to keep its state. Because the UI
// still talks to the same host contract, the same bundles run here without a
// build step.
//
//   npm start                      (checkout: host runs under system Python)
//   npm start -- --minecraft-dir="C:\\Users\\me\\AppData\\Roaming\\.minecraft"
//   npm run dist                   (packaged app: the host is bundled)
//
// A packaged build keeps two roots apart on purpose:
//   resources/root   read-only  the UI bundle, content mirror, bridge, assets
//   userData/data    writable    settings overlay, offline accounts, capes, log
// and points the host at the user's real .minecraft, like the launcher does.
const { app, BrowserWindow, Menu, Tray, nativeImage, dialog, shell, session, ipcMain } = require("electron");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const http = require("http");
const net = require("net");
const path = require("path");
const { LauncherLifecycle, createLifecycleHost } = require("./lifecycle");
const {UpdaterController, registerDesktopIpc, launchNsisInstaller, isPermittedUpdateUrl, GITHUB_UPDATE_SOURCE} = require("./updater");

const APP_ID = "app.mclauncher.desktop";
const APP_NAME = "Minecraft Launcher";

// Electron's native userData also selects its single-instance lock. Give only
// explicit smoke runs their own disposable directory before either is used.
function configureSmokeUserData() {
  if (!process.argv.includes("--smoke-test")) return;
  const arg = process.argv.find(value => value.startsWith("--smoke-user-data="));
  if (!arg) return;
  const directory = path.normalize(arg.slice("--smoke-user-data=".length));
  if (directory.includes("\0") || !path.isAbsolute(directory) ||
      path.parse(directory).root !== path.parse(path.resolve(directory)).root) {
    throw new Error("Smoke user data requires an absolute path.");
  }
  fs.mkdirSync(directory, {recursive: true});
  app.setPath("userData", directory);
}
configureSmokeUserData();

// Electron decides `app.isPackaged` from how it was started, and `npm start`
// launches the renamed binary (`Minecraft.exe`) so a checkout would otherwise
// look packaged. start.js sets LAUNCHER_DEV to say which one this is.
const isPackaged = app.isPackaged && !process.env.LAUNCHER_DEV;
const REPO_ROOT = path.resolve(__dirname, "..");
// The checkout layout doubles as the packaged resource layout, so the host
// (frozen or under Python) resolves the same relative paths in both.
const RESOURCE_ROOT = isPackaged ? path.join(process.resourcesPath, "root") : REPO_ROOT;
const HOST_EXE = path.join(process.resourcesPath, "host", "mc-launcher-host.exe");
const USER_DATA = app.getPath("userData");
// A checkout keeps the host's writable state in harness/ (the same overlay the
// browser harness and every screenshot use); the packaged app owns a data
// folder in userData because its resources are read-only.
const DATA_DIR = process.env.LAUNCHER_DATA_DIR || (isPackaged ? path.join(USER_DATA, "data") : path.join(REPO_ROOT, "harness"));
const WINDOW_STATE = path.join(USER_DATA, "window-state.json");
const SERVER_LOG = path.join(USER_DATA, "host-server.log");
// The launcher's game directory: the same `.minecraft` the official launcher
// and the game already use, unless the caller names another one.
const DEFAULT_STATE_DIR = path.join(app.getPath("appData"), ".minecraft");
const CAPTURED_STATE = path.join(
  RESOURCE_ROOT,
  "minecraft-launcher-re",
  "config",
  ".minecraft"
);
// Files a fresh game directory needs to boot: the profile, settings and the
// version manifest. They ship beside the app and are copied only when the
// user's own copy is missing. The captured account store is deliberately NOT
// here: it holds another machine's Microsoft tokens, so a release must never
// copy it into a user's .minecraft (accounts are added by the user, offline or
// through the Microsoft sign-in flow).
const STATE_SEED = [
  "launcher_profiles.json",
  "launcher_settings.json",
  "launcher_product_state_microsoft_store.json",
  "launcher_ui_state_microsoft_store.json",
  "treatment_tags.json",
  "versions/version_manifest_v2.json",
  "versions/jre_manifest.json",
];

let serverProcess = null;
let mainWindow = null;
let lifecycle = null;
let tray = null;
let launcherUrl = null;
let allowQuit = false;
let updaterController = null;
let disposeDesktopIpc = null;

// --- launcher state -------------------------------------------------------
// The reconstruction ships the captured launcher state. A real installation is
// used instead with --minecraft-dir (or LAUNCHER_STATE_DIR), which is what makes
// this a drop-in host for an existing .minecraft folder.
function launcherDirFromArgv() {
  const arg = process.argv.find((a) => a.startsWith("--minecraft-dir="));
  if (arg) return arg.slice("--minecraft-dir=".length).replace(/^"|"$/g, "");
  if (process.env.LAUNCHER_STATE_DIR) return process.env.LAUNCHER_STATE_DIR;
  return null;
}

function stateDir() {
  // A checkout reproduces the captured screenshots; the packaged app is a real
  // launcher and defaults to the machine's own Minecraft folder.
  return launcherDirFromArgv() || (isPackaged ? DEFAULT_STATE_DIR : CAPTURED_STATE);
}

function gameDir() {
  // The capture keeps its game files beside `.minecraft`, not inside it; a real
  // installation has the two pointing at the same folder.
  const explicit = launcherDirFromArgv();
  if (explicit) return explicit;
  return isPackaged ? DEFAULT_STATE_DIR : path.dirname(CAPTURED_STATE);
}

function seedStateDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
  const seedRoot = path.join(RESOURCE_ROOT, "default-state");
  for (const name of STATE_SEED) {
    const dest = path.join(dir, ...name.split("/"));
    if (fs.existsSync(dest)) continue;
    const src = path.join(seedRoot, ...name.split("/"));
    try {
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      fs.copyFileSync(src, dest);
    } catch (e) {
      /* the seed is optional: an empty state still renders */
    }
  }
}

function readLauncherSettings() {
  let settings = {};
  try {
    settings = JSON.parse(fs.readFileSync(path.join(stateDir(), "launcher_settings.json"), "utf8")) || {};
  } catch (e) { /* a new install has no settings yet */ }
  try {
    const overlay = JSON.parse(fs.readFileSync(path.join(DATA_DIR, "state.json"), "utf8"));
    if (overlay && overlay.__launcher_settings && typeof overlay.__launcher_settings === "object") {
      settings = Object.assign({}, settings, overlay.__launcher_settings);
    }
  } catch (e) { /* retain the native settings if the overlay is unavailable */ }
  return settings;
}

// --- python / bundled host ------------------------------------------------
function findPython() {
  const candidates = [];
  if (process.env.PYTHON) candidates.push({ cmd: process.env.PYTHON, args: [] });
  if (process.platform === "win32") {
    candidates.push({ cmd: "python", args: [] }, { cmd: "py", args: ["-3"] });
  } else {
    candidates.push({ cmd: "python3", args: [] }, { cmd: "python", args: [] });
  }
  for (const candidate of candidates) {
    try {
      const probe = spawnSync(candidate.cmd, candidate.args.concat(["--version"]), {
        timeout: 5000,
        windowsHide: true,
        encoding: "utf8",
      });
      const banner = String(probe.stdout || "") + String(probe.stderr || "");
      if (probe.status === 0 && /Python 3\./i.test(banner)) return candidate;
    } catch (e) {
      /* try the next one */
    }
  }
  return null;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.unref();
    probe.on("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const port = probe.address().port;
      probe.close(() => resolve(port));
    });
  });
}

function waitForServer(url, timeoutMs, child) {
  return new Promise((resolve, reject) => {
    let finished = false;
    let retryTimer, request;
    const finish = (error) => {
      if (finished) return;
      finished = true;
      clearTimeout(deadline);
      clearTimeout(retryTimer);
      if (child) {
        child.removeListener("error", onError);
        child.removeListener("exit", onExit);
      }
      if (request) request.destroy();
      if (error) reject(error);
      else resolve();
    };
    const onError = (error) => finish(error);
    const onExit = (code, signal) => finish(new Error("The launcher host exited before startup (" + (signal || code) + ")."));
    const deadline = setTimeout(() => finish(new Error("The launcher host did not start within " + timeoutMs / 1000 + "s.")), timeoutMs);
    if (child) {
      child.once("error", onError);
      child.once("exit", onExit);
      if (child.exitCode !== null || child.signalCode) {
        onExit(child.exitCode, child.signalCode);
        return;
      }
    }
    const retry = () => {
      if (!finished) retryTimer = setTimeout(attempt, 250);
    };
    const attempt = () => {
      if (finished) return;
      const req = request = http.get(url + "/", (res) => {
        res.resume();
        if (res.statusCode === 200) finish();
        else retry();
      });
      req.on("error", retry);
      req.setTimeout(3000, () => req.destroy());
    };
    attempt();
  });
}

function logTail(lines) {
  try {
    return fs.readFileSync(SERVER_LOG, "utf8").trim().split("\n").slice(-(lines || 20)).join("\n");
  } catch (e) {
    return "(no host log)";
  }
}

function hostCommand(port) {
  if (isPackaged && fs.existsSync(HOST_EXE)) {
    return { cmd: HOST_EXE, args: [String(port)], cwd: path.dirname(HOST_EXE) };
  }
  if (isPackaged) {
    throw new Error("The bundled launcher host is missing. Reinstall Minecraft Launcher.");
  }
  const python = findPython();
  if (!python) {
    throw new Error(
      "The launcher host is missing. Reinstall Minecraft Launcher" +
        (isPackaged ? "" : " or install Python 3 and run npm start again") +
        "."
    );
  }
  return {
    cmd: python.cmd,
    args: python.args.concat([path.join(RESOURCE_ROOT, "harness", "server.py"), String(port)]),
    // The cwd has to exist: spawn reports ENOENT for a missing directory, which
    // reads like a missing interpreter.
    cwd: fs.existsSync(path.join(RESOURCE_ROOT, "harness")) ? RESOURCE_ROOT : __dirname,
  };
}

async function startHost() {
  const port = await freePort();
  const baseUrl = "http://127.0.0.1:" + port;
  const state = stateDir();
  const game = gameDir();
  if (isPackaged) {
    try {
      seedStateDir(state);
    } catch (e) {
      /* keep going: the host answers with whatever state exists */
    }
  }

  fs.mkdirSync(DATA_DIR, { recursive: true });
  const host = hostCommand(port);
  const env = Object.assign({}, process.env, {
    PYTHONUNBUFFERED: "1",
    // The host reads its bundle from here and writes only inside DATA_DIR.
    LAUNCHER_ROOT: RESOURCE_ROOT,
    LAUNCHER_DATA_DIR: DATA_DIR,
    LAUNCHER_STATE_DIR: state,
    LAUNCHER_GAME_DIR: game,
    LAUNCHER_VERSION: app.getVersion(),
  });
  // A packaged build ships read-only sources and has nothing to hot-reload;
  // the checkout keeps the watcher so editing harness/*.py still works.
  if (isPackaged) env.LAUNCHER_NO_RELOAD = "1";

  const logStream = fs.createWriteStream(SERVER_LOG, { flags: "w" });
  console.log("[shell] host:", host.cmd, host.args.join(" "));
  serverProcess = spawn(host.cmd, host.args, {
    cwd: host.cwd,
    windowsHide: true,
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  const spawnedHost = serverProcess;
  let hostReady = false;
  // A spawn that never produced a process (missing exe, bad interpreter) has
  // to be reported here: the exit event never fires for it.
  serverProcess.on("error", (err) => {
    logStream.write("\n[shell] host spawn failed: " + ((err && err.message) || err) + "\n");
    console.error("[shell] host spawn failed:", err);
  });
  serverProcess.stdout.pipe(logStream);
  serverProcess.stderr.pipe(logStream);
  serverProcess.on("exit", (code, signal) => {
    // An exit during shutdown is expected; anything else means the window is
    // now showing a dead page, so say why instead of leaving it blank.
    if (serverProcess === spawnedHost && hostReady && !app.isQuitting) {
      dialog.showErrorBox(
        "Launcher host stopped",
        "The local host server exited " + (signal ? "with signal " + signal : "with code " + code) + ".\n\n" + logTail(12)
      );
    }
    if (typeof lifecycle !== "undefined" && lifecycle && serverProcess === spawnedHost) lifecycle.hostStopped();
  });

  await waitForServer(baseUrl, 30000, spawnedHost);
  hostReady = true;
  return baseUrl;
}

function stopHost() {
  if (!serverProcess || serverProcess.killed) return;
  const host = serverProcess;
  serverProcess = null;
  try {
    // Only this owned host is ours to terminate. Its Minecraft descendants
    // must survive an OS shutdown, hard shell exit, or updater restart.
    host.kill("SIGTERM");
  } catch (e) {
    /* already gone */
  }
}

// --- window ---------------------------------------------------------------
function readWindowState() {
  try {
    const saved = JSON.parse(fs.readFileSync(WINDOW_STATE, "utf8"));
    if (saved && saved.width > 800 && saved.height > 500) return saved;
  } catch (e) {
    /* first run */
  }
  return { width: 1280, height: 720 };
}

function saveWindowState() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  try {
    fs.mkdirSync(USER_DATA, { recursive: true });
    fs.writeFileSync(WINDOW_STATE, JSON.stringify(mainWindow.getNormalBounds()));
  } catch (e) {
    /* not fatal */
  }
}

function buildMenu() {
  // The real launcher has no menu bar: the UI sits flush against the title bar.
  // A null application menu removes Chromium's stock File/Edit/View/Window/Help
  // strip and the shortcuts it would install with it.
  Menu.setApplicationMenu(null);
}

function lockToLocalContent(ses, launcherUrl) {
  // Every byte this app draws is local (the content mirror answers the URLs
  // the UI would otherwise fetch). Anything off-machine is a request the host
  // did not rewrite; cancelling it keeps the launcher working offline instead
  // of hanging on a dead remote when the network drops.
  ses.webRequest.onBeforeRequest((details, callback) => {
    let url;
    try {
      url = new URL(details.url);
    } catch (e) {
      return callback({ cancel: true });
    }
    const localProtocol = ["data:", "blob:", "file:", "devtools:", "chrome:"];
    callback({ cancel: !(localProtocol.includes(url.protocol) || isLauncherUrl(details.url, launcherUrl)) });
  });
}

function openExternalUrl(target) {
  let url;
  try { url = new URL(target); } catch (e) { return; }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return;
  const failed = (error) => dialog.showErrorBox(
    "Minecraft Launcher could not open this link",
    "The system browser could not be opened.\n\n" + String(error && error.message || error)
  );
  try {
    Promise.resolve(shell.openExternal(url.href)).catch(failed);
  } catch (error) {
    failed(error);
  }
}

function isLauncherUrl(target, launcherUrl) {
  try {
    const url = new URL(target), launcher = new URL(launcherUrl);
    return ["http:", "https:"].includes(url.protocol) && url.origin === launcher.origin && !url.username && !url.password;
  } catch (e) {
    return false;
  }
}

function createWindow(url) {
  const state = readWindowState();
  mainWindow = new BrowserWindow({
    // Centred on the primary display instead of reusing the saved position: the
    // window keeps its size between runs, where it sits is the shell's choice.
    center: true,
    width: state.width,
    height: state.height,
    // The real Windows title bar: app icon, title, minimise / maximise / close,
    // plus native dragging, double-click maximise and Aero snap. The launcher's
    // own layout starts below it, the way the official launcher's does.
    titleBarStyle: "default",
    minWidth: 1024,
    minHeight: 640,
    show: false,
    backgroundColor: "#17191c",
    title: APP_NAME,
    icon: isPackaged
      ? path.join(process.resourcesPath, "icon.ico")
      : path.join(
          REPO_ROOT,
          "minecraft-launcher-re",
          "package",
          "SmallLogo.altform-unplated_targetsize-256.png"
        ),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
    },
  });

  // The UI opens documentation and store pages in a new tab; inside a desktop
  // shell that means the system browser.
  mainWindow.webContents.setWindowOpenHandler((details) => {
    openExternalUrl(details.url);
    return { action: "deny" };
  });
  const guardNavigation = (event, target) => {
    if (isLauncherUrl(target, url)) return;
    event.preventDefault();
    openExternalUrl(target);
  };
  mainWindow.webContents.on("will-navigate", guardNavigation);
  mainWindow.webContents.on("will-redirect", (event, target, isInPlace, isMainFrame) => {
    if (isMainFrame === false) {
      if (!isLauncherUrl(target, url)) event.preventDefault();
      return;
    }
    guardNavigation(event, target);
  });
  // A page that cannot load is not an empty launcher: it is a broken install.
  mainWindow.webContents.on("did-fail-load", (event, code, description, failedUrl, isMainFrame) => {
    if (!isMainFrame || code === -3) return; // -3: aborted, normal on reload
    dialog.showErrorBox(
      "Minecraft Launcher could not load",
      description + " (" + code + ")\n\n" + failedUrl + "\n\nHost log:\n" + logTail(12)
    );
  });

  mainWindow.once("ready-to-show", () => {
    if (!lifecycle || !lifecycle.hidden) mainWindow.show();
  });
  mainWindow.on("close", (event) => {
    saveWindowState();
    if (!app.isQuitting && lifecycle && !lifecycle.stopped) {
      event.preventDefault();
      lifecycle.closeWindow();
      lifecycle.check();
    }
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  buildMenu();
  mainWindow.loadURL(url);
}

// --- lifecycle ------------------------------------------------------------
function showLauncherWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    if (launcherUrl && !app.isQuitting) createWindow(launcherUrl);
    return;
  }
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function reopenLauncher() {
  if (!lifecycle || lifecycle.stopped) return showLauncherWindow();
  return lifecycle.reopen().catch((error) => dialog.showErrorBox(
    "Minecraft Launcher could not resume",
    String(error && error.message || error) + "\n\nThe launcher host could not confirm its session. Try reopening again."
  ));
}

function quitLauncher() {
  allowQuit = true;
  if (lifecycle) lifecycle.stop();
  app.quit();
}

function requestLauncherQuit() {
  if (!lifecycle || lifecycle.stopped || process.argv.includes("--smoke-test")) return quitLauncher();
  saveWindowState();
  lifecycle.closeWindow();
  lifecycle.check();
}

function updateTray(status = lifecycle && lifecycle.status) {
  if (!tray) return;
  const busy = !status || status.gameRunning || status.activeWork;
  const state = !status ? "Checking game and download status…" : status.gameRunning ? "Minecraft is running" :
    status.activeWork ? "Download or installation work is active" : "Launcher is ready";
  tray.setToolTip(APP_NAME + " — " + state);
  tray.setContextMenu(Menu.buildFromTemplate([
    {label: "Open Minecraft Launcher", click: reopenLauncher},
    {label: state, enabled: false},
    {type: "separator"},
    {label: busy ? "Close launcher when idle" : "Quit launcher", click: requestLauncherQuit},
  ]));
}

function createTray() {
  if (tray) return;
  let icon = nativeImage.createFromPath(isPackaged ? path.join(process.resourcesPath, "icon.ico") : path.join(__dirname, "build", "icon.ico"));
  if (icon.isEmpty()) icon = nativeImage.createFromPath(path.join(RESOURCE_ROOT, "minecraft-launcher-re", "package", "SmallLogo.altform-unplated_targetsize-256.png"));
  tray = new Tray(icon);
  tray.on("click", reopenLauncher);
  tray.on("double-click", reopenLauncher);
  updateTray();
}

function beginLifecycle(url) {
  launcherUrl = url;
  lifecycle = new LauncherLifecycle(Object.assign(createLifecycleHost(url), {
    hideWindow: () => {if (mainWindow && !mainWindow.isDestroyed()) mainWindow.hide();},
    showWindow: showLauncherWindow,
    quit: quitLauncher,
    statusChanged: updateTray,
  }));
  createTray();
}

// Updater integration: readiness drives its button; installation must acquire
// fresh host admission with prepareUpdateRestart before spawning the installer.
async function readLifecycleStatus() {
  if (!lifecycle) throw new Error("The launcher host has not started.");
  return lifecycle.options.readStatus();
}

async function isUpdateInstallSafe() {
  return lifecycle ? lifecycle.isUpdateInstallSafe() : false;
}

async function prepareUpdateRestart() {
  return lifecycle ? lifecycle.prepareUpdateRestart() : false;
}

async function abortUpdateRestart() {
  if (lifecycle) await lifecycle.abortUpdateRestart();
}

function beginUpdater() {
  const {NsisUpdater} = require("electron-updater");
  const updater = new NsisUpdater();
  updaterController = new UpdaterController({updater, userData: USER_DATA, defaultFeedUrl: GITHUB_UPDATE_SOURCE.url,
    currentVersion: app.getVersion(), isPackaged, isUpdateInstallSafe, prepareUpdateRestart, abortUpdateRestart,
    launchInstaller: (installer, options) => launchNsisInstaller(installer, Object.assign({}, options,
      {resourcesPath: process.resourcesPath})), quitForUpdate});
  // electron-updater uses its own session. Scope generic sources to their
  // directory and GitHub requests to this repository and its release CDN.
  session.fromPartition("electron-updater", {cache: false}).webRequest.onBeforeRequest((details, callback) => {
    callback({cancel: !isPermittedUpdateUrl(details.url, updaterController.getState().feedUrl)});
  });
  disposeDesktopIpc = registerDesktopIpc({ipcMain, controller: updaterController,
    getWindow: () => mainWindow, getLauncherUrl: () => launcherUrl,
    getAppInfo: () => ({version: app.getVersion(), packaged: isPackaged})});
  if (!process.argv.includes("--smoke-test")) updaterController.start();
}

function quitForUpdate() {
  if (updaterController && updaterController.installLaunchConfirmed && updaterController.reserved &&
      updaterController.getState().status === "installing" &&
      lifecycle && lifecycle.updating) {
    saveWindowState();
    allowQuit = true;
    app.quit();
    return true;
  }
  return false;
}

app.setAppUserModelId(APP_ID);
app.setName(APP_NAME);
// Electron requires this before ready; the Settings checkbox takes effect
// when the desktop launcher is restarted, as its label promises.
if (readLauncherSettings().disableGPU === true) app.disableHardwareAcceleration();

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", reopenLauncher);
  app.on("activate", reopenLauncher);

  app.whenReady().then(async () => {
    try {
      session.defaultSession.setPermissionRequestHandler((wc, permission, callback) => {
        callback(false);
      });
      const url = await startHost();
      lockToLocalContent(session.defaultSession, url);
      beginLifecycle(url);
      beginUpdater();
      createWindow(url);
      lifecycle.start();
      // Headless smoke test for CI and for verifying the packaged shell without
      // a human watching the window.
      if (process.argv.includes("--smoke-test")) {
        // The page may already have finished loading by the time this runs, so
        // wait on the current state instead of on a future event.
        if (mainWindow.webContents.isLoading()) {
          await new Promise((resolve) => {
            const done = () => resolve();
            mainWindow.webContents.once("did-finish-load", done);
            mainWindow.webContents.once("did-fail-load", done);
            setTimeout(done, 20000);
          });
        }
        // The hero art and the news rail are large images: on a cold install
        // (first run after unpacking) they are still decoding at four seconds.
        await new Promise((resolve) => setTimeout(resolve, 8000));
        const smokeState = await mainWindow.webContents.executeJavaScript(`(() => ({
          text: document.body ? document.body.innerText.trim() : "",
          ready: !!window.MCLauncher && !!window.__APPDATA__,
          error: /Whoops!|An unexpected issue occurred|PAGE NOT FOUND/.test(document.body ? document.body.innerText : "")
        }))()`);
        if (!smokeState.ready || smokeState.text.length < 30 || smokeState.error) {
          throw new Error("Smoke test failed: launcher did not render a usable page.");
        }
        const image = await mainWindow.webContents.capturePage();
        const shot = path.join(USER_DATA, "smoke.png");
        fs.mkdirSync(path.dirname(shot), { recursive: true });
        fs.writeFileSync(shot, image.toPNG());
        app.quit();
        return;
      }
    } catch (e) {
      if (process.argv.includes("--smoke-test")) {
        console.error("[shell] smoke test failed:", (e && e.message) || e);
        stopHost();
        app.exit(1);
        return;
      }
      dialog.showErrorBox(
        "Minecraft Launcher could not start",
        String((e && e.message) || e) + "\n\nHost log:\n" + logTail(12)
      );
      app.quit();
    }
  });

  app.on("window-all-closed", requestLauncherQuit);
  app.on("before-quit", (event) => {
    if (!allowQuit && lifecycle && !lifecycle.stopped && !process.argv.includes("--smoke-test")) {
      event.preventDefault();
      requestLauncherQuit();
      return;
    }
    app.isQuitting = true;
    if (updaterController) updaterController.stop();
    if (disposeDesktopIpc) {disposeDesktopIpc(); disposeDesktopIpc = null;}
    if (lifecycle) lifecycle.stop();
    if (tray) {tray.destroy(); tray = null;}
    stopHost();
  });
  app.on("will-quit", stopHost);
  // A hard kill of the shell must not orphan the host.
  process.on("exit", stopHost);
}
