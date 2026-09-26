"""Reset the InsForge database to a FRESH state: schema stays, all rows are
wiped, and only the Master System Admin is re-seeded."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg

DSN = os.environ.get("LABCARE_DATABASE_URL")
if not DSN:
    print("set LABCARE_DATABASE_URL")
    sys.exit(2)

TABLES = [
    "sessions", "notification_pings", "notifications",
    "audit_logs", "comments", "pm_logs", "pm_schedules", "portal_links",
    "breakdowns", "complaints", "equipment", "categories", "departments",
    "locations", "admin_customer_links", "onboarding_apps", "users",
    "customers",
]

conn = psycopg.connect(
    DSN, connect_timeout=15,
    options="-c statement_timeout=120000 -c idle_in_transaction_session_timeout=60000")
cur = conn.cursor()
# identity columns restart from 1 for a genuinely clean slate
cur.execute("TRUNCATE TABLE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE")
conn.commit()

from database import now, hash_password
cur.execute(
    "INSERT INTO users (name,email,phone,password_hash,role,customer_id,"
    "location_id,department_id,active,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,%s)",
    ("System Admin", "admin@labcare.com", "", hash_password("Demo123!"), "admin",
     None, None, None, now()))
conn.commit()

cur.execute("SELECT id,name,email,role FROM users")
print("users now:", cur.fetchall())
for t in TABLES:
    cur.execute(f"SELECT COUNT(*) FROM {t}")
    print(f"  {t}: {cur.fetchone()[0]}")
conn.close()
print("RESET COMPLETE")
