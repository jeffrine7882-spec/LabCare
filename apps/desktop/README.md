# LabCare Alerts — Windows desktop app

## Install
Run **`LabCare Alerts Setup 1.0.0.exe`** (in `dist/`). It installs to
`%LOCALAPPDATA%\LabCare Alerts`, adds a Start-menu + desktop shortcut, and
launches on finish. **SmartScreen** will warn about an "unrecognized app"
(self-signed) — choose **More info → Run anyway**.

## What it does
* **System tray** app — sign in once with your LabCare account, then close the
  window; it keeps running in the tray.
* **Auto-starts with Windows** (login item).
* Polls your bell every 10 s. On a new notification it:
  * shows a native **Windows notification**, and
  * plays the LabCare **chime** (via Windows' SoundPlayer).
* Clicking a notification opens the LabCare web app in your browser.
* Right-click the tray icon for **Alerts on/off**, **Open LabCare**, **Sign out**, **Quit**.

## Honest current limitation
This app rings from its own background polling — works even with the window
closed, as long as the app is running (it starts with Windows). True push
even when the app is fully closed needs the same Firebase FCM credentials used
by the Android app (`apps/README-setup.md`); the server side is already built
and waiting (`server/apppush.py`).

## Build (optional, Linux with Wine)
```
npm install
npx electron-builder --win nsis   # → dist/LabCare Alerts Setup 1.0.0.exe
```
Requires `wine64` for embedding the icon/version into the .exe.
