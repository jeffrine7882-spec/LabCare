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
  // --- the LabCare web app -------------------------------------------------
  // The site URL is also exposed so the UI can render a real <a> fallback that
  // works even if an IPC call fails — the window must never be a dead end.
  openSite: (url) => ipcRenderer.invoke("site:open", url),
  openSiteExternal: () => ipcRenderer.invoke("site:openExternal"),
  appInfo: () => ipcRenderer.invoke("app:info"),
  siteUrl: "https://labcare.insforge.site/",
});
