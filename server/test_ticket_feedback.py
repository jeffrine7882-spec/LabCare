"""Tests for customer feedback on settled tickets.

A customer may leave a 1-5 star rating and a comment thread once their ticket
is resolved or closed, either signed in or through the public QR portal. The
property that matters most is that it is inert: feedback must never change a
ticket's status, assignment or timestamps, and a ticket nobody responds to must
behave exactly as before.

Also pinned down here:

* feedback opens only once the ticket is settled;
* only the customer side may give it — staff are the ones being rated, and
  require_role() waves admins through any role list, so the restriction is
  asserted explicitly rather than trusted to the role argument;
* one rating per ticket, a re-vote replacing the previous one;
* a portal visitor is confined to the link's own organization;
* the dashboard average is scoped, so a tenant is not shown another's ratings.

Run with:  python3 -m unittest test_ticket_feedback
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


class TicketFeedbackTest(unittest.TestCase):
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
        self.make_user("Eng A", "eng.a@t.test", "engineer", customer_id=self.org_a)
        self.make_user("Cust A", "cust.a@t.test", "customer", customer_id=self.org_a)
        self.make_user("Cust A2", "cust.a2@t.test", "customer", customer_id=self.org_a)
        self.make_user("Cust B", "cust.b@t.test", "customer", customer_id=self.org_b)
        self.ta = self.auth("ta.a@t.test")
        self.eng = self.auth("eng.a@t.test")
        self.cust = self.auth("cust.a@t.test")
        self.cust2 = self.auth("cust.a2@t.test")
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

    def settle(self, kind, tid, status="resolved"):
        url = "/api/%ss/%d" % (kind, tid)
        r = self.client.patch(url, headers=self.mh, json={"status": status})
        self.assertEqual(r.status_code, 200, r.get_json())

    def rate(self, kind, tid, stars, hdrs=None, body=None):
        b = body if body is not None else {"rating": stars}
        if hdrs is None:
            return self.client.post("/api/portal/%s/rating" % self.token,
                                    json=dict(b, kind=kind, id=tid))
        return self.post("/api/tickets/%s/%d/rating" % (kind, tid), hdrs, b)

    def comment(self, kind, tid, text, hdrs=None):
        if hdrs is None:
            return self.client.post("/api/portal/%s/feedback" % self.token,
                                    json={"kind": kind, "id": tid, "text": text})
        return self.post("/api/tickets/%s/%d/feedback" % (kind, tid), hdrs, {"text": text})

    def detail(self, kind, tid, hdrs):
        return self.client.get("/api/%ss/%d" % (kind, tid), headers=hdrs).get_json()

    def rating_rows(self, kind=None, tid=None):
        c = conn()
        q, p = "SELECT * FROM ticket_ratings", []
        if kind and tid:
            q += " WHERE entity_type=? AND entity_id=?"
            p += [kind, tid]
        rows = [dict(r) for r in c.execute(q, p).fetchall()]
        c.close()
        return rows

    def ticket_row(self, kind, tid):
        c = conn()
        row = dict(c.execute("SELECT * FROM %ss WHERE id=?" % kind, (tid,)).fetchone())
        c.close()
        return row

    # -------------------------------------------------------------------- tests
    def test_feedback_is_closed_until_the_ticket_is_settled(self):
        self.assertEqual(self.rate("complaint", self.cmp, 5, self.cust).status_code, 409)
        self.assertEqual(self.comment("complaint", self.cmp, "great", self.cust).status_code, 409)
        self.assertEqual(self.rating_rows(), [], "a rating was stored for an open ticket")

        self.settle("complaint", self.cmp, "resolved")
        self.assertEqual(self.rate("complaint", self.cmp, 5, self.cust).status_code, 201)

    def test_closed_complaints_also_take_feedback(self):
        self.settle("complaint", self.cmp, "closed")
        self.assertEqual(self.rate("complaint", self.cmp, 4, self.cust).status_code, 201)

    def test_resolved_breakdowns_take_feedback(self):
        self.settle("breakdown", self.brk, "resolved")
        self.assertEqual(self.rate("breakdown", self.brk, 5, self.cust).status_code, 201)
        self.assertEqual(self.comment("breakdown", self.brk, "fixed", self.cust).status_code, 201)

    def test_in_progress_is_not_settled(self):
        self.client.patch("/api/complaints/%d" % self.cmp, headers=self.mh,
                          json={"status": "in_progress"})
        self.assertEqual(self.rate("complaint", self.cmp, 5, self.cust).status_code, 409)

    def test_rating_must_be_one_to_five_stars(self):
        self.settle("complaint", self.cmp)
        for bad in (0, 6, -1, 3.5, "five", None):
            r = self.rate("complaint", self.cmp, bad, self.cust)
            self.assertEqual(r.status_code, 400, "rating %r was accepted" % bad)
        # ...and a string digit is fine, because browsers send select/input values as text
        self.assertEqual(self.rate("complaint", self.cmp, "4", self.cust).status_code, 201)

    def test_one_rating_per_ticket_and_a_revote_replaces_it(self):
        self.settle("complaint", self.cmp)
        self.assertEqual(self.rate("complaint", self.cmp, 2, self.cust).status_code, 201)
        r = self.rate("complaint", self.cmp, 5, self.cust)
        self.assertEqual(r.status_code, 200, "a re-vote should update, not create")
        rows = self.rating_rows("complaint", self.cmp)
        self.assertEqual(len(rows), 1, "ratings stacked up instead of being replaced")
        self.assertEqual(rows[0]["rating"], 5)
        self.assertEqual(self.detail("complaint", self.cmp, self.mh)["feedback"]["rating"]["rating"], 5)

    def test_complaints_and_breakdowns_rate_independently(self):
        self.settle("complaint", self.cmp)
        self.settle("breakdown", self.brk)
        self.rate("complaint", self.cmp, 5, self.cust)
        self.rate("breakdown", self.brk, 2, self.cust)
        self.assertEqual(self.rating_rows("complaint", self.cmp)[0]["rating"], 5)
        self.assertEqual(self.rating_rows("breakdown", self.brk)[0]["rating"], 2)

    def test_only_the_customer_side_may_give_feedback(self):
        """Staff are the ones being rated — including admins, whom require_role waves through."""
        self.settle("complaint", self.cmp)
        for hdrs, who in ((self.mh, "master admin"), (self.ta, "tenant admin"), (self.eng, "engineer")):
            r = self.rate("complaint", self.cmp, 5, hdrs)
            self.assertEqual(r.status_code, 403, "%s was allowed to rate" % who)
            r = self.comment("complaint", self.cmp, "we did great", hdrs)
            self.assertEqual(r.status_code, 403, "%s was allowed to comment" % who)
        self.assertEqual(self.rating_rows(), [])

    def test_another_organizations_customer_cannot_rate_it(self):
        self.settle("complaint", self.cmp)
        self.assertEqual(self.rate("complaint", self.cmp, 5, self.cust_b).status_code, 403)
        self.assertEqual(self.rating_rows(), [])

    def test_any_customer_user_at_the_organization_may_rate(self):
        """The person who noticed the fix may not be the person who reported it."""
        self.settle("complaint", self.cmp)
        self.assertEqual(self.rate("complaint", self.cmp, 4, self.cust2).status_code, 201)
        self.assertEqual(self.rating_rows("complaint", self.cmp)[0]["rating"], 4)

    def test_the_comment_thread_grows_and_keeps_its_order(self):
        self.settle("complaint", self.cmp)
        self.assertEqual(self.comment("complaint", self.cmp, "First", self.cust).status_code, 201)
        self.assertEqual(self.comment("complaint", self.cmp, "Second", self.cust2).status_code, 201)
        self.assertEqual(self.comment("complaint", self.cmp, "Third", self.cust).status_code, 201)
        got = self.detail("complaint", self.cmp, self.mh)["feedback"]["comments"]
        self.assertEqual([g["text"] for g in got], ["First", "Second", "Third"])
        self.assertEqual([g["author_name"] for g in got], ["Cust A", "Cust A2", "Cust A"])

    def test_a_comment_may_come_without_a_rating_and_vice_versa(self):
        """Both halves are optional, independently."""
        self.settle("complaint", self.cmp)
        self.assertEqual(self.comment("complaint", self.cmp, "All good now", self.cust).status_code, 201)
        fb = self.detail("complaint", self.cmp, self.mh)["feedback"]
        self.assertIsNone(fb["rating"], "a comment invented a rating")
        self.assertEqual(len(fb["comments"]), 1)

        self.settle("breakdown", self.brk)
        self.assertEqual(self.rate("breakdown", self.brk, 5, self.cust).status_code, 201)
        fb = self.detail("breakdown", self.brk, self.mh)["feedback"]
        self.assertEqual(fb["rating"]["rating"], 5)
        self.assertEqual(fb["comments"], [], "a rating invented a comment")

    def test_blank_and_oversized_comments_are_rejected(self):
        self.settle("complaint", self.cmp)
        self.assertEqual(self.comment("complaint", self.cmp, "   ", self.cust).status_code, 400)
        self.assertEqual(self.comment("complaint", self.cmp, "x" * 2001, self.cust).status_code, 400)
        self.assertEqual(self.comment("complaint", self.cmp, "x" * 2000, self.cust).status_code, 201)

    def test_feedback_never_changes_the_ticket(self):
        """The guarantee the requirement rests on."""
        self.settle("complaint", self.cmp)
        before = self.ticket_row("complaint", self.cmp)
        self.rate("complaint", self.cmp, 1, self.cust)
        self.comment("complaint", self.cmp, "Terrible, still broken", self.cust)
        after = self.ticket_row("complaint", self.cmp)
        self.assertEqual(before, after, "feedback altered the ticket row")
        self.assertEqual(after["status"], "resolved")

    def test_a_low_rating_does_not_reopen_or_reassign_anything(self):
        self.settle("complaint", self.cmp)
        assigned_before = self.ticket_row("complaint", self.cmp)["assigned_to"]
        self.rate("complaint", self.cmp, 1, self.cust)
        row = self.ticket_row("complaint", self.cmp)
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["assigned_to"], assigned_before)

    def test_an_unrated_ticket_reports_no_feedback(self):
        self.settle("complaint", self.cmp)
        fb = self.detail("complaint", self.cmp, self.mh)["feedback"]
        self.assertEqual(fb, {"rating": None, "comments": []})
        self.assertTrue(self.detail("complaint", self.cmp, self.mh)["feedback_open"])

    def test_feedback_open_tracks_the_status(self):
        self.assertFalse(self.detail("complaint", self.cmp, self.mh)["feedback_open"])
        self.settle("complaint", self.cmp)
        self.assertTrue(self.detail("complaint", self.cmp, self.mh)["feedback_open"])

    def test_staff_may_read_feedback(self):
        self.settle("complaint", self.cmp)
        self.rate("complaint", self.cmp, 5, self.cust)
        self.comment("complaint", self.cmp, "Very quick fix", self.cust)
        for hdrs, who in ((self.mh, "master"), (self.ta, "tenant admin"), (self.eng, "engineer")):
            fb = self.detail("complaint", self.cmp, hdrs)["feedback"]
            self.assertEqual(fb["rating"]["rating"], 5, "%s could not read the rating" % who)
            self.assertEqual(fb["comments"][0]["text"], "Very quick fix")

    def test_the_rating_names_who_gave_it(self):
        self.settle("complaint", self.cmp)
        self.rate("complaint", self.cmp, 5, self.cust)
        fb = self.detail("complaint", self.cmp, self.mh)["feedback"]
        self.assertEqual(fb["rating"]["rated_by"], "Cust A")

    def test_feedback_is_recorded_in_the_ticket_history(self):
        """Recorded in audit_logs, the table the History timeline renders from.

        Asserted against the table rather than the API: the app fetches this
        timeline from GET /api/audit, a route that does not exist anywhere in
        app.py, so the History section currently renders "History unavailable."
        That is a pre-existing bug, independent of feedback.
        """
        self.settle("complaint", self.cmp)
        self.rate("complaint", self.cmp, 4, self.cust)
        self.comment("complaint", self.cmp, "Good service", self.cust)
        c = conn()
        rows = [dict(r) for r in c.execute(
            "SELECT action, detail, user_name FROM audit_logs "
            "WHERE entity_type='complaint' AND entity_id=? ORDER BY id", (self.cmp,)).fetchall()]
        c.close()
        actions = [r["action"] for r in rows]
        self.assertIn("rating", actions)
        self.assertIn("feedback", actions)
        rating_row = [r for r in rows if r["action"] == "rating"][0]
        self.assertEqual(rating_row["detail"], "4 stars")
        self.assertEqual(rating_row["user_name"], "Cust A")

    # --------------------------------------------------------------- the portal
    def test_portal_visitor_may_rate_without_an_account(self):
        self.settle("complaint", self.cmp)
        r = self.rate("complaint", self.cmp, 5)
        self.assertEqual(r.status_code, 201, r.get_json())
        rows = self.rating_rows("complaint", self.cmp)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["user_id"], "a portal visitor was given a user id")

    def test_portal_rating_uses_the_supplied_name_or_reads_as_customer(self):
        self.settle("complaint", self.cmp)
        self.settle("breakdown", self.brk)
        self.rate("complaint", self.cmp, 5, body={"rating": 5, "name": "Dr. Aina"})
        self.rate("breakdown", self.brk, 4)
        fb = self.detail("complaint", self.cmp, self.mh)["feedback"]
        self.assertEqual(fb["rating"]["rated_by"], "Dr. Aina")
        fb = self.detail("breakdown", self.brk, self.mh)["feedback"]
        self.assertEqual(fb["rating"]["rated_by"], "Customer")

    def test_portal_visitor_may_comment(self):
        self.settle("complaint", self.cmp)
        r = self.client.post("/api/portal/%s/feedback" % self.token,
                             json={"kind": "complaint", "id": self.cmp,
                                   "text": "Working perfectly now", "name": "Lab Tech"})
        self.assertEqual(r.status_code, 201, r.get_json())
        fb = self.detail("complaint", self.cmp, self.mh)["feedback"]
        self.assertEqual(fb["comments"][0]["text"], "Working perfectly now")
        self.assertEqual(fb["comments"][0]["author_name"], "Lab Tech")

    def test_portal_is_confined_to_its_own_organization(self):
        self.settle("complaint", self.cmp_b)
        self.assertEqual(self.rate("complaint", self.cmp_b, 5).status_code, 404)
        self.assertEqual(self.comment("complaint", self.cmp_b, "hi").status_code, 404)
        s, d = self.portal_get("feedback", {"kind": "complaint", "id": self.cmp_b})
        self.assertEqual(s, 404)
        self.assertEqual(self.rating_rows(), [])

    def test_portal_needs_a_valid_token(self):
        self.settle("complaint", self.cmp)
        r = self.client.post("/api/portal/deadbeefdeadbeef/rating",
                             json={"kind": "complaint", "id": self.cmp, "rating": 5})
        self.assertEqual(r.status_code, 404, r.get_json())

    def test_portal_respects_the_settlement_rule(self):
        self.assertEqual(self.rate("complaint", self.cmp, 5).status_code, 409)
        self.assertEqual(self.comment("complaint", self.cmp, "hi").status_code, 409)

    def test_portal_validates_its_input(self):
        self.settle("complaint", self.cmp)
        self.assertEqual(self.rate("complaint", self.cmp, 9).status_code, 400)
        self.assertEqual(self.rate("nonsense", self.cmp, 5).status_code, 400)
        self.assertEqual(self.client.post("/api/portal/%s/feedback" % self.token,
                                         json={"kind": "complaint", "id": self.cmp, "text": ""}
                                         ).status_code, 400)
        s, d = self.portal_get("feedback", {"kind": "complaint"})
        self.assertEqual(s, 400)

    def portal_get(self, what, params):
        q = "&".join("%s=%s" % kv for kv in params.items())
        r = self.client.get("/api/portal/%s/%s?%s" % (self.token, what, q))
        return r.status_code, r.get_json()

    def test_portal_feedback_endpoint_reports_the_state(self):
        s, d = self.portal_get("feedback", {"kind": "complaint", "id": self.cmp})
        self.assertEqual(s, 200)
        self.assertFalse(d["settled"], "an open ticket reported as settled")

        self.settle("complaint", self.cmp)
        self.rate("complaint", self.cmp, 3)
        self.comment("complaint", self.cmp, "Slow but fine")
        s, d = self.portal_get("feedback", {"kind": "complaint", "id": self.cmp})
        self.assertTrue(d["settled"])
        self.assertEqual(d["status"], "resolved")
        self.assertEqual(d["rating"]["rating"], 3)
        self.assertEqual(len(d["comments"]), 1)

    def test_portal_history_carries_the_rating_inline(self):
        self.settle("complaint", self.cmp)
        self.rate("complaint", self.cmp, 5)
        self.comment("complaint", self.cmp, "thanks")
        rows = self.client.get("/api/portal/%s/history" % self.token).get_json()
        mine = [r for r in rows if r["id"] == self.cmp and r["kind"] == "complaint"][0]
        self.assertEqual(mine["rating"], 5)
        self.assertEqual(mine["feedback_comments"], 1)
        self.assertTrue(mine["feedback_open"])
        # An unsettled ticket is flagged as not open for feedback.
        open_one = [r for r in rows if r["id"] == self.brk and r["kind"] == "breakdown"][0]
        self.assertFalse(open_one["feedback_open"])
        self.assertIsNone(open_one["rating"])

    # ---------------------------------------------------------------- dashboard
    def test_dashboard_reports_the_average_and_count(self):
        d = self.client.get("/api/dashboard", headers=self.mh).get_json()
        self.assertEqual(d["customer_feedback"], {"average_rating": None, "rating_count": 0},
                         "an unrated platform should not invent an average")

        self.settle("complaint", self.cmp)
        self.settle("breakdown", self.brk)
        self.rate("complaint", self.cmp, 5, self.cust)
        self.rate("breakdown", self.brk, 4, self.cust)
        d = self.client.get("/api/dashboard", headers=self.mh).get_json()
        self.assertEqual(d["customer_feedback"], {"average_rating": 4.5, "rating_count": 2})

    def test_dashboard_average_is_scoped_per_tenant(self):
        """One tenant must not be shown another tenant's satisfaction."""
        self.settle("complaint", self.cmp)
        self.settle("complaint", self.cmp_b)
        self.rate("complaint", self.cmp, 5, self.cust)
        self.rate("complaint", self.cmp_b, 1, self.cust_b)

        self.make_user("TA B", "ta.b@t.test", "admin", customer_id=self.org_b)
        ta_b = self.auth("ta.b@t.test")
        self.assertEqual(self.client.get("/api/dashboard", headers=self.ta).get_json()
                         ["customer_feedback"], {"average_rating": 5.0, "rating_count": 1})
        self.assertEqual(self.client.get("/api/dashboard", headers=ta_b).get_json()
                         ["customer_feedback"], {"average_rating": 1.0, "rating_count": 1})
        self.assertEqual(self.client.get("/api/dashboard", headers=self.mh).get_json()
                         ["customer_feedback"], {"average_rating": 3.0, "rating_count": 2})

    def test_a_customer_user_sees_their_own_organizations_average(self):
        self.settle("complaint", self.cmp)
        self.settle("complaint", self.cmp_b)
        self.rate("complaint", self.cmp, 2, self.cust)
        self.rate("complaint", self.cmp_b, 5, self.cust_b)
        d = self.client.get("/api/dashboard", headers=self.cust).get_json()
        self.assertEqual(d["customer_feedback"], {"average_rating": 2.0, "rating_count": 1})

    # ------------------------------------------------------------------ guarding
    def test_unknown_ticket_and_kind(self):
        self.assertEqual(self.rate("complaint", 999999, 5, self.cust).status_code, 404)
        self.assertEqual(self.rate("invoice", 1, 5, self.cust).status_code, 400)
        self.assertEqual(self.comment("complaint", 999999, "hi", self.cust).status_code, 404)

    def test_feedback_endpoints_need_authentication(self):
        anon = app.test_client()
        self.assertEqual(anon.post("/api/tickets/complaint/1/rating",
                                  json={"rating": 5}).status_code, 401)
        self.assertEqual(anon.post("/api/tickets/complaint/1/feedback",
                                  json={"text": "hi"}).status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
