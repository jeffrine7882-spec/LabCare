# LabSynch Alerts — Windows desktop app

## Install
Run **`LabSynch-Alerts-Setup-1.2.0.exe`** (in `Release/windows/`, or `dist/`
after a local build). It installs to `%LOCALAPPDATA%\LabSynch Alerts`, adds a
Start-menu + desktop shortcut, and launches on finish. **SmartScreen** will warn
about an "unrecognized app" (self-signed) — choose **More info → Run anyway**.

> Upgrading from 1.1.0? Uninstall it first (Settings → Apps → LabSynch Alerts).
> 1.1.0 shipped broken — see *Why 1.2.0 exists* below.

## What it does
* **Opens the LabSynch site.** The **Site** tab loads
  `https://labcare.insforge.site` inside the app, already signed in with the
  account this app holds (the token is mirrored into the site's own
  `labcare_token` session cookie, and the web app calls `/api/me` on boot).
  The same site opens from the **Open the LabSynch site** button on Home, from
  the tray menu, from an alert bubble's **Open LabSynch**, and from a Windows
  notification click. Files the site downloads (PDF service reports) land in
  your Downloads folder.
* **Never a blank page.** The window shows as soon as it can paint. If the
  bundled UI cannot load, the window falls back to the real site; if the site
  cannot load, you get a recovery page with **Retry** and **Open the site in my
  browser**.
* **System tray** app — sign in once with your LabSynch account, then close the
  window; it keeps running in the tray.
* **Auto-starts with Windows** (login item).
* Polls your bell every 10 s. On a new notification it:
  * floats an **always-on-top bubble banner** at the top-right of your screen
    (no focus theft, dismiss or "Open LabSynch" buttons), with **sound**, and
  * adds a native **Windows notification** as a durable breadcrumb.
* Right-click the tray icon for **Open LabSynch site**, **Open Alerts window**,
  **Alerts on/off**, **Sign out**, **Quit**.

## Why 1.2.0 exists
1.1.0 installed an app that showed a blank page and could not reach the site:

1. **`build.files` is an allowlist, and it was missing two files.** It listed
   only `main.js`, `preload.js`, `renderer/**`, `assets/**` — so `bubble.html`
   and `bubble-preload.js` were never packed into `app.asar`. `npm start` worked
   (it reads from disk), but the installed app's alert bubble had nothing to
   load: a blank bubble, no **Open LabSynch** button, and no sound.
2. **The main window was created with `show: false` and nothing ever showed
   it** — there was no `ready-to-show` handler and no `win.show()` at launch, so
   after the installer's `runAfterFinish` launch the app looked dead.
3. **No Site tab at all.** The Android app has one; the desktop app's only path
   to the site was the (broken) bubble.
4. **No failure path.** Any load error left an empty window with no way out.
5. **`Quit` never quit** — the close handler always hid the window because
   `app.isQuitting` was never set.

Fixes: the two files are now packaged; the window shows on `ready-to-show` with
a failsafe timer; a **Site** tab opens the real web app; every failure lands on
a recovery page that still links to the site; sounds are copied out of the asar
so the external player can read them; and `before-quit` sets `app.isQuitting`.

## Checks (no Electron binary, no display, no network needed)
```
npm test                 # both checks below
npm run verify:package   # every runtime file is inside build.files
node smoke-test.js       # runs main.js against a mock Electron and asserts:
                         #   window shows, UI loads, Site opens the site,
                         #   failed load -> recovery page, Quit quits,
                         #   an alert still rings without the bubble UI
```
Both run in the *Build release binaries* workflow **before** the installer is
built, so a file can never be silently dropped again. Against the old 1.1.0
source `smoke-test.js` fails 20 checks; against this source it passes all.

## Honest current limitation
This app rings from its own background polling — works even with the window
closed, as long as the app is running (it starts with Windows). True push
even when the app is fully closed needs the same Firebase FCM credentials used
by the Android app (`apps/README-setup.md`); the server side is already built
and waiting (`server/apppush.py`).

## Build
```
npm install
npm run pack             # runs the checks, then:
                         # npx electron-builder --win nsis
```
On Linux this needs `wine64` for embedding the icon/version into the .exe; CI
builds on `windows-latest` instead. If `npm install` cannot download the Electron
binary (sandboxed network), set `ELECTRON_SKIP_BINARY_DOWNLOAD=1` — the checks
above still run, since they do not need Electron itself.
