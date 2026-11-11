// Print what a route actually renders (text, tabs, banners) in Electron.
//   cd app && npx electron ../harness/probe.js /games/java/configurations [...]
const { app, BrowserWindow } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const http = require("http");

const PORT = 8792;
const BASE = `http://127.0.0.1:${PORT}`;
// Electron does not forward a script's CLI arguments on Windows, so the routes
// come from the environment instead.
const routes = (process.env.PROBE_ROUTES || "/")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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

app.disableHardwareAcceleration();
process.on("unhandledRejection", (e) => {
  console.error("[probe] failed:", e && (e.stack || e.message));
  app.exit(2);
});
app.whenReady().then(async () => {
  console.log("[probe] routes:", routes);
  const child = spawn("python", [path.join(__dirname, "server.py"), String(PORT)], {
    cwd: path.dirname(__dirname),
    stdio: ["ignore", "inherit", "inherit"],
    windowsHide: true,
  });
  await waitForServer();
  console.log("[probe] server up");
  const win = new BrowserWindow({
    width: 1280,
    height: 800,
    show: false,
    webPreferences: { backgroundThrottling: false },
  });
  // A persisted redux store keeps the UI's own cache (last skin fetch, cached
  // product list) across runs, which hides exactly the bugs a fresh visit shows.
  // PROBE_CLEAN=1 wipes local storage first, the way a new installation starts.
  if (process.env.PROBE_CLEAN) {
    await win.webContents.session.clearStorageData({
      storages: ["localstorage", "indexdb", "cachestorage", "websql"],
    });
    console.log("[probe] local storage cleared");
  }

  for (const route of routes) {
    const errors = [];
    const listener = (e) => {
      if (e.level === "error" || e.level === 3) errors.push(e.message);
    };
    win.webContents.on("console-message", listener);
    await win.loadURL(BASE + route);
    await sleep(3500);
    const info = await win.webContents.executeJavaScript(`(() => {
      const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
      return {
        path: location.pathname,
        text: clean(document.body.innerText).slice(0, 3000),
        tabs: [...document.querySelectorAll('[aria-selected=true]')].map((e) => clean(e.title || e.innerText).slice(0, 30)),
        alerts: [...document.querySelectorAll('[id$=Alert],[id*=alert]')].map((e) => clean(e.innerText).slice(0, 120)),
        main: [...document.querySelectorAll('main,[role=main]')].map((e) => clean(e.innerText).slice(0, 1500)),
        segments: [...document.querySelectorAll('[data-testid]')].map((e) => e.dataset.testid).slice(0, 40),
        canvases: [...document.querySelectorAll('canvas')].map((c) => ({
          w: c.width, h: c.height,
          cls: String(c.className || '').slice(0, 40),
        })),
        images: [...document.images].filter((i) => i.getAttribute('src')).length,
      };
    })()`);
    if (process.env.PROBE_EVAL) {
      // A one-off expression for checks that need a precise target (an exact
      // button label, a host call) rather than the first fuzzy match.
      const out = await win.webContents.executeJavaScript(process.env.PROBE_EVAL).catch((e) => "error: " + e);
      console.log("[probe] eval:", JSON.stringify(out));
    }
    if (process.env.PROBE_CLICK) {
      const clicked = await win.webContents.executeJavaScript(`(() => {
        const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
        const el = [...document.querySelectorAll('button, a, [role=button], [role=menuitem], [role=tab]')]
          .find((e) => clean(e.innerText || e.getAttribute('aria-label')).includes(${JSON.stringify(
            process.env.PROBE_CLICK
          )}));
        if (!el) return false;
        el.click();
        return true;
      })()`);
      console.log("[probe] clicked:", process.env.PROBE_CLICK, clicked);
      await new Promise((r) => setTimeout(r, 2500));
      info.text = await win.webContents.executeJavaScript(
        "(document.body.innerText||'').replace(/\\s+/g,' ').trim().slice(0,3000)"
      );
      info.path = await win.webContents.executeJavaScript("location.pathname");
    }
    win.webContents.removeListener("console-message", listener);
    console.log("\n=== " + route + "  ->", info.path);
    console.log("tabs:", JSON.stringify(info.tabs));
    console.log("alerts:", JSON.stringify(info.alerts));
    console.log("testids:", JSON.stringify(info.segments));
    console.log("canvases:", JSON.stringify(info.canvases || []), "images:", info.images);
    console.log("text:", info.text);
    if (process.env.PROBE_HTML) {
      const dom = await win.webContents.executeJavaScript(`(() => ({
        dialogs: [...document.querySelectorAll('[role=dialog],[aria-modal=true]')].map((d) => ({
          role: d.getAttribute('role'), id: d.id, cls: String(d.className).slice(0, 60),
          labelled: d.getAttribute('aria-labelledby') || d.getAttribute('aria-label'),
          text: (d.innerText || '').replace(/\\s+/g, ' ').slice(0, 120),
        })),
        nav: document.querySelectorAll('[id$="-menulink-wrap"]').length,
        html: document.body.innerHTML.slice(0, 600),
      }))()`);
      console.log("dom:", JSON.stringify(dom, null, 1));
    }
    if (process.env.PROBE_SHOT) {
      const file = path.join(__dirname, "shots", `probe_${route.replace(/[^a-z0-9]+/gi, "_")}.png`);
      const image = await win.webContents.capturePage();
      require("fs").writeFileSync(file, image.toPNG());
      console.log("shot:", file);
    }
    const find = process.env.PROBE_FIND;
    if (find) {
      const hits = await win.webContents.executeJavaScript(`(() => {
        const needle = ${JSON.stringify(find)}.toLowerCase();
        const all = [...document.querySelectorAll('*')].filter((e) =>
          (e.textContent || '').toLowerCase().includes(needle)
        );
        // Keep only the innermost matches: an ancestor's textContent includes
        // the needle too, and its markup would bury the element that matters.
        return all
          .filter((e) => ![...e.children].some((c) => (c.textContent || '').toLowerCase().includes(needle)))
          .slice(0, 6)
          .map((e) => {
            const chain = [];
            let n = e;
            for (let i = 0; i < 7 && n; i++, n = n.parentElement) {
              chain.push(n.tagName.toLowerCase() + (n.id ? '#' + n.id : '') + (n.dataset && n.dataset.testid ? '[' + n.dataset.testid + ']' : ''));
            }
            return { text: (e.innerText || '').slice(0, 90), chain: chain.join(' < '), html: (e.parentElement ? e.parentElement.outerHTML : '').slice(0, 500) };
          });
      })()`);
      console.log("find:", JSON.stringify(hits, null, 1));
    }
    if (errors.length) console.log("console:", errors.slice(0, 4).join(" | ").slice(0, 900));
  }
  win.destroy();
  child.kill();
  app.exit(0);
});
