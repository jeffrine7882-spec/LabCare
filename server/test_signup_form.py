"""Tests for the public "Request an account" form (POST /api/signup).

The login-screen form posts what an HTML <select> holds — strings such as
"5" — while ids read back from the database are ints. The ownership check in
_validate_loc_dept compared the two with `!=`, so choosing an EXISTING
organization together with one of its EXISTING locations was rejected with
"Location does not belong to the selected organization" every single time
(only the "type a new name" paths worked). These tests pin the behaviour of
every path the form can take, with ids given exactly as the browser sends them.

Run with:  python3 -m unittest test_signup_form
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

from server.database import init_db, conn, now
init_db()
from server.app import app, _id, _validate_loc_dept


class IdCoercionTest(unittest.TestCase):
    def test_id_normalises_form_values(self):
        self.assertEqual(_id("5"), 5)
        self.assertEqual(_id(5), 5)
        self.assertEqual(_id(" 7 "), 7)
        for unset in ("", None, 0, "0"):
            self.assertIsNone(_id(unset), repr(unset))
        # the form's sentinel values must never look like a record id
        self.assertIsNone(_id("__new__"))
        self.assertIsNone(_id("name:Main Lab"))


class SignupFormTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        c = conn()
        c.execute("PRAGMA foreign_keys = OFF")
        for t in ["notifications", "notification_pings", "departments", "locations",
                  "onboarding_apps", "sessions", "users", "customers"]:
            c.execute(f"DELETE FROM {t}")
        c.execute("PRAGMA foreign_keys = ON")
        # Org A has two locations (each mirrored by a department); Org B has one
        # location WITHOUT a department row; Org C has no locations at all.
        self.org_a = self._customer(c, "Hospital A")
        self.org_b = self._customer(c, "Clinic B")
        self.org_c = self._customer(c, "Lab C")
        self.loc_a1 = self._location(c, self.org_a, "Molecular Lab", department=True)
        self.loc_a2 = self._location(c, self.org_a, "Haematology", department=True)
        self.loc_b1 = self._location(c, self.org_b, "Main Lab", department=False)
        c.commit()
        c.close()
        self.n = 0

    def _customer(self, c, name):
        return c.execute(
            "INSERT INTO customers (name,contact_name,email,phone,address,city,created_at) VALUES (?,?,?,?,?,?,?)",
            (name, "Contact", f"{name.lower().replace(' ', '')}@x.test", "0123", "", "", now())).lastrowid

    def _location(self, c, customer_id, name, department):
        lid = c.execute(
            "INSERT INTO locations (customer_id,name,address,city,created_at) VALUES (?,?,?,?,?)",
            (customer_id, name, "", "", now())).lastrowid
        if department:
            c.execute("INSERT INTO departments (customer_id,location_id,name,created_at) VALUES (?,?,?,?)",
                      (customer_id, lid, name, now()))
        return lid

    def signup(self, **fields):
        self.n += 1
        body = {"name": f"Person {self.n}", "email": f"person{self.n}@x.test", "phone": "0123456789",
                "password": "secret12", "role": "customer"}
        body.update(fields)
        return self.client.post("/api/signup", json=body)

    def pending(self):
        c = conn()
        rows = c.execute("SELECT * FROM onboarding_apps ORDER BY id DESC").fetchall()
        c.close()
        return [dict(r) for r in rows]

    # ---- the regression: ids as the <select> posts them (strings) -------------
    def test_existing_org_and_location_as_strings_is_accepted(self):
        r = self.signup(customer_id=str(self.org_a), location_id=str(self.loc_a1), department_id="")
        self.assertEqual(r.status_code, 201, r.get_json())
        app_row = self.pending()[0]
        self.assertEqual(app_row["customer_id"], self.org_a)
        self.assertEqual(app_row["location_id"], self.loc_a1)
        # the department mirroring the location is resolved server-side
        c = conn()
        dept = c.execute("SELECT id FROM departments WHERE location_id=?", (self.loc_a1,)).fetchone()
        c.close()
        self.assertEqual(app_row["department_id"], dept["id"])

    def test_existing_org_and_location_as_ints_is_accepted(self):
        r = self.signup(customer_id=self.org_a, location_id=self.loc_a2)
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(self.pending()[0]["location_id"], self.loc_a2)

    def test_location_without_department_row_is_accepted(self):
        # The old form invented department_id = location_id here; the server must
        # cope with no department at all and must not be fooled by a made-up id.
        r = self.signup(customer_id=str(self.org_b), location_id=str(self.loc_b1))
        self.assertEqual(r.status_code, 201, r.get_json())
        row = self.pending()[0]
        self.assertEqual(row["location_id"], self.loc_b1)
        self.assertIsNone(row["department_id"])

    def test_department_of_another_location_is_still_rejected(self):
        c = conn()
        dept_a2 = c.execute("SELECT id FROM departments WHERE location_id=?", (self.loc_a2,)).fetchone()["id"]
        c.close()
        r = self.signup(customer_id=str(self.org_a), location_id=str(self.loc_a1), department_id=str(dept_a2))
        self.assertEqual(r.status_code, 400)
        self.assertIn("not within the selected location", r.get_json()["error"])

    def test_location_of_another_org_is_still_rejected(self):
        r = self.signup(customer_id=str(self.org_b), location_id=str(self.loc_a1))
        self.assertEqual(r.status_code, 400)
        self.assertIn("does not belong", r.get_json()["error"])
        self.assertEqual(self.pending(), [])

    def test_validate_loc_dept_is_type_insensitive(self):
        c = conn()
        with app.app_context():  # the error branch builds a jsonify() response
            self.assertEqual(_validate_loc_dept(c, str(self.org_a), str(self.loc_a1), None), (None, None))
            self.assertEqual(_validate_loc_dept(c, self.org_a, self.loc_a1, None), (None, None))
            self.assertEqual(_validate_loc_dept(c, str(self.org_a), self.loc_a1, None), (None, None))
            err, code = _validate_loc_dept(c, str(self.org_b), str(self.loc_a1), None)
        c.close()
        self.assertEqual(code, 400)

    # ---- the "create new" paths keep working ---------------------------------
    def test_new_location_under_existing_org(self):
        r = self.signup(customer_id=str(self.org_c), new_location_name="Cytology")
        self.assertEqual(r.status_code, 201, r.get_json())
        row = self.pending()[0]
        self.assertEqual(row["customer_id"], self.org_c)
        c = conn()
        loc = c.execute("SELECT id, customer_id FROM locations WHERE name='Cytology'").fetchone()
        dept = c.execute("SELECT id FROM departments WHERE location_id=?", (loc["id"],)).fetchone()
        c.close()
        self.assertEqual(loc["customer_id"], self.org_c)
        self.assertEqual(row["location_id"], loc["id"])
        self.assertEqual(row["department_id"], dept["id"])

    def test_new_location_name_matching_existing_reuses_it(self):
        r = self.signup(customer_id=str(self.org_a), new_location_name="molecular lab")
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(self.pending()[0]["location_id"], self.loc_a1)

    def test_new_org_with_new_location(self):
        r = self.signup(new_customer_name="Klinik Baru", new_location_name="Main Lab")
        self.assertEqual(r.status_code, 201, r.get_json())
        row = self.pending()[0]
        c = conn()
        cust = c.execute("SELECT id, pending_care FROM customers WHERE name='Klinik Baru'").fetchone()
        loc = c.execute("SELECT id, name FROM locations WHERE customer_id=?", (cust["id"],)).fetchone()
        c.close()
        self.assertEqual(row["customer_id"], cust["id"])
        self.assertEqual(cust["pending_care"], 1)
        self.assertEqual(loc["name"], "Main Lab")
        self.assertEqual(row["location_id"], loc["id"])

    def test_new_org_reusing_an_existing_location_name_copies_not_links(self):
        # "name:Main Lab" in the form becomes new_location_name for the new org:
        # the new org gets its OWN "Main Lab", Clinic B's is untouched.
        r = self.signup(new_customer_name="Klinik Dua", new_location_name="Main Lab")
        self.assertEqual(r.status_code, 201, r.get_json())
        row = self.pending()[0]
        self.assertNotEqual(row["location_id"], self.loc_b1)
        c = conn()
        n = c.execute("SELECT COUNT(*) AS n FROM locations WHERE name='Main Lab'").fetchone()["n"]
        c.close()
        self.assertEqual(n, 2)

    # ---- guidance when the form is incomplete ---------------------------------
    def test_missing_org_and_missing_location_are_explained(self):
        r = self.signup()
        self.assertEqual(r.status_code, 400)
        self.assertIn("organization", r.get_json()["error"].lower())
        r = self.signup(customer_id=str(self.org_a))
        self.assertEqual(r.status_code, 400)
        self.assertIn("location/department", r.get_json()["error"].lower())

    def test_lookup_options_feed_the_form(self):
        d = self.client.get("/api/lookup/options").get_json()
        self.assertEqual({c["name"] for c in d["customers"]}, {"Hospital A", "Clinic B", "Lab C"})
        by_org = {}
        for l in d["locations"]:
            by_org.setdefault(l["customer_id"], set()).add(l["name"])
        self.assertEqual(by_org[self.org_a], {"Molecular Lab", "Haematology"})
        self.assertEqual(by_org[self.org_b], {"Main Lab"})
        self.assertNotIn(self.org_c, by_org)


if __name__ == "__main__":
    unittest.main()
