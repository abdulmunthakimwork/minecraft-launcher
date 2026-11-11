// Drive the launcher's real interaction flows (dialogs, menus, detail views)
// and report which ones end in the React error boundary. Run with Electron:
//   cd app && npx electron ../harness/flows.js
const { app, BrowserWindow } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const http = require("http");

const PORT = 8793;
const BASE = `http://127.0.0.1:${PORT}`;
const CRASH = /Whoops!|An unexpected issue occurred|PAGE NOT FOUND|Content not found/i;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Each step clicks the first visible control whose text/aria-label contains the
// string. A step without text waits only.
const FLOWS = [
  { name: "java: install dialog", route: "/games/java/play", steps: [{ click: "INSTALL" }] },
  { name: "java: buy as gift", route: "/games/java/play", steps: [{ click: "Buy as Gift" }] },
  { name: "java: play demo", route: "/games/java/play", steps: [{ click: "Play Demo" }] },
  { name: "java: profile name copy", route: "/games/java/play", steps: [{ click: "Profile Name" }] },
  { name: "patch notes: first card", route: "/games/java/patch_notes", steps: [{ click: "Minecraft" }] },
  { name: "skins: new skin", route: "/games/java/skins", steps: [{ click: "New skin" }] },
  { name: "skins: library", route: "/games/java/skins", steps: [{ click: "Library" }] },
  { name: "skins: add to library", route: "/games/java/skins", steps: [{ click: "Add to library" }] },
  { name: "realms java: try realms", route: "/games/java/realms", steps: [{ click: "Realms" }] },
  { name: "installations: new", route: "/games/java/configurations", steps: [{ click: "New installation" }] },
  // The harness screens: opened the way a user opens them, so a throw in
  // their own markup (or in the menu item that leads there) is reported like
  // any other crash. Export stops before "Export\u2026", which opens the native
  // folder picker and would hang the run.
  { name: "installations: import modpack", route: "/games/java/configurations", steps: [{ click: "Import modpack" }] },
  { name: "installations: export modpack", route: "/games/java/configurations", steps: [{ click: "more options" }, { click: "Export modpack" }] },
  { name: "installations: edit loader row", route: "/games/java/configurations", steps: [{ click: "more options" }, { click: "Edit" }, { selector: "#__hx-loader-row [data-loader='fabric-loader']" }] },
  // Forge and NeoForge are live chips now; pressing one fetches its version
  // list from the host (official sources) without leaving the page.
  { name: "installations: edit loader row forge", route: "/games/java/configurations", steps: [{ click: "more options" }, { click: "Edit" }, { selector: "#__hx-loader-row [data-loader='forge']" }] },
  { name: "installations: edit loader row neoforge", route: "/games/java/configurations", steps: [{ click: "more options" }, { click: "Edit" }, { selector: "#__hx-loader-row [data-loader='neoforge']" }] },
  { name: "account menu", route: "/games/java/play", steps: [{ selector: "#AccountSettingsDropdown" }] },
  { name: "account menu: switch", route: "/games/java/play", steps: [{ selector: "#AccountSettingsDropdown" }, { click: "Switch" }] },
  { name: "account menu: sign out", route: "/games/java/play", steps: [{ selector: "#AccountSettingsDropdown" }, { click: "Sign out" }] },
  { name: "settings: add microsoft account", route: "/settings/accounts", steps: [{ click: "Add Microsoft account" }] },
  { name: "settings: sign in", route: "/settings/accounts", steps: [{ click: "Add Microsoft account" }, { click: "SIGN IN WITH MICROSOFT" }] },
  // The harness' own online sign-in screen (harness/ui.js), opened the way a
  // user opens it. Pressing its button would start a real browser round trip,
  // so the flow stops at the screen.
  { name: "settings: online microsoft sign-in screen", route: "/settings/accounts", steps: [{ click: "Sign in with Microsoft" }] },
  { name: "settings: advanced options", route: "/settings/accounts", steps: [{ click: "ADVANCED OPTIONS" }] },
  { name: "settings: account dots", route: "/settings/accounts", steps: [{ selector: "#AccountSettingsDropdown" }] },
  { name: "settings: language", route: "/settings/general", steps: [{ click: "LANGUAGE" }] },
  { name: "settings: bug report", route: "/settings/about", steps: [{ click: "Report a Launcher bug" }] },
  { name: "settings: licenses", route: "/settings/about", steps: [{ click: "Third-party licenses" }] },
  { name: "home: search", route: "/", steps: [{ click: "SEARCH" }] },
  { name: "home: filters", route: "/", steps: [{ click: "FILTERS" }] },
  { name: "bedrock: marketplace pass buy", route: "/games/bedrock/marketplace_pass", steps: [{ click: "Marketplace Pass" }] },
  { name: "bedrock: creator tools", route: "/games/bedrock/creator_tools", steps: [{ click: "Creator Tools" }] },
  { name: "bedrock: faq expand", route: "/games/bedrock/faq", steps: [{ click: "How" }] },
  { name: "bedrock: play", route: "/games/bedrock/play", steps: [{ click: "Play" }] },
];

function waitForServer() {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const tick = () =>
      http
        .get(BASE + "/", (res) => {
          res.resume();
          resolve();
        })
        .on("error", () => {
          if (Date.now() - started > 15000) reject(new Error("no server"));
          else setTimeout(tick, 200);
        });
    tick();
  });
}

const PROBE = `(() => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const dialogs = [...document.querySelectorAll('[role=dialog]')].map((d) => clean(d.innerText));
  return {
    path: location.pathname,
    text: clean(document.body.innerText).slice(0, 1200),
    dialogs,
    errors: (window.__errors || []).map((e) => e.text.replace(/\\s+/g, ' ').slice(0, 300)),
    // Every URL bridge.js had to rewrite away from a remote host. verify.html
    // fails a route on any of them; here the same rule covers an interaction.
    remote: (window.__netlog || []).length,
    remoteSample: (window.__netlog || []).slice(0, 3),
  };
})()`;

const CLICK = (label) => `(() => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const all = [...document.querySelectorAll('button, a, [role=button], [role=menuitem], [role=tab], label, summary')];
  const el = all.find((e) => {
    const text = clean(e.innerText || e.getAttribute('aria-label') || e.getAttribute('title'));
    const r = e.getBoundingClientRect();
    return text.includes(${JSON.stringify(label)}) && r.width > 2 && r.height > 2;
  });
  if (!el) return false;
  el.click();
  return true;
})()`;

// A step that names a selector instead of a label: the loader row's chips and
// other controls whose text also appears elsewhere in the page are clicked by
// their own attribute, so the press cannot land on the wrong element.
const SELECT = (sel) => `(() => {
  const el = document.querySelector(${JSON.stringify(sel)});
  if (!el) return false;
  const r = el.getBoundingClientRect();
  if (r.width <= 2 || r.height <= 2) return false;
  el.click();
  return true;
})()`;

app.disableHardwareAcceleration();
process.on("unhandledRejection", (e) => {
  console.error("[flows] failed:", e && (e.stack || e.message));
  app.exit(2);
});

app.whenReady().then(async () => {
  const child = spawn("python", [path.join(__dirname, "server.py"), String(PORT)], {
    cwd: path.dirname(__dirname),
    stdio: ["ignore", "ignore", "inherit"],
    windowsHide: true,
  });
  await waitForServer();
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    show: false,
    webPreferences: { backgroundThrottling: false, offscreen: true },
  });
  const report = [];
  for (const flow of FLOWS) {
    await win.loadURL(BASE + flow.route);
    await sleep(2600);
    const steps = [];
    let clickedAll = true;
    for (const step of flow.steps) {
      const target = step.click || step.selector;
      const clicked = step.click
        ? await win.webContents.executeJavaScript(CLICK(step.click))
        : step.selector
          ? await win.webContents.executeJavaScript(SELECT(step.selector))
          : true;
      if (target) {
        steps.push(`${target}${clicked ? "" : " (not found)"}`);
        if (!clicked) clickedAll = false;
      }
      await sleep(1700);
    }
    const info = await win.webContents.executeJavaScript(PROBE);
    const crash = CRASH.test(info.text) || info.dialogs.some((d) => CRASH.test(d));
    // A rewritten URL means the page asked for something off this machine.
    // The guard localised it, but the offline claim is that nothing was asked
    // for at all -- the rule verify.html applies to every route, now applied
    // to what a click opens.
    const leak = info.remote > 0;
    report.push({ flow: flow.name, clicked: steps, clickedAll, crash, leak, ...info });
    console.log(
      `\n${crash || leak || !clickedAll || info.errors.length ? "FAIL" : "ok  "} ${flow.name}  [${steps.join(" > ")}]  path=${info.path}\n  ${
        (info.dialogs[0] || info.text).slice(0, 260)
      }`
    );
    if (leak) console.log(`  remote: ${info.remote}  e.g. ${(info.remoteSample || []).join(", ")}`);
    if (info.errors.length) console.log("  error:", info.errors[0].slice(0, 200));
  }
  win.destroy();
  child.kill();
  fs.writeFileSync(path.join(__dirname, "flows-report.json"), JSON.stringify(report, null, 2));
  const crashes = report.filter((r) => r.crash).length;
  const leaks = report.filter((r) => r.leak).length;
  console.log(
    `\n${report.length} flows, ${crashes} crashed, ${leaks} reached the network, ` +
      `${report.filter((r) => !r.clickedAll).length} with unclicked steps`
  );
  app.exit(crashes || leaks || report.some((r) => !r.clickedAll || r.errors.length) ? 1 : 0);
});
