"""LabCare — reset the database to the minimal demo dataset.

Wipes every table, then creates exactly:

  * 1 Master System Admin            admin@labcare.com
  * 1 tenant admin                   admin.bioref@labcare.com
  * 1 customer organisation          BioReference Labs  (created BY that admin)
  * 1 technician                     tech.bioref@labcare.com
  * 1 customer user                  user.bioref@labcare.com
  * 1 piece of equipment             Centrifuge — Eppendorf 5810R (BRF-CEN-001)
  * 1 complaint ticket               CMP-0001  (in-app, also listed in the portal)
  * 1 breakdown ticket               BRK-0001  (in-app, also listed in the portal)
  * 1 portal (QR) link               so the tickets are visible on the public portal

Every account uses the password **Demo123!**.

Run against the live InsForge Postgres::

    LABCARE_DATABASE_URL="$(npx -y @insforge/cli db connection-string)" \
        python3 server/seed_minimal.py

…or against a local SQLite file (useful for testing)::

    LABCARE_DB=/tmp/labcare.db python3 server/seed_minimal.py

The script is destructive by design (it is a reset), and idempotent: running it
again always ends with the same minimal dataset.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import database
from database import conn, now, hash_password, init_db

MASTER_EMAIL = "admin@labcare.com"
PASSWORD = "Demo123!"

ORG_NAME = "BioReference Labs"
ORG_CONTACT = {
    "contact_name": "Lab Manager",
    "email": "lab@bioref.example",
    "phone": "04-123 4567",
    "address": "1 Jalan Sultan Azlan Shah",
    "city": "George Town",
}

TENANT_ADMIN = {"name": "Sarah Lim", "email": "admin.bioref@labcare.com"}
TECHNICIAN = {"name": "Aidil Rahman", "email": "tech.bioref@labcare.com"}
CUSTOMER_USER = {"name": "Mei Ling Tan", "email": "user.bioref@labcare.com"}

EQUIPMENT = {
    "name": "Centrifuge",
    "model": "Eppendorf 5810R",
    "serial_number": "BRF-CEN-001",
    "category": "Centrifuges",
}

COMPLAINT = {
    "subject": "Centrifuge making a grinding noise",
    "description": "Loud grinding noise during the spin cycle; runs are being aborted.",
    "priority": "high",
}
BREAKDOWN = {
    "fault_description": "Rotor seized mid-run — controller shows error E-04.",
    "priority": "high",
}
PORTAL_LABEL = "Centrifuge QR"

# Children first, so plain DELETEs stay referentially safe on SQLite.
TABLES = [
    "attachments", "audit_logs", "comments", "notifications", "notification_pings",
    "push_subscriptions", "sessions", "pm_logs", "pm_schedules", "portal_links",
    "breakdowns", "complaints", "equipment", "categories", "departments",
    "locations", "onboarding_apps", "users", "customers",
]

# Retired table from the removed tenant-admin "care list" feature.
LEGACY_TABLES = ["admin_customer_links"]


def log(msg):
    print(f"  {msg}")


def is_postgres():
    # PG_ENABLED is True only when a DSN is set *and* psycopg is importable.
    return bool(getattr(database, "PG_ENABLED", False))


def wipe(c):
    """Delete every row (and reset id counters) so the seed starts from zero."""
    if is_postgres():
        # One statement, and identity columns restart at 1.
        c.execute("TRUNCATE TABLE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE")
    else:
        for t in TABLES:
            try:
                c.execute(f"DELETE FROM {t}")
            except Exception:
                pass  # table not present in this (older) SQLite file
        try:
            c.execute("DELETE FROM sqlite_sequence")
        except Exception:
            pass
    for t in LEGACY_TABLES:
        c.execute(f"DROP TABLE IF EXISTS {t}")
    c.commit()


def login(cl, email):
    r = cl.post("/api/login", json={"email": email, "password": PASSWORD})
    body = r.get_json() or {}
    if r.status_code != 200 or not body.get("token"):
        raise SystemExit(f"login failed for {email}: {r.status_code} {body}")
    return body["token"], body["user"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def create(cl, token, path, payload, what):
    r = cl.post(path, json=payload, headers=auth(token))
    if r.status_code not in (200, 201):
        raise SystemExit(f"could not create {what}: {r.status_code} {r.get_data(as_text=True)[:200]}")
    log(f"created {what}")
    return r.get_json()


def seed():
    """Insert the master account, then build the dataset through the real API."""
    c = conn()
    c.execute(
        "INSERT INTO users (name,email,phone,password_hash,role,customer_id,"
        "location_id,department_id,active,created_at) VALUES (?,?,?,?,?,?,?,?,1,?)",
        ("System Admin", MASTER_EMAIL, "", hash_password(PASSWORD), "admin",
         None, None, None, now()),
    )
    c.commit()
    c.close()
    log(f"created Master System Admin ({MASTER_EMAIL})")

    # Import late: app.py reads the DB config at import time.
    from app import app

    cl = app.test_client()
    master_tok, _ = login(cl, MASTER_EMAIL)

    # The master creates the tenant admin WITHOUT an organisation: the admin
    # creates their own organisation, which then becomes theirs.
    create(cl, master_tok, "/api/users", {
        "name": TENANT_ADMIN["name"], "email": TENANT_ADMIN["email"],
        "password": PASSWORD, "role": "admin",
    }, f"tenant admin {TENANT_ADMIN['email']} (no organisation yet)")

    tenant_tok, tenant_me = login(cl, TENANT_ADMIN["email"])
    if tenant_me.get("customer_id"):
        raise SystemExit("expected the new tenant admin to have no organisation yet")

    org = create(cl, tenant_tok, "/api/customers", {"name": ORG_NAME, **ORG_CONTACT},
                 f"organisation '{ORG_NAME}' (created by the tenant admin)")
    cid = org["id"]
    _, tenant_me = login(cl, TENANT_ADMIN["email"])
    if tenant_me.get("customer_id") != cid:
        raise SystemExit("the tenant admin was not linked to the organisation they created")

    # ...and staffs it: a technician and a customer user for their own organisation.
    create(cl, tenant_tok, "/api/users", {
        "name": TECHNICIAN["name"], "email": TECHNICIAN["email"],
        "password": PASSWORD, "role": "technician", "customer_id": cid,
    }, f"technician {TECHNICIAN['email']}")

    create(cl, tenant_tok, "/api/users", {
        "name": CUSTOMER_USER["name"], "email": CUSTOMER_USER["email"],
        "password": PASSWORD, "role": "customer", "customer_id": cid,
    }, f"customer user {CUSTOMER_USER['email']}")

    equipment = create(cl, tenant_tok, "/api/equipment",
                       {"customer_id": cid, **EQUIPMENT}, f"equipment {EQUIPMENT['serial_number']}")
    eid = equipment["id"]

    # The tenant admin files the tickets — the same path a real tenant admin uses.
    complaint = create(cl, tenant_tok, "/api/complaints",
                       {"customer_id": cid, "equipment_id": eid, **COMPLAINT}, "complaint ticket")
    breakdown = create(cl, tenant_tok, "/api/breakdowns",
                       {"customer_id": cid, "equipment_id": eid, **BREAKDOWN}, "breakdown ticket")

    # A portal link so the same tickets are visible on the public QR portal.
    portal = create(cl, tenant_tok, "/api/portal-links",
                    {"customer_id": cid, "equipment_id": eid, "label": PORTAL_LABEL},
                    "portal (QR) link")

    return {
        "org": org, "equipment": equipment,
        "complaint": complaint, "breakdown": breakdown, "portal": portal,
    }


def report(out):
    counts = {}
    c = conn()
    for t in TABLES:
        try:
            counts[t] = c.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
        except Exception:
            counts[t] = None
    c.close()

    print("\nDatabase now holds:")
    for t in ("customers", "users", "equipment", "complaints", "breakdowns",
              "portal_links", "locations", "departments", "categories",
              "pm_schedules", "comments", "attachments"):
        print(f"  {t:<16} {counts.get(t)}")
    leftovers = {t: n for t, n in counts.items() if n}
    extra = sorted(k for k in leftovers if k not in ("customers", "users", "equipment",
                                                     "complaints", "breakdowns", "portal_links"))
    if extra:
        print("  (also present, as by-products of the seeded tickets: "
              + ", ".join(f"{k}={leftovers[k]}" for k in extra) + ")")

    print("\nSign in (password %s):" % PASSWORD)
    print(f"  Master System Admin  {MASTER_EMAIL}")
    print(f"  Tenant admin         {TENANT_ADMIN['email']}   ({ORG_NAME})")
    print(f"  Technician           {TECHNICIAN['email']}")
    print(f"  Customer             {CUSTOMER_USER['email']}")
    print(f"\n  Tickets              {out['complaint']['code']} (complaint), "
          f"{out['breakdown']['code']} (breakdown)")
    print(f"  Portal link          /portal.html?t={out['portal']['token']}")


def main():
    target = "InsForge Postgres" if is_postgres() else os.environ.get("LABCARE_DB", "SQLite (default)")
    if not is_postgres() and not os.environ.get("LABCARE_DB"):
        print("Refusing to reset the default SQLite file. Set LABCARE_DATABASE_URL "
              "(InsForge) or LABCARE_DB (a scratch SQLite file).")
        sys.exit(2)

    print(f"Resetting to the minimal demo dataset on {target}\n")
    init_db()                      # make sure the schema exists/migrated
    c = conn()
    wipe(c)
    c.close()
    print("  wiped all tables\n")
    out = seed()
    report(out)


if __name__ == "__main__":
    main()
