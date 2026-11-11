# Minecraft Launcher reconstruction

A Windows desktop launcher host with an Electron shell and Python backend. This repository contains the authored host, bridge, launcher extensions and regression tests. It works with a locally supplied extracted launcher interface; that interface and its captured media are not included in this source snapshot.

Version: **3.41.31**. Download the Windows setup or portable build from [Releases](https://github.com/abdulmunthakimwork/minecraft-launcher/releases).

Features include Java installations and mod loaders, a compatible mod browser, dependency repair, transactional content changes, modpack import/export, offline profiles, Microsoft sign-in setup, download controls, background game lifecycle, local crash explanations and verified GitHub launcher updates.

Microsoft sign-in requires your own registered public Microsoft client ID. Bedrock launch, Realms management, Marketplace purchases and messaging are not implemented here; their screens provide official destinations. This is a reconstruction project and is not an official Mojang or Microsoft release. No project-wide source license is declared.

See [Build and verification](docs/BUILD.md), [Architecture](docs/ARCHITECTURE.md) and [Updates](docs/UPDATES.md).

## Quick start

Install Node.js/npm and Python 3 with Tk, then provide the local resource prerequisites described in the build guide.

```powershell
npm ci --prefix app
```

Use the build guide's isolated-development command to copy neutral seed files into a new game folder and start the source checkout.

The packaged app bundles the Python host. Accounts and settings are stored in the user's data directories; public builds use the neutral templates in `build-seeds/` and never seed account tokens, personal profiles, quick-play history or custom capes.
