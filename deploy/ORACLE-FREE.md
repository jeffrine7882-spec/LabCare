# LabSynch on Oracle Cloud — Always Free ($0/mo, Singapore)

Run the LabSynch backend + SQLite for **free, forever** on Oracle Cloud's
Always Free tier. Oracle is the best free option for LabSynch because it has
**Singapore** regions (~20–40 ms from Penang, vs. GCP's US-only ~200 ms) and a
huge allowance: **200 GB block storage** + 10 TB/month egress. No code changes;
SQLite runs on a real local disk.

```
 Browser ──► Netlify (frontend)  ──► labcareassist.netlify.app
                 │  /api/*  (netlify.toml proxy)
                 ▼
           https://<name>.duckdns.org  (nginx + Let's Encrypt)
                 │
        Oracle A1 ARM VM (2 OCPU / 12 GB) → Flask/Waitress :8000 → labcare.db
```

---

## 0. Current Always Free allowance (verified Aug 2026)

| Resource | Free allowance |
| --- | --- |
| ARM **VM.Standard.A1.Flex** | **2 OCPU + 12 GB RAM** total *(halved from 4/24 in mid-2026)* |
| AMD **VM.Standard.E2.1.Micro** | 2× (1/8 OCPU, 1 GB RAM each) |
| Block/boot storage | **200 GB** total (one data block volume + boot) |
| Egress | 10 TB/month |
| Static IP | 1 reserved public IPv4 free |

For LabSynch, a single **A1 1 OCPU / 6 GB** instance is already many times more
than the app needs (Flask+Waitress+SQLite idles around ~150 MB). Picking 1/6
instead of 2/12 also keeps utilisation % higher, which matters for the idle
reclaim rule (see §9).

> ⚠️ **Signup requires a credit card** (like GCP/AWS). Always Free resources are
> not charged — but the card is mandatory. Oracle's free signup can occasionally
> reject a card; retrying once or twice usually works.

---

## 1. Sign up + pick home region

1. Go to https://www.oracle.com/cloud/free/ → **Start for free**.
2. Fill in details, verify card. **Home region: Singapore (`ap-singapore-1`)**
   — it's permanent; choose carefully. (If Singapore shows "out of capacity"
   for ARM later, Osaka/Tokyo are good fallbacks.)

---

## 2. Create the VM instance

1. Console → **Compute → Instances → Create instance**.
2. Name: `labcare`.
3. **Image:** Ubuntu → **Canonical Ubuntu 24.04 LTS (aarch64)** — make sure the
   image is tagged **"Always Free-eligible"**.
4. **Shape:** *Change shape* → **Ampere** → **VM.Standard.A1.Flex**
   (if ARM shows "Out of host capacity", retry later, or use the
   **AMD VM.Standard.E2.1.Micro** — 1 GB RAM is enough for Flask+SQLite).
   - Set **1 OCPU / 6 GB RAM** (or 2/12 if you prefer headroom).
5. **Boot volume:** keep default (50 GB is fine; 200 GB total allowed).
6. **SSH keys:** download/generate a key pair — OCI uses **SSH key auth only**
   (no password). Use an already-uploaded key or paste a public key.
7. **VCN/subnet:** leave the suggested defaults (it auto-creates a VCN).
8. Click **Create**.

> A1 note: ARM CPU counts are labelled **OCPU** (1 OCPU = 1 full vCPU core,
> unlike the fractional AMD micro).

---

## 3. Open ports (TWO firewall layers — most common trip-up)

Oracle has **two** firewalls. Both must be open, or traffic silently drops.

### Layer 1 — OCI Security List (console)

1. **Networking → Virtual Cloud Networks → `<your-vcn>` → Security Lists →
   Default Security List → Add Ingress Rules**.
2. Add three rules (Source type **CIDR**, Source **0.0.0.0/0**):

| Protocol | Destination port | Purpose |
| --- | --- | --- |
| TCP | 22 | SSH |
| TCP | 80 | HTTP (certbot challenge) |
| TCP | 443 | HTTPS |

### Layer 2 — OS firewall (iptables, on the VM)

OCI's Ubuntu server images usually start with an empty/ACCEPT policy, but some
ship restrictive `iptables` rules. Check and fix:

```bash
sudo iptables -L INPUT -n | head -20
# if you see a default DROP + only :22 allowed, allow 80/443:
sudo iptables -I INPUT -p tcp --dport 80  -m state --state NEW -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -m state --state NEW -j ACCEPT
# persist:
sudo apt install -y iptables-persistent && sudo netfilter-persistent save
```

> Do **not** also enable `ufw` — running two user-space firewalls confuses the
> picture. Use the OCI security list + iptables (or just the security list).

---

## 4. SSH in and install the app (venv + systemd)

```bash
ssh -i ~/.ssh/<your-key> ubuntu@<VM-public-IP>
# (or the Console's "Cloud Shell" / browser SSH)

sudo apt update && sudo apt install -y python3-venv python3-pip nginx git

# get the code — git clone your repo, or scp from your laptop (see GCE-FREE.md)
sudo mkdir -p /opt/labcare
sudo cp -r server static /opt/labcare/
sudo cp requirements.txt /opt/labcare/

cd /opt/labcare
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

# data dir (SQLite DB + ticket history log → easy to back up)
sudo mkdir -p /var/lib/labcare
sudo chown www-data:www-data /var/lib/labcare

# systemd service (already configured for venv + secure cookies)
sudo cp deploy/labcare.service /etc/systemd/system/labcare.service
sudo systemctl daemon-reload
sudo systemctl enable --now labcare
sudo systemctl status labcare        # → "active (running)"

# smoke test BEFORE TLS:
curl -s http://127.0.0.1:8000/api/ping
#   → {"db":"ok","ok":true}
```

The committed `deploy/labcare.service` runs as `www-data`, uses
`/opt/labcare/venv/bin/python3 run.py`, and stores the DB at
`/var/lib/labcare/labcare.db` with `LABCARE_SECURE_COOKIES=1`.

---

## 5. Point a hostname at the VM

### Option A — free: DuckDNS subdomain (still works in 2026)

1. https://www.duckdns.org → sign in (Google/GitHub) → create `<name>.duckdns.org`.
2. Set its A record to your VM's **public IP**. To keep the IP stable, reserve a
   free static IP first: **Networking → IP Management → Reserved Public IPs →
   Reserve** (an in-use reserved IP is free), then attach it to the instance.

```bash
# optional auto-refresh (redundant with a static IP, but harmless):
sudo tee /etc/cron.d/duckdns >/dev/null <<'EOF'
*/5 * * * * root curl -s "https://www.duckdns.org/update?domains=<name>&token=<TOKEN>&ip=" >/dev/null
EOF
```

### Option B — you own a domain

At your DNS provider: `api.labcare.yourcompany.com → <VM public IP>` (A record).

---

## 6. nginx + Let's Encrypt TLS

```bash
SERVERNAME=<name>.duckdns.org      # or your own domain

sudo cp deploy/nginx-labcare.conf /etc/nginx/sites-available/labcare
sudo sed -i "s/BACKEND-DOMAIN.example.com/$SERVERNAME/g" \
            /etc/nginx/sites-available/labcare
sudo ln -s /etc/nginx/sites-available/labcare /etc/nginx/sites-enabled/labcare
sudo nginx -t && sudo systemctl reload nginx

sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d "$SERVERNAME" -m you@example.com --agree-tos -n
sudo nginx -t && sudo systemctl reload nginx
```

Verify from OUTSIDE the VM (your laptop / Netlify):

```bash
curl -s https://$SERVERNAME/api/ping
#   → {"db":"ok","ok":true}     ← the "backend is live" check
```

---

## 7. Point Netlify at the backend

1. **Send me your backend URL** (e.g. `https://labcare-sg.duckdns.org`) and I'll
   put it in `netlify.toml` — or do it yourself: in `netlify.toml`, change the
   `/api/*` redirect's
   `to = "https://BACKEND_BASE_URL.example.com/api/:splat"`
   to
   `to = "https://<name>.duckdns.org/api/:splat"`.
2. Redeploy the frontend (push to the connected repo / `netlify deploy --prod --dir=static`).

### Verify end-to-end

```bash
curl -s https://labcareassist.netlify.app/api/ping   # → {"db":"ok","ok":true}
```

Then sign in at https://labcareassist.netlify.app with `admin@labcare.com` /
`Demo123!`.

---

## 8. Backups

```bash
sudo tar czf /var/lib/labcare/backup-$(date +%F).tgz -C /var/lib/labcare labcare.db ticket_history.log
# copy off the VM occasionally:
scp -i ~/.ssh/<key> ubuntu@<IP>:/var/lib/labcare/backup-*.tgz ./
```

---

## 9. Prevent "idle instance" reclamation (important)

Oracle reclaims Always Free instances it deems **idle over 7 days**
(CPU < 20% **and** network < 20% **and** memory < 20%-of-shape). An
internal-only tool like LabSynch can nearly reach that on a quiet week. Cheap
insurance — a keep-alive cron that curls your public URL (external traffic
counts toward the network metric) and does a couple of seconds of CPU work:

```bash
sudo tee /usr/local/bin/labcare-keepalive >/dev/null <<'EOF'
#!/bin/bash
# generates modest CPU + real external network traffic so the VM never
# looks "idle" to Oracle's reclaimer. Runs every 5 minutes (see cron below).
curl -sf -o /dev/null "https://<name>.duckdns.org/api/ping"
curl -sf -o /dev/null "https://www.google.com/generate_204"
openssl speed -seconds 2 aes-128-cbc >/dev/null 2>&1
EOF
sudo chmod +x /usr/local/bin/labcare-keepalive

sudo tee /etc/cron.d/labcare-keepalive >/dev/null <<'EOF'
*/5 * * * * root /usr/local/bin/labcare-keepalive >/dev/null 2>&1
EOF
```

Also, Oracle normally emails a warning **before** reclaiming — if you ever get
one, just SSH in (or open the app) and reply/act.

> Extra safety margin: use the **1 OCPU / 6 GB** A1 (not 2/12) — the same app
> load is then a higher % of CPU/RAM, staying above the idle thresholds more
> easily. Or use the AMD micro, where the memory criterion doesn't apply.

---

## 10. Troubleshooting

| Symptom | Fix |
| --- | --- |
| "Out of host capacity" for A1 | Retry (mornings SG time), switch AZ, or use the AMD micro shape |
| `curl` works on VM but not from outside | One of the TWO firewalls is closed — §3 (OCI security list AND iptables) |
| Netlify `/api/ping` → 502 | Backend down (`systemctl status labcare`) or `netlify.toml` URL still placeholder |
| certbot fails | Port 80 blocked — fix §3, then `sudo certbot --nginx -d $SERVERNAME` again |
| Instance got stopped/reclaimed | Oracle emailed a warning first; log in and act — data is safe on the block/boot volume |
| Billed unexpectedly | Verify: AMD micro or A1 ≤ 2 OCPU; boot volume(s) total ≤ 200 GB; no extra shapes/IPs |
| Slow first request | ARM is fine; check the app is `active (running)` and you're on the SG region |

---

## If you outgrow the free tier

Same code moves to a bigger box unchanged — e.g. paid OCI A1 in Singapore, or
Render Starter (see `deploy/render.yaml`). Only the URL in `netlify.toml`
changes.
