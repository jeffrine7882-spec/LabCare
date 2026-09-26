"""Tests for tenant admins creating user accounts.

Covers the rule that a tenant admin may add a staff account WITHOUT linking it
to a single organisation: the account instead sits under a "linked tenant admin"
and inherits exactly that admin's care list.

The security property that matters most is the negative one — a customer-less
account must NOT fall through to system-wide visibility. `tenant_scope()`
returns None for "unscoped", and None means every organisation on the platform,
so a customer-less account with no tenant-admin link would silently see other
tenants' data. These tests pin that down from both directions.

Run with:  python3 -m unittest test_tenant_admin_users
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


class TenantAdminCreatesUsersTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        c = conn()
        c.execute("PRAGMA foreign_keys = OFF")
        for t in ["pm_logs", "pm_schedules", "portal_links", "push_subscriptions", "app_devices",
                  "audit_logs", "comments", "notifications", "notification_pings",
                  "breakdowns", "complaints", "equipment", "departments", "locations",
                  "admin_customer_links", "onboarding_apps", "sessions", "users", "customers"]:
            c.execute(f"DELETE FROM {t}")
        c.execute("PRAGMA foreign_keys = ON")
        c.execute(
            "INSERT INTO users (name, email, password_hash, role, pending, active, created_at) "
            "VALUES (?, ?, ?, ?, 0, 1, ?)",
            ("Master Admin", MASTER, hash_password(PW), "admin", "2026-01-01 00:00:00"))
        c.commit()
        c.close()

        self.mh = self.auth(MASTER)

        # Two separate tenants: TA1 cares for Org A and Org B, TA2 for Org C only.
        self.org_a = self.make_customer("Org A")
        self.org_b = self.make_customer("Org B")
        self.org_c = self.make_customer("Org C")
        self.make_user("TA One", "ta1@t.test", "admin", customer_id=self.org_a)
        self.make_user("TA Two", "ta2@t.test", "admin", customer_id=self.org_c)
        self.t1 = self.auth("ta1@t.test")
        self.t2 = self.auth("ta2@t.test")
        self.post("/api/my-customers/%d" % self.org_b, self.t1)   # TA1 += Org B
        self.ta1 = self.me(self.t1)
        self.ta2 = self.me(self.t2)

        # One complaint per organisation so visibility is directly observable.
        for org, hdrs in ((self.org_a, self.t1), (self.org_b, self.t1), (self.org_c, self.t2)):
            self.post("/api/complaints", hdrs, {
                "customer_id": org, "subject": "ticket in %d" % org,
                "description": "d", "priority": "low"})

    # ------------------------------------------------------------------ helpers
    def auth(self, email):
        r = self.client.post("/api/login", json={"email": email, "password": PW})
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
        return self.post("/api/users", self.mh, body)

    def visible_orgs(self, hdrs):
        rows = self.client.get("/api/complaints", headers=hdrs).get_json()
        return sorted(r["customer_id"] for r in rows)

    # -------------------------------------------------------------------- tests
    def test_staff_account_needs_no_organisation(self):
        """A tenant admin can add an engineer with no organisation at all."""
        r = self.post("/api/users", self.t1, {
            "name": "Eng NoOrg", "email": "e1@t.test", "password": PW, "role": "engineer"})
        self.assertEqual(r.status_code, 201, r.get_json())
        d = r.get_json()
        self.assertIsNone(d["customer_id"])
        # ...and the account is linked to the tenant admin who created it.
        self.assertEqual(d["responsible_admin_id"], self.ta1["id"])

    def test_customer_less_staff_are_scoped_to_the_care_list(self):
        """The security property: they see their tenant admin's organisations only.

        Not one organisation (they have none) and crucially not every
        organisation on the platform, which is what an unscoped account sees.
        """
        self.post("/api/users", self.t1, {
            "name": "Eng NoOrg", "email": "e1@t.test", "password": PW, "role": "engineer"})
        seen = self.visible_orgs(self.auth("e1@t.test"))
        self.assertEqual(seen, sorted([self.org_a, self.org_b]))
        self.assertNotIn(self.org_c, seen, "leaked another tenant's organisation")

    def test_organisation_may_still_be_chosen_and_is_coerced(self):
        """Naming an organisation still works — including as a string.

        Browsers send <select> values as strings while care-list scopes hold
        ints; without coercion this legitimately-chosen organisation used to be
        rejected with 403 "an organisation you care for".
        """
        r = self.post("/api/users", self.t1, {
            "name": "Eng StrId", "email": "e2@t.test", "password": PW,
            "role": "engineer", "customer_id": str(self.org_a)})
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(r.get_json()["customer_id"], self.org_a)
        self.assertIsInstance(r.get_json()["customer_id"], int)
        self.assertEqual(self.visible_orgs(self.auth("e2@t.test")), [self.org_a])

    def test_organisation_outside_the_care_list_is_rejected(self):
        r = self.post("/api/users", self.t1, {
            "name": "Eng Far", "email": "e5@t.test", "password": PW,
            "role": "engineer", "customer_id": self.org_c})
        self.assertEqual(r.status_code, 403)

    def test_customer_role_still_requires_an_organisation(self):
        """Only staff roles may skip it — customer accounts are an organisation's
        own people, so they must belong to one."""
        r = self.post("/api/users", self.t1, {
            "name": "Cust NoOrg", "email": "c1@t.test", "password": PW, "role": "customer"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "Linked organization is required for customer accounts")
        r = self.post("/api/users", self.t1, {
            "name": "Cust Org", "email": "c2@t.test", "password": PW,
            "role": "customer", "customer_id": str(self.org_b)})
        self.assertEqual(r.status_code, 201, r.get_json())

    def test_cannot_hand_an_account_to_an_unrelated_tenant_admin(self):
        """TA1 may not place an account under TA2, who shares no organisation."""
        r = self.post("/api/users", self.t1, {
            "name": "Eng Hijack", "email": "e3@t.test", "password": PW,
            "role": "engineer", "responsible_admin_id": self.ta2["id"]})
        self.assertEqual(r.status_code, 403)
        self.assertIn("does not care for any organization you manage", r.get_json()["error"])

    def test_masters_labcare_wide_staff_are_unchanged(self):
        """No regression: the master's unbound engineers still see everything."""
        r = self.make_user("Eng Wide", "w1@t.test", "engineer")
        self.assertEqual(r.status_code, 201, r.get_json())
        d = r.get_json()
        self.assertIsNone(d["customer_id"])
        self.assertFalse(d["responsible_admin_id"])
        self.assertEqual(self.visible_orgs(self.auth("w1@t.test")),
                         sorted([self.org_a, self.org_b, self.org_c]))

    def test_unbound_tenant_admins_staff_see_nothing(self):
        """A tenant admin with no organisations yet has an empty care list, so the
        accounts they create see nothing — never everything."""
        self.make_user("TA New", "ta3@t.test", "admin")
        t3 = self.auth("ta3@t.test")
        r = self.post("/api/users", t3, {
            "name": "Eng Orphan", "email": "e4@t.test", "password": PW, "role": "engineer"})
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(self.visible_orgs(self.auth("e4@t.test")), [])

    def test_editing_a_customer_less_account(self):
        eng = self.post("/api/users", self.t1, {
            "name": "Eng NoOrg", "email": "e1@t.test", "password": PW, "role": "engineer"}).get_json()
        uid = eng["id"]
        # its own tenant admin may edit it
        r = self.patch("/api/users/%d" % uid, self.t1, {"name": "Renamed"})
        self.assertEqual(r.status_code, 200, r.get_json())
        # an unrelated tenant admin may not
        r = self.patch("/api/users/%d" % uid, self.t2, {"name": "Hijack"})
        self.assertEqual(r.status_code, 403)
        # it can be attached to an organisation later...
        r = self.patch("/api/users/%d" % uid, self.t1, {"customer_id": self.org_b})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["customer_id"], self.org_b)
        self.assertEqual(self.visible_orgs(self.auth("e1@t.test")), [self.org_b])
        # ...but only one the tenant admin actually cares for
        r = self.patch("/api/users/%d" % uid, self.t1, {"customer_id": self.org_c})
        self.assertEqual(r.status_code, 403)

    def test_cannot_clear_the_organisation_on_a_customer_account(self):
        cust = self.post("/api/users", self.t1, {
            "name": "Cust Org", "email": "c2@t.test", "password": PW,
            "role": "customer", "customer_id": self.org_b}).get_json()
        r = self.patch("/api/users/%d" % cust["id"], self.t1, {"customer_id": None})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "Linked organization is required for customer accounts")


if __name__ == "__main__":
    unittest.main(verbosity=2)
