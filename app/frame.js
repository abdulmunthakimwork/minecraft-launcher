// Frameless window dragging.
//
// The shell has no title bar, so the window has to be moved from the app's own
// surface — but a CSS drag region is the wrong tool here: `-webkit-app-region`
// is inherited by every descendant that does not opt out, so a drag surface on
// the sidebar (or the whole window) silently swallows clicks on rows, tiles and
// dropdowns that are plain `div`s. That is exactly how the app stopped
// responding in places.
//
// Instead one invisible handle is placed in the sidebar's largest empty gap —
// the band between the product links and the What's New / Settings footer,
// which holds no controls on any route. The gap is measured from the links
// themselves so the handle follows a resize, a different sidebar, or a page
// whose nav is longer than another's. Everything else stays a normal,
// clickable page, and if a sidebar has no usable gap the handle collapses to
// nothing rather than covering a control.
const CSS = `
#__shell-drag-handle {
  position: fixed;
  left: 0;
  z-index: 4;
  -webkit-app-region: drag;
  pointer-events: auto;
  background: transparent;
}
`;

// Creates (or moves) the handle. The sidebar is the `[role="navigation"]` that
// is as tall as the window and narrower than the content area; the settings
// sections have a second, ordinary navigation that must not be used.
const PLACE_HANDLE = `(() => {
  if (window.__shellHandle) return window.__shellHandle;
  const navs = [...document.querySelectorAll('[role="navigation"]')];
  const side = navs.find((el) => {
    const r = el.getBoundingClientRect();
    return r.height > innerHeight * 0.8 && r.width > 120 && r.width < 320;
  });
  if (!side) return false;

  const place = () => {
    const handle = document.getElementById("__shell-drag-handle") || (() => {
      const el = document.createElement("div");
      el.id = "__shell-drag-handle";
      el.setAttribute("aria-hidden", "true");
      document.body.appendChild(el);
      return el;
    })();

    const box = side.getBoundingClientRect();
    const rows = [...side.querySelectorAll("a")]
      .map((a) => a.getBoundingClientRect())
      .filter((r) => r.height > 2 && r.width > 2)
      .map((r) => ({ top: r.top, bottom: r.bottom }))
      .sort((a, b) => a.top - b.top);

    // The sidebar's own edges count as gap boundaries, so a nav with one entry
    // still yields a band (below it) instead of nothing.
    const edges = [{ top: box.top, bottom: box.top }, ...rows, { top: box.bottom, bottom: box.bottom }];
    let best = { top: 0, bottom: 0 };
    for (let i = 1; i < edges.length; i++) {
      const top = edges[i - 1].bottom;
      const bottom = edges[i].top;
      if (bottom - top > best.bottom - best.top) best = { top, bottom };
    }

    // A few pixels of margin so the handle never overlaps a link's hit area.
    const height = Math.max(0, Math.round(best.bottom - best.top) - 12);
    handle.style.top = Math.round(best.top + 6) + "px";
    handle.style.height = height >= 24 ? height + "px" : "0px";
    handle.style.width = Math.round(box.width) + "px";
  };

  place();
  addEventListener("resize", place);
  // The nav re-renders on navigation, so the band has to follow it -- but only
  // once per frame, not once per mutated node.
  let queued = false;
  new MutationObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      place();
    });
  }).observe(side, { childList: true, subtree: true });
  window.__shellHandle = true;
  return true;
})()`;

function install(win) {
  if (!win || win.isDestroyed()) return;
  let timer = null;

  // `dom-ready` fires before the bundle renders, so the sidebar does not exist
  // yet on the first pass; keep looking for a few seconds.
  const place = (tries) => {
    if (win.isDestroyed()) return;
    win.webContents
      .executeJavaScript(PLACE_HANDLE)
      .then((placed) => {
        if (!placed && tries < 40) timer = setTimeout(() => place(tries + 1), 300);
      })
      .catch(() => {});
  };

  const apply = () => {
    win.webContents.insertCSS(CSS).catch(() => {
      /* the page went away mid-load; the next navigation retries */
    });
    if (timer) clearTimeout(timer);
    place(0);
  };

  win.webContents.on("dom-ready", apply);
  win.webContents.on("did-finish-load", apply);
  win.webContents.on("did-navigate-in-page", apply);
  win.on("resize", () => {
    if (!win.isDestroyed()) win.webContents.executeJavaScript(PLACE_HANDLE).catch(() => {});
  });
}

module.exports = { install, CSS, PLACE_HANDLE };
