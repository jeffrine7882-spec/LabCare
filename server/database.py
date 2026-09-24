"""LabCare — database layer (SQLite)."""
import os
import sqlite3
import hashlib
import uuid
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("LABCARE_DB", os.path.join(BASE_DIR, "labcare.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    phone TEXT DEFAULT '',
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','technician','customer')),
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
    role TEXT NOT NULL CHECK(role IN ('technician','customer')),
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
    entity_type TEXT NOT NULL,          -- 'complaint' | 'breakdown'
    entity_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,          -- 'complaint' | 'breakdown'
    entity_id INTEGER NOT NULL,
    user_id INTEGER,
    user_name TEXT DEFAULT '',            -- denormalised so logs survive user deletion
    action TEXT NOT NULL,               -- 'created' | 'updated' | 'status' | 'assigned' |
                                        -- 'comment' | 'attachment' | 'resolution' | 'deleted'
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
    entity_type TEXT NOT NULL,          -- 'complaint' | 'breakdown'
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
    updated_at TEXT NOT NULL
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
"""


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def init_db():
    c = conn()
    c.executescript(SCHEMA)
    _migrate(c)
    backfill_audit_history(c)
    c.commit()
    c.close()


def _migrate(c):
    """Lightweight, additive migrations for databases created before these columns/tables existed."""
    user_cols = [r["name"] for r in c.execute("PRAGMA table_info(users)")]
    if "pending" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN pending INTEGER DEFAULT 0")

    complaint_cols = [r["name"] for r in c.execute("PRAGMA table_info(complaints)")]
    if "reporter_name" not in complaint_cols:
        c.execute("ALTER TABLE complaints ADD COLUMN reporter_name TEXT DEFAULT ''")
    if "reporter_phone" not in complaint_cols:
        c.execute("ALTER TABLE complaints ADD COLUMN reporter_phone TEXT DEFAULT ''")
    # Acceptance — who accepted a complaint and the reply shown to the reporter.
    if "accepted_by" not in complaint_cols:
        c.execute("ALTER TABLE complaints ADD COLUMN accepted_by INTEGER")
    if "accepted_at" not in complaint_cols:
        c.execute("ALTER TABLE complaints ADD COLUMN accepted_at TEXT")
    if "accept_reply" not in complaint_cols:
        c.execute("ALTER TABLE complaints ADD COLUMN accept_reply TEXT DEFAULT ''")

    # Multi-tenant: an explicit "responsible tenant admin" for each record so the
    # master (or tenant admins) can see who cares for it. References users.id; the
    # value must be an active admin linked to the record's customer.
    for table in ("users", "equipment", "complaints", "breakdowns"):
        cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})")]
        if "responsible_admin_id" not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN responsible_admin_id INTEGER")

    # Record who closed/resolved each ticket (the "opener" is already stored as
    # created_by / reported_by).
    for table in ("complaints", "breakdowns"):
        cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})")]
        if "closed_by" not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN closed_by INTEGER")

    # Multi-customer tenant admins: each tenant admin's primary customer
    # (users.customer_id) is mirrored into the care-list link table so their full
    # scope is consistent everywhere.
    for row in c.execute(
            "SELECT id, customer_id FROM users WHERE role='admin' AND customer_id IS NOT NULL").fetchall():
        c.execute(
            "INSERT OR IGNORE INTO admin_customer_links (admin_id, customer_id, created_at) VALUES (?,?,?)",
            (row["id"], row["customer_id"], now()))


_STATUS_LABELS_BACKFILL = {
    "open": "Open", "in_progress": "In Progress", "resolved": "Resolved", "closed": "Closed",
    "reported": "Reported", "diagnosed": "Diagnosed", "on_hold": "On Hold",
}


def backfill_audit_history(c):
    """Idempotent: reconstruct a ticket's opening/closing history when missing.

    Seed data and pre-existing rows predate the audit trail, so this writes the
    key events — who opened it, who it was assigned to, its status change, and who
    resolved/closed it — straight from the ticket columns themselves.

    * The "opened/reported" event is always ensured (it is the one answer users
      always want: who opened the ticket).
    * The "resolved by / closed by" event is always ensured for resolved/closed
      tickets — it is the other answer users always want: who closed it.
    * Assignment / status-transition events are only added when the ticket has no
      real activity yet, so genuine mid-lifecycle history is never duplicated.
    """
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

            # keep the ticket's stored closer in sync so the detail panel shows
            # the same person as the history timeline
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

    # 1) opened / reported — always ensured
    if ensure_created:
        c.execute(
            "INSERT INTO audit_logs (entity_type,entity_id,user_id,user_name,action,detail,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (entity, r["id"], uid, uname, "created", f"{opener_label} {r['code']} — {subject}",
             r["created_at"]),
        )

    # 2) resolution / close (who did it) — always ensured for resolved tickets
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

    # mid-lifecycle activity is only reconstructed for tickets with no live history
    if not ensure_activity:
        return

    # 3) assignment (if any)
    a_uid, a_name = writer(r["assigned_to"])
    if a_uid and a_uid != uid:
        c.execute(
            "INSERT INTO audit_logs (entity_type,entity_id,user_id,user_name,action,detail,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (entity, r["id"], uid, uname, "assigned", f"→ {a_name}", r["created_at"]),
        )

    # 4) status change to the current status (skip if still initial)
    if cur and cur != initial:
        when = r["updated_at"] or r["resolved_at"] or r["created_at"]
        c.execute(
            "INSERT INTO audit_logs (entity_type,entity_id,user_id,user_name,action,detail,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (entity, r["id"], uid, uname, "status",
             f"{_STATUS_LABELS_BACKFILL.get(initial, initial)} → {_STATUS_LABELS_BACKFILL.get(cur, cur)}",
             when),
        )


def hash_password(pw):
    salt = "labcare::"
    return hashlib.sha256((salt + pw).encode()).hexdigest()


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


def next_code(prefix):
    """Generate a human-friendly sequential code like CMP-0001."""
    c = conn()
    row = c.execute("SELECT COUNT(*) AS n FROM complaints").fetchone()
    c.close()
    return f"{prefix}-{row['n'] + 1:04d}"


def next_code_for(table, prefix):
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
