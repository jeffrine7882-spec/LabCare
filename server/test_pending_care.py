"""Tests for the join-request care decision.

A signup that creates a brand-new organization leaves it with no tenant admin,
so nobody can see or manage it. Every tenant admin is therefore asked whether it
is under their care, and the master may assign it instead.

The properties that matter:

* phone number is required at signup, for every role;
* only a NEWLY created organization opens a care decision — signing up against
  an organization that already exists must not re-offer it to everyone;
* the claim is exclusive: the first tenant admin to take it wins and the others
  get a clear 409 rather than silently sharing it;
* a decline is per admin, so saying "not mine" cannot make the request vanish
  for everybody (which would strand the organization with no owner);
* a rejected join request withdraws its organization from the pending list, so
  tenant admins are not left deciding about something that will never go live;
* claiming a pending organization does NOT change the ordinary shared-care
  behaviour — afterwards another admin may still add it to their own list.

Run with:  python3 -m unittest test_pending_care
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
PHONE = "+60 12-345 6789"


class PendingCareTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        c = conn()
        c.execute("PRAGMA foreign_keys = OFF")
        for t in ["pm_logs", "pm_schedules", "portal_links", "push_subscriptions", "app_devices",
                  "audit_logs", "comments", "notifications", "notification_pings",
                  "breakdowns", "complaints", "equipment", "departments", "locations",
                  "pending_care_declines", "admin_customer_links", "onboarding_apps",
                  "sessions", "users", "customers"]:
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
        self.t1 = self.auth("ta1@t.test")
        self.t2 = self.auth("ta2@t.test")
        self.ta1_id = self.me(self.t1)["id"]
        self.ta2_id = self.me(self.t2)["id"]

    # ------------------------------------------------------------------ helpers
    def auth(self, email):
        r = self.client.post("/api/login", json={"email": email, "password": PW})
        return {"Authorization": "Bearer " + r.get_json()["token"]}

    def me(self, hdrs):
        return self.client.get("/api/me", headers=hdrs).get_json()

    def post(self, url, hdrs, body=None):
        return self.client.post(url, headers=hdrs, json=body or {})

    def make_customer(self, name):
        return self.post("/api/customers", self.mh, {"name": name}).get_json()["id"]

    def make_user(self, name, email, role, **kw):
        body = {"name": name, "email": email, "password": PW, "role": role, "phone": PHONE}
        body.update(kw)
        return self.post("/api/users", self.mh, body)

    def signup(self, **over):
        # No default organization fields: new_customer_name takes precedence over
        # customer_id server-side, so a leaked default would silently create an
        # organization in tests that meant to reuse an existing one.
        body = {"name": "Dr New", "email": "new@joiner.test", "password": PW,
                "phone": PHONE, "role": "customer"}
        body.update(over)
        return self.client.post("/api/signup", json=body)

    def pending_ids(self, hdrs):
        return [c["id"] for c in self.client.get(
            "/api/customers/pending-care", headers=hdrs).get_json()["customers"]]

    def pending_flag(self, cid):
        c = conn()
        row = c.execute("SELECT pending_care FROM customers WHERE id=?", (cid,)).fetchone()
        c.close()
        return row["pending_care"]

    def org_id_by_name(self, name):
        c = conn()
        row = c.execute("SELECT id FROM customers WHERE name=?", (name,)).fetchone()
        c.close()
        return row["id"] if row else None

    def care_links(self, cid):
        c = conn()
        ids = sorted(r["admin_id"] for r in c.execute(
            "SELECT admin_id FROM admin_customer_links WHERE customer_id=?", (cid,)).fetchall())
        c.close()
        return ids

    def care_notifications(self, hdrs):
        return [n for n in self.client.get("/api/notifications", headers=hdrs).get_json()
                if n.get("entity_type") == "pending_care"]

    def new_org_signup(self, name="Nova Biotech", email="new@joiner.test"):
        """Sign up against a brand-new organization and return its id."""
        r = self.signup(new_customer_name=name, email=email)
        self.assertEqual(r.status_code, 201, r.get_json())
        cid = self.org_id_by_name(name)
        self.assertIsNotNone(cid, "organization row was not created")
        return cid

    # -------------------------------------------------------------------- tests
    def test_phone_number_is_required(self):
        """Full name, email and phone number are all required — for every role."""
        for role, extra in (("customer", {"customer_id": self.org_a}),
                            ("engineer", {}), ("application", {})):
            r = self.signup(role=role, phone="", email="nophone.%s@joiner.test" % role, **extra)
            self.assertEqual(r.status_code, 400, "role %s accepted a missing phone" % role)
            self.assertIn("phone", r.get_json()["error"].lower())

        # ...and a supplied phone is stored on the request.
        self.assertEqual(self.signup(role="engineer", email="hasphone@joiner.test").status_code, 201)
        c = conn()
        row = c.execute("SELECT phone FROM onboarding_apps WHERE email='hasphone@joiner.test'").fetchone()
        c.close()
        self.assertEqual(row["phone"], PHONE)

    def test_new_organization_asks_every_tenant_admin(self):
        """A newly created organization is flagged and offered to all of them."""
        cid = self.new_org_signup()
        self.assertEqual(self.pending_flag(cid), 1)
        self.assertEqual(self.pending_ids(self.t1), [cid])
        self.assertEqual(self.pending_ids(self.t2), [cid])
        self.assertEqual(self.pending_ids(self.mh), [cid])

        for hdrs, who in ((self.t1, "TA One"), (self.t2, "TA Two")):
            notes = self.care_notifications(hdrs)
            self.assertTrue(notes, "%s got no care notification" % who)
            self.assertIn("under your care", notes[0]["text"])
            self.assertTrue(notes[0]["care_pending"])

        # The master is told too, but in their own terms: they assign.
        master_notes = self.care_notifications(self.mh)
        self.assertTrue(any("assign" in n["text"].lower() for n in master_notes))

    def test_master_gets_the_tenant_admins_to_assign_to(self):
        cid = self.new_org_signup()
        d = self.client.get("/api/customers/pending-care", headers=self.mh).get_json()
        self.assertEqual([c["id"] for c in d["customers"]], [cid])
        self.assertEqual(sorted(a["id"] for a in d["tenant_admins"]),
                         sorted([self.ta1_id, self.ta2_id]))
        # A tenant admin does not need (and is not given) that picker list.
        self.assertNotIn("tenant_admins",
                         self.client.get("/api/customers/pending-care", headers=self.t1).get_json())

    def test_join_request_details_come_with_the_organization(self):
        cid = self.new_org_signup(name="Solis Bio", email="solis@joiner.test")
        row = [c for c in self.client.get(
            "/api/customers/pending-care", headers=self.t1).get_json()["customers"]
            if c["id"] == cid][0]
        self.assertEqual(row["requested_by"], "Dr New")
        self.assertEqual(row["requested_by_email"], "solis@joiner.test")
        self.assertEqual(row["phone"], PHONE)

    def test_decline_hides_it_from_that_admin_only(self):
        """Saying "not mine" must not strand the organization for everybody."""
        cid = self.new_org_signup()
        self.assertEqual(self.post("/api/customers/%d/decline-care" % cid, self.t1).status_code, 200)

        self.assertNotIn(cid, self.pending_ids(self.t1), "still listed for the admin who declined")
        self.assertEqual(self.pending_ids(self.t2), [cid], "vanished for another tenant admin")
        self.assertEqual(self.pending_ids(self.mh), [cid], "vanished for the master")
        self.assertEqual(self.pending_flag(cid), 1, "a decline must not settle the decision")
        self.assertEqual(self.care_links(cid), [], "a decline must not create a care link")

        # The master hears about it, since assigning is now the likely next step.
        self.assertTrue(any("not under their care" in n["text"]
                            for n in self.care_notifications(self.mh)))
        # ...and the remaining admins can see that somebody already declined.
        row = [c for c in self.client.get(
            "/api/customers/pending-care", headers=self.t2).get_json()["customers"]
            if c["id"] == cid][0]
        self.assertEqual(row["declines"], 1)

    def test_first_tenant_admin_to_claim_wins(self):
        cid = self.new_org_signup()
        self.assertEqual(self.post("/api/customers/%d/take-care" % cid, self.t1).status_code, 200)

        self.assertEqual(self.pending_flag(cid), 0)
        self.assertEqual(self.care_links(cid), [self.ta1_id])
        self.assertEqual(self.pending_ids(self.t1), [])
        self.assertEqual(self.pending_ids(self.t2), [])
        self.assertEqual(self.pending_ids(self.mh), [])

        # The loser gets a clear conflict, not a silent second claim.
        r = self.post("/api/customers/%d/take-care" % cid, self.t2)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.care_links(cid), [self.ta1_id], "a second admin was linked")

        # The others are told who took it, and their notification stops offering
        # buttons for a decision that is already settled.
        notes = self.care_notifications(self.t2)
        self.assertTrue(any("TA One" in n["text"] and "took" in n["text"] for n in notes))
        settled = [n for n in notes if n["entity_id"] == cid and "asked to join" in n["text"]]
        self.assertTrue(settled)
        self.assertFalse(settled[0]["care_pending"])
        self.assertEqual(settled[0]["care_claimed_by"], "TA One")

    def test_claiming_clears_earlier_declines(self):
        cid = self.new_org_signup()
        self.post("/api/customers/%d/decline-care" % cid, self.t1)
        self.assertEqual(self.post("/api/customers/%d/take-care" % cid, self.t2).status_code, 200)
        c = conn()
        n = c.execute("SELECT COUNT(*) AS n FROM pending_care_declines WHERE customer_id=?",
                      (cid,)).fetchone()["n"]
        c.close()
        self.assertEqual(n, 0, "stale declines left behind after the claim")

    def test_master_assigns_to_a_chosen_tenant_admin(self):
        cid = self.new_org_signup()
        r = self.post("/api/customers/%d/assign-care" % cid, self.mh, {"admin_id": self.ta2_id})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.pending_flag(cid), 0)
        self.assertEqual(self.care_links(cid), [self.ta2_id])
        self.assertEqual(self.pending_ids(self.t1), [])

        assigned = self.care_notifications(self.t2)
        self.assertTrue(any("master assigned" in n["text"].lower() for n in assigned))
        # The other tenant admin is told it was settled, so they stop seeing it.
        self.assertTrue(any("assigned to TA Two" in n["text"] for n in self.care_notifications(self.t1)))

    def test_assign_needs_a_tenant_admin(self):
        cid = self.new_org_signup()
        self.assertEqual(self.post("/api/customers/%d/assign-care" % cid, self.mh, {}).status_code, 400)
        # The master cannot assign it to themselves — they already manage all.
        master_id = self.me(self.mh)["id"]
        self.assertEqual(self.post("/api/customers/%d/assign-care" % cid, self.mh,
                                  {"admin_id": master_id}).status_code, 400)
        self.assertEqual(self.pending_flag(cid), 1, "a failed assignment settled the decision")

    def test_only_the_master_may_assign(self):
        cid = self.new_org_signup()
        self.assertEqual(self.post("/api/customers/%d/assign-care" % cid, self.t1,
                                  {"admin_id": self.ta2_id}).status_code, 403)
        self.assertEqual(self.pending_flag(cid), 1)

    def test_the_master_does_not_take_care_themselves(self):
        cid = self.new_org_signup()
        self.assertEqual(self.post("/api/customers/%d/take-care" % cid, self.mh).status_code, 403)
        self.assertEqual(self.pending_flag(cid), 1, "the master claimed it anyway")

    def test_claiming_resumes_normal_shared_care(self):
        """Exclusivity applies to the decision only, not to care in general."""
        cid = self.new_org_signup()
        self.post("/api/customers/%d/take-care" % cid, self.t1)
        # Afterwards TA2 may still add it to their own list, as with any org.
        self.assertEqual(self.post("/api/my-customers/%d" % cid, self.t2).status_code, 200)
        self.assertEqual(self.care_links(cid), sorted([self.ta1_id, self.ta2_id]))

    def test_rejected_request_withdraws_the_organization(self):
        cid = self.new_org_signup()
        c = conn()
        app_id = c.execute("SELECT id FROM onboarding_apps WHERE customer_id=?", (cid,)).fetchone()["id"]
        c.close()

        r = self.post("/api/onboarding/%d/review" % app_id, self.mh, {"decision": "reject"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.pending_flag(cid), 0, "a rejected org is still awaiting a decision")
        self.assertEqual(self.pending_ids(self.t1), [])
        self.assertEqual(self.pending_ids(self.mh), [])
        # The organization row is kept: it already has a location and department.
        self.assertIsNotNone(self.org_id_by_name("Nova Biotech"))
        self.assertTrue(any("rejected" in n["text"] for n in self.care_notifications(self.t1)))

        # Acting on it afterwards is refused rather than silently succeeding.
        self.assertEqual(self.post("/api/customers/%d/take-care" % cid, self.t1).status_code, 409)

    def test_approving_leaves_the_claim_open(self):
        """Approval is about the person; the care decision is still theirs to make."""
        cid = self.new_org_signup()
        c = conn()
        app_id = c.execute("SELECT id FROM onboarding_apps WHERE customer_id=?", (cid,)).fetchone()["id"]
        c.close()
        self.assertEqual(self.post("/api/onboarding/%d/review" % app_id, self.mh,
                                  {"decision": "approve"}).status_code, 200)
        self.assertEqual(self.pending_flag(cid), 1)
        self.assertEqual(self.pending_ids(self.t1), [cid])

    def test_existing_organization_does_not_reopen_a_decision(self):
        """Only a NEWLY created organization is offered — not one that exists."""
        # An existing organization still needs a location/department chosen.
        r = self.signup(email="existing@joiner.test", customer_id=self.org_a,
                        new_location_name="Main Lab")
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(self.pending_flag(self.org_a), 0)
        self.assertEqual(self.pending_ids(self.t1), [])
        self.assertEqual(self.care_notifications(self.t2), [])

        # Same by name: an existing organization is reused, not re-offered.
        r2 = self.signup(email="byname@joiner.test", new_customer_name="Org A",
                         new_location_name="Annexe Lab")
        self.assertEqual(r2.status_code, 201, r2.get_json())
        self.assertEqual(self.pending_flag(self.org_a), 0)
        self.assertEqual(self.pending_ids(self.t1), [])

    def test_non_admin_roles_cannot_see_or_settle_it(self):
        cid = self.new_org_signup()
        self.make_user("Eng One", "eng1@t.test", "engineer", customer_id=self.org_a)
        eh = self.auth("eng1@t.test")
        self.assertEqual(self.client.get("/api/customers/pending-care", headers=eh).status_code, 403)
        self.assertEqual(self.post("/api/customers/%d/take-care" % cid, eh).status_code, 403)
        self.assertEqual(self.post("/api/customers/%d/decline-care" % cid, eh).status_code, 403)
        self.assertEqual(self.pending_flag(cid), 1)

    def test_unknown_organization_is_a_404(self):
        for path in ("take-care", "decline-care"):
            self.assertEqual(self.post("/api/customers/999999/%s" % path, self.t1).status_code, 404)
        self.assertEqual(self.post("/api/customers/999999/assign-care", self.mh,
                                  {"admin_id": self.ta1_id}).status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
