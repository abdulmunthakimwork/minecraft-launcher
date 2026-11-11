// The launcher's own screen for the things this build has no UI for.
//
// The UI is a compiled React bundle, so nothing here reaches into its tree:
// the entry point is added beside the app's own controls in the same place the
// app would put it, and the screen is built from the launcher's measured design
// tokens (dialog surface, title, field, footer buttons) so it reads as one of
// the app's own dialogs. Nothing is restyled and no component is patched.
//
//   /games/java/skins   ->  the native New skin editor, with cape upload inside it
//   /settings/accounts  ->  a matching offline-account action
//   /games/java/configurations -> Modpack import/export, built from the page's
//                                 own toolbar button, search field and rows
//  // Screens follow the launcher's own modal pattern: a full-window dark surface,

// a centred title with a close button top-right, a 550 px content column, and
// the buttons in a footer bar bottom-right.
(function () {
  "use strict";

  if (window.__harnessUI) return;
  window.__harnessUI = true;

  // Route the open screen was created on. The router is in-page and leaves the
  // URL alone, so a screen is also dismissed when a navigation link is pressed
  // (below) -- this only catches a real page load.
  var openedOn = null;
  var screenFocus = null;
  var screenBackground = [];
  var screenGeneration = 0;

  // --- design tokens (measured from the app's "Add new skin" dialog) -------
  var STYLE = [
    "#__hx-screen{position:fixed;inset:0;z-index:3000;display:flex;flex-direction:column;",
    'background:#171615;color:#fff;font-family:"Noto Sans","Helvetica Neue",Helvetica,Arial,sans-serif;}',
    "#__hx-screen .__hx-head{position:relative;flex:0 0 auto;height:60px;display:flex;align-items:center;",
    "justify-content:center;border-bottom:1px solid rgba(65,65,65,.4);}",
    "#__hx-screen h2{margin:0;font-size:18px;line-height:20px;font-weight:700;color:#fff;}",
    "#__hx-screen .__hx-close{position:absolute;right:12px;top:11px;width:36px;height:36px;display:grid;",
    "place-items:center;background:transparent;border:0;color:#fff;cursor:pointer;padding:0;}",
    "#__hx-screen .__hx-close:hover{background:rgba(255,255,255,.08);}",
    "#__hx-screen .__hx-body{flex:1 1 auto;overflow:auto;display:flex;justify-content:center;",
    "align-items:flex-start;padding:40px 20px;}",
    "#__hx-screen .__hx-inner{width:550px;max-width:100%;min-width:0;}",
    "#__hx-screen .__hx-row{margin-bottom:24px;}",
    "#__hx-screen label{display:block;font-size:12px;line-height:16px;font-weight:700;margin-bottom:4px;",
    "text-transform:uppercase;}",
    // The modpack search field is a clone of the launcher's own field and
    // keeps the app's styles -- icon clearance (32px), colours, focus ring --
    // so it is excluded from the generic screen-input skin below; without
    // the exclusion this ID-level rule wins over the app's classes and the
    // magnifier lands on top of the placeholder.
    "#__hx-screen input[type=text]:not(#__hx-modpack-search),#__hx-screen input[type=password]{width:100%;box-sizing:border-box;",
    "height:34px;padding:7px 10px;background:#262423;border:2px solid #86d562;border-radius:3px;",
    "color:#fff;font:12px/16px Arial;outline:none;}",
    // Native text field (module 40266): green edge at rest, lighter surface
    // on hover, white edge/ring on focus. Keep cloned search fields untouched.
    "#__hx-screen input[type=text]:not(#__hx-modpack-search):hover,#__hx-screen input[type=password]:hover{background:#3d3938;}",
    "#__hx-screen input[type=text]:not(#__hx-modpack-search):focus,#__hx-screen input[type=password]:focus{border-color:#fff;",
    "background:#3d3938;box-shadow:0 0 0 1px rgba(255,255,255,.7);}",
    "#__hx-screen input[type=text]:not(#__hx-modpack-search)::placeholder,#__hx-screen input[type=password]::placeholder{color:rgba(255,255,255,.5);}",
    "#__hx-screen input[type=text]:not(#__hx-modpack-search):focus::placeholder,#__hx-screen input[type=password]:focus::placeholder{color:rgba(255,255,255,.4);}",
    "#__hx-screen .__hx-note{margin:0 0 20px;font-size:12px;line-height:18px;color:#a7a7a7;overflow-wrap:anywhere;}",
    "#__hx-screen .__hx-error{min-height:16px;margin:-12px 0 16px;font-size:12px;color:#ff6b5b;overflow-wrap:anywhere;}",
    "#__hx-screen .__hx-foot{flex:0 0 auto;height:72px;display:flex;align-items:center;",
    "justify-content:flex-end;padding:0 20px;border-top:1px solid rgba(65,65,65,.4);}",
    // The launcher's own button, read rule for rule out of the app's runtime
    // stylesheet (the atomic classes on the real "Cancel" / "Save & Use"):
    // a 32px slab with a 6px darker band inset along the bottom, a ::before
    // bevel that paints a 2px light edge on the top-left and a softer one on
    // the bottom-right over the slab only, and a label lifted 3px above
    // centre. That bevel plus the lift is what makes the button look raised --
    // the bottom band alone reads flat. Hover lightens the secondary slab (and
    // darkens the green one). Pressing is the click animation: the face grows
    // from 100% - 6px to 100% - 2px so it drops onto the band, the label falls
    // from -3px to -1px, the band collapses to 2px and the slab darkens. Focus
    // is the app's own :focus (not :focus-visible): a 2px white outline plus a
    // white/blue ring, dropped to a white 3px ring while still held down. None
    // of it eases -- the real button reports transition "all" at 0s.
    "#__hx-screen .__hx-btn{position:relative;overflow:hidden;display:flex;align-items:center;",
    "justify-content:center;box-sizing:border-box;height:32px;padding:6px 18px;",
    "border-style:solid;border-width:0;border-color:#000;border-radius:0;",
    "background:#d0c5c0;color:#262423;font:700 12px/16px Arial;",
    "white-space:nowrap;text-overflow:ellipsis;cursor:pointer;outline:none;",
    "box-shadow:inset 0 -6px 0 #aba09c;}",
    "#__hx-screen .__hx-btn::before{content:'';position:absolute;top:0;left:0;width:100%;",
    "height:calc(100% - 6px);z-index:1;display:block;box-shadow:0 0 transparent,",
    "inset 2px 2px 0 rgba(255,255,255,.6),inset -2px -2px 0 rgba(255,255,255,.4);}",
    "#__hx-screen .__hx-btn>span{position:relative;z-index:2;display:inline-flex;align-items:center;",
    "justify-content:center;transform:translateY(-3px);}",
    "#__hx-screen .__hx-btn:hover{background:linear-gradient(0deg,rgba(255,255,255,.2),rgba(255,255,255,.2)),#d0c5c0;}",
    "#__hx-screen .__hx-btn:active{background:#aba09c;box-shadow:inset 0 -2px 0 #aba09c;}",
    // The press itself: the face slides down over the band and the label drops.
    "#__hx-screen .__hx-btn:active::before{height:calc(100% - 2px);}",
    "#__hx-screen .__hx-btn:active>span{transform:translateY(-1px);}",
    "#__hx-screen .__hx-btn:focus{outline:2px solid #fff;outline-offset:0;border-radius:0;",
    "box-shadow:0 0 0 1px #fff,0 0 0 3px #1e6eea,inset 0 -6px 0 #aba09c;}",
    // The app clears the border colour of a focused secondary button (and keeps
    // it on the green one), measured off the real Cancel / Save & Use.
    "#__hx-screen .__hx-btn:focus:not(.__hx-primary){border-color:transparent;}",
    "#__hx-screen .__hx-btn:active:focus{box-shadow:0 0 0 3px #fff,inset 0 -2px 0 #aba09c;}",
    "#__hx-screen .__hx-btn.__hx-primary{background:#3c8527;color:#fff;padding:6px 20px;",
    "box-shadow:inset 0 -6px 0 #2a641c;}",
    "#__hx-screen .__hx-btn.__hx-primary::before{box-shadow:0 0 transparent,",
    "inset 2px 2px 0 rgba(255,255,255,.2),inset -2px -2px 0 rgba(255,255,255,.1);}",
    "#__hx-screen .__hx-btn.__hx-primary:hover{background:linear-gradient(0deg,rgba(0,0,0,.1),rgba(0,0,0,.1)),#3c8527;}",
    "#__hx-screen .__hx-btn.__hx-primary:active{background:#2a641c;box-shadow:inset 0 -2px 0 #2a641c;}",
    "#__hx-screen .__hx-btn.__hx-primary:focus{outline:2px solid #fff;outline-offset:0;",
    "box-shadow:0 0 0 1px #fff,0 0 0 3px #1e6eea,inset 0 -6px 0 #2a641c;}",
    "#__hx-screen .__hx-btn.__hx-primary:active:focus{box-shadow:0 0 0 3px #fff,inset 0 -2px 0 #2a641c;}",
    // Disabled: the btn1 buttons define no disabled state in this build, so
    // this is the launcher's own disabled button recipe (the theme vars behind
    // it resolve to exactly these values) -- pale slab, no band, grey label,
    // grey outline -- rather than something invented.
    "#__hx-screen .__hx-btn[disabled]{background:#ede5e2;color:#aba09c;",
    "box-shadow:none;outline:2px solid #aba09c;outline-offset:0;cursor:default;}",
    "#__hx-screen .__hx-btn[disabled]::before{box-shadow:none;}",
    "#__hx-screen .__hx-btn[disabled]:hover,",
    "#__hx-screen .__hx-btn[disabled]:active{background:#ede5e2;box-shadow:none;}",
    "#__hx-screen .__hx-btn+.__hx-btn{margin-left:7px;}",
    "#__hx-screen .__hx-btn[hidden]{display:none;}",
    "#__hx-screen .__hx-foot{height:auto;min-height:72px;box-sizing:border-box;flex-wrap:wrap;gap:8px;padding:16px 20px;}",
    "#__hx-screen .__hx-job-controls{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 12px;}",
    "#__hx-screen .__hx-job-controls[hidden]{display:none;}",
    "#__hx-screen .__hx-job-controls .__hx-btn+.__hx-btn{margin-left:0;}",
    "#__hx-screen a.__hx-destination{color:#fff;text-decoration:underline;}",
    "#__hx-online-setup a{color:#fff;text-decoration:underline;}",
    "#__hx-screen .__hx-file{display:flex;align-items:center;gap:12px;}",
    "#__hx-screen .__hx-file-name{font-size:12px;color:#a7a7a7;overflow:hidden;text-overflow:ellipsis;",
    "white-space:nowrap;}",
    "#__hx-screen .__hx-preview{width:128px;height:64px;image-rendering:pixelated;display:none;",
    "background:rgba(0,0,0,.35);border:2px solid #262423;border-radius:3px;}",
    "#__hx-screen .__hx-capes{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;}",
    "#__hx-screen .__hx-capes img{width:64px;height:32px;image-rendering:pixelated;",
    "background:rgba(0,0,0,.35);border:2px solid #262423;border-radius:3px;}",
    // The cape-upload group injected into the editor form: field rows, the
    // file row and the right-aligned save row. Spacing matches the editor's
    // measured rhythm (labels 4px above their control, 16px between fields).
    "#__hx-cape-upload .__hx-field{margin-bottom:16px;}",
    "#__hx-cape-upload .__hx-file{display:flex;align-items:flex-start;gap:12px;}",
    "#__hx-cape-upload .__hx-file button{flex-shrink:0;}",
    "#__hx-cape-upload .__hx-file-name{font-size:12px;line-height:16px;color:#a7a7a7;",
    "white-space:normal;overflow-wrap:anywhere;flex:0 1 auto;min-width:0;padding-top:8px;}",
    "#__hx-cape-upload .__hx-preview{width:128px;height:64px;image-rendering:pixelated;",
    "object-fit:contain;background:rgba(0,0,0,.35);border-radius:3px;margin-bottom:16px;display:none;}",
    "#__hx-cape-upload .__hx-error{min-height:16px;margin:0 0 12px;font-size:12px;",
    "line-height:16px;color:#ff6b5b;overflow-wrap:anywhere;}",
    "#__hx-cape-upload .__hx-actions{display:flex;justify-content:flex-end;}",
    "#__hx-cape-upload .__hx-field input{width:100%;box-sizing:border-box;}",
    // The modpack dialog. Its result rows are the launcher's own installation
    // rows cloned per hit, so every style here is spacing; the rows themselves
    // keep the app's hover, focus and typography untouched.
    "#__hx-screen .__hx-mod-results{list-style:none;margin:0;padding:0;}",
    "#__hx-screen .__hx-mod-results>li{margin:0 0 4px;}",
    "#__hx-screen .__hx-mod-results img{object-fit:cover;}",
    // Official magnified typeramp (module 21837): Tiny 20, ExtraSmall 24,
    // ExtraLarge 44. Text fields (40266) retain their 7px padding/2px edge;
    // btn1 (79554) uses ExtraLarge for its height. The dialog heading (21075)
    // stays 18px in the native component, so only body/control text scales.
    "#__hx-screen[data-hx-magnified=true] label{font-size:20px;line-height:24px;}",
    "#__hx-screen[data-hx-magnified=true] input[type=text]:not(#__hx-modpack-search),",
    "#__hx-screen[data-hx-magnified=true] input[type=password]{font-size:20px;line-height:24px;height:42px;}",
    "#__hx-screen[data-hx-magnified=true] .__hx-btn{font-size:20px;line-height:24px;height:44px;}",
    "#__hx-updates[data-hx-magnified=true] .__hx-btn{font-size:20px;line-height:24px;height:44px;}",
    "#__hx-screen[data-hx-magnified=true] .__hx-note{font-size:20px;line-height:24px;}",
    "#__hx-screen[data-hx-magnified=true] .__hx-error{font-size:20px;line-height:24px;min-height:24px;}",
    "#__hx-screen[data-hx-magnified=true] .__hx-file-name,",
    "#__hx-cape-upload[data-hx-magnified=true] .__hx-file-name,",
    "#__hx-cape-upload[data-hx-magnified=true] .__hx-error{font-size:20px;line-height:24px;}",
    "#__hx-cape-upload[data-hx-magnified=true] .__hx-file-name{padding-top:10px;}",
    "[data-hx-text-note]{overflow-wrap:anywhere;}",
    // Keep the original horizontal tab strip. When its native labels no
    // longer fit, expose the launcher's own scrollbar rather than hiding
    // the only pointer affordance for the remaining tabs.
    "[data-hx-scroll-tabs=true][role=tablist]::-webkit-scrollbar{display:block;height:8px;}",
    "#__hx-tile-wrap{margin-top:0;}"
  ].join("").replace(/#__hx-screen \.__hx-btn/g, ":is(#__hx-screen,#__hx-updates) .__hx-btn");

  function ensureStyle() {
    if (document.getElementById("__hx-style")) return;
    var tag = document.createElement("style");
    tag.id = "__hx-style";
    tag.textContent = STYLE;
    (document.head || document.documentElement).appendChild(tag);
  }

  function syncTextSize(element) {
    var settings = window.__host && window.__host.appData && window.__host.appData.launcherSettings;
    var value = settings && settings.useMagnifiedMode ? "true" : "false";
    if (element) {
      if (element.getAttribute("data-hx-magnified") !== value) element.setAttribute("data-hx-magnified", value);
      if (element.hasAttribute("data-hx-text-note")) {
        var font = value === "true" ? "20px" : "12px";
        var line = value === "true" ? "24px" : element.hasAttribute("data-hx-setting-note") ? "18px" : "";
        if (element.style.fontSize !== font) element.style.fontSize = font;
        if (element.style.lineHeight !== line) element.style.lineHeight = line;
        if (!element.hasAttribute("data-hx-setting-note")) {
          var minimum = value === "true" ? "24px" : "16px";
          if (element.style.minHeight !== minimum) element.style.minHeight = minimum;
        }
      }
    } else {
      document.querySelectorAll("#__hx-screen,#__hx-cape-upload,#__hx-updates,[data-hx-text-note]").forEach(syncTextSize);
    }
  }

  function injectCompatibilityNoticeLayout() {
    // Native modules 98168/8095 place prose and a nowrap link in a flex row.
    // Wrapping those flex items gives each its own line when space is tight,
    // while retaining the native surface, padding, text and action states.
    document.querySelectorAll('main a[href^="https://aka.ms/DownloadNewLauncher"]').forEach(function (link) {
      var notice = link.closest('[role="alert"]');
      var wrapper = notice && notice.parentElement;
      if (!notice || !wrapper || getComputedStyle(wrapper).position !== "absolute") return;
      if (notice.style.flexWrap !== "wrap") notice.style.flexWrap = "wrap";
      if (notice.style.overflowWrap !== "anywhere") notice.style.overflowWrap = "anywhere";
      if (link.style.whiteSpace !== "normal") link.style.whiteSpace = "normal";
      var main = document.querySelector("main");
      var width = Math.max(0, (main ? main.clientWidth : innerWidth) - 40) + "px";
      if (wrapper.style.maxWidth !== width) wrapper.style.maxWidth = width;
    });
  }

  function injectNativeTabLayout() {
    var tabs = document.querySelector('[role="tablist"][id^="tab-"][id$="-linkwrap"]');
    if (!tabs) return;
    var overflowing = tabs.scrollWidth > tabs.clientWidth + 1;
    var value = overflowing ? "true" : "false";
    if (tabs.getAttribute("data-hx-scroll-tabs") !== value) tabs.setAttribute("data-hx-scroll-tabs", value);
    if (!tabs.classList.contains("scrollbars-default")) tabs.classList.add("scrollbars-default");
    function reveal(tab) {
      if (!tab) return;
      var outer = tabs.getBoundingClientRect(), inner = tab.getBoundingClientRect();
      if (inner.left < outer.left) tabs.scrollLeft += inner.left - outer.left;
      else if (inner.right > outer.right) tabs.scrollLeft += inner.right - outer.right;
    }
    var active = tabs.querySelector('[role="tab"][aria-selected="true"]');
    // Link type is explicit in the native component. Its labels can widen
    // without changing the strip's font or viewport (text-size/localization).
    // Include intrinsic geometry, which stays stable during manual scrolling.
    var key = [active && active.getAttribute("href") || "", tabs.clientWidth,
      tabs.scrollWidth, active && active.offsetLeft || 0,
      active && active.offsetWidth || 0].join("|");
    if (tabs.getAttribute("data-hx-tab-layout-key") !== key) {
      tabs.setAttribute("data-hx-tab-layout-key", key);
      reveal(active);
    }
    if (!tabs.hasAttribute("data-hx-tab-focus-bound")) {
      tabs.setAttribute("data-hx-tab-focus-bound", "true");
      tabs.addEventListener("focusin", function (event) {
        reveal(event.target.closest('[role="tab"]'));
      });
    }
    // The magnified native header remains 94px high while its heading and
    // tabs require 107px (plus a scrollbar when needed). Sum their intrinsic
    // heights/margins, independent of parent centering or prior min-height.
    var heading = tabs.parentElement && tabs.parentElement.querySelector("h1");
    var navigation = tabs.closest('[role="navigation"]');
    var header = navigation;
    while (header && getComputedStyle(header).position !== "sticky") header = header.parentElement;
    if (heading && navigation && header) {
      var headingStyle = getComputedStyle(heading), navStyle = getComputedStyle(navigation);
      var height = Math.ceil(heading.offsetHeight + tabs.offsetHeight +
        (parseFloat(headingStyle.marginBottom) || 0) + (parseFloat(navStyle.marginTop) || 0));
      var minHeight = height + "px";
      if (header.style.minHeight !== minHeight) header.style.minHeight = minHeight;
    }
  }

  function esc(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // --- the launcher's dialog shell ----------------------------------------
  // `innerWidth` widens the content column: the launcher's own dialogs come
  // in two measured widths (550 px for forms, the page width for anything
  // that lists things), and the modpack dialog is the second kind.
  function screen(title, innerWidth) {
    closeScreen();
    screenGeneration++;
    screenFocus = document.activeElement;
    ensureStyle();
    var el = document.createElement("div");
    el.id = "__hx-screen";
    syncTextSize(el);
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-modal", "true");
    el.setAttribute("aria-labelledby", "__hx-screen-title");
    el.tabIndex = -1;
    el.innerHTML =
      '<div class="__hx-head"><h2 id="__hx-screen-title">' +
      esc(title) +
      "</h2>" +
      '<button type="button" class="__hx-close" aria-label="Close">' +
      '<svg width="16" height="16" viewBox="0 0 11 11" aria-hidden="true">' +
      '<path d="M1 1l9 9M10 1L1 10" stroke="currentColor" stroke-width="1.2" fill="none"/></svg>' +
      "</button></div>" +
      // scrollbars-default is the launcher's own class (8px, #262626 track,
      // white/20 thumb): every harness screen then scrolls with the same
      // scrollbar the app paints, rather than the browser's default one.
      '<div class="__hx-body scrollbars-default"><form class="__hx-inner" autocomplete="off"></form></div>' +
      '<div class="__hx-foot"></div>';

    var body = el.querySelector("form");
    if (innerWidth) body.style.width = innerWidth + "px";
    var foot = el.querySelector(".__hx-foot");
    var api = {
      el: el,
      body: body,
      add: function (html) {
        body.insertAdjacentHTML("beforeend", html);
      },
      // Both kinds are plain buttons: the form only exists so that Enter in a
      // field submits, and its handler clicks the primary one. A type="submit"
      // primary would fire submit, click itself and loop.
      button: function (label, primary, onClick) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "__hx-btn" + (primary ? " __hx-primary" : "");
        b.innerHTML = "<span>" + esc(label) + "</span>";
        b.addEventListener("click", function (event) {
          event.preventDefault();
          if (onClick) onClick();
        });
        foot.appendChild(b);
        return b;
      },
      close: function () { if (el.isConnected) closeScreen(); },
    };

    body.addEventListener("submit", function (event) {
      event.preventDefault();
      var primary = foot.querySelector(".__hx-primary");
      if (primary && !primary.disabled) primary.click();
    });
    el.querySelector(".__hx-close").addEventListener("click", closeScreen);
    document.addEventListener("keydown", escHandler, true);
    document.addEventListener("focusin", screenFocusHandler, true);
    screenBackground = Array.prototype.map.call(document.body.children, function (node) {
      var previous = node.inert;
      node.inert = true;
      return { node: node, inert: previous };
    });
    document.body.appendChild(el);
    // Callers build the body synchronously. Focus its first field or primary
    // action after that content exists, while respecting an explicit focus.
    Promise.resolve().then(function () { focusScreen(el); });
    // Remembered so the in-app router can dismiss the screen: the launcher is a
    // single page, so navigating with the sidebar would otherwise leave this
    // panel sitting over the new page.
    openedOn = location.pathname;
    return api;
  }

  function escHandler(event) {
    var el = document.getElementById("__hx-screen");
    if (!el) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeScreen();
    } else if (event.key === "Tab") {
      var controls = screenControls(el);
      var current = document.activeElement;
      var first = controls[0];
      var last = controls[controls.length - 1];
      if (!controls.length) {
        event.preventDefault();
        el.focus();
      } else if (!el.contains(current) || current === el ||
          (event.shiftKey ? current === first : current === last)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      }
    }
  }

  function screenControls(el) {
    return Array.prototype.filter.call(el.querySelectorAll(
      'button,input:not([type="hidden"]),select,textarea,a[href],[tabindex],[contenteditable="true"]'
    ), function (node) {
      return node.tabIndex >= 0 && !node.matches(":disabled") &&
        !node.closest("[inert]") && node.getClientRects().length > 0 &&
        getComputedStyle(node).visibility !== "hidden";
    });
  }

  function focusScreen(el) {
    if (!el.isConnected || el.contains(document.activeElement)) return;
    var controls = screenControls(el);
    var content = controls.filter(function (node) {
      return node.matches("input,select,textarea,.__hx-primary");
    });
    (content[0] || controls[0] || el).focus();
  }

  function screenFocusHandler(event) {
    var el = document.getElementById("__hx-screen");
    if (el && !el.contains(event.target)) focusScreen(el);
  }

  function closeScreen() {
    var el = document.getElementById("__hx-screen");
    document.removeEventListener("keydown", escHandler, true);
    document.removeEventListener("focusin", screenFocusHandler, true);
    if (el) {
      el.dispatchEvent(new Event("__hxclose"));
      el.remove();
    }
    screenBackground.forEach(function (entry) { entry.node.inert = entry.inert; });
    screenBackground = [];
    var restore = screenFocus;
    screenFocus = null;
    if (restore && restore.isConnected && typeof restore.focus === "function") restore.focus();
    openedOn = null;
  }

  function field(id, label, placeholder, value) {
    return (
      '<div class="__hx-row"><label for="' + id + '">' + esc(label) + "</label>" +
      '<input type="text" id="' + id + '" name="' + id + '" placeholder="' + esc(placeholder || "") +
      '" value="' + esc(value || "") + '" spellcheck="false"></div>'
    );
  }

  // --- the single New skin entry point ------------------------------------
  // Cape upload belongs in the launcher's existing skin editor; never add a
  // second launcher tile or a separate cape library to the page.
  function injectSkinActions() {
    ["#__hx-add-cape", "#__hx-add-skin-cape", "#__hx-cape-library"].forEach(function (selector) {
      document.querySelectorAll(selector).forEach(function (element) { element.remove(); });
    });
    injectSkinCapeUpload();
  }

  function clonedButtonLabel(button, text) {
    var leaves = Array.prototype.filter.call(button.querySelectorAll("span"), function (span) {
      return !span.children.length && String(span.textContent || "").trim();
    });
    // The welcome CTA uses a styled div face and an empty overlap slab,
    // rather than spans. Replacing the button's text would delete both.
    var label = leaves[leaves.length - 1] || button.querySelector(".container") || button;
    label.textContent = text;
    button.removeAttribute("aria-labelledby");
    button.setAttribute("aria-label", text);
    button.disabled = false;
    button.removeAttribute("aria-disabled");
    button.type = "button";
    return label;
  }

  function nativeAccountAction(section) {
    // The section contains its logo, account listbox, then a direct action
    // wrapper. Account-row buttons and our cloned wrappers are separate.
    var children = Array.prototype.slice.call(section.children);
    for (var i = children.length - 1; i >= 0; i--) {
      var child = children[i];
      if (!child.hasAttribute("data-hx-account-action") &&
          !child.matches('[role="listbox"]') && !child.querySelector('[role="listbox"]') &&
          child.querySelector("button")) return child;
    }
    return null;
  }

  function injectFirstRunOfflineButton() {
    // This signup destination belongs to the native initial/relogin surface
    // in every locale. Its sibling is the native Microsoft sign-in button.
    var signup = document.querySelector('a[href^="https://sisu.xboxlive.com/connect/XboxLive/"]');
    var holder = signup && signup.parentElement;
    if (!holder || holder.closest("#__hx-screen")) return;
    var native = holder.querySelector("button");
    if (!native) return;
    bindOnlineSignIn(native);
    if (holder.querySelector("#__hx-first-offline")) return;
    var button = native.cloneNode(true);
    button.id = "__hx-first-offline";
    button.querySelectorAll("img,svg").forEach(function (icon) { icon.remove(); });
    // CTA styling uses .container/.overlap; their native IDs must stay unique.
    button.querySelectorAll("[id]").forEach(function (node) { node.removeAttribute("id"); });
    clonedButtonLabel(button, "Continue offline");
    button.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      openOfflineAccount();
    });
    holder.insertBefore(button, signup);
  }

  function injectOfflineAccountButton() {
    var section = document.getElementById("microsoftAccountSection");
    if (!section || section.querySelector("#__hx-add-offline")) return;
    var action = nativeAccountAction(section);
    var add = action && action.querySelector("button");
    if (!add || !add.parentElement) return;
    var holder = action.cloneNode(true);
    holder.setAttribute("data-hx-account-action", "offline");
    var button = holder.querySelector("button");
    if (!button) return;
    button.id = "__hx-add-offline";
    clonedButtonLabel(button, "Add offline account");
    button.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      openOfflineAccount();
    });
    action.parentElement.insertBefore(holder, action.nextSibling);
  }

  // Route the original action to the host's working sign-in dialog. Keeping
  // the native action preserves its layout and localized label without a
  // second button for the same task or the captured pane's error body.
  function injectOnlineSignInButton() {
    var section = document.getElementById("microsoftAccountSection");
    if (!section) return;
    section.querySelectorAll('[data-hx-account-action="online"]').forEach(function (holder) { holder.remove(); });
    var action = nativeAccountAction(section);
    var add = action && action.querySelector("button");
    bindOnlineSignIn(add);
  }

  function bindOnlineSignIn(button) {
    if (!button || button.hasAttribute("data-hx-online-bound")) return;
    button.setAttribute("data-hx-online-bound", "true");
    button.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopImmediatePropagation();
      openOnlineSignIn();
    }, true);
    button.addEventListener("keydown", function (event) {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (!event.repeat) openOnlineSignIn();
    }, true);
  }

  function openOnlineSignIn() {
    var s = screen("Sign in with Microsoft");
    s.add('<p class="__hx-note">Sign in with your Microsoft account in your browser. ' +
      'Use an account that owns Minecraft Java Edition for online play.</p>');
    s.add('<p class="__hx-note" id="__hx-online-status" role="status">Checking Microsoft sign-in setup\u2026</p>');
    s.add('<details id="__hx-online-setup"><summary>Microsoft application setup</summary>' +
      '<p class="__hx-note">This launcher needs its own registered Microsoft application before online sign-in is available. ' +
      'Offline play works without it.</p><p class="__hx-note">In ' +
      '<a href="https://entra.microsoft.com/" target="_blank" rel="noopener noreferrer">Microsoft Entra</a>, ' +
      'register an application that supports personal Microsoft accounts. Add a Mobile and desktop applications ' +
      'redirect URI of <code>http://localhost</code>, then paste its Application (client) ID below. ' +
      'Use the public ID only; a client secret is not needed.</p>' +
      field("__hx_msa_client_id", "Application (client) ID", "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx") + '</details>');
    s.add('<p class="__hx-error" id="__hx_online_error" role="alert"></p>');
    var error = s.body.querySelector("#__hx_online_error");
    var status = s.body.querySelector("#__hx-online-status");
    var setup = s.body.querySelector("#__hx-online-setup");
    var clientInput = s.body.querySelector("#__hx_msa_client_id");
    clientInput.setAttribute("aria-describedby", error.id);
    var configuration = null;
    s.button("Cancel", false, s.close);
    var retry = s.button("Retry", false, loadConfiguration);
    retry.hidden = true;
    var save = s.button("Save application ID", false, saveConfiguration);
    save.id = "__hx-msa-save";
    save.hidden = true;
    var go = s.button("Sign in with Microsoft", true, submit);
    var goLabel = go.querySelector("span");
    go.id = "__hx-online-go";
    go.type = "button";
    go.setAttribute("aria-describedby", "__hx_online_error");
    go.disabled = true;
    setup.addEventListener("toggle", function () {
      save.hidden = !setup.open || Boolean(configuration && configuration.environmentOverride);
    });
    clientInput.addEventListener("keydown", function (event) {
      if (event.key !== "Enter" || !setup.open) return;
      event.preventDefault();
      event.stopPropagation();
      if (!event.repeat) saveConfiguration();
    });
    loadConfiguration();

    async function readConfiguration(options) {
      var response = await fetch("/__msa/config", options || { cache: "no-store" });
      var body = await response.json();
      if (!response.ok) throw new Error(body.error || "Could not load Microsoft sign-in setup.");
      if (typeof body.configured !== "boolean") throw new Error("Could not load Microsoft sign-in setup.");
      return body;
    }

    function applyConfiguration(value) {
      configuration = value;
      clientInput.value = value.clientId || "";
      clientInput.readOnly = Boolean(value.environmentOverride);
      setup.open = !value.configured;
      save.disabled = false;
      save.hidden = !setup.open || Boolean(value.environmentOverride);
      go.disabled = !value.configured;
      status.textContent = value.environmentOverride
        ? "The application ID is set by LAUNCHER_MSA_CLIENT_ID. Change that environment setting and restart to use another registration."
        : value.configured ? "Application ID configured. You can continue to Microsoft in your browser."
        : "Set up the launcher's Microsoft application, or use Continue offline.";
    }

    async function loadConfiguration() {
      retry.hidden = true;
      go.disabled = true;
      save.disabled = true;
      error.textContent = "";
      try {
        var value = await readConfiguration();
        if (!s.el.isConnected) return;
        applyConfiguration(value);
      } catch (e) {
        if (!s.el.isConnected) return;
        status.textContent = "";
        error.textContent = e.message || "Could not load Microsoft sign-in setup.";
        retry.hidden = false;
      }
    }

    async function saveConfiguration() {
      if (save.disabled || !s.el.isConnected) return;
      var value = clientInput.value.trim();
      if (!/^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$/.test(value)) {
        error.textContent = "Enter the Application (client) ID from your Microsoft registration.";
        clientInput.focus();
        return;
      }
      save.disabled = true;
      go.disabled = true;
      error.textContent = "";
      try {
        var saved = await readConfiguration({method:"POST", headers:{"Content-Type":"application/json"},
          body:JSON.stringify({clientId:value})});
        if (!s.el.isConnected) return;
        applyConfiguration(saved);
      } catch (e) {
        if (!s.el.isConnected) return;
        save.disabled = false;
        go.disabled = !Boolean(configuration && configuration.configured);
        error.textContent = e.message || "Could not save Microsoft sign-in setup.";
      }
    }

    async function submit() {
      if (go.disabled || !s.el.isConnected) return;
      error.textContent = "";
      go.disabled = true;
      save.disabled = true;
      var original = goLabel.textContent;
      goLabel.textContent = "Waiting for your browser\u2026";
      try {
        var account = await window.__harness.microsoftSignIn();
        refreshAccountScreen(s, account && (account.javaProfileName || account.username));
      } catch (e) {
        if (!s.el.isConnected) return;
        go.disabled = false;
        save.disabled = false;
        goLabel.textContent = original;
        error.textContent = (e && e.message) || "Could not sign in.";
      }
    }
  }

  function refreshAccountScreen(s, name) {
    if (!s.el.isConnected) return;
    var route = location.pathname;
    s.close();
    var generation = screenGeneration;
    setTimeout(function () {
      if (generation !== screenGeneration || location.pathname !== route || document.getElementById("__hx-screen")) return;
      var section = document.getElementById("microsoftAccountSection");
      if (section && (!name || section.textContent.indexOf(name) === -1)) location.reload();
    }, 500);
  }

  function openOfflineAccount() {
    var s = screen("Add offline account");
    s.add('<p class="__hx-note">Play without signing in to Microsoft. The name becomes your player name.</p>');
    s.add(field("__hx_offline_name", "Name", "Player name"));
    s.add('<p class="__hx-error" id="__hx_offline_error" role="alert"></p>');
    var input = s.body.querySelector("#__hx_offline_name");
    var error = s.body.querySelector("#__hx_offline_error");
    input.setAttribute("aria-describedby", error.id);
    input.addEventListener("input", function () {
      input.removeAttribute("aria-invalid");
      error.textContent = "";
    });
    var cancel = s.button("Cancel", false, s.close);
    var save = s.button("Add account", true, submit);
    cancel.type = "button";
    input.focus();

    async function submit() {
      if (save.disabled || !s.el.isConnected) return;
      var name = (input.value || "").trim();
      error.textContent = "";
      input.removeAttribute("aria-invalid");
      if (!/^[A-Za-z0-9_]{3,16}$/.test(name)) {
        input.setAttribute("aria-invalid", "true");
        error.textContent = "Use 3–16 letters, numbers or underscores.";
        input.focus();
        return;
      }
      save.disabled = true;
      input.disabled = true;
      try {
        await window.__harness.accountAction("login", { name: name });
        refreshAccountScreen(s, name);
      } catch (e) {
        if (!s.el.isConnected) return;
        save.disabled = false;
        input.disabled = false;
        error.textContent = (e && e.message) || "Could not add the account.";
      }
    }
  }

  // Keep the native choices and persistence, with copy matching the local host.
  function injectUnavailableSettings() {
    [
      { id: "keepLauncherOpen", note: "Closing the launcher keeps your game running in the background. Use the tray icon to reopen it. This setting keeps the window visible after launch." },
      { id: "crashAssistance", label: "Show local crash explanations", note: "Read explanations from this installation’s logs in Logs. Analysis stays on this computer; crash reports are never uploaded." },
    ].forEach(function (setting) {
      var input = document.querySelector('input[type="checkbox"][id="' + setting.id + '"]');
      if (!input || !input.parentElement) return;
      var wrapper = input.closest("label") || input.parentElement;
      var noteId = "__hx-setting-" + setting.id + "-note";
      var note = document.getElementById(noteId);
      input.disabled = false;
      var nativeLabel = document.querySelector('label[for="' + setting.id + '"]');
      if (setting.label && nativeLabel && nativeLabel.textContent !== setting.label) {
        var text = nativeLabel.querySelector('span');
        (text || nativeLabel).textContent = setting.label;
      }
      if (setting.label && input.getAttribute("aria-label") !== setting.label) input.setAttribute("aria-label", setting.label);
      if (!note) {
        note = document.createElement("p");
        note.id = noteId;
        note.setAttribute("data-hx-setting-note", setting.id);
        note.setAttribute("data-hx-text-note", "");
        note.textContent = setting.note;
        note.style.cssText = "margin:4px 0 16px;font-size:12px;line-height:18px;color:#a7a7a7;";
        syncTextSize(note);
        var label = wrapper.querySelector('label[for="' + setting.id + '"]') ||
          document.querySelector('label[for="' + setting.id + '"]');
        if (label) {
          note.style.marginLeft = Math.max(0, Math.round(
            label.getBoundingClientRect().left - wrapper.getBoundingClientRect().left
          )) + "px";
        }
        var rowGap = parseFloat(getComputedStyle(wrapper).marginBottom) || 0;
        note.style.marginTop = (4 - rowGap) + "px";
      }
      if (wrapper.nextElementSibling !== note) wrapper.insertAdjacentElement("afterend", note);
      if (note.textContent !== setting.note) note.textContent = setting.note;
      var main = note.closest("main");
      if (main) {
        var available = Math.max(0, Math.floor(Math.min(innerWidth, main.getBoundingClientRect().right) - note.getBoundingClientRect().left - 20)) + "px";
        if (note.style.maxWidth !== available) note.style.maxWidth = available;
      }
      var describedBy = (input.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
      if (describedBy.indexOf(noteId) < 0) {
        describedBy.push(noteId);
        input.setAttribute("aria-describedby", describedBy.join(" "));
      }
    });
  }

  // The desktop shell owns the default source, verification and installation.
  // This dialog reflects its state and preserves an edited source draft.
  function openUpdatesDialog() {
    var s = screen("Launcher updates");
    var desktop = window.launcherDesktop;
    var githubSource = "https://github.com/abdulmunthakimwork/minecraft-launcher/";
    s.add('<p class="__hx-note" id="__hx-update-version">Reading launcher version…</p>');
    s.add('<p class="__hx-note">GitHub is the default update source. With a source configured, the launcher checks at startup and every six hours, downloads and verifies updates automatically, then waits for you to choose Restart and install when the launcher is idle.</p>');
    s.add(field("__hx-update-feed", "Update source", githubSource, ""));
    s.add('<p class="__hx-note">Use the GitHub repository above or a custom HTTPS release directory. Leave the source empty to turn off automatic checks.</p>');
    s.add('<p class="__hx-error" id="__hx-update-status" role="status" aria-live="polite"></p>');
    s.add('<p class="__hx-note" id="__hx-update-install-reason" hidden></p>');
    var feed = s.body.querySelector("#__hx-update-feed");
    var version = s.body.querySelector("#__hx-update-version");
    var status = s.body.querySelector("#__hx-update-status");
    var reason = s.body.querySelector("#__hx-update-install-reason");
    var state = null, pendingAction = false, feedTouched = false, feedInitialized = false, sequence = 0;
    var unsubscribe = null;
    s.button("Close", false, s.close);
    var save = s.button("Save source", false, saveFeed);
    save.setAttribute("data-hx-update-save", "");
    var useGithub = s.button("Use GitHub", false, function () {
      if (!s.el.isConnected || useGithub.disabled) return;
      feed.value = githubSource;
      feedTouched = true;
      saveFeed();
    });
    useGithub.setAttribute("data-hx-update-github", "");
    var check = s.button("Check for updates", false, function () { action("checkForUpdates"); });
    check.setAttribute("data-hx-update-check", "");
    var install = s.button("Restart and install", true, function () {
      if (state && state.status === "ready" && state.installAllowed) action("restartAndInstall");
    });
    install.setAttribute("data-hx-update-install", "");
    install.setAttribute("aria-describedby", reason.id);
    feed.disabled = save.disabled = useGithub.disabled = check.disabled = install.disabled = true;
    feed.addEventListener("input", function () { feedTouched = true; });
    s.el.addEventListener("__hxclose", function () {
      sequence++;
      if (typeof unsubscribe === "function") unsubscribe();
      unsubscribe = null;
    }, { once: true });

    function notice(text, error) {
      status.textContent = text;
      status.style.color = error ? "" : "#a7a7a7";
      status.setAttribute("role", error ? "alert" : "status");
    }
    function render(next) {
      if (!s.el.isConnected || !next) return;
      state = next;
      sequence++;
      if (!feedTouched && (!feedInitialized || document.activeElement !== feed)) feed.value = next.feedUrl || "";
      feedInitialized = true;
      if (next.currentVersion) version.textContent = "Installed launcher version: " + next.currentVersion;
      var busy = pendingAction || ["checking", "downloading", "installing"].indexOf(next.status) >= 0;
      feed.disabled = save.disabled = useGithub.disabled = busy;
      check.disabled = busy || !next.feedUrl;
      check.querySelector("span").textContent = next.status === "error" ? "Retry update check" : "Check for updates";
      install.hidden = next.status !== "ready" && next.status !== "installing";
      install.disabled = pendingAction || next.status !== "ready" || !next.installAllowed;
      reason.hidden = next.status !== "ready" || !!next.installAllowed;
      reason.textContent = reason.hidden ? "" : "Finish any running game, download or installation work before restarting to install the update.";
      if (next.status === "setup") notice("Automatic update checks are off. Use GitHub or save a custom HTTPS release directory to turn them on.");
      else if (next.status === "checking") notice("Checking the configured update source…");
      else if (next.status === "downloading") {
        var p = next.progress;
        var percent = p && Number.isFinite(Number(p.percent)) ? Math.min(100, Math.max(0, Number(p.percent))) : null;
        notice("Downloading launcher " + (next.availableVersion || "update") +
          (percent == null ? "…" : " — " + Math.round(percent) + "%") +
          (p && p.total ? " (" + sizeText(p.transferred) + " / " + sizeText(p.total) + ")" : "") + ". Verification runs before installation.");
      } else if (next.status === "ready") notice("Launcher " + (next.availableVersion || "update") + " is downloaded and verified. Restart and install when the launcher is idle.");
      else if (next.status === "installing") notice("Restarting to install the verified launcher update…");
      else if (next.status === "error") notice(next.error || "The update check failed. Retry when the update source is available.", true);
      else if (next.status === "idle" && next.lastCheckedAt) notice("Up to date. The launcher checks again automatically every six hours.");
      else notice(next.availableVersion ? "Available launcher version: " + next.availableVersion : "The update source is configured. The launcher checks automatically at startup and every six hours, or you can check now.");
    }
    async function action(method, value) {
      if (!s.el.isConnected || !desktop || !state || pendingAction || typeof desktop[method] !== "function") return;
      if ((method === "configureUpdateFeed" && save.disabled) ||
          (method === "checkForUpdates" && check.disabled) ||
          (method === "restartAndInstall" && install.disabled)) return;
      pendingAction = true;
      if (state) render(state);
      var token = sequence;
      try {
        var next = await desktop[method](value);
        if (!s.el.isConnected) return;
        pendingAction = false;
        if (token === sequence) render(next);
        else if (state) render(state);
      } catch (error) {
        if (!s.el.isConnected) return;
        pendingAction = false;
        if (state) render(state);
        notice((error && error.message) || "The launcher update action failed.", true);
      }
    }
    function saveFeed() {
      if (!s.el.isConnected || save.disabled) return;
      var value = feed.value.trim();
      if (value) {
        try {
          var url = new URL(value);
          if (url.protocol !== "https:" || url.username || url.password || url.hash) throw new Error();
        } catch (_) {
          feed.setAttribute("aria-invalid", "true");
          notice("Enter an HTTPS GitHub repository or release directory URL without credentials or a fragment. Leave it empty to turn off automatic checks.", true);
          return;
        }
      }
      feed.removeAttribute("aria-invalid");
      action("configureUpdateFeed", value);
    }
    if (!desktop || typeof desktop.getUpdateState !== "function") {
      install.hidden = true;
      notice("Launcher updates are available in the desktop app. This browser view cannot configure or install them.");
      version.textContent = "Desktop launcher version unavailable in this view.";
      return;
    }
    var initial = sequence;
    if (typeof desktop.onUpdateState === "function") unsubscribe = desktop.onUpdateState(render);
    desktop.getUpdateState().then(function (next) { if (initial === sequence) render(next); }).catch(function (error) {
      if (s.el.isConnected) notice((error && error.message) || "Could not read launcher update state.", true);
    });
    if (typeof desktop.getAppInfo === "function") desktop.getAppInfo().then(function (info) {
      if (s.el.isConnected && info && info.version) version.textContent = "Installed launcher version: " + info.version;
    }).catch(function () {});
  }

  // One page-level subscription serves the current Settings entry. React can
  // replace that entry without retaining detached nodes or adding listeners.
  var updatesEntryDesktop = null, updatesEntryState = null, updatesEntryUnsubscribe = null, updatesEntryRevision = 0;
  function renderUpdatesEntry() {
    var entry = document.querySelector("#__hx-updates button");
    if (!entry) return;
    var ready = updatesEntryState && updatesEntryState.status === "ready";
    var label = ready ? "Launcher update ready" : "Launcher updates";
    var text = entry.querySelector("span");
    if (text && text.textContent !== label) text.textContent = label;
    if (ready) {
      var description = label + (updatesEntryState.availableVersion ? ": version " + updatesEntryState.availableVersion : "") + ". Review and restart when ready.";
      if (entry.getAttribute("aria-label") !== description) entry.setAttribute("aria-label", description);
    } else entry.removeAttribute("aria-label");
  }
  function watchUpdatesEntry() {
    var desktop = window.launcherDesktop;
    if (!desktop || typeof desktop.getUpdateState !== "function") return;
    if (updatesEntryDesktop === desktop) { renderUpdatesEntry(); return; }
    if (typeof updatesEntryUnsubscribe === "function") updatesEntryUnsubscribe();
    updatesEntryDesktop = desktop;
    updatesEntryState = null;
    updatesEntryUnsubscribe = null;
    updatesEntryRevision++;
    function receive(next) {
      if (updatesEntryDesktop !== desktop || !next) return;
      updatesEntryState = next;
      updatesEntryRevision++;
      renderUpdatesEntry();
    }
    if (typeof desktop.onUpdateState === "function") updatesEntryUnsubscribe = desktop.onUpdateState(receive);
    var initial = updatesEntryRevision;
    desktop.getUpdateState().then(function (next) {
      if (updatesEntryDesktop === desktop && initial === updatesEntryRevision) receive(next);
    }).catch(function () {});
    renderUpdatesEntry();
  }
  function injectUpdatesButton() {
    var main = document.querySelector("main");
    if (!main) return;
    if (main.querySelector("#__hx-updates")) { watchUpdatesEntry(); return; }
    var anchor = main.querySelector("#keepLauncherOpen") || main.querySelector("h1");
    if (!anchor) return;
    ensureStyle();
    var group = document.createElement("div");
    group.id = "__hx-updates";
    group.style.cssText = "margin:16px 0;";
    syncTextSize(group);
    var entry = document.createElement("button");
    entry.type = "button";
    entry.className = "__hx-btn";
    entry.innerHTML = "<span>Launcher updates</span>";
    entry.addEventListener("click", function (event) { event.preventDefault(); openUpdatesDialog(); });
    group.appendChild(entry);
    var row = anchor.closest("label") || anchor.parentElement;
    (row === main ? anchor : row).insertAdjacentElement("afterend", group);
    watchUpdatesEntry();
  }

  var UNSUPPORTED = {
    bedrock: { title: "Bedrock Edition", note: "Bedrock Edition cannot be installed or launched by this build. Use the official Minecraft Launcher for Bedrock; Java Edition is supported here.", url: "https://www.minecraft.net/en-us/download", label: "Get the official Minecraft Launcher" },
    realms: { title: "Minecraft Realms", note: "Realms play, trials and subscription management are unavailable in this build. Use the official Minecraft Launcher or the Minecraft Realms website.", url: "https://www.minecraft.net/en-us/realms", label: "Open official Minecraft Realms" },
    marketplace: { title: "Minecraft Marketplace", note: "Marketplace purchases, subscriptions and in-game content actions are unavailable in this build. You can read the catalog here and visit the official Marketplace website.", url: "https://www.minecraft.net/en-us/marketplace", label: "Open official Minecraft Marketplace" },
    purchases: { title: "Minecraft purchases", note: "Purchases and gifts are unavailable in this build. Use the official Minecraft website or Microsoft Store. Existing local Java installations remain available here.", url: "https://www.minecraft.net/en-us/store/minecraft-java-bedrock-edition-pc", label: "Open official Minecraft store" },
    messaging: { title: "Minecraft messaging", note: "Online messaging actions are unavailable in this build. Read-only notifications remain available. Use the official Minecraft Launcher for online services.", url: "https://www.minecraft.net/en-us/download", label: "Get the official Minecraft Launcher" }
  };
  function openUnsupportedDialog(service) {
    var info = UNSUPPORTED[service];
    if (!info) return;
    var s = screen(info.title);
    s.add('<p class="__hx-note">' + esc(info.note) + '</p>');
    s.add('<p class="__hx-note"><a class="__hx-destination" href="' + info.url + '" target="_blank" rel="noopener noreferrer">' + esc(info.label) + '</a></p>');
    s.add('<p class="__hx-note">To play here, close this dialog and select Java Edition in the sidebar.</p>');
    s.button("Close", false, s.close);
    var java = document.querySelector('a[href="/games/java/play"]') || document.querySelector('a[href="/games/java"]');
    if (java) s.button("Play Java Edition", true, function () { s.close(); java.click(); });
  }
  function injectUnsupportedServices(route) {
    var capabilities = window.__host && window.__host.appData && window.__host.appData.capabilities;
    if (!capabilities) return;
    document.querySelectorAll('main button,main a[href],main [role="button"],button[aria-label]').forEach(function (action) {
      if (action.closest('#__hx-screen,[data-hx-installation-template]')) return;
      var bound = action.getAttribute('data-hx-unsupported-action');
      if (bound && UNSUPPORTED[bound]) {
        if (action.disabled || action.textContent.trim() !== UNSUPPORTED[bound].label) clonedButtonLabel(action, UNSUPPORTED[bound].label);
        return;
      }
      var href = action.getAttribute('href') || '';
      var label = (action.getAttribute('aria-label') || action.textContent || '').trim();
      var service = null;
      if (!capabilities.purchases && /^#openOneVanillaMultisku/.test(href)) service = 'purchases';
      else if (/^minecraft:/.test(href)) service = 'bedrock';
      else if (!capabilities.realms && /^\/games\/(java|bedrock)\/realms$/.test(route) && !/^https:/.test(href) && /subscribe|free trial|purchase.*realms|sign up.*realms/i.test(label)) service = 'realms';
      else if (!capabilities.purchases && route === '/games/bedrock/marketplace_pass' && !/^https:/.test(href) && /subscribe|visit.*marketplace|open.*bedrock|purchase|buy|sign up.*realms/i.test(label)) service = 'marketplace';
      else if (route === '/games/bedrock/play' && !/^https:/.test(href) && /^(play|install|update|buy now|purchase minecraft|try for free)$/i.test(label)) service = 'bedrock';
      else if (!capabilities.purchases && !/^https:/.test(href) && /^(buy(?: now| minecraft| a gift)?|purchase minecraft(?:.*)?|get minecraft|gift minecraft(?:.*)?|try for free)$/i.test(label)) service = 'purchases';
      else if (/^(send message|messaging)$/i.test(label)) service = 'messaging';
      if (!service) return;
      action.setAttribute('data-hx-unsupported-action', service);
      clonedButtonLabel(action, UNSUPPORTED[service].label);
      action.addEventListener('click', function (event) {
        event.preventDefault(); event.stopImmediatePropagation(); openUnsupportedDialog(service);
      }, true);
      action.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault(); event.stopImmediatePropagation(); openUnsupportedDialog(service);
        }
      }, true);
    });
  }

  // --- cape upload inside the existing New skin editor --------------------
  function injectSkinCapeUpload() {
    var dialog = document.getElementById("modal-dialog");
    if (!dialog || dialog.querySelector("#__hx-cape-upload")) return;
    var group = dialog.querySelector('[role="radiogroup"][aria-labelledby="cape_label"]');
    var nameSource = dialog.querySelector('input[name="name"]');
    if (!group || !nameSource) return;

    // The picker is a control group (label above, radio cards below) in the
    // editor form's column; the upload section joins that column as one more
    // group, so it lines up under the picker at the picker's width and keeps
    // the form's vertical rhythm. Class hashes change between builds, so
    // everything here is measured from live elements, not hardcoded.
    var capeGroup = group.parentElement;
    // The skin-file field immediately precedes Cape in the native editor.
    // Its real button is Browse in every locale; tooltip links are separate.
    var fileGroup = capeGroup && capeGroup.previousElementSibling;
    var browseSource = fileGroup && fileGroup.querySelector("button");
    if (!browseSource) return;
    var labelSource = capeGroup ? capeGroup.firstElementChild : null;
    if (!labelSource) return;
    // The group's label is a styled <label> inside a plain wrapper (the
    // wrapper also carries a React-controlled tooltip); cloning the inner
    // element keeps the heading's typography.
    var labelInner = labelSource.querySelector("label") || labelSource;
    var aboveGroup = capeGroup.previousElementSibling;
    var groupGap = aboveGroup
      ? Math.round(Number(String(getComputedStyle(aboveGroup).marginBottom).replace(/px$/, "")) || 0)
      : 24;
    if (!groupGap) groupGap = 24;

    function cloneButton(source, id, label) {
      var button = source.cloneNode(true);
      button.querySelectorAll("[id]").forEach(function (node) { node.removeAttribute("id"); });
      button.id = id;
      clonedButtonLabel(button, label);
      button.addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
      });
      return button;
    }

    // Holder: one more group in the column, below the picker. It inherits
    // the column's width and the group spacing from the picker's siblings.
    var holder = document.createElement("div");
    holder.id = "__hx-cape-upload";
    holder.style.cssText = "margin-top:" + groupGap + "px;";

    var groupHeading = labelSource.cloneNode(true);
    Array.prototype.forEach.call(groupHeading.querySelectorAll("a"), function (el) {
      el.remove();
    });
    Array.prototype.forEach.call(groupHeading.querySelectorAll("[id]"), function (el) {
      el.removeAttribute("id");
    });
    var headingLabel = groupHeading.querySelector("label");
    if (headingLabel) {
      headingLabel.removeAttribute("for");
      headingLabel.removeAttribute("id");
      headingLabel.textContent = "Upload a cape";
    } else {
      groupHeading.textContent = "Upload a cape";
    }
    holder.appendChild(groupHeading);

    var capeNameLabel = labelInner.cloneNode(true);
    capeNameLabel.removeAttribute("id");
    capeNameLabel.setAttribute("for", "__hx-cape-name");
    capeNameLabel.textContent = "Cape name";
    var nameInput = nameSource.cloneNode(false);
    nameInput.id = "__hx-cape-name";
    nameInput.removeAttribute("name");
    nameInput.removeAttribute("aria-describedby");
    nameInput.removeAttribute("aria-invalid");
    nameInput.value = "";
    nameInput.placeholder = "My cape";
    nameInput.setAttribute("aria-label", "Cape name");
    var nameField = document.createElement("div");
    nameField.className = "__hx-field";
    nameField.appendChild(capeNameLabel);
    nameField.appendChild(nameInput);
    holder.appendChild(nameField);

    var fileLabel = labelInner.cloneNode(true);
    fileLabel.removeAttribute("id");
    fileLabel.removeAttribute("for");
    fileLabel.textContent = "Cape file (64 × 32 PNG)";
    var browse = cloneButton(browseSource, "__hx-cape-browse", "Browse");
    var fileName = document.createElement("span");
    fileName.className = "__hx-file-name";
    fileName.textContent = "No file chosen";
    var fileRow = document.createElement("div");
    fileRow.className = "__hx-file";
    fileRow.appendChild(browse);
    fileRow.appendChild(fileName);
    var fileField = document.createElement("div");
    fileField.className = "__hx-field";
    fileField.appendChild(fileLabel);
    fileField.appendChild(fileRow);
    holder.appendChild(fileField);

    var preview = document.createElement("img");
    preview.className = "__hx-preview";
    preview.alt = "Cape preview";
    holder.appendChild(preview);

    var error = document.createElement("p");
    error.id = "__hx-cape-status";
    error.className = "__hx-error";
    error.setAttribute("role", "status");
    holder.appendChild(error);
    browse.setAttribute("aria-describedby", error.id);
    function setCapeStatus(message, success) {
      error.textContent = message || "";
      error.setAttribute("role", message && !success ? "alert" : "status");
      error.style.color = success ? "#86d562" : "";
      if (!message || success) browse.removeAttribute("aria-invalid");
    }

    var save = cloneButton(browseSource, "__hx-cape-save", "Save cape");
    var actions = document.createElement("div");
    actions.className = "__hx-actions";
    actions.appendChild(save);
    holder.appendChild(actions);

    syncTextSize(holder);
    capeGroup.insertAdjacentElement("afterend", holder);
    var fileData = null;
    var pending = false;
    var savingCape = false;
    var skinForm = nameSource.closest("form");
    var nativeSaveButtons = skinForm && skinForm.lastElementChild
      ? Array.prototype.slice.call(skinForm.lastElementChild.querySelectorAll("button")).slice(1) : [];
    var lockedSkinControls = [];

    if (skinForm) {
      // React may repaint disabled controls while another field changes.
      // Guard the native submit path as well as painting its disabled state.
      skinForm.addEventListener("submit", function (event) {
        if (!savingCape) return;
        event.preventDefault();
        event.stopPropagation();
      }, true);
      skinForm.addEventListener("click", function (event) {
        if (!savingCape) return;
        var button = event.target.closest("button");
        if (!group.contains(event.target) && nativeSaveButtons.indexOf(button) < 0) return;
        event.preventDefault();
        event.stopPropagation();
      }, true);
    }

    function setPending(on, lockSkin) {
      pending = on;
      browse.disabled = on;
      save.disabled = on;
      nameInput.disabled = on;
      holder.setAttribute("aria-busy", String(on));
      if (on && lockSkin) {
        savingCape = true;
        lockedSkinControls = nativeSaveButtons.concat(Array.prototype.slice.call(group.querySelectorAll("input")))
          .map(function (control) {
            var previous = control.disabled;
            control.disabled = true;
            return { control: control, disabled: previous };
          });
      } else if (!on) {
        savingCape = false;
        lockedSkinControls.forEach(function (entry) { entry.control.disabled = entry.disabled; });
        lockedSkinControls = [];
      }
    }

    function clearChoice() {
      fileData = null;
      fileName.textContent = "No file chosen";
      preview.removeAttribute("src");
      preview.hidden = true;
      preview.style.display = "none";
    }

    function fileError(message) {
      clearChoice();
      setCapeStatus(message);
      browse.setAttribute("aria-invalid", "true");
    }

    browse.addEventListener("click", async function () {
      if (pending || !holder.isConnected) return;
      setPending(true);
      setCapeStatus("");
      var replacing = false;
      try {
        var chosen = await fetch("/__dialog", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ options: { filters: [".png"] } }),
        }).then(function (response) { return response.json(); });
        if (!holder.isConnected) return;
        if (!chosen || chosen.error) throw new Error((chosen && chosen.error) || "Could not choose a cape file.");
        if (!chosen.path) return;
        replacing = true;
        var file = await fetch("/__read_file", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: chosen.path }),
        }).then(function (response) { return response.json(); });
        if (!holder.isConnected) return;
        if (!file || !file.base64) throw new Error((file && file.error) || "Could not read that file.");
        var data = "data:image/png;base64," + file.base64;
        var dims = await imageSize(data);
        if (!holder.isConnected) return;
        if (dims.width !== 64 || dims.height !== 32) {
          fileError("That image is " + dims.width + " × " + dims.height + "; a cape must be 64 × 32.");
          return;
        }
        fileData = data;
        fileName.textContent = file.name || "Cape image selected";
        preview.src = data;
        preview.hidden = false;
        preview.style.display = "block";
        if (!nameInput.value) nameInput.value = String(file.name || "").replace(/\.png$/i, "");
      } catch (err) {
        if (holder.isConnected) {
          var message = (err && err.message) || "Could not read that file.";
          if (replacing) fileError(message);
          else setCapeStatus(message);
        }
      } finally {
        if (holder.isConnected) setPending(false);
      }
    });

    save.addEventListener("click", async function (event) {
      event.preventDefault();
      event.stopPropagation();
      if (pending || !holder.isConnected) return;
      setCapeStatus("");
      if (!fileData) {
        setCapeStatus("Choose a 64 × 32 PNG first.");
        browse.setAttribute("aria-invalid", "true");
        browse.focus();
        return;
      }
      setPending(true, true);
      try {
        var response = await fetch("/__capes", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: nameInput.value.trim() || "Cape", data: fileData }),
        });
        var payload = await response.json().catch(function () { return {}; });
        if (!response.ok) throw new Error(payload.error || "Could not save the cape.");
        var saved = Array.isArray(payload.capes) ? payload.capes : [];
        var cape = saved[saved.length - 1];
        var appData = window.__host && window.__host.appData;
        if (appData && appData.skins) appData.skins.capes = saved;
        if (window.__host && typeof window.__host.push === "function") window.__host.push("skins");
        if (!holder.isConnected) return;
        // Complete selection in this response turn before announcing success
        // or accepting the native skin's Save action.
        setPending(false);
        updateSkinCapePicker(dialog, saved, cape && cape.id);
        clearChoice();
        setCapeStatus("Cape added and selected. Finish the skin, then choose Save or Save & Use.", true);
      } catch (err) {
        if (holder.isConnected) setCapeStatus((err && err.message) || "Could not save the cape.");
      } finally {
        if (holder.isConnected) setPending(false);
      }
    });
  }

  function updateSkinCapePicker(dialog, capes, selectedId) {
    if (!dialog || !dialog.isConnected) return;
    var group = dialog.querySelector('[role="radiogroup"][aria-labelledby="cape_label"]');
    if (!group) return;
    function pickerRadios() {
      return Array.prototype.slice.call(group.querySelectorAll('input[name="capeId"],input[data-hx-cape-id]'));
    }
    function syncChecked(selected) {
      pickerRadios().forEach(function (radio) {
        radio.checked = radio === selected;
        radio.setAttribute("aria-checked", String(radio === selected));
      });
    }
    function nativeChange(radio) {
      var source = group.querySelector('input[name="capeId"]');
      var key = source && Object.keys(source).find(function (name) { return name.indexOf("__reactProps$") === 0; });
      var onChange = key && source[key] && source[key].onChange;
      if (typeof onChange === "function") {
        onChange({ target: { name: "capeId", id: radio.id, type: "radio", value: radio.value, checked: true } });
      }
    }
    if (!group.hasAttribute("data-hx-cape-picker")) {
      group.setAttribute("data-hx-cape-picker", "true");
      group.addEventListener("change", function (event) {
        var radio = event.target;
        if (radio && radio.name === "capeId" && radio.checked) syncChecked(radio);
      });
      group.addEventListener("keydown", function (event) {
        var step = /^(ArrowRight|ArrowDown)$/.test(event.key) ? 1 : /^(ArrowLeft|ArrowUp)$/.test(event.key) ? -1 : 0;
        if (!step) return;
        var radios = pickerRadios().filter(function (radio) { return !radio.disabled; });
        var index = radios.indexOf(event.target);
        if (index < 0 || !radios.length) return;
        event.preventDefault();
        var next = radios[(index + step + radios.length) % radios.length];
        next.focus();
        next.scrollIntoView({ block: "nearest" });
        next.click();
      });
    }
    var selectedRadio = null;

    (Array.isArray(capes) ? capes : []).forEach(function (cape) {
      if (!cape || !cape.id || cape.id === "no_cape" || cape.id === "mojang_cape") return;
      var existing = pickerRadios().find(function (radio) {
        return radio.value === cape.id;
      });
      if (existing) {
        if (cape.id === selectedId) selectedRadio = existing;
        return;
      }
      var card = group.querySelector('[role="radio"]');
      var wrapper = card && card.parentElement && card.parentElement.parentElement;
      if (!wrapper) return;
      var clone = wrapper.cloneNode(true);
      var radio = clone.querySelector('input[name="capeId"]');
      var label = radio && Array.prototype.find.call(clone.querySelectorAll("label"), function (item) {
        return item.htmlFor === radio.id;
      });
      var image = clone.querySelector("img");
      if (!radio || !label || !image) return;
      clone.querySelectorAll("[id]").forEach(function (node) { node.removeAttribute("id"); });
      radio.id = "harness_cape_" + String(cape.id).replace(/[^a-z0-9_-]/gi, "_");
      // React restores managed radio groups after clicks and rejects a DOM
      // clone with the same name. The native form still receives capeId;
      // the added cards keep a separate browser group and forward that field.
      radio.name = "__hx-capeId";
      radio.setAttribute("data-hx-cape-id", cape.id);
      radio.value = cape.id;
      radio.checked = false;
      radio.removeAttribute("checked");
      radio.setAttribute("aria-checked", "false");
      label.htmlFor = radio.id;
      label.textContent = cape.alias || "Cape";
      image.src = cape.capeImage || cape.url || "";
      image.alt = cape.alias || "Cape";
      radio.addEventListener("change", function (event) {
        if (!radio.checked) return;
        syncChecked(radio);
        nativeChange(radio);
      });
      clone.addEventListener("click", function (event) {
        if (event.target === radio || event.target.closest("label")) return;
        event.preventDefault();
        radio.click();
      });
      group.appendChild(clone);
      if (cape.id === selectedId) selectedRadio = radio;
    });
    if (selectedRadio) {
      // A real radio click updates both React's form state and DOM selection.
      selectedRadio.checked = false;
      selectedRadio.click();
    }
  }

  function imageSize(src) {
    return new Promise(function (resolve) {
      var img = new Image();
      img.onload = function () { resolve({ width: img.naturalWidth, height: img.naturalHeight }); };
      img.onerror = function () { resolve({ width: 0, height: 0 }); };
      img.src = src;
    });
  }

  // --- modpack import / export (Installations page) ------------------------
  // A modpack is a `.mrpack` published on Modrinth. The page has no network
  // (`bridge.js` localises every remote URL), so the catalog and the download
  // happen on the host through `/__modpacks/*`; importing writes the pack into
  // its own game folder and adds an installation -- the same profile write the
  // New installation flow performs. Every element here is cloned from the page
  // itself (the toolbar button, the search field, the installation rows and
  // their Play buttons), so the dialog is the launcher's own furniture.
  function countText(value) {
    var n = Number(value) || 0;
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "K";
    return String(n);
  }

  function sizeText(bytes) {
    var n = Number(bytes) || 0;
    if (n >= 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + " MB";
    if (n >= 1024) return Math.round(n / 1024) + " KB";
    return n + " B";
  }

  function modpackCancelled(st) {
    return st && (st.phase === "cancelled" || st.controlStatus === "cancelled");
  }

  // Controls belong to one host job, including after its dialog is reopened.
  // Status capabilities are authoritative during acquisition and commit.
  function modpackJobControls(s, kind, status, onStatus, onError) {
    var box = document.createElement("div");
    box.className = "__hx-job-controls";
    box.hidden = true;
    var reason = document.createElement("p");
    reason.className = "__hx-note";
    reason.id = "__hx-" + kind + "-control-reason";
    reason.setAttribute("data-hx-job-control-reason", "");
    reason.setAttribute("role", "status");
    reason.hidden = true;
    var ownedId = null, state = null, busy = false, serial = 0;
    var buttons = {};
    ["pause", "resume", "cancel"].forEach(function (action) {
      var button = s.button(action === "pause" ? "Pause" : action === "resume" ? "Resume" : "Cancel download", false, function () { control(action); });
      button.setAttribute("data-hx-job-action", action);
      button.setAttribute("aria-describedby", reason.id);
      button.disabled = true;
      buttons[action] = button;
      box.appendChild(button);
    });
    status.insertAdjacentElement("afterend", box);
    box.insertAdjacentElement("afterend", reason);
    function paint() {
      var active = state && state.job === kind && !modpackCancelled(state);
      box.hidden = !active;
      var paused = state && (state.paused || state.controlStatus === "paused");
      buttons.pause.hidden = !!paused;
      buttons.resume.hidden = !paused;
      buttons.pause.disabled = busy || !ownedId || !active || !state.canPause;
      buttons.resume.disabled = busy || !ownedId || !active || !state.canResume;
      buttons.cancel.disabled = busy || !ownedId || !active || !state.canCancel;
      var note = active && (state.controlReason || (state.cancelRequested ? "Cancellation requested. Waiting for this job to stop safely…" :
        paused ? "Paused. Verified files are kept; resume to continue." : !ownedId ? "Waiting for the host to identify this job…" : ""));
      reason.textContent = note || "";
      reason.hidden = !note;
    }
    function update(st) {
      if (ownedId && st && st.jobId && ownedId !== st.jobId) return false;
      if (!ownedId && st && st.job === kind && st.jobId) ownedId = st.jobId;
      state = st;
      paint();
      return true;
    }
    function reset(id) {
      serial++;
      busy = false;
      ownedId = id || null;
      state = null;
      paint();
    }
    async function control(action) {
      if (!s.el.isConnected || !ownedId || busy || buttons[action].disabled || !state || state.job !== kind) return;
      var token = ++serial, id = ownedId;
      busy = true;
      paint();
      try {
        var response = await fetch("/__modpacks/control", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ jobId: id, action: action })
        });
        var out = await response.json();
        if (token !== serial || !s.el.isConnected || id !== ownedId) return;
        busy = false;
        if (!out || out.error || out.ok === false) {
          paint();
          onError((out && out.error) || "That control is unavailable for this job.");
        } else {
          var snapshot = out.status || (Object.prototype.hasOwnProperty.call(out, "job") ? out : null);
          if (snapshot) onStatus(snapshot);
          else paint();
        }
      } catch (error) {
        if (token !== serial || !s.el.isConnected) return;
        busy = false;
        paint();
        onError((error && error.message) || "Could not control the download. Try again.");
      }
    }
    s.el.addEventListener("__hxclose", function () { reset(); }, { once: true });
    return { update: update, reset: reset };
  }

  function loaderLabel(loader) {
    var name = String(loader || "").replace(/-loader$/, "");
    return name ? name.charAt(0).toUpperCase() + name.slice(1) : "";
  }

  // The two text lines every installation row shows -- its name and its
  // version -- as the elements carrying them. The row's shape changes between
  // builds (a modded installation wraps its icon differently), but the name
  // is always the first text-only div and the version the second; the button
  // labels are spans and stay out of this list.
  function rowTextLines(row) {
    var lines = [];
    Array.prototype.forEach.call(row.querySelectorAll("div"), function (div) {
      if (!div.children.length && (div.textContent || "").trim()) lines.push(div);
    });
    return lines;
  }

  // The native Marketplace Pass feature band has three fixed 240px cards,
  // 72px gaps and 300px artwork offset 30px beyond each card. Keep that
  // furniture and its wide layout, but let the band wrap inside a small
  // desktop window. The closing artwork also needs its intrinsic width
  // bounded by the content column; its aspect ratio remains unchanged.
  function injectMarketplaceLayout() {
    if (location.pathname !== "/games/bedrock/marketplace_pass") return;
    function setStyle(node, property, value) {
      if (node.style[property] !== value) node.style[property] = value;
    }
    var first = document.querySelector('main img[src*="marketplacepass/featureCard_1."]');
    var group = first && first.parentElement && first.parentElement.parentElement &&
      first.parentElement.parentElement.parentElement;
    if (group && group.children.length === 3 &&
        group.querySelectorAll('img[src*="marketplacepass/featureCard_"]').length === 3) {
      setStyle(group.parentElement, "maxWidth", "100%");
      setStyle(group, "maxWidth", "100%");
      setStyle(group, "boxSizing", "border-box");
      setStyle(group, "paddingInline", "30px");
      setStyle(group, "flexWrap", "wrap");
      setStyle(group, "justifyContent", "center");
    }
    var artwork = document.querySelector('main img[src*="marketplacepass/followTheLight."]');
    if (artwork) {
      setStyle(artwork, "maxWidth", "100%");
      setStyle(artwork, "height", "auto");
    }
  }

  // The native Realms row combines a 420px minimum copy column with artwork
  // sized from its 350px maximum height. When the row cannot fit both, reserve
  // the original copy width and scale the art into the remaining space. Its
  // aspect ratio and the native two-column composition stay intact; wider
  // windows use the original rules again.
  function injectRealmsLayout() {
    if (!/^\/games\/(java|bedrock)\/realms$/.test(location.pathname)) return;
    var artwork = document.querySelector('main img[src*="realms/javaRealmsUpsell."],main img[src*="realms/bedrockRealmsUpsell."]');
    var row = artwork && artwork.parentElement;
    var copy = artwork && artwork.nextElementSibling;
    if (!row || row.children.length !== 2 || !copy || copy.children.length !== 3) return;
    if (!artwork.naturalWidth || !artwork.naturalHeight) {
      if (!artwork.__hxRealmsLoadBound) {
        artwork.__hxRealmsLoadBound = true;
        artwork.addEventListener("load", scan, { once: true });
      }
      return;
    }
    var minimumCopy = parseFloat(getComputedStyle(copy).minWidth);
    var maximumArtHeight = parseFloat(getComputedStyle(artwork).maxHeight);
    if (!(minimumCopy > 0) || !(maximumArtHeight > 0) || row.clientWidth <= minimumCopy) return;
    var nativeArtWidth = Math.min(artwork.naturalWidth,
      maximumArtHeight * artwork.naturalWidth / artwork.naturalHeight);
    var compact = row.clientWidth < minimumCopy + nativeArtWidth;
    var width = compact ? "calc(100% - " + minimumCopy + "px)" : "";
    var height = compact ? "auto" : "";
    if (artwork.style.width !== width) artwork.style.width = width;
    if (artwork.style.height !== height) artwork.style.height = height;
  }

  // The native Java bar centres Play and Quick Play while anchoring the
  // installation picker at its left edge. Its fixed 280px menu wrapper can
  // overlap Play at the minimum window size. Keep the original control and
  // menu recipes, narrowing only the picker wrapper when the two collide.
  var playSelectorObserver = null;
  var playSelectorTargets = [];
  function injectJavaPlayLayout() {
    var label = location.pathname === "/games/java/play" && document.getElementById("installation-select-label");
    var trigger = label && label.parentElement.querySelector('[role="button"][aria-controls="toggle-list-menu"]');
    var cta = trigger && document.querySelector("main #bigCTAButton");
    var wrapper = trigger && trigger.parentElement;
    var bar = wrapper;
    while (bar && !bar.contains(cta)) bar = bar.parentElement;
    if (!trigger || !cta || !wrapper || !bar) {
      if (playSelectorObserver) playSelectorObserver.disconnect();
      playSelectorObserver = null;
      playSelectorTargets = [];
      return;
    }
    var pickerRect = trigger.getBoundingClientRect();
    var ctaRect = cta.getBoundingClientRect();
    if (!pickerRect.width || !ctaRect.width) return;
    var native = wrapper.__hxPlaySelectorLayout;
    if (!native) {
      native = { width: wrapper.getBoundingClientRect().width, inline: {} };
      ["width", "min-width", "max-width"].forEach(function (property) {
        native.inline[property] = { value: wrapper.style.getPropertyValue(property), priority: wrapper.style.getPropertyPriority(property) };
      });
      wrapper.__hxPlaySelectorLayout = native;
    }
    var available = ctaRect.left - pickerRect.left - 8;
    var verticalOverlap = pickerRect.top < ctaRect.bottom && pickerRect.bottom > ctaRect.top;
    // Compare against the cached native width, not the already narrowed
    // rectangle, so resize notifications cannot alternate narrow and wide.
    var narrow = verticalOverlap && available > 0 && available < native.width;
    var width = Math.floor(available) + "px";
    ["width", "min-width", "max-width"].forEach(function (property) {
      var value = narrow ? width : native.inline[property].value;
      var priority = native.inline[property].priority;
      if (wrapper.style.getPropertyValue(property) !== value || wrapper.style.getPropertyPriority(property) !== priority) {
        if (value) wrapper.style.setProperty(property, value, priority);
        else wrapper.style.removeProperty(property);
      }
    });
    if (typeof ResizeObserver !== "undefined" &&
        (playSelectorTargets[0] !== wrapper || playSelectorTargets[1] !== cta || playSelectorTargets[2] !== bar)) {
      if (playSelectorObserver) playSelectorObserver.disconnect();
      playSelectorObserver = new ResizeObserver(scan);
      playSelectorTargets = [wrapper, cta, bar];
      playSelectorTargets.forEach(function (node) { playSelectorObserver.observe(node); });
    }
  }

  // The toolbar entry: "Import modpack", cloned from "New installation" so
  // it is the same element with the same theme, sitting beside it.
  function injectModpackButton() {
    // The native filter form and its adjacent toolbar have stable structure;
    // the New installation label changes with the selected language.
    var label = document.getElementById("versions_search_label");
    var filters = label && label.closest("form");
    var toolbar = filters && filters.parentElement && filters.parentElement.nextElementSibling;
    var add = toolbar && toolbar.querySelector("button");
    if (!add || !add.parentElement || add.parentElement.querySelector("#__hx-modpacks")) return;
    var clone = add.cloneNode(true);
    clone.id = "__hx-modpacks";
    clonedButtonLabel(clone, "Import modpack");
    add.setAttribute("data-hx-action", "create-installation");
    // The launcher keeps 8 px between adjacent buttons in a row (measured off
    // the installation rows' Play / Locate / More group).
    clone.style.marginLeft = "8px";
    clone.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      openModpackDialog();
    });
    add.parentElement.appendChild(clone);
  }

  function openModpackDialog() {
    var s = screen("Import modpack", 980);
    s.add('<p class="__hx-note">Modpacks are served from Modrinth. Importing one downloads it ' +
      "into its own game folder and adds an installation for it. You can also import a local .mrpack file; files named by its index may need downloading.</p>");

    // The search field is the page's own (wrapper, magnifier icon and all),
    // cloned whole and recoded: id, placeholder and aria-label change, and
    // nothing about its inner layout is overridden, so the icon keeps its
    // padding and the field reads exactly as it does on Installations.
    var searchRow = document.createElement("div");
    searchRow.className = "__hx-row";
    var label = document.createElement("label");
    label.setAttribute("for", "__hx-modpack-search");
    label.textContent = "Search";
    searchRow.appendChild(label);
    var controls = document.createElement("div");
    controls.style.display = "flex";
    controls.style.alignItems = "center";
    controls.style.gap = "8px";
    var input = null;
    var searchLabel = document.getElementById("versions_search_label");
    var source = searchLabel && searchLabel.parentElement && searchLabel.parentElement.querySelector("input");
    if (source && source.parentElement) {
      var wrapper = source.parentElement.cloneNode(true);
      wrapper.removeAttribute('id');
      wrapper.querySelectorAll('[id]').forEach(function (element) { element.removeAttribute('id'); });
      var hadResults = !!wrapper.querySelector('.results-tag');
      wrapper.querySelectorAll('.results-tag,[role="status"]').forEach(function (element) { element.remove(); });
      input = wrapper.querySelector("input");
      input.id = "__hx-modpack-search";
      input.value = "";
      input.placeholder = "Search for modpacks";
      input.setAttribute("aria-label", "Search for modpacks");
      if (hadResults) input.style.paddingRight = '32px';
      wrapper.style.flex = "1 1 auto";
      wrapper.style.width = "auto";
      wrapper.style.minWidth = "0";
      controls.appendChild(wrapper);
    } else {
      input = document.createElement("input");
      input.type = "text";
      input.id = "__hx-modpack-search";
      input.placeholder = "Search for modpacks";
      input.spellcheck = false;
      input.style.flex = "1 1 auto";
      controls.appendChild(input);
    }
    var clearSearch = wrapper && wrapper.querySelector('.reset-button');
    if (!clearSearch) {
      clearSearch = s.button('Clear', false, function () { resetSearch(); });
      controls.appendChild(clearSearch);
    } else {
      clearSearch.style.right = '8px';
      clearSearch.addEventListener('click', function (event) {
        event.preventDefault(); resetSearch();
      });
    }
    clearSearch.setAttribute('aria-label', 'Clear modpack search');
    clearSearch.setAttribute('data-hx-modpack-clear', '');
    clearSearch.style.display = 'none';
    function resetSearch() {
      if (job) return;
      input.value = '';
      clearSearch.style.display = 'none';
      window.clearTimeout(debounce);
      search();
      input.focus();
    }
    // The source switch reuses the page's own filter chips -- two of the
    // Releases / Snapshots / Modded toggles, recoded into a single-select
    // pair beside the field: Modrinth (wired) and CurseForge (a stub until
    // someone wires an API key behind it).
    var provider = "modrinth";
    var chipSource = document.getElementById("filter-release");
    if (chipSource && chipSource.parentElement) {
      var chips = document.createElement("div");
      chips.setAttribute("role", "group");
      chips.setAttribute("aria-label", "Source");
      chips.style.display = "flex";
      chips.style.alignItems = "center";
      chips.style.gap = "8px";
      var mr = chipSource.parentElement.cloneNode(true);
      var cf = chipSource.parentElement.cloneNode(true);
      var mrBox = mr.querySelector("input");
      var cfBox = cf.querySelector("input");
      var mrLabel = mr.querySelector("label");
      var cfLabel = cf.querySelector("label");
      mrBox.id = "__hx-src-modrinth";
      cfBox.id = "__hx-src-curseforge";
      mrBox.setAttribute("aria-label", "Modrinth");
      cfBox.setAttribute("aria-label", "CurseForge");
      if (mrLabel) {
        mrLabel.setAttribute("for", mrBox.id);
        mrLabel.textContent = "Modrinth";
      }
      if (cfLabel) {
        cfLabel.setAttribute("for", cfBox.id);
        cfLabel.textContent = "CurseForge";
      }
      mrBox.checked = true;
      cfBox.checked = false;
      cfBox.removeAttribute("checked");
      // CurseForge has no API key behind it yet, so its chip is disabled for
      // real, not just inert on click. The clone carries the app's own atomic
      // classes, so `:disabled` alone paints the launcher's disabled chip --
      // grey box (#978c88), pale label (#ede5e2) -- and the app's hover and
      // active rules all exclude :disabled, so the dead chip stays dead under
      // the cursor. Removing the checked attribute only affects the box;
      // the style keys off the disabled state, which needs the property.
      cfBox.disabled = true;
      var choose = function (value) {
        if (provider === value) {
          // The chip toggled itself off; a source is always selected.
          if (value === "modrinth") mrBox.checked = true;
          else cfBox.checked = true;
          return;
        }
        provider = value;
        mrBox.checked = value === "modrinth";
        cfBox.checked = value === "curseforge";
        if (value === "modrinth") {
          search();
        } else {
          list.textContent = "";
          offset = 0;
          total = 0;
          seen = {};
          setStatus("CurseForge isn\u2019t available yet.", "muted");
        }
      };
      mrBox.addEventListener("change", function () { choose("modrinth"); });
      cfBox.addEventListener("change", function () { choose("curseforge"); });
      chips.appendChild(mr);
      chips.appendChild(cf);
      controls.appendChild(chips);
    }
    searchRow.appendChild(controls);
    s.body.appendChild(searchRow);
    // The status sits above the results: the list is taller than the dialog
    // viewport, and progress or an error message belongs where the eye is.
    s.add('<p class="__hx-error" id="__hx-mod-status" role="status"></p>');
    s.add('<p class="__hx-note" id="__hx-import-background" hidden>Import continues if you close this window.</p>');
    s.add('<ul class="__hx-mod-results" id="__hx-mod-results"></ul>');
    var list = s.body.querySelector("#__hx-mod-results");
    var status = s.body.querySelector("#__hx-mod-status");
    var backgroundNote = s.body.querySelector("#__hx-import-background");
    var closeButton = s.button("Cancel", false, s.close);
    var closeLabel = closeButton.querySelector("span");

    var searchToken = 0;
    var job = false;
    var poll = null;
    var current = null;
    var jobGeneration = 0;
    var checking = false;
    var recovering = true;
    var jobControls = modpackJobControls(s, "import", status, receiveStatus, function (message) { setStatus(message); });
    var localToolbar = document.createElement('div');
    localToolbar.style.marginBottom = '16px';
    var localButton = s.button('Import from file…', false, function () {
      startImport(null, localButton, localButton.querySelector('span'));
    });
    localButton.setAttribute('data-hx-local-pack-import', '');
    localToolbar.appendChild(localButton);
    s.body.insertBefore(localToolbar, searchRow);

    // Tone: "ok" green for success, "muted" for progress (the launcher's
    // loading states are grey, not red), and red only for real failures.
    function setStatus(text, tone) {
      status.textContent = text || "";
      status.style.color = tone === "ok" ? "#86d562" : tone === "muted" ? "#a7a7a7" : "";
    }

    function setBusy(on) {
      job = on;
      input.disabled = on;
      clearSearch.disabled = on;
      localButton.disabled = on;
      if (mrBox) mrBox.disabled = on;
      closeLabel.textContent = on ? "Close" : "Cancel";
      backgroundNote.hidden = !on;
      Array.prototype.forEach.call(list.querySelectorAll("li button"), function (button) {
        button.disabled = on || button.hasAttribute("data-hx-imported");
      });
    }

    // A page installation row, used as the template for every result. Its
    // Play / Locate / More group is trimmed down to one button, relabelled
    // "Import" -- the row's own primary action, in the row's own theme.
    function templateRow() {
      var native = document.querySelector('[data-hx-installation-template] li');
      if (native) return native;
      var rows = document.querySelectorAll("main ul li");
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].querySelector('button[id="MoreOptionsDropdown"]')) return rows[i];
      }
      return null;
    }

    function modpackRow(hit) {
      var template = templateRow();
      if (!template) return null;
      var li = template.cloneNode(true);
      li.removeAttribute("data-configuration-id");
      li.setAttribute("data-hx-modpack-id", hit.id);
      li.querySelectorAll("[id]").forEach(function (node) { node.removeAttribute("id"); });
      // The native row's action group renders Play before Locate and More.
      var play = li.querySelector("button");
      if (!play || !play.parentElement) return null;
      // Every other control on the row belongs to an installed installation
      // -- Locate and More options mean nothing for a search hit -- so the
      // row keeps exactly one button, relabelled "Import" below.
      Array.prototype.forEach.call(li.querySelectorAll("button"), function (button) {
        if (button !== play) button.remove();
      });
      // The warning badge marks the template row's own modded version
      // document, not this pack; it must not travel with the clone.
      var safety = li.querySelector('[data-testid="player-safety-icon"]');
      if (safety) {
        var holder = safety.parentElement;
        safety.remove();
        if (holder && !holder.children.length && !(holder.textContent || "").trim()) holder.remove();
      }
      var leaf = clonedButtonLabel(play, "Import");
      play.disabled = job;
      play.addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        startImport(hit, play, leaf);
      });
      var image = li.querySelector("img");
      if (image && hit.icon) {
        // Without an icon the row keeps the template's grass block, which is
        // the launcher's own "no icon" state rather than a broken image.
        image.src = hit.icon;
        image.alt = hit.title || "Modpack";
        image.removeAttribute("aria-hidden");
      }
      // The row's text block: the installation's name on the first line and
      // its version on the second (measured off the page's own rows, where a
      // named installation reads "<name>\n<version id>").
      var lines = rowTextLines(li);
      var detail = ["by " + (hit.author || "unknown")];
      if (hit.loader) detail.push(loaderLabel(hit.loader));
      detail.push(countText(hit.downloads) + " downloads");
      if (lines[0]) lines[0].textContent = hit.title || "";
      if (lines[1]) lines[1].textContent = detail.join(" · ");
      var focus = li.querySelector('[tabindex][aria-label]');
      if (focus) focus.setAttribute("aria-label", (hit.title || "Modpack") + " (" + detail.join(" · ") + ")");
      return li;
    }

    // --- paged results -------------------------------------------------
    // The dialog asks for the next window of hits as its list scrolls to the
    // bottom; a fresh search (typing, Enter, or switching back from the stub
    // provider) starts over at offset zero.
    var PAGE = 18;
    var offset = 0;
    var total = Infinity;
    var seen = {};
    var loading = false;
    var autofill = 0;

    function search(reset) {
      if (job || provider !== "modrinth" || !s.el.isConnected) return;
      var token = ++searchToken;
      if (reset !== false) {
        offset = 0;
        total = Infinity;
        seen = {};
        autofill = 0;
        list.textContent = "";
      }
      loading = true;
      setStatus(offset ? "Loading more\u2026" : "Searching Modrinth\u2026", "muted");
      fetch("/__modpacks/search?query=" + encodeURIComponent((input.value || "").trim()) +
            "&offset=" + offset, { cache: "no-store" })
        .then(function (response) { return response.json(); })
        .then(function (doc) {
          if (token !== searchToken || !s.el.isConnected) return;
          loading = false;
          if (!doc || doc.error) {
            setStatus((doc && doc.error) || "Modrinth is unreachable.");
            return;
          }
          var hits = doc.hits || [];
          if (typeof doc.total === "number") total = doc.total;
          if (!hits.length) {
            total = offset; // the catalog had no further pages
            setStatus(offset ? "" : "No modpacks found" +
              ((input.value || "").trim() ? "." : " on Modrinth."), offset ? "" : "muted");
            return;
          }
          setStatus("");
          var missingTemplate = false;
          hits.forEach(function (hit) {
            if (!hit || seen[hit.id]) return;
            var row = modpackRow(hit);
            if (!row) { missingTemplate = true; return; }
            seen[hit.id] = true;
            list.appendChild(row);
          });
          if (missingTemplate) {
            setStatus("Could not display the modpack results. Reload the launcher and try again.");
            return;
          }
          offset += hits.length;
          fillViewport();
        })
        .catch(function (err) {
          if (token !== searchToken || !s.el.isConnected) return;
          loading = false;
          setStatus(String((err && err.message) || err));
        });
    }

    // When the first pages do not reach the dialog's bottom -- a short
    // result list, or a tall window -- keep pulling until they do, so the
    // scroll trigger is always reachable.
    function fillViewport() {
      if (job || loading || provider !== "modrinth" || offset >= total) return;
      if (autofill++ >= 8) return;
      var box = scroller();
      if (box && box.scrollHeight > box.clientHeight + 40) return;
      search(false);
    }

    function scroller() {
      var el = list;
      while (el && el !== document.documentElement) {
        var overflow = window.getComputedStyle(el).overflowY;
        if ((overflow === "auto" || overflow === "scroll") && el.scrollHeight > el.clientHeight) return el;
        el = el.parentElement;
      }
      return null;
    }

    function loadMore() {
      if (job || loading || provider !== "modrinth" || offset >= total) return;
      search(false);
    }

    // Scroll events do not bubble, so the dialog listens in the capture
    // phase: whichever element actually scrolls -- the dialog body, or an
    // overlay wrapper above it -- the trigger still sees it. Each dialog
    // only reacts to scrolling inside itself and stops when disconnected.
    s.el.addEventListener(
      "scroll",
      function (event) {
        if (!s.el.isConnected) return;
        var box = event.target;
        if (!box || (box !== s.el && !s.el.contains(box) && !box.contains(s.el))) return;
        if (box.scrollTop + box.clientHeight >= box.scrollHeight - 240) loadMore();
      },
      true
    );

    function startImport(hit, button, label) {
      if (job || !s.el.isConnected) return;
      var generation = ++jobGeneration;
      invalidateSearch();
      setBusy(true);
      current = { button: button, label: label, original: label.textContent, reusable: !hit };
      label.textContent = "Importing…";
      setStatus(hit ? "Starting the import…" : "Choose a local .mrpack file…", "muted");
      fetch(hit ? "/__modpacks/import" : "/__modpacks/import-file", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(hit ? { projectId: hit.id } : {}),
      })
        .then(function (response) { return response.json(); })
        .then(function (out) {
          if (generation !== jobGeneration || !job || !s.el.isConnected) return;
          if (out && out.error) {
            fail(out.error);
            return;
          }
          if (out && out.cancelled) {
            stopPolling(); setBusy(false);
            if (current) current.label.textContent = current.original;
            current = null;
            setStatus('File selection was cancelled.', 'muted');
            return;
          }
          if (!out || !out.ok || out.started === false) {
            fail('The import could not be started. Try again.');
            return;
          }
          jobControls.reset(out.jobId);
          poll = window.setInterval(check, 400);
          check();
        })
        .catch(function (err) {
          if (generation === jobGeneration && job && s.el.isConnected) fail(String((err && err.message) || err));
        });
    }

    function invalidateSearch() {
      searchToken++;
      loading = false;
      window.clearTimeout(debounce);
    }

    function stopPolling() {
      window.clearInterval(poll);
      poll = null;
      jobGeneration++;
      checking = false;
      jobControls.reset();
    }

    function check() {
      if (!job || !s.el.isConnected) {
        stopPolling();
        return;
      }
      if (checking || recovering) return;
      checking = true;
      var generation = jobGeneration;
      fetch("/__modpacks/status", { cache: "no-store" })
        .then(function (response) { return response.json(); })
        .then(function (st) {
          if (generation !== jobGeneration || !job) return;
          if (!s.el.isConnected) {
            stopPolling();
            return;
          }
          receiveStatus(st);
        })
        .catch(function () { /* the host answers on the next tick */ })
        .finally(function () { if (generation === jobGeneration) checking = false; });
    }

    function receiveStatus(st) {
      if (!job || !s.el.isConnected) return;
      if (!jobControls.update(st)) { fail("This import stopped. A different job is now active."); return; }
      if (modpackCancelled(st)) {
        stopPolling(); setBusy(false);
        if (current) current.label.textContent = current.original;
        current = null;
        setStatus("Import cancelled. No installation was added.", "muted");
        return;
      }
      if (!st || st.job !== "import") {
        if (st && st.phase === "done" && st.result && !st.cancelRequested) succeed(st.result);
        else fail((st && st.error) || "The import stopped.");
        return;
      }
      var text = st.cancelRequested ? "Cancelling the import…" : st.paused || st.controlStatus === "paused" ? "Import paused." : st.controlStatus === "pause_requested" ? "Pausing the import…" : st.message || "Importing…";
      if (st.total) text += " (" + st.done + "/" + st.total + ")";
      setStatus(text, "muted");
    }

    function succeed(result) {
      stopPolling();
      setBusy(false);
      setStatus("Imported " + result.name + " — " + countText(result.files) + " files, " +
        sizeText(result.bytes) + ". It is now in your installations.", "ok");
      if (current) {
        current.label.textContent = current.reusable ? current.original : "Imported";
        if (!current.reusable) {
          current.button.setAttribute("data-hx-imported", "true");
          current.button.disabled = true;
        }
        current = null;
      }
      // The installations list reads these two slices; mirroring the host's
      // answer into them and firing the slice events is what makes the new
      // installation appear without a reload -- the same lever the cape
      // upload pulls for the skin library.
      var host = window.__host;
      var seed = host && host.appData;
      if (seed && typeof host.push === "function") {
        if (result.gameVersions) {
          seed.gameVersions = result.gameVersions;
          host.push("gameVersions");
        }
        if (result.profiles) {
          if (!seed.configurationData) seed.configurationData = {};
          seed.configurationData.profiles = result.profiles;
          host.push("configurationData");
        }
      }
    }

    function fail(message) {
      stopPolling();
      setBusy(false);
      setStatus(message || "The import failed.");
      if (current) {
        current.label.textContent = current.original;
        current = null;
      }
    }

    var debounce = null;
    input.addEventListener("input", function () {
      clearSearch.style.display = input.value ? '' : 'none';
      window.clearTimeout(debounce);
      debounce = window.setTimeout(search, 350);
    });
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        window.clearTimeout(debounce);
        search();
      }
    });
    input.focus();
    search();
    // An import that outlives its dialog keeps running on the host; reopening
    // picks its progress back up instead of allowing a second job.
    var resumeGeneration = jobGeneration;
    fetch("/__modpacks/status", { cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (st) {
        if (resumeGeneration === jobGeneration && st && st.job === "import" && s.el.isConnected && !job && !poll) {
          jobGeneration++;
          invalidateSearch();
          setBusy(true);
          jobControls.update(st);
          poll = window.setInterval(check, 400);
          check();
        }
      })
      .catch(function () { /* no host job */ }).finally(function () {
        recovering = false;
        if (job && poll && s.el.isConnected) check();
      });
  }

  // "Export modpack" joins the row's own More options menu, beside Edit and
  // Duplicate, cloned from those items so it is styled by the app itself.
  // The menu is portaled out of the row (it renders at the body level), so the
  // row is remembered when its trigger button is pressed -- the only moment the
  // connection between menu and installation is visible in the DOM.
  var menuRow = null;
  document.addEventListener(
    "click",
    function (event) {
      var button = event.target && event.target.closest
        ? event.target.closest('button[id="MoreOptionsDropdown"]')
        : null;
      if (button && !button.closest("#__hx-screen")) menuRow = button.closest("li");
      var create = event.target && event.target.closest
        ? event.target.closest('[data-hx-action="create-installation"]') : null;
      if (create) editProfileRow = null;
    },
    true
  );

  // The row whose More-options menu returned Edit: the menu is portalled out
  // of the row, so the row is captured at the moment its Edit item is pressed,
  // the only instant the connection is visible in the DOM.
  var editProfileRow = null;
  document.addEventListener(
    "click",
    function (event) {
      var item = event.target && event.target.closest
        ? event.target.closest('#MoreOptionsDropdownMenu [role="menuitem"]')
        : null;
      var menu = item && document.getElementById("MoreOptionsDropdownMenu");
      if (item && menu && item === menu.querySelector('[role="menuitem"]')) {
        editProfileRow = menuRow;
        if (window.__installationWorkspace) {
          event.preventDefault();
          event.stopImmediatePropagation();
          var profile = profileForRow(menuRow);
          var opener = menuRow && menuRow.querySelector('button[id="MoreOptionsDropdown"]');
          if (menu.parentElement) menu.remove();
          if (profile && profile.id) window.__installationWorkspace.open(profile.id, opener);
          else {
            var unavailable = screen("Edit installation");
            unavailable.add('<p class="__hx-error" role="alert">This installation could not be identified. Refresh the installation list and try again.</p>');
            unavailable.button("Close", false, unavailable.close);
          }
        }
      }
    },
    true
  );

  function injectExportMenuItem() {
    var menu = document.getElementById("MoreOptionsDropdownMenu");
    if (!menu || menu.querySelector("#__hx-export-item")) return;
    var item = menu.querySelector('[role="menuitem"]');
    if (!item) return;
    item.setAttribute("data-hx-action", "edit-installation");
    var clone = item.cloneNode(true);
    clone.id = "__hx-export-item";
    clone.textContent = "Export modpack";
    clone.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      if (menu.parentElement) menu.remove();
      openExportDialog(profileForRow(menuRow));
    });
    (item.parentElement || menu).appendChild(clone);
  }

  // --- mod loader picker in the installation forms ------------------------
  // The launcher's own form only lists vanilla versions; installing a loader
  // means writing the loader's flattened version document into the local
  // version store and pointing the profile at it -- the same write a modpack
  // import performs. Fabric and Quilt publish that document from their meta
  // services; Forge and NeoForge produce it through their official installers,
  // run host-side against a staging folder (harness/modpacks.py). The row
  // injects between Version and Game directory with one chip per loader. It serves both dialogs of the
  // page: "Create new installation" and "Edit installation". On Edit the
  // profile's own loader (read from lastVersionId) is preselected, and Save
  // with nothing changed submits as-is. Pressing Install/Save with a loader
  // armed materializes it first (`/__loaders/materialize`), surfaces the
  // outcome in-row, and only then lets the form submit; the bridge swaps
  // lastVersionId for the materialized id. Everything below is cloned from
  // the dialog's own furniture: the row wrapper from a form row, the label
  // from the Name label, the chips from the Browse button, and the selected
  // state's colors from the primary (Install/Save) button.
  var LOADER_CHOICES = [
    { id: null, label: "Vanilla" },
    { id: "fabric-loader", label: "Fabric" },
    { id: "quilt-loader", label: "Quilt" },
    { id: "forge", label: "Forge" },
    { id: "neoforge", label: "NeoForge" },
  ];
  var loaderDialogNode = null;    // the dialog the current choice belongs to
  var loaderVersionFor = "";      // minecraft+loader the loaded list belongs to
  var loaderVersionList = null;   // {versions:[], recommended} once fetched

  // The loader id shapes a profile's lastVersionId can carry:
  //   fabric-loader-<build>-<minecraft>, quilt-loader-<build>-<minecraft>,
  //   forge-<minecraft>-<build> and neoforge-<build>.
  // A modpack import and this picker's own materialization both write
  // documents in these shapes.
  var LOADER_ID_PREFIXES = ["fabric-loader-", "quilt-loader-", "forge-", "neoforge-"];

  function splitLoaderVersionId(id) {
    var text = String(id || "");
    for (var i = 0; i < LOADER_ID_PREFIXES.length; i++) {
      var prefix = LOADER_ID_PREFIXES[i];
      if (text.indexOf(prefix) !== 0) continue;
      var rest = text.slice(prefix.length);
      var split = rest.indexOf("-");
      if (split <= 0) {
        // `neoforge-21.1.2`: the build carries no dash; the Minecraft
        // version comes from the dialog or the profile's own metadata.
        return { loader: prefix.replace(/-$/, ""), minecraft: "",
          loaderVersion: rest || null };
      }
      if (prefix === "forge-") {
        // `forge-<minecraft>-<build>`: the Minecraft version comes first.
        return { loader: "forge", minecraft: rest.slice(0, split),
          loaderVersion: rest.slice(split + 1) };
      }
      return { loader: prefix.replace(/-$/, ""), minecraft: rest.slice(split + 1),
        loaderVersion: rest.slice(0, split) };
    }
    return null;
  }

  // Meta services know the loader as fabric-loader / quilt-loader, but a
  // saved pack index may spell the same thing the way Modrinth search
  // categories do; both forms map onto this picker's chip ids.
  function normalizeLoader(loader) {
    var text = String(loader || "");
    if (text === "fabric") return "fabric-loader";
    if (text === "quilt") return "quilt-loader";
    return text;
  }

  // The loader pair an Edit dialog has to preselect. The live lastVersionId
  // is the truth -- edits write it without touching any meta the profile
  // carries -- so its shape answers first; the metadata a modpack import
  // recorded is only the fallback for profiles whose id is plain vanilla.
  function profileLoaderBaseline(profile) {
    if (!profile || typeof profile !== "object") return null;
    var parsed = splitLoaderVersionId(profile.lastVersionId);
    var meta = (profile.modpack || {});
    if (parsed) {
      // NeoForge IDs encode the loader build alone. Imported packs retain
      // their base release; manual installs expose it from the host's local
      // version document. IDs that already encode Minecraft keep that truth.
      if (!parsed.minecraft) {
        var host = window.__host && window.__host.appData;
        var versions = (host && host.gameVersions && host.gameVersions.versions) || {};
        var installed = versions[profile.lastVersionId] || {};
        parsed.minecraft = String(meta.minecraft || installed.minecraftVersion || "");
      }
      return parsed;
    }
    if (meta.loader && meta.loaderVersion) {
      var loader = normalizeLoader(meta.loader);
      var known = LOADER_CHOICES.some(function (c) { return c.id === loader; });
      if (known) {
        return { loader: loader, loaderVersion: String(meta.loaderVersion),
          minecraft: String(meta.minecraft || "") };
      }
    }
    return null;
  }

  function baseMinecraftVersion(dialog) {
    // The version control shows its selection as the search field's
    // placeholder: "Latest release (26.3)", "release 26.2", or a raw loader
    // id. The form holds the real id internally; this maps the label back to
    // what the materialize route can resolve (it aliases the two "latest"
    // forms against the manifest itself).
    var input = dialog.querySelector('[data-testid="toggle-list-search"]');
    var text = input ? String(input.placeholder || "").trim() : "";
    var host = window.__host && window.__host.appData;
    var versions = (host && host.gameVersions && host.gameVersions.versions) || {};
    var known = Object.keys(versions).sort(function (a, b) { return b.length - a.length; });
    for (var i = 0; i < known.length; i++) {
      var id = known[i];
      if (text === id || text.slice(-(id.length + 1)) === " " + id ||
          text.slice(-(id.length + 2)) === "(" + id + ")") return id;
    }
    // Latest labels are translated, but the concrete id in parentheses is not.
    var concrete = text.match(/\(([^()\s]+)\)\s*$/);
    if (concrete) return concrete[1];
    if (/^latest release\b/i.test(text)) return "latest-release";
    if (/^latest snapshot\b/i.test(text)) return "latest-snapshot";
    return text.replace(/^(release|snapshot)\s+/i, "") || "latest-release";
  }

  function loaderStatusLabel(id) {
    var choice = LOADER_CHOICES.filter(function (c) { return c.id === id; })[0];
    return choice ? choice.label : "Vanilla";
  }

  // Ask the host which loader versions a meta service publishes for a given
  // Minecraft version, newest first with its own recommended pick. The result
  // is cached per (minecraft, loader) so re-selecting a chip is instant.
  function fetchLoaderVersions(minecraft, loader) {
    var key = minecraft + "|" + loader;
    if (loaderVersionFor === key && loaderVersionList) {
      return Promise.resolve(loaderVersionList);
    }
    return fetch("/__loaders/versions?minecraft=" + encodeURIComponent(minecraft) +
      "&loader=" + encodeURIComponent(loader))
      .then(function (r) { return r.json(); })
      .then(function (out) {
        if (out && out.versions) {
          loaderVersionFor = key;
          loaderVersionList = out;
          return out;
        }
        throw new Error((out && out.error) || "no loader versions");
      });
  }

  function injectLoaderPicker() {
    var dialog = document.getElementById("modal-dialog");
    if (!dialog) {
      loaderDialogNode = null;
      window.__hxLoaderChoice = null;
      window.__hxArmedLoader = null;
      return;
    }
    // The same dialog element serves Create and Edit, so the element being
    // new decides when the choice starts over: a fresh dialog starts on
    // Vanilla with nothing armed. On Edit the profile under edit answers the
    // preselection and gates Save.
    var nameInput = dialog.querySelector("#configuration_name");
    var gameInput = dialog.querySelector("#configuration_game_dir");
    var versionInput = dialog.querySelector('[data-testid="toggle-list-search"]');
    var form = dialog.closest("form") || dialog.querySelector("form");
    if (!nameInput || !gameInput || !versionInput || !form ||
        !dialog.querySelector("#version-select-label")) return;
    var isEdit = !form.querySelector("#toggleLaunchAfterInstallGlyph");
    if (loaderDialogNode !== dialog) {
      loaderDialogNode = dialog;
      window.__hxLoaderChoice = null;
      window.__hxArmedLoader = null;
    }
    if (dialog.querySelector("#__hx-loader-row")) return;
    // Mutable picks and bypasses belong to this form, never a later dialog.
    var chosenLoaderVersion = null;
    var loaderBypass = false;
    var loaderVersionsLoading = false;
    var loaderVersionsAvailable = false;
    var versionRequest = 0;
    var preparationRequest = 0;
    var preparing = false;
    var currentMinecraft = null;
    var formObserver = null;
    var outsideLoaderClick = null;
    var loaderNavigationKeyDown = null;
    // The Edit menu item knows the row it belongs to (see editProfileRow
    // below); the dialog itself only carries the profile's name.
    var editBaseline = isEdit ? profileLoaderBaseline(profileForRow(editProfileRow))
      : null;

    // The row wrapper carries build-hashed classes that change between loads,
    // so rows are found structurally: the form column's direct children are
    // the rows, each one a label wrapper plus its control.
    function fieldRow(input) {
      var node = input.parentElement;
      while (node && node !== dialog) {
        if (node.querySelector("label")) return node;
        node = node.parentElement;
      }
      return null;
    }
    var nameRow = fieldRow(nameInput);
    var versionRow = fieldRow(versionInput);
    var gameRow = fieldRow(gameInput);
    var browseSource = gameRow && gameRow.querySelector("button");
    var install = form.querySelector('button[type="submit"]');
    if (!versionRow || !browseSource || !install) return;

    // Selected-chip colors, measured off the primary button so the picker
    // reads as the dialog's own furniture in every theme state.
    var primary = getComputedStyle(install);
    var onBackground = primary.backgroundColor;
    var onColor = primary.color;

    var row = versionRow.cloneNode(false);
    row.id = "__hx-loader-row";
    function currentDialog() {
      return dialog.isConnected && row.isConnected && loaderDialogNode === dialog;
    }
    function invalidatePreparation() {
      preparationRequest++;
      if (preparing && install.isConnected) install.disabled = false;
      preparing = false;
      window.__hxArmedLoader = null;
    }

    var labelSource = (nameRow && nameRow.querySelector("label")) || versionRow.querySelector("label");
    var label = labelSource.cloneNode(true);
    label.removeAttribute("id");
    label.removeAttribute("for");
    label.textContent = "Mod loader";
    row.appendChild(label);

    var chips = document.createElement("div");
    chips.style.cssText = "display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;";

    function reflect() {
      var selected = window.__hxLoaderChoice || null;
      Array.prototype.forEach.call(chips.children, function (chip) {
        var on = (chip.getAttribute("data-loader") || null) === selected;
        chip.setAttribute("aria-pressed", on ? "true" : "false");
        chip.style.background = on ? onBackground : "";
        chip.style.color = on ? onColor : "";
      });
    }

    // Selects a loader in this dialog: marks the chip, shows the version
    // control, and refills its list from the meta service for whatever base
    // version the dialog currently resolves. Shared by the chips' own click
    // and by Edit's preselection of the profile's installed loader.
    function armLoader(choice, baseVersion, preferredVersion) {
      var request = ++versionRequest;
      invalidatePreparation();
      window.__hxLoaderChoice = choice.id;
      chosenLoaderVersion = preferredVersion || null;
      loaderVersionsLoading = !!choice.id;
      loaderVersionsAvailable = false;
      lastLoaderInfo = null;
      if (loaderMenuPanel) loaderMenuPanel.style.display = "none";
      if (loaderTrigger) loaderTrigger.setAttribute("aria-expanded", "false");
      reflect();
      setStatus("");
      if (!choice.id) {
        showVersionRow(false);
        return;
      }
      showVersionRow(true);
      var mc = baseVersion || dialogMinecraft();
      setLoaderTriggerText("Loading versions\u2026");
      loaderMenuList.textContent = "";
      versionNote.textContent = "";
      versionNote.style.color = "#a7a7a7";
      fetchLoaderVersions(mc, choice.id)
        .then(function (info) {
          if (!currentDialog() || request !== versionRequest ||
              window.__hxLoaderChoice !== choice.id || dialogMinecraft() !== mc) return;
          fillVersionList(info);
        })
        .catch(function (error) {
          if (!currentDialog() || request !== versionRequest ||
              window.__hxLoaderChoice !== choice.id || dialogMinecraft() !== mc) return;
          loaderVersionsLoading = false;
          loaderMenuList.textContent = "";
          setLoaderTriggerText(LOADER_PLACEHOLDER);
          versionNote.textContent = String((error && error.message) || error);
          versionNote.style.color = "#ff6b5b";
        });
    }

    LOADER_CHOICES.forEach(function (choice) {
      var chip = browseSource.cloneNode(true);
      chip.removeAttribute("id");
      clonedButtonLabel(chip, choice.label);
      chip.setAttribute("data-loader", choice.id || "");
      chip.setAttribute("aria-pressed", "false");
      chip.addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        armLoader(choice);
      });
      chips.appendChild(chip);
    });
    row.appendChild(chips);

    // Loader version: a compact dropdown that appears once a real loader is
    // chosen. The recommended build is preselected; the list is the window of
    // builds the host returned for this Minecraft version. Styled off the
    // dialog's own field controls so it sits in the same visual family.
    var versionWrap = document.createElement("div");
    versionWrap.id = "__hx-loader-version";
    versionWrap.style.cssText = "margin-top:12px;display:none;";
    var versionLabel = labelSource.cloneNode(true);
    versionLabel.id = "__hx-loader-version-label";
    versionLabel.removeAttribute("for");
    versionLabel.textContent = "Loader version";
    versionWrap.appendChild(versionLabel);
    // The loader version reuses the dialog's own Version control, verbatim:
    // the 280x48 role=button trigger with the launcher's chevron, its own
    // wrapper, and the toggle-list popup the app renders underneath it.
    // Cloning keeps the hashed classes, hover/press/focus states and theming
    // exactly as the app paints them -- no new design, no inline styles to
    // drift from the app's palette.
    var versionTrigger = versionRow.querySelector('[role="button"]');
    var loaderTrigger = versionTrigger ? versionTrigger.cloneNode(true) : null;
    var loaderInput = loaderTrigger && loaderTrigger.querySelector("input");
    var loaderMenuList = null;   // the <ul> the rows are appended to
    var loaderMenuPanel = null;  // the app-shaped popup wrapper around it
    var lastLoaderInfo = null;
    var LOADER_PLACEHOLDER = "Select " + "loader version";
    function setLoaderTriggerText(text) {
      if (loaderInput) loaderInput.placeholder = text;
    }
    // The app paints its Version dropdown from a toggle-list component whose
    // markup exists only while that dropdown is open: an absolutely placed
    // 280-wide panel wrapping ul#toggle-list-menu, whose li > button > span
    // rows are single lines of text. That markup is captured the first time
    // it appears and reused here, so the loader picker is literally the app's
    // own dropdown rather than a second design. The installations list -- the
    // first [role=list] in the page -- is a different component entirely (it
    // carries Play / Locate / More-options buttons) and is never the source.
    var menuTemplate = null;
    function captureMenuTemplate() {
      if (menuTemplate) return true;
      var ul = document.getElementById("toggle-list-menu");
      var li = ul && ul.querySelector("li");
      if (!ul || !li) return false;
      var group = ul.parentElement;
      var inner = group && group.parentElement;
      var panel = inner && inner.parentElement;
      if (!group || !inner || !panel) return false;
      menuTemplate = {
        panel: panel.cloneNode(false),
        inner: inner.cloneNode(false),
        group: group.cloneNode(false),
        ul: ul.cloneNode(false),
        li: li.cloneNode(true)
      };
      menuTemplate.ul.removeAttribute("id");   // one toggle-list-menu per page
      return true;
    }
    // Builds the popup: the app's own panel/scroll/row markup once it has
    // been seen, otherwise the same hierarchy at the same sizes so the menu
    // still reads as the app's dropdown until the real classes arrive.
    function createMenu() {
      var ul, inner, panel;
      if (captureMenuTemplate()) {
        ul = menuTemplate.ul.cloneNode(false);
        var group = menuTemplate.group.cloneNode(false);
        group.appendChild(ul);
        inner = menuTemplate.inner.cloneNode(false);
        inner.appendChild(group);
        panel = menuTemplate.panel.cloneNode(false);
      } else {
        ul = document.createElement("ul");
        ul.setAttribute("aria-label", "Options");
        ul.style.cssText = "list-style:none;margin:0;padding:0;display:flex;gap:4px;flex-direction:column;";
        var group2 = document.createElement("div");
        group2.appendChild(ul);
        inner = document.createElement("div");
        // The app's own toggle-list panel wears scrollbars-default; the
        // fallback wears it too, so the menu matches before capture as well.
        inner.className = "scrollbars-default";
        inner.appendChild(group2);
        inner.style.cssText = "box-sizing:border-box;width:calc(280px - 6px);min-width:calc(280px - 6px);" +
          "max-width:calc(280px - 6px);max-height:340px;padding:2px;" +
          "overflow-y:auto;overflow-x:hidden;background:rgb(49,45,44);" +
          "border:1px solid rgb(60,133,39);outline:none;scroll-behavior:smooth;";
        panel = document.createElement("div");
        panel.style.cssText = "position:absolute;left:0;top:100%;z-index:1000;width:100%;";
        // Native ToggleList (compiled module 73627) supplies these states even
        // before its menu markup has been captured. Keep this sheet inside the
        // fallback panel so replacing it with a native clone removes it too.
        var fallbackStyle = document.createElement("style");
        fallbackStyle.textContent =
          "#__hx-loader-version .__hx-loader-fallback-option{background-color:#312d2c;" +
          "border:1px solid #312d2c;outline:1px solid #312d2c;" +
          "transition:background-color 120ms,border-color 120ms;}" +
          "#__hx-loader-version .__hx-loader-fallback-option:hover{background-color:#514d4c;}" +
          "#__hx-loader-version .__hx-loader-fallback-option:focus," +
          "#__hx-loader-version .__hx-loader-fallback-option:focus-visible{" +
          "background-color:#514d4c;border-color:#1e6eea;outline-color:#fff;outline-offset:1px;}";
        panel.appendChild(fallbackStyle);
      }
      panel.appendChild(inner);
      panel.style.display = "none";
      loaderMenuList = ul;
      loaderMenuPanel = panel;
      return panel;
    }
    // Swaps in a freshly built popup (used once the app's own markup is
    // available) without disturbing the trigger it sits beside.
    function rebuildMenu() {
      var old = loaderMenuPanel;
      var parent = old && old.parentNode;
      var fresh = createMenu();
      if (parent && old) parent.replaceChild(fresh, old);
      return fresh;
    }
    // One row of the app's dropdown: li > button > span, one line of text.
    function makeRow(text) {
      if (menuTemplate) {
        var li = menuTemplate.li.cloneNode(true);
        var button = li.querySelector("button");
        var span = button && button.querySelector("span");
        if (span) span.textContent = text;
        else if (button) button.textContent = text;
        if (button) button.setAttribute("aria-label", text);
        return li;
      }
      var row2 = document.createElement("li");
      row2.style.cssText = "list-style:none;margin:0;padding:0;";
      var fallback = document.createElement("button");
      fallback.type = "button";
      fallback.className = "__hx-loader-fallback-option";
      fallback.setAttribute("aria-label", text);
      fallback.style.cssText = "display:flex;align-items:center;justify-content:flex-start;" +
        "box-sizing:border-box;position:relative;width:100%;text-align:left;user-select:none;" +
        "padding:1px 6px;color:#fff;" +
        "scroll-margin-block-start:6px;scroll-margin-block-end:6px;cursor:pointer;";
      var label = document.createElement("span");
      label.style.cssText = "display:inline-flex;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
      label.textContent = text;
      fallback.appendChild(label);
      row2.appendChild(fallback);
      return row2;
    }
    // The chosen row's text, read from its span rather than the whole row so
    // no sibling line of the markup leaks into the trigger.
    function rowText(li) {
      var span = li && li.querySelector("button span");
      if (span && span.textContent) return span.textContent.trim();
      var button = li && li.querySelector("button");
      if (button && button.textContent) return button.textContent.trim();
      return ((li && li.textContent) || "").trim();
    }
    // The trigger is cloned before the app settles its focus/hover state, so
    // re-read the live control's classes whenever the row is revealed; the
    // loader box then wears whatever border/background the Version box wears.
    function syncTriggerStyle() {
      if (!versionTrigger || !loaderTrigger) return;
      loaderTrigger.className = versionTrigger.className;
    }
    if (loaderTrigger && loaderInput) {
      loaderTrigger.removeAttribute("aria-controls");
      loaderTrigger.removeAttribute("aria-pressed");
      loaderTrigger.setAttribute("aria-labelledby", versionLabel.id);
      loaderTrigger.setAttribute("aria-expanded", "false");
      loaderInput.removeAttribute("data-testid");
      loaderInput.removeAttribute("id");
      loaderInput.removeAttribute("aria-labelledby");
      loaderInput.setAttribute("aria-label", "Loader version");
      loaderInput.removeAttribute("aria-expanded");
      loaderInput.removeAttribute("aria-haspopup");
      loaderInput.readOnly = true;
      setLoaderTriggerText(LOADER_PLACEHOLDER);
      // The app's own field wrapper (role=presentation, 280 wide) is cloned so
      // the trigger and popup lay out at the same width they do above -- a
      // bare div would shrink to the 142px column instead.
      var wrapperSource = versionTrigger && versionTrigger.parentElement;
      var loaderPanel = wrapperSource ? wrapperSource.cloneNode(false)
        : document.createElement("div");
      if (!wrapperSource) loaderPanel.style.position = "relative";
      loaderPanel.appendChild(loaderTrigger);
      loaderPanel.appendChild(createMenu());
      versionWrap.appendChild(loaderPanel);
      // If the app's own dropdown opens first, take its markup and repaint the
      // loader menu with it, so both end up identical within the session.
      var captureObserver = null;
      if (window.MutationObserver) {
        captureObserver = new MutationObserver(function () {
          if (menuTemplate || !captureMenuTemplate()) return;
          captureObserver.disconnect();
          rebuildMenu();
          if (lastLoaderInfo) fillVersionList(lastLoaderInfo);
        });
        captureObserver.observe(dialog, { childList: true, subtree: true });
      }
      function claimTemplate() {
        // Last chance before showing: the app's menu may be open already.
        if (menuTemplate || !captureMenuTemplate()) return;
        if (captureObserver) captureObserver.disconnect();
        rebuildMenu();
        if (lastLoaderInfo) fillVersionList(lastLoaderInfo);
      }
      // Close on any outside press, matching the app's own dismiss behaviour.
      function closeLoaderMenu(restoreFocus) {
        if (!loaderMenuPanel) return;
        loaderMenuPanel.style.display = "none";
        loaderTrigger.setAttribute("aria-expanded", "false");
        if (restoreFocus) loaderTrigger.focus();
      }
      function openLoaderMenu() {
        claimTemplate();
        if (!loaderMenuPanel) return;
        loaderMenuPanel.style.display = "";
        loaderTrigger.setAttribute("aria-expanded", "true");
      }
      outsideLoaderClick = function (event) {
        if (loaderMenuPanel && !loaderPanel.contains(event.target)) {
          closeLoaderMenu(false);
        }
      };
      document.addEventListener("click", outsideLoaderClick, true);
      loaderTrigger.addEventListener("click", function (event) {
        event.preventDefault();
        event.stopPropagation();
        var open = loaderMenuPanel && loaderMenuPanel.style.display !== "none";
        if (open) closeLoaderMenu(false);
        else openLoaderMenu();
      });
      loaderPanel.addEventListener("keydown", function (event) {
        var key = event.key;
        var open = loaderMenuPanel && loaderMenuPanel.style.display !== "none";
        var onTrigger = loaderTrigger.contains(event.target);
        if (key === "Escape" && open) {
          event.preventDefault();
          event.stopPropagation();
          closeLoaderMenu(true);
        } else if (onTrigger && (key === "Enter" || key === " ")) {
          // The official controller consumes Enter on role=button DIVs in a
          // body capture listener and emits a click. Respect that activation;
          // toggling again here would immediately close the newly opened menu.
          var handledByNative = event.defaultPrevented;
          event.preventDefault();
          event.stopPropagation();
          if (handledByNative) return;
          if (open) closeLoaderMenu(false);
          else openLoaderMenu();
        }
      });
      // The official body capture listener also moves arrow-key focus. Claim
      // only this picker's navigation keys before it runs, so each press moves
      // one option rather than both the controller and this menu moving it.
      loaderNavigationKeyDown = function (event) {
        if (!currentDialog() || !loaderPanel.contains(event.target)) return;
        var key = event.key;
        if (key !== "ArrowDown" && key !== "ArrowUp" && key !== "Home" && key !== "End") return;
        event.preventDefault();
        event.stopPropagation();
        if (loaderMenuPanel && loaderMenuPanel.style.display === "none") openLoaderMenu();
        var options = Array.prototype.slice.call(loaderMenuList.querySelectorAll("button:not(:disabled)"));
        if (!options.length) return;
        var index = options.indexOf(document.activeElement);
        if (key === "Home") index = 0;
        else if (key === "End") index = options.length - 1;
        else if (key === "ArrowDown") index = (index + 1) % options.length;
        else index = index < 0 ? options.length - 1 : (index + options.length - 1) % options.length;
        options[index].focus();
      };
      document.addEventListener("keydown", loaderNavigationKeyDown, true);
      // Delegated from the wrapper, not the <ul>: the popup is rebuilt when
      // the app's own markup is captured, and a listener bound to the old list
      // would go with it.
      loaderPanel.addEventListener("click", function (event) {
        if (!loaderMenuPanel || !loaderMenuPanel.contains(event.target)) return;
        var li = event.target.closest ? event.target.closest("li") : null;
        if (!li || !loaderMenuList || !loaderMenuList.contains(li)) return;
        var value = li.getAttribute("data-loader-version");
        if (!value) return;
        invalidatePreparation();
        chosenLoaderVersion = value;
        setLoaderTriggerText(rowText(li));
        closeLoaderMenu(true);
        setStatus("");
      });
    }
    var versionNote = document.createElement("p");
    versionNote.setAttribute("role", "status");
    versionNote.setAttribute("data-hx-text-note", "");
    versionNote.style.cssText = "min-height:16px;margin:6px 0 0;font-size:12px;color:#a7a7a7;";
    syncTextSize(versionNote);
    versionWrap.appendChild(versionNote);
    row.appendChild(versionWrap);

    function fillVersionList(info) {
      lastLoaderInfo = info;
      loaderVersionsLoading = false;
      var versions = ((info && info.versions) || []).slice();
      // An Edit keeps the profile's current build selectable even once it has
      // fallen out of the picker's newest window, so re-opening and saving
      // without touching anything never quietly moves the installation.
      var current = editBaseline && editBaseline.loaderVersion &&
        editBaseline.loader === window.__hxLoaderChoice &&
        String(editBaseline.minecraft || "") === String(dialogMinecraft())
        ? editBaseline.loaderVersion : null;
      if (current && versions.indexOf(current) < 0) versions.push(current);
      loaderVersionsAvailable = versions.length > 0;
      var recommended = info && info.recommended;
      if (!loaderMenuList) return;
      loaderMenuList.textContent = "";
      versions.forEach(function (v) {
        var li = makeRow(v === recommended ? v + " (recommended)" : v);
        li.setAttribute("data-loader-version", v);
        loaderMenuList.appendChild(li);
      });
      // A previously chosen build (or an Edit's own current one) wins over
      // the service's recommended pick; anything unknown falls back to it.
      var pick = chosenLoaderVersion || current || recommended;
      if (pick && versions.indexOf(pick) < 0) pick = recommended;
      chosenLoaderVersion = pick || null;
      var chosenItem = pick && loaderMenuList.querySelector('[data-loader-version="' + pick + '"]');
      setLoaderTriggerText(chosenItem ? rowText(chosenItem) : (pick || LOADER_PLACEHOLDER));
      versionNote.textContent = versions.length
        ? "" : "No published loader version for this Minecraft release.";
    }

    function showVersionRow(show) {
      if (show) syncTriggerStyle();
      versionWrap.style.display = show ? "" : "none";
    }

    var status = document.createElement("p");
    status.id = "__hx-loader-status";
    status.setAttribute("role", "status");
    status.setAttribute("data-hx-text-note", "");
    status.style.cssText = "min-height:16px;margin:8px 0 0;font-size:12px;color:#a7a7a7;";
    syncTextSize(status);
    row.appendChild(status);

    function setStatus(message, error) {
      status.textContent = message || "";
      status.style.color = error ? "#ff6b5b" : "#a7a7a7";
    }

    // Both gates that reach the form's own handler: a press on the Install
    // button (click, before React's root listener sees it) and Enter in a
    // field (submit, with no click at all). Either one with a loader armed
    // and not yet prepared materializes it first and holds the form; once
    // prepared (`__hxArmedLoader` set) or bypassing, both stand aside -- the
    // bridge swaps lastVersionId on the way out.
    // The base Minecraft version a materialize route resolves. An Edit dialog
    // whose Version control still answers with its loader id cannot be parsed
    // as a vanilla pair, so the profile's own base version stands in until
    // the user picks a plain vanilla version.
    function dialogMinecraft() {
      var mc = baseMinecraftVersion(dialog);
      var parsed = splitLoaderVersionId(mc);
      if (editBaseline && parsed) {
        return editBaseline.minecraft || parsed.minecraft || mc;
      }
      return mc;
    }
    function gate(event) {
      if (loaderBypass) return;
      var choice = window.__hxLoaderChoice;
      if (!choice) return;                 // Vanilla: the form submits as-is
      if (preparing) {
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      if (loaderVersionsLoading || !loaderVersionsAvailable) {
        event.preventDefault();
        event.stopPropagation();
        setStatus(loaderVersionsLoading ? "Wait for the loader versions to finish loading."
          : "No compatible loader version is available. Choose another loader or Minecraft version.",
          !loaderVersionsLoading);
        return;
      }
      var mc = dialogMinecraft();
      // A prepared id serves only while it still matches this dialog's pick:
      // a choice changed since the last Save re-materializes instead of
      // submitting behind a stale id.
      var armed = window.__hxArmedLoader;
      if (armed && armed.loader === choice &&
          (armed.chosen || "") === (chosenLoaderVersion || "") &&
          String(armed.mc || "") === String(mc)) return;
      // Unchanged on Edit: the form's own patch already carries the loader id
      // the profile points at, so Save needs no materialized write.
      if (editBaseline && choice === editBaseline.loader &&
          (chosenLoaderVersion || "") === (editBaseline.loaderVersion || "") &&
          String(mc) === String(editBaseline.minecraft || "")) return;
      event.preventDefault();
      event.stopPropagation();
      var request = ++preparationRequest;
      preparing = true;
      var label = loaderStatusLabel(choice);
      install.disabled = true;
      var chosen = chosenLoaderVersion;
      setStatus("Preparing " + label + " " + (chosen ? chosen + " " : "") +
        "for Minecraft " + mc + "\u2026");
      fetch("/__loaders/materialize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ minecraft: mc, loader: choice, version: chosen }),
      })
        .then(function (response) { return response.json(); })
        .then(function (out) {
          if (!currentDialog() || request !== preparationRequest ||
              window.__hxLoaderChoice !== choice || dialogMinecraft() !== mc ||
              (chosenLoaderVersion || "") !== (chosen || "")) return;
          preparing = false;
          install.disabled = false;
          if (!out || out.error || !out.versionId) {
            setStatus((out && out.error) || ("Could not prepare " + label + "."), true);
            return;
          }
          // Kept armed for the rest of the dialog session, and matched against
          // the pick it stands for: the app's update flow can reach the host
          // more than once for one Save, and every call after the first must
          // still carry the materialized id instead of the form's stale one.
          window.__hxArmedLoader = { loader: choice, versionId: out.versionId,
            chosen: chosen || "", mc: String(mc) };
          setStatus("");
          loaderBypass = true;
          try {
            install.click();
          } finally {
            loaderBypass = false;
          }
        })
        .catch(function (error) {
          if (!currentDialog() || request !== preparationRequest) return;
          preparing = false;
          install.disabled = false;
          setStatus(String((error && error.message) || error), true);
        });
    }
    install.addEventListener("click", gate, true);
    form.addEventListener("submit", gate, true);

    // The picker lives inside "More options", with the advanced Java fields:
    // a loader choice is an advanced knob, not something a first install needs
    // to see. The group is the direct child of the form column that holds the
    // java/jvm rows (its own classes are build-hashed, so it is recognised by
    // the rows it contains). Falling back to the top-level Version row keeps
    // the picker visible if the section is ever restructured.
    // The section is found by its own toggle button ("More options" /
    // "Less options"), not by the text of its fields -- while collapsed the
    // launcher renders none of the java rows, so the section's text is only
    // the toggle's label. The row is inserted as the first child of that
    // section's collapsible content wrapper, above the Java fields; a loader
    // is an advanced knob, exactly what that section is for. The Java fields
    // mount into the same wrapper when the section expands, so they follow.
    // The advanced section's content wrapper carries the id
    // `advanced_settings_tooltip` and sits beside its own "More options"
    // toggle button; the launcher mounts the advanced Java fields into it
    // when the section expands. The loader row becomes its first child so the
    // picker appears with those advanced fields -- never at the top level a
    // first install sees. If the id ever changes, the toggle-button search
    // finds the wrapper's direct-child sibling; the Version-row fallback
    // keeps the picker visible rather than losing it.
    // The picker sits directly beneath the Version row: the loader version
    // selector picks the recommended build for whichever Minecraft version is
    // chosen, so it belongs beside the control that chooses that version,
    // sharing its field rhythm (label, then controls). `versionRow` is the
    // dialog's own Version group, so inserting after it keeps the two rows
    // visually paired rather than exiling the picker into the Java/JVM
    // advanced block behind "More options", which has nothing to do with
    // choosing a loader. The advanced block stays untouched.
    if (versionRow && versionRow.parentNode) {
      versionRow.insertAdjacentElement("afterend", row);
    } else if (dialog.children[1]) {
      dialog.children[1].appendChild(row);
    }
    currentMinecraft = dialogMinecraft();
    // React exposes the base selection through its placeholder. Watch that
    // value (and replacement field nodes), then fetch a fresh compatible list.
    formObserver = new MutationObserver(function () {
      if (!currentDialog()) {
        versionRequest++;
        preparationRequest++;
        formObserver.disconnect();
        if (captureObserver) captureObserver.disconnect();
        if (outsideLoaderClick) document.removeEventListener("click", outsideLoaderClick, true);
        if (loaderNavigationKeyDown) document.removeEventListener("keydown", loaderNavigationKeyDown, true);
        if (loaderDialogNode === dialog) {
          loaderDialogNode = null;
          window.__hxLoaderChoice = null;
          window.__hxArmedLoader = null;
        }
        return;
      }
      var mc = dialogMinecraft();
      if (mc === currentMinecraft) return;
      currentMinecraft = mc;
      var choice = LOADER_CHOICES.filter(function (item) {
        return item.id === window.__hxLoaderChoice;
      })[0];
      if (choice) armLoader(choice, mc);
    });
    formObserver.observe(document.body, { childList: true, subtree: true,
      attributes: true, attributeFilter: ["placeholder"] });
    reflect();
    // On Edit the dialog arrives with the profile's own loader in place: pick
    // its chip, show the version control and refill its list for the base
    // version the profile builds on -- not the raw loader id the Version
    // control answers with.
    if (editBaseline && editBaseline.loader) {
      var baselineChoice = LOADER_CHOICES.filter(function (c) {
        return c.id === editBaseline.loader;
      })[0];
      if (baselineChoice) {
        armLoader(baselineChoice, editBaseline.minecraft, editBaseline.loaderVersion || null);
      }
    }
  }

  // The installation a row stands for. The row renders the profile's name (or,
  // for the two default installations, the version label their type spells),
  // so the profile is matched by what the row actually shows.
  function profileForRow(row) {
    if (!row) return null;
    var host = window.__host && window.__host.appData;
    var profiles = (host && host.configurationData && host.configurationData.profiles) || {};
    var ids = Object.keys(profiles);
    // The extracted row component receives `configuration` with its exact ID.
    // Read that existing prop before the legacy textual fallback, so identical
    // names and versions still identify distinct installations.
    var fiberKey = Object.keys(row).find(function (key) { return key.indexOf("__reactFiber$") === 0; });
    var fiber = fiberKey && row[fiberKey];
    for (var depth = 0; fiber && depth < 24; depth++, fiber = fiber.return) {
      var configuration = fiber.memoizedProps && fiber.memoizedProps.configuration;
      if (configuration && configuration.id && profiles[configuration.id]) {
        return Object.assign({}, profiles[configuration.id], { id: configuration.id });
      }
    }
    var explicit = row.getAttribute("data-configuration-id");
    if (explicit) {
      return profiles[explicit] ? Object.assign({}, profiles[explicit], { id: explicit }) : null;
    }
    var lines = rowTextLines(row);
    var name = lines[0] ? String(lines[0].textContent || "").trim() : "";
    var version = lines[1] ? String(lines[1].textContent || "").trim() : "";
    var latest = (host && host.gameVersions && host.gameVersions.latest) || {};
    var candidates = ids.filter(function (id) {
      var profile = profiles[id] || {};
      var profileName = String(profile.name || "").trim();
      var type = String(profile.type || "");
      var isDefault = !profileName && (type === "latest-release" || type === "latest-snapshot");
      if (!isDefault && (!name || profileName !== name)) return false;
      var selected = String(profile.lastVersionId || profile.versionId || type || "");
      if (selected === "latest" || selected === "latest-release") selected = latest.release || "";
      else if (selected === "latest-snapshot") selected = latest.snapshot || "";
      return !!version && selected === version;
    });
    // The native rows expose name and resolved version, but no profile id.
    // Identical rows cannot safely select an installation for export or edit.
    return candidates.length === 1
      ? Object.assign({}, profiles[candidates[0]], { id: candidates[0] }) : null;
  }

  function openExportDialog(profile) {
    var s = screen("Export modpack");
    var name = String((profile && profile.name) || "").trim() || "this installation";
    s.add('<p class="__hx-note">Save ' + esc(name) + " as a .mrpack file: the format the Import " +
      "dialog reads, and the format the Modrinth app understands. Files Modrinth knows are " +
      "referenced by download; anything else is bundled into the file. Mods, resource packs, " +
      "shader packs and configuration are exported — options, saves and logs stay where they are.</p>");
    s.add('<p class="__hx-error" id="__hx-export-status" role="status"></p>');
    var status = s.body.querySelector("#__hx-export-status");
    var poll = null;
    var savedDir = "";
    var generation = 0;
    var checking = false;
    var working = false;
    var recovering = true;
    var jobControls = modpackJobControls(s, "export", status, receiveStatus, function (message) { setStatus(message); });

    function setStatus(text, tone) {
      status.textContent = text || "";
      status.style.color = tone === "ok" ? "#86d562" : tone === "muted" ? "#a7a7a7" : "";
    }

    var close = s.button("Cancel", false, s.close);
    var closeLabel = close.querySelector("span");
    var open = s.button("Open folder", false, function () {
      if (!savedDir) return;
      fetch("/__open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: savedDir, kind: "directory" }),
      });
    });
    open.style.display = "none";
    var go = s.button("Export…", true, start);

    function setWorking(on) {
      working = on;
      go.disabled = on;
      closeLabel.textContent = on ? "Close" : "Cancel";
    }

    function stopPolling() {
      window.clearInterval(poll);
      poll = null;
      generation++;
      checking = false;
      jobControls.reset();
      setWorking(false);
    }

    function start() {
      if (working || !s.el.isConnected) return;
      if (!profile || !profile.id) {
        setStatus("Could not identify this installation.");
        return;
      }
      var token = ++generation;
      setWorking(true);
      savedDir = "";
      open.style.display = "none";
      setStatus("Waiting for a folder…", "muted");
      fetch("/__modpacks/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ configurationId: profile.id }),
      })
        .then(function (response) { return response.json(); })
        .then(function (out) {
          if (token !== generation || !working || !s.el.isConnected) return;
          if (out && out.error) {
            stopPolling();
            setStatus(out.error);
            return;
          }
          if (out && out.cancelled) {
            stopPolling();
            setStatus("Folder selection was cancelled.", "muted");
            return;
          }
          if (!out || !out.ok || out.started === false) {
            stopPolling();
            setStatus("The export could not be started. Try again.");
            return;
          }
          jobControls.reset(out.jobId);
          setStatus("Working…", "muted");
          poll = window.setInterval(check, 400);
          check();
        })
        .catch(function (err) {
          if (token !== generation || !working || !s.el.isConnected) return;
          stopPolling();
          setStatus(String((err && err.message) || err));
        });
    }

    function check() {
      if (!working || !s.el.isConnected) {
        stopPolling();
        return;
      }
      if (checking || recovering) return;
      checking = true;
      var token = generation;
      fetch("/__modpacks/status", { cache: "no-store" })
        .then(function (response) { return response.json(); })
        .then(function (st) {
          if (token !== generation || !working) return;
          if (!s.el.isConnected) {
            stopPolling();
            return;
          }
          receiveStatus(st);
        })
        .catch(function () { /* the host answers on the next tick */ })
        .finally(function () { if (token === generation) checking = false; });
    }

    function receiveStatus(st) {
      if (!working || !s.el.isConnected) return;
      if (!jobControls.update(st)) {
        stopPolling(); setStatus("This export stopped. A different job is now active."); return;
      }
      if (modpackCancelled(st)) {
        stopPolling();
        savedDir = ""; open.style.display = "none";
        setStatus("Export cancelled. No file was saved.", "muted");
        return;
      }
      if (!st || st.job !== "export") {
        if (st && st.phase === "done" && st.result && !st.cancelRequested) succeed(st.result);
        else { stopPolling(); setStatus((st && st.error) || "The export stopped."); }
        return;
      }
      var text = st.cancelRequested ? "Cancelling the export…" : st.paused || st.controlStatus === "paused" ? "Export paused." : st.controlStatus === "pause_requested" ? "Pausing the export…" : st.message || "Exporting…";
      setStatus(text + (st.total ? " (" + st.done + "/" + st.total + ")" : ""), "muted");
    }

    function succeed(result) {
      stopPolling();
      setStatus("Saved " + result.path + " (" + sizeText(result.size) + ": " + result.files +
        " from Modrinth, " + result.bundled + " bundled)." +
        ((result.warnings || []).length ? ' ' + result.warnings.join(' ') : ''), "ok");
      savedDir = String(result.path || "").replace(/[\\\/][^\\\/]+$/, "");
      if (savedDir) open.style.display = "";
    }
    var recoveryGeneration = generation;
    fetch("/__modpacks/status", { cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (st) {
        if (recoveryGeneration !== generation || working || !s.el.isConnected || !st || st.job !== "export") return;
        generation++;
        setWorking(true);
        receiveStatus(st);
        if (working) poll = window.setInterval(check, 400);
      }).catch(function () {}).finally(function () {
        recovering = false;
        if (working && poll && s.el.isConnected) check();
      });
  }

  // --- address bar follows the page's own navigation -----------------------
  // Inside CEF the URL is cosmetic and the app's store-driven router never
  // touches history, so from a real origin the address goes stale as soon as
  // the user navigates. Mirror the currently selected tab's href into the
  // location with replaceState: no history entries are added, so the browser
  // keeps one entry per load (the app has no back handling of its own). Local
  // deep links keep working: bridge.js seeds the entry route from the URL and
  // flips __entrySettled once it has landed, and this mirror waits for that
  // flag so a pending /games/java/skins link cannot be rewritten to the
  // product page's default tab by the shell's first render.
  function syncUrlToRoute() {
    if (location.protocol === "file:") return;
    if (window.__entrySettled === false) return;
    var link =
      document.querySelector('main [aria-current="page"][href]') ||
      document.querySelector('main [role="tab"][aria-selected="true"][href]') ||
      document.querySelector('[aria-current="page"][href]');
    var href = link ? link.getAttribute("href") : null;
    if (!href || href.charAt(0) !== "/" || href === location.pathname) return;
    try {
      history.replaceState(null, "", href);
    } catch (e) {
      /* the URL is not replaceState-able; leave the address as it was */
    }
  }

  // --- route watch --------------------------------------------------------
  // The app renders client-side, so the same page is reused for every route and
  // React replaces the subtree on navigation. A debounced observer re-adds the
  // entry points whenever the page they belong to is (re)rendered.
  var pending = null;
  function scan() {
    if (pending) return;
    pending = setTimeout(function () {
      pending = null;
      if (!document.body) return;
      ensureStyle();
      syncTextSize();
      syncUrlToRoute();
      injectNativeTabLayout();
      injectCompatibilityNoticeLayout();
      injectJavaPlayLayout();
      injectUnsupportedServices(location.pathname);
      if (openedOn && openedOn !== location.pathname) closeScreen();
      injectFirstRunOfflineButton();
      if (/^\/games\/[a-z0-9]+\/skins$/.test(location.pathname)) {
        injectSkinActions();
        injectSkinCapeUpload();
      }
      if (location.pathname === "/settings/accounts") {
        injectOfflineAccountButton();
        injectOnlineSignInButton();
      }
      if (location.pathname === "/settings/general") {
        injectUnavailableSettings();
        injectUpdatesButton();
      }
      if (location.pathname === "/settings/about") injectUpdatesButton();
      if (location.pathname === "/games/java/configurations") {
        document.querySelectorAll('main li').forEach(function (row) {
          if (!row.querySelector('button[id="MoreOptionsDropdown"]')) return;
          var profile = profileForRow(row);
          if (profile) row.setAttribute("data-configuration-id", profile.id);
        });
        injectModpackButton();
        injectExportMenuItem();
        injectLoaderPicker();
      }
      if (location.pathname === "/games/bedrock/marketplace_pass") injectMarketplaceLayout();
      if (/^\/games\/(java|bedrock)\/realms$/.test(location.pathname)) injectRealmsLayout();
    }, 120);
  }

  // The launcher is one page: pressing a navigation link changes the view
  // underneath, so any screen that is open goes with it.
  document.addEventListener(
    "click",
    function (event) {
      if (!document.getElementById("__hx-screen")) return;
      var link = event.target && event.target.closest ? event.target.closest('a[href],a[role="tab"]') : null;
      if (link && !link.closest("#__hx-screen")) closeScreen();
    },
    true
  );

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", scan);
  } else {
    scan();
  }
  new MutationObserver(scan).observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("popstate", scan);
  window.addEventListener("resize", scan);
  window.__launcherUI = { screen: screen, field: field, esc: esc, profileForRow: profileForRow, closeScreen: closeScreen };
})();
