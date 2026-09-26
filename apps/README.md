# LabSynch native apps

| App | Installer | Where |
|---|---|---|
| **Android** | `LabSynch-Alerts-v1.5.apk` | `../Release/android/` (also on the GitHub [Releases](../../releases) page) |
| **Windows** | `LabSynch-Alerts-Setup-1.4.0.exe` | `../Release/windows/` |

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

## Ringing with the app closed (mandatory)
Both apps are built so the alert still rings when the app is closed, killed,
asleep or rebooted — not only while their windows are open:

- **Android v1.4** layers a foreground service, per-poll/per-ring wake locks,
  Doze-proof `setExactAndAllowWhileIdle` wake-up alarms every ~12 s, restart
  alarms on task-removed/destroy (survives being swiped away and OEM killers,
  with a `setAlarmClock()` allowlist fallback), a 60 s heartbeat watchdog, and
  a lock-screen full-screen alert. Home → **Ring protection** shows the status
  of every setting this needs with one-tap fixes; the battery-optimisation
  exemption is asked for right after sign-in (one Allow).
- **Windows 1.3.0** hides to the tray on window close, auto-starts with
  Windows **hidden** (`--hidden`), and registers a per-minute **watchdog
  Scheduled Task** that relaunches the exe if it was quit or killed — hidden,
  straight to the tray, and a no-op when already running. Quit asks for
  confirmation, and the uninstaller removes the watchdog task and the
  auto-start entry.

## Signed in until you sign out (v1.5 APK / 1.4.0 EXE)
Both apps (and the web app) keep the session until the user signs out. A
server restart or deploy, an outage, a proxy/captive-portal 401 or a network
blip never signs anyone out any more — the server no longer wipes sessions on
boot, sessions never expire, and the clients treat a 401 as a verdict only when
the LabSynch API itself repeats it (JSON `Not authenticated`, re-checked against
`/api/me`) for six consecutive polls (≈ 1 minute). That single case — the user
signed out on the site, or an administrator removed the account — drops the
dead token and shows a "Signed out of LabSynch — sign in again" notification
instead of polling silently forever. A sign-in or sign-out made inside the Site
tab is mirrored into the app (Android: JS bridge; Windows: the site's session
cookie), and a manual sign-out in either app also ends the session on the
server.

Remaining honest limit: an explicit **Force stop** (Android) stops everything
by OS design, and server-push ringing (app fully closed/force-stopped, or iOS)
still needs the FCM credentials in `README-setup.md` — the server side is
already built and waiting (`server/apppush.py` + `/api/app/register`).
