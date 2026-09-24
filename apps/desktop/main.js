/*
 * LabCare Alerts — Windows desktop tray app.
 *
 * Sits in the system tray, auto-starts with Windows, and rings (sound +
 * native notification) for every new LabCare bell notification. It polls the
 * same lightweight /api/notifications/ping the web app uses, so enabling
 * "Desktop alerts" in the web app is NOT required — this app works on its own.
 *
 * The notification itself opens the LabCare web app when clicked.
 */
const { app, BrowserWindow, Tray, Menu, Notification, nativeImage, ipcMain } = require("electron");
const path = require("path");
const fs = require("fs");

const API_BASE = "https://labcare.insforge.site";
const POLL_INTERVAL_MS = 10000;

let tray = null;
let win = null;
let token = "";
let email = "";
let pollTimer = null;
let lastNotifId = null;
let enabled = true;

const prefsPath = path.join(app.getPath("userData"), "config.json");
const chimePath = path.join(__dirname, "assets", "chime.wav");

function loadPrefs() {
  try {
    const d = JSON.parse(fs.readFileSync(prefsPath, "utf8"));
    token = d.token || "";
    email = d.email || "";
    enabled = d.enabled !== false;
    lastNotifId = d.lastNotifId ?? null;
  } catch (e) {}
}

function savePrefs() {
  try {
    fs.writeFileSync(prefsPath, JSON.stringify({ token, email, enabled, lastNotifId }));
  } catch (e) {}
}

// ---- single instance -------------------------------------------------------
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => showWindow());

  app.whenReady().then(() => {
    loadPrefs();
    app.setAppUserModelId("com.insforge.labcare");
    createTray();
    createWindow();
    if (token) schedulePolling();
  });

  app.on("window-all-closed", (e) => {
    // keep running in the tray
  });

  app.setLoginItemSettings({ openAtLogin: true }); // auto-start with Windows
}

// ---- tray ------------------------------------------------------------------
function createTray() {
  const icon = nativeImage.createFromDataURL(
    `data:image/png;base64,${fs.readFileSync(path.join(__dirname, "assets", "tray.png")).toString("base64")}`
  );
  tray = new Tray(icon);
  tray.setToolTip("LabCare Alerts");
  refreshMenu();
  tray.on("click", () => showWindow());
}

function refreshMenu() {
  const menu = Menu.buildFromTemplate([
    { label: "Open LabCare", click: showWindow },
    { type: "separator" },
    {
      label: enabled ? "Alerts: ON" : "Alerts: OFF",
      click: () => {
        enabled = !enabled;
        savePrefs();
        refreshMenu();
        if (enabled && token) schedulePolling();
      },
    },
    { label: "Sign out", click: signOut },
    { type: "separator" },
    { label: "Quit", click: () => app.quit() },
  ]);
  tray.setContextMenu(menu);
}

// ---- window ----------------------------------------------------------------
function createWindow() {
  win = new BrowserWindow({
    width: 380,
    height: 560,
    resizable: false,
    show: false,
      icon: path.join(__dirname, "assets", "icon.png"),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.loadFile(path.join(__dirname, "renderer", "index.html"));
  win.on("close", (e) => {
    if (!app.isQuitting) {
      e.preventDefault();
      win.hide();
    }
  });
}

function showWindow() {
  if (!win) createWindow();
  win.show();
  win.focus();
}

function signOut() {
  token = "";
  email = "";
  savePrefs();
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
  refreshMenu();
  if (win) win.webContents.send("state", { signedIn: false, email: "" });
  showWindow();
}

// ---- polling + alerts ------------------------------------------------------
function schedulePolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
  pollOnce();
}

async function pollOnce() {
  if (!enabled || !token) return;
  try {
    const res = await fetch(API_BASE + "/api/notifications/ping", {
      headers: { Authorization: "Bearer " + token, Accept: "application/json" },
    });
    if (!res.ok) {
      if (res.status === 401) { signOut(); return; }
      return;
    }
    const body = await res.json();
    const latest = body.latest;
    if (!latest) return;
    if (lastNotifId == null) { lastNotifId = latest.id; savePrefs(); return; }
    if (latest.id !== lastNotifId) {
      lastNotifId = latest.id;
      savePrefs();
      if (body.unread > 0) {
        ring(latest.text || "New LabCare alert");
      }
    }
  } catch (e) {}
}

async function ring(text) {
  // 1. Windows notification (with sound)
  if (Notification.isSupported()) {
    const n = new Notification({ title: "LabCare", body: String(text).slice(0, 180) });
    n.on("click", () => { openWebApp(); });
    n.show();
  }
  // 2. Also play our custom chime for a reliable audible ring
  try {
    const { exec } = require("child_process");
    const player = process.platform === "win32"
      ? `powershell -c (New-Object Media.SoundPlayer '${chimePath}').PlaySync()`
      : `aplay "${chimePath}"`;
    exec(player);
  } catch (e) {}
  // 3. flash tray / update menu badge text
  tray.setToolTip("LabCare — new alert");
}

function openWebApp() {
  const { shell } = require("electron");
  shell.openExternal(API_BASE);
}

// ---- auth / signal bridge --------------------------------------------------
ipcMain.handle("auth:signIn", async (evt, credentials) => {  try {
    const res = await fetch(API_BASE + "/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(credentials),
    });
    const data = await res.json();
    if (!res.ok) {
      return {
        ok: false,
        pending: /approv/i.test(String(data.error || "")),
        error: data.error || "Sign in failed (" + res.status + ")",
      };
    }
    token = data.token;
    email = credentials.email;
    lastNotifId = null;
    savePrefs();
    schedulePolling();
    refreshMenu();
    return { ok: true, name: data.user && data.user.name };
  } catch (e) {
    return { ok: false, pending: false, error: "Cannot reach the LabCare server." };
  }
});

ipcMain.handle("auth:signOut", () => {
  signOut();
  return { ok: true };
});

ipcMain.handle("auth:state", () => ({ signedIn: !!token, email }));
