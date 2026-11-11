// Full interaction audit for the reconstructed launcher, driven by the same
// Chromium the desktop wrapper uses (Electron), not the preview WebView.
//
//   cd app && npx electron ../harness/audit.js
//   cd app && AUDIT_ROUTES="/settings/accounts" npx electron ../harness/audit.js
//
// Two things are checked, because a page can render perfectly and still be
// unusable:
//
//   1. Every route loads, paints its own content and raises no console error.
//   2. Every control on it can actually be clicked. "Clickable" is tested the
//      way a person tests it -- a real mouse press at the centre of the target
//      -- and then three failure modes are looked for by name:
//
//        covered   something else (an overlay, a scrim, a stray panel) sits on
//                  top of the control at that point
//        drag      an ancestor is a window drag region, so the press moves the
//                  window instead of reaching the app
//        dead      the press lands but nothing at all happens: no view change,
//                  no dialog, no state change
//
// Navigation links are pressed too and confirmed against the app's own
// `aria-selected`/`aria-current` state rather than the URL -- the launcher's URL
// is cosmetic inside CEF, because its router is store-driven and never touches
// history. Dialogs that a press opens get their own controls pressed as well,
// one level deep.
//
// Controls are re-collected before every press: React re-creates nodes on each
// render, so an element reference or attribute from an earlier pass is stale by
// the time the next press runs.
//
// Screenshots of every route land in harness/shots/ and the full result is
// written to harness/audit-report.json.
const { app, BrowserWindow } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const http = require("http");

const HARNESS = __dirname;
const ROOT = path.dirname(HARNESS);
const SHOTS = path.join(HARNESS, "shots");
const PORT = 8791;
const BASE = `http://127.0.0.1:${PORT}`;

// AUDIT_ROUTES="/settings/accounts,/games/java/skins" narrows the sweep while
// working on one page.
const ROUTES = process.env.AUDIT_ROUTES
  ? process.env.AUDIT_ROUTES.split(",").map((r) => r.trim()).filter(Boolean)
  : [
      "/",
      "/home/general",
      "/games/java/play",
      "/games/java/configurations",
      "/games/java/realms",
      "/games/java/skins",
      "/games/java/patch_notes",
      "/games/bedrock/play",
      "/games/bedrock/realms",
      "/games/bedrock/creator_tools",
      "/games/bedrock/marketplace_pass",
      "/games/bedrock/faq",
      "/games/bedrock/patch_notes",
      "/settings/general",
      "/settings/accounts",
      "/settings/about",
    ];

const CRASH = /Whoops!|An unexpected issue occurred/i;

// Controls whose press has an effect outside the page: they start a real
// download or a game launch, hand a URL to the system browser, open a native
// file picker or delete captured data. They are listed as skipped so the report
// says what was not pressed instead of quietly counting it as a pass.
const SKIP = {
  label: /^(PLAY|LAUNCH|INSTALL|UPDATE|BUY|PURCHASE|GET MINECRAFT|PLAY DEMO|BUY AS GIFT|SIGN IN WITH MICROSOFT|SIGN OUT|SIGN IN|ADD ACCOUNT|DELETE|REMOVE|UNINSTALL|RESET|RESTORE|REPORT A LAUNCHER BUG)\b/i,
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function waitForServer(timeoutMs = 20000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const tick = () => {
      http
        .get(BASE + "/", (res) => {
          res.resume();
          resolve();
        })
        .on("error", () => {
          if (Date.now() - started > timeoutMs) reject(new Error("server did not start"));
          else setTimeout(tick, 250);
        });
    };
    tick();
  });
}

function startServer() {
  const child = spawn(
    process.platform === "win32" ? "python" : "python3",
    [path.join(HARNESS, "server.py"), String(PORT)],
    { cwd: ROOT, stdio: ["ignore", "pipe", "pipe"], windowsHide: true }
  );
  child.stdout.on("data", (d) => {
    const text = String(d);
    if (/serving|Error|Traceback/.test(text)) process.stdout.write("[server] " + text);
  });
  child.stderr.on("data", (d) => process.stderr.write("[server] " + d));
  return child;
}

// --- in-page probes (stringified and evaluated in the renderer) -------------

// Collects the controls in `scope`, tags each with a unique attribute so one can
// be addressed for a hit test, and reports what a press would do.
function collectControls(scope) {
  const SEL = [
    "button",
    "a[href]",
    '[role="button"]',
    '[role="tab"]',
    '[role="switch"]',
    '[role="checkbox"]',
    '[role="radio"]',
    '[role="menuitem"]',
    "input:not([type=hidden])",
    "select",
    "textarea",
  ].join(",");
  const root = scope ? document.querySelector(scope) : document;
  if (!root) return [];
  // Ids must be unique for the whole document, not per collection: a panel's
  // controls and the page's controls are both live at the same time, and a
  // repeated id would make the hit test measure the element behind the panel.
  const round = (window.__auditRound = (window.__auditRound || 0) + 1);
  const out = [];
  root.querySelectorAll(SEL).forEach((el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 6 || r.height < 6) return;
    if (r.bottom < 0 || r.right < 0 || r.top > innerHeight || r.left > innerWidth) return;
    const cs = getComputedStyle(el);
    if (cs.visibility === "hidden" || cs.display === "none" || Number(cs.opacity) < 0.05) return;
    if (el.closest('[aria-hidden="true"]')) return;
    const label = (el.getAttribute("aria-label") || el.innerText || el.value || el.getAttribute("title") || "")
      .replace(/\s+/g, " ")
      .trim();
    if (!label && el.tagName !== "INPUT" && el.tagName !== "SELECT") return;
    const id = "audit-" + round + "-" + out.length;
    el.setAttribute("data-audit-id", id);
    out.push({
      id,
      tag: el.tagName,
      domId: el.id || "",
      label: label.slice(0, 70),
      href: el.getAttribute("href"),
      disabled:
        el.disabled === true ||
        el.getAttribute("aria-disabled") === "true" ||
        el.classList.contains("disabled"),
      // The shell makes the tab it is already showing inert (`pointer-events:
      // none`) so pressing it cannot reload the page it is on. That is a real
      // "not clickable by design", not something on top of the control.
      inert: cs.pointerEvents === "none",
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
    });
  });
  return out;
}

// What sits at the control's centre, and is any ancestor a drag region?
function hitTest(id) {
  const el = document.querySelector('[data-audit-id="' + id + '"]');
  if (!el) return { gone: true };
  let r = el.getBoundingClientRect();
  const cx = r.left + r.width / 2;
  const cy = r.top + r.height / 2;
  // A card can be half in view: its top edge is on screen but its centre is
  // below the fold, where nothing can be pressed. Bring it into view first.
  if (cy < 0 || cy > innerHeight || cx < 0 || cx > innerWidth) {
    el.scrollIntoView({ block: "center", inline: "center" });
    r = el.getBoundingClientRect();
    return { retry: true };
  }
  const x = Math.round(r.left + r.width / 2);
  const y = Math.round(r.top + r.height / 2);
  const top = document.elementFromPoint(x, y);
  let drag = null;
  for (let n = top; n; n = n.parentElement) {
    if (getComputedStyle(n).webkitAppRegion === "drag") {
      drag = n.tagName.toLowerCase() + (n.id ? "#" + n.id : "");
      break;
    }
  }
  return {
    x,
    y,
    rect: Math.round(r.left) + "," + Math.round(r.top) + " " + Math.round(r.width) + "x" + Math.round(r.height),
    hit: Boolean(top) && (top === el || el.contains(top)),
    top: top ? top.tagName.toLowerCase() + (top.id ? "#" + top.id : "") : null,
    // The top few elements at that point, for when the hit test fails: it says
    // exactly what is in the way and what the target's own pointer-events are.
    stack: document
      .elementsFromPoint(x, y)
      .slice(0, 4)
      .map((n) => n.tagName.toLowerCase() + (n.id ? "#" + n.id : "") + "[" + getComputedStyle(n).pointerEvents + "]"),
    selfPointer: getComputedStyle(el).pointerEvents,
    topText: top ? (top.innerText || "").replace(/\s+/g, " ").trim().slice(0, 40) : "",
    drag,
  };
}

// A fingerprint of what the page shows and which controls claim to be active.
// This is how "did that press do anything at all?" is answered.
function viewState() {
  const text = document.body.innerText || "";
  let hash = 2166136261;
  for (let i = 0; i < text.length; i += 3) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  const active = [...document.querySelectorAll('[aria-selected="true"],[aria-checked],[aria-expanded],[aria-current="page"]')]
    .map((el) => {
      const key = el.getAttribute("href") || (el.innerText || "").replace(/\s+/g, " ").trim().slice(0, 24);
      return key + ":" + el.getAttribute("aria-selected") + el.getAttribute("aria-checked") + el.getAttribute("aria-expanded");
    })
    .join("|");
  return {
    path: location.pathname,
    len: text.length,
    hash: hash >>> 0,
    active: active.slice(0, 600),
    // Every panel the shell can stack over a page: its own dialogs live in
    // #modal-dialog (no role attribute), the harness screens in #__hx-screen.
    // While one is up the page underneath is unclickable, so the count is part
    // of "is this still the page as it loaded?".
    panels: document.querySelectorAll("#modal-dialog,#__hx-screen,[role=dialog],[role=menu],[role=listbox]").length,
  };
}

// Scope used to press the controls of whatever panel a press opened. Menus and
// listboxes are deliberately excluded: they are transient popups whose entries
// are usually the same navigation the page already offers, and pressing them
// leaves state behind (opening a drawer, switching accounts) that has nothing to
// do with the page under test.
const PANELS = "#modal-dialog,#__hx-screen,[role=dialog]";

// Closes whatever panel is stacked over the page, through its own control, so
// the next press starts from the page again.
function closePanels() {
  const SEL = "#modal-dialog,#__hx-screen,[role=dialog]";
  const panels = [...document.querySelectorAll(SEL)].filter((p) => {
    const r = p.getBoundingClientRect();
    const cs = getComputedStyle(p);
    return r.width > 2 && r.height > 2 && cs.visibility !== "hidden" && cs.display !== "none";
  });
  let closed = 0;
  for (const panel of panels) {
    const candidates = [
      ...panel.querySelectorAll('[aria-label="Close"],[aria-label="close"],.__hx-close'),
      ...[...panel.querySelectorAll("button,a")].filter((b) =>
        /^(close|cancel|×|✕)$/i.test((b.innerText || b.getAttribute("aria-label") || "").trim()) || b.id === "closeDialog"
      ),
    ];
    const hit = candidates.find((b) => b.getBoundingClientRect().width > 2);
    if (hit) {
      hit.click();
      closed++;
    }
  }
  if (!closed && panels.length) {
    // The patch-notes drawer keeps its Close control in the header, outside the
    // scrolling body; fall back to the one visible close control on the page.
    const anywhere = [...document.querySelectorAll('[aria-label="Close"],[aria-label="close"]')].find(
      (b) => b.getBoundingClientRect().width > 2
    );
    if (anywhere) {
      anywhere.click();
      closed++;
    }
  }
  return closed;
}

const stringify = (fn, arg) => `(${fn.toString()})(${arg === undefined ? "" : JSON.stringify(arg)})`;
const sameState = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const keyOf = (c) => c.tag + "|" + c.label + "|" + (c.href || "");

const read = (win, fn, arg) => win.webContents.executeJavaScript(stringify(fn, arg));
const readState = (win) => read(win, viewState);

// Keeps a picture of the page at the moment a press failed, so a report entry
// can be checked against what was actually on screen.
let shotSeq = 0;
async function snapFailure(win, route, control) {
  const slug = (route.replace(/[^a-z0-9]+/gi, "_") + "-fail-" + control.replace(/[^a-z0-9]+/gi, "_")).slice(0, 90);
  try {
    fs.writeFileSync(path.join(SHOTS, slug + ".png"), (await win.webContents.capturePage()).toPNG());
    shotSeq++;
    return slug;
  } catch (e) {
    return "shot failed: " + String(e && e.message);
  }
}

async function press(win, x, y) {
  // A page that reloads while a press is in flight can swallow the mouseup, and
  // every later press is then treated as a drag by Chromium and never becomes a
  // click. Starting each press with a release at a neutral point keeps the
  // button state honest.
  win.webContents.sendInputEvent({ type: "mouseUp", x: 1, y: 1, button: "left", clickCount: 1 });
  win.webContents.sendInputEvent({ type: "mouseMove", x, y });
  await sleep(40);
  win.webContents.sendInputEvent({ type: "mouseDown", x, y, button: "left", clickCount: 1 });
  await sleep(50);
  win.webContents.sendInputEvent({ type: "mouseUp", x, y, button: "left", clickCount: 1 });
}

async function escape(win) {
  win.webContents.sendInputEvent({ type: "keyDown", keyCode: "Escape" });
  win.webContents.sendInputEvent({ type: "keyUp", keyCode: "Escape" });
  await sleep(250);
}

async function crashed(win) {
  return win.webContents.executeJavaScript(
    `new RegExp(${JSON.stringify(CRASH.source)}).test(document.body.innerText || "")`
  );
}

async function waitForShell(win) {
  for (let i = 0; i < 40; i++) {
    const ready = await win.webContents.executeJavaScript(
      `Boolean(document.querySelector('[id$="-menulink-wrap"]')) && document.body.innerText.trim().length > 80`
    );
    if (ready) return true;
    await sleep(250);
  }
  return false;
}

// The shell opens a patch-notes panel over the app when it has notes the user
// has not read. It is a real overlay with its own Close control, and while it
// is up the sidebar genuinely cannot be clicked -- so a sweep has to dismiss it
// instead of reporting every link behind it as covered.
async function dismissOverlays(win) {
  for (let i = 0; i < 3; i++) {
    // Escape is the shell's own way out of most panels; the rest have a Close
    // control, which is looked up in the panel and then one level out (the
    // drawer's close button sits in its header, outside the scroll area).
    await escape(win);
    const closed = await read(win, closePanels);
    if (!closed) {
      const any = await win.webContents.executeJavaScript(
        `Boolean(document.querySelector(${JSON.stringify(PANELS)}))`
      );
      if (!any) return true;
    }
    await sleep(500);
  }
  return false;
}

// Synthetic input is only delivered to a page loaded after the window that
// shows it was created. Reloading in place leaves the renderer refusing every
// press afterwards, which is exactly what made whole pages read as dead -- so
// each load builds a new window and tears the previous one down. Everything
// else in this file holds `win`, which forwards to whichever window is live.
let page = null;
// Console errors of the page being inspected. Each load builds a new window,
// so the listener is attached at load time rather than beforehand.
let consoleErrors = null;
const win = {
  get webContents() {
    return page.webContents;
  },
  loadURL: (url) => page.loadURL(url),
  setAlwaysOnTop: (on) => page.setAlwaysOnTop(on),
  moveTop: () => page.moveTop(),
  focus: () => page.focus(),
  destroy: () => {
    if (page && !page.isDestroyed()) page.destroy();
    page = null;
  },
};

async function loadRoute(route) {
  const old = page;
  page = new BrowserWindow({
    width: 1280,
    height: 720,
    show: true,
    titleBarStyle: "hidden",
    webPreferences: { backgroundThrottling: false },
  });
  // Same install path the app uses, so the drag handle (and anything else the
  // frame module injects) is part of what is tested.
  require(path.join(ROOT, "app", "frame.js")).install(page);
  if (consoleErrors) {
    page.webContents.on("console-message", (event) => {
      if (event.level === "error" || event.level === 3) consoleErrors.push(event.message);
    });
  }
  if (old && !old.isDestroyed()) old.destroy();
  await page.loadURL(BASE + route);
  await waitForShell(page);
  await dismissOverlays(page);
  await sleep(600);
  // A window that is behind another one does not receive synthetic input at
  // all -- Chromium drops it for the occluded page. Raising the window is what
  // makes a press land, so the sweep keeps it on top while it runs.
  page.setAlwaysOnTop(true);
  page.moveTop();
  page.focus();
  page.webContents.focus();
}

async function inspect(win, route) {
  consoleErrors = [];
  await loadRoute(route);
  await sleep(600);

  const state = await win.webContents.executeJavaScript(`(${function () {
    const text = (document.body.innerText || "").replace(/\s+/g, " ").trim();
    return {
      text: text.slice(0, 2000),
      chars: text.length,
      dialogs: [...document.querySelectorAll("[role=dialog]")]
        .map((d) => (d.innerText || "").replace(/\s+/g, " ").trim().slice(0, 200)),
      tabs: [...document.querySelectorAll("[role=tab][aria-selected=true]")]
        .map((el) => (el.title || el.innerText || "").replace(/\s+/g, " ").trim()),
      errors: (window.__errors || []).map((e) => String(e.text).replace(/\s+/g, " ").slice(0, 400)),
    };
  }.toString()})()`);

  const slug = route.replace(/[^a-z0-9]+/gi, "_").replace(/^_|_$/g, "") || "root";
  const shot = path.join(SHOTS, slug + ".png");
  try {
    fs.writeFileSync(shot, (await win.webContents.capturePage()).toPNG());
  } catch (e) {
    /* a stalled frame is reported below, not fatal */
  }
  const collected = consoleErrors;
  consoleErrors = null;
  return {
    route,
    chars: state.chars,
    tabs: state.tabs,
    dialogs: state.dialogs,
    crash: CRASH.test(state.text),
    snippet: state.text.slice(0, 240),
    consoleErrors: collected.slice(0, 6),
    pageErrors: state.errors.slice(0, 6),
    shot,
  };
}

// Presses every control in `scope` matching `pick`, one at a time, re-collecting
// before each press so a re-rendered page cannot produce a stale target.
async function pressControls(win, scope, pick, results, { route, region, navigates, depth = 0 }) {
  const base = await read(win, collectControls, scope);
  const wanted = base.filter(pick);
  // Every press starts from the page as it first loaded. A press that opens a
  // menu or flips a switch leaves state behind that changes what the next press
  // does -- a dropdown left open makes the sidebar ignore clicks, which reads
  // as a dead control. So the page is reloaded whenever a press changed it.
  const baseline = await readState(win);
  const settle = async () => {
    await escape(win);
    if (navigates) {
      await loadRoute(route);
      return;
    }
    if (sameState(await readState(win), baseline)) return;
    // A panel the press opened is closed with its own control first; anything
    // else that changed (a switch, a tab, a theme) only comes back on reload.
    await read(win, closePanels);
    await sleep(300);
    if (!sameState(await readState(win), baseline)) await loadRoute(route);
  };
  for (const want of wanted) {
    const key = keyOf(want);
    // An off-origin link is handled by the shell (it opens the system browser),
    // so pressing it here would leave the app instead of exercising it.
    if (want.href && /^https?:/i.test(want.href)) {
      results.push({ route, region, control: want.label + " -> " + want.href, tag: want.tag, skipped: "opens outside the app" });
      continue;
    }
    if (SKIP.label.test(want.label) || (SKIP.id && SKIP.id.test(want.domId || ""))) {
      results.push({ route, region, control: want.label, tag: want.tag, skipped: "side effects" });
      continue;
    }
    if (want.disabled || want.inert) {
      results.push({ route, region, control: want.label, tag: want.tag, skipped: want.inert ? "inert (current page)" : "disabled" });
      continue;
    }
    const list = await read(win, collectControls, scope);
    const occurrence = wanted.filter((c) => keyOf(c) === key).indexOf(want);
    const target = list.filter((c) => keyOf(c) === key)[occurrence];
    const label = want.label + (want.href ? " -> " + want.href : "");
    if (!target) {
      results.push({ route, region, control: label, tag: want.tag, gone: true });
      continue;
    }
    let probe = await read(win, hitTest, target.id);
    if (probe.retry) {
      await sleep(250);
      probe = await read(win, hitTest, target.id);
    }
    if (probe.gone) {
      results.push({ route, region, control: label, tag: want.tag, gone: true });
      continue;
    }
    if (probe.drag) {
      results.push({
        route,
        region,
        control: label,
        tag: want.tag,
        failed: "drag",
        detail: probe.drag,
        shot: await snapFailure(win, route, label),
      });
      continue;
    }
    if (!probe.hit) {
      results.push({
        route,
        region,
        control: label,
        tag: want.tag,
        failed: "covered",
        shot: await snapFailure(win, route, label),
        detail:
          probe.top + " over it" + (probe.topText ? " (" + probe.topText + ")" : "") + " at " + probe.x + "," + probe.y + " target " + probe.rect + " stack " + probe.stack.join(" < ") + " self " + probe.selfPointer,
      });
      continue;
    }

    const before = await readState(win);
    await press(win, probe.x, probe.y);
    await sleep(navigates ? 1100 : 700);
    const after = await readState(win);

    if (await crashed(win)) {
      results.push({
        route,
        region,
        control: label,
        tag: want.tag,
        failed: "crash",
        shot: await snapFailure(win, route, label),
      });
      await settle();
      continue;
    }
    if (sameState(before, after)) {
      // Not a failure by itself: some controls in this shell are deliberately
      // inert (an account row only takes focus, a tab that is already open).
      // It is reported as a warning so the list can be read, while covered,
      // drag and crash -- the three ways a control stops working -- fail.
      results.push({ route, region, control: label, tag: want.tag, warn: "no visible change", detail: before.path + " unchanged" });
      await settle();
      continue;
    }
    results.push({
      route,
      region,
      control: label,
      tag: want.tag,
      ok: true,
      openedPanel: after.panels > before.panels,
      changedView: after.hash !== before.hash || after.active !== before.active,
      landed: after.path,
    });
    // One level into a dialog this press opened: a dialog whose own buttons are
    // dead is the same bug, just hidden behind a click.
    if (!navigates && after.panels > before.panels && depth === 0) {
      await pressControls(win, PANELS, () => true, results, {
        route,
        region: "dialog",
        navigates: false,
        depth: 1,
      });
    }
    await settle();
  }
}

app.commandLine.appendSwitch("disable-gpu");
app.disableHardwareAcceleration();
// A window that Windows reports as occluded gets its renderer backgrounded, and
// a backgrounded page drops synthetic mouse input -- presses then do nothing at
// all, which reads as a page full of dead controls. These keep the renderer
// awake no matter what is in front of the window.
app.commandLine.appendSwitch("disable-backgrounding-occluded-windows");
app.commandLine.appendSwitch("disable-renderer-backgrounding");
app.commandLine.appendSwitch("disable-background-timer-throttling");

app.whenReady().then(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const server = startServer();
  const report = { startedAt: new Date().toISOString(), routes: [], interactions: [] };
  try {
    await waitForServer();

    for (const route of ROUTES) {
      process.stdout.write(`\n=== ${route}\n`);
      const row = await inspect(win, route);
      report.routes.push(row);
      process.stdout.write(
        `  chars=${row.chars} tabs=${JSON.stringify(row.tabs)} dialogs=${row.dialogs.length} crash=${row.crash}\n`
      );
      if (row.consoleErrors.length) console.log("  console:", row.consoleErrors[0].slice(0, 300));
      if (row.chars <= 80 || row.crash) continue;

      // Page controls first (they do not leave the page), then the links, which
      // need the page put back between presses. The reload in between gives the
      // link pass a clean page: a drawer or menu left open by a page control
      // covers the sidebar, and every link behind it would read as covered.
      await pressControls(win, null, (c) => !(c.href && c.href.startsWith("/")), report.interactions, {
        route,
        region: "content",
        navigates: false,
      });
      await loadRoute(route);
      await pressControls(win, null, (c) => Boolean(c.href && c.href.startsWith("/")), report.interactions, {
        route,
        region: "navigation",
        navigates: true,
      });
      await loadRoute(route);

      const here = report.interactions.filter((h) => h.route === route);
      const bad = here.filter((h) => h.failed);
      console.log(
        `  controls=${here.length} pressed=${here.filter((h) => h.ok).length}` +
          (bad.length ? `  FAILURES=${bad.length}` : "")
      );
      for (const failure of bad) console.log(`   ! ${failure.failed}: ${failure.control}`);
    }
    win.destroy();
  } catch (e) {
    report.fatal = String(e && (e.stack || e.message));
    console.error(e);
  } finally {
    fs.writeFileSync(path.join(HARNESS, "audit-report.json"), JSON.stringify(report, null, 2));
    server.kill();
  }
  const badRoutes = report.routes.filter((r) => r.crash || r.chars < 80 || r.consoleErrors.length);
  const failed = report.interactions.filter((h) => h.failed);
  const count = (kind) => failed.filter((h) => h.failed === kind).length;
  const skipped = report.interactions.filter((h) => h.skipped);
  const gone = report.interactions.filter((h) => h.gone);
  const warned = report.interactions.filter((h) => h.warn);
  console.log(
    `\n${report.routes.length} routes, ${badRoutes.length} with problems; ` +
      `${report.interactions.filter((h) => h.ok).length} presses had an effect, ` +
      `${failed.length} failed (${count("covered")} covered, ${count("drag")} drag, ` +
      `${count("crash")} crashes), ${warned.length} with no visible change, ` +
      `${skipped.length} skipped, ${gone.length} disappeared mid-pass`
  );
  if (failed.length) {
    console.log("failed:");
    for (const f of failed) console.log(`  ${f.route}  ${f.failed}  ${f.control}${f.detail ? "  (" + f.detail + ")" : ""}`);
  }
  if (warned.length) {
    console.log("no visible change:");
    for (const w of warned) console.log(`  ${w.route}  ${w.control}`);
  }
  app.exit(badRoutes.length || failed.length ? 1 : 0);
});
