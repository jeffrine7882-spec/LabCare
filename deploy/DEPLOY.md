# Deploying LabSynch on Netlify (labcareassist.netlify.app)

You chose to use the **`labcareassist.netlify.app`** address. Because `*.netlify.app`
is served by Netlify itself, the final architecture is:

```
 Browser ──HTTPS──► Netlify (frontend: static/)          ──► labcareassist.netlify.app
                        │  /api/*  (netlify.toml proxy)
                        ▼
                Your VPS (backend: Flask + Waitress + SQLite)   (https://...)
```

**Netlify hosts the web app; your server hosts the backend.** Two steps:

1. Deploy the backend to a server with a public HTTPS URL.
2. Deploy the frontend to Netlify and set the API proxy target.

---

## Step 1 — Backend on a VPS (choose Docker or systemd)

> **Free-server walkthroughs (pick either):**
> - **`deploy/ORACLE-FREE.md`** — Oracle Cloud Always Free ($0/mo, **Singapore
>   region**, ARM A1 + 200 GB disk) — the best free option for MY latency.
> - **`deploy/GCE-FREE.md`** — Google Cloud always-free `e2-micro` VM ($0/mo,
>   US region) with nginx + Let's Encrypt.
>
> Both include the free DuckDNS-domain path for HTTPS.

### Option 1A: Docker (fastest)

```bash
# on your server, from the project root
docker build -t labcare-backend -f deploy/Dockerfile .
docker run -d --name labcare \
  -p 127.0.0.1:8000:8000 \
  -e LABCARE_SECURE_COOKIES=1 \
  -v /opt/labcare-data:/data \
  -e LABCARE_DB=/data/labcare.db \
  -e LABCARE_HISTORY_LOG=/data/ticket_history.log \
  labcare-backend
```

### Option 1B: systemd (no Docker)

```bash
# on your server, from the project root
sudo mkdir -p /opt/labcare && sudo cp -r server static /opt/labcare/
cd /opt/labcare && python3 -m venv venv && venv/bin/pip install -r requirements.txt

sudo mkdir -p /var/lib/labcare && sudo chown www-data:www-data /var/lib/labcare

sudo cp deploy/labcare.service /etc/systemd/system/labcare.service
sudo systemctl daemon-reload && sudo systemctl enable --now labcare
sudo systemctl status labcare
```

The unit already sets `ExecStart=/opt/labcare/venv/bin/python3 run.py`,
`WorkingDirectory=/opt/labcare/server`, and stores data in `/var/lib/labcare/`
(`labcare.db` + `ticket_history.log`), with `LABCARE_SECURE_COOKIES=1` on.

Verify the backend directly (HTTP, before TLS):

```bash
curl -s http://127.0.0.1:8000/api/ping      # → {"ok": true, "db": "ok"}
```

### Step 2 — Put nginx + TLS in front of the backend

You need a **valid public HTTPS URL** for Netlify to proxy to. You **must have
your own domain** (or a subdomain of one): `labcareassist.netlify.app` is owned
by Netlify and cannot be pointed at your VPS or get a cert there.

```bash
# 0. Point a DNS record at your server first, e.g.
#      api.labcare.mycompany.com   A    <your server IP>

sudo apt update && sudo apt install -y nginx certbot python3-certbot-nginx

# edit deploy/nginx-labcare.conf:
#   - replace BOTH "BACKEND-DOMAIN.example.com" with your real domain

sudo cp deploy/nginx-labcare.conf /etc/nginx/sites-available/labcare
sudo ln -s /etc/nginx/sites-available/labcare /etc/nginx/sites-enabled/labcare
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d api.labcare.mycompany.com      # automatic TLS
```

Check it works (from anywhere on the internet):

```bash
curl -s https://api.labcare.mycompany.com/api/ping     # → {"ok":true,"db":"ok"}
```

> **No domain?** Any host behind HTTPS works — e.g. a Render/Railway deployment
> of the same code (see Option 1C). The Netlify proxy needs **HTTPS with a
> valid cert (not self-signed)**, otherwise Netlify's edge will refuse the
> connection.

### Option 1C: PaaS backend — no VPS at all (no server admin)

Point the Netlify `/api` proxy at any PaaS that gives a public HTTPS URL.

**Render** (`deploy/render.yaml` — Blueprint, builds with pip, runs `run.py`,
health-checks `/api/ping`, mounts a 5 GB persistent disk at `/data` for SQLite +
history log, `plan: starter` ~$7/mo + disk):
- Render **Free** shuts down after 15 idle minutes and has **no persistent
  disk**, so it would wipe `labcare.db` — don't use it for real data.
- `plan: starter` is the cheapest instance that can attach the disk the DB needs.

**Railway** (`railway.toml` — build = NIXPACKS, start = `run.py`):
```bash
railway up
# In the dashboard: attach a volume at /data, and add env vars
#   LABCARE_DB=/data/labcare.db
#   LABCARE_HISTORY_LOG=/data/ticket_history.log
#   LABCARE_SECURE_COOKIES=1
```

Both give you a public `https://…onrender.com` / `https://…up.railway.app` URL —
use that HTTPS URL as the `to =` target in `netlify.toml`.

---

## Step 2 (frontend) — Deploy to Netlify at labcareassist.netlify.app

1. Edit `netlify.toml` → replace `BACKEND_BASE_URL.example.com` with your backend
   URL **once** (the `to =` value only).
2. Deploy:

```bash
netlify login
netlify deploy --prod --dir=static
# the first deploy prints a *.netlify.app URL; to claim labcareassist.netlify.app:
netlify sites:create --name labcareassist
```

3. (Alternative) connect this Git repo to Netlify — the `[build]` block in
   `netlify.toml` already sets publish dir to `static`, command is a no-op.

The frontend calls only relative `/api/...` URLs, so `netlify.toml`'s
`/api/* → backend/:splat` rule is the whole integration.

---

## Verify end-to-end
1. Open `https://labcareassist.netlify.app`.
2. Sign in (demo: `admin@labcare.com` / `Demo123!`).
3. Create a complaint → it appears instantly (requests go Netlify → backend).
4. Check `https://labcareassist.netlify.app/api/ping` (proxied) returns
   `{"ok": true, "db": "ok"}`.

## Operational notes

| Concern | Action |
| --- | --- |
| Backups | `labcare.db` + `ticket_history.log` (the `/data` volume or `/opt/labcare/server`) |
| Session cookie | Backend runs with `LABCARE_SECURE_COOKIES=1`; cookie is `Secure`, sent over TLS only |
| QR portal URLs | Set `LABCARE_PORTAL_URL=https://labcareassist.netlify.app` so QR codes encode the public frontend URL (otherwise scans open the backend's own/derived host) |
| Uploads | In-app cap 8 MB; Netlify proxy forwards body; nginx `client_max_body_size 12m` |
| Backend uptime | `docker restart policy` or systemd `Restart=always` — already configured |

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| **Cannot sign in after pushing to Netlify** | The API backend is not connected. Check `https://labcareassist.netlify.app/api/ping` — a **502** means the `/api/*` proxy in `netlify.toml` still has the placeholder, or the backend host is down. Write the real backend HTTPS URL into the `to =` line, redeploy, and re-test. The app now shows a ⚠️ "Backend not connected" banner when this happens |
| 502/504 from `/api/*` | Backend service down — `systemctl status labcare` or `docker ps`; probe `curl http://127.0.0.1:8000/api/ping`. On Render/Railway check the service health + logs |
| Certificate expiry | `certbot renew` is automatic; verify with `systemctl list-timers` |
| Changes not showing | Static assets are cache-busted (`?v=N`), but hard-refresh (Ctrl/Cmd-Shift-R) if needed |

> ⚠️ Netlify proxy target must be a **valid, public HTTPS URL** (Netlify's edge
> rejects self-signed backends), and it is written **literally** into the
> `to = "..."` line — Netlify redirects cannot read environment variables.

## Files

```
static/                      # Netlify frontend (publish dir)
netlify.toml                 # Netlify build + /api proxy + SPA fallback
.gitignore                   # keeps DB / logs / env out of Git
deploy/Dockerfile            # backend image (VPS / container hosts)
deploy/labcare.service       # backend as a systemd service
deploy/nginx-labcare.conf    # nginx TLS + reverse proxy for the backend
deploy/ORACLE-FREE.md        # $0/mo walkthrough: Oracle Always Free (Singapore)
deploy/GCE-FREE.md           # $0/mo walkthrough: GCP always-free e2-micro VM
deploy/render.yaml           # Render PaaS backend (no VPS needed)
railway.toml                 # Railway PaaS backend (no VPS needed)
server/run.py                # Waitress entry point
requirements.txt
```
