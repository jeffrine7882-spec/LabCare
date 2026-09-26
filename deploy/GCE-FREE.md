# LabSynch on Google Cloud — Always-Free e2-micro VPS ($0/mo)

Run the LabSynch backend for free on Google Cloud's always-free VM and point
Netlify's `/api/*` proxy at it. No code changes; SQLite runs on a real local
disk (the recommended way to run SQLite in GCP).

Cost: **$0/mo forever** (as long as you stay inside the free limits below).
Region note: free VM lives in the **US** (us-west1 / us-central1 / us-east1),
so add ~150–250 ms latency for Penang users — negligible for a ticket app.

```
 Browser ──► Netlify (frontend)  ──► labcareassist.netlify.app
                 │  /api/*  (netlify.toml)
                 ▼
           https://<name>.duckdns.org  (nginx + Let's Encrypt)
                 │
           GCE e2-micro VM → Flask/Waitress on 127.0.0.1:8000 → labcare.db
```

---

## 0. What you need before starting

1. A **Google Cloud account** (billing account with a card — required even for
   free tier, but you won't be charged if you stay inside limits).
2. *(optional)* a domain you own. If you don't have one, use a **free DuckDNS
   subdomain** (`<name>.duckdns.org`) — fully supported in this guide.
3. Your project code gets onto the VM via `git clone` (if your repo is on
   GitHub/GitLab) or `scp`.

> ⚠️ Free-tier rules that keep this $0 — do not break them:
> - exactly **one** `e2-micro` instance, in **us-west1 / us-central1 / us-east1**
> - boot disk = **Standard persistent disk, ≤ 30 GB** (SSD/Balanced are NOT free)
> - 1 GB egress/month (fine for a small team; don't stream files through it)
> - never upgrade the machine type or add a second VM

---

## 1. Create the VM (console)

1. Console → **Compute Engine → Create instance**.
2. Name: `labcare`. Region/zone: `us-central1` (or us-west1/us-east1).
3. **Machine type:** `e2-micro` (scroll to the bottom of the list).
4. **Boot disk:** *Change* → Ubuntu **24.04 LTS**, size **30 GB**, type
   **Standard persistent disk**.
5. **Firewall:** tick **Allow HTTP traffic** and **Allow HTTPS traffic**.
6. Click **Create**.

### Reserve a static external IP (free, keeps your URL stable)

- VPC network → **IP addresses** → **Reserve external static address**
  (region us-central1 → attach to `labcare`). An in-use static IP is free
  on the e2-micro free tier.

---

## 2. Open the firewall (if "Allow HTTP/HTTPS" was missed)

- **VPC network → Firewall → Create firewall rule**
  - Name `allow-http-https`, targets `All instances`,
    source `0.0.0.0/0`, protocols **tcp:80,443**.
- SSH (tcp:22) is normally pre-allowed via the `default-allow-ssh` rule.

---

## 3. SSH into the VM and install the app (venv + systemd)

```bash
gcloud compute ssh labcare --zone=us-central1-a
# or use the "SSH" button in the Console (opens a browser terminal)

# base packages
sudo apt update && sudo apt install -y python3-venv python3-pip nginx git

# get the code (pick ONE):
#   A) git clone your repo        →  cd labcare
#   B) scp from your laptop: run this ON YOUR LAPTOP, then continue on the VM
#        scp -r server static requirements.txt deploy run.py \
#            <your-ssh-username>@<VM-IP>:/home/<user>/labcare

# on the VM:
sudo mkdir -p /opt/labcare
sudo cp -r server static /opt/labcare/
sudo cp requirements.txt /opt/labcare/

cd /opt/labcare
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

# data dir (SQLite DB + logs live here → easy to back up)
sudo mkdir -p /var/lib/labcare
sudo chown www-data:www-data /var/lib/labcare

# systemd service (already configured for venv + secure cookies)
sudo cp deploy/labcare.service /etc/systemd/system/labcare.service
sudo systemctl daemon-reload
sudo systemctl enable --now labcare
sudo systemctl status labcare        # should show "active (running)"

# smoke test BEFORE TLS:
curl -s http://127.0.0.1:8000/api/ping
#   → {"db":"ok","ok":true}
```

The service runs as `www-data` with `LABCARE_SECURE_COOKIES=1` and stores the
DB at `/var/lib/labcare/labcare.db`.

---

## 4. Point a hostname at the VM

### Option A — free: DuckDNS subdomain

1. https://www.duckdns.org → sign in (Google/GitHub) → create `<name>.duckdns.org`.
2. Next to the domain, paste your VM's **static external IP** and click *update ip*.
   (You can leave the IP blank and DuckDNS auto-detects it.)
3. Since your GCE IP is static, this is set-and-forget. Optional auto-refresh:

```bash
sudo tee /etc/cron.d/duckdns >/dev/null <<'EOF'
*/5 * * * * root curl -s "https://www.duckdns.org/update?domains=<name>&token=<TOKEN>&ip=" >/dev/null
EOF
```

### Option B — you own a domain

At your DNS provider create an A record:
`api.labcare.yourcompany.com  →  <VM static IP>`

---

## 5. nginx + Let's Encrypt TLS

```bash
# replace SERVERNAME below with (A) <name>.duckdns.org  or  (B) your domain
SERVERNAME=<name>.duckdns.org

# fix placeholder in the committed nginx config
sudo cp deploy/nginx-labcare.conf /etc/nginx/sites-available/labcare
sudo sed -i "s/BACKEND-DOMAIN.example.com/$SERVERNAME/g" \
            /etc/nginx/sites-available/labcare
sudo ln -s /etc/nginx/sites-available/labcare /etc/nginx/sites-enabled/labcare
sudo nginx -t && sudo systemctl reload nginx

# TLS (HTTP-01 works because port 80 is open)
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d "$SERVERNAME" -m you@example.com --agree-tos -n
# then edit the ssl_certificate lines certbot wrote, OR rerun a full reload:
sudo nginx -t && sudo systemctl reload nginx
```

Verify from *outside* the VM (i.e. from your laptop or any browser):

```bash
curl -s https://$SERVERNAME/api/ping
#   → {"db":"ok","ok":true}     ← this is the "backend is live" check
```

> Note: `deploy/nginx-labcare.conf` already has `ssl_certificate` lines under
> the Let's Encrypt paths — running `certbot --nginx` above fills them in.
> If certbot can't find a matching server block, just run certbot first, then
> `sudo cp deploy/nginx-labcare.conf ...` with the paths certbot uses
> (`/etc/letsencrypt/live/$SERVERNAME/`).

---

## 6. Point Netlify at the backend

1. **Paste your backend URL to me** (e.g. `https://labcare-myvm.duckdns.org`)
   and I'll write it into `netlify.toml` — or do it yourself:
   `netlify.toml` → the `/api/*` redirect → change
   `to = "https://BACKEND_BASE_URL.example.com/api/:splat"`
   to
   `to = "https://<name>.duckdns.org/api/:splat"`
2. Redeploy the frontend (push to the connected repo, or
   `netlify deploy --prod --dir=static`).

### Verify end-to-end

```bash
curl -s https://labcareassist.netlify.app/api/ping     # → {"db":"ok","ok":true}
```

Then sign in at `https://labcareassist.netlify.app` with
`admin@labcare.com` / `Demo123!`.

---

## 7. Backups & ops

```bash
# one-liner snapshot of everything that matters (DB + ticket history log):
sudo tar czf /var/lib/labcare/backup-$(date +%F).tgz -C /var/lib/labcare labcare.db ticket_history.log
# copy it off the VM occasionally:
gcloud compute scp labcare:/var/lib/labcare/backup-*.tgz ./ --zone=us-central1-a   # from laptop

sudo systemctl restart labcare     # restart the app
journalctl -u labcare -n 50        # read app logs
sudo certbot renew --dry-run       # confirm auto-renewal works
```

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `curl localhost/api/ping` fine but public URL times out | Firewall rule missing — allow tcp:80,443 (step 2); check DuckDNS points at the **static** IP |
| Netlify `/api/ping` → 502 | Backend down: `systemctl status labcare`; or `netlify.toml` URL still placeholder |
| `certbot` fails | Port 80 not reachable (firewall) or wrong DuckDNS IP |
| Site slow from MY | Expected: free VM is in the US (~200 ms). Acceptable for an internal ticketing app |
| Billed unexpectedly | You broke a free-tier rule — verify machine `e2-micro`, disk **Standard ≤30 GB**, single VM, US region |
| Uploads >8 MB rejected | nginx `client_max_body_size 12m` is set; in-app cap is 8 MB |

---

## If you ever outgrow the free VM

Move the same code to a **Singapore** host to cut latency for Penang users:
- Render Starter (`deploy/render.yaml`, ~$8/mo, persistent disk) or
- a bigger GCE/DigitalOcean VM in asia-southeast1 (~$6/mo).
The only thing that changes is the backend URL in `netlify.toml` — the app
and data move as-is.
