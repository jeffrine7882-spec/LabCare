const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("labcare", {
  signIn: (email, password) => ipcRenderer.invoke("auth:signIn", { email, password }),
  signOut: () => ipcRenderer.invoke("auth:signOut"),
  getState: () => ipcRenderer.invoke("auth:state"),
  onState: (cb) => ipcRenderer.on("state", (e, s) => cb(s)),
});
