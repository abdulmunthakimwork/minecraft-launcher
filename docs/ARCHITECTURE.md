# Architecture

`app/main.js` owns the Electron window, local host process, single-instance behavior and desktop IPC. `app/lifecycle.js` keeps active games/work alive when the window closes and coordinates safe update/shutdown admission. `app/updater.js` stages and verifies full NSIS installers, then awaits installer-process creation before quitting.

`harness/server.py` serves the supplied compiled interface and local resources. `harness/bridge.js` implements its host calls and event contracts. `harness/ui.js` adds native-themed controls and dialogs, while `harness/installation_ui.js` owns the installation workspace. The captured interface is served without editing its files; narrow behavior rewrites happen as responses are served.

Python worker modules resolve installations, loaders, Java, dependencies, mods and modpacks. Content mutations use staged transactions and job-scoped controls. Crash explanations use bounded evidence from the selected installation's logs and run locally. `harness/msa.py` stores Microsoft sessions in the application's data folder using Windows protection; a publisher must provide its own registered public client ID.

Packaged resources are read-only. Electron state lives in `%APPDATA%\Minecraft Launcher`; host accounts/settings/logs live under its `data` folder. The game directory defaults to `%APPDATA%\.minecraft` or the caller's `--minecraft-dir` override. The neutral public seeds are copied only when the destination file is missing.
