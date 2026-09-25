# LabCare — native installers

Built by GitHub Actions run [#3](https://github.com/jeffrine7882-spec/LabCare/actions/runs/36127341323)
from commit 9941aeb on 2026-09-25 UTC.

## Android — android/LabCare-Alerts-v1.1.apk
1. Copy the APK to the phone (USB / cloud drive / direct download).
2. Tap it; allow "install unknown apps" when Android asks. The APK is
   self-signed, not from the Play Store — that is expected.
3. Open "LabCare Alerts" and sign in with your LabCare account.
4. Keep "Phone alerts" on: every new bell notification drops a heads-up
   bubble banner on top of the display, rings your chosen sound and
   vibrates the phone — even with the screen off.
Note: each CI build uses a fresh signing key, so uninstall any older CI
build before installing a new one.

## Windows — windows/LabCare-Alerts-Setup-1.1.0.exe
1. Run the installer. SmartScreen will warn about an "unrecognized app"
   (self-signed) — choose **More info → Run anyway**.
2. It installs under %LOCALAPPDATA%\LabCare Alerts, adds Start-menu +
   desktop shortcuts, and auto-starts with Windows.
3. Sign in once; the app then lives in the system tray. Every new bell
   notification floats an always-on-top bubble banner (top-right of
   your screen) with the alert sound + dismiss/open buttons, plus a
   Windows notification. Clicking either opens the web app.

## Web app (no install)
https://labcare.insforge.site — also installable as a PWA
("Add to Home screen").
