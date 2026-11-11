# Launcher updates

No update service address is bundled. Settings → Launcher updates shows setup until an HTTPS release directory is supplied. GitHub Releases currently provides manual setup/portable downloads; its cross-origin asset redirects do not meet this version's generic-feed policy, so do not paste a GitHub Releases URL into the automatic feed field.

Build the release first, then generate metadata from the actual output:

```powershell
node app/release-metadata.js "C:\path\to\release"
```

The command writes `latest.yml` with exact installer byte size and SHA-512, plus `Minecraft-Launcher-<version>-release.json` with app identity, size, SHA-256 and SHA-512. Host those metadata files and the full NSIS setup in one HTTPS directory. Upload the setup and versioned manifest before replacing `latest.yml`; avoid long caching for `latest.yml`. Portable executables and blockmaps are not used for automatic updates.

The generic feed requires a credential-free HTTPS directory URL. It rejects HTTPS downgrades, redirects to another origin, directory escapes, unrelated app installers and prerelease versions. The staged setup is rechecked before Restart and install. Installation remains unavailable during active game/download/commit work, and closing the app never automatically installs an update.

Publishing metadata does not configure clients or validate a hosted service. A future GitHub updater provider needs an explicit implementation and verification of its redirect/asset rules.
