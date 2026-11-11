// Runs the shell through a binary named Minecraft.exe.
//
// Discord's game detection matches the running executable's name against its
// games database: the original launcher shows up as "Playing Minecraft"
// because its process is literally Minecraft.exe (the Store package's entry
// point). An unmodified `electron .` run is electron.exe, which Discord
// ignores. So before starting, this script keeps a hard link (copy as
// fallback) of the Electron binary named Minecraft.exe and launches the app
// through it. The window title, icon and userData path are set by main.js and
// package.json, so nothing else changes.
//
// The link must sit in node_modules/electron/dist next to electron.exe:
// Electron resolves icudt.dat, the .pak files and its other resources relative
// to its own executable, and a renamed copy in an empty folder dies at startup
// with "Invalid file descriptor to ICU data received".
//
//   npm start
//   npm start -- --minecraft-dir="C:\\Users\\me\\AppData\\Roaming\\.minecraft"
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

// Under plain Node, the electron package exports the path to its executable.
const electronExe = require("electron");
const target = path.join(path.dirname(electronExe), "Minecraft.exe");

// A hard link shares the file with the Electron binary, so it costs no extra
// bytes; the stat check covers the case where node_modules was reinstalled and
// the old link now points at a stale inode.
function linkIsCurrent() {
  try {
    const a = fs.statSync(electronExe);
    const b = fs.statSync(target);
    return a.size === b.size && Math.abs(a.mtimeMs - b.mtimeMs) < 2000;
  } catch (e) {
    return false;
  }
}

if (!linkIsCurrent()) {
  try {
    fs.unlinkSync(target);
  } catch (e) {
    /* first run */
  }
  try {
    fs.linkSync(electronExe, target);
  } catch (e) {
    // A different volume (or a filesystem without hard links) cannot link;
    // a plain copy is larger but works everywhere.
    fs.copyFileSync(electronExe, target);
  }
}

// `Minecraft.exe` is a renamed Electron binary, which Electron reports as a
// packaged app. The flag keeps main.js on the checkout paths it should use.
const child = spawn(target, [__dirname].concat(process.argv.slice(2)), {
  stdio: "inherit",
  windowsHide: false,
  env: Object.assign({}, process.env, { LAUNCHER_DEV: "1" }),
});
child.on("exit", (code, signal) => {
  process.exit(signal ? 1 : code || 0);
});
