// Minimal bridge for the always-on-top alert bubble window.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("bubble", {
  onShow(cb) {
    ipcRenderer.on("bubble:show", (evt, text, soundId) => cb(text, soundId));
  },
  openApp() { ipcRenderer.send("bubble:open"); },
  dismiss() { ipcRenderer.send("bubble:dismiss"); },
});
