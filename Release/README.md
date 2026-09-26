# LabSynch — native installers

Built by GitHub Actions run [#104](https://github.com/jeffrine7882-spec/LabCare/actions/runs/36255883566)
from commit b5cefdb on 2026-09-26 UTC.

## Android — android/LabSynch-Alerts-v1.5.apk
Requires Android 8.0+ (API 26) and installs on the latest Android
14/15/16 devices (targets SDK 34). Self-signed — not on the Play Store.
1. Copy the APK to the phone (USB / cloud drive / direct download).
2. Tap it; allow "install unknown apps" when Android asks. The APK is
   self-signed, not from the Play Store — that is expected.
3. Open "LabSynch Alerts" and sign in with your LabSynch account — the
   app opens the LabSynch site itself (Site tab), plus Notifications
   and the ring-sound picker. You stay signed in until you tap
   Sign out (v1.5): server restarts, outages or network blips never
   sign you out. A sign-in or sign-out made inside the Site tab
   applies to the phone alerts too.
4. **Allow the battery prompt** shown right after sign-in (once): it
   lets the alert relay run unrestricted while the phone idles.
5. Keep "Phone alerts" on: every new bell notification drops a heads-up
   bubble banner on top of the display, rings your chosen sound and
   vibrates the phone — v1.5 keeps that ringing even when the app is
   closed, swiped away, the screen is off or the phone reboots
   (foreground service + wake locks + Doze-proof wake-up alarms +
   restart alarms + a 60 s watchdog; Home → Ring protection shows
   status and one-tap fixes).
Note: each CI build uses a fresh signing key, so uninstall any older CI
build before installing a new one.

## Windows — windows/LabSynch-Alerts-Setup-1.4.0.exe
1. Run the installer. SmartScreen will warn about an "unrecognized app"
   (self-signed) — choose **More info → Run anyway**.
2. It installs under %LOCALAPPDATA%\LabSynch Alerts, adds Start-menu +
   desktop shortcuts, and auto-starts with Windows — hidden, straight
   to the system tray.
3. Sign in once; the app then lives in the system tray. You stay
   signed in until you click Sign out (1.4.0): server restarts,
   outages or network blips never sign you out, and a sign-in made
   inside the Site tab is picked up by the alerts too. Every new bell
   notification floats an always-on-top bubble banner (top-right of
   your screen) with the alert sound + dismiss/open buttons, plus a
   Windows notification. Clicking either opens the web app.
4. **Rings even when the app is closed:** closing the window only hides
   it to the tray, the login auto-start runs hidden, and a per-minute
   watchdog Scheduled Task relaunches the exe if it was quit or killed
   (hidden, and a no-op when already running). Quit asks for
   confirmation first. Uninstalling removes the watchdog task and the
   auto-start entry.
5. The **Site** tab opens the LabSynch web app itself
   (https://labcare.insforge.site) inside the app, already signed in
   with the account you used here — as does the **Open the LabSynch
   site** button on Home and the tray menu. If the site cannot load
   you get a page with **Retry** and **Open the site in my browser**,
   never a blank window.

v1.5 APK / 1.4.0 EXE: you stay signed in until you sign out — no
more being signed out by a server restart or deploy, an outage or a
network blip; only Sign out (or an administrator removing the
account) ends a session, and in that one case the app tells you with
a "Signed out of LabSynch" notification instead of going silent.
Earlier: v1.4 / 1.3.0 made the alert ring even with the app closed;
1.2.0 fixed the blank-page installer from 1.1.0 (missing bubble
files, window never shown). Uninstall older versions first
(Settings → Apps → LabSynch Alerts).

## Web app (no install)
https://labcare.insforge.site — also installable as a PWA
("Add to Home screen").
