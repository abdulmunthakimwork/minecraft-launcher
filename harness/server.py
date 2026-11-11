#!/usr/bin/env python3
"""Serve the reconstructed launcher UI with a mock native host injected.

- static files come from minecraft-launcher-re/src/ui (unmodified)
- HTML responses get `window.__APPDATA__` (built from the REAL launcher state
  files in minecraft-launcher-re/config/.minecraft) plus harness/bridge.js
  injected before the first script, standing in for launcher.dll's host objects.
- client-side routes (e.g. /settings/accounts) fall back to index.html, the way
  launcher.dll's scheme handler serves any path from the embedded UI.
- media the reconstruction could not recover is answered with a transparent
  placeholder so missing packs do not break layout or flood the console.

usage: python harness/server.py [port]
"""
import base64
import collections
import datetime
import functools
import hashlib
import importlib
import json
import mimetypes
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_api  # noqa: E402  (sibling module, resolved via the path above)
import installer  # noqa: E402  (sibling module, resolved via the path above)
import launcher  # noqa: E402  (sibling module, resolved via the path above)
import modpacks  # noqa: E402  (sibling module, resolved via the path above)
import msa  # noqa: E402  (sibling module, resolved via the path above)
import installation_service
import instance_content
import content_provider
import content_resolver
import instance_tools
import quickplay
import download_control

# Work admission and shutdown use one gate. Worker status never takes the
# transaction coordinator, so a paused download can still be resumed.
if '_HOST_GATE' not in globals():
    _HOST_GATE = threading.RLock()
    _HOST_WORK = 0
    _HOST_SHUTDOWN = False


def _launcher_preferences():
    defaults = _read_json('launcher_settings.json', {})
    settings = dict(defaults) if isinstance(defaults, dict) else {}
    saved = _load_overlay()
    for name in ('__launcher_settings', '__configuration_settings'):
        value = saved.get(name)
        if isinstance(value, dict):
            settings.update(value)
    return settings


def _lifecycle_status():
    with _HOST_GATE:
        try:
            launch = launcher.status()
            install = installer.state()
            content_busy = installation_service.busy()
            pack_busy = modpacks.busy()
            if (not isinstance(launch, dict) or type(launch.get('running')) is not bool
                    or not isinstance(install, dict) or type(install.get('active')) is not bool
                    or type(content_busy) is not bool or type(pack_busy) is not bool):
                raise ValueError('Untrusted worker status')
            pid = launch.get('pid')
            running = launch['running'] and type(pid) is int and pid > 0
            active = bool(_HOST_WORK or (launch['running'] and not running)
                          or install['active'] or content_busy or pack_busy)
        except Exception:
            running, active = False, True
        keep_open = _launcher_preferences().get('keepLauncherOpen', True)
        return {'gameRunning': running, 'activeWork': active, 'keepLauncherOpen': keep_open is not False}


def _work_request(path):
    path = urllib.parse.unquote(path)
    if path in ('/__install_control', '/__modpacks/control', '/__launch_stop') or path.endswith('/job/control'):
        return False
    return (path.startswith('/__instances/') or path.startswith('/__configurations/')
            or path.startswith('/__modpacks/') or path in (
                '/__launch', '/__install', '/__loaders/materialize', '/__uninstall',
                '/__state/__configurations', '/__state/__deleted_configurations'))


_DOWNLOAD_REWRITES = (
    ('return!(e===i.Yq.java||!t)&&"Repair"!==t.intent',
     'return e===i.Yq.java?!!t&&(t.canPause===true||t.canResume===true):!!t&&"Repair"!==t.intent'),
    ('isCancelEnabled:b,onPause:p,onResume:E,onCancel:h,additionalStateText:C',
     'isCancelEnabled:e===i.Yq.java?()=>!!(g[e]&&g[e].canCancel):b,onPause:p,onResume:E,onCancel:h,additionalStateText:e===i.Yq.java?()=>g[e]&&g[e].controlReason||"":C'),
)

# Everything the app reads (UI bundle, content mirror, bridge, fonts) lives
# under BASE. A packaged desktop build points LAUNCHER_ROOT at its resources
# folder, because the frozen host is not sitting in the repository layout.
APP_VERSION = os.environ.get("LAUNCHER_VERSION") or "3.41.30"
BASE = os.environ.get("LAUNCHER_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
UI = os.path.join(BASE, "minecraft-launcher-re", "src", "ui")
# The desktop shell can point the harness at a real launcher installation
# (`--minecraft-dir`), so the same UI drives the actual .minecraft folder
# instead of the captured copy. The capture stays the default: it is the state
# every screenshot and route check was verified against.
STATE = os.environ.get("LAUNCHER_STATE_DIR") or os.path.join(
    BASE, "minecraft-launcher-re", "config", ".minecraft"
)
GAME_DIR = os.environ.get("LAUNCHER_GAME_DIR") or os.path.dirname(STATE)
BRIDGE = os.path.join(BASE, "harness", "bridge.js")
# Harness' own screens (offline accounts, custom capes). Injected after the
# bridge so it can use the host calls the bridge registers.
UI_JS = os.path.join(BASE, "harness", "ui.js")
INSTALLATION_UI_JS = os.path.join(BASE, "harness", "installation_ui.js")
FONTS = os.path.join(BASE, "harness", "fonts")
# Mojang's version manifest, captured by re-tools/capture_versions.py. The
# launcher gets this from the host's updater; the UI needs it for the version
# selector and for the patch-notes filter (a note whose version has no
# releaseTime is dropped).
VERSIONS = os.path.join(BASE, "harness", "versions.json")
# Everything the app writes (settings overlay, offline accounts, uploaded capes,
# the game log) lives in DATA_DIR. In the checkout that is harness/; a packaged
# build passes its own data folder, because the resources folder is read-only.
DATA_DIR = os.environ.get("LAUNCHER_DATA_DIR") or os.path.join(BASE, "harness")
STORE_OVERLAY = os.path.join(DATA_DIR, "state.json")
# Mirror of everything the UI used to fetch from launchercontent.mojang.com,
# captured by re-tools/capture_content.py. Keeping it on disk is what makes the
# app work with the network unplugged.
CONTENT = os.path.join(BASE, "harness", "content")
# Artwork generated for the harness itself (the default skin textures the real
# launcher gets from its skin service).
ASSETS = os.path.join(BASE, "harness", "assets")
# Accounts created inside the launcher without a Microsoft sign-in. The captured
# launcher state stays read-only, so these live in their own file.
OFFLINE_STORE = os.path.join(DATA_DIR, "offline_accounts.json")
# Microsoft sign-in state (optional online path; see harness/msa.py). The
# token store lives beside the offline accounts and never enters the page.
msa.configure(DATA_DIR)
# A 64x32 fully transparent PNG. A cape whose PNG file cannot be read falls
# back to this for the picker's clipped <img>; an empty src would render a
# broken-image box instead of an empty slot.
CAPE_PLACEHOLDER = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAAAgCAYAAACinX6EAAAAHklEQVR42u3BAQ0AAADCoPdP"
    "bQ8HFAAAAAAAAADwbiAgAAGwRW23AAAAAElFTkSuQmCC"
)
# The two pages the browser redirect shows after a Microsoft sign-in attempt.
# They are the only HTML this host serves besides the launcher itself.
_MSA_STYLE = (
    "body{font-family:Segoe UI,sans-serif;background:#171615;color:#ede5e2;"
    "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
    "main{max-width:520px;text-align:center}h1{font-size:22px}"
    "p{color:#a7a7a7;line-height:1.5}"
)
_MSA_PAGE_DONE = (
    "<!doctype html><meta charset=utf-8><title>Minecraft Launcher</title>"
    "<style>%s</style><main><h1>Signed in</h1>"
    "<p>You can close this window and return to the Minecraft Launcher.</p>"
    "</main>" % _MSA_STYLE
)
_MSA_PAGE_ERROR = (
    "<!doctype html><meta charset=utf-8><title>Minecraft Launcher</title>"
    "<style>%s</style><main><h1>Sign-in failed</h1><p>%%s</p>"
    "<p>You can close this window and return to the Minecraft Launcher.</p>"
    "</main>" % _MSA_STYLE
)


def _html_escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))

# Cape PNGs uploaded through harness/ui.js. The index (name, id, added) lives in
# the state overlay; the pixels live here, because the editor and the in-game
# texture service both need the bytes, not just the name.
CAPE_DIR = os.path.join(DATA_DIR, "capes")
# The game's own log, kept next to the app's writable state (launcher.py writes
# it; the harness folder itself is read-only in a packaged build).
GAME_LOG = os.path.join(DATA_DIR, "game.log")

# Hold the whole read/modify/write transaction, including writes from import
# workers. Keep the same lock when this module is hot-reloaded.
_STORE_LOCK = globals().get("_STORE_LOCK") or threading.RLock()
# Source identities observed by this host session survive hot reload. No
# profile values are cached, so a missing source cannot resurrect an old row.
_CONFIGURATION_IDENTITIES = globals().get("_CONFIGURATION_IDENTITIES") or collections.OrderedDict()
_IDENTITY_CONTEXT_LIMIT = 16
_IDENTITY_PROFILE_LIMIT = 4096


def _serialized_store(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with _STORE_LOCK:
            return function(*args, **kwargs)
    return wrapped


def _seed_data_dir():
    """A packaged build starts with an empty data folder: seed it from the app.

    The overlay (saved skins, settings, uploaded cape index) and cape pixels
    ship beside the app. Accounts are always added by the user. Nothing is
    overwritten: an existing data folder is the user's.
    """
    if os.path.abspath(DATA_DIR) == os.path.abspath(os.path.join(BASE, "harness")):
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    for name in ("state.json",):
        src = os.path.join(BASE, "harness", name)
        dest = os.path.join(DATA_DIR, name)
        if os.path.isfile(src) and not os.path.exists(dest):
            try:
                shutil.copyfile(src, dest)
            except OSError:
                pass
    capes_src = os.path.join(BASE, "harness", "capes")
    if os.path.isdir(capes_src) and not os.path.isdir(CAPE_DIR):
        try:
            shutil.copytree(capes_src, CAPE_DIR)
        except OSError:
            pass


def _load_capes():
    """Return the bundled Mojang cape followed by custom capes."""
    overlay = _load_overlay()
    entries = overlay.get("__capes")
    out = [{
        "id": "mojang_cape",
        "alias": "Mojang cape",
        "file": os.path.join(ASSETS, "mojang_cape.png"),
        "added": None,
    }]
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ident = str(entry.get("id") or "")
        path = os.path.join(CAPE_DIR, ident + ".png")
        if ident and os.path.isfile(path):
            out.append(
                {
                    "id": ident,
                    "alias": str(entry.get("alias") or "Cape"),
                    "file": path,
                    "added": entry.get("added"),
                }
            )
    return out


def _cape_data_uri(entry):
    try:
        with open(entry["file"], "rb") as fh:
            return "data:image/png;base64," + base64.b64encode(fh.read()).decode("ascii")
    except OSError:
        return CAPE_PLACEHOLDER


@_serialized_store
def _add_cape(name, data):
    """Store one uploaded cape. Returns (status, payload)."""
    if not isinstance(data, str) or not data.startswith("data:image/png;base64,"):
        return 400, {"error": "cape must be a PNG"}
    try:
        raw = base64.b64decode(data.split(",", 1)[1])
    except Exception:
        return 400, {"error": "cape data is not valid base64"}
    if not raw.startswith(b"\x89PNG"):
        return 400, {"error": "cape is not a PNG"}
    if len(raw) > 512 * 1024:
        return 400, {"error": "cape is larger than 512 KB"}
    # IHDR carries the size in bytes 16..24; a cape is 64x32 and Minecraft
    # rejects the texture outright when it is not.
    if len(raw) < 24:
        return 400, {"error": "cape is not a PNG"}
    width = int.from_bytes(raw[16:20], "big")
    height = int.from_bytes(raw[20:24], "big")
    if (width, height) != (64, 32):
        return 400, {"error": "cape must be 64x32, not %dx%d" % (width, height)}
    os.makedirs(CAPE_DIR, exist_ok=True)
    ident = "cape-" + uuid.uuid4().hex[:12]
    with open(os.path.join(CAPE_DIR, ident + ".png"), "wb") as fh:
        fh.write(raw)
    overlay = _load_overlay()
    entries = overlay.get("__capes")
    if not isinstance(entries, list):
        entries = []
    alias = "".join(c for c in str(name or "").strip() if c.isprintable())[:32] or "Cape"
    entries.append(
        {
            "id": ident,
            "alias": alias,
            "added": datetime.datetime.now().isoformat(timespec="seconds"),
        }
    )
    overlay["__capes"] = entries
    _write_overlay(overlay)
    return 200, {"capes": [_cape_entry(e) for e in _load_capes()]}


def _cape_entry(entry):
    """One cape as the skin editor's cape picker expects it."""
    return {
        "id": entry["id"],
        "alias": entry["alias"],
        "url": "/__capes/" + entry["id"] + ".png",
        "capeImage": _cape_data_uri(entry),
    }


# Let the installer's `isInstalled` probe find the game directory immediately,
# before any POST /__install has hinted it there.
installer.DIR_HINTS["game_dir"] = GAME_DIR
installer.DIR_HINTS["state"] = STATE

# Product ids map to the launcher's own packaging enum (the stored product
# state key is e.g. "java-retail-Piston"), not to display names.
PRODUCT_TYPES = {
    "java": "Piston",
    "dungeons": "Piston",
    "bedrock": "Uwp",
    "legends": "Uwp",
    "dungeons2": "Uwp",
}

# `platformSupport` is matched against the detected platform name ("Windows"
# on a normal desktop) before a product is considered runnable here. The
# captured session had no platform list, which made every product look
# unsupported and raised the "can not be played on this device" banner.
PRODUCT_PLATFORMS = ("windows", "Windows")

# Entitlement ids the UI checks for ownership (see the product table in the app
# bundle: java -> game_minecraft, bedrock -> product/game_minecraft_bedrock).
# The captured entitlement store predates any purchase fetch, so it is empty;
# the account's own licenseProductIds still list the Java and Bedrock store
# licences, so the local session reports those entitlements as owned.
ACCOUNT_ENTITLEMENTS = (
    "game_minecraft",
    "product_minecraft_bedrock",
    "game_minecraft_bedrock",
)
ENTITLEMENT_SOURCE = "RETAIL"

PLAY_RECORD_SOURCES = ("Java", "Bedrock", "Launcher", "Unknown")

# launcher.dll publishes a map of appData slice name -> DOM event name; the UI
# reads it as `MCLauncher.common.getConstants().eventNames` and subscribes with
# `window.addEventListener(eventNames.<slice>, ...)`. Without it every listener
# is registered on `undefined` and native pushes never arrive.
#
# The names and the order come from launcher.dll's own `modelNames[]` pointer
# table (re-tools/dll_model_names.py recovers it); the DLL builds each event as
# the literal "coreEvent." + the model name.
MODEL_NAMES = (
    "modelInfo",
    "downloads",
    "gameVersions",
    "gameInstances",
    "versions",
    "credits",
    "constants",
    "configurationData",
    "users",
    "launcherSettings",
    "systemInfo",
    "productInfo",
    "skins",
    "volumes",
    "msiPatch",
    "treatmentTags",
    "quickPlayData",
    "realmsData",
    "connectionStatus",
    "latentTelemetryData",
    "preferencesData",
)
MODEL_EVENT_MAP = {name: "coreEvent." + name for name in MODEL_NAMES}

# --- local-first URL rewriting -------------------------------------------
# The UI is a CEF app: launcher.dll serves the bundle from a virtual root and
# supplies the service roots out of band, so the bundle hard-codes Mojang URLs.
# Outside CEF those constants would reach the network, so they are rewritten as
# the file is served. The extracted bundles stay pristine on disk; only the
# bytes handed to the browser change.
_ORIGIN_REWRITES = (
    # Content base: the UI builds `${launcherContentUrl}/v2/<file>` for API
    # calls and `${launcherContentUrl}<path>` for images, so an empty base makes
    # both origin-relative and they resolve against this server.
    (
        'launcherContentUrl:"https://launchercontent.mojang.com"',
        'launcherContentUrl:""',
    ),
    # Marketplace base: `${marketplaceStoreWebServicesUrl}${path}`.
    (
        'marketplaceStoreWebServicesUrl:"https://store.mktpl.minecraft-services.net"',
        'marketplaceStoreWebServicesUrl:""',
    ),
    # FallbackDiscoverableServices roots, mirrored from harness/local_api.py.
    ('"https://authorization.franchise.minecraft-services.net"', '"/api/auth"'),
    ('"https://entitlements.mktpl.minecraft-services.net"', '"/api/entitlements"'),
    (
        '"https://bedrock.frontendlegacy.realms.minecraft-services.net"',
        '"/api/realms/bedrock"',
    ),
    (
        '"https://java.frontendlegacy.realms.minecraft-services.net"',
        '"/api/realms/java"',
    ),
    ('"https://store.mktpl.minecraft-services.net"', '"/api/store"'),
    ('"https://persona-secondary.franchise.minecraft-services.net"', '"/api/persona"'),
    ('"https://messaging.mktpl.minecraft-services.net"', '"/api/messaging"'),
    (
        '"https://client.stage-b4c666a2.discovery.minecraft-services.net"',
        '"/api/discovery"',
    ),
    ('"https://client.discovery.minecraft-services.net"', '"/api/discovery"'),
    ('"https://local.minecraft.net"', '"/api/local"'),
    # The Minecoin pack picker carries its pack art as absolute Xbox CDN URLs
    # baked into the bundle's own catalog (`url` / `urlWithResolution` on every
    # card). The capture has no copy of that art, so the page would ask for it
    # and bridge.js would answer with the transparent placeholder -- an ask no
    # route makes, and so one the route sweep never saw. Pointing the two hosts
    # at the stub path keeps the same placeholder art while the request stays
    # on this machine (flows.js now fails on any remote ask, which is how this
    # was found).
    ('"https://xforgeassets001.xboxlive.com', '"/__stub/xforgeassets001.xboxlive.com'),
    ('"https://xforgeassets002.xboxlive.com', '"/__stub/xforgeassets002.xboxlive.com'),
    # The Library grid appends every `defaultSkins` entry (Steve and Alex) as a
    # read-only card. In the real launcher those two are the New Skin editor's
    # base models, not library entries, so the append is dropped on the way out.
    # `defaultSkins` itself stays in the slice: the editor dereferences
    # `alex.skinImage` / `steve.skinImage` as its fallback texture.
    (
        "Object.keys(n).forEach((t=>{e.push({readOnly:!0,...n[t]})})),",
        "",
    ),
    # The game dropdown beside PLAY paints one row per id in a list the bundle
    # hardcodes: java, bedrock, dungeons, dungeons2, legends. This build ships
    # the first two only. The other three have to stay in `productInfo` (their
    # purchase dialogs mount at the root and read `productData[id].productId`,
    # so dropping one there throws before the shell paints), so the menu list
    # itself is narrowed here, the same way `hideHiddenProducts` in bridge.js
    # narrows the sidebar nav.
    (
        "Y=[s.Yq.java,s.Yq.bedrock,s.Yq.dungeons,s.Yq.dungeons2,s.Yq.legends]",
        "Y=[s.Yq.java,s.Yq.bedrock]",
    ),
    # Native installation rows have no DOM identity. Forward their existing
    # configuration id so custom actions identify even identical-looking rows.
    # This is a serve-time patch; the extracted bundle stays unchanged.
    (
        'return n.createElement(Fe,null,n.createElement(Ce,null),n.createElement(we,{tabIndex:0,"aria-label":',
        'return n.createElement(Fe,{"data-configuration-id":e.id},n.createElement(Ce,null),n.createElement(we,{tabIndex:0,"aria-label":',
    ),
    # Import search must have an original row to clone even before any saved
    # installation exists or after its filters remove every visible row.
    # Mount the same native component in a hidden, inert template so its
    # generated styles, type setting and controls are always available.
    (
        'F.length>0?n.createElement(Ze,{role:"list"},F.map((e=>n.createElement(Oe,{configuration:e,key:e.id})))):n.createElement(Je,null,n.createElement(c.hE,null,n.createElement(m.T,null,"No result")),n.createElement("p",{className:E({marginTop:"7px"})},n.createElement(m.T,null,"Found no installations matching your search")))',
        'n.createElement(n.Fragment,null,n.createElement("div",{hidden:!0,inert:"","aria-hidden":!0,"data-hx-installation-template":!0},n.createElement("ul",null,n.createElement(Oe,{configuration:{id:"__hx-template",name:"Modpack",type:"custom",versionId:"latest-release",icon:"Grass",gameDir:""}}))),F.length>0?n.createElement(Ze,{role:"list"},F.map((e=>n.createElement(Oe,{configuration:e,key:e.id})))):n.createElement(Je,null,n.createElement(c.hE,null,n.createElement(m.T,null,"No result")),n.createElement("p",{className:E({marginTop:"7px"})},n.createElement(m.T,null,"Found no installations matching your search"))))',
    ),
    # Local Java capability is distinct from ownership: only offline Java
    # bypasses the native purchase/skin gates. Existing ownership stays intact.
    ("return!S&&!G&&!(null!==r&&void 0!==r&&null!==(e=r.entitlements)&&void 0!==e&&e.includes(\"game_minecraft\")&&U)",
     "return!window.__harness.canUseLocalJava(A,r)&&!S&&!G&&!(null!==r&&void 0!==r&&null!==(e=r.entitlements)&&void 0!==e&&e.includes(\"game_minecraft\")&&U)"),
    ("return!z&&!K&&!(null!==(t=e.entitlements)&&void 0!==t&&t.includes(\"game_minecraft\")&&Y)",
     "return!window.__harness.canUseLocalJava(j,e)&&!z&&!K&&!(null!==(t=e.entitlements)&&void 0!==t&&t.includes(\"game_minecraft\")&&Y)"),
    ("return!h&&!S&&!(null!==(t=e.entitlements)&&void 0!==t&&t.includes(\"game_minecraft\")&&v)",
     "return!window.__harness.canUseLocalJava(E,e)&&!h&&!S&&!(null!==(t=e.entitlements)&&void 0!==t&&t.includes(\"game_minecraft\")&&v)"),
    ("[null===r||void 0===r?void 0:r.entitlements,G,U,S])?K:V",
     "[A,null===r||void 0===r?void 0:r.offline,null===r||void 0===r?void 0:r.entitlements,G,U,S])?K:V"),
    ("[e.entitlements,K,Y,z]),ie=",
     "[j,e.offline,e.entitlements,K,Y,z]),ie="),
    ("[e.entitlements,S,v,h])?a.createElement(Ie",
     "[E,e.offline,e.entitlements,S,v,h])?a.createElement(Ie"),
    ("null!==i&&void 0!==i&&null!==(e=i.entitlements)&&void 0!==e&&e.includes(\"game_minecraft\")",
     "(window.__harness.canUseLocalJava(\"java\",i)||null!==i&&void 0!==i&&null!==(e=i.entitlements)&&void 0!==e&&e.includes(\"game_minecraft\"))"),
    # The complete local Java play experience includes native configurations,
    # the scrolling Play control, and game news instead of purchase-only upsell.
    # These consumer gates use capability without changing ownsGame or licenses.
    ("z&&a.createElement(Ot,null,j===i.Yq.java?",
     "(z||window.__harness.canUseLocalJava(j,e))&&a.createElement(Ot,null,j===i.Yq.java?"),
    ("if(!z||!L)return;if(t)return void(t=!1);",
     "if(!z&&!window.__harness.canUseLocalJava(j)||!L)return;if(t)return void(t=!1);"),
    ("[T,z,L,J,Q]);const F=",
     "[T,z,L,J,Q,j,e.offline]);const F="),
    ("d.playable&&(d.ownsGame||d.hasTrialEntitlement)",
     "d.playable&&(d.ownsGame||d.hasTrialEntitlement||window.__harness.canUseLocalJava(r.productId))"),
    # Recent local Java installations use the native Quick Play bar. Its
    # eligibility remains separate from product ownership and online services.
    ("D=(0,r.useMemo)((()=>(g.productId===o.Yq.java||g.productId===o.Yq.bedrock)&&P&&b),[b,P,g.productId])",
     "D=(0,r.useMemo)((()=>window.__harness.canUseLocalJava(g.productId,_)||(g.productId===o.Yq.java||g.productId===o.Yq.bedrock)&&P&&b),[b,P,g.productId,_])"),
    ("E&&C&&a.createElement(a.Fragment,null,a.createElement(Bt,{innerRef:e=>{u.current=e},className:\"dropdownSubArrow\"",
     "(E||window.__harness.canUseLocalJava(e))&&C&&a.createElement(a.Fragment,null,a.createElement(Bt,{innerRef:e=>{u.current=e},className:\"dropdownSubArrow\""),
    ("!N&&Y&&M.productId!==i.Yq.dungeons&&M.productId!==i.Yq.dungeons2&&a.createElement(Lr",
     "!N&&Y&&!window.__harness.canUseLocalJava(M.productId,p)&&M.productId!==i.Yq.dungeons&&M.productId!==i.Yq.dungeons2&&a.createElement(Lr"),
    ("I&&!h&&M.productId!==i.Yq.dungeons2&&a.createElement(Lr,null,a.createElement(ea",
     "I&&!h&&!window.__harness.canUseLocalJava(M.productId,p)&&M.productId!==i.Yq.dungeons2&&a.createElement(Lr,null,a.createElement(ea"),
    ("S=tt(j,!w,i.Yq.java,L,p,v,A,",
     "S=window.__harness.canUseLocalJava(i.Yq.java,b)?null:tt(j,!w,i.Yq.java,L,p,v,A,"),
    ("effect:async(e,t)=>{const i=t.getState(),n=e.payload;if(i.realms[n].supported)",
     "effect:async(e,t)=>{if(!window.__harness.isServiceSupported(\"realms\"))return;const i=t.getState(),n=e.payload;if(i.realms[n].supported)"),
    ("actionCreator:m,effect:(e,t)=>{const i=e.payload;r.Ay.realms.resetRealmsData(),t.dispatch(d(i)),t.dispatch(h(i)),t.dispatch(f(i)),t.dispatch(p(i))}",
     "actionCreator:m,effect:(e,t)=>{if(!window.__harness.isServiceSupported(\"realms\"))return;const i=e.payload;r.Ay.realms.resetRealmsData(),t.dispatch(d(i)),t.dispatch(h(i)),t.dispatch(f(i)),t.dispatch(p(i))}"),
    ('a.createElement(l.T,{placeholders:[v]},"%1$s cannot be launched with this version of the Launcher.")',
     'a.createElement(l.T,{placeholders:[v]},"%1$s is unavailable in this build.")'),
    ("!i&&n.createElement(ae,{currentTyperamp:x},n.createElement(d.ZT,{id:\"RealmsGameOwnershipAlert\",message:n.createElement(n.Fragment,null,n.createElement(c.T,{placeholders:[E]},\"%1$s is required to play Realms.\"),n.createElement(re,{currenttheme:d.cm,currentTyperamp:x,href:\"#openOneVanillaMultisku\",\"aria-label\":\"Purchase Minecraft\",onKeyDown:(0,u.P0)(a),onClick:a,type:\"button\"},n.createElement(c.T,null,\"Click here to buy\"))),severity:\"Info\",position:\"Top\"}))",
     "(!i||!window.__harness.isServiceSupported(\"realms\"))&&n.createElement(ae,{currentTyperamp:x},n.createElement(d.ZT,{id:\"RealmsGameOwnershipAlert\",message:window.__harness.isServiceSupported(\"realms\")?n.createElement(n.Fragment,null,n.createElement(c.T,{placeholders:[E]},\"%1$s is required to play Realms.\"),n.createElement(re,{currenttheme:d.cm,currentTyperamp:x,href:\"#openOneVanillaMultisku\",\"aria-label\":\"Purchase Minecraft\",onKeyDown:(0,u.P0)(a),onClick:a,type:\"button\"},n.createElement(c.T,null,\"Click here to buy\"))):n.createElement(n.Fragment,null,\"Realms is unavailable in this build. \",n.createElement(re,{currenttheme:d.cm,currentTyperamp:x,href:\"https://aka.ms/DownloadNewLauncher?OCID=Launcher\",\"aria-label\":\"Download the official Minecraft Launcher\",type:\"button\"},\"Download the official Minecraft Launcher.\")),severity:\"Info\",position:\"Top\"}))"),
    ("!t&&n.createElement(Z,{currentTyperamp:d},n.createElement(o.ZT,{id:\"RealmsGameOwnershipAlert\",message:n.createElement(n.Fragment,null,n.createElement(r.T,{placeholders:[m]},\"%1$s is required to play Realms.\"),n.createElement(R,{currentTyperamp:d,href:\"#openOneVanillaMultisku\",\"aria-label\":s(\"Purchase %1$s\",\"%1$s is an untranslated Minecraft product name\",[\"Minecraft\"]),onClick:c,onKeyDown:(0,y.P0)(c)},n.createElement(r.T,null,\"Click here to buy\"))),severity:\"Info\",position:\"Top\"}))",
     "(!t||!window.__harness.isServiceSupported(\"realms\"))&&n.createElement(Z,{currentTyperamp:d},n.createElement(o.ZT,{id:\"RealmsGameOwnershipAlert\",message:window.__harness.isServiceSupported(\"realms\")?n.createElement(n.Fragment,null,n.createElement(r.T,{placeholders:[m]},\"%1$s is required to play Realms.\"),n.createElement(R,{currentTyperamp:d,href:\"#openOneVanillaMultisku\",\"aria-label\":s(\"Purchase %1$s\",\"%1$s is an untranslated Minecraft product name\",[\"Minecraft\"]),onClick:c,onKeyDown:(0,y.P0)(c)},n.createElement(r.T,null,\"Click here to buy\"))):n.createElement(n.Fragment,null,\"Realms is unavailable in this build. \",n.createElement(R,{currentTyperamp:d,href:\"https://aka.ms/DownloadNewLauncher?OCID=Launcher\",\"aria-label\":\"Download the official Minecraft Launcher\"},\"Download the official Minecraft Launcher.\")),severity:\"Info\",position:\"Top\"}))"),
)

# path -> (mtime, size, rewritten bytes); rewriting a 9 MB bundle per request
# would be wasteful and the source only changes when the extraction does.
_js_cache = {}

# A 1x1 transparent GIF answers any unrecovered raster asset: the bytes always
# decode, so the page keeps its layout instead of showing a broken-image icon.
_PLACEHOLDER_GIF = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)
_PLACEHOLDER_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>'
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp")
_MEDIA_EXTS = (".webm", ".mp4", ".mp3", ".ogg")
_FONT_EXTS = (".woff2", ".woff", ".ttf", ".otf", ".eot")

# A minimal valid WebAssembly module: the header every wasm loader accepts.
_STUB_WASM = b"\x00asm\x01\x00\x00\x00"

# Documents the UI asks for that the content origin no longer serves. An empty
# document keeps the feature on its empty state instead of a failed request.
SYNTHETIC_CONTENT = {
    "/v2/dynamicPromotionalContent.json": {"entries": []},
}

# Two UI typefaces only exist inside encrypted asset packs that could not be
# recovered. These open-licensed stand-ins keep the intended family (and the
# metrics close enough) instead of failing the request.
FONT_FALLBACKS = (
    ("Minecraft-Seven", "NotoSans-Bold.f5e091c7.woff2"),
    ("MinecraftSixteen", "NotoSans.0918cbc5.woff2"),
)

_missing_media = set()


def _read_json(name, default, header=False):
    """Read one of the launcher's state files (optionally past its 4-line header)."""
    path = os.path.join(STATE, name)
    try:
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read()
    except (OSError, UnicodeError):
        return default
    if header and text.startswith("#$"):
        text = text[text.index("{") :]
    try:
        return json.loads(text)
    except ValueError:
        return default


def _load_versions(selector=False):
    """The `gameVersions` slice, from the captured version manifest.

    `selector` is True for a map a version dropdown reads. Loader documents a
    modpack import or a previous install wrote into `versions/` are left out
    there -- a loader is picked from the form's own Mod loader row, never from
    the Version list. Every other caller (the boot page, Installations page,
    import, launch) keeps the full map, including those `modded` entries the
    "Modded installations" filter reads.

    The full map is what the page boots with too: the Installations list drops
    every installation whose version id is missing from the slice, so a
    modded installation would vanish on every reload. Its loader ids then
    also appear in the dialogs' Version dropdown, labelled `modded ...` --
    the honest state of disk, and no worse than what an import push leaves
    in the store within a session.

    launcher.dll hands the UI version entries that always carry an `issues`
    array (jre_win32_incompatible, system_incompatible, not_compliant,
    modded) computed natively. The raw Mojang manifest this file was captured
    from has no such field, and the UI reads `entry.issues` unguarded once the
    entry exists -- every installation selector throws on it, which paints the
    error boundary ("Whoops!") over the whole page. Offline there is no basis
    to flag any issue, so each entry gets the empty array the native host would
    produce for a healthy install.
    """
    # A missing catalog must not hide existing installations. The bundled
    # catalog remains authoritative; the launcher's cached Mojang manifest is
    # a fallback, and local/profile entries are still read if neither exists.
    doc = {}
    for manifest in (VERSIONS, os.path.join(STATE, "versions", "version_manifest_v2.json")):
        try:
            with open(manifest, encoding="utf-8-sig") as fh:
                candidate = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(candidate, dict):
            doc = candidate
            break
    latest = doc.get("latest") or {}
    if not isinstance(latest, dict):
        latest = {}
    versions = doc.get("versions") or {}
    if isinstance(versions, list):
        versions = {entry["id"]: entry for entry in versions
                    if isinstance(entry, dict) and isinstance(entry.get("id"), str)}
    if not isinstance(versions, dict):
        versions = {}
    for entry in versions.values():
        if isinstance(entry, dict) and "issues" not in entry:
            entry["issues"] = []
    # Installed versions the manifest does not know: a modpack import writes a
    # loader's version document (Fabric/Quilt) into `versions/`, and the UI
    # lists installations against this map -- an id missing here renders as an
    # unknown version. Such entries carry no manifest url (the document is
    # already on disk) and are flagged `modded`, which is exactly what the
    # "Modded installations" filter on the Installations page reads.
    configurations = _effective_configurations()
    roots = [STATE, GAME_DIR]
    roots.extend(profile["gameDir"] for profile in configurations.values()
                 if isinstance(profile, dict) and isinstance(profile.get("gameDir"), str)
                 and profile["gameDir"])
    seen_roots = set()
    for root in roots:
        root_key = os.path.normcase(os.path.abspath(root))
        if root_key in seen_roots:
            continue
        seen_roots.add(root_key)
        try:
            local = sorted(os.listdir(os.path.join(root, "versions")))
        except OSError:
            local = []
        for ident in local:
            if ident in versions:
                continue
            path = installer._version_metadata_path(root, ident)
            if not path:
                continue
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    added = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(added, dict) or not added.get("id"):
                continue
            raw_libraries = added.get("libraries")
            raw_libraries = raw_libraries if isinstance(raw_libraries, list) else []
            libraries = [str(lib.get("name") or "") for lib in raw_libraries
                         if isinstance(lib, dict)]
            modded = bool(added.get("inheritsFrom") or
                added.get("jar") and added.get("jar") != ident or
                ident.startswith(("fabric-loader-", "quilt-loader-", "forge-", "neoforge-")) or
                any(name.startswith(("net.fabricmc:fabric-loader:", "org.quiltmc:quilt-loader:",
                                     "net.minecraftforge:forge:", "net.neoforged:neoforge:"))
                    for name in libraries))
            if selector and modded:
                continue
            versions[ident] = {
                "id": ident,
                "type": added.get("type") or "release",
                "url": "",
                "sha1": "",
                "time": added.get("time") or added.get("releaseTime") or "",
                "releaseTime": added.get("releaseTime") or "",
                "issues": ["modded"] if modded else [],
                # NeoForge's id names only the loader build. Keep its installed
                # parent available to the editor without offering it as vanilla.
                "minecraftVersion": added.get("inheritsFrom") or added.get("minecraftVersion") or "",
            }
    # The UI filters out an installation when getVersionId(lastVersionId)
    # cannot resolve to an entry in this map. Keep saved profiles visible even
    # when their version document is absent or was never captured. This is only
    # for the installations data; version selectors must still offer real
    # manifest builds and locally installed loader versions only.
    if not selector:
        latest_aliases = {
            "latest": latest.get("release"),
            "latest-release": latest.get("release"),
            "latest-snapshot": latest.get("snapshot"),
        }
        for profile in configurations.values():
            if not isinstance(profile, dict):
                continue
            version_id = str(profile.get("lastVersionId") or "")
            version_id = latest_aliases.get(version_id, version_id)
            if not version_id or version_id in versions:
                continue
            is_modded = bool(profile.get("modpack")) or version_id.startswith((
                "fabric-loader-", "quilt-loader-", "forge-", "neoforge-"
            ))
            versions[version_id] = {
                "id": version_id,
                "type": "release",
                "url": "",
                "sha1": "",
                "time": "",
                "releaseTime": "",
                "issues": ["modded"] if is_modded else [],
            }
    return {
        "latest": {
            "release": latest.get("release", ""),
            "snapshot": latest.get("snapshot", ""),
        },
        "versions": versions,
    }


def _install_plan(version_id, run_dir=None):
    """The download/disk numbers the install dialog shows, from the version JSON.

    The launcher's native download planner normally fills `pendingUpdate` on a
    product's version entry; the UI's install dialog treats a missing or
    zero-size plan as a failure and renders its "Whoops!" body. The plan is
    therefore computed from the version document itself -- client jar, asset
    index, log config and every library artifact the version declares -- which
    is the same set of files the launcher would download, so the size shown is
    the real one rather than an invented placeholder.

    The document is the one the launcher captured under
    `versions/<id>/<id>.json`; if it is not present the dialog keeps its honest
    error state instead of showing a made-up number.
    """
    roots = [GAME_DIR, run_dir]
    path = installer._version_metadata_path(STATE, version_id, roots)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = installer._resolved_version(json.load(fh), path, state_dir=STATE, version_roots=roots)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return None

    def size_of(entry):
        return int((entry or {}).get("size") or 0)

    total = 0
    downloads = doc.get("downloads") or {}
    # Only the client build is installed by the launcher; server_safe is a
    # separate product and is not part of this action.
    total += size_of(downloads.get("client"))
    total += size_of(doc.get("assetIndex"))
    total += size_of(((doc.get("logging") or {}).get("client") or {}).get("file"))
    for lib in doc.get("libraries") or []:
        dl = lib.get("downloads") or {}
        total += size_of(dl.get("artifact"))
        # Native libraries (LWJGL, etc.) live under classifiers.
        for cls in (dl.get("classifiers") or {}).values():
            total += size_of(cls)
    if not total:
        return None
    return {
        "versionId": version_id,
        "downloadRequiredForAction": total,
        "storageUsedAfter": total,
        "storageRequiredForAction": total,
    }


def _load_overlay():
    try:
        with open(STORE_OVERLAY, encoding="utf-8-sig") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@_serialized_store
def _write_overlay(data):
    os.makedirs(os.path.dirname(STORE_OVERLAY), exist_ok=True)
    tmp = STORE_OVERLAY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, STORE_OVERLAY)


# --- installations ---------------------------------------------------------
# The launcher keeps installations in `launcher_profiles.json`. The capture is
# read-only, so a created or edited profile lands in the overlay
# (`__configurations`, id -> profile) and a deleted one in
# `__deleted_configurations`; both are applied here. This is the same file the
# native launcher rewrites in place, so the UI's own Edit / Duplicate / Delete
# flows work against real persisted state rather than a per-session id.
def _profile_source_paths():
    paths = []
    for root in (STATE, GAME_DIR, os.path.join(GAME_DIR, ".minecraft")):
        for name in ("launcher_profiles.json", "launcher_profiles_microsoft_store.json"):
            path = os.path.normcase(os.path.abspath(os.path.join(root, name)))
            if path not in paths:
                paths.append(path)
    return paths


def _profile_value(profile):
    # Imported maps occasionally include a stale embedded id. The map key is
    # the identity used by every host operation and must also reach the UI.
    return {key: value for key, value in profile.items() if key != "id"}


def _source_profile_id(source, ident):
    return "source-" + hashlib.sha256((source + "\0" + ident).encode("utf-8")).hexdigest()


def _profile_refs(value):
    if not isinstance(value, list):
        return []
    return [ref for ref in value if isinstance(ref, dict)
            and isinstance(ref.get("source"), str) and isinstance(ref.get("id"), str)]


@_serialized_store
def _configuration_records(overlay=None, include_deleted=False):
    overlay = _load_overlay() if overlay is None else overlay
    paths = _profile_source_paths()
    roots = tuple(sorted({os.path.normcase(os.path.abspath(root)) for root in (STATE, GAME_DIR)}))
    context_key = (os.path.normcase(os.path.abspath(DATA_DIR)), roots)
    for key in list(_CONFIGURATION_IDENTITIES):
        if key != context_key and not os.path.isdir(key[0]):
            _CONFIGURATION_IDENTITIES.pop(key, None)
    known = _CONFIGURATION_IDENTITIES.pop(context_key, {})
    _CONFIGURATION_IDENTITIES[context_key] = known
    while len(_CONFIGURATION_IDENTITIES) > _IDENTITY_CONTEXT_LIMIT:
        _CONFIGURATION_IDENTITIES.popitem(last=False)
    saved_sources = overlay.get("__configuration_sources")
    saved_sources = saved_sources if isinstance(saved_sources, dict) else {}
    saved = overlay.get("__configurations")
    saved_rows = []
    history = overlay.get("__configuration_source_overrides")
    for entry in (history.values() if isinstance(history, dict) else ()):
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            saved_rows.append((entry["id"], entry.get("profile"), _profile_refs(entry.get("sources"))))
    saved_rows.extend((ident, profile, _profile_refs(saved_sources.get(ident)))
                      for ident, profile in (saved.items() if isinstance(saved, dict) else ()))
    reserved = {ident: refs for ident, refs in known.items()
                if any(ref["source"] in paths for ref in refs)}
    for ident, profile, refs in saved_rows:
        if isinstance(profile, dict) and any(ref["source"] in paths for ref in refs):
            # Saved identity survives source read failures and precedence changes.
            # An unrelated discovered profile must never claim its existing ID.
            reserved.setdefault(ident, refs)
    deleted_sources = overlay.get("__deleted_configuration_sources")
    deleted_sources = deleted_sources if isinstance(deleted_sources, dict) else {}
    for ident in overlay.get("__deleted_configurations") or []:
        refs = _profile_refs(deleted_sources.get(ident))
        if any(ref["source"] in paths for ref in refs):
            reserved.setdefault(ident, refs)
    records = {}
    observed = {}
    bindings = {(ref["source"], ref["id"]): ident
                for ident, refs in reserved.items() for ref in refs if ref["source"] in paths}
    def derived_id(source, raw_id):
        ref = {"source": source, "id": raw_id}
        ident, suffix = _source_profile_id(source, raw_id), 0
        while ident in records or ident in reserved and ref not in reserved[ident]:
            suffix += 1
            ident = _source_profile_id(source, raw_id + "\0" + str(suffix))
        return ident
    for path in paths:
        try:
            with open(path, encoding="utf-8-sig") as fh:
                document = json.load(fh)
        except (OSError, ValueError):
            continue
        profiles = document.get("profiles") if isinstance(document, dict) else None
        if not isinstance(profiles, dict):
            continue
        for raw_id, profile in profiles.items():
            if not isinstance(raw_id, str) or not isinstance(profile, dict):
                continue
            value = _profile_value(profile)
            ref = {"source": path, "id": raw_id}
            mirrored = next((record for record in records.values()
                             if record["profile"] == value
                             and any(origin["id"] == raw_id for origin in record["sources"])), None)
            if mirrored is not None:
                mirrored["sources"].append(ref)
                continue
            ident = bindings.get((path, raw_id), raw_id)
            if (path, raw_id) not in bindings and len(known) + len(observed) >= _IDENTITY_PROFILE_LIMIT:
                # Overflow identities use their source directly; never evict an
                # observed raw binding and let a different installation reuse it.
                ident = derived_id(path, raw_id)
            if ident in records or ident in reserved and ref not in reserved[ident]:
                ident = derived_id(path, raw_id)
            records[ident] = {"profile": value, "sources": [ref]}
            if ident not in known and len(known) + len(observed) < _IDENTITY_PROFILE_LIMIT:
                observed[ident] = [ref]
    for ident, profile, refs in saved_rows:
        if not isinstance(profile, dict):
            continue
        if refs:
            # An edit belongs to the source profile, not to whichever unrelated
            # profile happens to have the same raw id after changing roots.
            if not any(ref["source"] in paths for ref in refs):
                continue
            matching = next((key for key, record in records.items()
                             if any(ref in record["sources"] for ref in refs)), None)
            if matching is not None:
                record = records.pop(matching)
                record["profile"].update(_profile_value(profile))
                if ident in records:
                    ident = matching
                records[ident] = record
            else:
                if ident in records:
                    ref = next(ref for ref in refs if ref["source"] in paths)
                    ident = derived_id(ref["source"], ref["id"])
                records[ident] = {"profile": _profile_value(profile), "sources": refs}
        else:
            # Existing overlay records predate source provenance and retain
            # their original behavior. Launcher-created profiles have no source.
            record = records.get(ident, {"profile": {}, "sources": []})
            record["profile"].update(_profile_value(profile))
            records[ident] = record
    for ident, record in records.items():
        if record["sources"] and (ident in known or len(known) < _IDENTITY_PROFILE_LIMIT):
            refs = known.get(ident, [])
            known[ident] = refs + [dict(ref) for ref in record["sources"] if ref not in refs]
    if include_deleted:
        return records
    for ident in overlay.get("__deleted_configurations") or []:
        refs = _profile_refs(deleted_sources.get(ident))
        if refs:
            for key, record in list(records.items()):
                # Mirrored files are one installation. Deleting it hides all
                # original mirrors, while an unrelated same-id source survives.
                if any(ref in record["sources"] for ref in refs):
                    records.pop(key, None)
        else:
            # Do not reinterpret historical deletions and resurrect old rows.
            records.pop(ident, None)
    return records


def _effective_configurations():
    return {ident: record["profile"] for ident, record in _configuration_records().items()}


@_serialized_store
def _save_configuration(ident, profile):
    """Write one profile into the overlay and return the merged list."""
    overlay = _load_overlay()
    record = _configuration_records(overlay, include_deleted=True).get(ident)
    saved = overlay.get("__configurations")
    if not isinstance(saved, dict):
        saved = {}
    sources = overlay.get("__configuration_sources")
    sources = sources if isinstance(sources, dict) else {}
    previous_refs = _profile_refs(sources.get(ident))
    if previous_refs and record and record["sources"] and not any(ref in previous_refs for ref in record["sources"]):
        # A data directory can follow several selected Minecraft roots. Keep
        # prior edits when another source happens to reuse the same raw key.
        history = overlay.get("__configuration_source_overrides")
        history = history if isinstance(history, dict) else {}
        ref = previous_refs[0]
        history[_source_profile_id(ref["source"], ref["id"])] = {
            "id": ident, "profile": saved.get(ident), "sources": previous_refs,
        }
        overlay["__configuration_source_overrides"] = history
    # Keep the complete source snapshot so partial edits retain the original
    # game directory/version while its source document is temporarily unreadable.
    saved[ident] = _profile_value(dict(record["profile"], **profile) if record else profile)
    overlay["__configurations"] = saved
    if record and record["sources"]:
        sources[ident] = record["sources"]
    overlay["__configuration_sources"] = sources
    deleted_sources = overlay.get("__deleted_configuration_sources")
    deleted_sources = deleted_sources if isinstance(deleted_sources, dict) else {}
    deleted_refs = _profile_refs(deleted_sources.get(ident))
    remaining = [ref for ref in deleted_refs if not record or ref not in record["sources"]]
    if deleted_refs and remaining:
        deleted_sources[ident] = remaining
    else:
        deleted_sources.pop(ident, None)
        overlay["__deleted_configurations"] = [
            d for d in (overlay.get("__deleted_configurations") or []) if d != ident
        ]
    overlay["__deleted_configuration_sources"] = deleted_sources
    _write_overlay(overlay)
    return _effective_configurations()


def _configuration(ident):
    if not ident:
        return None
    return _effective_configurations().get(str(ident))


def _quickplay_context():
    return tuple(sorted({os.path.normcase(os.path.abspath(root)) for root in (STATE, GAME_DIR)}))


def _quickplay_configurations(overlay):
    records = _configuration_records(overlay)
    registry = _CONFIGURATION_IDENTITIES.get((os.path.normcase(os.path.abspath(DATA_DIR)), _quickplay_context()), {})
    for ident, record in records.items():
        record["availableSources"] = list(record["sources"])
        known = registry.get(ident) or []
        if record["sources"] and any(ref in known for ref in record["sources"]):
            record["sources"] = known + [ref for ref in record["sources"] if ref not in known]
    return records


@_serialized_store
def _quickplay_slice(overlay=None):
    overlay = _load_overlay() if overlay is None else overlay
    return quickplay.build_slice(_quickplay_configurations(overlay), overlay.get("__quickplay_state"),
                                _quickplay_context(), bool(overlay.get("__quick_play_cleared")))


@_serialized_store
def _mutate_quickplay(action, body):
    overlay = _load_overlay()
    records = _quickplay_configurations(overlay)
    arguments = (records, overlay.get("__quickplay_state"), _quickplay_context())
    legacy = bool(overlay.get("__quick_play_cleared"))
    if action == "update":
        state = quickplay.update(*arguments, body.get("record"), legacy)
    else:
        state = quickplay.delete(*arguments, body, legacy)
    overlay["__quickplay_state"] = state
    _write_overlay(overlay)
    data = _quickplay_slice(overlay)
    result = {"ok": True, "quickPlayData": data}
    if action == "update":
        result["record"] = next(item for item in data["playRecords"] if item["id"] == body["record"]["id"])
    return result


@_serialized_store
def _record_quickplay_launch(ident, captured, context):
    """Merge history after spawn while preserving concurrent installation edits."""
    overlay = _load_overlay()
    now = int(time.time() * 1000)
    binding = quickplay.configuration_binding(ident, captured, context)
    current = next(((key, record) for key, record in _quickplay_configurations(overlay).items()
                    if quickplay.same_binding(binding, quickplay.configuration_binding(key, record, _quickplay_context()))), None)
    warning = None
    if current is not None:
        current_id, _ = current
        _save_configuration(current_id, {"lastUsed": datetime.datetime.fromtimestamp(
            now / 1000, datetime.timezone.utc).isoformat().replace("+00:00", "Z")})
        overlay = _load_overlay()
    else:
        warning = "Minecraft started, but its installation source became unavailable; Quick Play history will return when that source is available."
    overlay["__quickplay_state"] = quickplay.record_launch(ident, captured, overlay.get("__quickplay_state"), context, now)
    _write_overlay(overlay)
    return _quickplay_slice(overlay), warning


# --- modpacks --------------------------------------------------------------
# Import and export live in harness/modpacks.py; this file owns everything
# that touches the launcher's own state, so the profile a pack becomes and the
# version document its loader needs are written here.
def _base_version_doc(minecraft):
    """The vanilla version document a loader profile builds on, as (doc, error)."""
    path, error = _version_metadata(minecraft)
    if error:
        return None, error
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh), None
    except (OSError, ValueError) as exc:
        return None, "could not read the %s version document: %s" % (minecraft, exc)


def _modpack_imported(summary):
    """Turn a finished import into a real installation.

    Runs on the import thread: the pack itself is already on disk, and this is
    the same write the UI's own New installation flow performs -- one profile
    in the overlay, pointing at the pack's version and its own game directory.
    """
    ident = uuid.uuid4().hex
    taken = {
        str((profile or {}).get("name") or "").lower()
        for profile in _effective_configurations().values()
    }
    name = str(summary.get("name") or "Modpack").strip() or "Modpack"
    candidate = name
    n = 2
    while candidate.lower() in taken:
        candidate = "%s %d" % (name, n)
        n += 1
    profile = {
        "name": candidate,
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "lastVersionId": summary["lastVersionId"],
        "gameDir": summary["instanceDir"],
        "type": "custom",
        "icon": "Grass",
        # Kept so an export of this installation can name the same Minecraft
        # version and loader in its pack index.
        "modpack": {
            "source": summary.get('source') or "modrinth",
            "project": summary.get("projectId"),
            "version": summary.get("versionId"),
            "minecraft": summary.get("minecraft"),
            "loader": summary.get("loader"),
            "loaderVersion": summary.get("loaderVersion"),
        },
    }
    profiles = _save_configuration(ident, profile)
    return dict(summary, name=candidate, id=ident, profiles=profiles,
                # The page's version slice is a seed; the UI pushes it back in
                # after an import so the new installation resolves its version.
                gameVersions=_load_versions())


_LOADER_ID_PREFIXES = ("fabric-loader-", "quilt-loader-", "forge-", "neoforge-")


def _inherited_minecraft(version_id, run_dir=None):
    """The Minecraft version a stored loader document inherits from.

    A `neoforge-<build>` id carries no Minecraft version, and an installation
    created by the loader picker has no modpack metadata either, so the only
    local source for the target is the loader's own version document. The id
    is validated first because it is about to become a path segment.
    """
    if not isinstance(version_id, str) or not version_id:
        return ""
    if (len(version_id) > 128 or version_id in (".", "..")
            or version_id != os.path.basename(version_id)
            or any(ch not in "abcdefghijklmnopqrstuvwxyz"
                   "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for ch in version_id)):
        return ""
    path = installer._version_metadata_path(STATE, version_id, [GAME_DIR, run_dir])
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return ""
    if not isinstance(doc, dict):
        return ""
    parent = str(doc.get("inheritsFrom") or "")
    if parent and not installer._safe_version_id(parent):
        return ""
    return _resolve_alias(parent or None) if parent else ""


def _modpack_dependencies(profile):
    """The `dependencies` block an export writes into its pack index."""
    meta = (profile or {}).get("modpack") or {}
    version_id = str((profile or {}).get("lastVersionId") or "")
    minecraft = str(meta.get("minecraft") or _inherited_minecraft(version_id, (profile or {}).get("gameDir")) or "")
    loader, loader_version = str(meta.get("loader") or ""), str(meta.get("loaderVersion") or "")
    for prefix in ("fabric-loader-", "quilt-loader-"):
        if version_id.startswith(prefix):
            inferred, _, base = version_id[len(prefix):].partition("-")
            if minecraft and version_id.endswith("-" + minecraft):
                inferred = version_id[len(prefix):-(len(minecraft) + 1)]
            loader, loader_version = loader or prefix[:-1], loader_version or inferred
            minecraft = minecraft or base
            break
    if version_id.startswith("forge-"):
        base, _, inferred = version_id[len("forge-"):].partition("-")
        loader, loader_version = loader or "forge", loader_version or inferred
        minecraft = minecraft or base
    elif version_id.startswith("neoforge-"):
        loader, loader_version = loader or "neoforge", loader_version or version_id[len("neoforge-"):]
    elif "-forge-" in version_id:
        base, _, inferred = version_id.partition("-forge-")
        loader, loader_version = loader or "forge", loader_version or inferred
        minecraft = minecraft or base
    if not minecraft and not loader:
        minecraft = _resolve_alias(version_id or None)
    dependencies = {"minecraft": minecraft} if minecraft else {}
    if loader and loader_version:
        dependencies[loader] = loader_version
    return dependencies


def _version_metadata(version_id, run_dir=None):
    """Path to a version document, fetching it when only the manifest has it.

    The capture carries the version document for the versions that were
    installed when it was taken. Selecting any other version in the New
    installation dialog is still valid -- the native launcher downloads that
    document before it plans the install -- so it is fetched here from the URL
    the manifest names, checked against its sha1 and stored where the installer
    expects it. Returns (path, error).
    """
    if not installer._safe_version_id(version_id):
        return None, "invalid version id: " + str(version_id)
    local = installer._version_metadata_path(STATE, version_id, [GAME_DIR, run_dir])
    if local:
        return local, None
    boundary = os.path.realpath(STATE)
    path = os.path.realpath(os.path.join(STATE, "versions", version_id, version_id + ".json"))
    try:
        contained = os.path.commonpath([boundary, path]) == boundary
    except ValueError:
        contained = False
    if not contained:
        return None, "version metadata path is outside the version store"
    entry = (_load_versions().get("versions") or {}).get(version_id) or {}
    url = entry.get("url")
    if not url:
        return None, "unknown version: " + str(version_id)
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "Minecraft-Launcher/" + APP_VERSION}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except Exception as exc:  # network, TLS, timeout
        return None, "could not download version metadata: " + str(exc)
    expected = entry.get("sha1")
    if expected and hashlib.sha1(body).hexdigest() != expected:
        return None, "version metadata checksum mismatch: " + str(version_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except OSError as exc:
        return None, "could not store version metadata: " + str(exc)
    return path, None


def _resolve_alias(version_id):
    """`latest-release` / `latest-snapshot` -> the manifest's concrete id."""
    latest = _load_versions().get("latest") or {}
    if version_id in (None, "", "latest", "latest-release"):
        return latest.get("release") or ""
    if version_id == "latest-snapshot":
        return latest.get("snapshot") or ""
    return str(version_id)


def _installation_context(profile):
    ident = _resolve_alias(profile.get("lastVersionId") or profile.get("type"))
    context = {"versionId": ident, "minecraft": ident, "loader": "vanilla"}
    path = installer._version_metadata_path(STATE, ident, [GAME_DIR, profile.get("gameDir")])
    doc = {}
    if path:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            raise ValueError("Invalid installation version metadata")
    parent = doc.get("inheritsFrom") or doc.get("jar") or _inherited_minecraft(ident, profile.get("gameDir"))
    parent = str(parent) if parent and str(parent) != ident and installer._safe_version_id(str(parent)) else ""
    if parent:
        context["minecraft"] = parent
    if doc.get("inheritsFrom"):
        try:
            _, merged = launcher._resolve_version(STATE, ident, version_roots=[GAME_DIR, profile.get("gameDir")])
            doc = merged
        except RuntimeError:
            pass
    raw_libraries = doc.get("libraries")
    raw_libraries = raw_libraries if isinstance(raw_libraries, list) else []
    libraries = [str(lib.get("name") or "") for lib in raw_libraries if isinstance(lib, dict)]
    for name in libraries:
        parts = name.split(":")
        if len(parts) < 3:
            continue
        if parts[:2] == ["net.fabricmc", "fabric-loader"]:
            context.update(loader="fabric", loaderVersion=parts[2])
        elif parts[:2] == ["org.quiltmc", "quilt-loader"]:
            context.update(loader="quilt", loaderVersion=parts[2])
        elif parts[:2] == ["net.minecraftforge", "forge"]:
            base, _, build = parts[2].partition("-")
            context.update(loader="forge", loaderVersion=build or parts[2], minecraft=base if build else context["minecraft"])
        elif parts[:2] == ["net.neoforged", "neoforge"]:
            context.update(loader="neoforge", loaderVersion=parts[2])
        elif parts[:2] == ["net.neoforged.fancymodloader", "loader"]:
            context["fmlVersion"] = parts[2]
    # Flattened launcher documents can establish the loader through libraries
    # without carrying inheritsFrom. Parse the base independently of that
    # detection, while retaining authoritative parent and library versions.
    for prefix, loader in (("fabric-loader-", "fabric"), ("quilt-loader-", "quilt"), ("neoforge-", "neoforge"), ("forge-", "forge")):
        if not ident.startswith(prefix):
            continue
        if context["loader"] == "vanilla":
            context["loader"] = loader
        rest = ident[len(prefix):]
        if loader in ("fabric", "quilt"):
            base = parent
            if not base:
                matches = [version for version in (_load_versions().get("versions") or {}) if rest.endswith("-" + version)]
                base = max(matches, key=len) if matches else rest.rpartition("-")[2]
            build = rest[:-(len(base) + 1)] if base and rest.endswith("-" + base) else rest
            context.setdefault("loaderVersion", build)
            if context["minecraft"] == ident:
                context["minecraft"] = base
        elif loader == "forge":
            base, _, build = rest.partition("-")
            context.setdefault("loaderVersion", build)
            if context["minecraft"] == ident:
                context["minecraft"] = base
        else:
            context.setdefault("loaderVersion", rest)
        break
    java = doc.get("javaVersion") or {}
    if doc and isinstance(java, dict):
        # Match installer.find_java: pre-javaVersion documents use jre-legacy.
        context["javaMajor"] = java.get("majorVersion") or 8
    return context


def _owned_launch_status():
    snapshot = launcher.status()
    return dict(snapshot, configurationId=snapshot.get('configurationId', globals().get('_LAUNCH_INSTANCE')), logPath=GAME_LOG)


def _installation_service():
    return installation_service.InstallationService(
        GAME_DIR, STATE, DATA_DIR, profiles=_effective_configurations,
        save_profile=_save_configuration, context=_installation_context,
        versions=lambda: list((_load_versions(selector=True).get("versions") or {}).values()),
        running=lambda: launcher.status().get("running", False),
        external_busy=lambda: modpacks.busy() or installer.state().get("active", False),
        prepare_loader=lambda minecraft, loader, build: modpacks.materialize_loader(
            STATE, minecraft, {"fabric": "fabric-loader", "quilt": "quilt-loader"}.get(loader, loader),
            _base_version_doc, version=build, game_dir=GAME_DIR),
        open_path=_open_path, pick_path=_pick_path,
        launch_status=_owned_launch_status,
        diagnosis_enabled=lambda: _launcher_preferences().get('crashAssistance', True) is not False,
        launch_stop=launcher.stop, redact=launcher._redact)


# --- offline accounts -----------------------------------------------------
# These are real launcher accounts as far as the UI is concerned, but they are
# minted here instead of by Microsoft: a name, a stable local id, and the
# entitlements this build grants. They are what makes the launcher usable with
# no network and no Microsoft sign-in.
def _load_offline():
    try:
        with open(OFFLINE_STORE, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    accounts = doc.get("accounts")
    doc["accounts"] = [a for a in accounts if isinstance(a, dict)] if isinstance(accounts, list) else []
    hidden = doc.get("hidden")
    doc["hidden"] = [h for h in hidden if isinstance(h, str)] if isinstance(hidden, list) else []
    if not isinstance(doc.get("active"), str):
        doc["active"] = None
    return doc


@_serialized_store
def _save_offline(doc):
    os.makedirs(os.path.dirname(OFFLINE_STORE), exist_ok=True)
    tmp = OFFLINE_STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(tmp, OFFLINE_STORE)


def _avatar_uri(name):
    """The face crop of a default skin, as the data URI the UI expects."""
    path = os.path.join(ASSETS, "skins", name + "_avatar.png")
    try:
        with open(path, "rb") as fh:
            return "data:image/png;base64," + base64.b64encode(fh.read()).decode("ascii")
    except OSError:
        return None


def _offline_record(name, ident=None):
    ident = ident or "offline-" + uuid.uuid4().hex[:12]
    # Local identity enables offline host operations, not store ownership.
    entitlements = []
    return {
        "id": ident,
        "localId": ident,
        "remoteId": "",
        "type": "Offline",
        "offline": True,
        "persistent": True,
        "username": name,
        "gamertag": name,
        "javaProfileName": name,
        "accessToken": "",
        "accessTokenExpiresAt": None,
        "azureToken": "",
        "avatar": _avatar_uri("steve"),
        "entitlements": entitlements,
        "entitlementsSource": {ent: ENTITLEMENT_SOURCE for ent in entitlements},
        "licenseProductIds": [],
        "xbox": {},
        "requiresInteractiveLogin": False,
        "eligibleForFreeTrials": False,
        "eligibleForMigration": False,
        "hasMultipleProfiles": False,
        "inForcedMigration": False,
        "legacy": False,
        "userProperites": [],
    }


def _unique_name(name, taken):
    """A Minecraft profile name is 3-16 chars; keep it unique per store."""
    base = "".join(c for c in (name or "").strip() if c.isalnum() or c in "_-")[:16]
    if len(base) < 3:
        base = "Player"
    candidate = base
    n = 2
    while candidate.lower() in taken:
        suffix = str(n)
        candidate = base[: 16 - len(suffix)] + suffix
        n += 1
    return candidate


def _default_skins():
    """The two skins the New Skin editor falls back to.

    `defaultSkins.alex.skinImage` / `.steve.skinImage` are dereferenced as soon
    as the editor opens, so an empty map throws inside the page and the React
    boundary paints "Whoops!". The textures are drawn by
    re-tools/make_default_skins.py.
    """
    def skin(key, label, slim):
        return {
            "id": key,
            "name": label,
            "skinImage": "/__assets/skins/%s.png" % key,
            "modelImage": "/__assets/skins/%s.png" % key,
            "slim": slim,
            "textureId": key,
            "created": "2026-10-21T00:00:00.000Z",
        }

    return {
        "steve": skin("steve", "Steve", False),
        "alex": skin("alex", "Alex", True),
    }


def _skins_slice():
    defaults = _default_skins()
    # Skins saved or activated in the UI persist through the same overlay the
    # other host writes use (write_store -> state.json); without this a reload
    # would quietly drop the user's library and selection.
    custom = {}
    try:
        raw = read_store("__custom_skins")
        if raw:
            doc = json.loads(raw)
            if isinstance(doc, dict):
                custom = {k: v for k, v in doc.items() if isinstance(v, dict)}
    except Exception:
        custom = {}
    active = defaults["steve"]
    try:
        active_id = read_store("__active_skin_id")
    except Exception:
        active_id = None
    if active_id and active_id in custom:
        active = custom[active_id]
    elif active_id == "alex":
        active = defaults["alex"]
    # The cape picker renders its own "No cape" card before mapping over this
    # list, so serving a no_cape sentinel here painted two identical cards.
    # The library does hide any saved skin whose capeId is not in this list
    # (`a.capeId && capes.findIndex(...) === -1`), but skins saved without a
    # cape store capeId undefined and every persisted skin references a real
    # cape id, so the sentinel is safe to leave out.
    # The bundled Mojang cape is selectable like any uploaded cape.
    capes = []
    capes.extend(_cape_entry(entry) for entry in _load_capes())
    return {
        "defaultSkins": defaults,
        "customSkins": custom,
        "capes": capes,
        "activeSkin": active,
    }


def _dashed_uuid(value):
    """A canonical UUID string for the game's profile lookup.

    The captured account carries a hex id without dashes; an offline account
    carries an `offline-...` id that is not a UUID at all. Hex ids get their
    dashes back, everything else is hashed offline-style so the launcher and the
    skin service agree on one stable id.
    """
    text = "".join(ch for ch in (value or "") if ch in "0123456789abcdefABCDEF")
    if len(text) == 32:
        return "%s-%s-%s-%s-%s" % (
            text[0:8], text[8:12], text[12:16], text[16:20], text[20:32]
        )
    return str(uuid.uuid3(uuid.NAMESPACE_DNS, "OfflinePlayer:" + (value or "Player")))


def _active_identity():
    """The player name/uuid the game is launched with (same source as the game)."""
    accounts, active = build_accounts()
    record = next((a for a in accounts if a.get("id") == active), None)
    if not record:
        return "Player", ""
    name = str(record.get("javaProfileName") or record.get("username") or "Player")
    if record.get("offline"):
        return name, uuid.uuid3(uuid.NAMESPACE_DNS, "OfflinePlayer:" + name).hex
    return name, str(record.get("remoteId") or record.get("uuid") or "")


def _selected_game_identity():
    """Resolve the selected account on the host; never silently switch players."""
    accounts, active = build_accounts()
    record = next((a for a in accounts if a.get("id") == active), None)
    if not record:
        raise RuntimeError("Add or select an account before playing.")
    name = str(record.get("javaProfileName") or record.get("username") or "Player")
    if record.get("offline"):
        return {"name": name, "uuid": uuid.uuid3(uuid.NAMESPACE_DNS, "OfflinePlayer:" + name).hex,
                "access_token": "0", "xuid": "0", "user_type": "legacy"}
    player = msa.game_identity() if str(active).startswith("msa-") else None
    if not player or "msa-" + player["uuid"].replace("-", "") != active:
        raise RuntimeError("Sign in to the selected Microsoft account again before playing.")
    if not record.get("entitlements"):
        raise RuntimeError("This Microsoft account does not own Minecraft Java Edition.")
    return dict(player, user_type="msa")


def _active_profile_id():
    """The canonical profile id emitted by the local session service."""
    return _dashed_uuid(_active_identity()[1])


def _skin_texture_bytes():
    """PNG bytes of the skin the UI has applied.

    The editor stores a picked skin as a data URI; the default skins live in
    `harness/assets/skins`. This is what the game downloads for the player when
    the skin service is pointed at this server.
    """
    active = _skins_slice().get("activeSkin") or {}
    image = active.get("skinImage") or ""
    if isinstance(image, str) and image.startswith("data:image/"):
        try:
            return base64.b64decode(image.split(",", 1)[1])
        except Exception:
            pass
    if isinstance(image, str) and image.startswith("/__assets/skins/"):
        path = os.path.join(ASSETS, "skins", os.path.basename(image))
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            pass
    try:
        with open(os.path.join(ASSETS, "skins", "steve.png"), "rb") as fh:
            return fh.read()
    except OSError:
        return b""


def _textures_value(origin, ident):
    """The base64 `textures` property the game unpacks into a skin."""
    active = _skins_slice().get("activeSkin") or {}
    entry = {"url": origin + "/skin/" + (ident or "skin") + ".png"}
    if active.get("slim"):
        entry["metadata"] = {"model": "slim"}
    textures = {"SKIN": entry}
    # A cape the UI applied is sent to the game as a second texture. It is served
    # from the already-allowed /skin/ prefix so the client's own URL check (which
    # validates against the discovery document) accepts it.
    cape_id = str(active.get("capeId") or "")
    if cape_id and cape_id != "no_cape" and any(c["id"] == cape_id for c in _load_capes()):
        textures["CAPE"] = {"url": origin + "/skin/" + cape_id + ".png"}
    payload = {
        "timestamp": int(time.time() * 1000),
        "profileId": ident,
        "profileName": _active_identity()[0],
        "isPublic": True,
        "textures": textures,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _profile_payload(origin, ident):
    name = _active_identity()[0]
    return {
        "id": ident,
        "name": name,
        "properties": [
            {"name": "textures", "value": _textures_value(origin, ident)}
        ],
        "profileActions": [],
    }


def _discovery_payload(origin):
    """The Minecraft Services discovery document, answered locally.

    The client resolves every service root from this document. Pointing the
    session/profile and texture entries at this server is what lets the game
    show the skin the launcher applied, without the signed Mojang service.
    """
    return {
        "environment": "prod",
        "product": "minecraft",
        "discovery": {
            "product": "minecraft",
            "authentication": {"endpoints": {}},
            "session": {
                "endpoints": {
                    "getProfileById": {
                        "uri": origin + "/session/profile/{profileId}"
                    },
                }
            },
            "player": {"endpoints": {}},
            "profiles": {
                "endpoints": {
                    "getTexture": {
                        "uri": origin + "/skin/{textureId}",
                        "validUris": [origin + "/skin/"],
                    },
                }
            },
            "telemetry": {"endpoints": {}},
        },
    }


def accounts_payload():
    """The `getAccountData` document: accounts plus the selected record.

    `activeAccount` is the record itself, not its id: the account reducer reads
    `.id` off it (e.g. to decide whether a logout removed the selected account).
    """
    accounts, active_id = build_accounts()
    # `activeAccount` is the id, not the record: the account slice's refresh
    # reducer resolves it with `accounts.find((a) => a.id === activeAccount)`.
    # Sending the record itself silently leaves the launcher with no signed-in
    # account, and its first-run account chooser covers the whole window.
    return {
        "accounts": accounts,
        "activeAccount": active_id,
        "errors": [],
        "hasSecureStorageAccess": True,
    }


@_serialized_store
def account_action(body):
    """Create, select, rename or remove an account.

    Creating an account is how the sign-in button behaves offline: the UI's own
    "Sign in with Microsoft" screen calls `loginWithXbox`, and the host answers
    with a local account instead of a token, so the flow completes in the
    launcher's own UI rather than dead-ending on a network error.
    """
    action = str(body.get("action") or "")
    doc = _load_offline()
    accounts, _ = build_accounts()
    taken = {str(a.get("javaProfileName") or "").lower() for a in accounts}

    if action == "login":
        wanted = str(body.get("name") or "").strip()
        existing = next(
            (
                a
                for a in doc["accounts"]
                if str(a.get("javaProfileName") or "").lower() == wanted.lower()
            ),
            None,
        )
        if existing:
            doc["active"] = existing["id"]
        else:
            base = wanted or os.environ.get("USERNAME") or "Player"
            record = _offline_record(_unique_name(base, taken))
            doc["accounts"].append(record)
            doc["active"] = record["id"]
            doc["hidden"] = [h for h in doc["hidden"] if h != record["id"]]
        _save_offline(doc)
        return 200, accounts_payload()

    if action == "switch":
        ident = str(body.get("id") or "")
        if ident not in {a.get("id") for a in accounts}:
            return 404, {"error": "no such account", "id": ident}
        doc["active"] = ident
        _save_offline(doc)
        return 200, accounts_payload()

    if action == "logout":
        ident = str(body.get("id") or "")
        # The Microsoft session is its own store; signing out drops it there
        # and never touches the captured or offline records.
        msa_record = msa.account_record()
        if msa_record and ident == msa_record["id"]:
            msa.logout()
        msa_signed_out = bool(msa_record and ident == msa_record["id"])
        before = len(doc["accounts"])
        doc["accounts"] = [a for a in doc["accounts"] if a.get("id") != ident]
        if (not msa_signed_out and len(doc["accounts"]) == before and ident
                and ident not in doc["hidden"]):
            # A captured Microsoft account cannot be deleted from the immutable
            # launcher state, so it is hidden from the list instead.
            doc["hidden"].append(ident)
        if doc["active"] == ident:
            doc["active"] = doc["accounts"][0]["id"] if doc["accounts"] else None
        _save_offline(doc)
        return 200, accounts_payload()

    if action == "rename":
        ident = str(body.get("id") or "")
        record = next((a for a in doc["accounts"] if a.get("id") == ident), None)
        if not record:
            return 404, {"error": "only local accounts can be renamed", "id": ident}
        name = _unique_name(
            str(body.get("name") or ""),
            taken - {str(record.get("javaProfileName") or "").lower()},
        )
        record["username"] = name
        record["gamertag"] = name
        record["javaProfileName"] = name
        _save_offline(doc)
        return 200, accounts_payload()

    return 400, {"error": "unknown action", "action": action}


def read_store(name):
    """The `MCLauncher.common.getStore(name)` value, from overlay or real state.

    Writes go to harness/state.json so the captured launcher state stays pristine.
    """
    overlay = _load_overlay()
    if name in overlay:
        return overlay[name]
    ui_state = _read_json("launcher_ui_state_microsoft_store.json", {}, header=True)
    value = (ui_state.get("data") or {}).get(name)
    return value if isinstance(value, str) else None


@_serialized_store
def write_store(name, value):
    overlay = _load_overlay()
    if value is None:
        overlay.pop(name, None)
    else:
        overlay[name] = value
    _write_overlay(overlay)


@_serialized_store
def _update_launcher_settings(patch):
    overlay = _load_overlay()
    current = overlay.get("__launcher_settings")
    current = dict(current) if isinstance(current, dict) else {}
    current.update(patch)
    overlay["__launcher_settings"] = current
    if "locale" in patch:
        overlay["__locale"] = patch["locale"]
    _write_overlay(overlay)
    return current


@_serialized_store
def _update_configuration_settings(patch):
    overlay = _load_overlay()
    current = overlay.get("__configuration_settings")
    current = dict(current) if isinstance(current, dict) else {}
    current.update(patch)
    overlay["__configuration_settings"] = current
    _write_overlay(overlay)
    # Return the same flat settings the native serializer reads on startup,
    # including unchanged filters/output flags and unrelated launcher defaults.
    defaults = _read_json("launcher_settings.json", {})
    settings = dict(defaults) if isinstance(defaults, dict) else {}
    locale = overlay.get("__locale")
    if locale:
        settings["locale"] = locale
    launcher_settings = overlay.get("__launcher_settings")
    if isinstance(launcher_settings, dict):
        settings.update(launcher_settings)
    settings.update(current)
    return settings


@_serialized_store
def _write_skin_state(patch):
    overlay = _load_overlay()
    if "customSkins" in patch:
        if not isinstance(patch["customSkins"], dict):
            raise ValueError("skin library must be an object")
        overlay["__custom_skins"] = json.dumps(patch["customSkins"])
    if "activeSkinId" in patch:
        if not isinstance(patch["activeSkinId"], str):
            raise ValueError("active skin id must be a string")
        overlay["__active_skin_id"] = patch["activeSkinId"]
    _write_overlay(overlay)


@_serialized_store
def _delete_configuration(ident):
    overlay = _load_overlay()
    record = _configuration_records(overlay).get(ident)
    saved = overlay.get("__configurations")
    saved_sources = overlay.get("__configuration_sources")
    saved_refs = _profile_refs(saved_sources.get(ident)) if isinstance(saved_sources, dict) else []
    remove_saved = not saved_refs or bool(record and any(ref in saved_refs for ref in record["sources"]))
    if isinstance(saved, dict) and remove_saved:
        saved.pop(ident, None)
    deleted = overlay.get("__deleted_configurations") or []
    previously_deleted = ident in deleted
    if ident not in deleted:
        deleted.append(ident)
    overlay["__deleted_configurations"] = deleted
    sources = overlay.get("__deleted_configuration_sources")
    sources = sources if isinstance(sources, dict) else {}
    refs = _profile_refs(sources.get(ident))
    if record and record["sources"] and (not previously_deleted or refs):
        sources[ident] = refs + [ref for ref in record["sources"] if ref not in refs]
    overlay["__deleted_configuration_sources"] = sources
    if isinstance(saved_sources, dict) and remove_saved:
        saved_sources.pop(ident, None)
    _write_overlay(overlay)
    return _effective_configurations()


UNLOADED = {
    "invites": "Unloaded",
    "activePlayerCounts": "Unloaded",
    "trialEligibility": "Unloaded",
    "worlds": "Unloaded",
    "editOperation": "Unloaded",
    "worldDetails": {},
}


def _realms(product_supported):
    return {
        "supported": product_supported,
        "joinedWorlds": [],
        "ownedWorlds": [],
        "invites": [],
        "loadingStates": dict(UNLOADED),
        "subscriptions": [],
        "unassociatedSubscriptions": [],
    }


def build_accounts():
    """Translate the real launcher account store into the shape the UI reads.

    The on-disk record has the Xbox identity but not the fields the account
    slice indexes on (id / javaProfileName / entitlements), so they are derived.
    """
    store = _read_json("launcher_accounts_microsoft_store.json", {})
    raw = store.get("accounts") or {}
    out = []
    for acc in raw.values():
        if not isinstance(acc, dict):
            continue
        local_id = acc.get("localId") or acc.get("id") or acc.get("remoteId")
        name = acc.get("username") or acc.get("gamertag") or "Player"
        record = dict(acc)
        entitlements = acc.get("entitlements") or []
        sources = acc.get("entitlementsSource") or {}
        record.update({
            "id": local_id,
            "localId": local_id,
            "gamertag": name,
            "javaProfileName": acc.get("javaProfileName") or name,
            "entitlements": entitlements,
            "entitlementsSource": sources,
            "xbox": acc.get("xbox") or {},
            "accessToken": "",
            "requiresInteractiveLogin": True,
        })
        out.append(record)
    # Accounts minted by the harness follow the captured ones. The selection in
    # the overlay wins: it only exists because the user made that choice, and it
    # is also how a captured Microsoft account is signed out (the capture itself
    # stays read-only).
    offline = _load_offline()
    out = [a for a in out if a.get("id") not in offline["hidden"]]
    out.extend(offline["accounts"])
    active = store.get("activeAccountLocalId")
    if active in offline["hidden"]:
        active = None
    # A real Microsoft account (sign-in through msa.py) leads the list when
    # present; an offline choice still wins because the user made it last.
    msa_record = msa.account_record()
    if msa_record and all(a.get("id") != msa_record["id"] for a in out):
        out.insert(0, msa_record)
    if offline["active"]:
        active = offline["active"]
    elif not active and msa_record:
        active = msa_record["id"]
    if active not in {a.get("id") for a in out}:
        active = out[0].get("id") if out else None
    return [_public_account(a) for a in out], active


def _public_account(value):
    """Strip credentials from imported/captured records as well as live ones."""
    if isinstance(value, dict):
        record = {key: ("" if key.lower().replace("_", "").endswith(("token", "tokens")) or key.lower() in ("password", "secret")
                       else _public_account(item)) for key, item in value.items()}
        if value.get("offline") or value.get("type") == "Offline":
            # Older local records carried compatibility entitlements. Do not
            # republish them as licences or rewrite the user's saved record.
            record.update(entitlements=[], entitlementsSource={}, licenseProductIds=[])
        return record
    if isinstance(value, list):
        return [_public_account(item) for item in value]
    return value


def _volumes_slice():
    """Volumes are drives: the picker titles rows by mountPoint and resolves
    the selection by name. The game directory's drive is the one volume this
    machine has; free space per drive comes from the host shim."""
    drive = GAME_DIR[:2] if len(GAME_DIR) > 1 and GAME_DIR[1] == ":" else "local"
    volume = {
        "name": drive,
        "mountPoint": drive + "\\" if drive != "local" else GAME_DIR,
        "isDefault": True,
    }
    return {"selectedVolumeName": drive, "volumes": [volume]}


def _available_disk_space(target=None, volume=None):
    """Inspect the actual install volume without creating a missing directory."""
    if target is not None and volume is not None:
        raise ValueError("Choose an installation path or a volume, not both")
    if volume is not None:
        selected = next((item for item in _volumes_slice()["volumes"] if item.get("name") == volume), None)
        if selected is None:
            raise ValueError("Unknown installation volume")
        target = selected.get("mountPoint")
    if target is not None and (not isinstance(target, str) or "\x00" in target):
        raise ValueError("Invalid installation directory")
    current = os.path.abspath(os.path.expanduser(target or GAME_DIR))
    while True:
        try:
            info = os.stat(current)
        except (FileNotFoundError, NotADirectoryError):
            if os.path.lexists(current):
                raise ValueError("Installation directory cannot be opened")
            parent = os.path.dirname(current)
            if parent == current:
                raise OSError("Installation volume cannot be opened")
            current = parent
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("Installation path is not a directory")
        break
    usage = shutil.disk_usage(current)
    if any(type(value) is not int or value < 0 for value in (usage.free, usage.total, usage.used)):
        raise OSError("The operating system returned invalid disk space")
    return {"value": usage.free, "total": usage.total, "used": usage.used}


def build_appdata():
    product_state = _read_json("launcher_product_state_microsoft_store.json", {})
    overlay = _load_overlay()
    settings = _read_json("launcher_settings.json", {})
    locale_override = read_store("__locale")
    if locale_override:
        settings = dict(settings, locale=locale_override)
    # Choices made in Settings are written to the overlay (the capture stays
    # read-only) and merged here, so a reload comes back with them applied --
    # the same thing the native launcher does by rewriting its settings file.
    saved_settings = overlay.get("__launcher_settings")
    if isinstance(saved_settings, dict):
        settings = dict(settings, **saved_settings)
    config_settings = dict(settings)
    saved_config = overlay.get("__configuration_settings")
    if isinstance(saved_config, dict):
        config_settings.update(saved_config)
    accounts, active_account = build_accounts()
    tags = _read_json("treatment_tags.json", {})
    # Installations the user created, edited or deleted live in the overlay; the
    # capture stays as it was, so both are applied on the way out.
    config_map = _effective_configurations()

    users = {"selectedUserId": active_account, "userMap": {}}
    for acc in accounts:
        users["userMap"][acc["id"]] = acc

    # The UI's product template is keyed by these five product ids; the stored
    # product state only tracks update-check timestamps, so synthesize records.
    release = (_load_versions().get("latest") or {}).get("release")
    java_ready = bool(release) and installer.is_installed(GAME_DIR, STATE, release)
    def product(pid, title):
        java_installed = pid == "java" and java_ready
        return {
            "productId": pid,
            "productType": PRODUCT_TYPES[pid],
            "productTitle": title,
            "versionData": [{
                "versionId": "retail",
                "isInstalled": java_installed,
                "isPlayableOffline": java_installed,
                "pendingUpdate": None,
                "installVolume": None,
            }],
            "hasPlatformSupport": True,
            "platformSupport": list(PRODUCT_PLATFORMS),
            "isTrialAvailable": False,
            "processingQueueGroup": [],
            "productLibraryDir": GAME_DIR,
        }

    # Every product the launcher knows about has to be present, even though
    # this build only ships Java and Bedrock. The app's product store, its route
    # table and its purchase-dialog registry all index the full set by id: the
    # dialogs are mounted at the root for every product and read
    # `productData[id].productId`, so dropping one from here throws before the
    # shell paints. The nav entries for the out-of-scope products are hidden in
    # the render layer instead (see HIDDEN_PRODUCTS in bridge.js) and the game
    # dropdown beside PLAY only lists the two that ship (see _ORIGIN_REWRITES).
    products = {
        "java": product("java", "Minecraft: Java Edition"),
        "bedrock": product("bedrock", "Minecraft: Bedrock Edition"),
        "dungeons": product("dungeons", "Minecraft Dungeons"),
        "legends": product("legends", "Minecraft Legends"),
        "dungeons2": product("dungeons2", "Minecraft Dungeons II"),
    }

    # The install dialog reads the plan from the version entry it is opening;
    # without one it renders its "Whoops!" body even though nothing failed.
    plan = _install_plan(release)
    if plan:
        products["java"]["versionData"][0]["pendingUpdate"] = plan
    if java_ready:
        # Only a complete installation can clear the download plan.
        products["java"]["versionData"][0]["pendingUpdate"] = None

    # quickPlayData is consumed as a store slice: the UI indexes `.playRecords`
    # and `.playRecordSourceMap[source]` before any record exists.
    try:
        quick_play = _quickplay_slice(overlay)
    except quickplay.QuickPlayError as exc:
        # A damaged optional history entry must not prevent the launcher from
        # opening. Its endpoint still reports the error and preserves the data.
        quick_play = {"playRecords": [], "playRecordSourceMap": {source: [] for source in PLAY_RECORD_SOURCES},
                      "error": str(exc)}

    return {
        "productInfo": {"products": products},
        "launcherSettings": settings,
        "configurationData": {"settings": config_settings, "profiles": config_map},
        "users": users,
        "profiles": config_map,
        "treatmentTags": tags,
        "skins": _skins_slice(),
        "volumes": _volumes_slice(),
        "versions": {
            "core": {"name": APP_VERSION, "commit": "378b419869c23acb53c94d4d0a8b50ae9e46e633",
                     "updateAvailable": False},
            "bootstrap": {"updateAvailable": False},
            # The channel selector (and the "use beta" toggle, which the page
            # only shows when there are exactly two) is driven by this list.
            # This build ships one channel; an empty list made the whole
            # settings body conditional and hide every launcher setting.
            "availableChannels": ["release"],
        },
        "constants": {
            "defaultGameDirectory": GAME_DIR,
            "defaultJVMArguments": "-Xmx2G -XX:+UnlockExperimentalVMOptions -XX:+UseG1GC",
            "defaultJavaExecutable": "javaw.exe",
            "isGamecoreClient": False,
            "releaseDontHackThis": 0,
            "enabledSkills": [],
        },
        # The UI matches `name` and `platform` against its own lowercase enums
        # (windows / osx / linux); `osName` is the display string. Without the
        # enum fields every product reads as unsupported on this device.
        "systemInfo": {
            "name": "windows",
            "platform": "windows",
            "version": "10.0.26100",
            "isHandheld": False,
            "osName": "Windows",
            "osVersion": "10.0.26100",
            "architecture": "x64",
        },
        "modelInfo": {"modelEventMap": MODEL_EVENT_MAP},
        "gameVersions": _load_versions(),
        "gameInstances": {"instances": []},
        "downloads": {"current": None, "pool": {}},
        "latentTelemetryData": {},
        "quickPlayData": quick_play,
        "realmsData": {"java": _realms(False), "bedrock": _realms(False)},
        "capabilities": {"localJava": True, "realms": False, "purchases": False},
        "__accounts": accounts,
        "__activeAccountId": active_account,
        # Shape is fixed by the connectionStatus slice: the UI reads `isOnline`
        # and the four substatus flags, and shows the "couldn't connect"
        # banner whenever `isOnline` is falsy.
        "connectionStatus": {
            "isOnline": True,
            "substatus": {
                "isNetworkActive": True,
                "launcherContent": True,
                "minecraftServices": False,
                "xalServices": False,
            },
            "epochLastOfflineTimeMs": 0,
        },
        # Bedrock browsing works on Windows, but this standalone host cannot
        # install or launch it. Keep the native compatibility notice visible.
        "gamecore": {
            "isGamecoreClientAvailable": True,
            "gamecoreClientInstallState": None,
        },
        "msiPatch": None,
        "preferencesData": {},
        # The credits route flattens `credits.libraries`; the real slice lists
        # the open-source components shipped with the launcher.
        "credits": {"libraries": []},
    }


# --- native shell helpers --------------------------------------------------
# launcher.dll can open a folder, launch a file with its default application
# and show the OS file picker. The harness server runs on the same machine, so
# it does those same three things for real instead of answering a stub.
def _open_path(target):
    if not target:
        return {"ok": False, "error": "no path given"}
    path = os.path.abspath(os.path.expanduser(str(target)))
    if not os.path.exists(path):
        return {"ok": False, "error": "not found: " + path}
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606 - the host opens folders/files on purpose
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "path": path}


_PICKER = (
    "import json, sys, tkinter as tk\n"
    "from tkinter import filedialog\n"
    "kind = sys.argv[1] if len(sys.argv) > 1 else 'file'\n"
    "initial = sys.argv[2] if len(sys.argv) > 2 else ''\n"
    "patterns = sys.argv[3] if len(sys.argv) > 3 else ''\n"
    "root = tk.Tk()\n"
    "root.withdraw()\n"
    "root.attributes('-topmost', True)\n"
    "if kind == 'directory':\n"
    "    path = filedialog.askdirectory(initialdir=initial or None, title='Choose a folder')\n"
    "elif kind == 'save':\n"
    "    path = filedialog.asksaveasfilename(initialfile=initial or None, title='Export files')\n"
    "else:\n"
    "    types = [('Files', patterns)] if patterns else None\n"
    "    path = filedialog.askopenfilename(initialdir=initial or None, title='Choose a file',\n"
    "                                      filetypes=types or [('All files', '*.*')])\n"
    "print(json.dumps({'path': path or ''}))\n"
)


def _pick_path_mode(argv):
    """`--pick-path <kind> <initial> <patterns>`: teach the frozen host the picker.

    Prints one JSON line, exactly like the `python -c` version, so the caller
    parses both the same way. Returns the process exit code.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:                  # tk not shipped in this build
        print(json.dumps({"path": "", "error": str(exc)}))
        return 0
    kind = argv[0] if argv else "file"
    initial = argv[1] if len(argv) > 1 else ""
    patterns = argv[2] if len(argv) > 2 else ""
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        if kind == "directory":
            path = filedialog.askdirectory(initialdir=initial or None, title="Choose a folder")
        elif kind == "save":
            path = filedialog.asksaveasfilename(initialfile=initial or None, title="Export files")
        else:
            types = [("Files", patterns)] if patterns else None
            path = filedialog.askopenfilename(
                initialdir=initial or None,
                title="Choose a file",
                filetypes=types or [("All files", "*.*")],
            )
        root.destroy()
    except Exception as exc:
        print(json.dumps({"path": "", "error": str(exc)}))
        return 0
    print(json.dumps({"path": path or ""}))
    return 0


def _pick_path(options):
    """A real native file/folder picker, run out-of-process so Tk stays out of the server."""
    options = options if isinstance(options, dict) else {}
    kind = "save" if options.get("save") else "directory" if options.get("directory") or options.get("kind") == "directory" else "file"
    initial = str(options.get("defaultName") if kind == "save" else options.get("defaultPath") or options.get("initialDir") or "")
    filters = options.get("filters") or options.get("extensions") or []
    patterns = ";".join("*" + f if str(f).startswith(".") else str(f) for f in filters) if filters else ""
    # A frozen build has no `python -c` to run: the dialog is a second instance
    # of this executable, started in its own picker mode.
    command = (
        [sys.executable, "--pick-path"]
        if getattr(sys, "frozen", False)
        else [sys.executable, "-c", _PICKER]
    )
    try:
        done = subprocess.run(
            command + [kind, initial, patterns],
            capture_output=True, timeout=300, text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError("Native file picker could not be started or completed: " + str(exc)) from exc
    if done.returncode != 0:
        details = (done.stderr or "").strip()[-400:]
        raise OSError("Native file picker failed" + (": " + details if details else ""))
    for line in reversed((done.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                result = json.loads(line)
            except ValueError as exc:
                raise OSError("Native file picker returned an invalid response") from exc
            if result.get("error"):
                raise OSError("Native file picker is unavailable: " + str(result["error"])[:400])
            if not isinstance(result.get("path"), str):
                raise OSError("Native file picker returned an invalid path")
            return result["path"]
    raise OSError("Native file picker did not return a selection result")


def _read_file_body(target):
    """Bytes of a file the host picked, as base64 (the skin editor's contract)."""
    if not target:
        return {"error": "no path"}
    path = os.path.abspath(os.path.expanduser(str(target)))
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return {"error": str(exc)}
    if size > 16 * 1024 * 1024:
        return {"error": "file is larger than 16 MB"}
    try:
        with open(path, "rb") as fh:
            data = fh.read(16 * 1024 * 1024 + 1)
    except OSError as exc:
        return {"error": str(exc)}
    if len(data) > 16 * 1024 * 1024:
        return {"error": "file is larger than 16 MB"}
    return {
        "name": os.path.basename(path),
        "size": len(data),
        "base64": base64.b64encode(data).decode("ascii"),
    }


def _uninstall(product_id, remove_saved_data):
    """Remove an installed version the way the launcher's Uninstall does."""
    if product_id and product_id != "java":
        return {"error": "this product is managed by the Microsoft Store"}
    release = (_load_versions().get("latest") or {}).get("release")
    if not release:
        return {"error": "no installed version"}
    if not installer._safe_version_id(release):
        return {"error": "invalid installed version"}
    # Validate every requested destination before the first removal. A linked
    # versions tree or saved-data folder must not cause a partial uninstall.
    try:
        root = instance_content.canonical_root(GAME_DIR)
        vdir = os.path.join(root, "versions", release)
        client_targets = []
        for name in (release + ".jar", release + ".json"):
            target = instance_content.canonical_root(os.path.join(vdir, name))
            if os.path.commonpath((root, target)) != root:
                return {"error": "installed version points outside the game directory"}
            if os.path.isfile(target):
                client_targets.append((name, target))
        data_targets = []
        if remove_saved_data:
            for folder in ("saves", "screenshots", "logs"):
                target = instance_content.canonical_root(os.path.join(root, folder))
                if os.path.commonpath((root, target)) != root:
                    return {"error": "saved data folder points outside the game directory: " + folder}
                if os.path.isdir(target):
                    data_targets.append((folder, target))
    except (instance_content.ContentError, OSError, ValueError) as exc:
        return {"error": str(exc)}
    removed = []
    for name, target in client_targets:
        try:
            os.remove(target)
            removed.append(name)
        except OSError as exc:
            return {"error": str(exc)}
    for folder, target in data_targets:
        try:
            shutil.rmtree(target)
        except OSError as exc:
            return {"error": str(exc)}
        removed.append(folder + "/")
    if not removed:
        return {"error": "nothing to remove"}
    return {"ok": True, "removed": removed}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=UI, **kw)

    # -- routing ---------------------------------------------------------
    def _local_request(self):
        """Reject foreign Host/Origin while accepting local hostname aliases."""
        port = self.server.server_address[1]
        def allowed(value, origin=False):
            try:
                parsed = urllib.parse.urlsplit(value if origin else "http://" + value)
                return (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
                        and parsed.port == port and not parsed.username and not parsed.password
                        and parsed.path in ("", "/") and not parsed.query and not parsed.fragment)
            except ValueError:
                return False
        if not allowed(self.headers.get("Host") or ""):
            self._send_json({"error": "foreign host refused"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and not allowed(origin, True):
            self._send_json({"error": "cross-origin request refused"}, 403)
            return False
        path = urllib.parse.unquote(self.path.split("?", 1)[0]).replace("\\", "/")
        if ".." in path.split("/") or "\x00" in path:
            self.send_error(404)
            return False
        return True

    def do_HEAD(self):
        if self._local_request():
            super().do_HEAD()

    def do_GET(self):
        if not self._local_request():
            return
        path = self.path.split("?", 1)[0]
        if path == '/__lifecycle':
            return self._send_json(_lifecycle_status())
        # The real app is served from the `https://mojang/launcher/` virtual
        # scheme, so a few components build asset URLs with that prefix. Outside
        # CEF the app lives at the origin root, so the prefix is dropped rather
        # than 404ing the asset (the Java page's ore icons are the visible case).
        if path == "/launcher" or path.startswith("/launcher/"):
            path = path[len("/launcher"):] or "/"
            query = self.path.split("?", 1)
            self.path = path + ("?" + query[1] if len(query) > 1 else "")
        if path == "/__bridge.js":
            return self._send_file(BRIDGE, "application/javascript")
        if path == "/__ui.js":
            return self._send_file(UI_JS, "application/javascript")
        if path == "/__installation_ui.js":
            return self._send_file(INSTALLATION_UI_JS, "application/javascript")
        if path == "/__content_icon":
            return self._send_content_icon()
        if path.startswith("/__instances/"):
            return self._instance_request(path)
        # The capture did not include source maps, and the browser only asks
        # for them with devtools open. Answering 204 keeps the console clean
        # without inventing content.
        if path.endswith(".js.map") or path.endswith(".css.map"):
            self.send_response(204)
            self.end_headers()
            return
        # Self-check page: loads every route in a hidden frame and reports which
        # ones render and whether any of them touched the network.
        if path == "/__verify.html":
            return self._send_file(
                os.path.join(BASE, "harness", "verify.html"), "text/html; charset=utf-8"
            )
        if path.startswith("/__state/"):
            return self._send_json({"value": read_store(path[len("/__state/"):])})
        if path == "/__install_status":
            return self._send_json(installer.state())
        if path == "/__quickplay":
            try:
                return self._send_json(_quickplay_slice())
            except quickplay.QuickPlayError as exc:
                return self._send_json({"error": str(exc), "code": exc.code}, exc.status)
        if path == "/__disk_space":
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
            try:
                result = _available_disk_space((params.get("path") or [None])[-1],
                                               (params.get("volume") or [None])[-1])
            except ValueError as exc:
                return self._send_json({"error": str(exc), "code": "INVALID_DISK_TARGET"}, 400)
            except OSError as exc:
                return self._send_json({"error": str(exc), "code": "DISK_SPACE_UNAVAILABLE"}, 500)
            return self._send_json(result)
        if path == "/__loaders/versions":
            # The loader-version picker's source: every stable loader build the
            # meta service publishes for the chosen Minecraft version, newest
            # first, plus the one this launcher would pick on its own.
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            params = dict(
                part.split("=", 1) for part in query.split("&") if "=" in part
            )
            minecraft = _resolve_alias(
                urllib.parse.unquote(params.get("minecraft", "")) or None
            )
            loader = urllib.parse.unquote(params.get("loader", ""))
            if not minecraft or not loader:
                return self._send_json({"error": "loader and minecraft are required"}, 400)
            try:
                offered, recommended = modpacks.loader_versions(loader, minecraft)
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)
            return self._send_json({
                "ok": True,
                "minecraft": minecraft,
                "versions": offered,
                "recommended": recommended,
            })
        if path == "/__modpacks/status":
            return self._send_json(modpacks.state())
        if path == "/__modpacks/search":
            query = ""
            offset = 0
            if "?" in self.path:
                for part in self.path.split("?", 1)[1].split("&"):
                    if part.startswith("query="):
                        query = urllib.parse.unquote_plus(part[6:])
                    elif part.startswith("offset="):
                        try:
                            offset = max(0, int(part[7:]))
                        except ValueError:
                            offset = 0
            try:
                return self._send_json(modpacks.search(query, offset=offset))
            except Exception as exc:  # network, TLS, API change
                return self._send_json({"error": "Modrinth is unreachable: %s" % exc}, 502)
        if path == "/__launch_status":
            return self._send_json(launcher.status())
        if path == "/__launch_log":
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            since = 0
            for part in query.split("&"):
                if part.startswith("since="):
                    try:
                        since = int(part[6:])
                    except ValueError:
                        since = 0
            return self._send_json(launcher.log_since(since))
        if path == "/__accounts":
            return self._send_json(accounts_payload())
        # Optional Microsoft sign-in (harness/msa.py): the page polls status,
        # asks for the authorize URL, and the browser's redirect comes back
        # to /__msa/callback on this same server.
        if path == "/__msa/config":
            return self._send_json(msa.configuration())
        if path == "/__msa/status":
            return self._send_json(msa.status())
        if path == "/__msa/signin":
            try:
                url = msa.sign_in(self.server.server_address[1])
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)
            return self._send_json({"ok": True, "url": url})
        if path == "/__msa/callback":
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            params = urllib.parse.parse_qs(query)
            code = (params.get("code") or [None])[0]
            state = (params.get("state") or [None])[0]
            auth_error = (params.get("error_description") or params.get("error") or [None])[0]
            if auth_error:
                msa_status_error = str(auth_error)
                try:
                    msa.fail(msa_status_error, state)
                    page = _MSA_PAGE_ERROR % _html_escape(msa_status_error)
                except ValueError as exc:
                    page = _MSA_PAGE_ERROR % _html_escape(str(exc))
            elif not code:
                page = _MSA_PAGE_ERROR % "Microsoft did not return a sign-in code."
            else:
                try:
                    msa.callback(code, state)
                    page = _MSA_PAGE_DONE
                except ValueError as exc:
                    page = _MSA_PAGE_ERROR % _html_escape(str(exc))
            return self._send_bytes(page.encode("utf-8"), "text/html; charset=utf-8")
        # Capes uploaded through the harness' own screen. The list is what the
        # skin editor's cape picker reads; the image route is what the editor and
        # the in-game texture entry point at.
        if path == "/__capes":
            return self._send_json({"capes": [_cape_entry(e) for e in _load_capes()]})
        if path.startswith("/__capes/"):
            return self._send_cape(path[len("/__capes/"):])
        # In-game skins: the client resolves its services through this discovery
        # document (launcher.py passes minecraft.api.discovery.host), then pulls
        # the profile and texture for the player from the routes below.
        if path == "/discovery/minecraft/client":
            return self._send_json(_discovery_payload(self._origin()))
        if path.startswith("/session/profile/"):
            # This is a local profile service for the account launching the game,
            # not a lookup for arbitrary player data. Restricting the response to
            # that one canonical id means a profile's texture URL always maps to
            # the only skin route the server will serve.
            ident = _dashed_uuid(path[len("/session/profile/"):].strip("/"))
            # The game's account lookup can use either the launcher account id
            # or its local, generated profile UUID. Both identify the single
            # active player, but every response normalizes to the profile UUID
            # used in the texture URL.
            profile_ident = _active_profile_id()
            if ident != profile_ident:
                self.send_error(404)
                return
            return self._send_json(_profile_payload(self._origin(), profile_ident))
        if path.startswith("/skin/"):
            # A cape id served from this prefix is a cape, not the skin: both
            # have to live under the one URL the client's allowed-domain check
            # accepts. The bundled Mojang cape is served like uploaded capes.
            name = path[len("/skin/"):].split("?", 1)[0].strip("/")
            cape = next((c for c in _load_capes() if c["id"] + ".png" == name), None)
            if cape:
                try:
                    with open(cape["file"], "rb") as fh:
                        return self._send_bytes(fh.read(), "image/png")
                except OSError:
                    self.send_error(404)
                    return
            texture_id = name[:-4] if name.endswith(".png") else name
            if texture_id != _active_profile_id():
                self.send_error(404)
                return
            body = _skin_texture_bytes()
            if not body:
                self.send_error(404)
                return
            return self._send_bytes(body, "image/png")
        if path.startswith("/__assets/"):
            return self._send_asset(path)
        # Every remote URL the app asked for is rewritten to /__stub/<host>/...
        # by harness/bridge.js, so this endpoint is what keeps the launcher
        # from ever reaching the network.
        if path.startswith("/__stub/"):
            return self._send_stub(path)
        # The launcher's franchise services, answered locally (harness/local_api.py).
        if path.startswith("/api/"):
            status, payload = local_api.respond(path)
            return self._send_json(payload, status)
        if path.startswith("/static/js/") and path.endswith(".js"):
            return self._send_localized_js(path)
        if path.startswith("/static/media/"):
            return self._send_media(path)
        if path in SYNTHETIC_CONTENT:
            return self._send_json(SYNTHETIC_CONTENT[path])
        # Content the UI used to pull from launchercontent.mojang.com. The mirror
        # keeps the URL layout, so the served path is the request path.
        local = self._content_file(path)
        if local:
            return self._send_local(local)
        if path.startswith("/v2/") or path.startswith("/images/"):
            return self._send_json(
                {"error": "not in local content mirror", "path": path}, 404
            )
        if path in ("/", "/index.html", "/__preview.html") or not os.path.splitext(path)[1]:
            return self._send_preview()
        return super().do_GET()

    def _send_content_icon(self):
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
        urls = params.get("url") or []
        url = content_provider.optional_icon_url(urls[0]) if len(urls) == 1 else None
        if not url:
            return self._send_json({"error": "Unsupported project logo URL"}, 400)
        try:
            image = content_provider.ModrinthProvider().icon(url)
        except content_provider.ProviderError as exc:
            return self._send_json({"error": str(exc), "code": exc.code}, 502)
        except (OSError, ValueError) as exc:
            return self._send_json({"error": "Could not load the project logo: " + str(exc)}, 502)
        self.send_response(200)
        self.send_header("Content-Type", image["contentType"])
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(len(image["body"])))
        self.end_headers()
        self.wfile.write(image["body"])

    # -- local stub ------------------------------------------------------
    def _send_stub(self, path):
        """Answer for a URL that pointed outside this application.

        Images become a transparent placeholder so the layout survives, fonts
        get the local substitute, scripts and stylesheets come back empty but
        valid, and the Microsoft identity endpoints return a clean 401 so a
        sign-in attempt fails immediately instead of hanging.
        """
        rest = path[len("/__stub/"):]
        host, _, tail = rest.partition("/")
        ext = os.path.splitext(tail)[1].lower()
        accept = (self.headers.get("Accept") or "").lower()

        if ext == ".svg":
            return self._send_bytes(_PLACEHOLDER_SVG, "image/svg+xml")
        if ext in _IMAGE_EXTS:
            return self._send_bytes(_PLACEHOLDER_GIF, "image/gif")
        if ext in _FONT_EXTS:
            substitute = os.path.join(FONTS, os.path.basename(tail))
            if os.path.isfile(substitute):
                return self._send_file(substitute, "font/woff2")
            return self._send_bytes(_PLACEHOLDER_GIF, "image/gif")
        if ext in (".js", ".mjs"):
            return self._send_bytes(b"export default {};\n", "application/javascript")
        if ext == ".css":
            return self._send_bytes(b"/* local stub */\n", "text/css")
        if ext == ".wasm":
            return self._send_bytes(_STUB_WASM, "application/wasm")
        if ext == ".json":
            return self._send_json({})
        # An <img> whose URL carries no file extension still asks for an image
        # (the placeholder services use paths like /600x400?text=Hi).
        if "image" in accept and "text/html" not in accept:
            return self._send_bytes(_PLACEHOLDER_GIF, "image/gif")
        # Microsoft identity endpoints cannot be answered without a real
        # account; a 401 is the honest local answer.
        if any(k in host for k in ("auth.xboxlive.com", "xsts.", "sisu.",
                                   "login.live.com", "login.microsoftonline.com")):
            return self._send_json(
                {"error": "sign-in requires network access", "code": 401}, 401
            )
        return self._send_json({})

    # -- local content ---------------------------------------------------
    def _content_file(self, path):
        """Map a request path onto the captured content mirror, if present."""
        rel = path.lstrip("/")
        if not rel or ".." in rel:
            return None
        local = os.path.join(CONTENT, rel.replace("/", os.sep))
        if os.path.isfile(local):
            return local
        # Per-locale documents (`/v2/<name>/<locale>.json`) are fetched in the
        # launcher's language. The mirror holds the base locale, and a missing
        # translation must not empty the page, so fall back to it rather than
        # 404ing on all 55 languages.
        if path.startswith("/v2/") and path.endswith(".json"):
            parts = path.strip("/").split("/")
            if len(parts) == 3:
                base = os.path.join(CONTENT, "v2", parts[1], "en-US.json")
                if os.path.isfile(base):
                    return base
            if len(parts) == 3 and parts[1] == "news":
                base = os.path.join(CONTENT, "v2", "news.json")
                if os.path.isfile(base):
                    return base
        return None

    def _send_asset(self, path):
        """Files generated for the harness (currently the default skin art)."""
        rel = path[len("/__assets/"):]
        if not rel or ".." in rel:
            self.send_error(404)
            return
        local = os.path.join(ASSETS, rel.replace("/", os.sep))
        if not os.path.isfile(local):
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(local)[0] or "application/octet-stream"
        return self._send_file(local, ctype)

    def _send_local(self, local):
        ctype = mimetypes.guess_type(local)[0] or "application/octet-stream"
        try:
            if os.path.basename(os.path.dirname(local)) == "homepagePromotionalItems":
                body = self._filter_promos(local)
            else:
                with open(local, "rb") as fh:
                    body = fh.read()
        except (OSError, ValueError):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "max-age=300")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _filter_promos(path):
        """Drop promos whose call to action is a product this build lacks.

        The home page renders the featured promo's button as a product action;
        with no Dungeons II product here that button becomes a disabled
        "Incompatible with Launcher" chip on the launcher's landing page. The
        coming-soon articles stay -- only the unusable call to action goes.
        """
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)

        def usable(item):
            for button in item.get("associatedButtons") or []:
                if not isinstance(button, dict):
                    continue
                jump = str(button.get("jumpLink") or "")
                if jump.lower().startswith(("dungeons", "legends")):
                    return False
                launch = button.get("readMoreLaunchGame")
                if isinstance(launch, dict) and launch.get("launchGame") != "java":
                    if not button.get("readMoreLink"):
                        return False
                    button.pop("readMoreLaunchGame", None)
                    button["readMoreGenericLink"] = {"readMoreText": "Read More"}
            return True

        for key in ("entries", "filterableEntries"):
            items = doc.get(key)
            if isinstance(items, list):
                doc[key] = [i for i in items if not isinstance(i, dict) or usable(i)]
        return json.dumps(doc).encode("utf-8")

    def _origin(self):
        """The base URL for the local host server's actual bound interface."""
        host, port = self.server.server_address[:2]
        display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        if ":" in display_host and not display_host.startswith("["):
            display_host = "[" + display_host + "]"
        return "http://%s:%d" % (display_host, port)

    def _send_localized_js(self, path):
        """Serve a bundle with its remote service roots pointed at this server."""
        local = os.path.join(UI, path.lstrip("/").replace("/", os.sep))
        if os.path.commonpath((os.path.realpath(UI), os.path.realpath(local))) != os.path.realpath(UI):
            return self.send_error(404)
        try:
            st = os.stat(local)
        except OSError:
            return super().do_GET()
        key = (local, st.st_mtime, st.st_size)
        cached = _js_cache.get(key)
        if cached is None:
            with open(local, "rb") as fh:
                text = fh.read().decode("utf-8", "surrogateescape")
            hits = 0
            for old, new in (*_ORIGIN_REWRITES, *_DOWNLOAD_REWRITES):
                if old in text:
                    hits += text.count(old)
                    text = text.replace(old, new)
            cached = (text.encode("utf-8", "surrogateescape"), hits)
            _js_cache.clear()
            _js_cache[key] = cached
        body, hits = cached
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Harness-Localized", str(hits))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        global _HOST_WORK
        if not self._local_request():
            return
        admitted = False
        try:
            path = self.path.split("?", 1)[0]
            guarded = _work_request(path)
            if guarded:
                with _HOST_GATE:
                    if _HOST_SHUTDOWN:
                        return self._reject_work('Launcher is preparing to close. Reopen it before starting another operation.', 'launcher_shutting_down')
                    _HOST_WORK += 1
                    admitted = True
            if guarded:
                with installation_service.coordinator():
                    if not path.startswith("/__instances/") and (installation_service.busy() or launcher.status().get("running") or installer.state().get("active") or modpacks.busy()):
                        # Consume a normal request before closing HTTP/1.0. An
                        # unread POST body can reset the socket on Windows and
                        # hide this useful operation-conflict response.
                        length = int(self.headers.get("Content-Length") or 0)
                        if length < 0 or length > 48 * 1024 * 1024:
                            return self._send_json({"error": "Installation request is too large"}, 413)
                        if length:
                            self.rfile.read(length)
                        return self._send_json({"error": "Close Minecraft and wait for installation operations to finish before changing installations"}, 409)
                    return self._do_POST()
            return self._do_POST()
        except OSError as exc:
            return self._send_json({"error": "could not persist or access launcher data: " + str(exc)}, 500)
        except ValueError as exc:
            return self._send_json({"error": str(exc)}, 400)
        finally:
            if admitted:
                with _HOST_GATE:
                    _HOST_WORK -= 1

    def _reject_work(self, message, code):
        length = int(self.headers.get('Content-Length') or 0)
        if length < 0 or length > 48 * 1024 * 1024:
            return self._send_json({'error': 'Installation request is too large'}, 413)
        if length:
            self.rfile.read(length)
        return self._send_json({'ok': False, 'error': message, 'code': code}, 409)

    def _download_control(self, owner, body):
        try:
            snapshot = owner.control(body.get('jobId'), body.get('action'))
            return self._send_json({'ok': True, 'status': snapshot})
        except download_control.ControlError as exc:
            return self._send_json({'ok': False, 'error': str(exc), 'code': exc.code},
                                   404 if exc.code == 'missing_job' else 409)

    def _instance_request(self, path, body=None):
        try:
            parts = urllib.parse.unquote(path[len("/__instances/"):]).split("/")
            if len(parts) > 2 and not (len(parts) == 3 and parts[1:] == ['job', 'control']):
                raise installation_service.ServiceError(404, "unknown_action", "Unknown installation resource")
            ident, resource = parts[0], '/'.join(parts[1:])
            params = {key: values[-1] for key, values in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
            service = _installation_service()
            if resource == "image" and body is None:
                data = service.get(ident, "tools", {"kind": "screenshots"})
                name = params.get("name")
                item = next((item for item in data.get("items", []) if item.get("filename") == name), None)
                if not item:
                    raise installation_service.ServiceError(404, "missing_image", "Screenshot no longer exists")
                root = service._root(service._profile(ident))
                target = str(instance_content.canonical_root(os.path.join(root, item["path"])))
                if os.path.getsize(target) > 32 * 1024 * 1024:
                    raise installation_service.ServiceError(413, "image_too_large", "Screenshot is too large to preview")
                with open(target, "rb") as fh:
                    raw = fh.read()
                ctype = ("image/png" if raw.startswith(b"\x89PNG\r\n\x1a\n") else
                         "image/jpeg" if raw.startswith(b"\xff\xd8\xff") else
                         "image/gif" if raw.startswith((b"GIF87a", b"GIF89a")) else
                         "image/webp" if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP" else None)
                if not ctype:
                    raise installation_service.ServiceError(400, "invalid_image", "Unsupported screenshot data")
                return self._send_bytes(raw, ctype)
            result = service.get(ident, resource, params) if body is None else service.post(ident, resource, body)
            if body is not None and resource in ("general", "copy") and result.get("ok"):
                # Native list/version slices need the persisted result too;
                # an editor-only acknowledgement leaves its parent list stale.
                result = dict(result, profiles=_effective_configurations(), gameVersions=_load_versions())
            return self._send_json(result)
        except (installation_service.ServiceError, instance_content.ContentError, content_provider.ProviderError) as exc:
            return self._send_json({"ok": False, "error": str(exc), "code": exc.code, "details": exc.details}, getattr(exc, "status", 409))
        except (ValueError, TypeError, KeyError) as exc:
            return self._send_json({"ok": False, "error": "Invalid installation request: " + str(exc)}, 400)
        except OSError as exc:
            return self._send_json({"ok": False, "error": "Could not access installation files: " + str(exc)}, 500)

    def _do_POST(self):
        global _HOST_SHUTDOWN
        path = self.path.split("?", 1)[0]
        # This server is the host: a page from another origin must not be able
        # to drive it (install, launch, read files). Browsers send Origin on
        # cross-site POSTs, and it cannot be forged from script.
        # Telemetry and other fire-and-forget beacons land on the stub host. They
        # carry no data this app needs, but answering 404 makes every page log a
        # failed request; accept and discard them instead.
        if path.startswith("/__stub/"):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            return self._send_json({"ok": True})
        # The client announces itself to the session service when joining a
        # multiplayer server; offline the game never needs it, but answering
        # keeps a LAN/realms attempt from hanging on a dead endpoint.
        if path.startswith("/session/"):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            return self._send_json({"ok": True})
        if path == "/__msa/config":
            lengths = self.headers.get_all("Content-Length", [])
            if (len(lengths) != 1 or self.headers.get_all("Transfer-Encoding")
                    or not lengths[0].strip().isascii() or not lengths[0].strip().isdecimal()):
                return self._send_json({"error": "Microsoft configuration request has invalid HTTP framing", "code": "INVALID_MSA_CONFIGURATION"}, 400)
            try:
                length = int(lengths[0].strip())
                if length > 1024:
                    return self._send_json({"error": "Microsoft configuration request is too large", "code": "INVALID_MSA_CONFIGURATION"}, 413)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    return self._send_json({"error": "Microsoft configuration request body is incomplete", "code": "INVALID_MSA_CONFIGURATION"}, 400)
                body = json.loads(raw or b"{}")
            except (ValueError, UnicodeDecodeError):
                return self._send_json({"error": "Microsoft configuration must be valid JSON", "code": "INVALID_MSA_CONFIGURATION"}, 400)
            if not isinstance(body, dict) or set(body) != {"clientId"}:
                return self._send_json({"error": "Provide only the public application (client) ID", "code": "INVALID_MSA_CONFIGURATION"}, 400)
            try:
                return self._send_json(msa.save_client_id(body["clientId"]))
            except msa.ConfigurationBusy as exc:
                return self._send_json({"error": str(exc), "code": "MSA_SIGNIN_IN_PROGRESS"}, 409)
            except ValueError as exc:
                return self._send_json({"error": str(exc), "code": "INVALID_MSA_CLIENT_ID"}, 400)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or path.startswith("/__instances/") and length > 48 * 1024 * 1024:
                return self._send_json({"error": "Installation request is too large"}, 413)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self.send_error(400)
            return
        if not isinstance(body, dict):
            return self._send_json({"error": "request body must be an object"}, 400)
        if path == '/__lifecycle/prepare-shutdown':
            with _HOST_GATE:
                status = _lifecycle_status()
                safe = not status['gameRunning'] and not status['activeWork']
                if safe:
                    _HOST_SHUTDOWN = True
                return self._send_json({'safeToStop': safe})
        if path == '/__lifecycle/resume':
            with _HOST_GATE:
                _HOST_SHUTDOWN = False
                return self._send_json({'ok': True})
        if path.startswith("/__instances/"):
            return self._instance_request(path, body)
        if path == "/__accounts":
            status, payload = account_action(body if isinstance(body, dict) else {})
            return self._send_json(payload, status)
        if path == "/__msa/logout":
            msa.logout()
            return self._send_json(accounts_payload())
        if path == "/__capes":
            status, payload = _add_cape(
                (body or {}).get("name") if isinstance(body, dict) else None,
                (body or {}).get("data") if isinstance(body, dict) else None,
            )
            return self._send_json(payload, status)
        if path == "/__launcher_settings":
            current = _update_launcher_settings(body)
            return self._send_json({"ok": True, "settings": current})
        if path == "/__configuration_settings":
            settings = _update_configuration_settings(body)
            return self._send_json({"ok": True, "settings": settings})
        if path == "/__skin_state":
            _write_skin_state(body)
            return self._send_json({"ok": True})
        if path == "/__open":
            target = str((body or {}).get("path") or "")
            return self._send_json(_open_path(target))
        if path == "/__dialog":
            options = (body or {}).get("options") or {}
            return self._send_json({"path": _pick_path(options)})
        if path == "/__read_file":
            return self._send_json(_read_file_body((body or {}).get("path")))
        if path == "/__launch":
            if modpacks.busy():
                return self._send_json({"error": "wait for installation operations to finish before launching"}, 409)
            # A press on an installation's Play button carries that installation's
            # id, not a version: launcher.dll reads lastVersionId (and the run
            # settings) out of launcher_profiles.json itself, so this resolves the
            # same profile the UI is showing.
            configuration = _configuration((body or {}).get("configurationId"))
            if body.get("configurationId") and not configuration:
                return self._send_json({"error": "No such configuration"}, 404)
            configured = (configuration or {}).get("lastVersionId")
            vid = _resolve_alias(configured or (body or {}).get("versionId"))
            captured = _quickplay_configurations(_load_overlay()).get(body.get("configurationId")) if configuration else None
            launch_context = _quickplay_context()
            try:
                player = _selected_game_identity()
                result = launcher.start(
                        STATE, GAME_DIR, vid,
                        log_path=GAME_LOG,
                        run_dir=(configuration or {}).get("gameDir") or None,
                        java_dir=(configuration or {}).get("javaDir") or None,
                        java_args=(configuration or {}).get("javaArgs") or None,
                        resolution=(configuration or {}).get("resolution") or None,
                        server_port=self.server.server_address[1],
                        # Do not use `localhost`: it can resolve to IPv6 while
                        # this server is bound to IPv4. The discovery URL must
                        # target the actual listener the launcher started.
                        server_host=self.server.server_address[0],
                        # An active Microsoft account launches with its real
                        # Minecraft profile and token; offline accounts keep
                        # the legacy path launcher.py already builds.
                        player=player,
                        configuration_id=body.get('configurationId') if configuration else None,
                    )
                globals()["_LAUNCH_INSTANCE"] = body.get("configurationId") if configuration else None
                if captured and result.get("running"):
                    try:
                        data, warning = _record_quickplay_launch(body["configurationId"], captured, launch_context)
                        result = dict(result, quickPlayData=data)
                        if warning:
                            result["warning"] = warning
                    except (OSError, ValueError) as exc:
                        result = dict(result, warning="Minecraft started, but Quick Play history could not be saved: " + str(exc))
                return self._send_json(result)
            except RuntimeError as exc:
                return self._send_json({"error": str(exc)}, 409)
        if path == "/__launch_stop":
            return self._send_json(launcher.stop())
        if path == "/__uninstall":
            return self._send_json(_uninstall((body or {}).get("productId"), bool((body or {}).get("removeSavedData"))))
        if path == "/__install_control":
            return self._download_control(installer, body)
        if path == '/__modpacks/control':
            return self._download_control(modpacks, body)
        if path == "/__configurations/create":
            configuration = (body or {}).get("configuration")
            if not isinstance(configuration, dict):
                return self._send_json({"error": "configuration missing"}, 400)
            ident = uuid.uuid4().hex
            profile = dict(configuration)
            created = datetime.datetime.now(datetime.timezone.utc)
            profile.setdefault("created", created.isoformat().replace("+00:00", "Z"))
            profile.setdefault("type", "custom")
            profiles = _save_configuration(ident, profile)
            return self._send_json({
                "ok": True,
                "id": ident,
                "configuration": dict(profile, id=ident),
                "profiles": profiles,
            })
        if path == "/__configurations/update":
            ident = str((body or {}).get("id") or "")
            patch = (body or {}).get("patch")
            if not ident:
                return self._send_json({"error": "configuration id missing"}, 400)
            if not isinstance(patch, dict):
                return self._send_json({"error": "configuration patch missing"}, 400)
            with _STORE_LOCK:
                current = _effective_configurations().get(ident)
                if current is None:
                    return self._send_json({"error": "No such configuration"}, 404)
                profile = dict(current, **patch)
                profiles = _save_configuration(ident, profile)
            return self._send_json({
                "ok": True,
                "configuration": dict(profile, id=ident),
                "profiles": profiles,
            })
        if path == "/__configurations/delete":
            ident = str((body or {}).get("id") or "")
            if not ident:
                return self._send_json({"error": "configuration id missing"}, 400)
            return self._send_json({"ok": True, "profiles": _delete_configuration(ident)})
        if path in ("/__quickplay/update", "/__quickplay/delete"):
            try:
                return self._send_json(_mutate_quickplay(path.rsplit("/", 1)[1], body))
            except quickplay.QuickPlayError as exc:
                return self._send_json({"error": str(exc), "code": exc.code}, exc.status)
        if path == "/__install":
            if modpacks.busy():
                return self._send_json({"error": "wait for installation operations to finish before installing the game"}, 409)
            configuration = _configuration((body or {}).get("configurationId"))
            if body.get("configurationId") and not configuration:
                return self._send_json({"error": "No such configuration"}, 404)
            vid = _resolve_alias(
                (configuration or {}).get("lastVersionId") or (body or {}).get("versionId")
            )
            if not vid:
                return self._send_json({"error": "no version in the manifest"}, 400)
            run_dir = (configuration or {}).get("gameDir") or None
            version_json, error = _version_metadata(vid, run_dir)
            if error:
                return self._send_json({"error": error}, 404)
            return self._send_json(installer.start(GAME_DIR, STATE, version_json, run_dir=run_dir))
        if path == "/__loaders/materialize":
            # The New installation form's loader picker: resolve the latest
            # stable Fabric/Quilt loader for the chosen Minecraft version,
            # flatten it onto the vanilla document and store the result in the
            # local version store, then hand back the version id the new
            # profile's lastVersionId should point at. Same document, same
            # store and the same write a modpack import performs for its pack.
            # The form names what it shows -- `latest-release`, `release 26.2`
            # -- so the alias resolves here against the manifest, exactly as
            # an install press does.
            minecraft = _resolve_alias(str((body or {}).get("minecraft") or "") or None)
            loader = str((body or {}).get("loader") or "")
            version = str((body or {}).get("version") or "")
            if not minecraft:
                return self._send_json({"error": "no Minecraft version selected"}, 400)
            try:
                version_id = modpacks.materialize_loader(
                    STATE, minecraft, loader, _base_version_doc, version=version,
                    game_dir=GAME_DIR,
                )
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)
            return self._send_json({"ok": True, "versionId": version_id})
        if path == "/__modpacks/import-file":
            if modpacks.busy():
                return self._send_json({"error": "another modpack job is already running"}, 409)
            try:
                # The client cannot supply an arbitrary file path for import.
                selected = _pick_path({'filters': ['.mrpack'], 'defaultPath': GAME_DIR})
                if not selected:
                    return self._send_json({'ok': True, 'cancelled': True, 'started': False})
                started = modpacks.start_import_file(GAME_DIR, STATE, selected,
                    base_version=_base_version_doc, on_done=_modpack_imported)
                if not started:
                    return self._send_json({'error': 'another modpack job is already running'}, 409)
            except instance_content.ContentError as exc:
                return self._send_json({'ok': False, 'error': str(exc), 'code': exc.code,
                                        'details': exc.details}, 400)
            except (OSError, ValueError) as exc:
                return self._send_json({'error': str(exc)}, 400)
            return self._send_json({'ok': True, 'started': True})
        if path == "/__modpacks/import":
            project_id = str((body or {}).get("projectId") or "")
            version_id = (body or {}).get("versionId")
            if not project_id:
                return self._send_json({"error": "no modpack selected"}, 400)
            if modpacks.busy():
                return self._send_json({"error": "another modpack job is already running"}, 409)
            try:
                started = modpacks.start_import(
                    GAME_DIR, STATE, project_id,
                    version_id=version_id if isinstance(version_id, str) else None,
                    base_version=_base_version_doc,
                    on_done=_modpack_imported,
                )
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)
            return self._send_json({"ok": True, "started": started})
        if path == "/__modpacks/export":
            configuration = _configuration((body or {}).get("configurationId"))
            if not configuration:
                return self._send_json({"error": "no such installation"}, 404)
            # The destination is a real native folder picker, the same one the
            # cape upload uses for files; cancelling it cancels the export.
            # An explicit `path` skips the picker (scripted exports).
            out_dir = str((body or {}).get("path") or "")
            if not out_dir:
                out_dir = _pick_path({
                    "directory": True,
                    "defaultPath": configuration.get("gameDir") or GAME_DIR,
                })
            if not out_dir:
                return self._send_json({"error": "No folder was chosen — the export was cancelled."})
            if not modpacks.start_export(
                GAME_DIR, configuration, out_dir,
                dependencies=_modpack_dependencies(configuration),
            ):
                return self._send_json({"error": "another modpack job is already running"}, 409)
            return self._send_json({"ok": True})
        if not path.startswith("/__state/"):
            self.send_error(404)
            return
        write_store(path[len("/__state/"):], body.get("value"))
        self._send_json({"ok": True})

    # -- responses -------------------------------------------------------
    def _send_bytes(self, body, ctype, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store" if ctype == "image/png" else "max-age=60")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_cape(self, rest):
        """One uploaded cape PNG from the id the editor was given."""
        name = rest.split("?", 1)[0].strip("/")
        cape = next((c for c in _load_capes() if c["id"] + ".png" == name), None)
        if not cape:
            self.send_error(404)
            return
        try:
            with open(cape["file"], "rb") as fh:
                self._send_bytes(fh.read(), "image/png")
        except OSError:
            self.send_error(404)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, p, ctype):
        try:
            with open(p, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_media(self, path):
        """Serve real media when restored, else a stand-in that keeps layout."""
        local = os.path.join(UI, path.lstrip("/").replace("/", os.sep))
        if os.path.isfile(local):
            return super().do_GET()
        # Original pack members recovered by CRC stay in the harness, keeping
        # re-extraction output untouched. Resolve the full requested path;
        # basename matching could substitute unrelated art from another pack.
        media_root = os.path.realpath(os.path.join(ASSETS, "media"))
        restored = os.path.realpath(os.path.join(
            media_root, path[len("/static/media/"):].replace("/", os.sep)
        ))
        try:
            contained = os.path.commonpath((media_root, restored)) == media_root
        except ValueError:
            contained = False
        if not contained:
            self.send_error(404)
            return
        if os.path.isfile(restored):
            ctype = mimetypes.guess_type(restored)[0] or "application/octet-stream"
            return self._send_file(restored, ctype)
        # Open-licensed stand-ins for fonts the capture could not recover (the
        # packs are encrypted; these keep the intended typeface family).
        basename = os.path.basename(path)
        substitute = os.path.join(FONTS, basename)
        if os.path.isfile(substitute):
            return self._send_file(substitute, "font/woff2")
        if path.startswith("/static/media/fonts/"):
            for prefix, stand_in in FONT_FALLBACKS:
                if basename.startswith(prefix):
                    stand_in_path = os.path.join(FONTS, stand_in)
                    if os.path.isfile(stand_in_path):
                        return self._send_file(stand_in_path, "font/woff2")
        ext = os.path.splitext(path)[1].lower()
        if path not in _missing_media:
            _missing_media.add(path)
            sys.stdout.write("[media] unrecovered asset (placeholder): %s\n" % path)
            sys.stdout.flush()
        if ext in _IMAGE_EXTS:
            body, ctype = _PLACEHOLDER_GIF, "image/gif"
        elif ext == ".svg":
            body, ctype = _PLACEHOLDER_SVG, "image/svg+xml"
        elif ext in _MEDIA_EXTS:
            self.send_response(204)
            self.send_header("X-Harness-Media", "unrecovered")
            self.end_headers()
            return
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Harness-Media", "unrecovered")
        self.send_header("Cache-Control", "max-age=60")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_preview(self):
        with open(os.path.join(UI, "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        # The app ships relative asset paths (`static/js/...`) because launcher.dll
        # always serves it from the same virtual root; on a deep link the browser
        # would otherwise resolve them under the route, so pin the base.
        # Sidebar scope: this build ships Java and Bedrock; the other products
        # stay in appData (every product store, route and purchase dialog in the
        # app indexes the full set) but their nav entries are hidden. This rule
        # lands in <head>, so nothing flashes before bridge.js runs.
        hide_css = (
            "<style>"
            '[id$="-menulink-wrap"]:has(a[href^="/games/dungeons/"],'
            'a[href^="/games/dungeons2/"],a[href^="/games/legends/"])'
            "{display:none !important}"
            "</style>"
        )
        inject = (
            '<base href="/">'
            + hide_css
            + "<script>window.__APPDATA__="
            + json.dumps(build_appdata()).replace("</", "<\\/")
            + ";</script><script src=\"/__bridge.js\"></script><script src=\"/__ui.js\"></script><script src=\"/__installation_ui.js\"></script>"
        )
        html = html.replace("<head>", "<head>" + inject, 1)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))
        sys.stdout.flush()


def _watch_stamp(path):
    """(mtime, size), or None when the file is mid-write / missing."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    stamp = (st.st_mtime, st.st_size)
    if os.path.abspath(path) == os.path.abspath(__file__):
        # Older watcher threads retain their original path list. Including the
        # workspace modules here makes those threads see newly added sources.
        siblings = []
        for name in ("installation_service.py", "instance_content.py", "content_provider.py", "content_resolver.py", "instance_tools.py", "quickplay.py"):
            try:
                child = os.stat(os.path.join(os.path.dirname(__file__), name))
                siblings.append((child.st_mtime, child.st_size))
            except OSError:
                siblings.append(None)
        return stamp + (tuple(siblings),)
    return stamp


def _reload_host(srv):
    """Re-read the harness sources into the running process.

    Siblings are reloaded as modules; this file cannot be (it runs as
    `__main__`, which has no import spec), so its source is re-executed into its
    own dict instead -- the dict a running module keeps its whole life, so every
    handler still resolves names through it. `_RELOADING` stops the bottom of
    the file from calling `main()` and binding the port a second time, and the
    new `Handler` is swapped onto the server so the next connection uses it.
    """
    global _RELOADING
    with installation_service.coordinator():
        if installation_service.busy() or launcher.status().get("running") or installer.state().get("active") or modpacks.busy():
            if not globals().get("_PENDING_RELOAD"):
                globals()["_PENDING_RELOAD"] = True
                def later():
                    while installation_service.busy() or launcher.status().get("running") or installer.state().get("active") or modpacks.busy():
                        time.sleep(0.5)
                    globals()["_PENDING_RELOAD"] = False
                    _reload_host(srv)
                threading.Thread(target=later, name="pending-host-reload", daemon=True).start()
            return False
        return _reload_idle_host(srv)


def _reload_idle_host(srv):
    global _RELOADING
    # Keep completed diagnostics and process ownership when replacing code.
    launch_state = {name: getattr(launcher, name) for name in ("_LOCK", "STATE", "LINES", "_LINE_OFFSET", "_PROCESS", "_LOG_PATH", "_SECRET_VALUES")}
    for mod in (content_provider, instance_content, content_resolver, instance_tools, installation_service, quickplay, launcher, installer, local_api, modpacks, msa):
        try:
            importlib.reload(mod)
        except Exception:
            traceback.print_exc()
    for name, value in launch_state.items():
        setattr(launcher, name, value)
    # A reload re-executes this file, which re-imports msa fresh; restore the
    # signed-in session state the old module held.
    try:
        msa.reset_after_reload()
    except Exception:
        pass
    me = sys.modules[__name__]
    _RELOADING = True
    try:
        with open(os.path.abspath(__file__), encoding="utf-8") as fh:
            exec(compile(fh.read(), __file__, "exec"), me.__dict__)
    except Exception:
        # A syntax error fails at compile(), before a line runs, so the old
        # server keeps serving; anything else leaves the dict partially
        # updated, which is why the traceback goes to the log unfiltered.
        traceback.print_exc()
    finally:
        _RELOADING = False
    if isinstance(getattr(me, "Handler", None), type):
        srv.RequestHandlerClass = me.Handler
    print("[hot-reload] re-read harness sources", flush=True)
    return True


def _watch_sources(srv):
    # bridge.js / ui.js are read from disk per request and need no watching;
    # these are the ones Python holds in memory. The watcher's own code is only
    # read when the thread starts, so changing it takes one restart.
    paths = (
        os.path.abspath(__file__),
        os.path.abspath(local_api.__file__),
        os.path.abspath(installer.__file__),
        os.path.abspath(launcher.__file__),
        os.path.abspath(modpacks.__file__),
        os.path.abspath(msa.__file__),
        os.path.abspath(installation_service.__file__),
        os.path.abspath(instance_content.__file__),
        os.path.abspath(content_provider.__file__),
        os.path.abspath(content_resolver.__file__),
        os.path.abspath(instance_tools.__file__),
        os.path.abspath(quickplay.__file__),
    )
    stamps = {p: _watch_stamp(p) for p in paths}
    while True:
        time.sleep(0.5)
        if not any(_watch_stamp(p) != stamps[p] for p in paths):
            continue
        time.sleep(0.25)  # let the editor finish writing
        for p in paths:
            stamps[p] = _watch_stamp(p)
        _reload_host(srv)


def main():
    argv = sys.argv[1:]
    if argv[:1] == ["--pick-path"]:
        return _pick_path_mode(argv[1:])
    port = int(argv[0]) if argv else 8790
    _seed_data_dir()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("serving %s on http://127.0.0.1:%d/" % (UI, port), flush=True)
    # A packaged build ships read-only sources: there is nothing to watch, and
    # reading this file again would only re-run the server setup.
    if not getattr(sys, "frozen", False) and os.environ.get("LAUNCHER_NO_RELOAD") != "1":
        print("[hot-reload] watching harness/*.py -- edit, then reload the page", flush=True)
        threading.Thread(target=_watch_sources, args=(srv,), daemon=True).start()
    srv.serve_forever()


# The watcher re-executes this file in the same process; `_RELOADING` is set
# in this module's dict while that happens so the guard below does not reach
# `main()` and try to bind the port a second time. It is deliberately never
# assigned at the top level: a re-exec would reset it before the guard ran.
if __name__ == "__main__" and not globals().get("_RELOADING", False):
    main()
