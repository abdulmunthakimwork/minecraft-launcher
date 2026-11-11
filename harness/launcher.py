"""Real game launcher for the harness.

`installer.py` puts the actual client on disk. This module does the second
half of what launcher.dll does when the user presses Play: read the installed
version document, build the Java command line the game itself declares
(classpath from the client jar plus every Windows library, natives extracted
out of their classifier jars, the asset index the version names, the JVM
arguments and the game arguments with their `${...}` placeholders filled), and
run it as a child process whose output is streamed back to the UI.

One process at a time, module-level, guarded by a lock -- the same contract the
native launcher offers: Play starts the game, and the launcher's log window
shows what the game printed.

    status()                 -> snapshot for /__launch_status
    log_since(n)             -> {"lines": [...], "next": int, "running": bool}
    start(...)               -> snapshot after spawning
    stop()                   -> snapshot after terminating
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile

import modpacks
import installer

_LOCK = threading.Lock()

STATE = {
    "running": False,
    "pid": None,
    "versionId": None,
    "startedAt": None,
    "exitCode": None,
    "error": None,
    "command": None,
    "logPath": None,
}

LINES = []          # ring buffer of decoded output lines
LOG_LIMIT = 4000
_LINE_OFFSET = 0    # absolute index of the oldest retained line
_PROCESS = None
_LOG_PATH = None
_SECRET_VALUES = ()  # host-only credentials used to sanitize game diagnostics


# --- helpers ---------------------------------------------------------------
def _os_name():
    return "windows" if sys.platform.startswith("win") else ("osx" if sys.platform == "darwin" else "linux")


def _rule_allows(rules, features=None):
    """Minecraft's rule grammar: last matching rule wins; no rules means allow."""
    if not rules:
        return True
    allowed = False
    for rule in rules:
        action = rule.get("action")
        os_rule = rule.get("os") or {}
        if os_rule.get("name") and os_rule["name"] != _os_name():
            continue
        if os_rule.get("arch"):
            arch = "x86" if sys.maxsize <= 2 ** 32 else "x86_64"
            if os_rule["arch"] not in (arch, "arm64" if arch == "x86_64" else arch):
                continue
        feat = rule.get("features") or {}
        if feat:
            match = all(bool((features or {}).get(k)) == bool(v) for k, v in feat.items())
            if not match:
                continue
        allowed = action == "allow"
    return allowed


def _classpath(version, game_dir):
    entries = []
    jar_id = str(version.get("jar") or version.get("id") or "")
    jar = os.path.join(game_dir, "versions", jar_id, jar_id + ".jar")
    if os.path.isfile(jar):
        entries.append(jar)
    for lib in version.get("libraries") or []:
        # Platform-restricted entries (the linux/macos native jars) must stay off
        # a Windows classpath: their files are not installed and the JVM would
        # reject the missing entry on startup.
        if not _rule_allows(lib.get("rules")):
            continue
        artifact = installer.library_artifact(lib)
        if artifact and artifact.get("path"):
            entries.append(os.path.join(game_dir, "libraries", *artifact["path"].split("/")))
    # Deduplicate, keep order (client jar first, as the launcher does).
    seen = set()
    out = []
    for entry in entries:
        if entry not in seen:
            seen.add(entry)
            out.append(entry)
    return out


def _extract_natives(version, game_dir):
    """Unzip every natives classifier into the version's natives directory."""
    target = os.path.join(game_dir, "versions", version.get("id", ""), "natives")
    for lib in version.get("libraries") or []:
        classifier = (lib.get("downloads") or {}).get("classifiers") or {}
        entry = classifier.get("natives-windows")
        if not entry or not entry.get("path"):
            continue
        jar = os.path.join(game_dir, "libraries", *entry["path"].split("/"))
        if not os.path.isfile(jar):
            continue
        marker = os.path.join(target, "." + os.path.basename(jar) + ".extracted")
        if os.path.isfile(marker):
            continue
        os.makedirs(target, exist_ok=True)
        try:
            with zipfile.ZipFile(jar) as archive:
                for name in archive.namelist():
                    if name.endswith("/") or name.startswith("META-INF"):
                        continue
                    if not name.lower().endswith((".dll", ".so", ".dylib", ".jnilib")):
                        continue
                    dest = os.path.join(target, os.path.basename(name))
                    with archive.open(name) as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write("ok")
        except (OSError, zipfile.BadZipFile):
            continue
    return target


def _expand(value, mapping):
    def repl(match):
        key = match.group(1)
        if key in mapping and mapping[key] is not None:
            return str(mapping[key])
        return match.group(0)

    return re.sub(r"\$\{([^}]+)\}", repl, value)


def _join_args(args, mapping):
    out = []
    for arg in args:
        if isinstance(arg, str):
            out.append(_expand(arg, mapping))
        elif isinstance(arg, dict):
            if _rule_allows(arg.get("rules")):
                value = arg.get("value")
                if isinstance(value, list):
                    out.extend(_expand(v, mapping) for v in value)
                elif isinstance(value, str):
                    out.append(_expand(value, mapping))
    return out


def _find_java(game_dir, version, log):
    # Share runtime completeness and major-version selection with installation
    # and readiness, including jre-legacy for metadata predating javaVersion.
    java = installer.find_java(game_dir, version)
    if java:
        log.append("Using Java runtime: " + java)
    return java


def _account_identity(state_dir):
    """The player name/uuid the client will show.

    Two stores are consulted: the harness' own offline accounts (a list keyed by
    the selected id) and the captured launcher store (a map whose Microsoft
    record carries only a username -- its profile blob is empty). A record
    without a usable hex id gets the standard offline UUID, the same arithmetic
    an offline server applies, so the game and the harness' skin service agree
    on who the player is.
    """
    name = None
    ident = None
    data_dir = os.environ.get("LAUNCHER_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
    for path in (
        os.path.join(data_dir, "offline_accounts.json"),
        os.path.join(state_dir, "launcher_accounts_microsoft_store.json"),
    ):
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            if text.startswith("#$"):
                text = text[text.index("{"):]
            doc = json.loads(text)
        except (OSError, ValueError):
            continue
        accounts = doc.get("accounts") or {}
        if isinstance(accounts, list):
            active = doc.get("active")
            for acc in accounts:
                if not isinstance(acc, dict) or (active and acc.get("id") != active):
                    continue
                name = acc.get("name") or acc.get("username")
                ident = acc.get("uuid") or acc.get("id")
                break
        elif isinstance(accounts, dict):
            for acc in accounts.values():
                if not isinstance(acc, dict):
                    continue
                profile = acc.get("minecraftProfile") or {}
                name = profile.get("name") or acc.get("username") or acc.get("name")
                ident = profile.get("id") or acc.get("uuid")
                break
        if name:
            break
    name = name or "Player"
    digits = "".join(ch for ch in (ident or "") if ch in "0123456789abcdefABCDEF")
    if len(digits) == 32:
        return name, digits
    return name, uuid.uuid3(uuid.NAMESPACE_DNS, "OfflinePlayer:" + name).hex


def _client_jar(game_dir, version_id):
    return os.path.join(game_dir, "versions", version_id, version_id + ".jar")


def identity(state_dir):
    """The player name/uuid this launcher starts the game as."""
    return _account_identity(state_dir)


def status():
    with _LOCK:
        snapshot = dict(STATE)
        snapshot["lines"] = LINES[-40:]
    return snapshot


def log_since(since):
    with _LOCK:
        total = _LINE_OFFSET + len(LINES)
        start = min(total, max(_LINE_OFFSET, int(since or 0)))
        lines = LINES[start - _LINE_OFFSET:]
        next_index = total
        running = STATE["running"]
    return {"lines": lines, "next": next_index, "running": running, "total": total}


def stop():
    global _PROCESS
    with _LOCK:
        proc = _PROCESS
        starting = STATE['running'] and proc is None
        if starting:
            STATE['error'] = 'Minecraft is still starting. Try Stop again when startup finishes.'
    if starting:
        return status()
    if proc is None:
        return status()
    error = None
    code = proc.poll()
    attempted = code is None
    if code is None:
        try:
            if sys.platform.startswith("win"):
                result = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                if result.returncode:
                    error = 'The operating system could not stop Minecraft.'
            else:
                proc.terminate()
            if error is None:
                proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            error = 'Minecraft did not stop. Try again or close its window before editing installation files.'
        code = proc.poll()
    with _LOCK:
        if _PROCESS is proc:
            STATE.update(running=code is None, exitCode=code,
                         error=(error or 'Minecraft is still running. Close its window before editing installation files.') if code is None else None)
            owned = True
        else:
            owned = False
    if owned and attempted and code is not None:
        _append("[harness] game terminated by the launcher", owner=proc)
    return status()


def _redact(text, secrets=None):
    for secret in (_SECRET_VALUES if secrets is None else secrets):
        if secret and secret != "0":
            text = text.replace(secret, "[REDACTED]")
    return text


def _append(line, secrets=None, owner=None):
    global _LINE_OFFSET
    line = _redact(line, secrets)
    with _LOCK:
        if owner is not None and _PROCESS is not owner:
            return None
        LINES.append(line)
        excess = max(0, len(LINES) - LOG_LIMIT)
        if excess:
            del LINES[:excess]
            _LINE_OFFSET += excess
    return line


def _pump(proc, log_path=..., secrets=None):
    if log_path is ...:
        log_path = _LOG_PATH
    if secrets is None:
        secrets = _SECRET_VALUES
    try:
        for raw in iter(proc.stdout.readline, b""):
            try:
                text = raw.decode("utf-8", "replace").rstrip()
            except Exception:
                text = repr(raw)
            text = _append(text, secrets, owner=proc)
            if text is None:
                continue
            if log_path:
                try:
                    with _LOCK:
                        if _PROCESS is proc:
                            with open(log_path, "a", encoding="utf-8") as fh:
                                fh.write(text + "\n")
                except OSError:
                    pass
    except Exception:
        pass
    code = proc.wait()
    with _LOCK:
        if _PROCESS is proc:
            STATE.update(running=False, exitCode=code, error=None)
    _append("[harness] game exited with code " + str(code), owner=proc)


def resolve_version_id(state_dir, version_id):
    """The concrete version id for an alias, the way launcher.dll reads it.

    A profile's `lastVersionId` is often `latest-release` / `latest-snapshot`
    rather than a number: the launcher resolves those against the version
    manifest on every launch, so a new release moves the installation with it.
    """
    latest_path = os.path.join(state_dir, "versions", "version_manifest_v2.json")
    latest = {}
    if version_id in (None, "", "latest", "latest-release", "latest-snapshot"):
        try:
            with open(latest_path, encoding="utf-8-sig") as fh:
                latest = json.load(fh).get("latest") or {}
        except (OSError, ValueError):
            latest = {}
    if version_id in (None, "", "latest", "latest-release"):
        return latest.get("release") or ""
    if version_id == "latest-snapshot":
        return latest.get("snapshot") or ""
    return str(version_id)


def _version_metadata_path(state_dir, version_id, version_roots=None):
    """Find local metadata in the same store order the host enumerates."""
    if not installer._safe_version_id(version_id):
        raise RuntimeError("invalid version id: " + str(version_id))
    return installer._version_metadata_path(state_dir, version_id, version_roots)


def _resolve_version(state_dir, version_id, depth=0, version_roots=None):
    version_id = resolve_version_id(state_dir, version_id)
    if not version_id:
        raise RuntimeError("no installed version to launch")
    path = _version_metadata_path(state_dir, version_id, version_roots)
    if not path:
        raise RuntimeError("version metadata missing for " + version_id)
    with open(path, encoding="utf-8-sig") as fh:
        version = json.load(fh)
    parent_id = version.get("inheritsFrom")
    if not parent_id:
        return version_id, version
    if depth >= 16:
        raise RuntimeError("version inheritance is too deep for " + version_id)
    parent_path = _version_metadata_path(state_dir, str(parent_id), version_roots)
    if not parent_path:
        raise RuntimeError("base Minecraft version " + str(parent_id) + " is missing for " + version_id)
    _, parent = _resolve_version(state_dir, str(parent_id), depth + 1, version_roots)
    merged = modpacks.flatten_loader_profile(version, parent)
    merged["jar"] = str(parent.get("jar") or parent.get("id") or parent_id)
    return version_id, merged


def start(state_dir, game_dir, version_id=None, log_path=None, server_port=None,
          server_host="localhost", run_dir=None, java_dir=None, java_args=None,
          resolution=None, player=None, configuration_id=None):
    """Spawn the game. Raises RuntimeError with a user-facing reason on failure.

    `game_dir` is where the version is installed (jar, libraries, assets, the
    runtime). `run_dir` is the installation's own game directory, when the
    profile sets one: the launcher starts the same files but points the client's
    saves, options and logs at that folder. `java_dir`, `java_args` and
    `resolution` are the profile's run settings; each is optional and only
    overrides the version's own value.
    """
    global _PROCESS, _LOG_PATH, _LINE_OFFSET, _SECRET_VALUES
    with _LOCK:
        already_running = STATE["running"]
        if not already_running:
            _PROCESS = None
            token = str((player or {}).get("access_token") or "0")
            _SECRET_VALUES = (token,) if token != "0" else ()
            STATE.update({"running": True, "pid": None, "exitCode": None,
                          "error": None, "startedAt": time.time(),
                          "configurationId": configuration_id})
    if already_running:
        return status()
    log = []
    install_dir = game_dir
    if run_dir:
        game_dir = run_dir
    try:
        version_id, version = _resolve_version(state_dir, version_id,
                                               version_roots=[install_dir, run_dir])
        jar = _client_jar(install_dir, str(version.get("jar") or version_id))
        if not os.path.isfile(jar):
            raise RuntimeError("Minecraft " + version_id + " is not installed yet")
        java = ""
        if java_dir:
            candidate = java_dir
            if os.path.isdir(candidate):
                candidate = os.path.join(candidate, "java.exe" if sys.platform.startswith("win") else "bin/java")
            if os.path.isfile(candidate):
                java = candidate
                log.append("Using Java runtime: " + candidate)
            else:
                log.append("Configured Java executable is missing; using the version's runtime")
        if not java:
            java = _find_java(install_dir, version, log)
        if not java:
            raise RuntimeError("no Java runtime found; reinstall the version to fetch its runtime")
        natives = _extract_natives(version, install_dir)
        classpath = _classpath(version, install_dir)
        assets_root = os.path.join(install_dir, "assets")
        index = (version.get("assetIndex") or {}).get("id") or "legacy"
        name, uuid = _account_identity(state_dir)
        # An online Microsoft account (harness/msa.py) launches with its real
        # Minecraft profile name, UUID and access token, and the client is
        # told the session is `msa` -- that is what lets a licensed account
        # play on online servers. Offline accounts keep the legacy identity.
        access_token = "0"
        xuid = "0"
        user_type = "legacy"
        if player:
            name = str(player.get("name") or name)
            uuid = str(player.get("uuid") or uuid)
            access_token = str(player.get("access_token") or "0")
            xuid = str(player.get("xuid") or "0")
            user_type = str(player.get("user_type") or "msa")
        try:
            os.makedirs(game_dir, exist_ok=True)
        except OSError:
            pass
        mapping = {
            "auth_player_name": name,
            "version_name": version_id,
            "game_directory": game_dir,
            "assets_root": assets_root,
            "assets_index_name": index,
            "auth_uuid": uuid,
            "auth_access_token": access_token,
            "auth_session": access_token,
            "auth_xuid": xuid,
            "clientid": "harness",
            "user_type": user_type,
            "version_type": version.get("type") or "release",
            "natives_directory": natives,
            "launcher_name": "minecraft-launcher",
            "launcher_version": os.environ.get("LAUNCHER_VERSION") or "3.41.30",
            "classpath": os.pathsep.join(classpath),
            "classpath_separator": os.pathsep,
            "library_directory": os.path.join(install_dir, "libraries"),
            "resolution_width": str((resolution or {}).get("width") or 854),
            "resolution_height": str((resolution or {}).get("height") or 480),
            "quickPlayPath": os.path.join(state_dir, "quickPlay", "quickPlayLog.json"),
        }
        args = [java, "-Xmx2G", "-XX:+UnlockExperimentalVMOptions", "-XX:+UseG1GC"]
        if java_args:
            try:
                args += shlex.split(str(java_args))
            except ValueError:
                args += str(java_args).split()
        modern = version.get("arguments") or {}
        if modern.get("jvm"):
            args += _join_args(modern.get("jvm"), mapping)
        # Newer versions declare the natives path themselves; legacy ones rely on
        # the launcher to add it.
        if not any(str(a).startswith("-Djava.library.path=") for a in args):
            args.append("-Djava.library.path=" + natives)
        if server_port:
            # The client resolves its Minecraft Services roots from a discovery
            # document. Pointing that at the harness makes the game fetch the
            # player's profile and skin from this server, so the skin applied in
            # the launcher is the one rendered in game.
            args.append(
                "-Dminecraft.api.discovery.host=http://%s:%d"
                "/discovery/minecraft/client" % (server_host, server_port)
            )
        args += [version.get("mainClass") or "net.minecraft.client.main.Main"]
        if modern.get("game"):
            args += _join_args(modern.get("game"), mapping)
            # The profile's window size. Modern version documents no longer
            # declare ${resolution_width}/${resolution_height}, but the client
            # still parses --width/--height, which is what the native launcher
            # passes for an installation with its own resolution.
            if resolution and (resolution.get("width") or resolution.get("height")) \
                    and "--width" not in args:
                args += ["--width", mapping["resolution_width"],
                         "--height", mapping["resolution_height"]]
        else:
            legacy = version.get("minecraftArguments") or \
                "--username ${auth_player_name} --version ${version_name} --gameDir ${game_directory} " \
                "--assetsDir ${assets_root} --assetIndex ${assets_index_name} --uuid ${auth_uuid} " \
                "--accessToken ${auth_access_token} --userType ${user_type} --versionType ${version_type}"
            # Split the metadata template first. Expansion can introduce spaces
            # in a player name or filesystem path, which belong to one argument.
            args += [_expand(arg, mapping) for arg in shlex.split(legacy)]

        if sys.platform.startswith("win"):
            os.makedirs(os.path.join(game_dir, "logs"), exist_ok=True)
        _LOG_PATH = log_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "game.log")
        try:
            with open(_LOG_PATH, "w", encoding="utf-8") as fh:
                fh.write(_redact("[harness] " + " ".join(log)) + "\n")
                fh.write(_redact("[harness] launching " + version_id) + "\n")
        except OSError:
            _LOG_PATH = None

        creationflags = 0
        if sys.platform.startswith("win"):
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # A modern classpath is ~18 KB of absolute paths; keeping it on the
        # command line gets close to Windows' 32 KB limit (and blew past a few
        # intermediate APIs). The JVM reads CLASSPATH from the environment when
        # no -cp is given, so the classpath travels there and the command line
        # stays short.
        child_env = dict(os.environ)
        child_env["CLASSPATH"] = mapping["classpath"]
        # The metadata's JVM arguments commonly contain -cp ${classpath}.
        # Move that option off argv too; Java ignores CLASSPATH when -cp remains.
        main_index = args.index(version.get("mainClass") or "net.minecraft.client.main.Main")
        jvm_args = []
        index = 0
        while index < main_index:
            value = args[index]
            if value in ("-cp", "-classpath", "--class-path"):
                if index + 1 >= main_index:
                    raise RuntimeError("Java classpath option has no value")
                child_env["CLASSPATH"] = args[index + 1]
                index += 2
                continue
            if value.startswith("--class-path="):
                child_env["CLASSPATH"] = value.split("=", 1)[1]
            else:
                jvm_args.append(value)
            index += 1
        args = jvm_args + args[main_index:]
        proc = subprocess.Popen(
            args,
            cwd=game_dir,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        with _LOCK:
            _PROCESS = proc
            STATE.update({"pid": proc.pid, "versionId": version_id,
                          "command": _redact(" ".join(args[:6])) + " ... (CLASSPATH via env)",
                          "logPath": _LOG_PATH, "error": None})
            _LINE_OFFSET += len(LINES)
            LINES.clear()
        _append("[harness] launched Minecraft " + version_id + " (pid " + str(proc.pid) + ")")
        threading.Thread(target=_pump, args=(proc, _LOG_PATH, _SECRET_VALUES), daemon=True, name="mc-log").start()
        return status()
    except Exception as exc:
        error = _redact(str(exc))
        with _LOCK:
            STATE.update({"running": False, "error": error})
        raise RuntimeError(error) from None
