#!/usr/bin/env node
/*
 * Behavioural smoke test for the LabCare desktop app — runs main.js against a
 * mock Electron in plain Node, so it needs no Electron binary, no display and
 * no network. `npm test` / the release workflow run it before packaging.
 *
 * It exists because the 1.1.0 installer shipped an app that could show a blank
 * window and had no working link to the LabCare site, and nothing caught it.
 * These assertions pin the behaviour the user actually depends on:
 *
 *   1. the alerts window is really shown at launch (it used to be created
 *      hidden with nothing ever calling show()),
 *   2. the bundled UI loads,
 *   3. the Site action opens https://labcare.insforge.site/,
 *   4. a failed load lands on a recovery page that still links to the site,
 *   5. the tray offers "Open LabCare site",
 *   6. Quit actually quits (the close handler used to swallow it),
 *   7. an alert still rings when the bubble UI is unavailable.
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
  send() {}
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
  _readyCbs: [],
  requestSingleInstanceLock: () => true,
  whenReady: () => Promise.resolve(),
  on: (ev, cb) => { (app._evs[ev] = app._evs[ev] || []).push(cb); },
  _evs: {},
  emit(ev, ...a) { for (const cb of (app._evs[ev] || []).slice()) cb(...a); },
  quit: () => { app.emit("before-quit"); app.quitCalled = (app.quitCalled || 0) + 1; },
  setAppUserModelId: () => {},
  setLoginItemSettings: () => {},
  getVersion: () => require(path.join(APP_DIR, "package.json")).version,
  getPath: (k) => path.join(tmp, k),
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
  session: {
    fromPartition: () => ({
      cookies: { set: async () => {}, remove: async () => {} },
      on: () => {}, off: () => {},
    }),
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
  console.log("LabCare desktop smoke test\n");

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
  check("tray offers 'Open LabCare site'", labels.includes("Open LabCare site"), "labels: " + labels.join(", "));
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
    check("the recovery page links to the LabCare site", html.includes(SITE_URL));
    check("the recovery page offers the browser fallback",
      html.includes("labcare://open-external") && /Open the site in my browser/.test(html));
    check("the recovery page offers Retry", /Retry/.test(html));
  }

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
