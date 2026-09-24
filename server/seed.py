"""LabCare — initial seed data.

InsForge policy: a *fresh* database, seeded with the single Master System
Admin account only (admin@labcare.com). Customers, users, equipment, tickets
and all other demo records are intentionally NOT created — the tenant admin
builds those in-app.

Runs automatically at startup (see run.py / wsgi.py) and is a no-op once any
user exists, so it never duplicates or clobbers real data.
"""
from database import conn, now, hash_password, init_db

MASTER_EMAIL = "admin@labcare.com"


def seed():
    init_db()
    c = conn()

    # Start each run from a clean session table.
    c.execute("DELETE FROM sessions")

    existing = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    if existing:
        c.commit()
        c.close()
        return

    c.execute(
        "INSERT INTO users (name,email,phone,password_hash,role,customer_id,"
        "location_id,department_id,active,created_at) VALUES (?,?,?,?,?,?,?,?,1,?)",
        ("System Admin", MASTER_EMAIL, "", hash_password("Demo123!"), "admin",
         None, None, None, now()),
    )
    c.commit()
    c.close()
    print("Seed complete: Master System Admin account created (admin@labcare.com).")


if __name__ == "__main__":
    seed()
