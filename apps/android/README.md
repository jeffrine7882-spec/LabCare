# LabCare Alerts — Android APK

## Install
1. Copy **`LabCare-Alerts-v1.0.apk`** (in `dist/`) to your Android phone —
   e.g. over USB, cloud drive, or direct download.
2. Tap it in Files. Android will ask you to allow "install unknown apps"
   for that app — allow it. (The APK is self-signed, not from the Play Store.)
3. Open **LabCare Alerts** and sign in with your LabCare account.

## What it does
* Signs in with the same account as the web app (`https://labcare.insforge.site`).
* A foreground **alert relay** polls your bell every **10 seconds** and, on a
  new notification, **plays a chime + vibrates + shows a notification** —
  even with the phone screen off or the app UI closed.
* Auto-starts the relay after the phone reboots.
* A **Phone alerts** switch turns it on/off; sign-out clears the session.

### Honest current limitation
Ring-with-app-fully-closed **and killed** needs Firebase Cloud Messaging (the
server pushing to the phone), which needs credentials only you can create —
see **`server/apppush.py`** + the two env vars in `apps/README-setup.md`.
Until then the relay above does the ringing and works reliably in practice
(foreground service + battery optimization off).

The APK **also** needs the LabCare server to stay reachable (it's HTTPS, always-on).

## Build (optional)
Requires JDK 11 and `~/android-sdk` with build-tools 33.0.2 + platform 33:
```
./build.sh        # → dist/LabCare-Alerts-v1.0.apk (signed)
```

In-app emergency access: the app has no "demo" account built in; use any
account from the web app (e.g. `admin@labcare.com` / `Demo123!`).
