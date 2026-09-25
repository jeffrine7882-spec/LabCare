# FireBase setup (the two credentials the server needs)

1. Go to **https://console.firebase.google.com** → **Add project** (name `labcare`).
2. **Project settings → Service accounts → Generate new private key.** You get a JSON file.
3. Give these two values to the agent (or ask to be added to `.env.production`):

| Env var | Value |
|---|---|
| `LABCARE_FCM_PROJECT_ID` | the `project_id` field of that JSON |
| `LABCARE_FCM_SERVICE_JSON` | the whole JSON, on a single line |

Then run **`./deploy.sh backend`** (from the repo root) to redeploy the server.

Once those are set, `server/apppush.py` sends every bell notification to
registered phones through FCM, so both Android and iOS/desktop push work
end-to-end. Until then it logs one warning and skips gracefully.

Optional Android build (developer app):
   Project settings → Your apps → Add app → Android, package `com.insforge.labcare`
   → download `google-services.json` and place it in `apps/android/`.
