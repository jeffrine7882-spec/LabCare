# LabCare — Complaint & Breakdown Management

Lab equipment complaints and breakdown tracking with role-based login
(Admin / Technician / Customer), sound alerts, attachments, PDF service
reports, QR customer portal, preventive maintenance and an audit log.

## Project layout

```
labcare/
├── server/
│   ├── app.py          # Flask REST API + static web-app serving
│   ├── database.py     # Postgres (InsForge) + SQLite fallback, migrations, helpers
│   ├── report.py       # ReportLab PDF generation
│   ├── mailer.py       # email outbox (SMTP or local outbox.log)
│   ├── seed.py         # first-run seed (Master System Admin only)
│   ├── seed_minimal.py # reset the database to the minimal demo dataset
│   ├── run.py          # production entry point (Waitress)
│   └── wsgi.py         # WSGI entry point for external servers
├── static/
│   ├── index.html      # mobile web app shell
│   ├── app.js          # client logic
│   ├── styles.css      # responsive styles (mobile + desktop)
│   ├── manifest.json   # PWA manifest
│   ├── vercel.json     # InsForge hosting rewrites (/api -> compute, /portal)
│   └── icons/icon.svg
├── Dockerfile          # container build for InsForge compute
└── requirements.txt
```

## Backend (InsForge)

LabCare runs on **InsForge**: PostgreSQL (`database.insforge.app`) for all data,
plus a Flask container on InsForge **compute**. The same `database.py` keeps
working against plain SQLite for local development.

- **Frontend (InsForge hosting):** `https://labcare.insforge.site`
  (static app; `/api/*` is rewrite-proxied to the compute container below).
- **Compute container (Flask API, also serves static):**
  `https://labcare-api-ee5bd3a7-8f78-4005-87ea-6c57ff5728aa.fly.dev`
- **Postgres:** host `yj675q8e.ap-southeast.database.insforge.app` (region
  `ap-southeast`). The container connects using `LABCARE_DATABASE_URL`
  (= InsForge `db connection-string`).
- **Fresh data policy:** the database starts empty and is seeded with only the
  **Master System Admin** account. Customers, users, equipment and tickets are
  created in-app.
- **Minimal demo dataset:** `server/seed_minimal.py` wipes every table and
  recreates a single organisation (one tenant admin, one technician, one
  customer user, one piece of equipment, one complaint, one breakdown and one
  portal link) — run it with the InsForge connection string:
  `LABCARE_DATABASE_URL="$(npx -y @insforge/cli db connection-string)" python3 server/seed_minimal.py`.
- **Redeploy:** `./deploy.sh` (backend), `./deploy.sh frontend` (frontend),
  `./deploy.sh all` (both), `./deploy.sh push` (git). The script recovers
  automatically from a fresh sandbox (installs flyctl, re-fetches the Postgres
  connection string, rewrites `.env.production`).

## Running locally (development)

```bash
cd labcare/server
python3 app.py            # Flask dev server on http://0.0.0.0:8000
```

A fresh database is created and seeded (Master System Admin only) on first run.
To run against the InsForge Postgres instead of local SQLite:

```bash
cd labcare/server
export LABCARE_DATABASE_URL="postgresql://postgres:…@yj675q8e.ap-southeast.database.insforge.app:5432/insforge?sslmode=require"
python3 app.py            # Flask dev server on http://0.0.0.0:8000
```

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

### Environment variables

| Variable               | Default                       | Purpose                                   |
| ---------------------- | ----------------------------- | ----------------------------------------- |
| `PORT`                 | `8000`                        | listen port                               |
| `HOST`                 | `0.0.0.0`                     | bind address                              |
| `LABCARE_DB`           | `<server>/labcare.db`         | SQLite database file (used when no Postgres URL is set) |
| `LABCARE_DATABASE_URL` / `DATABASE_URL` | *(unset = SQLite)* | PostgreSQL connection string — point at InsForge Postgres |
| `LABCARE_TICKET_CAP`   | `2000`                        | max tickets per type (FIFO)               |
| `LABCARE_HISTORY_LOG`  | `<server>/ticket_history.log` | append-only archive of evicted tickets    |
| `LABCARE_PORTAL_URL`   | `https://labcare.insforge.site` | public base URL encoded into QR codes |

## Demo accounts

> These are for the built-in demo of the app. The login screen intentionally
> does **not** show them — they're documented here so a fresh setup can sign in.

Password for the account below is **`Demo123!`**.

| Role                | Email             |
| ------------------- | ----------------- |
| Master System Admin | admin@labcare.com |

The InsForge database starts **fresh**: only the Master System Admin exists.
Create customer organisations, then their tenant admins, technicians, users,
locations, departments, equipment and tickets in-app under **Admin → Customer
organisations** and **Admin → Team & users**.

## Key behaviour

- **Roles & multi-tenant scoping**: one shared database with strict
  `customer_id` scoping.
  - **Master System Admin** — a single, identity-bound account
    (`admin@labcare.com`) that sees and manages everything: customers,
    categories, onboarding/join requests and all users. Only this account can
    create or edit admin accounts, and only it can create new customer
    organisations. It can never be disabled, demoted or linked to a customer.
    Every other admin is a tenant admin.
  - **Tenant admin** (`admin` linked to one `customer_id`) manages only their
    own customer — its complaints, breakdowns, equipment, locations,
    departments, PM schedules and team members. Their scope is exactly that one
    organisation: they **cannot** create further customer organisations, cannot
    create or edit any admin account, cannot see or edit other organisations'
    data, and can only assign work to their own team or LabCare's provider
    technicians.
  - **Technician**: provider technicians (`customer_id NULL`) work across all
    customers; tenant technicians (`customer_id` set) are restricted to their
    customer.
  - **Customer** users are restricted to their own organisation, location and
    department.
  - The master can create tenant admins/technicians directly (via Team & users)
    and approve self-sign-ups; tenant admins can only create technicians and
    customer users for their own customer.
- **Responsible tenant admin**: every user, piece of equipment, complaint and
  breakdown carries an explicit `responsible_admin_id` — the tenant admin
  responsible for that record. The master (and provider staff) see a
  *Responsible tenant admin* picker on each form and the choice is validated
  server-side (must be an active admin of the record's organisation). When a
  customer has exactly one tenant admin it is filled in automatically; with
  several, one must be chosen explicitly; tenant admins/technicians are always
  assigned automatically (themselves or their organisation's admin).
- **Ticket numbering**: complaints `CMP-0001…`, breakdowns `BRK-0001…`.
- **FIFO storage**: each ticket type is capped (default 2000). The oldest
  tickets roll off into `ticket_history.log` (JSON lines) so nothing is lost.
- **History / audit log**: every ticket records who did what and when.
- **Attachments**: photos, PDF and Office documents (max 8 MB).
- **Sound + email alerts** for new tickets and updates.
- **Desktop push alerts**: every bell notification can also ring as a real
  system notification via Web Push (service worker + VAPID), so users hear the
  alert even when the app/tab/browser window is closed. Each signed-in user
  opts in per device from **Menu → Alerts & sound → Push notifications**; the
  subscription is stored against their account and respects the per-user sound
  preference. The VAPID keypair lives in [`server/vapid.json`](server/vapid.json)
  (override with `LABCARE_VAPID_PRIVATE`; the public key for clients is derived
  from it). `pywebpush` sends one push per recipient whenever `notify()` runs.

## Data

- **InsForge Postgres** — all relational data in production (see
  [`.insforge/project.json`](.insforge/project.json); connection string via
  `npx -y @insforge/cli db connection-string`).
- `labcare.db` — single-file SQLite database used **only** for local dev when
  no `LABCARE_DATABASE_URL` is set.
- `ticket_history.log` — append-only JSONL archive of tickets evicted by the
  FIFO cap (written to the container's working dir `/app/server` at runtime,
  or next to `server/` locally).
- `outbox.log` — email outbox when SMTP is not configured.
