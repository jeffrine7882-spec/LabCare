const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("labcare", {
  signIn: (email, password) => ipcRenderer.invoke("auth:signIn", { email, password }),
  signOut: () => ipcRenderer.invoke("auth:signOut"),
  getState: () => ipcRenderer.invoke("auth:state"),
  onState: (cb) => ipcRenderer.on("state", (e, s) => cb(s)),
  listNotifs: () => ipcRenderer.invoke("notif:list"),
  markRead: (id) => ipcRenderer.invoke("notif:markRead", id),
  getSound: () => ipcRenderer.invoke("sound:get"),
  setSound: (id) => ipcRenderer.invoke("sound:set", id),
  testSound: (id) => ipcRenderer.invoke("sound:test", id),
});
