# LabSynch — native installers

Built by GitHub Actions (see the **Actions** tab — *Build release binaries*) from
the latest `[release]` commit. v1.4 APK / 1.3.0 EXE: **the alert now rings even
when the app is closed.**

## Android — android/LabSynch-Alerts-v1.4.apk
Requires Android 8.0+ (API 26) and installs on the latest Android
14/15/16 devices (targets SDK 34). Self-signed — not on the Play Store.
1. Copy the APK to the phone (USB / cloud drive / direct download).
2. Tap it; allow "install unknown apps" when Android asks. The APK is
   self-signed, not from the Play Store — that is expected.
3. Open "LabSynch Alerts" and sign in with your LabSynch account — the app
   opens the LabSynch site itself (Site tab), plus Notifications and the
   ring-sound picker.
4. **Allow the battery prompt** shown right after sign-in (once): it lets the
   alert relay run unrestricted while the phone idles.
5. Keep "Phone alerts" on: every new bell notification drops a heads-up bubble
   banner on top of the display, rings your chosen sound and vibrates the
   phone — **and v1.4 keeps that ringing even when the app is closed, swiped
   away, the screen is off or the phone reboots**: a foreground service, wake
   locks across every poll and ring, Doze-proof wake-up alarms every ~12 s,
   restart alarms when the app is killed, a 60-second heartbeat watchdog, and
   a lock-screen full-screen alert. Home → **Ring protection** shows the status
   of every setting this depends on, with one-tap fixes.
Note: each CI build uses a fresh signing key, so uninstall any older CI build
before installing a new one. (A deliberate **Force stop** from App info still
stops everything — that is an OS rule no app can bypass; reopen the app to
re-arm the relay.)

## Windows — windows/LabSynch-Alerts-Setup-1.3.0.exe
1. Run the installer. SmartScreen will warn about an "unrecognized app"
   (self-signed) — choose **More info → Run anyway**.
2. It installs under %LOCALAPPDATA%\LabSynch Alerts, adds Start-menu +
   desktop shortcuts, and auto-starts with Windows — hidden, straight to the
   system tray.
3. Sign in once; the app then lives in the system tray. Every new bell
   notification floats an always-on-top bubble banner (top-right of your
   screen) with the alert sound + dismiss/open buttons, plus a Windows
   notification. Clicking either opens the web app.
4. **Rings even when the app is closed:** closing the window only hides it to
   the tray; the Windows login auto-start runs hidden; and a per-minute
   **watchdog** Scheduled Task relaunches the exe if it was quit or killed —
   hidden, and a no-op when the app is already running. Quit asks for
   confirmation first; uninstalling removes the watchdog task and the
   auto-start entry.
5. The **Site** tab opens the LabSynch web app itself
   (https://labcare.insforge.site) inside the app, already signed in with the
   account you used here — as does the **Open the LabSynch site** button on
   Home and the tray menu. If the site cannot load you get a page with
   **Retry** and **Open the site in my browser**, never a blank window.

Upgrading from 1.1.0/1.2.0? Uninstall it first (Settings → Apps → LabSynch
Alerts). 1.1.0 shipped a broken installer (blank page); 1.2.0 fixed it; 1.3.0
adds the ring-even-when-closed hardening.

## Web app (no install)
https://labcare.insforge.site — also installable as a PWA
("Add to Home screen").
