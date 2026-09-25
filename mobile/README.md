# LabCare mobile app (Expo / React Native)

A native companion app whose job is exactly one thing: **ring on the phone for
every LabCare bell notification** — even when the browser is closed, the app is
backgrounded, or the phone is locked. One app covers Android and iPhone; push
is delivered through **Firebase Cloud Messaging (FCM)**, which supports both.

The LabCare backend already has the receiving side implemented:

* tables: `app_devices` (per-user device/token registrations)
* endpoints: `POST /api/app/register`, `POST /api/app/unregister`,
  `GET /api/app/devices`, `GET /api/app/config`
* `notify()` fires `apppush.send_app_push(...)` for every bell notification
  (in addition to in-app + email + browser Web Push).

What remains (the parts that need your accounts) is below.

---

## 1. Create the Firebase project (~5 minutes, free)

1. Go to https://console.firebase.google.com → **Add project** (e.g. `labcare`).
2. **Project settings → Service accounts → Generate new private key.**
   You get a JSON file. Add its contents to the backend as two env vars
   (ask the agent to wire these into `.env.production` / deploy):

   - `LABCARE_FCM_PROJECT_ID` ← the `project_id` field of that JSON
   - `LABCARE_FCM_SERVICE_JSON` ← the whole JSON on a single line
     (or `LABCARE_FCM_KEY_B64` ← base64 of the JSON)

   Until these are set the backend logs one warning and skips app pushes —
   web push and everything else keep working.

3. **Build → Cloud Messaging** is already enabled by default; no further clicks.

## 2. Add the mobile app in Firebase

1. **Project settings → Your apps → Add app.**
2. Android → package name `com.insforge.labcare`, then **Download
   google-services.json** → save it as `mobile/google-services.json`
   (this file is git-ignored, and the account that deploys the app keeps it).
3. iOS → bundle id `com.insforge.labcare`, download `GoogleService-Info.plist`
   → save as `mobile/GoogleService-Info.plist` (only needed for a full iOS build;
   Expo Go on iOS does not receive push).
4. Note **App ID** and **Sender ID** shown on that page → put them in
   `mobile/.env` (copy `mobile/.env.example` and fill `FIREBASE_APP_ID` /
   `FIREBASE_SENDER_ID`). Expo reads these at build time for the push token.

## 3. Run it

```bash
cd mobile
npm install
npx expo start          # scan the QR with the Expo Go app
```

* **Android**: Expo Go shows notifications but you should verify on a dev
  build (`npx expo run:android`) for reliable background/locked-phone delivery.
* **iOS push**: requires an Apple Developer Program membership
  (https://developer.apple.com/programs, US$99/yr). With it, set up APNs keys
  (Apple Developer → Certificates, Identifiers & Profiles → Keys → APNs,
  upload the `.p8` to Firebase → Project settings → Cloud Messaging → iOS app)
  then `npx expo run:ios` on a real iPhone.

> Firebase treats Android and iOS tokens the same way: the LabCare backend
> sends one FCM v1 message per token, with `android.priority=high` and
> `apns.payload.aps.sound="default"`, so both platforms ring on delivery.

## 4. Test end-to-end

1. Sign in in the app (any LabCare account, e.g. `admin@labcare.com` /
   `Demo123!`), allow notifications when asked. The app POSTs the push token
   to `/api/app/register`.
2. From the web (or another account) trigger any bell notification, e.g. the
   QR portal `https://labcare.insforge.site/portal.html?t=brf-freezer` sends a
   report, or one user comments on a ticket.
3. Lock the phone / close the browser. The alert should arrive and ring.

Troubleshooting: `GET /api/app/devices` (signed in) lists the registered
devices; a token FCM rejects as `UNREGISTERED` is automatically deactivated.
