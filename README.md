# LabCare — Complaint & Breakdown Management

Lab equipment complaints and breakdown tracking with role-based login
(Admin / Technician / Customer), sound alerts, PDF service reports, QR
customer portal, preventive maintenance and an audit log.

## Project layout

```
labcare/
├── server/
│   ├── app.py          # Flask REST API + static web-app serving
│   ├── database.py     # Postgres (InsForge) + SQLite fallback, migrations, helpers
│   ├── report.py       # ReportLab PDF generation
│   ├── mailer.py       # email outbox (SMTP or local outbox.log)
│   ├── seed.py         # first-run seed (Master System Admin only)
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
  **Master System Admin** account. Organizations, users, equipment and tickets are
  created in-app.
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
The master creates **tenant admins** (linked to an organization, or entirely
unlinked — an unlinked tenant admin creates their own organization after
first login). Tenant admins then create their own organizations, users,
locations, departments, equipment and tickets in-app under **Admin →
Organizations** and **Admin → Team & users**.

## Key behaviour

- **Roles & multi-tenant scoping**: one shared database with strict
  `customer_id` scoping.
  - **Master System Admin** — a single, identity-bound account
    (`admin@labcare.com`) that sees and manages everything: organizations,
    categories, onboarding/join requests and all users. Only this account can
    create or edit admin accounts and organizations, and it can never be
    disabled, demoted or linked to an organization. Every other admin is a tenant admin.
    **Only the Master System Admin can delete a complaint or breakdown
    ticket.** The master can also **delete any user account** — deleting a
    user never cascades: their tickets, equipment, PM schedules and other
    records are kept and simply become unlinked/unassigned (history rows
    render the author as "Former user").
  - **Tenant admin** (any admin who is not the Master) manages only the
    organizations in their care list — their locations, departments,
    equipment, tickets, PM schedules and users. The master may create a tenant
    admin **without linking any organization**: after first login that tenant
    admin creates their **own** organization (it is
    automatically added to their care list), then its users (technicians and
    customer accounts), locations, departments and equipment. They **cannot**
    create or edit any admin account, cannot see other organizations' data,
    and can only assign work to their own team or LabCare's provider
    technicians.
  - **Technician**: provider technicians (`customer_id NULL` **and** no linked
    tenant admin — created by the master) work across all organizations; tenant
    technicians (`customer_id` set) are restricted to their organization. A
    technician a tenant admin creates **without** an organization is neither:
    `customer_id` is NULL but `responsible_admin_id` names that tenant admin, so
    they are restricted to **that admin's care list**. This distinction is a
    security boundary, not a convenience — in `tenant_scope()` a NULL
    `customer_id` means "unscoped", i.e. every organization on the platform, so a
    customer-less account with no linked admin would see other tenants' data.
    The link is therefore what defines the scope, and a tenant admin can never
    mint a system-wide account. An unbound tenant admin (no organizations yet)
    has an empty care list, so the accounts they create see nothing at all.
  - **Customer** users are restricted to their own organization, location and
    department.
  - The master can create tenant admins (linked **or** unlinked), technicians
    and customer users directly (via Team & users) and approve self-sign-ups;
    tenant admins can only create technicians and customer users for their own
    organizations — or, for technicians and application accounts, for their **tenant
    as a whole** by leaving the organization empty (see *Responsible tenant
    admin* below). Customer-role accounts always have to name an organization,
    since those are an organization's own people.
- **Responsible tenant admin**: every user, piece of equipment, complaint and
  breakdown carries an explicit `responsible_admin_id` — the tenant admin who
  "cares for" that record. The master (and provider staff) see a *Responsible
  tenant admin* picker on each form and the choice is validated server-side
  (must be an active admin of the record's organization). When an organization has
  exactly one tenant admin it is filled in automatically; with several, one must
  be chosen explicitly; tenant admins/technicians are always assigned
  automatically (themselves or their organization's admin).

  On the **Add user** form a tenant admin sees a *Linked tenant admin* picker
  next to the now-**optional** *Linked organization* — it defaults to themselves and
  lists only their peer admins (`GET /api/tenant-admins` with no `customer_id`,
  which the backend already scopes to the caller's care list). Leaving the
  organization empty places the account under the named admin's care. A tenant
  admin may only name themselves or a peer who cares for at least one of the
  same organizations, so an account cannot be pushed into a tenant they do not
  manage. Ids from these pickers are coerced server-side: browsers send
  `<select>` values as strings, which previously failed an integer care-list
  comparison and rejected a legitimately chosen organization with a 403.
- **Join requests & who cares for a new organization**: anyone can request an
  account from the sign-in screen. **Full name, email and phone number are
  required**, plus a password of at least 6 characters. The request stays
  pending until the master approves or rejects it (`GET /api/onboarding`).

  A request that **creates a new organization** leaves it with no tenant admin,
  so it is flagged `customers.pending_care` and **every tenant admin is asked
  whether it is under their care** — both as a bell notification with inline
  *Take into my care* / *Not mine* buttons and as a *New organizations awaiting
  care* panel on the Organizations screen. The master is told as well and may
  instead **assign** it to a chosen tenant admin.

  - **The first tenant admin to claim it wins.** The claim is a guarded
    `UPDATE … WHERE pending_care=1` whose rowcount decides the winner, so two
    admins answering at once cannot both take it — the second gets a 409.
  - **"Not mine" is per admin** (`pending_care_declines`), so a single decline
    cannot make the request vanish for everybody and strand the organization
    with no owner. The remaining admins see how many peers already declined.
  - Once claimed or assigned the flag is cleared and **ordinary shared care
    resumes** — another tenant admin may still add the organization to their own
    list. Exclusivity applies to the decision only, not to care in general.
  - Signing up against an organization that **already exists** (picked from the
    list, or matched by name) does **not** open a care decision.
  - **Rejecting** a join request withdraws its organization from the pending
    list and tells the admins who were asked. The organization row is kept: it
    already has a location and department, and a later signup naming the same
    organization reuses it.

  Backed by `GET /api/customers/pending-care`, `POST /api/customers/<id>/take-care`,
  `…/decline-care` and `…/assign-care` (master only). Notifications carry
  `care_pending` / `care_claimed_by` so the client stops offering buttons for a
  decision that is already settled.
- **Equipment categories**: the Add equipment form offers a prepared laboratory
  list — *General, Centrifuges, PCR, Cold Storage, Chromatography, Spectroscopy,
  Sterilization, Analyzers, Histology, Other* — installed on first run from
  `DEFAULT_CATEGORIES` in `server/seed.py`. Seeding runs at every startup but
  acts at most once per database: as soon as any prepared category is present
  the list counts as curated and is left exactly as arranged, so a category the
  master deletes is **not** resurrected by the next restart. A database holding
  only hand-made categories still gets the prepared list, because those defaults
  were never offered before; matching is case-insensitive.

  Categories are global rather than per organization, and `GET /api/categories`
  returns the whole shared list to every caller so the dropdown is always
  complete — previously tenant staff saw only the categories their own equipment
  already used, which left a brand-new tenant staring at an empty picker. Each
  caller's `equipment_count` still tallies only equipment they may see, so a
  tenant admin is not told how many instruments another tenant has.

  **Anyone who may add equipment** (tenant admins, engineers and application
  accounts) may also add a category when the one they need is missing: the
  equipment form offers *＋ New category…* and registers it through
  `POST /api/categories`. Renaming and deleting stay with the **master only** —
  those rewrite every tenant's equipment records, while adding one can only
  lengthen a shared pick-list. Deleting a category moves its equipment to
  *Other*, which itself cannot be deleted. The Categories screen (More →
  Categories) remains master-only.

  The complaint form keeps its own fixed category list and is unaffected.
- **Ticket numbering**: complaints `CMP-0001…`, breakdowns `BRK-0001…`.
- **FIFO storage**: each ticket type is capped (default 2000). The oldest
  tickets roll off into `ticket_history.log` (JSON lines) so nothing is lost.
- **History / audit log**: every ticket records who did what and when.
- **No file attachments**: neither ticket type accepts uploaded photos or
  documents. That function was replaced by a generated **Service report (PDF)**
  on every ticket — see below. The whole attachments subsystem is gone, not
  merely hidden: the upload / list / download / delete endpoints have been
  removed (writes now return 405), the `attachments` table is dropped by an
  idempotent migration in `database.py` and no longer created on a fresh
  install, and the frontend upload / preview / delete helpers are deleted. The
  `"Added file"` entries in the audit log are deliberately kept — the history
  log is an audit trail, and those rows live in `audit_logs`, not here.
- **Service report PDFs** ([`server/report.py`](server/report.py)): one per
  ticket, on the LabCare letterhead — `GET /api/complaints/<id>/report.pdf`
  (summary, description, linked breakdown work orders and conversation log) and
  `GET /api/breakdowns/<id>/report.pdf` (summary, fault description, source
  complaint, root cause, resolution notes and work log). Plus a management
  `GET /api/reports/trend.pdf`. Each ticket's **Report** section exposes its
  button.
- **Customer feedback on settled tickets**: once a ticket is finished the
  customer side can rate it out of 5 stars and leave comments. It is entirely
  optional — nothing is required to close a ticket, and one nobody rated looks
  and behaves exactly as before.
  - **When it opens**: complaints at `resolved` or `closed`, breakdowns at
    `resolved` (they have no closed status). Before that the section is not
    rendered at all and the API answers `409`.
  - **Who may give it**: customer-role users of that organization, plus anyone
    holding that organization's QR portal link — anonymous visitors included.
    Staff (master admin, tenant admin, technician, application) can **read**
    feedback but never write it; an attempt returns `403`.
  - **Shape**: one rating per ticket, editable — re-voting replaces the previous
    one rather than adding a row — plus a comment thread the customer side can
    keep adding to. A name is optional on the portal and defaults to "Customer".
  - **Where it appears**: the ticket detail view in the app, each settled ticket
    in the portal's history list, a **Customer satisfaction** card on the
    dashboard (average rating and how many ratings it covers, scoped to what the
    caller may see), and a **Customer feedback** section on both Service report
    PDFs. That section is omitted entirely when the customer left neither a
    rating nor a comment, rather than printing an empty one.
  - **Endpoints**: `POST /api/tickets/<kind>/<id>/rating` and `…/feedback` for
    signed-in customers, `GET|POST /api/portal/<token>/feedback` and
    `…/rating` for the public portal. Ratings are whole stars 1–5 (fractions are
    rejected); comment text is capped at 2000 characters.
  - **Never affects the ticket**: feedback cannot change status, priority,
    assignment, ordering or the work log. It lives in its own `ticket_ratings`
    and `ticket_feedback` tables, sends no notifications, and shows up in the
    audit log as `rating` / `feedback` entries.
- **Sound + email alerts** for new tickets and updates.
- **Desktop push alerts**: every bell notification can also ring as a real
  system notification via Web Push (service worker + VAPID), so users hear the
  alert even when the app/tab/browser window is closed. Each signed-in user
  opts in per device from **Menu → Alerts & sound → Push notifications**; the
  subscription is stored against their account and respects the per-user sound
  preference. The VAPID keypair lives in [`server/vapid.json`](server/vapid.json)
  (override with `LABCARE_VAPID_PRIVATE`; the public key for clients is derived
  from it). `pywebpush` sends one push per recipient whenever `notify()` runs.
- **Native mobile alerts (phone rings)**: an [Expo app](mobile/README.md) signs
  into LabCare and receives every bell notification via **Firebase Cloud
  Messaging**, so the phone rings even with the browser closed or the phone
  locked. Backend side (`app_devices` table + `server/apppush.py` + the
  `/api/app/*` endpoints) is implemented; enabling it only needs Firebase
  project credentials (`LABCARE_FCM_SERVICE_JSON` + `LABCARE_FCM_PROJECT_ID`).
  See [`mobile/README.md`](mobile/README.md) for the one-time setup.

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
