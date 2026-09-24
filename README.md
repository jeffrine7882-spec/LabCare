# LabCare — Complaint & Breakdown Management

Lab equipment complaints and breakdown tracking with role-based login
(Admin / Technician / Customer), sound alerts, attachments, PDF service
reports, QR customer portal, preventive maintenance and an audit log.

## Project layout

```
labcare/
├── server/
│   ├── app.py          # Flask REST API + static web-app serving
│   ├── database.py     # SQLite schema, migrations, helpers
│   ├── report.py       # ReportLab PDF generation
│   ├── mailer.py       # email outbox (SMTP or local outbox.log)
│   ├── seed.py         # demo data (first run only)
│   ├── run.py          # production entry point (Waitress)
│   └── wsgi.py         # WSGI entry point for external servers
├── static/
│   ├── index.html      # mobile web app shell
│   ├── app.js          # client logic
│   ├── styles.css      # responsive styles (mobile + desktop)
│   ├── manifest.json   # PWA manifest
│   └── icons/icon.svg
└── requirements.txt
```

## Running locally (development)

```bash
cd labcare/server
python3 app.py            # Flask dev server on http://0.0.0.0:8000
```

A fresh database is created and seeded on first run.

## Running in production

```bash
cd labcare/server
pip3 install -r ../requirements.txt
python3 run.py            # Waitress (production WSGI) on 0.0.0.0:8000
```

or with any WSGI server:

```bash
cd labcare/server
waitress-serve --listen=0.0.0.0:8000 wsgi:application
# or
gunicorn -b 0.0.0.0:8000 wsgi:application
```

### Reverse proxy + HTTPS (nginx / Let's Encrypt)

See [`deploy/DEPLOY.md`](deploy/DEPLOY.md) — includes an nginx site config
(`deploy/nginx-labcare.conf`) and a systemd unit (`deploy/labcare.service`)
for a full production setup. Summary: run the app with systemd on
`127.0.0.1:8000`, put nginx in front, and run `certbot --nginx -d your.host`
for a free auto-renewing TLS certificate. Set `LABCARE_SECURE_COOKIES=1`
(recommended behind HTTPS) so session cookies are marked `Secure`.

### Deploying on Netlify (frontend) + backend VPS

The chosen production target is `https://labcareassist.netlify.app`:

- **Netlify** hosts the static frontend (`static/` is the publish dir; see
  [`netlify.toml`](netlify.toml) which proxies all `/api/*` calls to your
  backend).
- **Your server** runs the Flask + Waitress backend (Docker image in
  [`deploy/Dockerfile`](deploy/Dockerfile), or the systemd unit) behind nginx
  with `certbot` TLS.

Full step-by-step: [`deploy/DEPLOY.md`](deploy/DEPLOY.md).

### Environment variables

| Variable               | Default                       | Purpose                                   |
| ---------------------- | ----------------------------- | ----------------------------------------- |
| `PORT`                 | `8000`                        | listen port                               |
| `HOST`                 | `0.0.0.0`                     | bind address                              |
| `LABCARE_DB`           | `<server>/labcare.db`         | SQLite database file                      |
| `LABCARE_TICKET_CAP`   | `2000`                        | max tickets per type (FIFO)               |
| `LABCARE_HISTORY_LOG`  | `<server>/ticket_history.log` | append-only archive of evicted tickets    |
| `LABCARE_PORTAL_URL`   | *(derived from request)*      | public base URL encoded into QR codes, e.g. `https://labcareassist.netlify.app` |

## Demo accounts

> These are for the built-in demo of the app. The login screen intentionally
> does **not** show them — they're documented here so a fresh setup can sign in.

Password for all demo accounts is **`Demo123!`**

| Role       | Email               |
| ---------- | ------------------- |
| Admin      | admin@labcare.com   |
| Technician | aidil@labcare.com   |
| Customer   | kavita@bioref.com   |

## Key behaviour

- **Roles**: Admin, Technician, Customer — customers are restricted to their
  own organisation, location and department.
- **Ticket numbering**: complaints `CMP-0001…`, breakdowns `BRK-0001…`.
- **FIFO storage**: each ticket type is capped (default 2000). The oldest
  tickets roll off into `ticket_history.log` (JSON lines) so nothing is lost.
- **History / audit log**: every ticket records who did what and when.
- **Attachments**: photos, PDF and Office documents (max 8 MB).
- **Sound + email alerts** for new tickets and updates.

## Data

- `labcare.db` — single-file SQLite database shared by all users.
- `ticket_history.log` — append-only JSONL archive of tickets evicted by the
  FIFO cap.
- `outbox.log` — email outbox when SMTP is not configured.
