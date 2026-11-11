# Build and verification

The build requires Windows, Node.js/npm, Python 3 with Tk and PyInstaller. The host uses Python's standard library; Pillow is needed only for the optional default-texture helper.

```powershell
python -m pip install pyinstaller
npm ci --prefix app
```

## Local resource prerequisites

The compiled interface and media are excluded from this source repository. Supply them locally before starting or packaging:

| Local path | Purpose |
| --- | --- |
| `minecraft-launcher-re/src/ui/` | Extracted compiled interface and its static resources |
| `harness/content/` | Static launcher content mirror |
| `harness/fonts/` | Interface font resources |
| `harness/assets/media/` | Static catalog artwork, video and audio |
| `harness/assets/skins/{alex,alex_avatar,steve,steve_avatar}.png` | Default textures and account icons |
| `harness/assets/mojang_cape.png` | Default cape resource |
| `harness/versions.json` | Version list consumed by the interface |
| `app/build/icon.ico` | Window and installer icon |

The public metadata helpers `re-tools/capture_content.py` and `re-tools/capture_versions.py` regenerate their respective content/version mirrors from official endpoints. `re-tools/make_default_skins.py` downloads default textures and creates the avatar crops; it requires Pillow. These helpers do not provide the compiled interface, fonts, supplemental catalog media or icon.

The public build configuration packages only the default textures named above. It excludes custom skin/cape folders, captured account/configuration stores and quick-play history. The `build-seeds/` files contain an empty profile map, empty settings/product/UI/treatment objects, an empty host overlay and official version/runtime manifest metadata. They are the only first-run state inputs used by this build.

PNG resources can carry local paths in embedded text or EXIF metadata. Before packaging, run the included helper on disposable local resource copies. It removes text, EXIF and timestamp chunks while retaining pixel/color chunks and their CRCs verbatim.

```powershell
python tools/sanitize-png-metadata.py minecraft-launcher-re/src/ui
python tools/sanitize-png-metadata.py harness/assets
python tools/sanitize-png-metadata.py harness/content
```

## Development and packaging

The NSIS configuration disables precompressed-file exclusions so a combined setup/portable build keeps every video in the shared application archive.

```powershell
npm run dist --prefix app
node app/release-metadata.js release
```

`npm run dist` freezes `harness/server.py` with PyInstaller, then creates the Windows NSIS setup and portable executable under `release/`. The resulting application does not require a separate Node or Python installation.

A development checkout does not automatically copy the packaged first-run seeds. For an isolated development game folder, copy the neutral files first and choose that folder explicitly:

```powershell
$devGame = Join-Path (Get-Location) '.dev-game'
New-Item -ItemType Directory -Path $devGame -Force | Out-Null
Copy-Item -Path 'build-seeds/default-state/*' -Destination $devGame -Recurse
npm start --prefix app -- "--minecraft-dir=$devGame"
```

You can instead choose an existing game folder with `--minecraft-dir`. The default checkout path is the local captured configuration directory; it is excluded from this source snapshot.

Existing account files, settings and installation profiles are never overwritten by first-run seeding. Public starter files carry no accounts. Do not replace the neutral seed paths with a personal capture when publishing a build.

## Verification

```powershell
python -B -m unittest discover -s harness -p "test_*.py"
npm test --prefix app
```

The app suite covers the shell, lifecycle, IPC, release metadata and updater using disposable state and synthetic processes/network. Python tests cover the host and installation/content workers with disposable data. Node UI regressions live in `harness/*.test.cjs`; several run a native Electron fixture or require locally supplied interface icons, so the complete UI gate requires all resource prerequisites.

Run the sanitizer regression fixture with `python -B -m unittest discover -s tools -p "test_*.py"`.

Tests do not establish successful production Microsoft sign-in or a live hosted update. Verify a packaged release with disposable user/game data, its checksums, and a normal installation before publishing.
