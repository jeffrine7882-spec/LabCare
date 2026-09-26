# LabSynch Alerts — Android APK

## Install
Requirements: **Android 8.0+**, installable on the latest Android 14/15/16
(targets SDK 34). Self-signed, not on the Play Store.


1. Copy **`LabSynch-Alerts-v1.5.apk`** (in `dist/`) to your Android phone —
   e.g. over USB, cloud drive, or direct download.
2. Tap it in Files. Android will ask you to allow "install unknown apps"
   for that app — allow it. (The APK is self-signed, not from the Play Store.)
3. Open **LabSynch Alerts** and sign in with your LabSynch account — the app
   opens the **LabSynch site itself** (Site tab) so you can work on your
   complaints, breakdowns and equipment right away.
4. **Allow the battery prompt** — right after sign-in the app asks once to
   "ignore battery optimizations". That single Allow is what lets Android keep
   the alert relay running unrestricted while the phone idles. Skip it and the
   app still rings, but alerts can be delayed (see below).

## What it does
* **Opens the LabSynch site** (`https://labcare.insforge.site`) in the app's
  **Site** tab, already signed in with your LabSynch account — the same data
  and features as the web app (tickets, equipment, PDF service reports).
* Signs in with the same account as the web app.
* A foreground **alert relay** polls your bell every **10 seconds** and, on a
  new notification, drops a **heads-up bubble banner on top of the display**,
  **plays your chosen sound + vibrates**. (The alert channel is
  `labcare_alerts_v2`: high importance for the floating banner, so upgrades
  get the new behaviour.)
* Auto-starts the relay after the phone reboots **and after the app itself is
  updated**.
* A **Phone alerts** switch turns it on/off; sign-out clears the session.
* **You stay signed in until you sign out (v1.5).** The relay never drops the
  session on its own: server restarts/deploys, outages, proxy 401s and network
  blips only pause alerts until the server is back. Only a session the API
  itself repeatedly reports as gone (six consecutive `Not authenticated` polls,
  each re-checked against `/api/me`, ≈ 1 min — i.e. you signed out on the
  site, or an administrator removed the account) is dropped, and then the
  phone shows a "Signed out of LabSynch — sign in again" notification instead
  of going silent. Signing in or out inside the Site tab is mirrored into the
  app through the `LabSynchDroid` JS bridge, so alerts follow the site session
  without a second sign-in.

## Ringing with the app closed (mandatory, v1.4)
The relay is built so the phone keeps ringing when the app is closed, swiped
away, asleep or rebooted — five layers, all cancelled when you turn alerts
off or sign out:

1. **Foreground service** — Android does not idle-kill it while it shows the
   "LabSynch alerts on" notification.
2. **Wake locks per poll and per ring** — a foreground service alone does not
   keep the CPU out of suspend; the relay now holds a partial wake lock across
   every bell check and the whole sound playback, so the ring is not cut short
   when the screen is off.
3. **Doze-proof WAKE alarms every ~12 s** (`setExactAndAllowWhileIdle`) —
   handler timers freeze the moment the CPU sleeps; these alarms do not, and
   their firing is exempt from the Android 12+ ban on starting foreground
   services from the background.
4. **Restart alarms** — `onTaskRemoved()`/`onDestroy()` immediately re-start
   the relay and schedule a delayed restart, so swiping the app away from
   Recents (or an OEM "battery saver" killing it) silences the phone for a
   couple of seconds at most. A `setAlarmClock()` fallback retries a refused
   start from the temporary allowlist.
5. **A 60-second watchdog** that checks the relay's heartbeat and resurrects
   it (and re-arms its alarm chains) if polls ever stop.

The **Home → Ring protection** card shows live status for every setting this
depends on, with one-tap fixes: battery optimisation (the big one — the
post-sign-in prompt asks for it once), Alarms & reminders (Android 12+),
notifications and full-screen alerts (Android 14+). Alerts also turn the
screen on over the lock screen via a full-screen intent.

Honest limits: a **Force stop** from App info stops everything by OS design
(no alarm, no service survives it — reopen the app to re-arm). On phones
where the user did **not** allow the battery exemption, deep Doze may batch
the wake-up alarms, delaying an alert by up to a few minutes while the phone
lies untouched. Ringing with the app closed and **force-stopped** (or
uninstalled) is impossible for any app without server push; the FCM path
remains available (`server/apppush.py` + `apps/README-setup.md`) for when
credentials exist.

The APK **also** needs the LabSynch server to stay reachable (it's HTTPS, always-on).

## Build (optional)
Requires JDK 11 and `~/android-sdk` with build-tools 33.0.2 + platform 33:
```
./build.sh        # → dist/LabSynch-Alerts-v1.5.apk (signed)
```

In-app emergency access: the app has no "demo" account built in; use any
account from the web app (e.g. `admin@labcare.com` / `Demo123!`).
