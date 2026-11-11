// Sandboxed preload: fixed operations only, with no generic IPC or Node access.
const {contextBridge, ipcRenderer} = require('electron');

contextBridge.exposeInMainWorld('launcherDesktop', Object.freeze({
  getAppInfo: () => ipcRenderer.invoke('launcher-desktop:info'),
  getUpdateState: () => ipcRenderer.invoke('launcher-desktop:update-state:get'),
  configureUpdateFeed: url => ipcRenderer.invoke('launcher-desktop:update-feed:set', url),
  checkForUpdates: () => ipcRenderer.invoke('launcher-desktop:update-check'),
  restartAndInstall: () => ipcRenderer.invoke('launcher-desktop:update-install'),
  onUpdateState: callback => {
    if (typeof callback !== 'function') throw new TypeError('Update state requires a callback.');
    const listener = (_event, state) => callback(state);
    ipcRenderer.on('launcher-desktop:update-state', listener);
    return () => ipcRenderer.removeListener('launcher-desktop:update-state', listener);
  },
}));
