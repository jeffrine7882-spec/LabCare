"""Tests for linking an engineer/application account to SEVERAL organizations.

On the Add team member form an admin may tick the organizations a staff account
serves. The rules:

* Only admins set it (the form and `POST/PATCH /api/users` are admin-only); a
  tenant admin may only tick organizations on their own care list, and every
  ticked organization must be under the linked tenant admin's care.
* A staff account is NOT limited to one organization — it can be linked to
  any number of them.
* Nothing ticked = the account reaches EVERY organization under the linked
  tenant admin's care (the previous behaviour, unchanged).

The security property that matters most is still the negative one: a staff
account must never reach an organization its tenant admin does not care for,
and an EMPTY reach must mean "nothing", never "everything".

Run with:  python3 -m unittest test_staff_customer_links
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


class StaffCustomerLinksTest(unittest.TestCase):
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

        # TA1 cares for Org A, Org B and Org C; TA2 cares for Org D only.
        self.org_a = self.make_customer("Org A")
        self.org_b = self.make_customer("Org B")
        self.org_c = self.make_customer("Org C")
        self.org_d = self.make_customer("Org D")
        self.make_user("TA One", "ta1@t.test", "admin", customer_id=self.org_a)
        self.make_user("TA Two", "ta2@t.test", "admin", customer_id=self.org_d)
        self.t1 = self.auth("ta1@t.test")
        self.t2 = self.auth("ta2@t.test")
        self.post("/api/my-customers/%d" % self.org_b, self.t1)
        self.post("/api/my-customers/%d" % self.org_c, self.t1)
        self.ta1 = self.me(self.t1)
        self.ta2 = self.me(self.t2)

        # One complaint per organization so reach is directly observable.
        for org, hdrs in ((self.org_a, self.t1), (self.org_b, self.t1),
                          (self.org_c, self.t1), (self.org_d, self.t2)):
            r = self.post("/api/complaints", hdrs, {
                "customer_id": org, "subject": "ticket in %d" % org,
                "description": "d", "priority": "low"})
            self.assertEqual(r.status_code, 201, r.get_json())

    # ------------------------------------------------------------------ helpers
    def auth(self, email):
        r = self.client.post("/api/login", json={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.get_json())
        return {"Authorization": "Bearer " + r.get_json()["token"]}

    def me(self, hdrs):
        return self.client.get("/api/me", headers=hdrs).get_json()

    def get(self, url, hdrs):
        return self.client.get(url, headers=hdrs)

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

    def add_staff(self, hdrs, email, **kw):
        body = {"name": "Staff " + email, "email": email, "password": PW, "role": "engineer"}
        body.update(kw)
        return self.post("/api/users", hdrs, body)

    def visible_orgs(self, hdrs):
        rows = self.get("/api/complaints", hdrs).get_json()
        return sorted(r["customer_id"] for r in rows)

    def visible_customers(self, hdrs):
        return sorted(r["id"] for r in self.get("/api/customers", hdrs).get_json())

    # -------------------------------------------------------------------- tests
    def test_staff_can_be_linked_to_several_organizations(self):
        """The core feature: one engineer, two of the tenant's organizations."""
        r = self.add_staff(self.t1, "e1@t.test", customer_ids=[self.org_a, self.org_b])
        self.assertEqual(r.status_code, 201, r.get_json())
        d = r.get_json()
        self.assertIsNone(d["customer_id"], "a multi-organization account has no single organization")
        self.assertEqual(sorted(d["customer_ids"]), sorted([self.org_a, self.org_b]))
        self.assertEqual(d["responsible_admin_id"], self.ta1["id"])
        eh = self.auth("e1@t.test")
        self.assertEqual(self.visible_orgs(eh), sorted([self.org_a, self.org_b]))
        self.assertEqual(self.visible_customers(eh), sorted([self.org_a, self.org_b]))
        # the organization the tenant admin cares for but did not tick is out of reach
        self.assertNotIn(self.org_c, self.visible_orgs(eh))

    def test_nothing_selected_means_the_whole_care_list(self):
        """Unchanged default: no selection = every organization under the tenant
        admin's care — both when the key is absent and when it is an empty list."""
        for email, body in (("e1@t.test", {}), ("e2@t.test", {"customer_ids": []})):
            r = self.add_staff(self.t1, email, **body)
            self.assertEqual(r.status_code, 201, r.get_json())
            self.assertEqual(r.get_json()["customer_ids"], [])
            self.assertEqual(self.visible_orgs(self.auth(email)),
                             sorted([self.org_a, self.org_b, self.org_c]))

    def test_application_role_works_the_same_way(self):
        r = self.add_staff(self.t1, "app@t.test", role="application", customer_ids=[self.org_c])
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(self.visible_orgs(self.auth("app@t.test")), [self.org_c])

    def test_ids_are_coerced_like_every_other_picker(self):
        """Checkbox values arrive as strings; duplicates and blanks are dropped."""
        r = self.add_staff(self.t1, "e1@t.test",
                           customer_ids=[str(self.org_a), str(self.org_a), "", None, self.org_c])
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(sorted(r.get_json()["customer_ids"]), sorted([self.org_a, self.org_c]))
        self.assertEqual(self.visible_orgs(self.auth("e1@t.test")), sorted([self.org_a, self.org_c]))

    def test_tenant_admin_cannot_link_outside_their_care_list(self):
        """The boundary: TA1 may not hand an account reach into TA2's tenant."""
        r = self.add_staff(self.t1, "e1@t.test", customer_ids=[self.org_a, self.org_d])
        self.assertEqual(r.status_code, 403)
        self.assertIn("organization you care for", r.get_json()["error"])
        # nothing was created
        self.assertEqual(self.client.post("/api/login", json={"email": "e1@t.test", "password": PW}).status_code, 401)

    def test_links_must_be_under_the_linked_tenant_admins_care(self):
        """The master may name any organization, but not one the linked tenant
        admin does not care for — the links are a selection of that care list."""
        r = self.make_user("Eng X", "e1@t.test", "engineer",
                           responsible_admin_id=self.ta1["id"], customer_ids=[self.org_a, self.org_d])
        self.assertEqual(r.status_code, 400, r.get_json())
        self.assertIn("linked tenant admin's care", r.get_json()["error"])
        r = self.make_user("Eng X", "e1@t.test", "engineer",
                           responsible_admin_id=self.ta1["id"], customer_ids=[self.org_a, self.org_b])
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(self.visible_orgs(self.auth("e1@t.test")), sorted([self.org_a, self.org_b]))

    def test_unknown_organization_is_rejected(self):
        r = self.make_user("Eng X", "e1@t.test", "engineer", customer_ids=[999999])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "Linked organization not found")

    def test_master_may_restrict_a_labcare_wide_engineer(self):
        """No tenant admin at all: the master's engineer reaches exactly the
        ticked organizations — across tenants if the master says so."""
        r = self.make_user("Eng Wide", "w1@t.test", "engineer", customer_ids=[self.org_a, self.org_d])
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(self.visible_orgs(self.auth("w1@t.test")), sorted([self.org_a, self.org_d]))

    def test_masters_unrestricted_staff_are_unchanged(self):
        """No regression: the master's unbound engineers still see everything."""
        r = self.make_user("Eng Wide", "w1@t.test", "engineer")
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(self.visible_orgs(self.auth("w1@t.test")),
                         sorted([self.org_a, self.org_b, self.org_c, self.org_d]))

    def test_legacy_single_organization_binding_still_wins(self):
        """An account bound the old way (users.customer_id) keeps that one
        organization; `customer_ids` is the multi-organization alternative."""
        r = self.add_staff(self.t1, "e1@t.test", customer_id=self.org_b)
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(r.get_json()["customer_id"], self.org_b)
        self.assertEqual(self.visible_orgs(self.auth("e1@t.test")), [self.org_b])
        # switching it to a selection clears the single binding
        uid = r.get_json()["id"]
        r = self.patch("/api/users/%d" % uid, self.t1, {"customer_ids": [self.org_a, self.org_c]})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIsNone(r.get_json()["customer_id"])
        self.assertEqual(sorted(r.get_json()["customer_ids"]), sorted([self.org_a, self.org_c]))
        self.assertEqual(sorted(r.get_json()["customer_names"]), ["Org A", "Org C"])
        self.assertEqual(self.visible_orgs(self.auth("e1@t.test")), sorted([self.org_a, self.org_c]))

    def test_editing_the_selection(self):
        uid = self.add_staff(self.t1, "e1@t.test", customer_ids=[self.org_a]).get_json()["id"]
        eh = self.auth("e1@t.test")
        self.assertEqual(self.visible_orgs(eh), [self.org_a])
        # add one
        r = self.patch("/api/users/%d" % uid, self.t1, {"customer_ids": [self.org_a, self.org_b]})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.visible_orgs(eh), sorted([self.org_a, self.org_b]))
        # outside the care list -> refused, selection untouched
        r = self.patch("/api/users/%d" % uid, self.t1, {"customer_ids": [self.org_d]})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.visible_orgs(eh), sorted([self.org_a, self.org_b]))
        # clear -> back to the whole care list
        r = self.patch("/api/users/%d" % uid, self.t1, {"customer_ids": []})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["customer_ids"], [])
        self.assertEqual(self.visible_orgs(eh), sorted([self.org_a, self.org_b, self.org_c]))
        # a malformed value is refused outright
        r = self.patch("/api/users/%d" % uid, self.t1, {"customer_ids": "nope"})
        self.assertEqual(r.status_code, 400)

    def test_an_unrelated_tenant_admin_cannot_touch_the_account(self):
        uid = self.add_staff(self.t1, "e1@t.test", customer_ids=[self.org_a]).get_json()["id"]
        r = self.patch("/api/users/%d" % uid, self.t2, {"customer_ids": [self.org_d]})
        self.assertEqual(r.status_code, 403)
        r = self.client.delete("/api/users/%d" % uid, headers=self.t2)
        self.assertEqual(r.status_code, 403)
        # deleting accounts is the master's alone — its own tenant admin may
        # edit or disable it, but not remove it
        r = self.client.delete("/api/users/%d" % uid, headers=self.t1)
        self.assertEqual(r.status_code, 403)
        r = self.patch("/api/users/%d" % uid, self.t1, {"active": 0})
        self.assertEqual(r.status_code, 200, r.get_json())
        # ...and when the master removes it the links go with it
        r = self.client.delete("/api/users/%d" % uid, headers=self.mh)
        self.assertEqual(r.status_code, 200, r.get_json())
        c = conn()
        n = c.execute("SELECT COUNT(*) n FROM staff_customer_links WHERE user_id=?", (uid,)).fetchone()["n"]
        c.close()
        self.assertEqual(n, 0)

    def test_reach_never_exceeds_the_tenant_admins_current_care_list(self):
        """If the tenant admin later drops an organization, their staff lose it
        too — and losing every linked organization means nothing, not everything."""
        self.add_staff(self.t1, "e1@t.test", customer_ids=[self.org_b, self.org_c])
        eh = self.auth("e1@t.test")
        self.assertEqual(self.visible_orgs(eh), sorted([self.org_b, self.org_c]))
        self.client.delete("/api/my-customers/%d" % self.org_c, headers=self.t1)
        self.assertEqual(self.visible_orgs(eh), [self.org_b])
        self.client.delete("/api/my-customers/%d" % self.org_b, headers=self.t1)
        self.assertEqual(self.visible_orgs(eh), [])
        self.assertEqual(self.visible_customers(eh), [])
        self.assertEqual(self.get("/api/pms", eh).get_json(), [])
        self.assertEqual(self.get("/api/portal-links", eh).get_json(), [])
        dash = self.get("/api/dashboard", eh).get_json()
        self.assertEqual(dash["counts"]["open_complaints"], 0)
        self.assertEqual(dash["counts"]["total_customers"], 0)
        self.assertEqual(dash["recent_complaints"], [])
        csv = self.get("/api/export.csv?type=complaints", eh)
        self.assertEqual(csv.status_code, 200)
        self.assertEqual(len([l for l in csv.get_data(as_text=True).splitlines() if l.strip()]), 1,
                         "CSV export leaked rows outside the (empty) reach")
        pdf = self.get("/api/reports/trend.pdf", eh)
        self.assertEqual(pdf.status_code, 200)
        self.assertNotIn(b"ticket in", pdf.get_data())

    def test_linked_staff_may_only_write_within_their_reach(self):
        """Writes guarded by tenant_guard (equipment, locations, PM, portals)
        honour the selection too — a linked engineer is not LabSynch-wide."""
        self.add_staff(self.t1, "e1@t.test", customer_ids=[self.org_a])
        eh = self.auth("e1@t.test")
        ok = self.post("/api/equipment", eh, {"name": "Centrifuge", "customer_id": self.org_a})
        self.assertEqual(ok.status_code, 201, ok.get_json())
        for org in (self.org_b, self.org_d):
            r = self.post("/api/equipment", eh, {"name": "Centrifuge", "customer_id": org})
            self.assertEqual(r.status_code, 403, "wrote into organization %d" % org)
            r = self.post("/api/locations", eh, {"name": "Lab", "customer_id": org})
            self.assertEqual(r.status_code, 403)
            r = self.post("/api/complaints", eh, {"customer_id": org, "subject": "x", "description": "d"})
            self.assertEqual(r.status_code, 403)

    def test_team_lists_follow_the_links(self):
        """A linked engineer shows up as team for the tenants they reach — not
        for another tenant — while LabSynch-wide engineers still show for all."""
        e_id = self.add_staff(self.t1, "e1@t.test", customer_ids=[self.org_a]).get_json()["id"]
        w_id = self.make_user("Eng Wide", "w1@t.test", "engineer").get_json()["id"]
        # TA1's team & users list carries the linked organization names
        team1 = self.get("/api/users", self.t1).get_json()
        mine = next(x for x in team1 if x["id"] == e_id)
        self.assertEqual(mine["customer_ids"], [self.org_a])
        self.assertEqual(mine["customer_names"], ["Org A"])
        self.assertIn(w_id, [x["id"] for x in team1])
        # TA2 sees the provider's engineer but not TA1's linked engineer
        ids2 = [x["id"] for x in self.get("/api/users", self.t2).get_json()]
        self.assertIn(w_id, ids2)
        self.assertNotIn(e_id, ids2)
        eng2 = [x["id"] for x in self.get("/api/engineers", self.t2).get_json()]
        self.assertIn(w_id, eng2)
        self.assertNotIn(e_id, eng2)
        eng1 = [x["id"] for x in self.get("/api/engineers", self.t1).get_json()]
        self.assertIn(e_id, eng1)
        # ...and cannot assign work to them
        r = self.post("/api/complaints", self.t2, {
            "customer_id": self.org_d, "subject": "x", "description": "d", "assigned_to": e_id})
        self.assertEqual(r.status_code, 403)
        r = self.post("/api/complaints", self.t1, {
            "customer_id": self.org_a, "subject": "x", "description": "d", "assigned_to": e_id})
        self.assertEqual(r.status_code, 201, r.get_json())

    def test_team_notifications_follow_the_links(self):
        """A new ticket rings the linked engineer only for their organizations."""
        e_id = self.add_staff(self.t1, "e1@t.test", customer_ids=[self.org_a]).get_json()["id"]
        eh = self.auth("e1@t.test")
        before = len(self.get("/api/notifications", eh).get_json())
        self.post("/api/complaints", self.t1, {"customer_id": self.org_b, "subject": "in B", "description": "d"})
        self.assertEqual(len(self.get("/api/notifications", eh).get_json()), before,
                         "heard about an organization outside the selection")
        self.post("/api/complaints", self.t1, {"customer_id": self.org_a, "subject": "in A", "description": "d"})
        self.assertGreater(len(self.get("/api/notifications", eh).get_json()), before)

    def test_tenant_admins_endpoint_exposes_care_lists_for_the_form(self):
        """The form filters the checklist by the chosen linked tenant admin."""
        rows = self.get("/api/tenant-admins", self.t1).get_json()
        me = next(x for x in rows if x["id"] == self.ta1["id"])
        self.assertEqual(sorted(me["customer_ids"]), sorted([self.org_a, self.org_b, self.org_c]))
        rows = self.get("/api/tenant-admins", self.mh).get_json()
        two = next(x for x in rows if x["id"] == self.ta2["id"])
        self.assertEqual(two["customer_ids"], [self.org_d])

    def test_me_and_login_report_the_linked_organizations(self):
        self.add_staff(self.t1, "e1@t.test", customer_ids=[self.org_a, self.org_b])
        r = self.client.post("/api/login", json={"email": "e1@t.test", "password": PW}).get_json()
        self.assertEqual(sorted(x["name"] for x in r["user"]["care_customers"]), ["Org A", "Org B"])
        me = self.me({"Authorization": "Bearer " + r["token"]})
        self.assertEqual(sorted(me["customer_ids"]), sorted([self.org_a, self.org_b]))

    def test_customer_role_ignores_the_selection(self):
        """Customer accounts belong to exactly one organization."""
        r = self.post("/api/users", self.t1, {
            "name": "Cust", "email": "c1@t.test", "password": PW, "role": "customer",
            "customer_id": self.org_a, "customer_ids": [self.org_a, self.org_b]})
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(r.get_json()["customer_id"], self.org_a)
        c = conn()
        n = c.execute("SELECT COUNT(*) n FROM staff_customer_links").fetchone()["n"]
        c.close()
        self.assertEqual(n, 0)

    def test_deleting_an_organization_drops_its_links(self):
        org_e = self.make_customer("Org E")
        self.post("/api/my-customers/%d" % org_e, self.t1)
        uid = self.add_staff(self.t1, "e1@t.test", customer_ids=[self.org_a, org_e]).get_json()["id"]
        r = self.client.delete("/api/customers/%d" % org_e, headers=self.mh)
        self.assertEqual(r.status_code, 200, r.get_json())
        row = next(x for x in self.get("/api/users", self.mh).get_json() if x["id"] == uid)
        self.assertEqual(row["customer_ids"], [self.org_a])


if __name__ == "__main__":
    unittest.main(verbosity=2)
