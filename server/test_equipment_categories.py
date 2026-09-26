"""Tests for the prepared equipment category list.

The Add equipment form used to offer only the categories a tenant's own
equipment already used, and only the master could add a new one — so a tenant
admin registering their first instrument faced an empty dropdown with no way to
fill it. These tests pin down the replacement behaviour:

* a database gets the prepared laboratory list on first run;
* seeding never resurrects a category the master deliberately deleted;
* every caller sees the whole shared list, but equipment counts stay scoped to
  what that caller may see (no cross-tenant totals leak);
* anyone who may add equipment — tenant admins and engineers alike — may add a
  category, while renaming and deleting remain master-only because those
  rewrite every tenant's records.

Run with:  python3 -m unittest test_equipment_categories
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

test_db = tempfile.mktemp(suffix=".db")
os.environ["LABCARE_DB"] = test_db
os.environ["LABCARE_SECRET"] = "test-secret"

from server.database import init_db, conn
init_db()
from server.app import app, hash_password
from server.seed import DEFAULT_CATEGORIES, seed_categories

PW = "Passw0rd!"
MASTER = "admin@labcare.com"


class EquipmentCategoryTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        c = conn()
        c.execute("PRAGMA foreign_keys = OFF")
        for t in ["pm_logs", "pm_schedules", "portal_links", "push_subscriptions", "app_devices",
                  "audit_logs", "comments", "notifications", "notification_pings",
                  "breakdowns", "complaints", "equipment", "departments", "locations",
                  "pending_care_declines", "admin_customer_links", "onboarding_apps",
                  "sessions", "users", "customers", "categories"]:
            c.execute("DELETE FROM %s" % t)
        c.execute("PRAGMA foreign_keys = ON")
        c.execute(
            "INSERT INTO users (name, email, password_hash, role, pending, active, created_at) "
            "VALUES (?, ?, ?, ?, 0, 1, ?)",
            ("Master Admin", MASTER, hash_password(PW), "admin", "2026-01-01 00:00:00"))
        c.commit()
        c.close()

        self.mh = self.auth(MASTER)
        self.org_a = self.make_customer("Org A")
        self.org_c = self.make_customer("Org C")
        self.make_user("TA One", "ta1@t.test", "admin", customer_id=self.org_a)
        self.make_user("TA Two", "ta2@t.test", "admin", customer_id=self.org_c)
        self.make_user("Eng One", "eng1@t.test", "engineer", customer_id=self.org_a)
        self.make_user("Cust One", "cust1@t.test", "customer", customer_id=self.org_a)
        self.t1 = self.auth("ta1@t.test")
        self.t2 = self.auth("ta2@t.test")
        self.eng = self.auth("eng1@t.test")
        self.cust = self.auth("cust1@t.test")

        # A real installation has the prepared list already seeded.
        c = conn()
        seed_categories(c)
        c.commit()
        c.close()

    # ------------------------------------------------------------------ helpers
    def auth(self, email):
        r = self.client.post("/api/login", json={"email": email, "password": PW})
        return {"Authorization": "Bearer " + r.get_json()["token"]}

    def post(self, url, hdrs, body=None):
        return self.client.post(url, headers=hdrs, json=body or {})

    def make_customer(self, name):
        return self.post("/api/customers", self.mh, {"name": name}).get_json()["id"]

    def make_user(self, name, email, role, **kw):
        body = {"name": name, "email": email, "password": PW, "role": role, "phone": "+60 12-000 0000"}
        body.update(kw)
        return self.post("/api/users", self.mh, body)

    def make_equipment(self, name, customer_id, hdrs, category="Centrifuges"):
        return self.post("/api/equipment", hdrs, {
            "name": name, "customer_id": customer_id, "category": category})

    def names(self, hdrs):
        return [c["name"] for c in self.client.get("/api/categories", headers=hdrs).get_json()]

    def counts(self, hdrs):
        return {c["name"]: c["equipment_count"]
                for c in self.client.get("/api/categories", headers=hdrs).get_json()}

    def db_names(self):
        c = conn()
        out = sorted(r["name"] for r in c.execute("SELECT name FROM categories").fetchall())
        c.close()
        return out

    # -------------------------------------------------------------------- tests
    def test_prepared_list_is_seeded_into_a_fresh_database(self):
        """The Add equipment form has a usable list from the very first run."""
        self.assertEqual(sorted(self.names(self.mh)), sorted(DEFAULT_CATEGORIES))
        # The laboratory staples a maintenance app is actually used for.
        for expected in ("Centrifuges", "PCR", "Cold Storage", "Chromatography",
                         "Spectroscopy", "Sterilization", "Analyzers", "Histology", "Other"):
            self.assertIn(expected, DEFAULT_CATEGORIES)

    def test_seeding_is_idempotent(self):
        c = conn()
        self.assertEqual(seed_categories(c), 0, "re-seeding an already seeded database added rows")
        c.commit()
        c.close()
        self.assertEqual(sorted(self.db_names()), sorted(DEFAULT_CATEGORIES))

    def test_seeding_does_not_resurrect_a_deleted_category(self):
        """A category the master removed on purpose must stay removed."""
        row = self.client.get("/api/categories", headers=self.mh).get_json()
        pcr = [c for c in row if c["name"] == "PCR"][0]
        self.assertEqual(self.client.delete("/api/categories/%d" % pcr["id"],
                                           headers=self.mh).status_code, 200)
        self.assertNotIn("PCR", self.db_names())

        c = conn()
        self.assertEqual(seed_categories(c), 0, "a deleted category was resurrected")
        c.commit()
        c.close()
        self.assertNotIn("PCR", self.db_names())

    def test_seeding_fills_a_database_of_only_hand_made_categories(self):
        """Those defaults were never offered before, so such an install gets them."""
        c = conn()
        c.execute("DELETE FROM categories")
        c.execute("INSERT INTO categories (name, created_at) VALUES ('Bespoke Rig', '2026-01-01')")
        added = seed_categories(c)
        c.commit()
        c.close()
        self.assertEqual(added, len(DEFAULT_CATEGORIES))
        names = self.db_names()
        self.assertIn("Bespoke Rig", names, "a hand-made category was dropped")
        for n in DEFAULT_CATEGORIES:
            self.assertIn(n, names)

    def test_matching_is_case_insensitive(self):
        c = conn()
        c.execute("DELETE FROM categories")
        c.execute("INSERT INTO categories (name, created_at) VALUES ('centrifuges', '2026-01-01')")
        self.assertEqual(seed_categories(c), 0, "a case difference caused a duplicate")
        c.commit()
        c.close()
        self.assertEqual(self.db_names(), ["centrifuges"])

    def test_every_tenant_sees_the_whole_shared_list(self):
        """Not just the categories their own equipment happens to use."""
        expected = sorted(DEFAULT_CATEGORIES)
        for hdrs, who in ((self.t1, "tenant admin one"), (self.t2, "tenant admin two"),
                          (self.eng, "engineer"), (self.cust, "customer user"),
                          (self.mh, "master")):
            self.assertEqual(sorted(self.names(hdrs)), expected,
                             "%s did not get the full list" % who)

    def test_a_fresh_tenant_is_not_staring_at_an_empty_dropdown(self):
        """The regression this fixes: no equipment yet meant no categories."""
        self.assertEqual(self.make_equipment("First Instrument", self.org_a, self.t1).status_code, 201)
        self.assertTrue(len(self.names(self.t1)) >= len(DEFAULT_CATEGORIES))

    def test_equipment_counts_stay_scoped_to_the_caller(self):
        """A tenant admin is not told how many instruments another tenant has."""
        self.make_equipment("Centrifuge A1", self.org_a, self.t1, "Centrifuges")
        self.make_equipment("Centrifuge A2", self.org_a, self.t1, "Centrifuges")
        self.make_equipment("Centrifuge C1", self.org_c, self.t2, "Centrifuges")

        self.assertEqual(self.counts(self.t1)["Centrifuges"], 2)
        self.assertEqual(self.counts(self.t2)["Centrifuges"], 1)
        # The master sees the whole platform.
        self.assertEqual(self.counts(self.mh)["Centrifuges"], 3)
        # A category nobody uses still reads zero rather than disappearing.
        self.assertEqual(self.counts(self.t1)["Histology"], 0)

    def test_a_tenant_admin_may_create_a_category(self):
        r = self.post("/api/categories", self.t1, {"name": "Mass Spectrometry"})
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(r.get_json()["name"], "Mass Spectrometry")
        # ...and it joins the shared list for every other tenant too.
        self.assertIn("Mass Spectrometry", self.names(self.t2))
        self.assertIn("Mass Spectrometry", self.names(self.mh))

    def test_an_engineer_may_create_a_category(self):
        r = self.post("/api/categories", self.eng, {"name": "Microscopy"})
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertIn("Microscopy", self.names(self.t1))

    def test_a_customer_user_may_not_create_one(self):
        """Customer accounts cannot add equipment either, so they cannot add categories."""
        r = self.post("/api/categories", self.cust, {"name": "Not Allowed"})
        self.assertEqual(r.status_code, 403, r.get_json())
        self.assertNotIn("Not Allowed", self.db_names())

    def test_anonymous_may_not_create_one(self):
        # A fresh client: self.client carries the login cookie from setUp, so
        # using it here would test the last logged-in role, not an anonymous one.
        anon = app.test_client()
        r = anon.post("/api/categories", json={"name": "Nope"})
        self.assertEqual(r.status_code, 401, r.get_json())
        self.assertNotIn("Nope", self.db_names())

    def test_a_blank_name_is_rejected(self):
        self.assertEqual(self.post("/api/categories", self.t1, {"name": "   "}).status_code, 400)

    def test_duplicates_are_rejected_case_insensitively(self):
        self.assertEqual(self.post("/api/categories", self.t1, {"name": "centrifuges"}).status_code, 409)
        self.assertEqual(self.post("/api/categories", self.t1, {"name": "Centrifuges"}).status_code, 409)
        self.assertEqual(self.db_names().count("Centrifuges"), 1)

    def test_new_category_survives_being_used_on_equipment(self):
        """The flow from the Add equipment form, end to end."""
        self.assertEqual(self.post("/api/categories", self.t1, {"name": "Water Purification"}).status_code, 201)
        r = self.make_equipment("Milli-Q System", self.org_a, self.t1, "Water Purification")
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(r.get_json()["category"], "Water Purification")
        self.assertEqual(self.counts(self.t1)["Water Purification"], 1)
        # Offered to the next tenant admin who opens the form.
        self.assertIn("Water Purification", self.names(self.t2))

    def test_renaming_stays_master_only(self):
        """Renaming rewrites every tenant's equipment, so it is not shared out."""
        cat = [c for c in self.client.get("/api/categories", headers=self.mh).get_json()
               if c["name"] == "Analyzers"][0]
        self.assertEqual(self.client.put("/api/categories/%d" % cat["id"], headers=self.t1,
                                        json={"name": "Chemistry Analyzers"}).status_code, 403)
        self.assertEqual(self.client.put("/api/categories/%d" % cat["id"], headers=self.eng,
                                        json={"name": "Chemistry Analyzers"}).status_code, 403)
        self.assertIn("Analyzers", self.db_names())

        self.assertEqual(self.client.put("/api/categories/%d" % cat["id"], headers=self.mh,
                                        json={"name": "Chemistry Analyzers"}).status_code, 200)
        self.assertIn("Chemistry Analyzers", self.db_names())

    def test_deleting_stays_master_only(self):
        cat = [c for c in self.client.get("/api/categories", headers=self.mh).get_json()
               if c["name"] == "Histology"][0]
        self.assertEqual(self.client.delete("/api/categories/%d" % cat["id"],
                                           headers=self.t1).status_code, 403)
        self.assertIn("Histology", self.db_names())
        self.assertEqual(self.client.delete("/api/categories/%d" % cat["id"],
                                           headers=self.mh).status_code, 200)
        self.assertNotIn("Histology", self.db_names())

    def test_other_is_the_protected_fallback(self):
        other = [c for c in self.client.get("/api/categories", headers=self.mh).get_json()
                 if c["name"] == "Other"][0]
        self.assertEqual(self.client.delete("/api/categories/%d" % other["id"],
                                           headers=self.mh).status_code, 409)
        self.assertIn("Other", self.db_names())


if __name__ == "__main__":
    unittest.main(verbosity=2)
