"""Capture the launcher's remote content into harness/content/ so the app can
run with no Mojang server dependency.

The UI fetches launcher content from
    https://launchercontent.mojang.com/v2/<file>
and builds image URLs as `${launcherContentUrl}<relative-path>`.

This script mirrors that tree locally:
    harness/content/v2/<file>                  (content JSON)
    harness/content/<relative-image-path>      (hero art, thumbnails, videos)

Run it while the network is up; afterwards the harness serves the tree and the
UI never touches Mojang again.
"""
import json
import os
import posixpath
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "harness", "content")
ORIGIN = "https://launchercontent.mojang.com"
STORE_ORIGIN = "https://store.mktpl.minecraft-services.net"

CONTENT_FILES = [
    # The Bedrock FAQ page loads this before it renders anything; a missing
    # document is the page's "Could not find FAQ" state.
    "faq/en-US.json",
    "promotionalCalendars.json",
    "news.json",
    "alertMessaging.json",
    "dynamicPromotionalContent.json",
    "highlightDialogs.json",
    "marketplacePassItems.json",
    "navigationIconOverride.json",
    "javaPatchNotes.json",
    "bedrockPatchNotes.json",
    "dungeonsPatchNotes.json",
    "dungeons2PatchNotes.json",
    "legendsPatchNotes.json",
    "launcherPatchNotes_v2.json",
    "voteEvent.json",
    "games.json",
    "_texurePackage.json",
]

# Content the launcher requests per locale from a sub-directory rather than as a
# single document. Only the base locale is mirrored; the server falls back to it
# for every other language.
DYNAMIC_FILES = (
    ("homepagePromotionalItems", "en-US"),
    ("dungeonsDLC", "en-US"),
)

MEDIA_RE = re.compile(
    r"^/?[\w\-./%@ ]+\.(?:png|jpe?g|webp|gif|svg|mp4|webm|mov|apng)$",
    re.IGNORECASE,
)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def save(url, relpath):
    dest = os.path.join(OUT, relpath.replace("/", os.sep))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # The mirror is durable, so a re-run only has to fill the gaps. Without this
    # a single added document -- a new patch note, the FAQ -- re-downloads the
    # whole tree, including every image, and takes minutes to do it.
    if os.path.isfile(dest) and os.path.getsize(dest) > 0 and not os.environ.get("REFRESH_CONTENT"):
        return True
    # Paths in the content documents contain spaces; percent-encode each
    # segment but keep the separators intact.
    enc = "/".join(urllib.parse.quote(seg) for seg in url.split("/")[3:])
    url = "/".join(url.split("/")[:3]) + "/" + enc
    try:
        body, ctype = fetch(url)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        print("  MISS %-70s %s" % (relpath, exc))
        return False
    with open(dest, "wb") as fh:
        fh.write(body)
    print("  ok   %-70s %7d bytes  %s" % (relpath, len(body), ctype))
    return True


def walk_strings(node, out):
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            walk_strings(v, out)
    elif isinstance(node, list):
        for v in node:
            walk_strings(v, out)


def media_paths(doc):
    """Relative media paths referenced anywhere in a content document."""
    strings = []
    walk_strings(doc, strings)
    found = set()
    for s in strings:
        s = s.strip()
        if not s or s.startswith(("http://", "https://", "data:", "minecraft://")):
            continue
        if MEDIA_RE.match(s):
            found.add(s.lstrip("/"))
    return found


def main():
    os.makedirs(OUT, exist_ok=True)
    ok = 0
    print("== content json ==")
    for name in CONTENT_FILES:
        if save("%s/v2/%s" % (ORIGIN, name), "v2/" + name):
            ok += 1

    # The UI queries `news/<newsType>.json` for the filtered news pages and
    # `news.json` for the front page; the types come from the entries.
    news_path = os.path.join(OUT, "v2", "news.json")
    if os.path.isfile(news_path):
        try:
            with open(news_path, encoding="utf-8-sig") as fh:
                news = json.load(fh)
        except ValueError:
            news = {}
        types = set()
        for entry in (news.get("entries") or []):
            for t in entry.get("newsType") or []:
                types.add(t)
        for t in sorted(types):
            save("%s/v2/news/%s.json" % (ORIGIN, t), "v2/news/%s.json" % t)

    # The news feed is also requested per locale (`news/<lang>.json`), so the
    # feed follows the launcher language. Every shipped locale is mirrored; the
    # server falls back to news.json for any that are not available.
    locales_dir = os.path.join(BASE, "minecraft-launcher-re", "src", "locales")
    if os.path.isdir(locales_dir):
        for loc in sorted(os.listdir(locales_dir)):
            if os.path.isdir(os.path.join(locales_dir, loc)):
                save(
                    "%s/v2/news/%s.json" % (ORIGIN, loc),
                    "v2/news/%s.json" % loc,
                )

    # Patch notes are split: `<product>PatchNotes.json` lists the articles and
    # each entry's `contentPath` points at the document holding its body. The UI
    # fetches that document when a note is opened, so the list alone leaves
    # every article empty.
    print("== patch note articles ==")
    articles = set()
    for root, _dirs, files in os.walk(os.path.join(OUT, "v2")):
        for f in files:
            if not f.endswith(".json"):
                continue
            try:
                with open(os.path.join(root, f), encoding="utf-8-sig") as fh:
                    doc = json.load(fh)
            except (OSError, ValueError):
                continue
            entries = doc.get("entries") if isinstance(doc, dict) else None
            for entry in entries or []:
                path = entry.get("contentPath") if isinstance(entry, dict) else None
                if isinstance(path, str) and path:
                    articles.add(path.lstrip("/"))
    print("  %d referenced documents" % len(articles))
    for rel in sorted(articles):
        save("%s/v2/%s" % (ORIGIN, rel), "v2/" + rel)

    # Per-locale dynamic content (home promos, Dungeons DLC).
    print("== dynamic content ==")
    for name, locale in DYNAMIC_FILES:
        save(
            "%s/v2/%s/%s.json" % (ORIGIN, name, locale),
            "v2/%s/%s.json" % (name, locale),
        )

    # Every content document may reference media; collect and mirror it all.
    print("== media ==")
    media = set()
    for root, _dirs, files in os.walk(os.path.join(OUT, "v2")):
        for f in files:
            if not f.endswith(".json"):
                continue
            path = os.path.join(root, f)
            try:
                with open(path, encoding="utf-8-sig") as fh:
                    doc = json.load(fh)
            except (OSError, ValueError):
                continue
            media |= media_paths(doc)
    print("  %d media paths referenced" % len(media))
    counter = [0]
    lock = threading.Lock()

    def grab(m):
        save("%s/%s" % (ORIGIN, m), m)
        with lock:
            counter[0] += 1
            if counter[0] % 50 == 0:
                print("   ... %d/%d" % (counter[0], len(media)), flush=True)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(grab, sorted(media)))
    ok += counter[0]

    # The marketplace store catalogue the UI reads for the in-app store.
    print("== marketplace store ==")
    for path in (
        "/api/v1.0/catalog/web/20CA2/pages",
        "/api/v1.0/catalog/web/20CA2/pages/marketplacePass",
    ):
        rel = "store" + path
        dest = os.path.join(OUT, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            body, ctype = fetch(STORE_ORIGIN + path)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            print("  MISS %-70s %s" % (rel, exc))
            continue
        with open(dest, "wb") as fh:
            fh.write(body)
        print("  ok   %-70s %7d bytes  %s" % (rel, len(body), ctype))
        ok += 1

    print("captured %d files into %s" % (ok, OUT))


if __name__ == "__main__":
    sys.exit(main())
