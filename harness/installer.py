"""Real game downloader for the harness.

The captured launcher state has version metadata only, so the install dialog
honestly failed with the app's own error path. This module changes the deal:
when the user clicks Install, the machine fetches exactly what the native
launcher would have fetched -- the client jar, the windows libraries, the
asset index and its objects, and the Java runtime the version declares --
into the game directory, verifying every file against the manifest's sha1.

Readiness checks the selected version's client, libraries and Java runtime
on disk, so the launcher UI transitions from real filesystem evidence.

State (module-level, guarded by one lock):
    state() -> snapshot dict with phase, progress and error for /__install_status.
"""
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile

import modpacks
import download_control

_LOCK = threading.Lock()
_RUNTIME_LOCK = threading.Lock()
_CONTROL = None
_STATE = {
    "active": False,
    "phase": "idle",
    "todo": [],
    "error": None,
    "finished_at": None,
    "result": None,          # per-version {"phase": name: {"done": size, "total": size}}
    "_detail": {},
}


def state():
    with _LOCK:
        snapshot = {
            "active": _STATE["active"],
            "phase": _STATE["phase"],
            "error": _STATE["error"],
            "finished_at": _STATE["finished_at"],
            "result": dict(_STATE["result"] or {}),
            "java_error": _STATE.get("java_error"),
        }
        todo = list(_STATE["todo"])
        detail = dict(_STATE.get("_detail") or {})
        control = _CONTROL if _CONTROL and _STATE.get("jobId") == _CONTROL.id else None
    # One counter per destination, grouped under its declared phase. URLs may
    # be shared by distinct runtime files and are not phase identifiers.
    phases = {}
    seen = set()
    for item in todo:
        key = _item_key(item)
        if key in seen:
            continue
        seen.add(key)
        phases.setdefault(item["phase"], {"done": 0, "total": 0})
        done = detail.get(key, 0)
        phases[item["phase"]]["total"] += max(item.get("size") or 0, done)
        phases[item["phase"]]["done"] += done
    for phase, entry in (snapshot["result"] or {}).items():
        phases.setdefault(phase, {"done": 0, "total": 0})
        phases[phase]["done"] = phases[phase]["total"] = entry
    total = sum((p["total"] for p in phases.values()), 0)
    done = sum((p["done"] for p in phases.values()), 0)
    return {
        "active": snapshot["active"],
        "phase": snapshot["phase"],
        "error": snapshot["error"],
        "finished_at": snapshot["finished_at"],
        "isInstalled": _client_jar_present(),
        "java_error": snapshot["java_error"],
        "phases": phases,
        "bytesDone": done,
        "bytesTotal": total,
        **(control.snapshot() if control else {"jobId": None, "controlStatus": "idle",
            "paused": False, "cancelRequested": False, "canPause": False,
            "canResume": False, "canCancel": False, "controlReason": "No active download."}),
    }


def control(job_id, action):
    """Control exactly the currently owned installer job, never a replacement."""
    with _LOCK:
        owned = _CONTROL
        if not owned or owned.id != job_id or _STATE.get("jobId") != job_id:
            raise download_control.ControlError("missing_job", "This download job is no longer current.")
    owned.request(action)
    return state()


def _progress(item, got):
    owned = download_control.current()
    with _LOCK:
        if owned and _STATE.get("jobId") != owned.id:
            return
        _STATE["_detail"][_item_key(item)] = got


def _update(**values):
    owned = download_control.current()
    with _LOCK:
        if not owned or _STATE.get("jobId") == owned.id:
            _STATE.update(values)


def _client_jar_present():
    game_dir = DIR_HINTS.get("game_dir")
    state_dir = DIR_HINTS.get("state")
    if not game_dir or not state_dir:
        return False
    version_id = DIR_HINTS.get("version_id")
    run_dir = DIR_HINTS.get("run_dir")
    if version_id:
        return is_installed(game_dir, state_dir, version_id, run_dir=run_dir)
    candidates = set()
    for root in _metadata_roots(state_dir, [game_dir, run_dir]):
        try:
            candidates.update(os.listdir(os.path.join(root, "versions")))
        except OSError:
            continue
    return any(is_installed(game_dir, state_dir, ident, run_dir=run_dir) for ident in candidates)


def _safe_version_id(version_id):
    return (isinstance(version_id, str) and 0 < len(version_id) <= 128
            and "\x00" not in version_id and "/" not in version_id and "\\" not in version_id
            and modpacks._safe_relative(version_id) == version_id)


def _metadata_roots(state_dir, version_roots=None):
    seen = set()
    for root in [state_dir] + list(version_roots or []):
        if not root:
            continue
        key = os.path.normcase(os.path.abspath(root))
        if key not in seen:
            seen.add(key)
            yield root


def _version_metadata_path(state_dir, version_id, version_roots=None):
    """Find metadata in STATE, shared installation and explicit instance order."""
    if not _safe_version_id(version_id):
        return None
    for root in _metadata_roots(state_dir, version_roots):
        boundary = os.path.realpath(root)
        path = os.path.realpath(os.path.join(root, "versions", version_id, version_id + ".json"))
        try:
            contained = os.path.commonpath([boundary, path]) == boundary
        except ValueError:
            contained = False
        if contained and os.path.isfile(path):
            return path
    return None


def is_installed(game_dir, state_dir, version_id, run_dir=None):
    """Check boot prerequisites for a specific version without downloading files."""
    if version_id in ("latest", "latest-release", "latest-snapshot"):
        try:
            with open(os.path.join(state_dir, "versions", "version_manifest_v2.json"), encoding="utf-8-sig") as fh:
                latest = json.load(fh).get("latest") or {}
            version_id = latest.get("snapshot" if version_id == "latest-snapshot" else "release")
        except (OSError, ValueError):
            return False
    if not _safe_version_id(version_id):
        return False
    roots = [game_dir, run_dir]
    path = _version_metadata_path(state_dir, version_id, roots)
    if not path:
        return False
    try:
        with open(path, encoding="utf-8-sig") as fh:
            version = _resolved_version(json.load(fh), path, state_dir=state_dir, version_roots=roots)
        jar_id = str(version.get("jar") or version_id)
        if not _safe_version_id(jar_id):
            return False
        if not _file_present(os.path.join(game_dir, "versions", jar_id, jar_id + ".jar")):
            return False
        for item in _framework(game_dir, version, state_dir):
            if not _file_present(item["dest"], item.get("size")):
                return False
        component = (version.get("javaVersion") or {}).get("component") or "jre-legacy"
        return bool(find_java(game_dir, version, component))
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return False


def _file_present(path, size=None, allow_empty=False):
    try:
        actual = os.path.getsize(path)
        size_matches = (size is None or actual == size) if allow_empty else (not size or actual == size)
        return os.path.isfile(path) and (actual > 0 or allow_empty) and size_matches
    except OSError:
        return False


def _runtime_java(game_dir, component):
    root = os.path.join(game_dir, "runtime", component)
    candidates = [os.path.join(root, "bin", name) for name in ("java.exe", "javaw.exe", "java")]
    executable = next((path for path in candidates if _file_present(path)), None)
    if not executable:
        return None
    manifest_path = os.path.join(root, "manifest.json")
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            files = json.load(fh).get("files") or {}
        executable_rel = os.path.relpath(executable, root).replace("\\", "/")
        if not isinstance(files, dict) or (files.get(executable_rel) or {}).get("type") != "file":
            return None
        for path, meta in files.items():
            if meta.get("type") != "file":
                continue
            rel = modpacks._safe_relative(path)
            if not rel:
                return None
            raw = (meta.get("downloads") or {}).get("raw") or {}
            if not _file_present(os.path.join(root, *rel.split("/")),
                                 meta.get("size", raw.get("size")), allow_empty=True):
                return None
    except (OSError, ValueError, TypeError):
        return None
    return executable


# server.py fills these; kept here so installer.py has no import from it.
DIR_HINTS = {"game_dir": None, "state": None, "version_id": None, "run_dir": None}


def start(game_dir, state_dir, version_json_path, java_component=None, run_dir=None):
    """Kick off (or report if already running). Returns the status snapshot."""
    global _CONTROL
    game_dir = os.path.abspath(game_dir)
    state_dir = os.path.abspath(state_dir)
    with _LOCK:
        already_active = _STATE["active"]
        if not already_active:
            _CONTROL = download_control.JobControl()
            owned = _CONTROL
            DIR_HINTS.update(game_dir=game_dir, state=state_dir,
                             version_id=os.path.basename(version_json_path).removesuffix(".json"),
                             run_dir=run_dir)
            _STATE.update(active=True, phase="calculating", error=None,
                          todo=[], result=None, _detail={}, finished_at=None, java_error=None,
                          jobId=owned.id)
    if already_active:
        return state()
    worker = threading.Thread(
        target=_run,
        args=(game_dir, version_json_path, java_component, state_dir, run_dir),
        kwargs={"job_control": owned},
        name="mc-installer", daemon=True,
    )
    worker.start()
    return state()


def _run(game_dir, version_json_path, java_component, state_dir=None, run_dir=None, job_control=None):
    with download_control.bind(job_control):
        _run_owned(game_dir, version_json_path, java_component, state_dir, run_dir)


def _run_owned(game_dir, version_json_path, java_component, state_dir=None, run_dir=None):
    owned = download_control.current()
    try:
        if owned:
            owned.checkpoint()
        state_dir = state_dir or os.path.dirname(os.path.dirname(os.path.dirname(version_json_path)))
        with open(version_json_path, encoding="utf-8-sig") as fh:
            version = _resolved_version(json.load(fh), version_json_path, state_dir=state_dir,
                                        version_roots=[game_dir, run_dir])
        java_component = java_component or (version.get("javaVersion") or {}).get("component") or "jre-legacy"
        jre = _read_jre_manifest(state_dir, version_json_path)
        framework = _framework(game_dir, version, state_dir)
        todo = framework  # list of {"phase","url","dest","size","sha1","kind"}
        with _RUNTIME_LOCK:
            if _runtime_entries(jre, java_component) or not find_java(game_dir, version, java_component):
                todo += _java_plan(game_dir, jre, java_component)
            _update(todo=todo, phase="downloading")
            _install_all(game_dir, todo, jre, java_component, version)
            if owned:
                owned.checkpoint()
            if not find_java(game_dir, version, java_component):
                raise RuntimeError("the required Java runtime is incomplete: " + java_component)
        with (owned.noninterruptible("Finalizing the installation") if owned else download_control.bind(None)):
            # state() reads this as {phase: bytes}; a flat phase aggregate keeps
            # the endpoint JSON-serializable once the install completes.
            result = {}
            seen = set()
            for item in todo:
                key = _item_key(item)
                if key in seen:
                    continue
                seen.add(key)
                size = os.path.getsize(item["dest"])
                result[item["phase"]] = result.get(item["phase"], 0) + size
            if owned:
                owned.finish("done")
            _update(result=result, active=False, phase="done", finished_at=time.time())
    except download_control.DownloadCancelled:
        if owned:
            owned.finish("cancelled")
        _update(active=False, phase="cancelled", error=None, finished_at=time.time())
    except Exception as exc:                      # keep the thread alive; report failure
        if owned:
            owned.finish("error")
        _update(active=False, phase="error", error=str(exc), finished_at=time.time())


def _explain_phases(todo):
    return sorted({item["phase"] for item in todo})


def _resolved_version(version, version_json_path, depth=0, state_dir=None, version_roots=None):
    """Flatten installer-generated loader metadata onto its vanilla version.

    Forge and NeoForge installers publish an `inheritsFrom` profile rather than
    duplicating Mojang's downloads, assets and Java requirement. Resolve that
    parent before planning downloads so installing a loader also fetches the
    vanilla client/runtime it actually needs.
    """
    parent_id = version.get("inheritsFrom")
    if not parent_id:
        return version
    if depth >= 16:
        raise RuntimeError("version inheritance is too deep for " + str(version.get("id") or "unknown"))
    if not _safe_version_id(parent_id):
        raise RuntimeError("invalid inherited Minecraft version: " + str(parent_id))
    state_dir = state_dir or os.path.dirname(os.path.dirname(os.path.dirname(version_json_path)))
    parent_path = _version_metadata_path(state_dir, parent_id, version_roots)
    if not parent_path:
        raise RuntimeError("base Minecraft version %s is missing or unreadable" % parent_id)
    try:
        with open(parent_path, encoding="utf-8-sig") as parent_file:
            parent = json.load(parent_file)
    except (OSError, ValueError) as exc:
        raise RuntimeError("base Minecraft version %s is missing or unreadable: %s" % (parent_id, exc))
    parent = _resolved_version(parent, parent_path, depth + 1, state_dir, version_roots)
    merged = modpacks.flatten_loader_profile(version, parent)
    merged["jar"] = str(parent.get("jar") or parent.get("id") or parent_id)
    return merged


def _framework(game_dir, version, state_dir=None):
    """Every byte that must land in game_dir for this version."""
    plan = []
    vid = version.get("id") or "unknown"
    # A loader profile inherits vanilla downloads and assets but keeps its own
    # version id. The client jar remains stored under the base version id.
    jar_id = str(version.get("jar") or vid)
    client = version.get("downloads", {}).get("client")
    if client:
        plan.append({
            "phase": "client", "url": client["url"],
            "dest": os.path.join(game_dir, "versions", jar_id, jar_id + ".jar"),
            "size": client.get("size", 0), "sha1": client.get("sha1"),
        })
    # 2. windows libraries.
    for lib in version.get("libraries", []):
        if not _library_allows_windows(lib):
            continue
        dl = lib.get("downloads", {})
        for key in ("natives-windows", "artifact"):
            entry = dl.get("classifiers", {}).get(key) if key == "natives-windows" else library_artifact(lib)
            if not entry:
                continue
            rel = entry.get("path") or key.replace("-", "/") + "/" + (entry.get("sha1") or "x") + "/" + (entry.get("url", "").rsplit("/", 1)[-1] or "x")
            item = {
                "phase": "libraries",
                "dest": os.path.join(game_dir, "libraries", *rel.split("/")),
                "size": entry.get("size", 0), "sha1": entry.get("sha1"),
            }
            if entry.get("url"):
                item["url"] = entry["url"]
            elif state_dir:
                # Forge/NeoForge installers sometimes generate artifacts into
                # their local Maven tree and record an empty URL. Copy those
                # artifacts from the version store into the active game dir.
                item["source"] = os.path.join(state_dir, "libraries", *rel.split("/"))
            else:
                continue
            plan.append(item)
    # 3. assets index + objects.
    index = version.get("assetIndex")
    assets_dir = os.path.join(game_dir, "assets")
    if index:
        dest = os.path.join(assets_dir, "indexes", index["id"] + ".json")
        plan.append({
            "phase": "asset index", "url": index["url"],
            "dest": dest,
            "size": index.get("size", 0), "sha1": index.get("sha1"),
            # Once this file lands the worker expands it into per-object
            # downloads (the objects list lives only inside the index).
            "_after_index": dest, "assets_dir": assets_dir,
        })
    # 4. java runtime.
    return plan


def library_artifact(lib):
    """Normalize Mojang download entries and ordinary loader Maven coordinates."""
    artifact = (lib.get("downloads") or {}).get("artifact")
    if artifact and artifact.get("path"):
        return artifact
    if not artifact and "downloads" in lib:
        # Mojang's classifiers-only records deliberately have no main jar.
        # Loader Maven records omit downloads metadata altogether.
        return None
    coordinate = str(lib.get("name") or "")
    name, separator, extension = coordinate.partition("@")
    parts = name.split(":")
    if len(parts) not in (3, 4) or not all(
            re.fullmatch(r"[A-Za-z0-9_.+\-]+", part) and part not in (".", "..") for part in parts):
        return artifact
    group, module, version = parts[:3]
    if any(not segment for segment in group.split(".")):
        return artifact
    extension = extension if separator else "jar"
    if not re.fullmatch(r"[A-Za-z0-9]+", extension):
        return artifact
    classifier = "-" + parts[3] if len(parts) == 4 else ""
    path = "/".join((group.replace(".", "/"), module, version,
                     module + "-" + version + classifier + "." + extension))
    if artifact:
        normalized = dict(artifact)
        normalized["path"] = path
        return normalized
    repo = str(lib.get("url") or "https://libraries.minecraft.net/")
    return {"path": path, "url": repo.rstrip("/") + "/" + path}


def _machine_arch():
    return "arm64" if os.environ.get("PROCESSOR_ARCHITECTURE", "").upper() == "ARM64" else "x86_64"


def _library_allows_windows(lib):
    """Minecraft's rule grammar, evaluated for this Windows machine.

    `os` is an object (`{"name": "windows"}`), not a string: comparing it
    directly made every platform-restricted library look disallowed, which is
    why no natives classifier was ever downloaded and the game could not start.
    Arch rules matter too -- the arm64 natives must not be fetched on x64.
    """
    rules = lib.get("rules")
    if not rules:
        return True
    arch = _machine_arch()
    allowed = False
    for rule in rules:
        action = rule.get("action")
        os_rule = rule.get("os") or {}
        name = os_rule.get("name")
        if name and name != "windows":
            continue
        wanted_arch = os_rule.get("arch")
        if wanted_arch and wanted_arch != arch and not (
            wanted_arch == "x86" and arch == "x86_64"
        ):
            continue
        allowed = action == "allow"
    return allowed


def _expand_index_objects(index_path, assets_dir):
    plan = []
    doc = json.load(open(index_path, encoding="utf-8"))
    for name, obj in (doc.get("objects") or {}).items():
        sha1 = obj.get("hash")
        plan.append({
            "phase": "assets",
            "url": "https://resources.download.minecraft.net/" + sha1[:2] + "/" + sha1,  # canonical asset CDN
            "dest": os.path.join(assets_dir, "objects", sha1[:2], sha1),
            "size": obj.get("size", 0), "sha1": sha1,
        })
    # Mojang's CDN path changed in 2026; the original host redirect still works:
    # the index carries map_to_resources/objects host info; fall back to the
    # canonical resources host if the primary 404s.
    for item in plan:
        item["fallthru"] = item["url"]
    return plan


def _install_all(game_dir, todo, jre, java_component, version):
    """Download the index first, then fetch distinct files concurrently."""
    owned = download_control.current()
    for item in todo:
        if owned:
            owned.checkpoint()
        source = item.get("source")
        if not source:
            continue
        if not os.path.isfile(source):
            raise RuntimeError("locally generated library is missing: " + source)
        if item.get("sha1") and _sha1_of(source) != item["sha1"]:
            raise RuntimeError("locally generated library failed its SHA-1 check: " + source)
        if os.path.abspath(source) == os.path.abspath(item["dest"]):
            _progress(item, os.path.getsize(source))
            continue
        os.makedirs(os.path.dirname(item["dest"]), exist_ok=True)
        temp = item["dest"] + ".part"
        with (owned.noninterruptible("Copying a generated library") if owned else download_control.bind(None)):
            try:
                shutil.copy2(source, temp)
                os.replace(temp, item["dest"])
            finally:
                if os.path.exists(temp):
                    os.remove(temp)
        _progress(item, os.path.getsize(item["dest"]))

    index_items = [item for item in todo if item.get("_after_index")]
    for item in index_items:
        _download_one(item)
        objects = _expand_index_objects(item["_after_index"], item["assets_dir"])
        with _LOCK:
            todo.extend(objects)

    # Version manifests can name the same file more than once (for example a
    # runtime shared by Java and Javac). The URL is the same, and writing each
    # to a single .part path concurrently races os.replace on Windows.
    unique = {}
    for item in todo:
        if item.get("source") or item.get("_after_index"):
            continue
        unique.setdefault(os.path.normcase(os.path.abspath(item["dest"])), item)
    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        def fetch(item):
            with download_control.bind(owned):
                return _download_one(item)
        futures = [pool.submit(fetch, item) for item in unique.values()]
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                errors.append(exc)
                if owned:
                    owned.finish("error")
    if errors:
        if any(isinstance(error, download_control.DownloadCancelled) for error in errors):
            raise download_control.DownloadCancelled("Download cancelled.")
        raise RuntimeError("; ".join(str(error) for error in errors[:3]))


def _fetch(url, dest, expected_size, expected_sha1):
    def progress(got):
        _progress({"dest": dest}, got)
    return download_control.download(url, dest, size=expected_size,
                                     hashes={"sha1": expected_sha1} if expected_sha1 else {},
                                     progress=progress, timeout=60)


def _sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for blob in iter(lambda: fh.read(1 << 20), b""):
            h.update(blob)
    return h.hexdigest()


def _download_one(item):
    for attempt in (1, 2, 3):
        try:
            _fetch(item["url"], item["dest"], item.get("size"), item.get("sha1"))
            _progress(item, os.path.getsize(item["dest"]))
            return
        except urllib.error.HTTPError as exc:
            if attempt == 3 or exc.code < 500 and exc.code != 403:
                raise
            time.sleep(attempt * 2)
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            if attempt == 3:
                raise RuntimeError(url_of(item) + ": " + str(exc))
            time.sleep(attempt * 2)


def url_of(item):
    return item.get("url") or item.get("_after_index") or "?"


def _item_key(item):
    return os.path.normcase(os.path.abspath(item["dest"]))


def _read_jre_manifest(state_dir, version_json_path=None):
    paths = [os.path.join(state_dir, "versions", "jre_manifest.json")]
    if version_json_path:
        paths.insert(0, os.path.join(os.path.dirname(version_json_path), "jre_manifest.json"))
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            continue
    return {}


def ensure_runtime(game_dir, state_dir, version, java_component=None):
    """Install only the version's Java runtime and return its executable.

    Loader installers use this before vanilla client/assets installation. The
    matching managed component wins; a matching system Java can supply old
    versions whose seeded runtime metadata is unavailable.
    """
    component = java_component or (version.get("javaVersion") or {}).get("component") or "jre-legacy"
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", component) or component in (".", ".."):
        raise RuntimeError("invalid Java runtime component")
    with _RUNTIME_LOCK:
        java = _runtime_java(game_dir, component)
        if java:
            return java
        jre = _read_jre_manifest(state_dir)
        if not _runtime_entries(jre, component):
            # PATH is checked for the required major, never whichever managed
            # runtime happens to be encountered first in the game directory.
            major = (version.get("javaVersion") or {}).get("majorVersion") or 8
            java = _system_java(major)
            if java:
                return java
        plan = _java_plan(game_dir, jre, component)
        _install_all(game_dir, plan, jre, component, version)
        java = _runtime_java(game_dir, component)
        if not java:
            raise RuntimeError("the required Java runtime is incomplete: " + component)
        return java


def find_java(game_dir, version, java_component=None):
    """Return the complete matching managed runtime or a matching PATH Java."""
    component = java_component or (version.get("javaVersion") or {}).get("component") or "jre-legacy"
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", component) or component in (".", ".."):
        return None
    java = _runtime_java(game_dir, component)
    if java:
        return java
    major = (version.get("javaVersion") or {}).get("majorVersion") or 8
    return _system_java(major)


def _system_java(required_major):
    path = shutil.which("java") or shutil.which("java.exe")
    if not path:
        return None
    try:
        result = subprocess.run([path, "-version"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        output = result.stdout.decode("utf-8", "replace")
        match = re.search(r'(?:version\s+"|\b(?:openjdk|java)\s+)(\d+(?:\.\d+)*)', output)
        if result.returncode or not match:
            return None
        parts = match.group(1).split(".")
        major = int(parts[1] if parts[0] == "1" and len(parts) > 1 else parts[0])
        return path if major == int(required_major) else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _java_plan(game_dir, jre, component):
    """java-runtime-<name> files, from the runtime's own manifest.json.

    jre_manifest.json's entries do not carry a zip download; each runtime ships
    a manifest listing every file (bin/javaw.exe, conf, legal, ...) with its own
    raw download. The manifest itself is fetched inline (the worker waits on it;
    it is ~150 KB, so blocking here is fine).
    """
    if not component:
        return []
    entries = _runtime_entries(jre, component)
    if not entries:
        raise RuntimeError("Java runtime metadata is unavailable for " + component)
    entries = sorted(entries, key=lambda e: (e.get("availability") or {}).get("group", 0), reverse=True)
    entry = entries[0]
    m = entry.get("manifest") or {}
    if not m.get("url"):
        raise RuntimeError("Java runtime manifest URL is missing for " + component)
    target_root = os.path.join(game_dir, "runtime", component)
    manifest_path = os.path.join(target_root, "manifest.json")
    try:
        _fetch(m["url"], manifest_path, m.get("size"), m.get("sha1"))
        with open(manifest_path, encoding="utf-8") as manifest_file:
            doc = json.load(manifest_file)
    except Exception as exc:                      # surfaced via state() for debugging
        if isinstance(exc, download_control.DownloadCancelled):
            raise
        _update(java_error=repr(exc))
        raise RuntimeError("could not fetch Java runtime manifest for %s: %s" % (component, exc)) from exc
    plan = []
    for path, meta in (doc.get("files") or {}).items():
        rel = modpacks._safe_relative(path)
        if not rel:
            raise RuntimeError("unsafe Java runtime path: " + str(path))
        kind = meta.get("type")
        dest = os.path.join(target_root, *rel.split("/"))
        if kind == "directory":
            os.makedirs(dest, exist_ok=True)
            continue
        if kind != "file":
            continue
        raw = (meta.get("downloads") or {}).get("raw") or {}
        if not raw.get("url"):
            continue
        plan.append({
            "phase": "java runtime", "url": raw["url"], "dest": dest,
            "size": meta.get("size", raw.get("size")), "sha1": meta.get("sha1") or raw.get("sha1"),
        })
    return plan


def _runtime_entries(jre, component):
    manifest = jre.get("manifest") or {}
    table = manifest.get("windows-x64") or {}
    entries = table.get(component) or []
    if not entries:
        for peer in ("windows-x86_64", "windows", "windows-x86"):
            entries = (manifest.get(peer) or {}).get(component) or []
            if entries:
                break
    return entries
