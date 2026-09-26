"""Who hears a ticket alert, and who may delete accounts / organizations.

Alert rules:
* Tenant admins, engineers and applications hear ONLY tickets of the
  organizations they are linked to (care list / bound organization / linked
  organizations / linked tenant admin's care list). Only the master and a
  LabSynch-wide engineer (no organization, no links, no tenant admin) hear
  every organization.
* Customer users hear the tickets of the organization they are linked to —
  and, when they are linked to a location, only that location's tickets — not
  merely the tickets they raised themselves.

Deletion rules:
* Only the master may delete a user (customer, staff or tenant admin).
* Only the master may delete an organization; a tenant admin cannot delete
  one even when it is under their care (they may still drop it from their
  care list, and still edit / disable the accounts they manage).

Run with:  python3 -m unittest test_alert_audience
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

PW = "Passw0rd!"
MASTER = "admin@labcare.com"


class AlertAudienceTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        c = conn()
        c.execute("PRAGMA foreign_keys = OFF")
        for t in ["pm_logs", "pm_schedules", "portal_links", "push_subscriptions", "app_devices",
                  "audit_logs", "comments", "notifications", "notification_pings",
                  "breakdowns", "complaints", "equipment", "departments", "locations",
                  "staff_customer_links", "admin_customer_links", "onboarding_apps",
                  "sessions", "users", "customers"]:
            c.execute(f"DELETE FROM {t}")
        c.execute("PRAGMA foreign_keys = ON")
        c.execute(
            "INSERT INTO users (name, email, password_hash, role, pending, active, created_at) "
            "VALUES (?, ?, ?, ?, 0, 1, ?)",
            ("Master Admin", MASTER, hash_password(PW), "admin", "2026-01-01 00:00:00"))
        c.commit()
        c.close()
        self.mh = self.auth(MASTER)

        # Organizations: TA1 cares for A (primary) + B; TA2 cares for D.
        self.org_a = self.make_customer("Org A")
        self.org_b = self.make_customer("Org B")
        self.org_d = self.make_customer("Org D")
        self.make_user("TA One", "ta1@t.test", "admin", customer_id=self.org_a)
        self.make_user("TA Two", "ta2@t.test", "admin", customer_id=self.org_d)
        self.t1 = self.auth("ta1@t.test")
        self.t2 = self.auth("ta2@t.test")
        self.post("/api/my-customers/%d" % self.org_b, self.t1)
        self.ta1 = self.me(self.t1)

        # Two locations in Org A.
        self.loc1 = self.post("/api/locations", self.t1, {"customer_id": self.org_a, "name": "Lab 1"}).get_json()["id"]
        self.loc2 = self.post("/api/locations", self.t1, {"customer_id": self.org_a, "name": "Lab 2"}).get_json()["id"]

        # Staff with different kinds of links.
        self.make_user("Eng A", "eng.a@t.test", "engineer", customer_id=self.org_a)          # bound to A
        r = self.post("/api/users", self.t1, {"name": "Eng B", "email": "eng.b@t.test", "password": PW,
                                              "role": "engineer", "customer_ids": [self.org_b]})  # linked to B
        self.assertEqual(r.status_code, 201, r.get_json())
        r = self.post("/api/users", self.t1, {"name": "Eng Care", "email": "eng.care@t.test",
                                              "password": PW, "role": "application"})           # TA1's care list
        self.assertEqual(r.status_code, 201, r.get_json())
        self.make_user("Eng Wide", "eng.wide@t.test", "engineer")                              # LabSynch-wide

        # Customer users: Org A (no location), Org A @ Lab 1, Org A @ Lab 2, Org D.
        self.make_user("Cust A", "cust.a@t.test", "customer", customer_id=self.org_a)
        self.make_user("Cust A1", "cust.a1@t.test", "customer", customer_id=self.org_a, location_id=self.loc1)
        self.make_user("Cust A2", "cust.a2@t.test", "customer", customer_id=self.org_a, location_id=self.loc2)
        self.make_user("Cust D", "cust.d@t.test", "customer", customer_id=self.org_d)

        self.h = {e: self.auth(e) for e in (
            "ta1@t.test", "ta2@t.test", "eng.a@t.test", "eng.b@t.test", "eng.care@t.test",
            "eng.wide@t.test", "cust.a@t.test", "cust.a1@t.test", "cust.a2@t.test", "cust.d@t.test")}
        self.h[MASTER] = self.mh

    # ------------------------------------------------------------------ helpers
    def auth(self, email):
        r = self.client.post("/api/login", json={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.get_json())
        return {"Authorization": "Bearer " + r.get_json()["token"]}

    def me(self, hdrs):
        return self.client.get("/api/me", headers=hdrs).get_json()

    def post(self, url, hdrs, body=None):
        return self.client.post(url, headers=hdrs, json=body or {})

    def patch(self, url, hdrs, body):
        return self.client.patch(url, headers=hdrs, json=body)

    def make_customer(self, name):
        return self.post("/api/customers", self.mh, {"name": name}).get_json()["id"]

    def make_user(self, name, email, role, **kw):
        body = {"name": name, "email": email, "password": PW, "role": role}
        body.update(kw)
        r = self.post("/api/users", self.mh, body)
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()

    def user_id(self, email):
        return [u for u in self.client.get("/api/users", headers=self.mh).get_json()
                if u["email"] == email][0]["id"]

    def complaint(self, hdrs, org, location_id=None, subject="s"):
        body = {"customer_id": org, "subject": subject, "description": "d", "priority": "low"}
        if location_id:
            body["location_id"] = location_id
        r = self.post("/api/complaints", hdrs, body)
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()

    def heard(self, email, code):
        notes = self.client.get("/api/notifications", headers=self.h[email]).get_json()
        return any(code in (n.get("text") or "") for n in notes)

    def assert_audience(self, code, hear, silent):
        for e in hear:
            self.assertTrue(self.heard(e, code), "%s should have been alerted about %s" % (e, code))
        for e in silent:
            self.assertFalse(self.heard(e, code), "%s must NOT be alerted about %s" % (e, code))

    # ------------------------------------------------------- staff: organization
    def test_staff_hear_only_the_organizations_they_are_linked_to(self):
        t = self.complaint(self.h["eng.wide@t.test"], self.org_a, self.loc1)
        self.assert_audience(
            t["code"],
            hear=[MASTER, "ta1@t.test", "eng.a@t.test", "eng.care@t.test"],
            silent=["ta2@t.test", "eng.b@t.test", "cust.d@t.test", "eng.wide@t.test"])  # eng.wide = actor

    def test_linked_engineer_hears_only_the_linked_organization(self):
        t = self.complaint(self.h["eng.wide@t.test"], self.org_b)
        self.assert_audience(
            t["code"],
            hear=[MASTER, "ta1@t.test", "eng.b@t.test", "eng.care@t.test"],
            silent=["eng.a@t.test", "ta2@t.test", "cust.a@t.test", "cust.a1@t.test"])

    def test_another_tenants_ticket_reaches_only_the_master_and_labsynch_wide_staff(self):
        t = self.complaint(self.h["ta2@t.test"], self.org_d)
        self.assert_audience(
            t["code"],
            hear=[MASTER, "eng.wide@t.test", "cust.d@t.test"],
            silent=["ta1@t.test", "eng.a@t.test", "eng.b@t.test", "eng.care@t.test",
                    "cust.a@t.test", "cust.a1@t.test", "cust.a2@t.test"])

    def test_dropping_an_organization_silences_its_alerts_for_the_tenant(self):
        self.client.delete("/api/my-customers/%d" % self.org_b, headers=self.t1)
        t = self.complaint(self.mh, self.org_b)
        self.assert_audience(t["code"], hear=["eng.wide@t.test"],
                             silent=["ta1@t.test", "eng.b@t.test", "eng.care@t.test"])

    # ------------------------------------------------ customers: org + location
    def test_customers_hear_their_organization_and_location(self):
        t = self.complaint(self.h["eng.wide@t.test"], self.org_a, self.loc1)
        self.assert_audience(
            t["code"],
            hear=["cust.a@t.test", "cust.a1@t.test"],            # organization-wide, and Lab 1
            silent=["cust.a2@t.test", "cust.d@t.test"])          # Lab 2, another organization

    def test_customer_without_a_location_hears_the_whole_organization(self):
        t1 = self.complaint(self.h["eng.wide@t.test"], self.org_a, self.loc1)
        t2 = self.complaint(self.h["eng.wide@t.test"], self.org_a, self.loc2)
        t3 = self.complaint(self.h["eng.wide@t.test"], self.org_a)
        for t in (t1, t2, t3):
            self.assertTrue(self.heard("cust.a@t.test", t["code"]))
        self.assertTrue(self.heard("cust.a2@t.test", t2["code"]))
        self.assertFalse(self.heard("cust.a2@t.test", t1["code"]))
        self.assertFalse(self.heard("cust.a2@t.test", t3["code"]),
                         "a location-linked customer is not alerted for organization-level tickets")

    def test_customers_at_the_location_follow_status_and_comments_too(self):
        t = self.complaint(self.h["eng.wide@t.test"], self.org_a, self.loc1)
        cid = t["id"]
        # a colleague's status change
        r = self.patch("/api/complaints/%d" % cid, self.h["eng.a@t.test"], {"status": "resolved"})
        self.assertEqual(r.status_code, 200, r.get_json())
        notes = lambda e: [n["text"] for n in self.client.get(
            "/api/notifications", headers=self.h[e]).get_json() if t["code"] in n["text"]]
        self.assertTrue(any("now" in x for x in notes("cust.a1@t.test")), notes("cust.a1@t.test"))
        self.assertFalse(any("now" in x for x in notes("cust.a2@t.test")))
        # ...and a comment
        r = self.post("/api/comments", self.h["cust.a1@t.test"],
                      {"entity_type": "complaint", "entity_id": cid, "text": "still broken"})
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertTrue(any("commented" in x for x in notes("cust.a@t.test")))
        self.assertTrue(any("commented" in x for x in notes("ta1@t.test")))
        self.assertFalse(any("commented" in x for x in notes("cust.a2@t.test")))
        self.assertFalse(any("commented" in x for x in notes("ta2@t.test")))

    def test_breakdown_alerts_follow_the_equipment_location(self):
        eq = self.post("/api/equipment", self.t1, {"name": "Analyzer", "customer_id": self.org_a,
                                                    "location_id": self.loc2}).get_json()
        r = self.post("/api/breakdowns", self.h["eng.wide@t.test"],
                      {"customer_id": self.org_a, "equipment_id": eq["id"],
                       "fault_description": "no power", "priority": "high"})
        self.assertEqual(r.status_code, 201, r.get_json())
        code = r.get_json()["code"]
        self.assert_audience(code,
                             hear=["cust.a@t.test", "cust.a2@t.test", "ta1@t.test", "eng.a@t.test"],
                             silent=["cust.a1@t.test", "cust.d@t.test", "ta2@t.test", "eng.b@t.test"])

    # ------------------------------------------------------------- deletions
    def test_only_the_master_deletes_users(self):
        for email in ("eng.a@t.test", "eng.care@t.test", "cust.a1@t.test"):
            uid = self.user_id(email)
            r = self.client.delete("/api/users/%d" % uid, headers=self.t1)
            self.assertEqual(r.status_code, 403, (email, r.get_json()))
        # a tenant admin can never remove a tenant admin either
        r = self.client.delete("/api/users/%d" % self.user_id("ta2@t.test"), headers=self.t1)
        self.assertEqual(r.status_code, 403)
        # ...but may still edit and disable the accounts under their care
        uid = self.user_id("eng.a@t.test")
        self.assertEqual(self.patch("/api/users/%d" % uid, self.t1, {"name": "Eng A2"}).status_code, 200)
        self.assertEqual(self.patch("/api/users/%d" % uid, self.t1, {"active": 0}).status_code, 200)
        # the master deletes anyone — staff, customers and tenant admins
        for email in ("eng.a@t.test", "cust.a1@t.test", "ta2@t.test"):
            r = self.client.delete("/api/users/%d" % self.user_id(email), headers=self.mh)
            self.assertEqual(r.status_code, 200, (email, r.get_json()))
        emails = [u["email"] for u in self.client.get("/api/users", headers=self.mh).get_json()]
        self.assertNotIn("ta2@t.test", emails)

    def test_only_the_master_deletes_organizations(self):
        org_e = self.make_customer("Org E")               # empty, deletable
        self.post("/api/my-customers/%d" % org_e, self.t1)
        self.assertIn(org_e, [x["id"] for x in self.client.get("/api/customers", headers=self.t1).get_json()])
        # under their care, and even their own primary organization: still no
        r = self.client.delete("/api/customers/%d" % org_e, headers=self.t1)
        self.assertEqual(r.status_code, 403, r.get_json())
        r = self.client.delete("/api/customers/%d" % self.org_a, headers=self.t1)
        self.assertEqual(r.status_code, 403, r.get_json())
        r = self.client.delete("/api/customers/%d" % org_e, headers=self.h["eng.wide@t.test"])
        self.assertEqual(r.status_code, 403, r.get_json())
        # dropping it from the care list is still theirs
        r = self.client.delete("/api/my-customers/%d" % org_e, headers=self.t1)
        self.assertEqual(r.status_code, 200, r.get_json())
        # the master deletes it
        r = self.client.delete("/api/customers/%d" % org_e, headers=self.mh)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertNotIn(org_e, [x["id"] for x in self.client.get("/api/customers", headers=self.mh).get_json()])


if __name__ == "__main__":
    unittest.main()
