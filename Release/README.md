# LabCare — native installers

Built by GitHub Actions run [#2](https://github.com/jeffrine7882-spec/LabCare/actions/runs/36126338116)
from commit 9b6ef22 on 2026-09-25 UTC.

## Android — android/LabCare-Alerts-v1.0.apk
1. Copy the APK to the phone (USB / cloud drive / direct download).
2. Tap it; allow "install unknown apps" when Android asks. The APK is
   self-signed, not from the Play Store — that is expected.
3. Open "LabCare Alerts" and sign in with your LabCare account.
4. Keep "Phone alerts" on to get a chime + vibration + notification for
   every new bell notification, even with the screen off.
Note: each CI build uses a fresh signing key, so uninstall any older CI
build before installing a new one.

## Windows — windows/LabCare-Alerts-Setup-1.0.0.exe
1. Run the installer. SmartScreen will warn about an "unrecognized app"
   (self-signed) — choose **More info → Run anyway**.
2. It installs under %LOCALAPPDATA%\LabCare Alerts, adds Start-menu +
   desktop shortcuts, and auto-starts with Windows.
3. Sign in once; the app then lives in the system tray and rings for
   every new bell notification. Clicking a notification opens the web app.

## Web app (no install)
https://labcare.insforge.site — also installable as a PWA
("Add to Home screen").
