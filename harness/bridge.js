// Mock native host for the reconstructed Minecraft Launcher UI.
// The real launcher injects `window.appData` (state) and `window.MCLauncher`
// (IPC) from launcher.dll before the webpack bundles run. This file recreates
// both surfaces so the extracted UI can boot outside CEF.
(function () {
  "use strict";

  const LOG = (...a) => console.info("[mock-host]", ...a);

  // --- error recording ----------------------------------------------------
  // The UI wraps every page in a React error boundary that renders a
  // "Whoops!" panel and swallows the cause, so a broken page looks like a
  // rendered page unless something keeps the error. React logs the failing
  // component and its stack with console.error just before the boundary
  // paints; recording those here is what lets the route check name the page
  // that broke, and the component that threw, rather than only noticing the
  // panel. Errors are kept per document, so a verification frame reads its own.
  const ERRORS = [];
  window.__errors = ERRORS;
  // Record of native calls the UI made, in order. When a page "does nothing"
  // on a click, this says whether the host was ever asked to do anything.
  const CALLS = [];
  window.__calls = CALLS;
  const trace = (name) => CALLS.push(name);
  const describe = (v) => {
    if (v instanceof Error) return (v.stack || v.message || String(v)).slice(0, 1200);
    if (typeof v === "string") return v.slice(0, 1200);
    try {
      return JSON.stringify(v).slice(0, 1200);
    } catch (e) {
      return String(v).slice(0, 1200);
    }
  };
  const recordError = (kind, args) => {
    const text = Array.prototype.map.call(args, describe).join(" ");
    if (!text || /\[mock-host\]/.test(text)) return;
    ERRORS.push({ kind, text, at: Date.now() });
  };
  const nativeConsoleError = console.error.bind(console);
  console.error = function (...args) {
    recordError("console", args);
    return nativeConsoleError(...args);
  };
  window.addEventListener("error", (e) => {
    recordError("window", [e.error || e.message]);
  });
  window.addEventListener("unhandledrejection", (e) => {
    recordError("rejection", [e.reason]);
  });

  // --- local-first network guard -----------------------------------------
  // The launcher must not depend on any remote service. Every URL the UI
  // touches is either already local, mirrored from the captured content, or
  // answered by a local stub. The guard is deliberately total: even a URL
  // that no longer appears anywhere in the bundle is rewritten to a local
  // stub path, so nothing can reach the network by accident.
  //
  // window.__netlog records every remote URL the app asked for, which is how
  // the offline build is verified.
  const NETLOG = [];
  window.__netlog = NETLOG;

  // Roots whose payloads are mirrored on this server: captured content plus
  // the franchise services implemented in harness/local_api.py.
  const MIRRORED_ROOTS = [
    ["https://launchercontent.mojang.com", ""],
    ["https://store.mktpl.minecraft-services.net", "/api/store"],
    ["https://entitlements.mktpl.minecraft-services.net", "/api/entitlements"],
    ["https://authorization.franchise.minecraft-services.net", "/api/auth"],
    [
      "https://bedrock.frontendlegacy.realms.minecraft-services.net",
      "/api/realms/bedrock",
    ],
    [
      "https://java.frontendlegacy.realms.minecraft-services.net",
      "/api/realms/java",
    ],
    ["https://persona-secondary.franchise.minecraft-services.net", "/api/persona"],
    ["https://messaging.mktpl.minecraft-services.net", "/api/messaging"],
    ["https://client.discovery.minecraft-services.net", "/api/discovery"],
    [
      "https://client.stage-b4c666a2.discovery.minecraft-services.net",
      "/api/discovery",
    ],
    ["https://local.minecraft.net", "/api/local"],
    ["https://api.minecraftservices.com", "/api/mcs"],
    ["https://api.minecraftservices.net", "/api/mcs"],
  ];

  const isLocalUrl = (raw) => /^(data|blob|about|javascript):/i.test(raw);

  const localize = (raw) => {
    if (typeof raw !== "string" || !raw || isLocalUrl(raw)) return raw;
    let u;
    try {
      u = new URL(raw, location.href);
    } catch (e) {
      return raw;
    }
    // The served bundle carries an empty content base, so a component that
    // joins it with its own slash (`${base}/${path}`) produces "//v2/images/x".
    // A browser reads that as a protocol-relative authority, but here it is a
    // path: no real service host is dotless, so a dotless authority can only be
    // a doubled path fragment.
    if (raw.startsWith("//") && u.hostname && !u.hostname.includes(".") && !u.port) {
      return ("/" + u.host + u.pathname + u.search).replace(/\/{2,}/g, "/");
    }
    if (u.origin === location.origin) return raw;
    // A few components interpolate the content root and the content-relative
    // path with a slash of their own, producing `host//v2/...`. A real server
    // folds that away; a string-prefix rewrite would not, and the path would
    // resolve as an authority. Collapse it before mapping.
    const path = (u.pathname + u.search).replace(/\/{2,}/g, "/");
    for (const [from, to] of MIRRORED_ROOTS) {
      if (u.href.startsWith(from)) return to + path;
    }
    // Telemetry/error reporting has no local mirror and must never leave the
    // machine; the stub discards it and it is not counted as a remote request
    // (the offline build is verified against NETLOG).
    if (u.hostname === "sentry.io" || u.hostname.endsWith(".sentry.io")) {
      return "/__stub/" + u.host + (path === "/" ? "" : path);
    }
    NETLOG.push(u.href);
    // Everything else is answered by the local stub server: images become a
    // transparent placeholder, identity endpoints return a clean 401, and the
    // rest come back as an empty document.
    return "/__stub/" + u.host + (path === "/" ? "" : path);
  };

  // Some hosts (and any occluded or background pane) stop delivering animation
  // frames without notice — sometimes only after boot. The app's router commits
  // navigations inside requestAnimationFrame, and the Babylon skin renderer
  // waits on one inside an onload handler, so a dead frame source stalls both.
  // Rather than a one-shot boot probe, race every frame: native request
  // first, timer as a fallback that wins only when frames stop arriving.
  const nativeRAF = window.requestAnimationFrame && window.requestAnimationFrame.bind(window);
  const nativeCAF = window.cancelAnimationFrame && window.cancelAnimationFrame.bind(window);
  if (nativeRAF) {
    let missed = 0;
    window.requestAnimationFrame = function (cb) {
      if (missed >= 4) {
        return setTimeout(() => cb(performance.now()), 16);
      }
      let done = false;
      const once = (t) => {
        if (done) return;
        done = true;
        if (missed > 0) missed -= 1;
        cb(t === undefined ? performance.now() : t);
      };
      const rafId = nativeRAF(once);
      const tid = setTimeout(() => {
        if (done) return;
        done = true;
        missed = Math.min(missed + 1, 8);
        if (missed === 4) console.info("[mock-host] animation frames stalled; animating on timers");
        cb(performance.now());
      }, 60 + missed * 60);
      return { raf: rafId, timer: tid };
    };
    window.cancelAnimationFrame = function (handle) {
      if (!handle) return;
      if (typeof handle === "object") {
        if (nativeCAF && typeof handle.raf === "number") nativeCAF(handle.raf);
        if (typeof handle.timer === "number") clearTimeout(handle.timer);
        return;
      }
      if (nativeCAF) nativeCAF(handle);
    };
    console.info("[mock-host] hybrid animation-frame source armed (native + timer fallback)");
  }

  const nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    if (typeof input === "string") {
      const next = localize(input);
      if (next !== input) {
        console.info("[mock-host] local fetch:", input, "->", next);
        return nativeFetch(next, init);
      }
      return nativeFetch(input, init);
    }
    if (input && typeof input.url === "string") {
      const next = localize(input.url);
      if (next !== input.url) {
        console.info("[mock-host] local fetch:", input.url, "->", next);
        return nativeFetch(new Request(next, input), init);
      }
    }
    return nativeFetch(input, init);
  };

  // XHR is used for the news feed and a few service calls; it needs the same
  // treatment as fetch.
  const nativeOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    const next = localize(String(url));
    if (next !== String(url)) {
      console.info("[mock-host] local xhr:", url, "->", next);
      url = next;
    }
    return nativeOpen.call(this, method, url, ...rest);
  };

  // Analytics beacons are fire-and-forget XHRs; they get the same treatment.
  if (navigator.sendBeacon) {
    const nativeBeacon = navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon = function (url, data) {
      const next = localize(String(url));
      if (next !== String(url)) console.info("[mock-host] local beacon:", url, "->", next);
      return nativeBeacon(next, data);
    };
  }

  // A socket cannot be answered by a static file server. Rather than leaving
  // the app waiting on a connection that will never open, fail immediately and
  // report it, so any caller's error path runs instead of hanging.
  function OfflineSocket(url) {
    console.warn("[mock-host] socket refused (local-only build):", url);
    this.url = String(url);
    this.readyState = 3; // CLOSED
    this.bufferedAmount = 0;
    this.close = () => {};
    this.send = () => {};
    this.addEventListener = () => {};
    this.removeEventListener = () => {};
    setTimeout(() => {
      if (typeof this.onerror === "function") this.onerror({ type: "error", target: this });
      if (typeof this.onclose === "function") this.onclose({ type: "close", target: this });
    }, 0);
  }
  window.WebSocket = OfflineSocket;
  window.EventSource = OfflineSocket;

  // Element attributes that carry a URL: images, scripts, stylesheets, fonts,
  // iframes and media. Patching setAttribute covers React's DOM writes.
  //
  // Anchors are deliberately left alone: their href is a destination the user
  // may follow, and the click handler below sends it to the system browser
  // instead of letting the launcher navigate away from itself.
  const localizeSrcset = (value) => typeof value === "string"
    ? value.replace(/(?:https?:)?\/\/[^\s,]+/gi, (url) => localize(url)) : value;
  const URL_ATTRS = new Set(["src", "srcset", "href", "poster", "data-src"]);
  const nativeSetAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    const attr = String(name).toLowerCase();
    const isAnchor = this.tagName === "A" && attr === "href";
    if (!isAnchor && URL_ATTRS.has(attr) && typeof value === "string") {
      const next = attr === "srcset" ? localizeSrcset(value) : localize(value);
      if (next !== value) {
        console.info("[mock-host] local attr:", name, value, "->", next);
        value = next;
      }
    }
    return nativeSetAttribute.call(this, name, value);
  };

  // Property assignment (img.src = ...) does not go through setAttribute.
  const patchSrcProp = (proto, prop) => {
    const desc = Object.getOwnPropertyDescriptor(proto, prop);
    if (!desc || !desc.set) return;
    Object.defineProperty(proto, prop, {
      ...desc,
      set(v) {
        const next = prop === "srcset" ? localizeSrcset(v) : typeof v === "string" ? localize(v) : v;
        if (next !== v) console.info("[mock-host] local prop:", prop, v, "->", next);
        desc.set.call(this, next);
      },
    });
  };
  patchSrcProp(HTMLImageElement.prototype, "src");
  patchSrcProp(HTMLImageElement.prototype, "srcset");
  patchSrcProp(HTMLSourceElement.prototype, "srcset");
  patchSrcProp(HTMLScriptElement.prototype, "src");
  patchSrcProp(HTMLLinkElement.prototype, "href");
  patchSrcProp(HTMLIFrameElement.prototype, "src");
  patchSrcProp(HTMLSourceElement.prototype, "src");
  patchSrcProp(HTMLMediaElement.prototype, "src");

  // The UI opens documentation and support pages in a new tab. Inside the
  // launcher that would navigate the whole app away, so external links are
  // handed to the system browser instead of the app frame.
  document.addEventListener(
    "click",
    (e) => {
      const a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
      if (!a) return;
      const href = a.getAttribute("href") || "";
      if (!/^https?:/i.test(href)) return;
      e.preventDefault();
      window.open(href, "_blank", "noopener,noreferrer");
    },
    true
  );

  // A click handler cannot see every way the app leaves. The account
  // dropdown's "Manage profile" runs the MSAL flow, which calls
  // `window.location.assign("https://www.minecraft.net/...")` directly --
  // inside launcher.dll a request handler catches that and opens the system
  // browser, but in a plain browser it navigates the launcher itself away to
  // minecraft.net and the app is gone. The Navigation API's cancelable
  // `navigate` event is the only hook that observes every cross-document
  // navigation (location.assign/replace, the href setter, link clicks, form
  // submits). Off-origin http(s) targets are cancelled here and handed to
  // window.open instead, which the desktop shell routes to the system browser
  // and a normal browser opens as a tab. Same-origin navigations -- including
  // the router's own pushState -- pass through untouched.
  // Feature-tested on `navigation` (the API object), not `onnavigate`: the
  // handler property is not exposed in every Chromium build even though the
  // event dispatches, and a false there silently skips the whole guard.
  if (typeof navigation !== "undefined") {
    console.info("[mock-host] navigation guard armed");
    window.addEventListener(
      "navigate",
      (e) => {
        let url;
        try {
          url = new URL(e.destination.url, location.href);
        } catch (err) {
          return;
        }
        // Logged before the cancelable check so the harness can tell an
        // uncancellable WebView (where nothing page-side can stop the
        // navigation) apart from a guard that never saw the event.
        console.info(
          "[mock-host] navigate event:", url.href,
          "cancelable=", e.cancelable, "type=", e.navigationType
        );
        if (!e.cancelable) return;
        if (url.origin === location.origin) return;
        if (!/^https?:$/.test(url.protocol)) return;
        e.preventDefault();
        console.info("[mock-host] external navigation opened externally:", url.href);
        window.open(url.href, "_blank", "noopener,noreferrer");
      },
      true
    );
  }

  // HTML parsing bypasses attribute/property setters. Localize inside an inert
  // template before live insertion, since an observer runs after image requests
  // have already started. The observer also covers later subtree mutations.
  const SWEEP_ATTRS = ("src srcset poster data-src").split(" ");
  const sweep = (root) => {
    if (!root || ![1, 11].includes(root.nodeType)) return;
    const nodes = root.querySelectorAll
      ? [...(root.nodeType === 1 ? [root] : []), ...root.querySelectorAll("img,source,script,link,iframe,video,audio")]
      : [root];
    for (const el of nodes) {
      for (const attr of SWEEP_ATTRS) {
        const value = el.getAttribute && el.getAttribute(attr);
        if (value) el.setAttribute(attr, attr === "srcset" ? localizeSrcset(value) : localize(value));
      }
      if (el.tagName === "LINK") {
        const href = el.getAttribute("href");
        if (href) el.setAttribute("href", localize(href));
      }
    }
    root.querySelectorAll && root.querySelectorAll("template").forEach((template) => sweep(template.content));
  };
  const innerHTML = Object.getOwnPropertyDescriptor(Element.prototype, "innerHTML");
  const localizeMarkup = (value) => {
    if (typeof value !== "string" || !/(?:https?:)?\/\//i.test(value) || !innerHTML || !innerHTML.set) return value;
    const template = document.createElement("template");
    innerHTML.set.call(template, value);
    sweep(template.content);
    return innerHTML.get.call(template);
  };
  for (const property of ["innerHTML", "outerHTML"]) {
    const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, property);
    if (!descriptor || !descriptor.set) continue;
    Object.defineProperty(Element.prototype, property, {
      ...descriptor,
      set(value) { descriptor.set.call(this, localizeMarkup(value)); },
    });
  }
  const insertAdjacentHTML = Element.prototype.insertAdjacentHTML;
  Element.prototype.insertAdjacentHTML = function (position, value) {
    return insertAdjacentHTML.call(this, position, localizeMarkup(value));
  };
  // --- sidebar scope ------------------------------------------------------
  // This build ships Java and Bedrock only. The other products stay in appData
  // because the launcher's product store, route table and purchase dialogs all
  // index the full set by id -- the dialogs mount at the root for every product
  // and read `productData[id].productId`, so deleting one there throws before
  // the shell paints. Their nav entries are hidden here instead, and a deep
  // link to one falls back to the launcher home.
  const HIDDEN_PRODUCTS = "dungeons|dungeons2|legends";
  const HIDDEN_LINK = new RegExp("^/games/(" + HIDDEN_PRODUCTS + ")/", "i");
  if (HIDDEN_LINK.test(location.pathname)) {
    console.info("[mock-host] product not in this build, home instead:", location.pathname);
    history.replaceState(null, "", "/");
  }
  const hideHiddenProducts = (root) => {
    const scope = root && root.querySelectorAll ? root : document;
    const links = scope.querySelectorAll("a[role=tab][href]");
    links.forEach((a) => {
      if (!HIDDEN_LINK.test(a.getAttribute("href") || "")) return;
      const entry = a.closest('[id$="-menulink-wrap"]') || a.parentElement || a;
      if (entry.dataset.harnessHidden === "1") return;
      entry.dataset.harnessHidden = "1";
      entry.style.display = "none";
    });
  };

  const observer = new MutationObserver((records) => {
    for (const record of records) {
      record.addedNodes.forEach((node) => {
        if (node.nodeType === 1) {
          sweep(node);
          hideHiddenProducts(node);
        }
      });
    }
    // React rewrites the account chip whenever the shell re-renders, so the
    // offline label is re-applied after every mutation that touches it.
    applyAccountType();
  });
  const startObserver = () => observer.observe(document.documentElement, {
    subtree: true,
    childList: true,
  });
  if (document.documentElement) startObserver();
  else document.addEventListener("DOMContentLoaded", startObserver, { once: true });

  // The nav can render before or after the observer is armed; a short sweep of
  // the first few seconds covers both without polling forever.
  const hideNow = () => hideHiddenProducts(document);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", hideNow, { once: true });
  } else {
    setTimeout(hideNow, 0);
  }
  [150, 600, 1500, 3000].forEach((ms) => setTimeout(hideNow, ms));
  [150, 600, 1500].forEach((ms) => setTimeout(applyAccountType, ms));

  // The app can only be *seeded* onto a handful of pages (its persisted
  // lastVisitedPage table). Anything deeper -- a product's extra tab, or a
  // settings page -- has to be reached through the app's own navigation, so a
  // URL pointing at one is honoured by clicking the matching link once the
  // shell has rendered.
  const normalisePath = (p) => (p || "/").replace(/\/+$/, "") || "/";
  const pathOf = (page) => PAGES[page] || null;
  // Activating a tab is asynchronous and a click that lands before the shell has
  // mounted its routes is simply swallowed, so a navigation is confirmed by the
  // tab's own selected state and retried until it takes. Clicking once and
  // assuming success left /settings/about showing General roughly half the time.
  const isActive = (path) => {
    const link = document.querySelector('a[href="' + path + '"]');
    return Boolean(link) && (link.getAttribute("aria-selected") === "true" ||
      link.getAttribute("aria-current") === "page");
  };
  // The settings section renders its own General/Accounts/About links, so the
  // section is open exactly when a second route into General exists. Declared
  // before openPath so that helper can never run before it is initialised.
  const settingsOpen = () => document.querySelectorAll('a[href="/settings/general"]').length > 1;
  const openPath = (path, attempt = 0) => {
    if (isActive(path)) {
      if (attempt > 0) console.info("[mock-host] opened", path);
      // The route seeded from the URL is now on screen; ui.js's address-bar
      // mirror may catch up.
      window.__entrySettled = true;
      return;
    }
    if (attempt > 10) {
      console.warn("[mock-host] gave up opening", path);
      return;
    }
    if (attempt === 0) console.info("[mock-host] opening", path);
    const link = document.querySelector('a[href="' + path + '"]');
    if (link) {
      link.click();
    } else if (path.startsWith("/settings/") && !settingsOpen()) {
      // A settings sub-section's links only exist once its section is open. The
      // nav tab for that section is a *toggle*, so it is clicked only while the
      // section is closed: clicking it again collapses back to home, which is
      // what a blind retry used to do to /settings/accounts and /settings/about.
      const section = document.querySelector('a[href="/settings/general"]');
      if (section) section.click();
    }
    setTimeout(() => openPath(path, attempt + 1), 400);
  };
  const syncRouteToUrl = () => {
    const path = normalisePath(location.pathname);
    const page = entryPage(path);
    if (!page) return;
    const landing = pathOf(page);
    // "/" and "/home/general" are the same page; everything else must match.
    if (!landing || path === landing || (landing === "/home/general" && path === "/")) return;
    openPath(path);
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", syncRouteToUrl, { once: true });
  } else {
    // Deferred so the route tables declared further down are initialised.
    setTimeout(syncRouteToUrl, 0);
  }

  // The launcher's franchise service roots. The real host resolves these
  // through service discovery; locally they all point back at this server.
  // Resolved through a function declaration so `common` (created above) can
  // reference it without depending on declaration order.
  function serviceRoot(service) {
    return (
      SERVICE_ROOTS[service] ||
      "/api/" + String(service || "unknown").toLowerCase()
    );
  }

  const SERVICE_ROOTS = {
    VOTING: "/api/voting",
    AUTH: "/api/auth",
    REALMS_BEDROCK: "/api/realms/bedrock",
    REALMS_JAVA: "/api/realms/java",
    REALMS_PAYMENTS: "/api/realms/payments",
    MARKETPLACE_ENTITLEMENTS: "/api/entitlements",
    MARKETPLACE_STORE: "/api/store",
    MESSAGING: "/api/messaging",
  };

  // --- universal inert value: callable, indexable, coercible --------------
  function anyValue(onCall) {
    const fn = function () { return anyValue(onCall); };
    return new Proxy(fn, {
      get(t, k) {
        if (k === "then") return undefined;            // never a thenable
        if (k === Symbol.iterator) return function* () {};
        if (k === Symbol.toPrimitive) return () => 0;
        if (k === "toJSON") return () => ({});
        if (k === "length") return 0;
        if (k === "toString") return () => "";
        if (k === "valueOf") return () => 0;
        if (k in t) return t[k];
        return anyValue(onCall);
      },
      apply() { if (onCall) onCall(); return anyValue(onCall); },
      construct() { if (onCall) onCall(); return anyValue(onCall); },
    });
  }

  // --- namespace: explicit members first, inert callables for the rest ----
  // Used for MCLauncher and its sub-objects (common, account, ...) so native
  // calls we do not model still resolve instead of throwing. The first call
  // of each unmodelled member is logged, so an audit sweep can list every
  // host call the UI actually makes.
  function namespace(members, label) {
    return new Proxy(members, {
      get(t, k) {
        if (k in t) return t[k];
        if (typeof k === "symbol" || k === "then") return undefined;
        if (k === "toJSON") return () => ({});
        let logged = false;
        const where = (label || "host") + "." + String(k);
        const memo = anyValue(() => {     // callable, constructible, await-safe
          if (!logged) {
            logged = true;
            console.info("[mock-host] inert host call:", where);
          }
        });
        t[k] = memo;
        return memo;
      },
    });
  }

  // --- result object: what native IPC handlers hand back -------------------
  // Covers `{value}`, `{success}`, `{data}`, array iteration and coercion so
  // callers never destructure undefined.
  const inert = {
    value: null,
    data: null,
    result: null,
    ok: true,
    success: true,
    error: null,
    errors: [],
    count: 0,
    length: 0,
    items: [],
    entries: [],
  };
  const inertResult = new Proxy(inert, {
    get(t, k) {
      if (k in t) return t[k];
      if (k === "then") return undefined;
      if (k === Symbol.iterator) return function* () {};
      if (k === Symbol.toPrimitive) return () => 0;
      if (k === "toJSON") return () => ({});
      if (k === "toString") return () => "";
      if (k === "valueOf") return () => 0;
      return anyValue();
    },
  });

  // --- appData: real state where we have it, inert values elsewhere -------
  const seed = window.__APPDATA__ || {};
  const appData = new Proxy(seed, {
    get(t, k) {
      if (k in t) return t[k];
      if (k === "toJSON") return () => t;
      return anyValue();
    },
  });
  window.appData = appData;

  // --- accounts -----------------------------------------------------------
  // The account store lives on the server (`/__accounts`) because offline
  // accounts have to survive a reload, and because a local account is created
  // by the same call the UI uses for a Microsoft sign-in. `activeAccount` is
  // the record itself, not an id: the account reducer reads `.id` off it.
  const accountsFromSeed = () => {
    const accounts = seed.__accounts || [];
    return {
      accounts,
      activeAccount: seed.__activeAccountId || undefined,
      errors: [],
      // launcher.dll reports whether it can reach the OS credential store. The
      // harness keeps accounts on disk, which is always "available"; without
      // this flag the UI shows its "you will not be logged in automatically" banner.
      hasSecureStorageAccess: true,
    };
  };
  let accountState = accountsFromSeed();
  // `activeAccount` in the payload is an id; everywhere else in this file the
  // record is what is needed.
  const activeRecord = () =>
    (accountState.accounts || []).find((a) => a && a.id === accountState.activeAccount) || null;

  const canUseLocalJava = (productId, account = activeRecord()) => Boolean(
    seed.capabilities && seed.capabilities.localJava && productId === "java" &&
    account && (account.offline || account.type === "Offline")
  );

  const isServiceSupported = (service) => Boolean(seed.capabilities && seed.capabilities[service]);

  const refreshAccounts = async () => {
    try {
      const res = await fetch("/__accounts", { cache: "no-store" });
      if (res.ok) accountState = await res.json();
    } catch (e) {
      console.error("[mock-host] account refresh failed:", e);
    }
    return accountState;
  };

  // Mirror the new account list into the state the shell renders from. The
  // sidebar reads `appData.users`, and a native host announces a change by
  // dispatching the slice's event -- the same lever used for every other slice.
  const publishAccounts = () => {
    const accounts = accountState.accounts || [];
    const active = activeRecord();
    seed.__accounts = accounts;
    seed.__activeAccountId = active ? active.id : undefined;
    const userMap = {};
    accounts.forEach((a) => {
      if (a && a.id) userMap[a.id] = a;
    });
    seed.users = { selectedUserId: seed.__activeAccountId, userMap };
    if (window.__host && window.__host.push) window.__host.push("users");
    // The account provider owns the sign-in gate separately from the users
    // model. It re-reads getAccountData only when this native event fires.
    window.dispatchEvent(new Event("ACCOUNT_DATA_EVENT"));
    applyAccountType();
  };

  const accountAction = async (action, extra) => {
    const res = await fetch("/__accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, ...(extra || {}) }),
    });
    if (!res.ok) {
      let message = "account request failed";
      try {
        const body = await res.json();
        if (body && body.error) message = body.error;
      } catch (e) {
        /* keep the generic message */
      }
      throw new Error(message);
    }
    accountState = await res.json();
    publishAccounts();
    return accountState;
  };

  // --- Microsoft sign-in ---------------------------------------------------
  // The real flow lives on the host (harness/msa.py): OAuth with PKCE, then
  // the Xbox/Minecraft token chain, all server-side. The page's only part is
  // asking the host for the authorize URL, handing it to the system browser
  // (the anchor-click interceptor below routes it there), and polling until
  // the browser's redirect has come back to the host. Offline accounts never
  // touch this path -- "Add offline account" goes through accountAction.
  const microsoftSignIn = async () => {
    trace("microsoftSignIn");
    const boot = await fetch("/__msa/signin", { cache: "no-store" })
      .then((r) => r.json())
      .catch(() => ({}));
    if (!boot || !boot.url) {
      throw hostError(
        "MSA_SIGNIN_FAILED",
        (boot && boot.error) || "could not start Microsoft sign-in"
      );
    }
    const opener = document.createElement("a");
    opener.href = boot.url;
    opener.rel = "noopener";
    document.body.appendChild(opener);
    opener.click();
    opener.remove();
    const deadline = Date.now() + 10 * 60 * 1000;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      let st;
      try {
        st = await fetch("/__msa/status", { cache: "no-store" }).then((r) => r.json());
      } catch (e) {
        continue;      // the host answers on the next tick
      }
      if (st && st.phase === "done") {
        await refreshAccounts();
        if (st.account && st.account.id) {
          await accountAction("switch", { id: st.account.id });
        }
        publishAccounts();
        return activeRecord();
      }
      if (st && st.phase === "error") {
        throw hostError("MSA_SIGNIN_FAILED", st.error || "Microsoft sign-in failed");
      }
    }
    throw hostError("MSA_SIGNIN_FAILED", "Microsoft sign-in timed out; try again.");
  };

  // The harness' own screens (harness/ui.js) add accounts through the same call
  // the launcher's sign-in uses, so the sidebar, the dropdown and the store all
  // update exactly as they do for a Microsoft sign-in.
  window.__harness = Object.assign(window.__harness || {}, {
    accountAction: accountAction,
    activeAccount: () => activeRecord(),
    microsoftSignIn: microsoftSignIn,
    canUseLocalJava,
    isServiceSupported,
  });

  // The dropdown's account-type line is a hard-coded "Microsoft account" in
  // this build. A local account is not one, so the label is corrected in
  // place; the markup and styling are untouched. Declared as a function so the
  // mutation observer (armed further down, but able to fire immediately) can
  // call it without hitting the temporal dead zone.
  function applyAccountType() {
    const active = activeRecord();
    const offline = Boolean(active && (active.offline || active.type === "Offline"));
    const el = document.querySelector('[data-testid="user-dropdown-account-type"]');
    if (!el) return;
    const want = offline ? "Offline account" : "Microsoft account";
    if ((el.textContent || "").trim() !== want) el.textContent = want;
  }

  const systemInfo = appData.systemInfo || {
    osName: "Windows",
    osVersion: "10.0.26100",
    platform: "win32",
    isHandheld: false,
  };

  // --- store persistence -------------------------------------------------
  // launcher.dll persists the UI's UiSettings/UiEvents blobs. The harness backs
  // them with /__state/* on the server, which seeds from the captured state and
  // writes changes to harness/state.json (the capture itself stays untouched).
  const storeEndpoint = (name) => "/__state/" + encodeURIComponent(name);

  // The launcher normally opens on whatever page was last visited, which it
  // keeps in the UiSettings store; the URL is only cosmetic inside CEF. Served
  // from a real origin the URL *is* the entry point, so a link to
  // /games/bedrock/play should open Bedrock. This is the inverse of the app's
  // own lastVisitedPage -> route table (PAGES).
  const PAGES = {
    home: "/home/general",
    java: "/games/java/play",
    bedrock: "/games/bedrock/play",
    dungeons: "/games/dungeons/play",
    dungeons2: "/games/dungeons2/play",
    legends: "/games/legends/play",
    "realms-java": "/games/java/realms",
    "realms-bedrock": "/games/bedrock/realms",
    "bedrock-creator-tools": "/games/bedrock/creator_tools",
  };
  const entryPage = (path) => {
    const m = /^\/games\/([a-z0-9]+)\/([a-z_]+)$/.exec(path);
    if (m) {
      const [, product, tab] = m;
      if (tab === "realms") return "realms-" + product;
      if (product === "bedrock" && tab === "creator_tools") return "bedrock-creator-tools";
      return product;
    }
    if (path === "/" || path === "/index.html" || path.startsWith("/settings")) return "home";
    if (path === "/home/general") return "home";
    return null;
  };
  const ENTRY_PAGE = entryPage(normalisePath(location.pathname));
  // Only steer the *first* read; later reads must reflect what the user chose.
  let entryPagePending = Boolean(ENTRY_PAGE);

  // ui.js mirrors the selected tab's route into the address bar with
  // replaceState, but it must not run while a deep link's route is still
  // being applied: the shell's first render can select the product's default
  // tab, which would rewrite a pending /games/java/skins to /games/java/play
  // before the click-through below lands. Hold the mirror until an openPath
  // confirms the entry route, or until this settle delay, whichever first.
  window.__entrySettled = !ENTRY_PAGE;
  setTimeout(() => {
    window.__entrySettled = true;
  }, 2500);

  const applyEntryPage = (name, payload) => {
    if (name !== "UiSettings" || !entryPagePending) return payload;
    entryPagePending = false;
    let blob = payload && payload.value;
    // A fresh profile has no UiSettings blob yet. Seed the first native read
    // from an empty object so bootstrap still honors the requested product;
    // the official UI merges this partial preference with its own defaults.
    if (blob == null) blob = "{}";
    if (typeof blob !== "string") return payload;
    try {
      const parsed = JSON.parse(blob);
      if (parsed && parsed.lastVisitedPage !== ENTRY_PAGE) {
        parsed.lastVisitedPage = ENTRY_PAGE;
        return { ...payload, value: JSON.stringify(parsed) };
      }
    } catch (e) {
      console.warn("[mock-host] UiSettings is not JSON; ignoring route seed");
    }
    return payload;
  };

  // Real download: the harness server streams the client jar, windows
  // libraries, assets and the matching Java runtime from the captured
  // manifest, verifying each sha1. This side mirrors progress into the
  // downloads slice and flips the product state from filesystem evidence
  // (versions/<id>/<id>.jar on the game directory).
  const LOADING_PHASES = {
    client: "Downloading client",
    libraries: "Downloading libraries",
    "asset index": "Downloading asset index",
    assets: "Downloading assets",
    "java runtime": "Downloading Java runtime",
  };
  function hostError(code, message) {
    if (typeof LauncherError === "function") return new LauncherError(code, message);
    const err = new Error(message || code);
    err.errorCode = code;
    return err;
  }
  function markInstalled() {
    try {
      const entry = seed.productInfo.products.java.versionData[0];
      entry.isInstalled = true;
      entry.isPlayableOffline = true;
      entry.pendingUpdate = null;
      push("productInfo");
    } catch (e) {
      /* product slice not ready; installer page will re-read on reload */
    }
  }
  async function installControl(action) {
    const current = seed.downloads && seed.downloads.current;
    if (!current || !current.jobId) return false;
    const ownedId = current.jobId;
    const out = await osRequest('/__install_control', {jobId: ownedId, action});
    if (!out || !out.ok || out.error || !out.status || out.status.jobId !== ownedId) return false;
    if (!seed.downloads.current || seed.downloads.current.jobId !== ownedId) return false;
    Object.assign(seed.downloads.current, out.status, {
      state: out.status.controlStatus === 'cancelled' ? 'Cancelled' : out.status.paused ? 'Paused' :
        out.status.controlReason && !out.status.canPause && !out.status.canResume ? 'Finalizing' : 'Downloading'
    });
    push('downloads');
    return true;
  }
  function trackDownload(ownedId) {
    return new Promise((done, fail) => {
      let lastJson = "";
      const tick = async () => {
        let snapshot = null;
        try {
          const res = await fetch("/__install_status", { cache: "no-store" });
          snapshot = await res.json();
        } catch (e) {
          /* transient hiccup; keep polling */
        }
        if (snapshot) {
          if (ownedId && snapshot.jobId !== ownedId) return fail(hostError('INSTALL_JOB_REPLACED', 'The installation job is no longer current.'));
          if (!ownedId && snapshot.jobId) ownedId = snapshot.jobId;
          const cancelled = snapshot.phase === 'cancelled' || snapshot.controlStatus === 'cancelled';
          const blob = JSON.stringify(snapshot);
          if (blob !== lastJson) {
            lastJson = blob;
            const percent = snapshot.bytesTotal ? snapshot.bytesDone / snapshot.bytesTotal : 0;
            seed.downloads = {
              current: {
                id: snapshot.jobId || "local-install",
                productId: 'java',
                intent: 'Install',
                jobId: snapshot.jobId,
                state: cancelled ? 'Cancelled' : snapshot.error ? "Errored" : snapshot.paused ? 'Paused' : snapshot.active ?
                  snapshot.controlReason && !snapshot.canPause && !snapshot.canResume ? 'Finalizing' : "Downloading" : snapshot.phase === 'done' || snapshot.isInstalled ? "Succeeded" : 'Errored',
                canPause: snapshot.canPause === true,
                canResume: snapshot.canResume === true,
                canCancel: snapshot.canCancel === true,
                controlStatus: snapshot.controlStatus,
                controlReason: snapshot.controlReason,
                phase: LOADING_PHASES[snapshot.phase] || snapshot.phase,
                progress: percent,
                currentBytes: snapshot.bytesDone,
                totalBytes: snapshot.bytesTotal,
              },
              pool: {},
            };
            seed.downloads.pool[seed.downloads.current.id] = seed.downloads.current;
            // The native DownloadsProvider follows gameInstances whose
            // downloadId names a live download; the downloads slice alone
            // does not make its progress or controls visible.
            const instances = (seed.gameInstances && seed.gameInstances.instances) || [];
            seed.gameInstances = {instances: instances.filter(item => !item.localInstaller)};
            if (snapshot.active) seed.gameInstances.instances.push({
              id: snapshot.jobId || 'local-install', productId: 'java',
              downloadId: snapshot.jobId || 'local-install', state: 'Downloading', localInstaller: true,
            });
            push('gameInstances');
            push("downloads");
            if (cancelled) return fail(hostError('INSTALL_CANCELLED', 'Installation cancelled.'));
            if (snapshot.error) return fail(hostError("FAILED_TO_INSTALL_PRODUCT", snapshot.error));
            if (!snapshot.active) {
              if (snapshot.phase === 'done' || snapshot.isInstalled) {
                markInstalled();
                return done(snapshot);
              }
              return fail(hostError('FAILED_TO_INSTALL_PRODUCT', 'Installation did not complete.'));
            }
          }
        }
        setTimeout(tick, 1200);
      };
      tick();
    });
  }

  // --- launching the game -------------------------------------------------
  // The native launcher spawns javaw.exe with the version's classpath, natives
  // and asset index. The harness server does exactly that (harness/launcher.py)
  // and streams the client's stdout back here, so Play really starts Minecraft
  // once the version is installed, and the log window shows real game output.
  const LOG_LINE_LIMIT = 4000;
  const logListeners = [];
  let logCursor = 0;
  let logPoller = null;
  let logTailGeneration = 0;
  let logTailPending = false;
  let launchedProcess = null;

  const deliverLog = (lines) => {
    if (!lines || !lines.length) return;
    for (const cb of logListeners.slice()) {
      try {
        lines.forEach((line) => cb(line));
      } catch (e) {
        console.error("[mock-host] log listener threw:", e);
      }
    }
  };

  const stopLogTail = () => {
    logTailGeneration++;
    if (logPoller) clearInterval(logPoller);
    logPoller = null;
    logTailPending = false;
  };

  const startLogTail = () => {
    if (logPoller) return;
    const generation = ++logTailGeneration;
    logPoller = setInterval(async () => {
      if (generation !== logTailGeneration || logTailPending) return;
      logTailPending = true;
      try {
        const res = await fetch("/__launch_log?since=" + logCursor, { cache: "no-store" });
        if (!res.ok || generation !== logTailGeneration) return;
        const doc = await res.json();
        if (generation !== logTailGeneration) return;
        if (Number.isFinite(doc.next) && doc.next < logCursor) return;
        if (Number.isFinite(doc.next)) logCursor = doc.next;
        deliverLog(doc.lines || []);
        if (!doc.running) {
          // One last drain already happened above; stop polling once the game
          // is gone so a closed game is not a busy loop.
          stopLogTail();
        }
      } catch (e) {
        /* a transient fetch failure must not kill the tail */
      } finally {
        if (generation === logTailGeneration) logTailPending = false;
      }
    }, 800);
  };

  async function launchProduct(productId, versionId, page, configurationId) {
    trace("launchProduct", productId, versionId, page, configurationId);
    if (productId !== "java") {
      throw hostError(
        "FAILED_TO_LAUNCH_PRODUCT",
        "This game is unavailable in this build. Use the official Minecraft Launcher to install and play it."
      );
    }
    let out = {};
    try {
      const res = await fetch("/__launch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          productId,
          versionId: versionId === "retail" ? undefined : versionId,
          configurationId: typeof configurationId === "string" ? configurationId : undefined,
        }),
      });
      out = await res.json().catch(() => ({}));
      if (!res.ok || out.error) {
        throw hostError("FAILED_TO_LAUNCH_PRODUCT", out.error || "launch failed (" + res.status + ")");
      }
    } catch (e) {
      if (e && e.errorCode) throw e;
      throw hostError("FAILED_TO_LAUNCH_PRODUCT", "launch service unreachable: " + e);
    }
    launchedProcess = out.pid || null;
    stopLogTail();
    logCursor = 0;
    startLogTail();
    if (out.quickPlayData) {
      seed.quickPlayData = out.quickPlayData;
      pushSlice("quickPlayData");
    }
    if (out.profiles) {
      seed.configurationData.profiles = out.profiles;
      pushSlice("configurationData");
    }
    return { ok: true, pid: out.pid || null };
  }

  async function launchQuickPlay(record, page) {
    const id = record && record.source === "Java" && record.javaInstance && record.javaInstance.configId;
    if (typeof id !== "string" || !id || record.javaInstance.game) {
      throw hostError("FAILED_TO_LAUNCH_PRODUCT", "This Quick Play entry cannot be launched here. Open the installation to choose a saved world or server.");
    }
    return launchProduct("java", undefined, page, id);
  }

  // Skin host calls. The UI reaches them from the ROOT namespace
  // (MCLauncher.getCapes etc.), so they are registered there and mirrored
  // under `skins.` for the modules that use the nested path. Lifecycle notes
  // from the app's own saga: saveSkin's RETURN VALUE becomes the stored skin
  // (it must carry an id), and activate replaces the active skin.
  const skinHostCalls = (() => {
    let pending = Promise.resolve();
    const serialize = (mutation) => {
      const result = pending.then(mutation);
      pending = result.catch(() => {});
      return result;
    };
    return {
    getCapes: async () => ({ capes: seed.skins.capes || [] }),
    getActiveSkin: async () => seed.skins.activeSkin || null,
    saveSkin: (skin) => serialize(async () => {
      if (!skin || typeof skin !== "object") {
        throw hostError("FAILED_TO_UPDATE_USERSKIN", "skin payload missing");
      }
      const id = skin.id || "custom-" + Date.now().toString(36);
      const full = Object.assign({}, skin, { id, created: skin.created || new Date().toISOString() });
      const customSkins = Object.assign({}, seed.skins.customSkins, { [id]: full });
      const out = await osRequest("/__skin_state", { customSkins });
      if (!out || !out.ok || out.error) throw hostError("FAILED_TO_UPDATE_USERSKIN", (out && out.error) || "Could not save skin.");
      seed.skins.customSkins = customSkins;
      pushSlice("skins");
      return full;
    }),
    activateSkin: (skin, profileId) => serialize(async () => {
      if (!skin || typeof skin !== "object") {
        throw hostError("FAILED_TO_UPDATE_USERSKIN", "skin payload missing");
      }
      const out = await osRequest("/__skin_state", { activeSkinId: skin.id || "" });
      if (!out || !out.ok || out.error) throw hostError("FAILED_TO_UPDATE_USERSKIN", (out && out.error) || "Could not apply skin.");
      seed.skins.activeSkin = skin;
      pushSlice("skins");
      return skin;
    }),
    deleteSkin: (id) => serialize(async () => {
      if (id && seed.skins.customSkins[id]) {
        const customSkins = Object.assign({}, seed.skins.customSkins);
        delete customSkins[id];
        const patch = { customSkins };
        // Deleting the skin that is in use has to clear the selection too, or
        // the Current panel points at a skin that no longer exists.
        if (seed.skins.activeSkin && seed.skins.activeSkin.id === id) {
          patch.activeSkinId = "steve";
        }
        const out = await osRequest("/__skin_state", patch);
        if (!out || !out.ok || out.error) throw hostError("FAILED_TO_UPDATE_USERSKIN", (out && out.error) || "Could not delete skin.");
        seed.skins.customSkins = customSkins;
        if (patch.activeSkinId) seed.skins.activeSkin = seed.skins.defaultSkins.steve;
        pushSlice("skins");
      }
      return true;
    }),
    };
  })();

  const diskSpace = async (kind, target) => {
    // The native install dialog matches message, rather than errorCode, to
    // highlight an unreadable location. Keep host diagnostics separately.
    const failure = (detail) => Object.assign(hostError("UNABLE_TO_OPEN_FILE", "UNABLE_TO_OPEN_FILE"), { detail });
    const query = typeof target === "string" && target
      ? "?" + kind + "=" + encodeURIComponent(target) : "";
    let res, out;
    try {
      res = await fetch("/__disk_space" + query, { cache: "no-store" });
      out = await res.json();
    } catch (error) {
      throw failure("Could not read available disk space: " + error.message);
    }
    if (!res.ok || !out || out.error || typeof out.value !== "number" ||
        !Number.isFinite(out.value) || out.value < 0) {
      throw failure((out && out.error) || "Available disk space could not be determined.");
    }
    return { value: out.value };
  };

  const selectVolume = async (name) => {
    const volumes = seed.volumes || {};
    const selected = volumes.selectedVolumeName ||
      ((volumes.volumes || []).find((volume) => volume.isDefault) || {}).name;
    if (!selected || name !== selected) {
      throw hostError("FAILED_TO_SET_VOLUME", "This launcher uses its configured Java game directory. Choose the directory in an installation's settings.");
    }
    return true;
  };

  const quickPlayRequest = (() => {
    let pending = Promise.resolve();
    return (path, payload) => {
      const result = pending.then(async () => {
        const out = await osRequest(path, payload);
        if (!out || !out.ok || out.error || !out.quickPlayData || !Array.isArray(out.quickPlayData.playRecords)) {
          throw hostError("FAILED_TO_UPDATE_QUICK_PLAY", (out && out.error) || "Could not save Quick Play changes.");
        }
        seed.quickPlayData = out.quickPlayData;
        pushSlice("quickPlayData");
        return out;
      });
      pending = result.catch(() => {});
      return result;
    };
  })();

  const chooseFile = async (options) => {
    const opts = options && typeof options === "object" ? options : {};
    const fail = (message) => {
      if (typeof opts.onError === "function") {
        opts.onError(message);
        return false;
      }
      throw hostError("UNABLE_TO_OPEN_FILE", message);
    };
    const out = await osRequest("/__dialog", { options: opts });
    if (!out || out.ok === false || out.error) {
      return fail((out && out.error) || "The file picker could not be opened.");
    }
    const chosen = typeof out.path === "string" ? out.path : "";
    if (typeof opts.onSuccess !== "function") return chosen;
    if (!chosen) return false;
    const file = await osRequest("/__read_file", { path: chosen });
    if (!file || file.ok === false || file.error || typeof file.base64 !== "string") {
      return fail((file && file.error) || "The selected file could not be read.");
    }
    try {
      await opts.onSuccess(file.name || chosen.split(/[\\/]/).pop(), file.base64, chosen);
    } catch (error) {
      return fail(error.message || "The selected file could not be applied.");
    }
    return true;
  };

  const common = namespace({
    // Free space drives the install dialog's "Required Disk Space" line and its
    // Install button. The UI reads `.value` off the result; a bare number makes
    // that read undefined and the line stays on a spinner forever.
    getAvailableSpace: (path) => diskSpace("path", path),
    getFranchiseServiceUri: async (service) => ({ value: serviceRoot(service) }),
    isMinecraftLauncherInstalled: async () => true,
    getStore: async (name) => {
      try {
        const res = await fetch(storeEndpoint(name), { cache: "no-store" });
        if (!res.ok) return { value: null };
        return applyEntryPage(name, await res.json());
      } catch (e) {
        console.error("[mock-host] getStore failed:", e);
        return { value: null };
      }
    },
    setStore: async (name, value) => {
      try {
        const res = await fetch(storeEndpoint(name), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: typeof value === "string" ? value : JSON.stringify(value) }),
        });
        return res.ok;
      } catch (e) {
        console.error("[mock-host] setStore failed:", e);
        return false;
      }
    },
    clearStore: async (name) => {
      try {
        const res = await fetch(storeEndpoint(name), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: null }),
        });
        return res.ok;
      } catch (e) {
        console.error("[mock-host] clearStore failed:", e);
        return false;
      }
    },
  }, "common");

  // --- slice pushes -------------------------------------------------------
  // launcher.dll tells the UI a slice changed by dispatching the event named in
  // modelInfo.modelEventMap. `push` is declared further down (it reads the event
  // map and needs `seed`), but the handlers below need it now, so this thin
  // forwarder keeps declaration order irrelevant.
  const pushSlice = (slice) => {
    const host = window.__host;
    return host && host.push ? host.push(slice) : false;
  };

  // --- launcher settings --------------------------------------------------
  // `launcher_settings.json` is the real launcher's own file. The capture is
  // read-only here, so writes land in the server overlay and are merged on the
  // way back out: a reload comes back with the user's choices applied, exactly
  // like the native launcher re-reading a file it rewrote.
  const saveLauncherSettings = (() => {
    let queue = Promise.resolve();
    return patch => {
      const submitted = {...patch};
      const task = queue.then(async () => {
        const out = await osRequest("/__launcher_settings", submitted);
        if (!out || !out.ok || out.error) throw hostError("FAILED_TO_SAVE_SETTINGS", (out && out.error) || "Could not save launcher settings.");
        seed.launcherSettings = Object.assign({}, seed.launcherSettings, submitted);
        pushSlice("launcherSettings");
        return true;
      });
      queue = task.catch(() => {});
      return task;
    };
  })();

  // --- installations ------------------------------------------------------
  // launcher_profiles.json is rewritten in place by the native launcher. Here
  // the server persists the change (the capture itself stays read-only) and
  // answers with the merged profile list; mirroring that into the slice and
  // firing the slice's event is what makes the UI re-read it -- the same lever
  // the host pulls after it writes the file.
  const configurationRequest = async (path, body) => {
    const out = await osRequest(path, body);
    if (!out || out.error || !out.ok) {
      throw hostError(
        "FAILED_TO_UPDATE_CONFIGURATION",
        (out && out.error) || "could not update installations"
      );
    }
    if (out.profiles) {
      if (!seed.configurationData) seed.configurationData = {};
      seed.configurationData.profiles = out.profiles;
      pushSlice("configurationData");
    }
    return out;
  };

  const saveConfigurationSettings = (() => {
    let queue = Promise.resolve();
    return settings => {
      const submitted = {...settings};
      const task = queue.then(async () => {
        const out = await osRequest("/__configuration_settings", submitted);
        if (!out || !out.ok || out.error) throw hostError("FAILED_TO_SAVE_SETTINGS", (out && out.error) || "Could not save installation settings.");
        if (seed.configurationData) seed.configurationData.settings = out.settings && typeof out.settings === "object"
          ? { ...out.settings } : { ...seed.configurationData.settings, ...submitted };
        pushSlice("configurationData");
        return true;
      });
      queue = task.catch(() => {});
      return task;
    };
  })();

  // Native helpers that need the operating system (open a folder, pick a file).
  // The harness server owns the machine, so these go through it rather than
  // pretending: a click on "Open launcher log" really opens Explorer.
  const osRequest = async (path, body) => {
    try {
      const res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
      const out = await res.json();
      return res.ok ? out : { ok: false, error: (out && out.error) || "Host request failed (HTTP " + res.status + ")." };
    } catch (e) {
      console.error("[mock-host] " + path + " failed:", e);
      return null;
    }
  };

  const profileHostCalls = (() => {
    const localAccount = () => {
      const active = activeRecord();
      if (!active || !(active.offline || active.type === "Offline")) {
        throw hostError("PROFILE_SERVICE_UNAVAILABLE", "Minecraft profile management is unavailable in this build for online accounts. Select an offline account to edit its local name.");
      }
      return active;
    };
    const profileNameAvailable = async (name) => {
      const active = localAccount();
      if (typeof name !== "string" || !/^[A-Za-z0-9_-]{3,16}$/.test(name)) return { status: "NOT_ALLOWED" };
      const taken = (accountState.accounts || []).some((account) => account.id !== active.id &&
        String(account.javaProfileName || account.username || "").toLowerCase() === name.toLowerCase());
      return { status: taken ? "DUPLICATE" : "AVAILABLE" };
    };
    const updateName = async (name) => {
      const active = localAccount();
      if ((await profileNameAvailable(name)).status !== "AVAILABLE") {
        throw hostError("INVALID_PROFILE_NAME", "Choose an available local profile name containing 3–16 letters, numbers, underscores or hyphens.");
      }
      await accountAction("rename", { id: active.id, name });
      return true;
    };
    return { profileNameAvailable, createMinecraftProfile: updateName, changeMinecraftProfileName: updateName };
  })();

  const handlers = namespace({
    common,
    // The app's facade binds `common.getStore` to `MCLauncher.getStore`, so the
    // store API has to exist at the root as well.
    getStore: common.getStore,
    setStore: common.setStore,
    clearStore: common.clearStore,
    refreshSystemInfo: async () => systemInfo,
    getSystemInfo: async () => systemInfo,
    // `getAccountData` resolves to the account slice's `data` payload: the UI
    // immediately indexes `accounts` and reads `activeAccount.id`.
    getAccountData: async () => accountsFromSeed(),
    refreshCurrencyBalance: async () => inertResult,
    getMCToken: async () => inertResult,
    getXboxXBLXToken: async () => inertResult,
    // The UI reads `.environment` and only switches to the testing content tree
    // when it equals "testing"; everything else is the prod tree, which is what
    // the local mirror holds.
    getDynamicResourceEnvironment: async () => ({ environment: "prod" }),
    // `common.getFranchiseServiceUri(service)` -> {value}. Returned roots are
    // origin-relative so every service call stays on this server.
    getFranchiseServiceUri: async (service) => ({ value: serviceRoot(service) }),
    ...profileHostCalls,
    // The sign-in panel reaches the host through the root object as well as
    // through `account`; both run the real Microsoft flow (harness/msa.py),
    // which fails with an honest message when no client id is configured.
    loginWithXbox: async () => {
      trace("loginWithXbox");
      return microsoftSignIn();
    },
    switchAccount: async (id) => {
      trace("switchAccount");
      await accountAction("switch", { id });
      return activeRecord();
    },
    logoutAccount: async (id) => {
      trace("logoutAccount");
      await accountAction("logout", { id });
      return true;
    },
    // The UI's configuration facade sends the launcher-format profile and
    // expects the saved profile (with its id) back.
    createConfiguration: async (configuration) => {
      trace("createConfiguration");
      // The form's mod-loader row (harness/ui.js) materializes the chosen
      // loader before this runs; the saved profile points at that loader's
      // version document instead of the vanilla version the form held. One
      // shot: a later vanilla create is left alone.
      const armed = window.__hxArmedLoader;
      if (armed && armed.versionId) {
        window.__hxArmedLoader = null;
        configuration = { ...configuration, lastVersionId: armed.versionId };
      }
      const out = await configurationRequest("/__configurations/create", { configuration });
      return out.configuration;
    },
    // The Edit installation form's mod-loader row (harness/ui.js) materializes
    // a changed loader before this runs; the patch then points at that
    // loader's version document instead of the vanilla or loader id the form
    // held. The armed id stays armed for the dialog session, not one shot: the
    // app's update flow reaches the host more than once for a single Save, and
    // every call must carry the materialized id. The gate re-arms on any later
    // change of pick, and a dialog reset drops it.
    updateConfiguration: (id, patch) => {
      const armed = window.__hxArmedLoader;
      if (armed && armed.versionId && patch && typeof patch.lastVersionId === "string") {
        patch = { ...patch, lastVersionId: armed.versionId };
      }
      return configurationRequest("/__configurations/update", { id, patch });
    },
    deleteConfiguration: (id) => configurationRequest("/__configurations/delete", { id }),
    updateConfigurationData: async (settings) => saveConfigurationSettings(settings || {}),
    cleanUnusedSecureTokens: async () => true,
    updateForcedMigrationStates: async () => true,
    // `{value: bytes}`: the install dialog formats `result.value` and compares
    // it with the install plan; a bare number leaves both unresolved.
    getAvailableSpace: common.getAvailableSpace,
    // The install-location picker asks the host for free space per drive and
    // for the volume list (served from appData); these back the settings UI.
    getAvailableVolumeSpace: (name) => diskSpace("volume", name),
    setVolume: selectVolume,
    // Telemetry is fire-and-forget in the real launcher too; resolving keeps
    // the UI's await-chains quiet without pretending to reach a service.
    telemetryEventLegacy: async () => true,
    telemetryEventUiAction: async () => true,
    reportLatency: async () => true,
    // No OneDesk key ships with this build; an empty string is what the UI
    // checks before initialising analytics.
    getOneDsInstrumentationKey: async () => "",
    // GameCore (MS Store) installs do not exist on this build; the Bedrock
    // page's "cannot be launched" notice reads this alongside appData.
    // The settings saga reads `.installState` off the result — returning a
    // bare null throws inside it and surfaces the
    // "Unable to determine new Launcher install status." toast on General.
    gamecoreLauncherInstallState: async () => ({ installState: null }),
    installProduct: async (productId, versionId, isTrial, page, configurationId) => {
      trace("installProduct", productId, versionId, isTrial, page, configurationId);
      if (productId !== "java") {
        // Bedrock and the Dungeons/Legends families install through the
        // Microsoft Store or GameCore on the real launcher; this build reports
        // that with its own error code rather than pretending.
        throw hostError("FAILED_TO_INSTALL_PRODUCT", "This game is unavailable in this build. Use the official Minecraft Launcher to install and play it.");
      }
      let first;
      try {
        const res = await fetch("/__install", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            productId,
            versionId: versionId === "retail" ? undefined : versionId,
            configurationId: typeof configurationId === "string" ? configurationId : undefined,
          }),
        });
        first = await res.json();
      } catch (e) {
        throw hostError("FAILED_TO_INSTALL_PRODUCT", "install service unreachable: " + e);
      }
      if (first && first.error) {
        throw hostError("FAILED_TO_INSTALL_PRODUCT", first.error);
      }
      await trackDownload(first && first.jobId);
      return { ok: true };
    },
    // Download progress is polled from the installer and mirrored into the
    // downloads slice; these two answer the UI's progress surfaces.
    getActiveDownloads: () => (seed.downloads && seed.downloads.current ? [seed.downloads.current] : []),
    getDownloadData: () => (seed.downloads && seed.downloads.current) || null,
    getMobVotes: async () => inertResult,
    setMobVotes: async () => true,
    purchaseStoreProduct: async () => { throw hostError("PURCHASE_UNAVAILABLE", "Purchases are unavailable in this build. Use the official Minecraft Launcher or Microsoft Store."); },
    appDataForceRefresh: async () => true,
    // `setLogListener(kind, callback)`: launcher.dll pushes game/log lines to the
    // callback. Backed by the launch log tail so the UI's log window shows the
    // running client's real output.
    setLogListener: async (kind, callback) => {
      const cb = typeof kind === "function" ? kind : callback;
      if (typeof cb !== "function") return false;
      if (!logListeners.includes(cb)) logListeners.push(cb);
      try {
        const res = await fetch("/__launch_status", { cache: "no-store" });
        const doc = await res.json();
        if (doc && doc.running) startLogTail();
        else if (Array.isArray(doc.lines) && doc.lines.length) deliverLog(doc.lines);
      } catch (e) {
        /* no launch yet */
      }
      return true;
    },
    removeLogListener: async (kind, callback) => {
      const cb = typeof kind === "function" ? kind : callback;
      const at = logListeners.indexOf(cb);
      if (at >= 0) logListeners.splice(at, 1);
      if (!logListeners.length) stopLogTail();
      return true;
    },
    // The real host writes launcher_settings.json and reboots the UI into the
    // chosen language; here the locale is persisted and applied on reload.
    setLocale: async (locale) => {
      await common.setStore("__locale", locale);
      return saveLauncherSettings({ locale });
    },
    setChannel: async (channel) => saveLauncherSettings({ channel }),
    setDisableGPU: async (disabled) => saveLauncherSettings({ disableGPU: Boolean(disabled) }),
    setUseArm64Jre: async (on) => saveLauncherSettings({ useArm64JreIfSupported: Boolean(on) }),
    setUseMagnifiedMode: async (on) => saveLauncherSettings({ useMagnifiedMode: Boolean(on) }),
    setQuickPlayEnabled: async (on) => saveLauncherSettings({ quickPlayEnabled: Boolean(on) }),
    setProductLibraryDir: async (dir) => {
      if (!dir) return false;
      await saveLauncherSettings({ productLibraryDir: String(dir) });
      await osRequest("/__settings/product_library_dir", { path: String(dir) });
      return true;
    },
    setAutoPlayVideoDisabled: async (on) => saveLauncherSettings({ autoPlayVideoDisabled: Boolean(on) }),
    setInputMethod: async () => true,
    setIsHandheld: async () => true,
    setOsName: async () => true,
    setPlatformName: async () => true,
    setOsVersion: async () => true,
    // The review dialog is the Windows Store's "rate this app"; there is no
    // package identity here to review, so it reports a dismissed dialog (the
    // same shape a user pressing Escape produces) instead of hanging.
    openPlatformReviewDialog: async () => ({ aborted: true, updateResult: false, resultCode: 0 }),
    launchGamecoreLauncher: async () => {
      throw hostError("GAMECORE_LAUNCHER_UNAVAILABLE", "The Microsoft Store launcher is not installed.");
    },
    // Native shell helpers. The real host can open folders and files and show a
    // picker; the harness server does the same on this machine.
    openDirectory: async (target) => {
      const out = await osRequest("/__open", { path: target, kind: "directory" });
      return Boolean(out && out.ok);
    },
    openFile: async (target) => {
      const out = await osRequest("/__open", { path: target, kind: "file" });
      return Boolean(out && out.ok);
    },
    // `openFileDialog` has two contracts in this UI. The skin editor passes
    // `{onSuccess(name, base64), mode: "read_file", filters: [".png"]}` and
    // expects the HOST to read the chosen file and hand the bytes back; the
    // settings pages just want the path. Both are honoured here, which is what
    // makes "Browse" for a skin file actually load the PNG.
    openFileDialog: chooseFile,
    openMinecraftLauncher: async () => true,
    clearInstance: async () => true,
    showVirtualControllerKeyboard: async () => true,
    sendCrashReportFromDialog: async () => true,
    crashReportNotSent: async () => true,
    sentryUILog: async () => true,
    sentryBreadcrumbUILog: async () => true,
    telemetryEventPatchInstall: async () => true,
    telemetryEventPurchaseFlow: async () => true,
    updateLatentTelemetryData: async () => true,
    updatePreferences: async (prefs) => {
      seed.preferencesData = Object.assign({}, seed.preferencesData, prefs || {});
      pushSlice("preferencesData");
      return true;
    },
    checkForUpdates: async () => window.launcherDesktop ? window.launcherDesktop.checkForUpdates() : {updateAvailable: false, status: 'setup'},
    cancelDownload: async () => {
      if (!await installControl('cancel')) throw hostError('INSTALL_CONTROL_FAILED', 'Unable to cancel this download. Refresh its status and try again.');
      return true;
    },
    resumeDownload: async () => {
      if (!await installControl('resume')) throw hostError('INSTALL_CONTROL_FAILED', 'Unable to resume this download. Refresh its status and try again.');
      return true;
    },
    pauseDownload: async () => {
      if (!await installControl('pause')) throw hostError('INSTALL_CONTROL_FAILED', 'Unable to pause this download. Refresh its status and try again.');
      return true;
    },
    // The UI asks the host for the full version document before it plans an
    // install; the capture already carries it, so read it from appData.
    requestFullVersionInfo: async (versionId) => {
      const versions = (seed.gameVersions && seed.gameVersions.versions) || {};
      const key = versionId && versions[versionId] ? versionId : null;
      if (!key) return inertResult;
      return versions[key];
    },
    getDefaultJvmArguments: async () => (seed.constants && seed.constants.defaultJVMArguments) || "",
    resetRealmsData: async () => true,
    getRealmsInvites: async () => [],
    getRealmsActivePlayerCounts: async () => ({}),
    getRealmsWorlds: async () => ({ servers: [] }),
    getRealmsWorldData: async () => ({}),
    respondToRealmsInvite: async () => { throw hostError("REALMS_UNAVAILABLE", "Realms is unavailable in this build. Use the official Minecraft Launcher."); },
    leaveRealm: async () => { throw hostError("REALMS_UNAVAILABLE", "Realms is unavailable in this build. Use the official Minecraft Launcher."); },
    getRealmsTrialEligibility: async () => false,
    redeemRealmsSubscription: async () => { throw hostError("REALMS_UNAVAILABLE", "Realms is unavailable in this build. Use the official Minecraft Launcher."); },

    // --- namespaces -------------------------------------------------------
    // These mirror launcher.dll's own facade (bundle 2839 maps each member to a
    // native call). Without them the inert auto-responder answers with a proxy,
    // which is what made pages render their empty state instead of their data.
    user: namespace({
      getUsers: async () => (accountState.accounts || []).slice(),
      getCurrentUser: async () => activeRecord(),
      setCurrentUser: async (id) => {
        await accountAction("switch", { id });
        return activeRecord();
      },
      setProfile: async (profile) => {
        const active = activeRecord();
        if (active && profile && profile.name) {
          await accountAction("rename", { id: active.id, name: profile.name });
        }
        return true;
      },
      login: async () => {
        await accountAction("login");
        return activeRecord();
      },
      logout: async (id) => {
        await accountAction("logout", { id });
        return true;
      },
      reconnect: async () => activeRecord(),
      validateSecurityQuestions: async () => true,
      getSecurityQuestions: async () => [],
    }, "user"),

    configurations: namespace({
      createConfiguration: (configuration) => handlers.createConfiguration(configuration),
      updateConfiguration: (id, patch) => handlers.updateConfiguration(id, patch),
      deleteConfiguration: (id) => handlers.deleteConfiguration(id),
      getConfigurations: () => {
        const profiles = (seed.configurationData && seed.configurationData.profiles) || {};
        return Object.keys(profiles).map((id) => ({ id, ...profiles[id] }));
      },
    }, "configurations"),

    versions: namespace({
      getVersions: () => (seed.gameVersions && seed.gameVersions.versions) || {},
      getVersionDetails: async (versionId) => {
        const versions = (seed.gameVersions && seed.gameVersions.versions) || {};
        return versions[versionId] || inertResult;
      },
      getDefaultJvmArguments: async () => (seed.constants && seed.constants.defaultJVMArguments) || "",
    }, "versions"),

    realms: namespace({
      getRealmsData: () => seed.realmsData || { java: { servers: [] }, bedrock: { servers: [] } },
      resetRealmsData: async () => true,
      getRealmsInvites: async () => [],
      getRealmsActivePlayerCounts: async () => ({}),
      getRealmsWorlds: async () => ({ servers: [] }),
      getRealmsWorldData: async () => ({}),
      respondToRealmsInvite: async () => { throw hostError("REALMS_UNAVAILABLE", "Realms is unavailable in this build. Use the official Minecraft Launcher."); },
      leaveRealm: async () => { throw hostError("REALMS_UNAVAILABLE", "Realms is unavailable in this build. Use the official Minecraft Launcher."); },
      getRealmsTrialEligibility: async () => false,
      redeemRealmsSubscription: async () => { throw hostError("REALMS_UNAVAILABLE", "Realms is unavailable in this build. Use the official Minecraft Launcher."); },
    }, "realms"),

    credits: namespace({
      getCoreCredits: () => ((seed.credits && seed.credits.libraries) || []).slice(),
    }, "credits"),

    products: namespace({
      getProducts: () => ({ ...((seed.productInfo && seed.productInfo.products) || {}) }),
      getProductIds: () => Object.keys((seed.productInfo && seed.productInfo.products) || {}),
      getProductData: (id) => ({ ...(((seed.productInfo && seed.productInfo.products) || {})[id] || {}) }),
      download: (id, versionId, isTrial, page, configurationId) =>
        handlers.installProduct(id, versionId, isTrial, page, configurationId),
      play: (id, versionId, page, configurationId) =>
        launchProduct(id, versionId, page, configurationId),
      uninstall: async (id, versionId, page, removeSavedData) => {
        const out = await osRequest("/__uninstall", { productId: id, removeSavedData: Boolean(removeSavedData) });
        if (!out || out.error) {
          throw hostError("FAILED_TO_UNINSTALL_PRODUCT", (out && out.error) || "uninstall failed");
        }
        try {
          const entry = seed.productInfo.products[id].versionData[0];
          entry.isInstalled = false;
          entry.isPlayableOffline = false;
          pushSlice("productInfo");
        } catch (e) {
          /* slice shape changed; the next reload re-reads the filesystem */
        }
        return { ok: true };
      },
      hardRepair: (id, versionId, page) => handlers.installProduct(id, versionId, false, page),
      updateProductInfo: async () => {
        pushSlice("productInfo");
        return true;
      },
      purchase: async () => {
        throw hostError("PURCHASE_UNAVAILABLE", "Purchases are unavailable in this build. Use the official Minecraft Launcher or Microsoft Store.");
      },
      isInAppGiftingSupported: async () => false,
      gift: async () => {
        throw hostError("PURCHASE_UNAVAILABLE", "Gifting is unavailable in this build. Use the Microsoft Store.");
      },
      refreshStorePrices: async () => true,
      getStorePricing: async () => inertResult,
      quickPlay: async (record, page) => launchQuickPlay(record, page),
      deleteQuickPlayInstance: async (id) => {
        await quickPlayRequest("/__quickplay/delete", { id });
        return true;
      },
      updateQuickPlayInstance: async (record) => {
        const out = await quickPlayRequest("/__quickplay/update", { record });
        return out.record;
      },
    }, "products"),

    settings: namespace({
      getSettings: () => ({
        ...((seed.configurationData && seed.configurationData.settings) || {}),
      }),
      updateSettings: async (settings) => saveConfigurationSettings(settings || {}),
    }, "settings"),

    launcherSettings: namespace({
      getLauncherSettings: () => ({ ...(seed.launcherSettings || {}) }),
      setLocale: async (locale) => handlers.setLocale(locale),
      setChannel: async (channel) => handlers.setChannel(channel),
      setUseHardwareRendering: async (on) => handlers.setDisableGPU(!on),
      getDynamicResourceEnvironment: async () => ({ environment: "prod" }),
      setUseArm64Jre: async (on) => handlers.setUseArm64Jre(on),
      setUseMagnifiedMode: async (on) => handlers.setUseMagnifiedMode(on),
      setQuickPlayEnabled: async (on) => handlers.setQuickPlayEnabled(on),
      setProductLibraryDir: async (dir) => handlers.setProductLibraryDir(dir),
      openPlatformReviewDialog: async () => handlers.openPlatformReviewDialog(),
      setAutoPlayVideoDisabled: async (on) => handlers.setAutoPlayVideoDisabled(on),
    }, "launcherSettings"),

    volumes: namespace({
      getVolumes: () => ((seed.volumes && seed.volumes.volumes) || []).slice(),
      getSelectedVolume: () => {
        const slice = seed.volumes || {};
        return (slice.volumes || []).find((v) => v.name === slice.selectedVolumeName) || null;
      },
      getDefaultVolume: () => ((seed.volumes && seed.volumes.volumes) || []).find((v) => v.isDefault) || null,
      setVolume: selectVolume,
      getAvailableVolumeSpace: (name) => diskSpace("volume", name),
    }, "volumes"),

    gamecore: namespace({
      // No Microsoft Store client on this build: `installState` stays null, which
      // is what the launcher reports for a standalone install.
      getState: async () => ({ installState: null }),
      openGamecoreLauncher: async () => {
        throw hostError("GAMECORE_LAUNCHER_UNAVAILABLE", "The Microsoft Store launcher is not installed.");
      },
    }, "gamecore"),

    msiPatch: namespace({
      getMSIPatch: () => ({ ...(seed.msiPatch || {}) }),
      installMSIPatch: async () => false,
    }, "msiPatch"),

    treatmentTags: namespace({
      getTreatmentTags: () => ({ ...(seed.treatmentTags || {}) }),
    }, "treatmentTags"),

    preferences: namespace({
      getPreferences: () => ({ ...(seed.preferencesData || {}) }),
      updatePreferences: async (prefs) => handlers.updatePreferences(prefs),
    }, "preferences"),

    quickPlay: namespace({
      getQuickPlayData: () => seed.quickPlayData || { playRecords: [], playRecordSourceMap: {} },
      deleteAllQuickPlayInstances: async () => {
        await quickPlayRequest("/__quickplay/delete", { all: true });
        return true;
      },
    }, "quickPlay"),

    connection: namespace({
      getConnectionStatus: () =>
        ({ ...(seed.connectionStatus || { isOnline: true, substatus: {} }) }),
    }, "connection"),

    telemetry: namespace({}, "telemetry"),

    // The UI reaches accounts through `MCLauncher.account`, not the root.
    account: namespace({
      // The refresh reducer expects `{data, isInitial}` and resolves
      // `data.activeAccount` (an id) against `data.accounts` itself.
      getOrRefreshAccountData: async () => {
        trace("account.getOrRefreshAccountData");
        return { data: await refreshAccounts(), isInitial: true };
      },
      // The "Sign in with Microsoft" screen calls this. It is a real online
      // sign-in (harness/msa.py); offline accounts come through the harness'
      // own Add offline account screen instead.
      loginWithXbox: async () => {
        trace("account.loginWithXbox");
        return microsoftSignIn();
      },
      switchAccount: async (id) => {
        trace("account.switchAccount");
        await accountAction("switch", { id });
        return activeRecord();
      },
      logout: async (id) => {
        trace("account.logout");
        await accountAction("logout", { id });
        return true;
      },
      ...profileHostCalls,
      cleanUnusedSecureTokens: async () => true,
      updateForcedMigrationStates: async () => true,
      refreshCurrencyBalance: async () => ({ value: 0 }),
      getMCToken: async () => ({ value: "" }),
      getXboxXBLXToken: async () => ({ value: "" }),
    }, "account"),

    // Skin storage. `getSkins` is served from appData (the renderer reads it
    // synchronously), but capes and the active skin come through the host.
    // The UI calls these off the ROOT namespace (MCLauncher.getCapes et al.),
    // not off `skins.` — registered both ways or the inert handler swallows
    // them and the Skins page's Current panel spins forever.
    skins: namespace({
      ...skinHostCalls,
    }, "skins"),

    ...skinHostCalls,
  }, "MCLauncher");

  // The host also publishes its error class on the bridge: `new MCLauncher.Error(code, msg)`.
  class LauncherError extends Error {
    constructor(code, message, options) {
      super(message || (typeof code === "string" ? code : "launcher error"));
      this.name = "LauncherError";
      this.code = code;
      // The UI's error mapper reads `errorCode` (falling back to `message`),
      // so native-style errors have to carry the code under that name too.
      this.errorCode = code;
      if (options && typeof options === "object") Object.assign(this, options);
    }
  }
  handlers.Error = LauncherError;

  // The app's facade (bundle 2839) builds its `Ay` object at module load from
  // `window.MCLauncher.<name>` and keeps those references forever. A member
  // that only exists under a namespace (`products.play`) is therefore invisible
  // to it: clicking Play called `undefined` and the click did nothing. Mirror
  // every facade member at the root, pointing at the same implementation.
  const FACADE_ALIASES = {
    launchProduct: "products.play",
    removeProduct: "products.uninstall",
    repairProduct: "products.hardRepair",
    updateProductInfo: "products.updateProductInfo",
    purchaseProduct: "products.purchase",
    isInAppGiftingSupported: "products.isInAppGiftingSupported",
    giftProduct: "products.gift",
    refreshStorePrices: "products.refreshStorePrices",
    getStorePricing: "products.getStorePricing",
    quickPlay: "products.quickPlay",
    deleteQuickPlayInstance: "products.deleteQuickPlayInstance",
    updateQuickPlayInstance: "products.updateQuickPlayInstance",
    deleteAllQuickPlayInstances: "quickPlay.deleteAllQuickPlayInstances",
    installMSIPatch: "msiPatch.installMSIPatch",
    setSelectedUser: "user.setCurrentUser",
    setSelectedUserProfile: "user.setProfile",
    login: "user.login",
    logout: "user.logout",
    reconnect: "user.reconnect",
    validateSecurityQuestions: "user.validateSecurityQuestions",
    getSecurityQuestions: "user.getSecurityQuestions",
  };
  for (const [rootName, path] of Object.entries(FACADE_ALIASES)) {
    const [space, member] = path.split(".");
    const existing = handlers[rootName];
    const alias = (...args) => handlers[space][member](...args);
    // Quick Play is both a callable root action and a namespace in the native
    // facade. Keep its data/delete members when installing the action alias.
    if (existing && typeof existing === "object") Object.assign(alias, existing);
    handlers[rootName] = alias;
  }
  // Small pure helpers the facade expects the host to provide.
  handlers.runLauncher = async () => true;
  handlers.updateAvailable = async () => false;
  handlers.getInstances = () => ((seed.gameInstances && seed.gameInstances.instances) || []).slice();
  handlers.createError = (code, message) => hostError(code, message);
  handlers.capitalizeWords = (text) =>
    String(text || "").replace(/\b\w/g, (c) => c.toUpperCase());

  window.MCLauncher = handlers;

  // The host injects a second object for content/telemetry fetch actions.
  //
  // IMPORTANT: this must stay empty. The UI's own module (99500) installs the
  // real implementation into `window.internalLauncherActions` and then spreads
  // whatever was already there over the top, so defining stubs here would
  // silently replace the working content fetchers with empty arrays.
  window.internalLauncherActions = window.internalLauncherActions || {};

  // --- native pushes -----------------------------------------------------
  // launcher.dll broadcasts a slice change by dispatching the event named in
  // modelInfo.modelEventMap; the UI's listeners then re-read appData. Expose
  // the same lever so the harness (or the console) can simulate a host push.
  const eventNames = (seed.modelInfo && seed.modelInfo.modelEventMap) || {};
  const push = (slice) => {
    const name = eventNames[slice];
    if (!name) {
      console.warn("[mock-host] no event name for slice:", slice);
      return false;
    }
    window.dispatchEvent(new CustomEvent(name, { detail: seed[slice] }));
    return true;
  };
  window.__host = { push, appData: seed, eventNames, netlog: NETLOG };
  // The initial state is already in appData, but the connection slice mounts
  // before it is read in some entry points; a single push settles it.
  setTimeout(() => push("connectionStatus"), 0);

  window.cefQuery = window.cefQuery || function (opts) {
    if (opts && typeof opts.onSuccess === "function") opts.onSuccess("{}");
  };

  // --- surface the errors the app normally swallows ----------------------
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    console.error("[mock-host] unhandled rejection:", (r && (r.stack || r.message)) || r);
  });
  window.addEventListener("error", (e) => {
    console.error("[mock-host] window error:", e.message);
  });

  LOG("appData + MCLauncher shim installed");
})();
