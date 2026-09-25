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
let bubble = null;
let bubbleTimer = null;
let token = "";
let email = "";
let pollTimer = null;
let lastNotifId = null;
let enabled = true;
let sound = "chime"; // chime | bell | beep | alarm

const prefsPath = path.join(app.getPath("userData"), "config.json");
const SOUNDS = {
  chime: path.join(__dirname, "assets", "chime.wav"),
  bell: path.join(__dirname, "assets", "bell.wav"),
  beep: path.join(__dirname, "assets", "beep.wav"),
  alarm: path.join(__dirname, "assets", "alarm.wav"),
};

function loadPrefs() {
  try {
    const d = JSON.parse(fs.readFileSync(prefsPath, "utf8"));
    token = d.token || "";
    email = d.email || "";
    enabled = d.enabled !== false;
    sound = SOUNDS[d.sound] ? d.sound : "chime";
    lastNotifId = d.lastNotifId ?? null;
  } catch (e) {}
}

function savePrefs() {
  try {
    fs.writeFileSync(prefsPath, JSON.stringify({ token, email, enabled, sound, lastNotifId }));
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

// ---- floating alert bubble (always on top of the display, no focus theft) --
function createBubble() {
  const { screen } = require("electron");
  const area = screen.getPrimaryDisplay().workArea;
  bubble = new BrowserWindow({
    width: 360,
    height: 112,
    x: area.x + area.width - 372,
    y: area.y + 12,
    frame: false,
    resizable: false,
    maximizable: false,
    show: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    focusable: false,           // never steal focus from whatever the user is doing
    transparent: true,          // lets the rounded-corner bubble look work
    backgroundColor: "#00000000",
    webPreferences: {
      preload: path.join(__dirname, "bubble-preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  bubble.loadFile(path.join(__dirname, "bubble.html"));
  bubble.setAlwaysOnTop(true, "screen-saver"); // float above (almost) everything
  bubble.on("closed", () => { bubble = null; });
  bubble.webContents.on("did-finish-load", () => {
    if (bubble) bubble.webContents.send("bubble:primed");
  });
}

function showBubble(text) {
  try {
    if (!bubble) createBubble();
    const fire = () => {
      if (!bubble) return;
      bubble.webContents.send("bubble:show", String(text).slice(0, 180), sound);
      bubble.showInactive(); // appear without taking focus
      if (bubbleTimer) clearTimeout(bubbleTimer);
      bubbleTimer = setTimeout(() => { try { bubble && bubble.hide(); } catch (e) {} }, 10000);
    };
    if (bubble.webContents.isLoading()) {
      bubble.webContents.once("did-finish-load", fire);
    } else {
      fire();
    }
    return true;
  } catch (e) {
    return false;
  }
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
  // 1. Floating bubble on top of the display + sound (preferred path)
  const bubbleOk = showBubble(text);
  // 2. Windows notification centre entry as a durable breadcrumb
  if (Notification.isSupported()) {
    const n = new Notification({ title: "LabCare", body: String(text).slice(0, 180) });
    n.on("click", () => { openWebApp(); });
    n.show();
  }
  // 3. Sound fallback if the bubble window could not be created
  if (!bubbleOk) playSound(sound);
  // 4. flash tray tooltip
  tray.setToolTip("LabCare — new alert");
}

function playSound(soundId) {
  try {
    const file = SOUNDS[soundId] || SOUNDS.chime;
    const { exec } = require("child_process");
    const player = process.platform === "win32"
      ? `powershell -c (New-Object Media.SoundPlayer '${file}').PlaySync()`
      : `aplay "${file}"`;
    exec(player);
  } catch (e) {}
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

// ---- in-app notifications + sound ------------------------------------------
ipcMain.handle("notif:list", async () => {
  if (!token) return { ok: false, error: "Not signed in" };
  try {
    const res = await fetch(API_BASE + "/api/notifications", {
      headers: { Authorization: "Bearer " + token, Accept: "application/json" },
    });
    const data = await res.json();
    if (!res.ok) return { ok: false, error: data.error || "Failed" };
    return { ok: true, items: data.map((n) => ({
      id: n.id,
      text: n.text,
      entity_type: n.entity_type || "",
      entity_id: n.entity_id || 0,
      read: !!n.read,
      created_at: n.created_at || "",
    })) };
  } catch (e) {
    return { ok: false, error: "Cannot reach the LabCare server." };
  }
});

ipcMain.handle("notif:markRead", async (evt, id) => {
  if (!token) return { ok: false };
  try {
    await fetch(API_BASE + "/api/notifications/read", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
      body: JSON.stringify(id ? { id } : {}),
    });
    return { ok: true };
  } catch (e) {
    return { ok: false };
  }
});

ipcMain.handle("sound:get", () => sound);

ipcMain.handle("sound:set", (evt, id) => {
  if (SOUNDS[id]) { sound = id; savePrefs(); }
  return sound;
});

ipcMain.handle("sound:test", (evt, id) => {
  // play via the bubble preview so the user hears what an alert looks + sounds like
  const ok = showBubble("Test alert — this is how a new complaint or breakdown rings on this PC.");
  if (!ok) playSound(id || sound);
  return true;
});

ipcMain.on("bubble:open", () => {
  if (bubbleTimer) clearTimeout(bubbleTimer);
  if (bubble) try { bubble.hide(); } catch (e) {}
  openWebApp();
});

ipcMain.on("bubble:dismiss", () => {
  if (bubbleTimer) clearTimeout(bubbleTimer);
  if (bubble) try { bubble.hide(); } catch (e) {}
});

app.on("before-quit", () => {
  if (bubble) try { bubble.destroy(); } catch (e) {}
});
