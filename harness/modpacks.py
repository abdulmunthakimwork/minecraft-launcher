"""Modpack browsing, import and export against Modrinth.

The UI cannot reach the network (`bridge.js` localises every remote URL), so
the catalog, the pack download and the install all happen here, on the host --
the same split the old Modrinth mod screen used, now at pack scale. A pack is a
`.mrpack` zip, Modrinth's open pack format: `modrinth.index.json` names the
Minecraft version, the mod loader and every file the instance needs, and
`overrides/` carries whatever is not served from Modrinth's CDN.

    search(query)                      -> catalog hits, icons inlined as data URIs
    start_import(...)                  -> background import job (state() reports it)
    export(game_dir, profile, out_dir) -> a .mrpack for one installation

Import creates nothing outside the game directory: pack files land in
`instances/<slug>/` and the installation points its `gameDir` there, so a
modpack never touches another installation's files. Every path that comes out
of a remote document is checked to stay inside that folder (a zip can name
`../../Windows/System32` just as easily as `mods/ sodium.jar`), every download
is written to a temp file and moved into place, and a dropped connection
leaves no half-written jar behind.

Loaders: the pack index names one of fabric-loader / quilt-loader / forge /
neoforge. Fabric and Quilt publish a complete version document (a "profile")
from their official meta endpoints, which is merged with the vanilla version
document into a self-contained version the launcher already knows how to
install and launch. Forge and NeoForge build their version documents with a
Java installer instead of publishing one; their declared Minecraft runtime is
prepared before running that official installer in a private staging folder.
"""
import base64
import concurrent.futures
import datetime
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
import download_control

API = "https://api.modrinth.com/v2"
CDN = "https://cdn.modrinth.com/"
AGENT = "minecraft-launcher-harness/1.0 (local desktop shell)"
# The two loaders that publish a launchable profile document; their version
# JSON comes straight from the loader's own meta service.
LOADER_META = {
    "fabric-loader": "https://meta.fabricmc.net/v2/versions/loader/%s/%s/profile/json",
    "quilt-loader": "https://meta.quiltmc.org/v3/versions/loader/%s/%s/profile/json",
}
KNOWN_LOADERS = set(LOADER_META) | {"forge", "neoforge"}

# --- Forge / NeoForge -----------------------------------------------------
# Neither Forge nor NeoForge publishes a profile JSON: their version document
# is produced by an official installer that downloads libraries and runs
# build-specific processor steps. The launcher therefore installs them the
# way those installers themselves would -- but only ever from the loader's
# own Maven host, never from a URL a pack names:
#
#   1. download the official installer jar (path built from the loader id,
#      the Minecraft version and the loader version, all validated tokens),
#   2. verify it against the same Maven's published SHA-1 sidecar (no
#      sidecar, no install: fail closed),
#   3. run it against a private staging folder with the game's own Java
#      runtime, and
#   4. copy the versions/ and libraries/ files it produced into the state
#      directory, skipping anything already there.
#
# The staging folder is deleted afterwards, so a failed or malicious run
# never leaves half-written state behind, and an existing installation is
# never overwritten.
FORGE_INSTALLER = (
    "https://maven.minecraftforge.net/net/minecraftforge/forge/"
    "{mc}-{version}/forge-{mc}-{version}-installer.jar"
)
NEOFORGE_INSTALLER = (
    "https://maven.neoforged.net/releases/net/neoforged/neoforge/"
    "{version}/neoforge-{version}-installer.jar"
)
# NeoForge's 1.20.1 line shipped as net.neoforged:forge; its maintainers
# discourage it, but packs still name it, so the import path knows the URL.
NEOFORGE_LEGACY_INSTALLER = (
    "https://maven.neoforged.net/releases/net/neoforged/forge/"
    "{mc}-{version}/forge-{mc}-{version}-installer.jar"
)
# Forge's official promotion list: one document, keyed `<mc>-latest` /
# `<mc>-recommended` (shape verified live against files.minecraftforge.net).
FORGE_PROMOTIONS = (
    "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
)
# NeoForge's global Maven list; the Minecraft line is derived from the prefix.
NEOFORGE_METADATA = (
    "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"
)
INSTALLER_TIMEOUT = 900
_VERSION_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _loader_name(loader):
    return {"forge": "Forge", "neoforge": "NeoForge"}.get(loader, str(loader).title())


def _require_token(value, what):
    """A version-shaped path token; anything else never enters a URL or a path."""
    text = str(value or "").strip()
    if not _VERSION_TOKEN.match(text):
        raise ValueError("%s is not a valid %s" % (value or "", what))
    return text


def installer_url(loader, minecraft, version):
    """The official installer URL for (loader, minecraft, version)."""
    version = _require_token(version, "loader version")
    if loader == "forge":
        minecraft = _require_token(minecraft, "Minecraft version")
        return FORGE_INSTALLER.format(mc=minecraft, version=version)
    if loader == "neoforge":
        if str(minecraft) == "1.20.1":
            return NEOFORGE_LEGACY_INSTALLER.format(
                mc=_require_token(minecraft, "Minecraft version"), version=version
            )
        return NEOFORGE_INSTALLER.format(version=version)
    raise ValueError("%s has no installer this launcher knows" % _loader_name(loader))


def _write_verified_download(url, dest, timeout=180):
    """Download `url` to `dest`, requiring the host's `.sha1` sidecar to match."""
    with urllib.request.urlopen(
        _request(url + ".sha1", timeout), timeout=timeout, context=_context()
    ) as response:
        expected = (response.read(512).decode("utf-8", "replace").split() or [""])[0].lower()
    if not re.match(r"^[0-9a-f]{40}$", expected):
        raise ValueError("%s publishes no usable SHA-1 for its installer" % url)
    return download_control.download(url, dest, hashes={"sha1": expected}, timeout=timeout,
        opener=lambda request, timeout: urllib.request.urlopen(request, timeout=timeout, context=_context()))


def _find_java(game_dir, state_dir):
    """A Java executable: the game's bundled runtime first, then the PATH."""
    suffix = "java.exe" if os.name == "nt" else "java"
    for root in (game_dir, state_dir):
        runtime = os.path.join(root, "runtime")
        try:
            components = sorted(os.listdir(runtime))
        except OSError:
            continue
        for component in components:
            for name in (suffix, "javaw.exe"):
                candidate = os.path.join(runtime, component, "bin", name)
                if os.path.isfile(candidate):
                    return candidate
    for name in (suffix, "javaw.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _merge_tree(src_root, dst_root):
    """Copy files from `src_root` into `dst_root`; existing files are kept."""
    copied = 0
    for dirpath, _dirs, files in os.walk(src_root):
        relative_dir = os.path.relpath(dirpath, src_root)
        for name in files:
            source = os.path.join(dirpath, name)
            target = dst_root if relative_dir == "." else os.path.join(dst_root, relative_dir)
            target = os.path.join(target, name)
            if os.path.exists(target):
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(source, target)
            copied += 1
    return copied


def install_forge_like(game_dir, state_dir, loader, minecraft, version,
                       base_version=None):
    """Run the loader's official installer in staging; returns the version id.

    The installer is the only supported way to build a Forge/NeoForge version
    document (its processors generate files no Maven artifact carries), so it
    does run -- as verified official code, against a throwaway folder, with the
    Minecraft version and profile files the installer itself downloads. What
    survives is only the versions/ and libraries/ output, merged into the
    state directory without overwriting anything already there.
    """
    url = installer_url(loader, minecraft, version)
    os.makedirs(state_dir, exist_ok=True)
    stage = tempfile.mkdtemp(prefix=".loader-", dir=state_dir)
    try:
        # The installer refuses to run without launcher-profile state beside
        # its target; an empty document satisfies it and keeps it away from
        # the user's real launcher_profiles.json.
        with open(os.path.join(stage, "launcher_profiles.json"), "w", encoding="utf-8") as fh:
            json.dump({"profiles": {}, "settings": {}}, fh)
        jar = _write_verified_download(url, os.path.join(stage, "installer.jar"))
        if base_version:
            parent, error = base_version(minecraft)
            if error or not isinstance(parent, dict):
                raise ValueError(error or ("Minecraft %s could not be resolved" % minecraft))
            import installer
            java = installer.ensure_runtime(game_dir, state_dir, parent)
        else:
            java = _find_java(game_dir, state_dir)
        if not java:
            raise ValueError(
                "no Java runtime found: install the Minecraft version first "
                "(its runtime is what runs the %s installer) or put Java on PATH"
                % _loader_name(loader)
            )
        owned = download_control.current()
        with (owned.noninterruptible("Running the official loader installer") if owned else download_control.bind(None)):
            run = subprocess.run(
                [java, "-jar", jar, "--installClient", stage],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=INSTALLER_TIMEOUT, cwd=stage,
            )
        output = run.stdout.decode("utf-8", "replace").strip()
        if run.returncode != 0:
            tail = " ".join(output.splitlines()[-8:])
            raise ValueError("the %s installer failed: %s" % (_loader_name(loader), tail or "no output"))
        staged_versions = os.path.join(stage, "versions")
        ids = []
        try:
            candidates = sorted(os.listdir(staged_versions))
        except OSError:
            candidates = []
        for ident in candidates:
            path = os.path.join(staged_versions, ident, ident + ".json")
            try:
                with open(path, encoding="utf-8") as fh:
                    doc = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict) and doc.get("id"):
                ids.append(str(doc["id"]))
        if not ids:
            raise ValueError(
                "the %s installer produced no version document" % _loader_name(loader)
            )
        # One installer installs one loader version; when a candidate set is
        # larger (an installer that also touched the vanilla version), the id
        # carrying the requested loader version wins.
        matching = [i for i in ids if str(version) in i]
        version_id = (matching or ids)[0]
        with (owned.noninterruptible("Materializing verified loader files") if owned else download_control.bind(None)):
            _merge_tree(os.path.join(stage, "versions"), os.path.join(state_dir, "versions"))
            _merge_tree(os.path.join(stage, "libraries"), os.path.join(state_dir, "libraries"))
        return version_id
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def neoforge_versions_for(minecraft, versions):
    """The NeoForge builds published for one Minecraft release, newest first.

    From 1.20.5 on, NeoForge's version encodes the Minecraft line (1.21.1 ->
    21.1.x), so the global Maven list can be filtered safely. Older lines are
    not offered here: their version schemes do not map cleanly, and guessing
    would hand the picker combinations that cannot launch.
    """
    match = re.match(r"^1\.(\d+)(?:\.(\d+))?$", str(minecraft or ""))
    if not match:
        return []
    minor, patch = int(match.group(1)), int(match.group(2) or 0)
    if (minor, patch) < (20, 5):
        return []
    prefix = "%d.%d." % (minor, patch)
    picked = [v for v in versions if str(v).startswith(prefix)]
    return sorted(picked, key=_version_key, reverse=True)[:LOADER_CHOICES_MAX]


def _version_key(version):
    return tuple(int(p) for p in re.findall(r"\d+", str(version))) or (0,)
# Modrinth's search API reports the loader under `categories`.
CATEGORY_LOADERS = {"fabric": "fabric-loader", "quilt": "quilt-loader",
                    "forge": "forge", "neoforge": "neoforge"}
ICON_LIMIT = 256 * 1024        # a project icon larger than this is not worth inlining
BLOCK = 1 << 16

# --- job state (mirrors installer.py: one job at a time, polled by the UI) --
_STATE_LOCK = threading.Lock()
_CONTROL = None
_STATE = {
    "job": None,               # "import" | "export" | None
    "phase": "idle",
    "message": "",
    "error": None,
    "done": 0,
    "total": 0,
    "result": None,
}


def state():
    with _STATE_LOCK:
        out = dict(_STATE)
        owned = _CONTROL if _CONTROL and _STATE.get("jobId") == _CONTROL.id else None
    out.update(owned.snapshot() if owned else {"jobId": None, "controlStatus": "idle",
        "paused": False, "cancelRequested": False, "canPause": False, "canResume": False,
        "canCancel": False, "controlReason": "No active modpack import."})
    if out.get("job") == "export":
        out.update(canPause=False, canResume=False, canCancel=False,
                   controlReason="Modpack export cannot be interrupted safely.")
    return out


def control(job_id, action):
    with _STATE_LOCK:
        owned = _CONTROL
        if not owned or owned.id != job_id or _STATE.get("jobId") != job_id:
            raise download_control.ControlError("missing_job", "This modpack job is no longer current.")
        if _STATE.get("job") == "export":
            raise download_control.ControlError("control_unavailable", "Modpack export cannot be interrupted safely.")
    owned.request(action)
    return state()


def busy():
    with _STATE_LOCK:
        return _STATE["job"] is not None


def _begin(job):
    global _CONTROL
    with _STATE_LOCK:
        if _STATE["job"]:
            return False
        _CONTROL = download_control.JobControl()
        _STATE.update({"job": job, "phase": "starting", "message": "", "jobId": _CONTROL.id,
                       "error": None, "done": 0, "total": 0, "result": None})
        return True


def _set(**kw):
    owned = download_control.current()
    with _STATE_LOCK:
        if not owned or _STATE.get("jobId") == owned.id:
            _STATE.update(kw)


def _finish(error=None, result=None):
    owned = download_control.current()
    with _STATE_LOCK:
        if owned and _STATE.get("jobId") != owned.id:
            return
        cancelled = isinstance(error, download_control.DownloadCancelled)
        if owned:
            owned.finish("cancelled" if cancelled else "error" if error else "done")
        _STATE.update({"job": None, "phase": "error" if error else "done",
                       "error": str(error) if error and not cancelled else None, "result": result,
                       "message": str(error) if error else _STATE["message"]})
        if cancelled:
            _STATE["phase"] = "cancelled"


# --- http ------------------------------------------------------------------
def _context():
    """TLS context for Modrinth's hosts.

    Python's default trust store on Windows is the OpenSSL bundle, which does
    not carry the CA that signs cdn.modrinth.com (the API host already verifies).
    `certifi` is the same bundle requests uses; when it is missing, the default
    store stays in place -- verification is never disabled.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _request(url, timeout=30, headers=None):
    head = {"User-Agent": AGENT}
    head.update(headers or {})
    return urllib.request.Request(url, headers=head)


def _get(path, params=None, timeout=30):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(
        _request(url, timeout, {"Accept": "application/json"}), timeout=timeout,
        context=_context(),
    ) as response:
        return json.load(response)


def _get_url(url, timeout=30):
    with urllib.request.urlopen(_request(url), timeout=timeout, context=_context()) as response:
        return json.load(response)


def _quote(value):
    return urllib.parse.quote(str(value), safe="")


# --- catalog ---------------------------------------------------------------
def _icon_uri(url):
    """A project icon as a data URI.

    The page cannot load it directly: `bridge.js` localises remote image URLs,
    so a Modrinth CDN icon would be replaced by the harness' transparent
    stub. Inlining is also what keeps the dialog working with no network on
    the page side -- the host has already fetched it by then.
    """
    if not isinstance(url, str) or not url.startswith(CDN):
        return None
    try:
        with urllib.request.urlopen(_request(url), timeout=20, context=_context()) as response:
            blob = response.read(ICON_LIMIT + 1)
            ctype = response.headers.get("Content-Type") or "image/png"
        if len(blob) > ICON_LIMIT:
            return None
        return "data:%s;base64,%s" % (ctype, base64.b64encode(blob).decode("ascii"))
    except Exception:
        return None


def search(query, limit=18, offset=0):
    """Modrinth's modpack catalog.

    An empty query means "the most downloaded packs", which is also the list
    the dialog opens with; a query switches to relevance. Loaders are reported
    under `categories` by the search API, so each hit carries the loader its
    latest version declares. `offset` pages the catalog: the dialog asks for
    the next window as its list scrolls to the bottom.
    """
    params = {
        "query": query or "",
        "limit": str(limit),
        "offset": str(max(0, int(offset or 0))),
        "index": "relevance" if query else "downloads",
        "facets": json.dumps([["project_type:modpack"]]),
    }
    doc = _get("/search", params)
    hits = doc.get("hits") or []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        icons = list(pool.map(_icon_uri, [hit.get("icon_url") for hit in hits]))
    out = []
    for hit, icon in zip(hits, icons):
        categories = hit.get("categories") or []
        loader = next((CATEGORY_LOADERS[c] for c in categories if c in CATEGORY_LOADERS), None)
        out.append({
            "id": hit.get("project_id"),
            "slug": hit.get("slug"),
            "title": hit.get("title"),
            "description": hit.get("description"),
            "author": hit.get("author"),
            "downloads": hit.get("downloads"),
            "followers": hit.get("followers"),
            "updated": hit.get("date_modified"),
            "loader": loader,
            "icon": icon,
        })
    return {"query": query or "", "total": doc.get("total_hits", len(out)),
            "offset": max(0, int(offset or 0)), "limit": limit, "hits": out}


def _pick_version(project_id, version_id=None):
    """The pack version to import: a named one, or the newest release.

    Modrinth returns versions newest-first, so the first release in the list
    is the one the Modrinth app would preselect.
    """
    views = _get("/project/%s/version" % _quote(project_id))
    if not isinstance(views, list) or not views:
        raise ValueError("this pack has no published versions")
    if version_id:
        for view in views:
            if view.get("id") == version_id:
                return view
        raise ValueError("that pack version no longer exists")
    releases = [v for v in views if v.get("version_type") == "release"]
    if not releases:
        raise ValueError("this pack has no release version")
    return releases[0]


def _pick_file(version):
    files = version.get("files") or []
    primary = [f for f in files if f.get("primary")]
    chosen = (primary or files or [None])[0]
    if not chosen or not chosen.get("url"):
        raise ValueError("this version has no downloadable file")
    if not str(chosen["url"]).startswith(CDN):
        raise ValueError("refusing a pack file that is not from Modrinth's CDN")
    if not str(chosen.get("filename") or "").lower().endswith(".mrpack"):
        raise ValueError("this version's file is not a .mrpack")
    return chosen


# --- safe paths ------------------------------------------------------------
def _safe_relative(name):
    """A relative path from a remote document, or None if it could escape.

    A zip entry or an index `path` may name any absolute location; a modpack
    import must only ever write inside its own instance folder.
    """
    if not isinstance(name, str) or not name:
        return None
    name = name.replace("\\", "/")
    if name.startswith("/") or ":" in name:
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def _inside(root, relative):
    return os.path.normpath(os.path.join(root, *relative.split("/")))


def _write_atomic(target, stream):
    os.makedirs(os.path.dirname(target), exist_ok=True)
    temp = target + ".part"
    with open(temp, "wb") as fh:
        while True:
            block = stream.read(BLOCK)
            if not block:
                break
            fh.write(block)
    os.replace(temp, target)


def _hash_file(path, algorithms=("sha1", "sha512")):
    hashers = {name: hashlib.new(name) for name in algorithms}
    with open(path, "rb") as fh:
        while True:
            block = fh.read(BLOCK)
            if not block:
                break
            for hasher in hashers.values():
                hasher.update(block)
    return {name: hasher.hexdigest() for name, hasher in hashers.items()}


# --- loader profiles -------------------------------------------------------
def flatten_loader_profile(child, parent):
    """Merge a loader's profile document with the vanilla version it inherits.

    Fabric and Quilt publish `{"inheritsFrom": "<mc>", libraries, mainClass}`:
    by itself it has no downloads, no asset index and no Java requirement, so
    neither the installer nor the launcher could use it. The merge is the same
    one every launcher performs: the child supplies identity, main class and
    its libraries first on the classpath; the parent supplies everything the
    client itself is made of. The result is a self-contained version document,
    so the harness needs no inheritance support anywhere else.
    """
    merged = dict(parent)
    merged.pop("inheritsFrom", None)
    for key in ("id", "mainClass", "type", "releaseTime", "time", "minecraftArguments"):
        if key in child:
            merged[key] = child[key]
    child_args = child.get("arguments") or {}
    if child_args or parent.get("arguments"):
        args = {}
        for section in ("game", "jvm"):
            base = list((parent.get("arguments") or {}).get(section) or [])
            extra = list(child_args.get(section) or [])
            if base or extra:
                args[section] = base + extra
        for key, value in child_args.items():
            if key not in ("game", "jvm"):
                args[key] = value
        merged["arguments"] = args
    seen = set()
    libraries = []
    for lib in list(child.get("libraries") or []) + list(parent.get("libraries") or []):
        name = lib.get("name")
        if name in seen:
            continue
        seen.add(name)
        libraries.append(lib)
    merged["libraries"] = libraries
    return merged


def _loader_document(loader, minecraft, loader_version, base_version):
    """The flattened version document a pack's loader needs, or an error.

    `base_version(minecraft)` is supplied by the server: it owns the manifest
    and the local version store, and returns (doc, error) for the vanilla
    version the loader builds on.
    """
    template = LOADER_META.get(loader)
    if not template:
        if loader in KNOWN_LOADERS:
            raise ValueError(
                "%s packs are not supported yet: this launcher can only install "
                "Fabric and Quilt packs." % loader.replace("-loader", "").title()
            )
        raise ValueError("this pack does not name a loader this launcher knows")
    try:
        child = _get_url(template % (_quote(minecraft), _quote(loader_version)), timeout=30)
    except Exception as exc:
        raise ValueError("could not reach the %s meta service: %s" % (loader, exc))
    parent, error = base_version(minecraft)
    if error or not isinstance(parent, dict):
        raise ValueError(error or ("Minecraft %s could not be resolved" % minecraft))
    return flatten_loader_profile(child, parent)


# --- import ----------------------------------------------------------------
MAX_PACK_ARCHIVE_BYTES = 2 * 1024 ** 3
MAX_PACK_INDEX_BYTES = 8 * 1024 ** 2
MAX_PACK_ENTRIES = 50000
MAX_PACK_UNPACKED_BYTES = 8 * 1024 ** 3


def _local_pack_path(path):
    from instance_content import canonical_root
    if not isinstance(path, (str, os.PathLike)) or not str(path).lower().endswith('.mrpack'):
        raise ValueError('Choose a .mrpack file exported from a Minecraft launcher.')
    target = str(canonical_root(path))
    if not os.path.isfile(target):
        raise ValueError('The selected modpack file is no longer available.')
    if os.path.getsize(target) > MAX_PACK_ARCHIVE_BYTES:
        raise ValueError('This modpack archive exceeds the supported 2 GiB limit.')
    return target


def _copy_pack(target, stream):
    total = 0
    with open(target, 'wb') as file:
        while True:
            block = stream.read(BLOCK)
            if not block:
                break
            total += len(block)
            if total > MAX_PACK_ARCHIVE_BYTES:
                raise ValueError('This modpack archive exceeds the supported 2 GiB limit.')
            file.write(block)


def _pack_relative(path):
    relative = _safe_relative(path)
    if not relative:
        raise ValueError('The pack contains a path outside its installation: %r' % path)
    for part in relative.split('/'):
        if (re.search(r'[<>:"|?*\x00-\x1f]', part) or part.endswith((' ', '.')) or
                re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I)):
            raise ValueError('The pack contains an unsupported filename: %r' % path)
    return relative


def _read_pack_index(archive):
    infos = archive.infolist()
    if len(infos) > MAX_PACK_ENTRIES or sum(info.file_size for info in infos) > MAX_PACK_UNPACKED_BYTES:
        raise ValueError('The modpack contains too many or oversized unpacked files.')
    indexes = [info for info in infos if info.filename == 'modrinth.index.json']
    if len(indexes) != 1:
        raise ValueError('Choose a valid .mrpack with one modrinth.index.json.')
    if indexes[0].file_size > MAX_PACK_INDEX_BYTES:
        raise ValueError('The modpack index is too large to read safely.')
    seen = set()
    for info in infos:
        # ZIP links must never become live game-volume content.
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError('The modpack contains a linked archive entry.')
        for prefix in ('overrides/', 'client-overrides/'):
            if info.filename.startswith(prefix) and not info.is_dir():
                relative = _pack_relative(info.filename[len(prefix):])
                key = (prefix, relative.casefold())
                if key in seen:
                    raise ValueError('The pack contains duplicate override paths: %s' % relative)
                seen.add(key)
    try:
        index = json.loads(archive.read(indexes[0]).decode('utf-8-sig'))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('The modpack index is not readable JSON: %s' % exc) from exc
    if not isinstance(index, dict) or type(index.get('formatVersion')) is not int or index['formatVersion'] != 1:
        raise ValueError('Unsupported modpack format version.')
    if index.get('game') != 'minecraft':
        raise ValueError('This modpack is not for Minecraft.')
    dependencies = index.get('dependencies')
    if not isinstance(dependencies, dict) or not dependencies.get('minecraft'):
        raise ValueError('The pack does not name a Minecraft version.')
    _require_token(dependencies['minecraft'], 'Minecraft version')
    loaders = [key for key in KNOWN_LOADERS if dependencies.get(key)]
    if len(loaders) > 1:
        raise ValueError('The pack names more than one mod loader.')
    for loader in loaders:
        _require_token(dependencies[loader], 'mod loader version')
    files = index.get('files', [])
    if not isinstance(files, list) or len(files) > MAX_PACK_ENTRIES:
        raise ValueError('The pack file list is invalid or too large.')
    paths = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError('The pack contains an invalid file record.')
        relative = _pack_relative(entry.get('path'))
        if relative.casefold() in paths:
            raise ValueError('The pack contains duplicate indexed paths: %s' % relative)
        paths.add(relative.casefold())
        hashes = entry.get('hashes')
        if not isinstance(hashes, dict) or not any(re.fullmatch(r'[a-fA-F0-9]{%d}' % size, str(hashes.get(key) or '')) for key, size in (('sha1', 40), ('sha512', 128))):
            raise ValueError('A pack file is missing its required checksum: %s' % relative)
        for key, size in (('sha1', 40), ('sha512', 128)):
            if key in hashes and not re.fullmatch(r'[a-fA-F0-9]{%d}' % size, str(hashes[key])):
                raise ValueError('A pack file checksum is invalid: %s' % relative)
        if not isinstance(entry.get('env', {}), dict):
            raise ValueError('A pack file has invalid environment metadata: %s' % relative)
        if (entry.get('env') or {}).get('client') == 'disabled':
            continue
        downloads = entry.get('downloads') or []
        url = entry.get('url') or (downloads[0] if isinstance(downloads, list) and downloads else None)
        if not isinstance(url, str) or not url.startswith(CDN):
            raise ValueError('A pack file has no supported Modrinth download: %s' % relative)
    return index


def _fetch_pack_file(root, entry):
    """One `files[]` entry of the pack index, verified, into the instance."""
    relative = _safe_relative(entry.get("path"))
    if not relative:
        raise ValueError("the pack wants to write outside its folder: %r" % (entry.get("path"),))
    url = entry.get("url") or (entry.get("downloads") or [None])[0]
    if not isinstance(url, str) or not url.startswith(CDN):
        raise ValueError("refusing a download that is not from Modrinth's CDN: %r" % url)
    target = _inside(root, relative)
    from instance_content import canonical_root
    canonical_root(target)
    canonical_root(target + ".part")
    expected = entry.get("hashes") or {}
    download_control.download(url, target, size=entry.get("fileSize"),
        hashes={key: expected[key] for key in ("sha1", "sha512") if key in expected},
        timeout=120, progress=lambda got: _set(currentBytes=got),
        opener=lambda request, timeout: urllib.request.urlopen(request, timeout=timeout, context=_context()))
    return os.path.getsize(target)


def _import_job(game_dir, state_dir, project_id, version_id, base_version, on_done, archive_path=None, job_control=None):
    with download_control.bind(job_control):
        return _import_owned(game_dir, state_dir, project_id, version_id, base_version, on_done, archive_path)


def _import_owned(game_dir, state_dir, project_id, version_id, base_version, on_done, archive_path=None):
    instance = None
    owned = download_control.current()
    try:
        if owned:
            owned.checkpoint()
        from instance_content import canonical_root
        game_dir = str(canonical_root(game_dir))
        if archive_path is not None:
            archive_path = _local_pack_path(archive_path)
            title = os.path.splitext(os.path.basename(archive_path))[0]
            version, chosen = {}, None
            _set(phase='reading', message='Reading the selected modpack')
        else:
            _set(phase="resolving", message="Looking up the pack")
            with (owned.noninterruptible("Resolving modpack metadata") if owned else download_control.bind(None)):
                project = _get("/project/%s" % _quote(project_id))
                title = str(project.get("title") or project.get("slug") or "Modpack").strip() or "Modpack"
                version = _pick_version(project_id, version_id)
                chosen = _pick_file(version)
            _set(phase="downloading", message="Downloading %s" % chosen["filename"])
        instances_root = os.path.join(game_dir, "instances")
        canonical_root(instances_root)
        os.makedirs(instances_root, exist_ok=True)
        temp_dir = tempfile.mkdtemp(prefix=".pack-", dir=instances_root)
        mrpack = os.path.join(temp_dir, "pack.mrpack")
        if archive_path is not None:
            with (owned.noninterruptible("Reading the local pack archive") if owned else download_control.bind(None)):
                with open(archive_path, 'rb') as source:
                    _copy_pack(mrpack, source)
        else:
            archive_hashes = chosen.get("hashes") or {}
            digest = archive_hashes.get("sha512") or archive_hashes.get("sha1")
            cache_archive = mrpack
            if isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{128}", digest):
                cache_archive = os.path.join(game_dir, ".launcher-downloads", "modpack-archives", digest.lower() + ".mrpack")
                canonical_root(cache_archive)
                canonical_root(cache_archive + ".part")
            download_control.download(chosen["url"], cache_archive, size=chosen.get("size"),
                hashes=chosen.get("hashes") or {}, timeout=300, max_bytes=MAX_PACK_ARCHIVE_BYTES,
                progress=lambda got: _set(currentBytes=got, bytesTotal=chosen.get("size") or 0),
                opener=lambda request, timeout: urllib.request.urlopen(request, timeout=timeout, context=_context()))
            if cache_archive != mrpack:
                with (owned.noninterruptible("Staging the verified modpack archive") if owned else download_control.bind(None)):
                    shutil.copyfile(cache_archive, mrpack)
        expected = chosen.get("hashes") or {} if chosen else {}
        actual = _hash_file(mrpack, tuple(k for k in ("sha1", "sha512") if k in expected))
        for algorithm, digest in actual.items():
            if str(expected[algorithm]).lower() != digest:
                raise ValueError("the pack archive failed its %s check" % algorithm)
        pack_size = os.path.getsize(mrpack)

        _set(phase="reading", message="Reading the pack index")
        with zipfile.ZipFile(mrpack) as archive:
            index = _read_pack_index(archive)
            if archive_path is not None:
                title = str(index.get('name') or title).strip()[:200] or 'Modpack'
                version = {'version_number': str(index.get('versionId') or 'Local pack')}
            dependencies = index.get("dependencies") or {}
            minecraft = str(dependencies.get("minecraft") or "")
            if not minecraft:
                raise ValueError("the pack does not name a Minecraft version")
            loader = next((k for k in KNOWN_LOADERS if dependencies.get(k)), None)

            instance = _instance_dir(game_dir, title)
            canonical_root(instance)
            os.makedirs(instance, exist_ok=True)

            if loader in LOADER_META:
                _set(phase="loader", message="Installing the %s profile" % _loader_name(loader))
                with (owned.noninterruptible("Materializing the loader profile") if owned else download_control.bind(None)):
                    doc = _loader_document(loader, minecraft, str(dependencies[loader]), base_version)
                    _store_version_doc(state_dir, doc)
                last_version_id = str(doc.get("id") or minecraft)
            elif loader:
                # Forge/NeoForge: the version document comes out of the loader's
                # own official installer, run against a staging folder and
                # merged into the version store (install_forge_like).
                _set(phase="loader", message="Running the official %s installer" % _loader_name(loader))
                last_version_id = install_forge_like(
                    game_dir, state_dir, loader, minecraft, str(dependencies[loader]),
                    base_version=base_version,
                )
            else:
                last_version_id = minecraft

            files = [f for f in (index.get("files") or [])
                     if isinstance(f, dict) and (f.get("env") or {}).get("client") != "disabled"]
            _set(phase="files", message="Downloading the pack's files", done=0, total=len(files))
            counters = {"done": 0, "bytes": 0}

            def cached_root(entry):
                expected = entry.get("hashes") or {}
                digest = expected.get("sha512") or expected.get("sha1")
                root = os.path.join(game_dir, ".launcher-downloads", "modpack-artifacts", digest.lower())
                canonical_root(root)
                return root

            def fetch(entry):
                with download_control.bind(owned):
                    size = _fetch_pack_file(cached_root(entry), entry)
                    with _STATE_LOCK:
                        counters["done"] += 1
                        counters["bytes"] += size
                    _set(done=counters["done"])

            errors = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                futures = [pool.submit(fetch, entry) for entry in files]
                for future in concurrent.futures.as_completed(futures):
                    try:
                        future.result()
                    except Exception as error:
                        errors.append(error)
                        if owned:
                            owned.finish("error")
            if errors:
                raise next((error for error in errors if isinstance(error, download_control.DownloadCancelled)), errors[0])

            _set(phase="overrides", message="Applying the pack's overrides")
            with (owned.noninterruptible("Materializing verified modpack content") if owned else download_control.bind(None)):
                for entry in files:
                    relative = _safe_relative(entry["path"])
                    source, target = _inside(cached_root(entry), relative), _inside(instance, relative)
                    canonical_root(source)
                    expected = entry.get("hashes") or {}
                    if any(actual.lower() != expected[algorithm].lower()
                           for algorithm, actual in _hash_file(source, tuple(expected)).items()):
                        raise ValueError("The verified modpack cache changed before materialization.")
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.copyfile(source, target)
                _apply_overrides(archive, instance)

        os.remove(mrpack)
        os.rmdir(temp_dir)               # only succeeds when nothing else is left
        result = {
            "name": title,
            "instanceDir": instance,
            "lastVersionId": last_version_id,
            "minecraft": minecraft,
            "loader": loader,
            "loaderVersion": str(dependencies.get(loader)) if loader else None,
            "versionNumber": version.get("version_number"),
            # Count final installed paths, including bundled overrides. Common
            # and client overrides can replace an indexed file, so download or
            # archive-entry counts would misreport the completed installation.
            "files": sum(len(names) for _, _, names in os.walk(instance)),
            "bytes": counters["bytes"] + pack_size,
            "projectId": project_id,
            "versionId": version.get("id"),
            "source": 'local' if archive_path is not None else 'modrinth',
        }
        with (owned.noninterruptible("Saving the imported installation") if owned else download_control.bind(None)):
            if on_done:
                result = on_done(result) or result
            if owned:
                owned.finish("done")
        _set(message="Imported %s" % title)
        _finish(result=result)
    except Exception as exc:
        # A failed import leaves nothing behind: the instance folder was made
        # by this job, so it goes back to not existing.
        if instance:
            _forget_dir(instance)
        if os.path.isdir(locals().get("temp_dir") or ""):
            shutil.rmtree(temp_dir, ignore_errors=True)
        _finish(error=exc)


def _instance_dir(game_dir, title):
    base = re.sub(r"[^A-Za-z0-9 _-]", "", title).strip() or "Modpack"
    root = os.path.join(game_dir, "instances")
    candidate = base
    n = 2
    while os.path.exists(os.path.join(root, candidate)):
        candidate = "%s (%d)" % (base, n)
        n += 1
    return os.path.join(root, candidate)


def _forget_dir(path):
    """Remove a directory tree; only paths under `instances/` qualify."""
    root = os.path.normpath(os.path.join(path, ".."))
    if os.path.basename(root) != "instances":
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _apply_overrides(archive, instance):
    """Extract `overrides/` and `client-overrides/` into the instance folder.

    Zip members can name any path on disk; only relative members that stay
    inside the instance are written, and directories are created on the way.
    """
    # Client-specific content wins over common overrides regardless of ZIP
    # member order, and both are applied after indexed downloads finish.
    for prefix in ("overrides/", "client-overrides/"):
        for info in archive.infolist():
            name = info.filename
            if not name.startswith(prefix):
                continue
            relative = _safe_relative(name[len(prefix):])
            if not relative or name.endswith("/"):
                continue
            target = _inside(instance, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with archive.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


LOADER_LIST = {
    "fabric-loader": "https://meta.fabricmc.net/v2/versions/loader/%s",
    "quilt-loader": "https://meta.quiltmc.org/v3/versions/loader/%s",
}


def _get_text(url, timeout=30):
    with urllib.request.urlopen(
        _request(url, timeout), timeout=timeout, context=_context()
    ) as response:
        return response.read().decode("utf-8", "replace")


def _loader_listing(loader, minecraft):
    """The raw loader list a meta service publishes for one Minecraft version.

    Both Fabric and Quilt answer with a list whose entries carry a `loader`
    object holding `{version, stable}`; Fabric marks stable builds outright
    while Quilt interleaves them with previews. Forge and NeoForge have no
    such list, so they are answered in the same shape from their official
    sources: Forge from the per-Minecraft promotions file (latest and
    recommended), NeoForge from its global Maven list filtered to the
    Minecraft line. Returns an error-shaped ValueError when the loader is
    unknown or the line cannot be resolved safely, so callers share one
    validation path.
    """
    if loader == "forge":
        try:
            promotions = _get_url(FORGE_PROMOTIONS, timeout=30)
        except Exception as exc:
            raise ValueError("could not reach Forge's promotion list: %s" % exc)
        promos = promotions.get("promos") or {}
        latest = str(promos.get(minecraft + "-latest") or "") or None
        recommended = str(promos.get(minecraft + "-recommended") or "") or None
        if not latest and not recommended:
            raise ValueError("Forge lists no build for Minecraft %s" % minecraft)
        # Mark the recommended build stable so the picker preselects it.
        entries = []
        if latest:
            entries.append({"loader": {"version": latest, "stable": latest == recommended}})
        if recommended and recommended != latest:
            entries.append({"loader": {"version": recommended, "stable": True}})
        return entries
    if loader == "neoforge":
        try:
            xml = _get_text(NEOFORGE_METADATA, timeout=30)
            versions = [
                (node.text or "").strip()
                for node in ET.fromstring(xml).iter("version")
            ]
        except Exception as exc:
            raise ValueError("could not reach NeoForge's Maven list: %s" % exc)
        picked = neoforge_versions_for(minecraft, versions)
        if not picked:
            if re.match(r"^1\.(\d+)(\.(\d+))?$", str(minecraft or "")):
                raise ValueError(
                    "NeoForge builds for Minecraft %s cannot be listed safely; "
                    "a modpack that names one still imports" % minecraft
                )
            raise ValueError("NeoForge lists no build for Minecraft %s" % minecraft)
        return [{"loader": {"version": v, "stable": i == 0}}
                for i, v in enumerate(picked)]
    template = LOADER_LIST.get(loader)
    if not template:
        raise ValueError("this launcher does not know that loader")
    try:
        return _get_url(template % _quote(minecraft), timeout=30)
    except Exception as exc:
        raise ValueError("could not reach the %s meta service: %s" % (loader, exc))


# How many loader versions the picker offers. Both lists run long (Quilt keeps
# years of beta builds); a picker is a short menu, not an archive browser, so
# it shows the newest window and nothing older than that.
LOADER_CHOICES_MAX = 20


def loader_versions(loader, minecraft):
    """The loader versions a picker offers, newest first, and the recommended one.

    Returns `([version], recommended)`. Both services list newest-first and mark
    a build `stable` when it is one. Neither list can be the picker alone: Fabric
    flags a single stable build for most versions (0.20.1 above 254 previews for
    one recent one) and Quilt flags none at all, so a stable-only filter would
    collapse the picker to one row or empty it. The picker therefore shows the
    newest window of everything the service published, and the recommended pick
    is the newest stable build in that window (the newest entry when the service
    has flagged none).
    """
    listing = _loader_listing(loader, minecraft)
    published = []
    stable = []
    for entry in listing or []:
        info = entry.get("loader") or {}
        version = str(info.get("version") or "")
        if not version:
            continue
        published.append(version)
        if info.get("stable"):
            stable.append(version)
    offered = (published or stable)[:LOADER_CHOICES_MAX]
    if not offered:
        raise ValueError(
            "the %s meta service lists no loader for Minecraft %s" % (loader, minecraft)
        )
    offered_set = set(offered)
    recommended = next((v for v in stable if v in offered_set), None) or offered[0]
    return offered, recommended


def latest_loader_version(loader, minecraft):
    """The newest stable loader version for (loader, minecraft), or an error."""
    return loader_versions(loader, minecraft)[1]


def materialize_loader(state_dir, minecraft, loader, base_version, version=None,
                       game_dir=None):
    """Fetch, flatten and store the loader's version document; returns its id.

    This is the same document a modpack import writes for its pack, produced
    from the meta services instead of a pack's dependency list: the version id
    it returns is what a profile's lastVersionId points at so the installer
    can find it on disk. An explicit `version` (a picker choice) is used as-is;
    left empty it falls back to the newest stable build. Forge and NeoForge
    take their installer path instead (no profile document is published).
    """
    version = str(version or "").strip() or latest_loader_version(loader, minecraft)
    if loader in ("forge", "neoforge"):
        return install_forge_like(game_dir or state_dir, state_dir, loader,
                                  minecraft, version, base_version=base_version)
    doc = _loader_document(loader, minecraft, version, base_version)
    _store_version_doc(state_dir, doc)
    return str(doc.get("id") or minecraft)


def _store_version_doc(state_dir, doc):
    ident = _require_token(doc.get("id"), "loader profile version id")
    folder = os.path.join(state_dir, "versions", ident)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, ident + ".json")
    temp = path + ".part"
    with open(temp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    os.replace(temp, path)


def start_import(game_dir, state_dir, project_id, version_id=None,
                 base_version=None, on_done=None):
    """Kick off an import job; False when one is already running."""
    if not project_id:
        raise ValueError("no project id")
    if not _begin("import"):
        return False
    threading.Thread(
        target=_import_job,
        args=(game_dir, state_dir, str(project_id), version_id, base_version, on_done),
        kwargs={"job_control": _CONTROL},
        daemon=True,
        name="modpack-import",
    ).start()
    return True


def start_import_file(game_dir, state_dir, path, base_version=None, on_done=None):
    """Import a host-selected local archive through the same verified pipeline."""
    selected = _local_pack_path(path)
    if not _begin('import'):
        return False
    threading.Thread(target=_import_job,
                     args=(game_dir, state_dir, None, None, base_version, on_done),
                     kwargs={'archive_path': selected, 'job_control': _CONTROL}, daemon=True,
                     name='modpack-local-import').start()
    return True


# --- export ----------------------------------------------------------------
FOLDERS = ("mods", "resourcepacks", "shaderpacks", "config")
RESOLVE_BATCH = 250


def _collect_files(instance):
    """Pack content and configuration, including unpacked directory packs."""
    from instance_content import canonical_root, _reparse
    instance = str(canonical_root(instance))
    found = []
    for folder in FOLDERS:
        root = os.path.join(instance, folder)
        if not os.path.isdir(root) or _reparse(root):
            continue
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not _reparse(os.path.join(current, d)))
            for name in sorted(files):
                path = os.path.join(current, name)
                if _reparse(path) or not os.path.isfile(path) or name.endswith(".part"):
                    continue
                relative = os.path.relpath(path, root).replace(os.sep, "/")
                found.append((folder, relative, path))
    return found


def _resolve_hashes(hashes):
    """Modrinth's version lookup for local file hashes: hash -> version summary."""
    resolved = {}
    for start in range(0, len(hashes), RESOLVE_BATCH):
        chunk = hashes[start:start + RESOLVE_BATCH]
        request = urllib.request.Request(
            API + "/version_files",
            data=json.dumps({"hashes": chunk, "algorithm": "sha1"}).encode("utf-8"),
            headers={"User-Agent": AGENT, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60, context=_context()) as response:
            doc = json.load(response)
        if isinstance(doc, dict):
            resolved.update({k: v for k, v in doc.items() if isinstance(v, dict)})
    return resolved


def _cdn_file(summary, sha1):
    """The Modrinth CDN download of a resolved version, if it has one.

    `version_files` answers with the whole version object, files included, so
    the export needs no second request per mod. A version can contain several
    artifacts, so only the file with the local checksum supplies its URL.
    """
    if not isinstance(summary, dict):
        return None
    files = summary.get("files") or []
    chosen = next((f for f in files if isinstance(f, dict) and
                   str((f.get("hashes") or {}).get("sha1") or "").lower() == sha1), None)
    url = (chosen or {}).get("url")
    if not isinstance(url, str) or not url.startswith(CDN):
        return None
    return {"url": url, "filename": chosen.get("filename"), "size": chosen.get("size")}


def _export_name(name):
    """One Windows-safe filename component; the pack's display name is kept."""
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", name).strip(" .")[:120].rstrip(" .")
    clean = clean or "installation"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", clean, re.IGNORECASE):
        clean = "_" + clean
    return clean


def export(game_dir, profile, out_dir, dependencies=None):
    """Write one installation out as a `.mrpack`.

    Jars Modrinth knows become index entries that reference the CDN (so the
    imported pack downloads the same files); anything else -- custom mods,
    resource packs, shader packs, configuration -- is bundled into `overrides/`,
    which is what makes an export round-trip through any Modrinth-format
    importer. Saves, logs, screenshots and player options are not pack content
    and are left out.
    """
    instance = str((profile or {}).get("gameDir") or game_dir)
    name = str((profile or {}).get("name") or "installation").strip() or "installation"
    entries = _collect_files(instance)
    if not entries:
        raise ValueError("this installation has no mods, resource packs, shader packs or configuration")
    _set(phase="hashing", message="Hashing the pack's files", done=0, total=len(entries))
    local = []
    for folder, filename, path in entries:
        # Nested files form unpacked packs or configuration and stay bundled.
        hashes = _hash_file(path) if folder != "config" and "/" not in filename else {}
        local.append({"folder": folder, "filename": filename, "path": path,
                      "sha1": hashes.get("sha1"), "sha512": hashes.get("sha512"),
                      "size": os.path.getsize(path)})
        with _STATE_LOCK:
            _STATE["done"] += 1
    _set(phase="resolving", message="Asking Modrinth about the files", done=0, total=0)
    warnings = []
    try:
        summaries = _resolve_hashes([entry["sha1"] for entry in local if entry["sha1"]])
    except (OSError, ValueError, urllib.error.URLError):
        summaries = {}
        warnings.append('Modrinth file lookup was unavailable. All pack files were bundled locally so the export remains complete.')

    index_files = []
    overrides = []
    for entry in local:
        info = _cdn_file(summaries.get(entry["sha1"]), entry["sha1"]) if entry["sha1"] else None
        if info and info.get("url"):
            index_files.append({
                "path": "%s/%s" % (entry["folder"], entry["filename"]),
                "hashes": {"sha1": entry["sha1"], "sha512": entry["sha512"]},
                "env": {"client": "required", "server": "unsupported"},
                "downloads": [info["url"]],
                "fileSize": entry["size"],
            })
        else:
            overrides.append(entry)

    stamp = datetime.datetime.now(datetime.timezone.utc)
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "installation"
    index = {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": "%s-%s" % (slug, stamp.strftime("%Y%m%d")),
        "name": name,
        "files": index_files,
        "dependencies": dependencies or {},
    }

    _set(phase="writing", message="Writing the pack file",
         done=0, total=len(overrides) + 1)
    basename = _export_name(name)
    target = os.path.join(out_dir, "%s.mrpack" % basename)
    n = 2
    while os.path.exists(target):
        target = os.path.join(out_dir, "%s (%d).mrpack" % (basename, n))
        n += 1
    fd, temp = tempfile.mkstemp(prefix=".export-", suffix=".part", dir=out_dir)
    os.close(fd)
    try:
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("modrinth.index.json", json.dumps(index, indent=2))
            with _STATE_LOCK:
                _STATE["done"] += 1
            for entry in overrides:
                archive.write(entry["path"], "overrides/%s/%s" % (entry["folder"], entry["filename"]))
                with _STATE_LOCK:
                    _STATE["done"] += 1
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.remove(temp)
    result = {
        "path": target,
        "size": os.path.getsize(target),
        "files": len(index_files),
        "bundled": len(overrides),
        "warnings": warnings,
    }
    _set(message="Saved %s" % os.path.basename(target))
    _finish(result=result)
    return result


def start_export(game_dir, profile, out_dir, dependencies=None, on_done=None):
    """Kick off an export job; False when one is already running."""
    if not _begin("export"):
        return False
    threading.Thread(
        target=_export_job,
        args=(game_dir, profile, out_dir, dependencies, on_done),
        kwargs={"job_control": _CONTROL},
        daemon=True,
        name="modpack-export",
    ).start()
    return True


def _export_job(game_dir, profile, out_dir, dependencies, on_done, job_control=None):
    with download_control.bind(job_control):
        try:
            result = export(game_dir, profile, out_dir, dependencies)
            if on_done:
                result = on_done(result) or result
            _finish(result=result)
        except Exception as exc:
            _finish(error=str(exc))
