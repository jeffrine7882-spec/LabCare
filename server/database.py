"""LabCare — database layer.

Two engines behind one interface:

* **InsForge Postgres** — used when ``LABCARE_DATABASE_URL`` (or the InsForge
  standard ``DATABASE_URL``) is set. The app keeps its SQLite-era queries;
  a thin psycopg3 cursor adapts them on the fly (``?`` -> ``%s``, ``%``
  escaping, ``INSERT OR IGNORE`` -> ``ON CONFLICT DO NOTHING``, ``lastrowid``
  -> ``RETURNING id``, ``date('now', ...)`` -> ``CURRENT_DATE - INTERVAL ...``).

* **SQLite** — the original local database, kept so ``python server/run.py``
  still works with no environment at all.

The public surface used by the rest of the app is unchanged:

    conn(), now(), hash_password(), init_db(), next_code_for(), rows_to_dicts()
"""
import os
import re
import sqlite3
import hashlib
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("LABCARE_DB", os.path.join(BASE_DIR, "labcare.db"))

try:  # Postgres driver is optional (only used when a Postgres URL is set).
    import psycopg
    from psycopg.rows import dict_row
    _HAS_PSYCOPG = True
except Exception:  # pragma: no cover - local SQLite-only installs
    psycopg = None
    dict_row = None
    _HAS_PSYCOPG = False

_PG_URL = (os.environ.get("LABCARE_DATABASE_URL") or
           os.environ.get("DATABASE_URL") or "").strip()
PG_ENABLED = bool(_PG_URL) and _HAS_PSYCOPG


# Malaysia Standard Time — UTC+8, no daylight saving. All timestamps stored by
# the app use this wall-clock so records always read as their true local time.
MYT = timezone(timedelta(hours=8), "MYT")


def now():
    return datetime.now(MYT).strftime("%Y-%m-%d %H:%M:%S")


def now_dt():
    return datetime.now(MYT)


def hash_password(pw):
    salt = "labcare::"
    return hashlib.sha256((salt + pw).encode()).hexdigest()


# ---------------------------------------------------------------------------
# SQLite engine (local development; the original LabCare behaviour).
# ---------------------------------------------------------------------------

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    phone TEXT DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','engineer','application','customer')),
    customer_id INTEGER,
    location_id INTEGER,
    department_id INTEGER,
    active INTEGER DEFAULT 1,
    pending INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS onboarding_apps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    phone TEXT DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('engineer','application','customer')),
    customer_id INTEGER,
    location_id INTEGER,
    department_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
    reviewed_by INTEGER,
    reviewed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    contact_name TEXT DEFAULT '',
    email TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    address TEXT DEFAULT '',
    city TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

-- Which customers a tenant admin cares for. A tenant admin's PRIMARY customer is
-- stored on users.customer_id; extra customers are linked here. Together they
-- form the admin's full scope (their "care list").
CREATE TABLE IF NOT EXISTS admin_customer_links (
    admin_id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (admin_id, customer_id)
);

CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    address TEXT DEFAULT '',
    city TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);

CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    location_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(location_id) REFERENCES locations(id)
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equipment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    location_id INTEGER,
    department_id INTEGER,
    name TEXT NOT NULL,
    model TEXT DEFAULT '',
    serial_number TEXT DEFAULT '',
    category TEXT DEFAULT '',
    installed_date TEXT DEFAULT '',
    warranty_expiry TEXT DEFAULT '',
    status TEXT DEFAULT 'active' CHECK(status IN ('active','retired')),
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(location_id) REFERENCES locations(id),
    FOREIGN KEY(department_id) REFERENCES departments(id),
    UNIQUE(customer_id, serial_number)
);

CREATE TABLE IF NOT EXISTS complaints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    customer_id INTEGER NOT NULL,
    equipment_id INTEGER,
    location_id INTEGER,
    department_id INTEGER,
    subject TEXT NOT NULL,
    description TEXT DEFAULT '',
    category TEXT DEFAULT 'General',
    priority TEXT DEFAULT 'medium' CHECK(priority IN ('low','medium','high','critical')),
    status TEXT DEFAULT 'open' CHECK(status IN ('open','in_progress','resolved','closed')),
    created_by INTEGER NOT NULL,
    assigned_to INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    reporter_name TEXT DEFAULT '',
    reporter_phone TEXT DEFAULT '',
    accepted_by INTEGER,
    accepted_at TEXT,
    accept_reply TEXT DEFAULT '',
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(equipment_id) REFERENCES equipment(id),
    FOREIGN KEY(location_id) REFERENCES locations(id),
    FOREIGN KEY(department_id) REFERENCES departments(id),
    FOREIGN KEY(created_by) REFERENCES users(id),
    FOREIGN KEY(assigned_to) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS breakdowns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    equipment_id INTEGER,
    customer_id INTEGER NOT NULL,
    complaint_id INTEGER,
    location_id INTEGER,
    department_id INTEGER,
    fault_description TEXT NOT NULL,
    root_cause TEXT DEFAULT '',
    priority TEXT DEFAULT 'medium' CHECK(priority IN ('low','medium','high','critical')),
    status TEXT DEFAULT 'reported' CHECK(status IN ('reported','diagnosed','in_progress','on_hold','resolved')),
    reported_by INTEGER NOT NULL,
    assigned_to INTEGER,
    resolution_notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    reporter_name TEXT DEFAULT '',
    reporter_phone TEXT DEFAULT '',
    accepted_by INTEGER,
    accepted_at TEXT,
    accept_reply TEXT DEFAULT '',
    FOREIGN KEY(equipment_id) REFERENCES equipment(id),
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(complaint_id) REFERENCES complaints(id),
    FOREIGN KEY(location_id) REFERENCES locations(id),
    FOREIGN KEY(department_id) REFERENCES departments(id),
    FOREIGN KEY(reported_by) REFERENCES users(id),
    FOREIGN KEY(assigned_to) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    user_id INTEGER,
    user_name TEXT DEFAULT '',
    action TEXT NOT NULL,
    detail TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    mime TEXT DEFAULT '',
    size INTEGER DEFAULT 0,
    uploaded_by INTEGER NOT NULL,
    data BLOB NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    entity_type TEXT DEFAULT '',
    entity_id INTEGER,
    text TEXT NOT NULL,
    read INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS notification_pings (
    user_id INTEGER PRIMARY KEY,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS pm_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    equipment_id INTEGER,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    interval_days INTEGER NOT NULL DEFAULT 90,
    last_done_at TEXT,
    next_due_at TEXT,
    assigned_to INTEGER,
    active INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(equipment_id) REFERENCES equipment(id),
    FOREIGN KEY(assigned_to) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS pm_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL,
    equipment_id INTEGER,
    performed_by INTEGER NOT NULL,
    performed_at TEXT NOT NULL,
    notes TEXT DEFAULT '',
    FOREIGN KEY(schedule_id) REFERENCES pm_schedules(id),
    FOREIGN KEY(performed_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS portal_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT UNIQUE NOT NULL,
    customer_id INTEGER NOT NULL,
    equipment_id INTEGER,
    label TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_by INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(equipment_id) REFERENCES equipment(id)
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    endpoint TEXT UNIQUE NOT NULL,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    active INTEGER DEFAULT 1,
    alert_on INTEGER DEFAULT 1,
    user_agent TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS app_devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    push_token TEXT UNIQUE NOT NULL,
    active INTEGER DEFAULT 1,
    alert_on INTEGER DEFAULT 1,
    device_name TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);
"""


def _sqlite_conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _sqlite_init_db():
    c = _sqlite_conn()
    c.executescript(_SQLITE_SCHEMA)
    _sqlite_migrate(c)
    backfill_audit_history(c)
    c.commit()
    c.close()


def _sqlite_migrate(c):
    """Additive migrations for databases created before these columns/tables existed."""
    user_cols = [r["name"] for r in c.execute("PRAGMA table_info(users)")]
    if "pending" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN pending INTEGER DEFAULT 0")

    complaint_cols = [r["name"] for r in c.execute("PRAGMA table_info(complaints)")]
    if "reporter_name" not in complaint_cols:
        c.execute("ALTER TABLE complaints ADD COLUMN reporter_name TEXT DEFAULT ''")
    if "reporter_phone" not in complaint_cols:
        c.execute("ALTER TABLE complaints ADD COLUMN reporter_phone TEXT DEFAULT ''")
    if "accepted_by" not in complaint_cols:
        c.execute("ALTER TABLE complaints ADD COLUMN accepted_by INTEGER")
    if "accepted_at" not in complaint_cols:
        c.execute("ALTER TABLE complaints ADD COLUMN accepted_at TEXT")
    if "accept_reply" not in complaint_cols:
        c.execute("ALTER TABLE complaints ADD COLUMN accept_reply TEXT DEFAULT ''")

    for table in ("users", "equipment", "complaints", "breakdowns"):
        cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})")]
        if "responsible_admin_id" not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN responsible_admin_id INTEGER")

    brk_cols = [r["name"] for r in c.execute("PRAGMA table_info(breakdowns)")]
    if "reporter_name" not in brk_cols:
        c.execute("ALTER TABLE breakdowns ADD COLUMN reporter_name TEXT DEFAULT ''")
    if "reporter_phone" not in brk_cols:
        c.execute("ALTER TABLE breakdowns ADD COLUMN reporter_phone TEXT DEFAULT ''")
    if "accepted_by" not in brk_cols:
        c.execute("ALTER TABLE breakdowns ADD COLUMN accepted_by INTEGER")
    if "accepted_at" not in brk_cols:
        c.execute("ALTER TABLE breakdowns ADD COLUMN accepted_at TEXT")
    if "accept_reply" not in brk_cols:
        c.execute("ALTER TABLE breakdowns ADD COLUMN accept_reply TEXT DEFAULT ''")

    for table in ("complaints", "breakdowns"):
        cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})")]
        if "closed_by" not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN closed_by INTEGER")

    for row in c.execute(
            "SELECT id, customer_id FROM users WHERE role='admin' AND customer_id IS NOT NULL").fetchall():
        c.execute(
            "INSERT OR IGNORE INTO admin_customer_links (admin_id, customer_id, created_at) VALUES (?,?,?)",
            (row["id"], row["customer_id"], now()))

    _sqlite_migrate_roles(c)


def _sqlite_migrate_roles(c):
    """Rename the technician role to engineer and add the application role.

    Tables created by older versions carry CHECK constraints mentioning
    'technician', which would reject the new role values. When detected,
    rebuild those tables with the widened CHECK, copying all rows.
    """
    for table in ("users", "onboarding_apps"):
        row = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        sql = row["sql"] if row else ""
        if "'technician'" in sql:
            new_sql = sql.replace(
                "CHECK(role IN ('admin','technician','customer'))",
                "CHECK(role IN ('admin','engineer','application','customer'))").replace(
                "CHECK(role IN ('technician','customer'))",
                "CHECK(role IN ('engineer','application','customer'))")
            new_sql = re.sub(r"^CREATE TABLE\s+[\w\x22\']+\s*\(",
                             f"CREATE TABLE {table}_mig (", new_sql.strip())
            c.execute(new_sql)
            cols = ", ".join(r["name"] for r in c.execute(f"PRAGMA table_info({table})"))
            c.execute(f"INSERT INTO {table}_mig ({cols}) SELECT {cols} FROM {table}")
            c.execute(f"DROP TABLE {table}")
            c.execute(f"ALTER TABLE {table}_mig RENAME TO {table}")
    c.execute("UPDATE users SET role='engineer' WHERE role='technician'")
    c.execute("UPDATE onboarding_apps SET role='engineer' WHERE role='technician'")


# ---------------------------------------------------------------------------
# Audit backfill — shared by both engines (uses only the conn() interface).
# ---------------------------------------------------------------------------

_STATUS_LABELS_BACKFILL = {
    "open": "Open", "in_progress": "In Progress", "resolved": "Resolved", "closed": "Closed",
    "reported": "Reported", "diagnosed": "Diagnosed", "on_hold": "On Hold",
}


def backfill_audit_history(c):
    """Idempotent: reconstruct a ticket's opening/closing history when missing."""
    for entity in ("complaint", "breakdown"):
        table = entity + "s"
        for r in c.execute(f"SELECT * FROM {table}").fetchall():
            eid = r["id"]
            has_created = c.execute(
                "SELECT 1 FROM audit_logs WHERE entity_type=? AND entity_id=? AND action='created'",
                (entity, eid)).fetchone() is not None
            has_resolution = c.execute(
                "SELECT 1 FROM audit_logs WHERE entity_type=? AND entity_id=? AND action='resolution'",
                (entity, eid)).fetchone() is not None
            has_activity = c.execute(
                "SELECT 1 FROM audit_logs WHERE entity_type=? AND entity_id=? AND action NOT IN ('created','resolution')",
                (entity, eid)).fetchone() is not None
            needs_created = not has_created
            needs_resolution = bool(r["resolved_at"] and not has_resolution)
            needs_activity = not has_activity

            if needs_created or needs_activity or needs_resolution:
                _audit_backfill_one(c, entity, r, ensure_created=needs_created,
                                    ensure_resolution=needs_resolution,
                                    ensure_activity=needs_activity)

            if r["resolved_at"] and not r["closed_by"]:
                who_opened = r["created_by"] if entity == "complaint" else r["reported_by"]
                closer_id = r["assigned_to"] or who_opened
                c.execute(f"UPDATE {table} SET closed_by=? WHERE id=?",
                          (closer_id, eid))


def _audit_backfill_one(c, entity, r, ensure_created=True, ensure_resolution=False,
                        ensure_activity=True):
    def user_name(uid):
        if not uid:
            return None
        row = c.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
        return row["name"] if row else None

    def writer(uid):
        return (uid, user_name(uid) or "")

    if entity == "complaint":
        initial = "open"
        subject = r["subject"]
        who_opened = r["created_by"]
        opener_label = "Opened"
    else:
        initial = "reported"
        subject = (r["fault_description"] or "")[:80]
        who_opened = r["reported_by"]
        opener_label = "Reported"

    uid, uname = writer(who_opened)
    cur = r["status"]

    if ensure_created:
        c.execute(
            "INSERT INTO audit_logs (entity_type,entity_id,user_id,user_name,action,detail,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (entity, r["id"], uid, uname, "created", f"{opener_label} {r['code']} — {subject}",
             r["created_at"]),
        )

    if ensure_resolution and r["resolved_at"] and (cur in ("resolved", "closed")):
        closer_id = r["closed_by"] or r["assigned_to"] or who_opened
        c_id, c_name = writer(closer_id)
        label = "Closed" if cur == "closed" else "Marked resolved"
        c.execute(
            "INSERT INTO audit_logs (entity_type,entity_id,user_id,user_name,action,detail,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (entity, r["id"], c_id, c_name, "resolution", f"{label} by {c_name}",
             r["resolved_at"]),
        )

    if not ensure_activity:
        return

    a_uid, a_name = writer(r["assigned_to"])
    if a_uid and a_uid != uid:
        c.execute(
            "INSERT INTO audit_logs (entity_type,entity_id,user_id,user_name,action,detail,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (entity, r["id"], uid, uname, "assigned", f"→ {a_name}", r["created_at"]),
        )

    if cur and cur != initial:
        when = r["updated_at"] or r["resolved_at"] or r["created_at"]
        c.execute(
            "INSERT INTO audit_logs (entity_type,entity_id,user_id,user_name,action,detail,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (entity, r["id"], uid, uname, "status",
             f"{_STATUS_LABELS_BACKFILL.get(initial, initial)} → {_STATUS_LABELS_BACKFILL.get(cur, cur)}",
             when),
        )


# ---------------------------------------------------------------------------
# PostgreSQL engine (InsForge). Mirrors the SQLite schema 1:1.
# ---------------------------------------------------------------------------

_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    phone TEXT DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','engineer','application','customer')),
    customer_id BIGINT,
    location_id BIGINT,
    department_id BIGINT,
    active INTEGER DEFAULT 1,
    pending INTEGER DEFAULT 0,
    responsible_admin_id BIGINT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS onboarding_apps (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    phone TEXT DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('engineer','application','customer')),
    customer_id BIGINT,
    location_id BIGINT,
    department_id BIGINT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
    reviewed_by BIGINT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    contact_name TEXT DEFAULT '',
    email TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    address TEXT DEFAULT '',
    city TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_customer_links (
    admin_id BIGINT NOT NULL,
    customer_id BIGINT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (admin_id, customer_id)
);

CREATE TABLE IF NOT EXISTS locations (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES customers(id),
    name TEXT NOT NULL,
    address TEXT DEFAULT '',
    city TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS departments (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES customers(id),
    location_id BIGINT NOT NULL REFERENCES locations(id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equipment (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES customers(id),
    location_id BIGINT REFERENCES locations(id),
    department_id BIGINT REFERENCES departments(id),
    name TEXT NOT NULL,
    model TEXT DEFAULT '',
    serial_number TEXT DEFAULT '',
    category TEXT DEFAULT '',
    installed_date TEXT DEFAULT '',
    warranty_expiry TEXT DEFAULT '',
    status TEXT DEFAULT 'active' CHECK(status IN ('active','retired')),
    notes TEXT DEFAULT '',
    responsible_admin_id BIGINT,
    created_at TEXT NOT NULL,
    UNIQUE(customer_id, serial_number)
);

CREATE TABLE IF NOT EXISTS complaints (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    customer_id BIGINT NOT NULL REFERENCES customers(id),
    equipment_id BIGINT REFERENCES equipment(id),
    location_id BIGINT REFERENCES locations(id),
    department_id BIGINT REFERENCES departments(id),
    subject TEXT NOT NULL,
    description TEXT DEFAULT '',
    category TEXT DEFAULT 'General',
    priority TEXT DEFAULT 'medium' CHECK(priority IN ('low','medium','high','critical')),
    status TEXT DEFAULT 'open' CHECK(status IN ('open','in_progress','resolved','closed')),
    created_by BIGINT NOT NULL REFERENCES users(id),
    assigned_to BIGINT REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    closed_by BIGINT,
    reporter_name TEXT DEFAULT '',
    reporter_phone TEXT DEFAULT '',
    accepted_by BIGINT,
    accepted_at TEXT,
    accept_reply TEXT DEFAULT '',
    responsible_admin_id BIGINT
);

CREATE TABLE IF NOT EXISTS breakdowns (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    equipment_id BIGINT REFERENCES equipment(id),
    customer_id BIGINT NOT NULL REFERENCES customers(id),
    complaint_id BIGINT REFERENCES complaints(id),
    location_id BIGINT REFERENCES locations(id),
    department_id BIGINT REFERENCES departments(id),
    fault_description TEXT NOT NULL,
    root_cause TEXT DEFAULT '',
    priority TEXT DEFAULT 'medium' CHECK(priority IN ('low','medium','high','critical')),
    status TEXT DEFAULT 'reported' CHECK(status IN ('reported','diagnosed','in_progress','on_hold','resolved')),
    reported_by BIGINT NOT NULL REFERENCES users(id),
    assigned_to BIGINT REFERENCES users(id),
    resolution_notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    closed_by BIGINT,
    reporter_name TEXT DEFAULT '',
    reporter_phone TEXT DEFAULT '',
    accepted_by BIGINT,
    accepted_at TEXT,
    accept_reply TEXT DEFAULT '',
    responsible_admin_id BIGINT
);

CREATE TABLE IF NOT EXISTS comments (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL REFERENCES users(id),
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id BIGINT NOT NULL,
    user_id BIGINT,
    user_name TEXT DEFAULT '',
    action TEXT NOT NULL,
    detail TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id BIGINT NOT NULL,
    filename TEXT NOT NULL,
    mime TEXT DEFAULT '',
    size INTEGER DEFAULT 0,
    uploaded_by BIGINT NOT NULL,
    data BYTEA NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    entity_type TEXT DEFAULT '',
    entity_id BIGINT,
    text TEXT NOT NULL,
    read INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_pings (
    user_id BIGINT PRIMARY KEY REFERENCES users(id),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pm_schedules (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES customers(id),
    equipment_id BIGINT REFERENCES equipment(id),
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    interval_days INTEGER NOT NULL DEFAULT 90,
    last_done_at TEXT,
    next_due_at TEXT,
    assigned_to BIGINT REFERENCES users(id),
    active INTEGER DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pm_logs (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    schedule_id BIGINT NOT NULL REFERENCES pm_schedules(id),
    equipment_id BIGINT,
    performed_by BIGINT NOT NULL REFERENCES users(id),
    performed_at TEXT NOT NULL,
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS portal_links (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    token TEXT UNIQUE NOT NULL,
    customer_id BIGINT NOT NULL REFERENCES customers(id),
    equipment_id BIGINT REFERENCES equipment(id),
    label TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_by BIGINT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    endpoint TEXT UNIQUE NOT NULL,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    active INTEGER DEFAULT 1,
    alert_on INTEGER DEFAULT 1,
    user_agent TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_devices (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    platform TEXT NOT NULL,
    push_token TEXT UNIQUE NOT NULL,
    active INTEGER DEFAULT 1,
    alert_on INTEGER DEFAULT 1,
    device_name TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
"""

# Safety-net column migrations for a Postgres database created by an earlier
# version of this schema. On the fresh schema above these are all no-ops.
_PG_ALTERS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS pending INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS responsible_admin_id BIGINT",
    "ALTER TABLE complaints ADD COLUMN IF NOT EXISTS reporter_name TEXT DEFAULT ''",
    "ALTER TABLE complaints ADD COLUMN IF NOT EXISTS reporter_phone TEXT DEFAULT ''",
    "ALTER TABLE complaints ADD COLUMN IF NOT EXISTS accepted_by BIGINT",
    "ALTER TABLE complaints ADD COLUMN IF NOT EXISTS accepted_at TEXT",
    "ALTER TABLE complaints ADD COLUMN IF NOT EXISTS accept_reply TEXT DEFAULT ''",
    "ALTER TABLE complaints ADD COLUMN IF NOT EXISTS responsible_admin_id BIGINT",
    "ALTER TABLE complaints ADD COLUMN IF NOT EXISTS closed_by BIGINT",
    "ALTER TABLE breakdowns ADD COLUMN IF NOT EXISTS reporter_name TEXT DEFAULT ''",
    "ALTER TABLE breakdowns ADD COLUMN IF NOT EXISTS reporter_phone TEXT DEFAULT ''",
    "ALTER TABLE breakdowns ADD COLUMN IF NOT EXISTS accepted_by BIGINT",
    "ALTER TABLE breakdowns ADD COLUMN IF NOT EXISTS accepted_at TEXT",
    "ALTER TABLE breakdowns ADD COLUMN IF NOT EXISTS accept_reply TEXT DEFAULT ''",
    "ALTER TABLE breakdowns ADD COLUMN IF NOT EXISTS responsible_admin_id BIGINT",
    "ALTER TABLE breakdowns ADD COLUMN IF NOT EXISTS closed_by BIGINT",
    "ALTER TABLE equipment ADD COLUMN IF NOT EXISTS responsible_admin_id BIGINT",
]

_RE_INSERT = re.compile(r"^\s*insert\b", re.I)
_RE_OR_IGNORE = re.compile(r"\binsert\s+or\s+ignore\s+into\b", re.I)
_RE_RETURNING = re.compile(r"\breturning\b", re.I)


def _pg_rewrite(sql):
    """Translate the SQLite-era SQL text into PostgreSQL.

    date('now', ...) is replaced with a to_char() *text* value in the same
    'YYYY-MM-DD HH24:MI:SS' shape the app stores, so TEXT-vs-TEXT comparisons
    keep the exact lexicographic semantics sqlite had (no TEXT <> timestamp
    operator errors).
    """
    sql = sql.replace("date('now','-3 months')",
                      "to_char(CURRENT_DATE - INTERVAL '3 months','YYYY-MM-DD HH24:MI:SS')")
    sql = sql.replace("date('now','-6 months')",
                      "to_char(CURRENT_DATE - INTERVAL '6 months','YYYY-MM-DD HH24:MI:SS')")
    sql = sql.replace("date('now')", "to_char(CURRENT_DATE,'YYYY-MM-DD HH24:MI:SS')")
    # INSERT OR IGNORE -> plain INSERT (the wrapper appends ON CONFLICT DO NOTHING).
    sql = _RE_OR_IGNORE.sub("INSERT INTO", sql)
    # Escape literal % (psycopg placeholder character) and convert ? -> %s,
    # leaving single-quoted strings alone.
    out = []
    in_str = False
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if in_str:
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    out.append("''")
                    i += 2
                    continue
                in_str = False
            out.append(ch)
            i += 1
            continue
        if ch == "'":
            in_str = True
            out.append(ch)
        elif ch == "%":
            out.append("%%")
        elif ch == "?":
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _norm_row(r):
    if r is None:
        return None
    return {k: (v.tobytes() if isinstance(v, memoryview) else v) for k, v in r.items()}


class _PGCursor:
    """Cursor shim: same shape the app expects from sqlite3 (dict rows + lastrowid)."""

    # Tables without an `id` column (sessions: token PK; notification_pings:
    # user_id PK; admin_customer_links: composite PK). RETURNING id is skipped
    # for these, because lastrowid is never read from them either.
    _NO_ID_TABLES = {"sessions", "notification_pings", "admin_customer_links"}

    def __init__(self, pg_conn):
        self._cur = pg_conn.cursor(row_factory=dict_row)
        self._lastrowid = None

    @staticmethod
    def _target_table(sql):
        m = re.match(r"\s*insert\s+(?:or\s+ignore\s+)?into\s+([a-z_][a-z0-9_]*)\b", sql, re.I)
        return m.group(1).lower() if m else None

    def execute(self, sql, params=None):
        orig = sql
        is_insert = bool(_RE_INSERT.match(orig))
        has_returning = _RE_RETURNING.search(orig) is not None
        or_ignore = _RE_OR_IGNORE.search(orig) is not None
        sql2 = _pg_rewrite(orig)
        if or_ignore:
            sql2 += " ON CONFLICT DO NOTHING"
        added_returning = (is_insert and not has_returning
                           and self._target_table(orig) not in self._NO_ID_TABLES)
        if added_returning:
            sql2 += " RETURNING id"
        if params is not None:
            self._cur.execute(sql2, params)
        else:
            self._cur.execute(sql2)
        self._lastrowid = None
        if added_returning:
            row = self._cur.fetchone()
            if row is not None:
                self._lastrowid = row.get("id")

    @property
    def lastrowid(self):
        return self._lastrowid

    def fetchone(self):
        return _norm_row(self._cur.fetchone())

    def fetchall(self):
        return [_norm_row(r) for r in self._cur.fetchall()]

    def close(self):
        self._cur.close()


class _PGConn:
    def __init__(self, url):
        # Safety nets: a killed request/container must never leave a wedgeable
        # open transaction holding table locks (idle_in_transaction timeout),
        # and no single statement should hang unbounded (statement timeout).
        self._conn = psycopg.connect(
            url,
            connect_timeout=15,
            options="-c TimeZone=Asia/Kuala_Lumpur "
                    "-c idle_in_transaction_session_timeout=30000 "
                    "-c statement_timeout=120000",
        )

    def execute(self, sql, params=None):
        cur = _PGCursor(self._conn)
        cur.execute(sql, params)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def _pg_migrate(c):
    for stmt in _PG_ALTERS:
        c.execute(stmt)
    # Role rename: technician -> engineer, plus the new 'application' role.
    # Older databases pin an incompatible CHECK on the role column, so do
    # drop -> convert -> widen (an ADD over the old data would fail).
    c.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check")
    c.execute("UPDATE users SET role='engineer' WHERE role='technician'")
    c.execute("ALTER TABLE users ADD CONSTRAINT users_role_check "
              "CHECK (role IN ('admin','engineer','application','customer'))")
    c.execute("ALTER TABLE onboarding_apps DROP CONSTRAINT IF EXISTS onboarding_apps_role_check")
    c.execute("UPDATE onboarding_apps SET role='engineer' WHERE role='technician'")
    c.execute("ALTER TABLE onboarding_apps ADD CONSTRAINT onboarding_apps_role_check "
              "CHECK (role IN ('engineer','application','customer'))")
    for r in c.execute(
            "SELECT id, customer_id FROM users WHERE role='admin' AND customer_id IS NOT NULL").fetchall():
        c.execute(
            "INSERT INTO admin_customer_links (admin_id,customer_id,created_at) "
            "VALUES (?,?,?) ON CONFLICT DO NOTHING",
            (r["id"], r["customer_id"], now()))


def _pg_init_db():
    raw = psycopg.connect(
        _PG_URL,
        connect_timeout=15,
        options="-c TimeZone=Asia/Kuala_Lumpur "
                "-c idle_in_transaction_session_timeout=60000 "
                "-c statement_timeout=120000",
    )
    try:
        with raw.cursor() as cur:
            cur.execute(_PG_SCHEMA)
        raw.commit()
    finally:
        raw.close()
    c = conn()
    _pg_migrate(c)
    backfill_audit_history(c)
    c.commit()
    c.close()


# ---------------------------------------------------------------------------
# Public interface.
# ---------------------------------------------------------------------------

def conn():
    """Open a database connection (Postgres when configured, else SQLite)."""
    if PG_ENABLED:
        return _PGConn(_PG_URL)
    return _sqlite_conn()


def init_db():
    """Create/migrate the schema and backfill audit history."""
    if PG_ENABLED:
        _pg_init_db()
    else:
        _sqlite_init_db()


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


def next_code_for(table, prefix):
    """Generate a human-friendly sequential code like CMP-0001."""
    c = conn()
    rows = c.execute(f"SELECT code FROM {table} WHERE code LIKE ?", (prefix + "-%",)).fetchall()
    c.close()
    maxn = 0
    for r in rows:
        try:
            maxn = max(maxn, int(str(r["code"]).rsplit("-", 1)[-1]))
        except (ValueError, AttributeError):
            pass
    return f"{prefix}-{maxn + 1:04d}"
