"""Tests for GET /api/audit — the ticket history timeline.

The History card on both ticket detail views has always called this endpoint,
but the route did not exist, so every ticket rendered "History unavailable."
while audit rows accumulated unread. These tests pin down the route that now
serves it:

* it answers with the rows the card expects (action, user_name, detail,
  created_at), newest first;
* it is scoped exactly like the ticket itself — a customer sees only their own
  organization's history, a tenant admin only their care list, the master
  everything, and nobody else;
* a ticket kind never leaks into the other's history at the same id;
* the actions real operations write (create, comment, status, rating,
  feedback) actually turn up, including ones written anonymously through the
  public portal;
* a deleted user's rows survive, because the name is denormalized onto the row.

Run with:  python3 -m unittest test_audit_history
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
PHONE = "+60 12-000 0000"


class AuditHistoryTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        c = conn()
        c.execute("PRAGMA foreign_keys = OFF")
        for t in ["pm_logs", "pm_schedules", "portal_links", "push_subscriptions", "app_devices",
                  "audit_logs", "comments", "ticket_feedback", "ticket_ratings",
                  "notifications", "notification_pings", "breakdowns", "complaints", "equipment",
                  "departments", "locations", "pending_care_declines", "admin_customer_links",
                  "onboarding_apps", "sessions", "users", "customers", "categories"]:
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
        self.org_b = self.make_customer("Org B")
        self.make_user("TA A", "ta.a@t.test", "admin", customer_id=self.org_a)
        self.make_user("TA B", "ta.b@t.test", "admin", customer_id=self.org_b)
        self.make_user("Eng A", "eng.a@t.test", "engineer", customer_id=self.org_a)
        self.make_user("Cust A", "cust.a@t.test", "customer", customer_id=self.org_a)
        self.make_user("Cust B", "cust.b@t.test", "customer", customer_id=self.org_b)
        self.ta = self.auth("ta.a@t.test")
        self.ta_b = self.auth("ta.b@t.test")
        self.eng = self.auth("eng.a@t.test")
        self.cust = self.auth("cust.a@t.test")
        self.cust_b = self.auth("cust.b@t.test")

        self.cmp = self.make_complaint(self.org_a)
        self.brk = self.make_breakdown(self.org_a)
        self.cmp_b = self.make_complaint(self.org_b)
        self.token = self.post("/api/portal-links", self.ta,
                               {"customer_id": self.org_a}).get_json()["token"]

    # ------------------------------------------------------------------ helpers
    def auth(self, email):
        r = self.client.post("/api/login", json={"email": email, "password": PW})
        return {"Authorization": "Bearer " + r.get_json()["token"]}

    def post(self, url, hdrs, body=None):
        return self.client.post(url, headers=hdrs, json=body or {})

    def make_customer(self, name):
        return self.post("/api/customers", self.mh, {"name": name}).get_json()["id"]

    def make_user(self, name, email, role, **kw):
        body = {"name": name, "email": email, "password": PW, "role": role, "phone": PHONE}
        body.update(kw)
        return self.post("/api/users", self.mh, body)

    def make_complaint(self, org):
        r = self.post("/api/complaints", self.mh, {
            "customer_id": org, "subject": "Noisy centrifuge", "description": "d",
            "priority": "medium"})
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()["id"]

    def make_breakdown(self, org):
        r = self.post("/api/breakdowns", self.mh, {
            "customer_id": org, "fault_description": "Will not spin", "priority": "medium"})
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()["id"]

    def history(self, kind, tid, hdrs=None):
        return self.client.get("/api/audit?entity_type=%s&entity_id=%d" % (kind, tid),
                               headers=hdrs or {})

    def rows(self, kind, tid, hdrs=None):
        return self.history(kind, tid, hdrs).get_json()

    def actions(self, kind, tid, hdrs=None):
        return [r["action"] for r in self.rows(kind, tid, hdrs)]

    def code_of(self, kind, tid):
        c = conn()
        row = c.execute("SELECT code FROM %ss WHERE id=?" % kind, (tid,)).fetchone()
        c.close()
        return row["code"]

    def seed_audit(self, kind, tid, entries):
        """Write history rows straight to the table with explicit timestamps,
        so ordering can be tested without depending on the clock."""
        c = conn()
        for i, (action, who, detail, at) in enumerate(entries):
            c.execute(
                "INSERT INTO audit_logs (entity_type, entity_id, user_id, user_name, action, "
                "detail, created_at) VALUES (?,?,?,?,?,?,?)",
                (kind, tid, None, who, action, detail, at))
        c.commit()
        c.close()

    # ---------------------------------------------------------------- auth & args
    def test_anonymous_is_rejected(self):
        # setUp logged six users in through self.client, so its cookie jar would
        # authenticate this request; a fresh client is the honest anonymous one.
        fresh = app.test_client()
        r = fresh.get("/api/audit?entity_type=complaint&entity_id=%d" % self.cmp)
        self.assertEqual(r.status_code, 401)

    def test_unknown_entity_type_is_rejected(self):
        for bad in ("nonsense", "complaints", ""):
            r = self.client.get("/api/audit?entity_type=%s&entity_id=%d" % (bad, self.cmp),
                                headers=self.mh)
            self.assertEqual(r.status_code, 400, bad)

    def test_missing_entity_type_is_rejected(self):
        r = self.client.get("/api/audit?entity_id=%d" % self.cmp, headers=self.mh)
        self.assertEqual(r.status_code, 400)

    def test_missing_or_invalid_entity_id_is_rejected(self):
        for q in ("/api/audit?entity_type=complaint",
                  "/api/audit?entity_type=complaint&entity_id=",
                  "/api/audit?entity_type=complaint&entity_id=abc"):
            r = self.client.get(q, headers=self.mh)
            self.assertEqual(r.status_code, 400, q)

    def test_unknown_ticket_is_404(self):
        self.assertEqual(self.history("complaint", 999999, self.mh).status_code, 404)
        self.assertEqual(self.history("breakdown", 999999, self.mh).status_code, 404)

    # ------------------------------------------------------------------- scoping
    def test_master_sees_any_history(self):
        self.assertEqual(self.history("complaint", self.cmp, self.mh).status_code, 200)
        self.assertEqual(self.history("complaint", self.cmp_b, self.mh).status_code, 200)

    def test_tenant_admin_sees_own_care_list_only(self):
        self.assertEqual(self.history("complaint", self.cmp, self.ta).status_code, 200)
        self.assertEqual(self.history("complaint", self.cmp_b, self.ta).status_code, 403)

    def test_other_tenant_admin_is_refused(self):
        self.assertEqual(self.history("complaint", self.cmp, self.ta_b).status_code, 403)

    def test_engineer_sees_their_own_organization(self):
        self.assertEqual(self.history("complaint", self.cmp, self.eng).status_code, 200)
        self.assertEqual(self.history("breakdown", self.brk, self.eng).status_code, 200)
        self.assertEqual(self.history("complaint", self.cmp_b, self.eng).status_code, 403)

    def test_customer_sees_their_own_ticket_only(self):
        self.assertEqual(self.history("complaint", self.cmp, self.cust).status_code, 200)
        self.assertEqual(self.history("complaint", self.cmp_b, self.cust).status_code, 403)

    def test_cross_organization_read_leaks_nothing(self):
        self.seed_audit("complaint", self.cmp_b, [("secret", "Org B Staff", "confidential", "2026-09-01 09:00:00")])
        r = self.history("complaint", self.cmp_b, self.cust)
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("confidential", r.get_data(as_text=True))

    def test_history_verdict_matches_the_ticket_verdict_for_every_role(self):
        """The History card is readable exactly when the ticket itself is.

        The two endpoints must never disagree: a card that loads for a ticket
        the user may not open would leak activity, and one that 403s on a ticket
        they may open would leave a permanent "History unavailable." hole."""
        tickets = [("complaint", self.cmp), ("breakdown", self.brk),
                   ("complaint", self.cmp_b)]
        people = [("master", self.mh), ("tenant admin A", self.ta),
                  ("tenant admin B", self.ta_b), ("engineer A", self.eng),
                  ("customer A", self.cust), ("customer B", self.cust_b)]
        seen = {"allowed": 0, "refused": 0}
        for who, hdrs in people:
            for kind, tid in tickets:
                audit_code = self.history(kind, tid, hdrs).status_code
                ticket_code = self.client.get("/api/%ss/%d" % (kind, tid),
                                              headers=hdrs).status_code
                self.assertEqual(
                    audit_code, ticket_code,
                    "%s on %s %d: history says %d, ticket says %d"
                    % (who, kind, tid, audit_code, ticket_code))
                seen["allowed" if audit_code == 200 else "refused"] += 1
        self.assertTrue(seen["allowed"], "the sweep granted nobody — it proves nothing")
        self.assertTrue(seen["refused"], "the sweep refused nobody — it proves nothing")

    def test_a_customer_bound_to_one_location_is_scoped_like_their_tickets(self):
        """A customer account's scope is organization + location + department,
        so within one organization some tickets are theirs and some are not."""
        loc = self.post("/api/locations", self.mh,
                        {"customer_id": self.org_a, "name": "Annex"}).get_json()["id"]
        self.make_user("Cust Annex", "cust.annex@t.test", "customer",
                       customer_id=self.org_a, location_id=loc)
        annex = self.auth("cust.annex@t.test")
        theirs = self.post("/api/complaints", self.mh, {
            "customer_id": self.org_a, "location_id": loc, "subject": "Annex freezer",
            "description": "d", "priority": "medium"}).get_json()["id"]

        elsewhere = self.post("/api/complaints", self.mh, {
            "customer_id": self.org_a, "subject": "Main Lab autoclave", "description": "d",
            "priority": "medium",
            "location_id": self.post("/api/locations", self.mh,
                                     {"customer_id": self.org_a,
                                      "name": "Main Lab"}).get_json()["id"],
        }).get_json()["id"]

        self.assertEqual(self.history("complaint", theirs, annex).status_code, 200)
        self.assertEqual(self.actions("complaint", theirs, annex), ["created"])
        # another location in the same organization is not theirs
        self.assertEqual(self.history("complaint", elsewhere, annex).status_code, 403)
        # a ticket with no location at all belongs to the whole organization, so
        # it is theirs — and the ticket endpoint agrees on all three
        self.assertEqual(self.history("complaint", self.cmp, annex).status_code, 200)
        for tid in (theirs, elsewhere, self.cmp):
            self.assertEqual(
                self.history("complaint", tid, annex).status_code,
                self.client.get("/api/complaints/%d" % tid, headers=annex).status_code,
                "history and ticket disagree on %d" % tid)

    # --------------------------------------------------------------------- shape
    def test_rows_carry_exactly_the_fields_the_timeline_renders(self):
        body = self.rows("complaint", self.cmp, self.mh)
        self.assertIsInstance(body, list, "the card maps over an array, not an object")
        self.assertTrue(body)
        for row in body:
            self.assertEqual(set(row.keys()), {"action", "user_name", "detail", "created_at"}, row)

    def test_newest_first_by_timestamp_then_id(self):
        tid = self.make_complaint(self.org_a)
        self.seed_audit("complaint", tid, [
            ("created", "First", "oldest", "2026-09-01 09:00:00"),
            ("comment", "Second", "middle", "2026-09-02 09:00:00"),
            ("status", "Third", "newest", "2026-09-03 09:00:00"),
        ])
        body = self.rows("complaint", tid, self.mh)
        # the ticket's own "created" row is newer than all three seeded stamps
        self.assertEqual(body[0]["action"], "created")
        self.assertEqual([r["detail"] for r in body[1:]], ["newest", "middle", "oldest"])

    def test_same_second_rows_fall_back_to_insertion_order(self):
        tid = self.make_complaint(self.org_a)
        at = "2026-09-05 10:00:00"
        self.seed_audit("complaint", tid, [
            ("comment", "A", "first", at),
            ("comment", "B", "second", at),
            ("comment", "C", "third", at),
        ])
        details = [r["detail"] for r in self.rows("complaint", tid, self.mh)
                   if r["action"] == "comment"]
        self.assertEqual(details, ["third", "second", "first"])

    def test_the_ticket_kind_decides_whose_history_comes_back(self):
        # audit_logs is keyed by (entity_type, entity_id) and the two ticket
        # sequences are independent, so one id can exist in both tables. Rows
        # written under the other kind must never come back with them.
        at = "2026-09-01 09:00:00"
        self.seed_audit("breakdown", self.cmp,
                        [("comment", "Brk Only", "breakdown side", at)])
        self.seed_audit("complaint", self.brk,
                        [("comment", "Cmp Only", "complaint side", at)])
        cmp_details = " | ".join(r["detail"] for r in self.rows("complaint", self.cmp, self.mh))
        brk_details = " | ".join(r["detail"] for r in self.rows("breakdown", self.brk, self.mh))
        self.assertNotIn("breakdown side", cmp_details)
        self.assertNotIn("complaint side", brk_details)
        self.assertIn("Opened CMP", cmp_details)
        self.assertIn("Opened BRK", brk_details)

    def test_history_is_per_ticket_not_per_organization(self):
        a = self.actions("complaint", self.cmp, self.mh)
        b = self.actions("complaint", self.cmp_b, self.mh)
        self.assertEqual(a, ["created"])
        self.assertEqual(b, ["created"])
        self.post("/api/comments", self.mh,
                  {"entity_type": "complaint", "entity_id": self.cmp, "text": "only on A"})
        a_after = self.rows("complaint", self.cmp, self.mh)
        b_after = self.rows("complaint", self.cmp_b, self.mh)
        # responding also auto-assigns the responder, so compare on content
        # rather than an exact row count
        self.assertIn("only on A", [r["detail"] for r in a_after])
        self.assertEqual([r["detail"] for r in b_after],
                         ["Opened %s — Noisy centrifuge" % self.code_of("complaint", self.cmp_b)])
        self.assertEqual(len(b_after), 1, "nothing said on A may appear on B")

    # ------------------------------------------------------- real recorded actions
    def test_opening_a_ticket_records_created(self):
        body = self.rows("complaint", self.cmp, self.mh)
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["action"], "created")
        self.assertEqual(body[0]["user_name"], "Master Admin")

    def test_a_comment_turns_up_in_history(self):
        self.post("/api/comments", self.eng,
                  {"entity_type": "complaint", "entity_id": self.cmp, "text": "Looking into it"})
        body = self.rows("complaint", self.cmp, self.mh)
        self.assertEqual(body[0]["action"], "comment")
        self.assertEqual(body[0]["user_name"], "Eng A")
        self.assertIn("Looking into it", body[0]["detail"])

    def test_a_status_change_turns_up_in_history(self):
        r = self.client.patch("/api/complaints/%d" % self.cmp, headers=self.mh,
                              json={"status": "in_progress"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIn("status", self.actions("complaint", self.cmp, self.mh))

    def test_customer_feedback_actions_turn_up_in_history(self):
        r = self.client.patch("/api/complaints/%d" % self.cmp, headers=self.mh,
                              json={"status": "resolved"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(
            self.post("/api/tickets/complaint/%d/rating" % self.cmp, self.cust,
                      {"rating": 4}).status_code, 201)
        self.assertEqual(
            self.post("/api/tickets/complaint/%d/feedback" % self.cmp, self.cust,
                      {"text": "Fast fix"}).status_code, 201)
        acts = self.actions("complaint", self.cmp, self.mh)
        self.assertIn("rating", acts)
        self.assertIn("feedback", acts)
        rating_row = [r for r in self.rows("complaint", self.cmp, self.mh)
                      if r["action"] == "rating"][0]
        self.assertEqual(rating_row["user_name"], "Cust A")
        self.assertIn("4", rating_row["detail"])

    def test_anonymous_portal_activity_is_attributed_and_readable(self):
        r = self.client.patch("/api/complaints/%d" % self.cmp, headers=self.mh,
                              json={"status": "resolved"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(
            self.client.post("/api/portal/%s/rating" % self.token,
                             json={"kind": "complaint", "id": self.cmp, "rating": 5,
                                   "name": "Lab Visitor"}).status_code, 201)
        body = self.rows("complaint", self.cmp, self.mh)
        self.assertEqual(body[0]["action"], "rating")
        self.assertEqual(body[0]["user_name"], "Lab Visitor")

    def test_portal_submission_itself_is_in_the_history(self):
        r = self.client.post("/api/portal/%s/complaints" % self.token, json={
            "kind": "complaint", "name": "Walk In", "phone": PHONE,
            "subject": "Freezer warm", "description": "d", "priority": "high"})
        self.assertEqual(r.status_code, 201, r.get_json())
        out = r.get_json()
        tid = out["id"]
        body = self.rows("complaint", tid, self.mh)
        self.assertEqual(body[0]["action"], "created")
        self.assertIn("via portal", body[0]["detail"])
        # the visitor's own name is kept on the ticket; the history row is
        # attributed to the organization's account, as with any created row
        self.assertEqual(out.get("reporter_name"), "Walk In")
        self.assertTrue(body[0]["user_name"], "the row is never attributed to nobody")
        # ...and the reporter's own customer account can read it too
        self.assertEqual(self.history("complaint", tid, self.cust).status_code, 200)

    # ----------------------------------------------------------------- durability
    def test_rows_survive_the_actor_being_deleted(self):
        self.post("/api/comments", self.eng,
                  {"entity_type": "complaint", "entity_id": self.cmp, "text": "Before I left"})
        r = self.client.get("/api/users", headers=self.mh)
        eng_id = [u for u in r.get_json() if u["email"] == "eng.a@t.test"][0]["id"]
        self.assertEqual(self.client.delete("/api/users/%d" % eng_id,
                                            headers=self.mh).status_code, 200)
        body = self.rows("complaint", self.cmp, self.mh)
        comment = [x for x in body if x["action"] == "comment"][0]
        self.assertEqual(comment["user_name"], "Eng A",
                         "the name is denormalized onto the row, so deleting the user "
                         "must not blank the history")
        self.assertIn("Before I left", comment["detail"])

    def test_a_ticket_with_no_history_returns_an_empty_list(self):
        tid = self.make_complaint(self.org_a)
        c = conn()
        c.execute("DELETE FROM audit_logs WHERE entity_type='complaint' AND entity_id=?", (tid,))
        c.commit()
        c.close()
        r = self.history("complaint", tid, self.mh)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
