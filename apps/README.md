# LabCare native apps

| App | Installer | Where |
|---|---|---|
| **Android** | `LabCare-Alerts-Android-v1.0.apk` | `../releases/` |
| **Windows** | `LabCare-Alerts-Windows-Setup-1.0.0.exe` | `../releases/` |

Both sign in with the **same account as the web app**
(`https://labcare.insforge.site`) and ring the LabCare chime + show a
notification for every new bell alert.

- **Android** (`android/`): foreground alert relay that polls the bell every
  10 s, rings + vibrates + notifies even with the screen off, and auto-starts
  after a reboot. APK is self-signed (allow "install unknown apps").
- **Windows** (`desktop/`): system-tray app that auto-starts with Windows,
  shows a native Windows notification + plays the chime, and opens the web app
  when you click an alert.

## Honest limitation (both apps)
They ring from their **own background polling**, which keeps working with the
app window closed / phone screen off — as long as the app process is running
(both auto-start). Ringing with the app **fully closed/killed** needs
Firebase Cloud Messaging: the server side is already implemented
(`server/apppush.py` + `/api/app/register`), and only needs two credentials
from a free Firebase project — see `README-setup.md`.
