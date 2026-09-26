/*
 * LabSynch Alerts — Windows desktop tray app.
 *
 * Sits in the system tray, auto-starts with Windows, and rings (sound +
 * native notification) for every new LabSynch bell notification. It polls the
 * same lightweight /api/notifications/ping the web app uses, so enabling
 * "Desktop alerts" in the web app is NOT required — this app works on its own.
 *
 * It also *is* a doorway to the LabSynch web app: the "Site" tab opens
 * https://labcare.insforge.site inside the app (already signed in when this
 * app holds a token), the tray menu and every alert bubble/notification open
 * it too, and nothing in here is allowed to leave the user on a blank window —
 * if the bundled UI cannot load we fall back to the real site, and if the site
 * cannot load we offer "Retry" + "Open the site in my browser".
 */
const { app, BrowserWindow, Tray, Menu, Notification, nativeImage, ipcMain, shell, session } = require("electron");
const path = require("path");
const fs = require("fs");

// The LabSynch web app — every "go to LabSynch" action lands here.
const SITE_URL = "https://labcare.insforge.site/";
const API_BASE = SITE_URL.replace(/\/+$/, "");
const SITE_ORIGIN = new URL(SITE_URL).origin;
const SITE_PARTITION = "persist:labcare-site";
const POLL_INTERVAL_MS = 10000;

let tray = null;
let win = null;
let siteWin = null;
let bubble = null;
let bubbleTimer = null;
let token = "";
let email = "";
let name = "";
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
    name = d.name || "";
    enabled = d.enabled !== false;
    sound = SOUNDS[d.sound] ? d.sound : "chime";
    lastNotifId = d.lastNotifId ?? null;
  } catch (e) {}
}

function savePrefs() {
  try {
    fs.writeFileSync(prefsPath, JSON.stringify({ token, email, name, enabled, sound, lastNotifId }));
  } catch (e) {}
}

function assetPath(fileName) {
  const p = path.join(__dirname, "assets", fileName);
  try { return fs.existsSync(p) ? p : null; } catch (e) { return null; }
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// ---- single instance -------------------------------------------------------
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => showWindow());

  app.whenReady().then(() => {
    loadPrefs();
    try { app.setAppUserModelId("com.insforge.labcare"); } catch (e) {}
    setAutoStart();
    // Each step is guarded: a missing tray icon must never stop the window
    // from opening (that is how the app used to end up invisible/blank).
    try { createTray(); } catch (e) { tray = null; }
    try { createWindow(); } catch (e) { openSiteInBrowser(); }
    if (token) schedulePolling();
  });

  app.on("window-all-closed", () => {
    // keep running in the tray
  });

  app.on("activate", () => showWindow());
}

function setAutoStart() {
  try {
    if (process.platform === "win32" || process.platform === "darwin") {
      app.setLoginItemSettings({ openAtLogin: true }); // auto-start with Windows
    }
  } catch (e) {}
}

// ---- tray ------------------------------------------------------------------
function createTray() {
  const iconFile = assetPath("tray.png");
  const icon = iconFile ? nativeImage.createFromPath(iconFile) : nativeImage.createEmpty();
  tray = new Tray(icon);
  tray.setToolTip("LabSynch Alerts");
  refreshMenu();
  tray.on("click", () => showWindow());
}

function refreshMenu() {
  if (!tray) return;
  try {
    const menu = Menu.buildFromTemplate([
      { label: "Open LabSynch site", click: () => openWebApp() },
      { label: "Open Alerts window", click: () => showWindow() },
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
      { label: "Quit", click: () => { app.isQuitting = true; app.quit(); } },
    ]);
    tray.setContextMenu(menu);
  } catch (e) {}
}

// ---- alerts window ---------------------------------------------------------
function createWindow() {
  if (win && !win.isDestroyed()) return win;
  win = new BrowserWindow({
    width: 400,
    height: 620,
    resizable: false,
    show: false,
    title: "LabSynch Alerts",
    backgroundColor: "#f3f5f9",
    icon: assetPath("icon.png") || undefined,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  loadAppUi();

  // The window used to be created hidden with nothing ever showing it, so the
  // installed app could look dead. Show it as soon as it can paint, and again
  // on a failsafe timer in case ready-to-show never fires.
  win.once("ready-to-show", () => { try { win.show(); } catch (e) {} });
  setTimeout(() => {
    try { if (win && !win.isDestroyed() && !win.isVisible()) win.show(); } catch (e) {}
  }, 2500);

  // Never leave the user on a blank window: any load/render failure falls back
  // to an explanatory page that still links to the LabSynch site.
  win.webContents.on("did-fail-load", (e, code, desc, url, isMainFrame) => {
    if (!isMainFrame || code === -3) return; // -3 = aborted (harmless)
    showLoadFailure(win, "LabSynch Alerts couldn't open", desc || ("error " + code));
  });
  win.webContents.on("render-process-gone", () => reloadAppUi());
  win.webContents.on("unresponsive", () => reloadAppUi());

  // Keep the alerts window on its own UI — links go to the site window/browser.
  win.webContents.setWindowOpenHandler(({ url }) => { routeExternal(url); return { action: "deny" }; });
  win.webContents.on("will-navigate", (e, url) => {
    const from = win && !win.isDestroyed() ? win.webContents.getURL() : "";
    if (from.startsWith("file://") && !url.startsWith("file://")) {
      e.preventDefault();
      routeExternal(url);
    }
  });

  win.on("close", (e) => {
    if (!app.isQuitting) {
      e.preventDefault();
      try { win.hide(); } catch (err) {}
    }
  });
  win.on("closed", () => { win = null; });
  return win;
}

/** Load the bundled UI, or the real site if the bundle is missing/unreadable. */
function loadAppUi() {
  if (!win || win.isDestroyed()) return;
  const page = path.join(__dirname, "renderer", "index.html");
  let ok = false;
  try { ok = fs.existsSync(page); } catch (e) { ok = false; }
  if (ok) {
    win.loadFile(page).catch(() => win.loadURL(SITE_URL).catch(() => {}));
  } else {
    // Last resort: show the LabSynch site itself rather than a blank window.
    win.loadURL(SITE_URL).catch(() => {});
  }
}

/**
 * Re-load the UI after a crash/hang, but never in a tight loop: if the bundled
 * UI keeps dying, hand the window to the real site instead.
 */
let uiReloads = 0;
function reloadAppUi() {
  if (!win || win.isDestroyed()) return;
  if (++uiReloads > 2) {
    try { win.loadURL(SITE_URL).catch(() => {}); } catch (e) {}
    return;
  }
  loadAppUi();
}

function showWindow() {
  try {
    if (!win || win.isDestroyed()) createWindow();
    win.show();
    win.focus();
  } catch (e) {}
}

function signOut() {
  token = "";
  email = "";
  name = "";
  savePrefs();
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
  refreshMenu();
  // Drop the site session too, so the Site tab shows the signed-out site.
  syncSiteSession().then(() => {
    if (siteWin && !siteWin.isDestroyed()) siteWin.loadURL(SITE_URL).catch(() => {});
  });
  if (win && !win.isDestroyed()) win.webContents.send("state", { signedIn: false, email: "", name: "" });
  showWindow();
}

// ---- LabSynch site window (the "Site" tab) ----------------------------------
/**
 * Mirror this app's token into the site's own session before loading it. The
 * server accepts the HttpOnly `labcare_token` cookie, and the web app always
 * calls /api/me on boot, so the site opens already signed in.
 */
async function syncSiteSession() {
  try {
    const ses = session.fromPartition(SITE_PARTITION);
    if (token) {
      await ses.cookies.set({
        url: SITE_ORIGIN,
        name: "labcare_token",
        value: token,
        path: "/",
        secure: SITE_ORIGIN.startsWith("https:"),
        httpOnly: true,
        sameSite: "lax",
        expirationDate: Math.floor(Date.now() / 1000) + 30 * 24 * 3600,
      });
    } else {
      try { await ses.cookies.remove(SITE_ORIGIN, "labcare_token"); } catch (e) {}
    }
    // Service reports and other files download to the user's Downloads folder.
    ses.off("will-download", onSiteDownload);
    ses.on("will-download", onSiteDownload);
  } catch (e) {}
}

function onSiteDownload(event, item) {
  try {
    const dir = app.getPath("downloads");
    const base = String(item.getFilename() || "labcare-download").replace(/[\\/:*?"<>|]/g, "_");
    const ext = path.extname(base);
    const stem = path.basename(base, ext) || "labcare-download";
    let target = path.join(dir, base);
    for (let i = 1; fs.existsSync(target) && i < 500; i++) {
      target = path.join(dir, `${stem} (${i})${ext}`);
    }
    item.setSavePath(target);
  } catch (e) {}
}

function isSiteUrl(url) {
  try { return new URL(url).origin === SITE_ORIGIN; } catch (e) { return false; }
}

/** Open (or re-focus) the LabSynch web app inside this app. */
async function openSite(targetUrl) {
  const url = (typeof targetUrl === "string" && /^https?:/i.test(targetUrl)) ? targetUrl : SITE_URL;
  await syncSiteSession();

  if (siteWin && !siteWin.isDestroyed()) {
    // Only reload when we are sitting on the built-in error page — otherwise
    // keep the user exactly where they were in the app.
    const current = siteWin.webContents.getURL();
    if (!current || current.startsWith("file://")) siteWin.loadURL(SITE_URL).catch(() => {});
    siteWin.show();
    siteWin.focus();
    return siteWin;
  }

  siteWin = new BrowserWindow({
    width: 1180,
    height: 840,
    minWidth: 380,
    minHeight: 560,
    title: "LabSynch",
    show: false,
    backgroundColor: "#f3f5f9",
    autoHideMenuBar: true,
    icon: assetPath("icon.png") || undefined,
    webPreferences: {
      partition: SITE_PARTITION,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
      // the site is a PWA that plays its own alert sounds
      autoplayPolicy: "no-user-gesture-required",
    },
  });

  siteWin.loadURL(url).catch(() => {});
  siteWin.once("ready-to-show", () => { try { siteWin.show(); } catch (e) {} });
  setTimeout(() => {
    try { if (siteWin && !siteWin.isDestroyed() && !siteWin.isVisible()) siteWin.show(); } catch (e) {}
  }, 4000);

  // Never strand the user on a blank page: explain + offer the browser.
  siteWin.webContents.on("did-fail-load", (e, code, desc, failedUrl, isMainFrame) => {
    if (!isMainFrame || code === -3) return;
    showLoadFailure(siteWin, "The LabSynch site didn't open", desc || ("error " + code), failedUrl);
  });
  siteWin.webContents.on("render-process-gone", () => {
    try { siteWin && !siteWin.isDestroyed() && siteWin.loadURL(SITE_URL).catch(() => {}); } catch (e) {}
  });

  siteWin.webContents.setWindowOpenHandler(({ url: u }) => { routeExternal(u); return { action: "deny" }; });
  siteWin.webContents.on("will-navigate", (e, navUrl) => {
    if (/^labcare:\/\/open-external/i.test(navUrl)) {
      e.preventDefault();
      openSiteInBrowser();
      return;
    }
    if (!/^https?:/i.test(navUrl) || !isSiteUrl(navUrl)) {
      e.preventDefault();
      routeExternal(navUrl);
    }
  });

  siteWin.on("closed", () => { siteWin = null; });
  return siteWin;
}

function openSiteInBrowser() {
  try { shell.openExternal(SITE_URL); } catch (e) {}
}

/** Every "open LabSynch" click: in-app site window, browser as the fallback. */
function openWebApp(targetUrl) {
  try {
    const p = openSite(targetUrl);
    if (p && typeof p.catch === "function") p.catch(() => openSiteInBrowser());
  } catch (e) {
    openSiteInBrowser();
  }
}

/** Route a link out of the app: our pseudo-scheme, the site, or the browser. */
function routeExternal(url) {
  if (!url) return;
  if (/^labcare:\/\/open-external/i.test(url)) { openSiteInBrowser(); return; }
  if (isSiteUrl(url)) { openWebApp(url); return; }
  if (/^https?:/i.test(url)) { try { shell.openExternal(url); } catch (e) {} }
}

// ---- "it didn't load" fallback page ---------------------------------------
/**
 * Written to userData (top-level data: URLs are blocked by Chromium) so a
 * failed load still shows a real page with a working path to the LabSynch site.
 */
function showLoadFailure(target, title, message, retryUrl) {
  try {
    if (!target || target.isDestroyed()) return;
    const dir = app.getPath("userData");
    fs.mkdirSync(dir, { recursive: true });
    const file = path.join(dir, "load-error.html");
    const retry = (typeof retryUrl === "string" && /^https?:/i.test(retryUrl)) ? retryUrl : SITE_URL;
    fs.writeFileSync(file, `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>LabSynch</title>
<style>
  * { box-sizing: border-box; }
  body { margin:0; background:#f3f5f9; color:#111827;
         font:15px/1.55 -apple-system,"Segoe UI",Roboto,sans-serif; }
  .box { max-width:520px; margin:0 auto; padding:44px 26px; text-align:center; }
  .logo { font-size:40px; }
  h1 { font-size:20px; margin:12px 0 6px; color:#0f766e; }
  p { color:#6b7280; font-size:14px; margin:0 0 20px; word-break:break-word; }
  a.btn { display:block; padding:13px; border-radius:12px; font-weight:700;
          font-size:14px; text-decoration:none; margin-bottom:10px; }
  a.primary { background:#0f766e; color:#fff; }
  a.ghost { background:#e2f2f0; color:#115e59; }
  .url { font-size:12px; color:#94a3b8; margin-top:14px; word-break:break-all; }
</style></head>
<body><div class="box">
  <div class="logo">🔬</div>
  <h1>${esc(title)}</h1>
  <p>${esc(message)}<br>Check your internet connection, then try again.</p>
  <a class="btn primary" href="${esc(SITE_URL)}">Retry</a>
  <a class="btn ghost" href="labcare://open-external">Open the site in my browser</a>
  <div class="url">${esc(retry)}</div>
</div></body></html>`, "utf8");
    target.loadFile(file).catch(() => { try { target.loadURL(SITE_URL); } catch (e) {} });
  } catch (e) {
    try { target.loadURL(SITE_URL); } catch (err) {}
  }
}

// ---- floating alert bubble (always on top of the display, no focus theft) --
function bubbleFilesPresent() {
  // These live next to main.js and MUST be in build.files, otherwise the
  // packaged app has no bubble UI and the alert cannot link to the site.
  try {
    return fs.existsSync(path.join(__dirname, "bubble.html")) &&
           fs.existsSync(path.join(__dirname, "bubble-preload.js"));
  } catch (e) { return false; }
}

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
      autoplayPolicy: "no-user-gesture-required", // the alert sound must play
    },
  });
  bubble.loadFile(path.join(__dirname, "bubble.html")).catch(() => {
    try { bubble && !bubble.isDestroyed() && bubble.destroy(); } catch (e) {}
    bubble = null;
  });
  bubble.setAlwaysOnTop(true, "screen-saver"); // float above (almost) everything
  bubble.on("closed", () => { bubble = null; });
  bubble.webContents.on("did-fail-load", () => {
    try { bubble && !bubble.isDestroyed() && bubble.destroy(); } catch (e) {}
    bubble = null;
  });
  bubble.webContents.on("did-finish-load", () => {
    if (bubble && !bubble.isDestroyed()) bubble.webContents.send("bubble:primed");
  });
}

function showBubble(text) {
  try {
    if (!bubbleFilesPresent()) return false; // caller falls back to playSound()
    if (!bubble || bubble.isDestroyed()) createBubble();
    if (!bubble) return false;
    const fire = () => {
      if (!bubble || bubble.isDestroyed()) return;
      bubble.webContents.send("bubble:show", String(text).slice(0, 180), sound);
      bubble.showInactive(); // appear without taking focus
      if (bubbleTimer) clearTimeout(bubbleTimer);
      bubbleTimer = setTimeout(() => { try { bubble && !bubble.isDestroyed() && bubble.hide(); } catch (e) {} }, 10000);
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
        ring(latest.text || "New LabSynch alert");
      }
    }
  } catch (e) {}
}

function ring(text) {
  // 1. Floating bubble on top of the display + sound (preferred path)
  const bubbleOk = showBubble(text);
  // 2. Windows notification centre entry as a durable breadcrumb
  try {
    if (Notification.isSupported()) {
      const n = new Notification({
        title: "LabSynch",
        body: String(text).slice(0, 180),
        icon: assetPath("icon.png") || undefined,
      });
      n.on("click", () => { openWebApp(); });
      n.show();
    }
  } catch (e) {}
  // 3. Sound fallback if the bubble window could not be created
  if (!bubbleOk) playSound(sound);
  // 4. flash tray tooltip
  try {
    if (tray) {
      tray.setToolTip("LabSynch — new alert");
      setTimeout(() => { try { tray && tray.setToolTip("LabSynch Alerts"); } catch (e) {} }, 8000);
    }
  } catch (e) {}
}

/**
 * The bundled .wav files live inside app.asar when installed, which only
 * Electron/Node can read — an external player cannot. Copy the sound out to
 * userData first so the fallback path actually makes a noise.
 */
function playablePath(soundId) {
  const src = SOUNDS[soundId] || SOUNDS.chime;
  try {
    if (!src.split(path.sep).includes("app.asar")) return src; // running from source
    const dir = path.join(app.getPath("userData"), "sounds");
    fs.mkdirSync(dir, { recursive: true });
    const dest = path.join(dir, path.basename(src));
    if (!fs.existsSync(dest) || fs.statSync(dest).size !== fs.statSync(src).size) {
      fs.copyFileSync(src, dest);
    }
    return dest;
  } catch (e) {
    return src;
  }
}

function playSound(soundId) {
  try {
    const file = playablePath(soundId);
    const { execFile } = require("child_process");
    const opts = { windowsHide: true };
    if (process.platform === "win32") {
      // execFile (not exec) so the path needs no shell quoting
      execFile("powershell.exe", [
        "-NoProfile", "-NonInteractive", "-Command",
        `(New-Object Media.SoundPlayer '${String(file).replace(/'/g, "''")}').PlaySync()`,
      ], opts, () => {});
    } else if (process.platform === "darwin") {
      execFile("afplay", [file], opts, () => {});
    } else {
      execFile("aplay", [file], opts, () => {});
    }
    return true;
  } catch (e) {
    return false;
  }
}

// ---- auth / signal bridge --------------------------------------------------
ipcMain.handle("auth:signIn", async (evt, credentials) => {
  try {
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
    name = (data.user && data.user.name) || "";
    lastNotifId = null;
    savePrefs();
    schedulePolling();
    refreshMenu();
    syncSiteSession(); // so the Site tab opens signed in straight away
    return { ok: true, name };
  } catch (e) {
    return { ok: false, pending: false, error: "Cannot reach the LabSynch server." };
  }
});

ipcMain.handle("auth:signOut", () => {
  signOut();
  return { ok: true };
});

ipcMain.handle("auth:state", () => ({ signedIn: !!token, email, name }));

// ---- site bridge -----------------------------------------------------------
ipcMain.handle("site:open", async (evt, url) => {
  try { await openSite(url); return { ok: true, url: SITE_URL }; }
  catch (e) { openSiteInBrowser(); return { ok: false, url: SITE_URL }; }
});

ipcMain.handle("site:openExternal", () => {
  openSiteInBrowser();
  return { ok: true, url: SITE_URL };
});

ipcMain.handle("app:info", () => ({
  version: app.getVersion(),
  siteUrl: SITE_URL,
  signedIn: !!token,
}));

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
    return { ok: false, error: "Cannot reach the LabSynch server." };
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
  // Without this the alerts window's close handler hides instead of closing
  // and "Quit" from the tray never actually exits.
  app.isQuitting = true;
  if (bubble) try { bubble.destroy(); } catch (e) {}
});
