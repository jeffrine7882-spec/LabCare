# LabSynch native apps

| App | Installer | Where |
|---|---|---|
| **Android** | `LabCare-Alerts-v1.3.apk` | `../Release/android/` (also on the GitHub [Releases](../../releases) page) |
| **Windows** | `LabCare-Alerts-Setup-1.1.0.exe` | `../Release/windows/` |

Both sign in with the **same account as the web app**
(`https://labcare.insforge.site`) and ring the LabSynch chime + show a
notification for every new bell alert.

- **Android** (`android/`): opens the **LabSynch site itself** in its Site tab
  (signed in with your account), plus a foreground alert relay that polls the
  bell every 10 s, rings + vibrates + notifies even with the screen off, and
  auto-starts after a reboot. APK is self-signed (allow "install unknown apps").
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
