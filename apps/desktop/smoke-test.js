#!/usr/bin/env node
/*
 * Behavioural smoke test for the LabSynch desktop app — runs main.js against a
 * mock Electron in plain Node, so it needs no Electron binary, no display and
 * no network. `npm test` / the release workflow run it before packaging.
 *
 * It exists because the 1.1.0 installer shipped an app that could show a blank
 * window and had no working link to the LabSynch site, and nothing caught it.
 * These assertions pin the behaviour the user actually depends on:
 *
 *   1. the alerts window is really shown at launch (it used to be created
 *      hidden with nothing ever calling show()),
 *   2. the bundled UI loads,
 *   3. the Site action opens https://labcare.insforge.site/,
 *   4. a failed load lands on a recovery page that still links to the site,
 *   5. the tray offers "Open LabSynch site",
 *   6. Quit actually quits (the close handler used to swallow it),
 *   7. an alert still rings when the bubble UI is unavailable,
 *   8. ringing-with-the-app-closed: a per-minute watchdog Scheduled Task
 *      relaunches the exe hidden, auto-start uses --hidden, a hidden
 *      relaunch never pops the window, a --hidden launch shows no window,
 *   9. Quit asks for confirmation and "Keep alerts running" keeps running,
 *  10. the sign-in is kept until the user signs out: outages, network errors,
 *      proxy 401s and a lone API 401 never sign the user out; only a session
 *      the API repeatedly confirms as gone does (with a notification), a
 *      manual sign-out also ends the session on the server, and a sign-in
 *      made inside the Site tab is adopted by the app.
 */
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const Module = require("module");

const APP_DIR = __dirname;
const MAIN = path.join(APP_DIR, "main.js");
const SITE_URL = "https://labcare.insforge.site/";

let failures = 0;
function check(name, cond, extra) {
  if (cond) {
    console.log("  \u2714 " + name);
  } else {
    failures++;
    console.error("  \u2716 " + name + (extra ? "\n      " + extra : ""));
  }
}

// ---------------------------------------------------------------------------
// Mock Electron
// ---------------------------------------------------------------------------
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "labcare-desktop-test-"));
const ipcHandlers = {};
const ipcOn = {};
const windows = [];
const menuTemplates = [];
let shellOpened = [];
let soundPlayed = [];
let notifications = [];
let dialogCalls = [];
let dialogResponse = 0; // what the mock "user" picks in the quit dialog
let loginItemCalls = [];

class WebContents {
  constructor(owner) {
    this.owner = owner;
    this.events = {};
    this.openHandler = null;
    this.loads = [];
    this.currentUrl = "";
  }
  on(ev, cb) { (this.events[ev] = this.events[ev] || []).push(cb); return this; }
  once(ev, cb) { return this.on(ev, cb); }
  off(ev, cb) {
    if (!this.events[ev]) return this;
    this.events[ev] = this.events[ev].filter((f) => f !== cb);
    return this;
  }
  emit(ev, ...args) {
    for (const cb of (this.events[ev] || []).slice()) cb({ preventDefault() { this.defaultPrevented = true; } }, ...args);
    return this;
  }
  setWindowOpenHandler(h) { this.openHandler = h; }
  // Real Electron emits ready-to-show once the renderer can paint; the app
  // relies on it to reveal the window, so the mock must too.
  loadFile(p) {
    this.loads.push({ kind: "file", value: p });
    this.currentUrl = "file://" + p;
    setTimeout(() => this.owner.emitWindow("ready-to-show"), 0);
    return Promise.resolve();
  }
  loadURL(u) {
    this.loads.push({ kind: "url", value: u });
    this.currentUrl = u;
    setTimeout(() => this.owner.emitWindow("ready-to-show"), 0);
    return Promise.resolve();
  }
  getURL() { return this.currentUrl; }
  isLoading() { return false; }
  isDestroyed() { return false; }
  send(channel, payload) { (this.sends = this.sends || []).push({ channel, payload }); }
  executeJavaScript() { return Promise.resolve(); }
  lastLoad() { return this.loads[this.loads.length - 1] || null; }
  loadedAnything(v) { return this.loads.some((l) => l.value === v || l.value.includes(v)); }
}

class BrowserWindow {
  constructor(opts) {
    this.opts = opts || {};
    this.webContents = new WebContents(this);
    this.visible = false;
    this.destroyed = false;
    this.closedPrevented = false;
    this.events = {};
    windows.push(this);
  }
  on(ev, cb) { (this.events[ev] = this.events[ev] || []).push(cb); return this; }
  once(ev, cb) { return this.on(ev, cb); }
  emitWindow(ev, e) {
    for (const cb of (this.events[ev] || []).slice()) cb(e || {});
  }
  show() { this.visible = true; }
  showInactive() { this.visible = true; }
  hide() { this.visible = false; }
  focus() {}
  isVisible() { return this.visible; }
  isDestroyed() { return this.destroyed; }
  destroy() { this.destroyed = true; }
  setAlwaysOnTop() {}
  loadFile(p) { return this.webContents.loadFile(p); }
  loadURL(u) { return this.webContents.loadURL(u); }
}

const app = {
  isQuitting: false,
  isPackaged: true, // so the watchdog-task registration runs in the test
  _readyCbs: [],
  requestSingleInstanceLock: () => true,
  whenReady: () => Promise.resolve(),
  on: (ev, cb) => { (app._evs[ev] = app._evs[ev] || []).push(cb); },
  _evs: {},
  emit(ev, ...a) { for (const cb of (app._evs[ev] || []).slice()) cb(...a); },
  quit: () => { app.emit("before-quit"); app.quitCalled = (app.quitCalled || 0) + 1; },
  setAppUserModelId: () => {},
  setLoginItemSettings: (s) => { loginItemCalls.push(s); },
  getVersion: () => require(path.join(APP_DIR, "package.json")).version,
  getPath: (k) => path.join(tmp, k),
};

// One persistent site partition, like Electron's persist:labcare-site.
const cookieJar = {};
const cookieListeners = { changed: [] };
const siteSession = {
  cookies: {
    set: async (c) => { cookieJar[c.name] = c; },
    remove: async (url, name) => { delete cookieJar[name]; },
    get: async ({ name } = {}) => Object.values(cookieJar).filter((c) => !name || c.name === name),
    on: (ev, cb) => { (cookieListeners[ev] = cookieListeners[ev] || []).push(cb); },
    off: (ev, cb) => { cookieListeners[ev] = (cookieListeners[ev] || []).filter((f) => f !== cb); },
    // what Chromium does when the site's /api/login answers with Set-Cookie
    emitSet(name, value) {
      cookieJar[name] = { name, value };
      for (const cb of (cookieListeners.changed || []).slice()) cb({}, { name, value }, "explicit", false);
    },
  },
  on: () => {}, off: () => {},
};

const mockElectron = {
  app,
  BrowserWindow,
  Tray: class {
    constructor(icon) { this.icon = icon; this.tooltip = ""; this.menu = null; }
    setToolTip(t) { this.tooltip = t; }
    setContextMenu(m) { this.menu = m; }
    on() {}
  },
  Menu: { buildFromTemplate: (t) => { menuTemplates.push(t); return { items: t }; } },
  Notification: class {
    constructor(o) { this.opts = o; notifications.push(this); }
    static isSupported() { return true; }
    on() {}
    show() { this.shown = true; }
  },
  nativeImage: {
    createFromPath: () => ({ empty: false }),
    createEmpty: () => ({ empty: true }),
    createFromDataURL: () => ({ empty: false }),
  },
  shell: { openExternal: (u) => { shellOpened.push(u); return Promise.resolve(); } },
  dialog: {
    showMessageBox: (opts) => {
      dialogCalls.push(opts);
      return Promise.resolve({ response: dialogResponse, checkboxChecked: false });
    },
  },
  session: {
    fromPartition: () => siteSession,
  },
  screen: { getPrimaryDisplay: () => ({ workArea: { x: 0, y: 0, width: 1920, height: 1080 } }) },
  ipcMain: {
    handle: (ch, cb) => { ipcHandlers[ch] = cb; },
    on: (ch, cb) => { ipcOn[ch] = cb; },
  },
};

// Intercept require("electron") from main.js (and its lazy inner requires).
const originalLoad = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === "electron") return mockElectron;
  if (request === "child_process") {
    return { execFile: (cmd, args) => { soundPlayed.push({ cmd, args }); }, exec: () => {} };
  }
  return originalLoad.apply(this, arguments);
};

// ---------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------
(async () => {
  console.log("LabSynch desktop smoke test\n");

  // Run the Windows code paths — the EXE is what ships. The mock Electron and
  // the intercepted child_process make this safe on any OS the test runs on.
  Object.defineProperty(process, "platform", { value: "win32", configurable: true });

  require(MAIN);
  // let app.whenReady().then(...) and its timers settle
  await new Promise((r) => setTimeout(r, 60));

  const main = windows[0];

  console.log("launch");
  check("an alerts window is created at launch", !!main);
  check("the window is actually shown (not left blank/hidden)", !!main && main.isVisible(),
    "createWindow() used show:false with no ready-to-show handler");
  check("the bundled UI is loaded",
    !!main && main.webContents.loadedAnything(path.join("renderer", "index.html")),
    "loads: " + JSON.stringify(main && main.webContents.loads));

  console.log("\ntray");
  const trayMenu = menuTemplates[0] || [];
  const labels = trayMenu.map((i) => i.label);
  check("tray offers 'Open LabSynch site'", labels.includes("Open LabSynch site"), "labels: " + labels.join(", "));
  check("tray still offers the alerts window", labels.includes("Open Alerts window"));

  console.log("\nsite link");
  check("site:open IPC is registered", typeof ipcHandlers["site:open"] === "function");
  const siteResult = ipcHandlers["site:open"] ? await ipcHandlers["site:open"]({}, undefined) : null;
  await new Promise((r) => setTimeout(r, 30));
  const siteWin = windows.find((w) => w !== main && w.webContents.loadedAnything(SITE_URL));
  check("the Site action opens " + SITE_URL, !!siteWin,
    "windows loaded: " + JSON.stringify(windows.map((w) => w.webContents.loads)));
  check("the site window is shown", !!siteWin && siteWin.isVisible());
  check("site:open reports success", siteResult && siteResult.ok === true);

  console.log("\nfailed load -> recovery page (never blank)");
  main.webContents.emit("did-fail-load", -105, "ERR_NAME_NOT_RESOLVED", SITE_URL, true);
  await new Promise((r) => setTimeout(r, 30));
  const last = main.webContents.lastLoad();
  const errPage = last && last.kind === "file" ? last.value : null;
  check("the main window loads a recovery page", !!errPage, "last load: " + JSON.stringify(last));
  if (errPage) {
    const html = fs.readFileSync(errPage, "utf8");
    check("the recovery page links to the LabSynch site", html.includes(SITE_URL));
    check("the recovery page offers the browser fallback",
      html.includes("labcare://open-external") && /Open the site in my browser/.test(html));
    check("the recovery page offers Retry", /Retry/.test(html));
  }

  console.log("\nring even when the app is closed");
  const watchdog = soundPlayed.find((s) => s.cmd === "schtasks");
  check("a watchdog Scheduled Task is registered (schtasks)",
    !!watchdog && watchdog.args.includes("LabSynch Alerts Watchdog"),
    "schtasks calls: " + JSON.stringify(soundPlayed.filter((s) => s.cmd === "schtasks")));
  check("the watchdog task re-launches every minute",
    !!watchdog && watchdog.args.includes("/SC") && watchdog.args.includes("MINUTE"));
  check("the watchdog relaunch is hidden (--hidden), so no window pops up",
    !!watchdog && watchdog.args.some((a) => typeof a === "string" && a.includes("--hidden")));
  check("auto-start logs in with Windows — hidden, straight to the tray",
    loginItemCalls.some((s) => s.openAtLogin === true
      && Array.isArray(s.args) && s.args.includes("--hidden")),
    "setLoginItemSettings calls: " + JSON.stringify(loginItemCalls));

  console.log("\nhidden relaunch must not steal focus");
  main.hide();
  app.emit("second-instance", {},
    ["C:\\Users\\lab\\AppData\\Local\\LabSynch Alerts\\LabSynch Alerts.exe", "--hidden"],
    "C:\\");
  await new Promise((r) => setTimeout(r, 10));
  check("a --hidden relaunch leaves the window hidden", !main.isVisible());
  app.emit("second-instance", {},
    ["C:\\Users\\lab\\AppData\\Local\\LabSynch Alerts\\LabSynch Alerts.exe"],
    "C:\\");
  await new Promise((r) => setTimeout(r, 10));
  check("a human relaunch (no --hidden) shows the window", main.isVisible());

  console.log("\nlaunched hidden (auto-start / watchdog)");
  process.argv.push("--hidden");
  delete require.cache[require.resolve(MAIN)];
  require(MAIN); // second instance of main.js, this time started hidden
  await new Promise((r) => setTimeout(r, 60));
  const hiddenWin = windows[windows.length - 1];
  check("a --hidden launch still creates the alerts window (on standby in the tray)", !!hiddenWin);
  check("a --hidden launch shows no window", !!hiddenWin && !hiddenWin.isVisible(),
    "the login item / watchdog must start in the tray, not throw a window at boot");

  console.log("\nquit asks first");
  app.isQuitting = false;
  const quitsBefore = app.quitCalled || 0;
  const lastMenu = menuTemplates[menuTemplates.length - 1] || [];
  const quitItem = lastMenu.find((i) => i.label === "Quit");
  check("the tray still offers Quit", !!quitItem);
  dialogResponse = 0; // "Keep alerts running"
  if (quitItem) quitItem.click();
  await new Promise((r) => setTimeout(r, 20));
  check("'Keep alerts running' (the default) does not quit",
    (app.quitCalled || 0) === quitsBefore);
  check("the quit dialog explains what silences the PC",
    dialogCalls.some((c) => /watchdog/i.test(String(c.detail || ""))));
  dialogResponse = 1; // "Quit anyway"
  if (quitItem) quitItem.click();
  await new Promise((r) => setTimeout(r, 20));
  check("'Quit anyway' really quits", (app.quitCalled || 0) > quitsBefore);

  console.log("\nquit really quits");
  app.emit("before-quit");
  check("before-quit marks the app as quitting", app.isQuitting === true);
  let prevented = false;
  main.emitWindow("close", { preventDefault: () => { prevented = true; } });
  check("the alerts window no longer swallows Quit", prevented === false,
    "the close handler hides the window unless app.isQuitting is set");

  console.log("\nalert still rings without the bubble UI");
  const bubbleHtml = path.join(APP_DIR, "bubble.html");
  const bubblePreload = path.join(APP_DIR, "bubble-preload.js");
  check("bubble.html ships in the app dir", fs.existsSync(bubbleHtml));
  check("bubble-preload.js ships in the app dir", fs.existsSync(bubblePreload));

  // Simulate the 1.1.0 packaging bug: bubble files absent from the install.
  // Stash them in place (same directory) — moving them to os.tmpdir() fails
  // with EXDEV on CI runners where the repo and temp are on different drives.
  const b1 = bubbleHtml + ".hidden";
  const b2 = bubblePreload + ".hidden";
  for (const f of [b1, b2]) { try { fs.rmSync(f, { force: true }); } catch (e) {} }
  fs.renameSync(bubbleHtml, b1);
  fs.renameSync(bubblePreload, b2);
  try {
    soundPlayed = [];
    notifications = [];
    if (ipcHandlers["sound:test"]) await ipcHandlers["sound:test"]({}, "chime");
    await new Promise((r) => setTimeout(r, 30));
    check("a sound still plays when the bubble cannot load", soundPlayed.length > 0,
      "without the fallback the alert would be completely silent");
  } finally {
    // Always put them back, and never let a restore error mask the real one or
    // leave stray *.hidden files behind in the working tree.
    for (const [from, to] of [[b1, bubbleHtml], [b2, bubblePreload]]) {
      try { if (fs.existsSync(from)) fs.renameSync(from, to); } catch (e) {
        console.error("  ! could not restore " + to + ": " + e.message);
      }
    }
  }
  check("bubble files restored", fs.existsSync(bubbleHtml) && fs.existsSync(bubblePreload));

  console.log("\nsigned in until the user signs out");
  // Drive the poll loop by hand: capture what schedulePolling() hands to
  // setInterval instead of waiting 10 s per tick.
  const realSetInterval = global.setInterval;
  let poll = null;
  global.setInterval = (fn, ms) => { poll = fn; return realSetInterval(() => {}, 1 << 30).unref(); };
  const realFetch = global.fetch;
  const httpLog = [];
  const jsonRes = (status, body) => ({
    ok: status >= 200 && status < 300, status,
    headers: { get: (h) => (h.toLowerCase() === "content-type" ? "application/json" : null) },
    json: async () => body,
  });
  const htmlRes = (status) => ({
    ok: false, status,
    headers: { get: (h) => (h.toLowerCase() === "content-type" ? "text/html" : null) },
    json: async () => { throw new Error("not json"); },
  });
  let server = () => jsonRes(200, { latest: null, unread: 0 });
  global.fetch = async (url, opts) => {
    httpLog.push({ url: String(url), opts: opts || {} });
    return server(String(url), opts || {});
  };
  const state = async () => ipcHandlers["auth:state"]({});
  const tick = async (n) => { for (let i = 0; i < n; i++) { await poll(); await new Promise((r) => setTimeout(r, 5)); } };
  try {
    server = (url) => url.endsWith("/api/login")
      ? jsonRes(200, { token: "tok-A", user: { name: "Test User" } })
      : jsonRes(200, { latest: { id: 7, text: "x" }, unread: 0 });
    const signedIn = await ipcHandlers["auth:signIn"]({}, { email: "tech@lab.test", password: "pw" });
    check("sign-in works against the mocked API", !!signedIn && signedIn.ok === true, JSON.stringify(signedIn));
    check("polling is scheduled after sign-in", typeof poll === "function");
    await new Promise((r) => setTimeout(r, 10));

    server = () => jsonRes(503, { error: "unavailable" });
    await tick(8);
    check("a server outage (503) never signs the user out", (await state()).signedIn === true);

    server = () => { throw new TypeError("fetch failed"); };
    await tick(8);
    check("network errors never sign the user out", (await state()).signedIn === true);

    server = () => htmlRes(401);
    await tick(8);
    check("a non-JSON 401 (proxy / captive portal) never signs the user out", (await state()).signedIn === true);

    server = () => htmlRes(403);
    await tick(8);
    check("a 403 never signs the user out", (await state()).signedIn === true);

    // one API 401 (e.g. mid-deploy) that heals: stays signed in, counter resets
    let flaky = 0;
    server = () => (flaky++ < 2 ? jsonRes(401, { error: "Not authenticated" }) : jsonRes(200, { latest: { id: 7, text: "x" }, unread: 0 }));
    await tick(6);
    check("a lone API 401 that heals never signs the user out", (await state()).signedIn === true);

    // the API keeps saying the session is gone, but /api/me disagrees → stay
    server = (url) => (url.endsWith("/api/me") ? jsonRes(200, { id: 1, email: "tech@lab.test", name: "Test User" })
      : jsonRes(401, { error: "Not authenticated" }));
    await tick(8);
    check("a 401 the /api/me re-check contradicts never signs the user out", (await state()).signedIn === true);

    // the real thing: session removed on the server (signed out on the site
    // or account removed) — confirmed on every poll, for a full minute
    notifications = [];
    const alertsWin = windows[windows.length - 1];
    alertsWin.hide();
    server = () => jsonRes(401, { error: "Not authenticated" });
    await tick(3);
    check("three confirmed 401s (≈30 s) are still not enough to sign out", (await state()).signedIn === true);
    await tick(4);
    check("a session the API confirms gone for ≈1 minute is finally signed out", (await state()).signedIn === false);
    check("…with a Windows notification telling the user to sign in again",
      notifications.some((n) => /signed out/i.test(String(n.opts && n.opts.title)) && n.shown),
      JSON.stringify(notifications.map((n) => n.opts)));
    check("…without forcing the window over the user's work", !alertsWin.isVisible());
    const sent = (alertsWin.webContents.sends || []).filter((s) => s.channel === "state").pop();
    check("…and the sign-in form explains why", !!sent && sent.payload.signedIn === false && /sign in again/i.test(String(sent.payload.reason)));
    const prefs = JSON.parse(fs.readFileSync(path.join(tmp, "userData", "config.json"), "utf8"));
    check("the dead token is not kept on disk", !prefs.token);
    check("no /api/logout was sent for a server-side sign-out", !httpLog.some((h) => h.url.endsWith("/api/logout")));

    // manual sign-out ends the session on the server as well
    httpLog.length = 0;
    server = (url) => url.endsWith("/api/login")
      ? jsonRes(200, { token: "tok-B", user: { name: "Test User" } })
      : jsonRes(200, { ok: true, latest: null, unread: 0 });
    await ipcHandlers["auth:signIn"]({}, { email: "tech@lab.test", password: "pw" });
    check("signing in again works after a server-side sign-out", (await state()).signedIn === true);
    await ipcHandlers["auth:signOut"]({});
    await new Promise((r) => setTimeout(r, 10));
    const logout = httpLog.find((h) => h.url.endsWith("/api/logout"));
    check("a manual sign-out ends the session on the server too (POST /api/logout)",
      !!logout && logout.opts.method === "POST" && /tok-B/.test(String(logout.opts.headers && logout.opts.headers.Authorization)));
    check("a manual sign-out signs out locally", (await state()).signedIn === false);
    check("a manual sign-out drops the mirrored site cookie", !cookieJar.labcare_token);

    // a sign-in made inside the Site tab is adopted by the app
    server = (url) => (url.endsWith("/api/me") ? jsonRes(200, { id: 3, email: "site@lab.test", name: "Site User" })
      : jsonRes(200, { latest: null, unread: 0 }));
    siteSession.cookies.emitSet("labcare_token", "tok-site");
    await new Promise((r) => setTimeout(r, 30));
    const adopted = await state();
    check("a sign-in made in the Site tab is adopted (alerts ring without a second sign-in)",
      adopted.signedIn === true && adopted.email === "site@lab.test", JSON.stringify(adopted));
    const adoptedPrefs = JSON.parse(fs.readFileSync(path.join(tmp, "userData", "config.json"), "utf8"));
    check("the adopted session is saved for the next start", adoptedPrefs.token === "tok-site");
    // …but a different account signing in on the site never replaces one already here
    siteSession.cookies.emitSet("labcare_token", "tok-other");
    await new Promise((r) => setTimeout(r, 30));
    check("a different site sign-in never replaces the app's own session", (await state()).email === "site@lab.test");
    // the mirrored cookie lives long enough to outlast any realistic gap
    await ipcHandlers["site:open"]({}, undefined);
    await new Promise((r) => setTimeout(r, 10));
    const mirrored = cookieJar.labcare_token;
    check("the Site tab session cookie is long-lived (≥ 1 year)",
      !!mirrored && (mirrored.expirationDate - Date.now() / 1000) > 365 * 24 * 3600, JSON.stringify(mirrored));
  } finally {
    global.setInterval = realSetInterval;
    global.fetch = realFetch;
  }

  console.log("\nbridge surface");
  const preloadSrc = fs.readFileSync(path.join(APP_DIR, "preload.js"), "utf8");
  for (const api of ["openSite", "openSiteExternal", "siteUrl", "getState", "signIn"]) {
    check("preload exposes " + api, preloadSrc.includes(api));
  }
  const uiSrc = fs.readFileSync(path.join(APP_DIR, "renderer", "index.html"), "utf8");
  check("the UI has a Site tab", uiSrc.includes('["site","Site"]'));
  check("the UI has a direct <a> link to the site", uiSrc.includes("sitelink"));
  check("the UI paints before the async boot (no blank frame)",
    /Starting…/.test(uiSrc) || /Starting\.\.\./.test(uiSrc));
  check("the UI degrades gracefully with no bridge", uiSrc.includes("bridgeMissing"));

  // cleanup
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (e) {}

  console.log(failures ? `\n${failures} check(s) FAILED\n` : "\nAll checks passed.\n");
  process.exit(failures ? 1 : 0);
})().catch((e) => {
  console.error("\nsmoke test crashed:", e);
  process.exit(1);
});
