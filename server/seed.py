"""LabSynch — initial seed data.

InsForge policy: a *fresh* database, seeded with the single Master System
Admin account only (admin@labcare.com). Customers, users, equipment, tickets
and all other demo records are intentionally NOT created — the tenant admin
builds those in-app.

Runs automatically at startup (see run.py / wsgi.py) and is a no-op once any
user exists, so it never duplicates or clobbers real data.
"""
from database import conn, now, hash_password, init_db

MASTER_EMAIL = "admin@labcare.com"

# The prepared equipment categories offered in the Add equipment form. These are
# the same names the complaint form has always listed, so the two agree.
DEFAULT_CATEGORIES = (
    "General",
    "Centrifuges",
    "PCR",
    "Cold Storage",
    "Chromatography",
    "Spectroscopy",
    "Sterilization",
    "Analyzers",
    "Histology",
    "Other",
)


def seed_categories(c):
    """Add the prepared equipment categories; return how many were inserted.

    Called on every startup but acts at most once per database: as soon as any
    prepared category is present the list counts as curated and is left exactly
    as it was arranged, so a category the master deletes is not resurrected by
    the next restart. A database holding only hand-made categories still gets
    the prepared list, because those defaults were never offered before.

    Matching is case-insensitive, so "centrifuges" and "Centrifuges" are the
    same category."""
    rows = c.execute("SELECT name FROM categories").fetchall()
    have = {(r["name"] or "").strip().lower() for r in rows}
    if have & {n.lower() for n in DEFAULT_CATEGORIES}:
        return 0
    added = 0
    for name in DEFAULT_CATEGORIES:
        if name.lower() in have:
            continue
        c.execute("INSERT INTO categories (name, created_at) VALUES (?,?)", (name, now()))
        added += 1
    return added


def seed():
    init_db()
    c = conn()

    # Sessions are deliberately left alone. seed() runs on EVERY server start
    # (run.py / wsgi.py — so every deploy, container restart and Fly machine
    # restart), and an earlier "start each run from a clean session table"
    # here signed every user out of every device each time. A session ends
    # only when the user signs out, or an admin removes the account.

    # Categories are offered from the very first run, unlike the master account
    # below which is only created into a database with no users at all.
    added_cats = seed_categories(c)
    c.commit()
    if added_cats:
        print("Seed: added %d prepared equipment categories." % added_cats)

    existing = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    if existing:
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
