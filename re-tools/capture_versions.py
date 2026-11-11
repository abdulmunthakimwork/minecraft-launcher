"""Capture the Minecraft version manifest into harness/versions.json.

The launcher gets its list of game versions from the host: `appData.gameVersions`
holds `.latest` plus a map of version id -> `{type, releaseTime, ...}`. The
reconstructed UI reads that map for three things: the version selector, the
Java patch-notes filter (which drops any note whose version has no `releaseTime`),
and the "release vs snapshot" split the patch-notes page shows. Without it the
patch-notes page renders its "Couldn't find patch notes" state even though every
note is mirrored.

The manifest is the same document the launcher downloads from piston-meta at
startup, so nothing is invented here.

    python re-tools/capture_versions.py
"""
import json
import os
import sys
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "harness", "versions.json")
URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# The launcher's own slice is `{latest: {release, snapshot}, versions: {id: ...}}`.
# Only these fields are read by the UI; `url`/`sha1` are kept because they are
# what an installer needs to resolve a version, and dropping them would mean a
# second capture the moment installs are wired up.
FIELDS = ("id", "type", "url", "time", "releaseTime", "sha1", "complianceLevel")


def main():
    req = urllib.request.Request(URL, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        manifest = json.load(resp)
    versions = {}
    for entry in manifest.get("versions") or []:
        vid = entry.get("id")
        if not vid:
            continue
        versions[vid] = {k: entry[k] for k in FIELDS if k in entry}
    # The launcher's `latest` is keyed the same way as the manifest.
    latest = {
        "release": (manifest.get("latest") or {}).get("release", ""),
        "snapshot": (manifest.get("latest") or {}).get("snapshot", ""),
    }
    payload = {"latest": latest, "versions": versions}
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    os.replace(tmp, OUT)
    print("captured %d versions (latest %s / %s) into %s"
          % (len(versions), latest["release"], latest["snapshot"], OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
