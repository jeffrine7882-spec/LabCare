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
    c.commit()
    c.close()


def _migrate(c):
    """Lightweight, additive migrations for databases created before these columns/tables existed."""
    user_cols = [r["name"] for r in c.execute("PRAGMA table_info(users)")]
    if "pending" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN pending INTEGER DEFAULT 0")


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
