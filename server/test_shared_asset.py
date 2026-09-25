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


class SharedAssetAndLocationTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        c = conn()
        c.execute("PRAGMA foreign_keys = OFF")
        for t in ["pm_logs", "pm_schedules", "portal_links", "push_subscriptions", "app_devices",
                  "audit_logs", "comments", "attachments", "notifications", "notification_pings",
                  "breakdowns", "complaints", "equipment", "departments", "locations",
                  "admin_customer_links", "onboarding_apps", "sessions", "users", "customers"]:
            c.execute(f"DELETE FROM {t}")
        c.execute("PRAGMA foreign_keys = ON")
        c.execute(
            "INSERT INTO users (name, email, password_hash, role, pending, active, created_at) "
            "VALUES (?, ?, ?, ?, 0, 1, ?)",
            ("Master Admin", "admin@labcare.com", hash_password("password123"), "admin", "2026-01-01 00:00:00")
        )
        c.commit()
        c.close()
        res = self.client.post("/api/login", json={"email": "admin@labcare.com", "password": "password123"})
        self.assertEqual(res.status_code, 200)

    def test_shared_locations_across_customers(self):
        # 1. Create two separate customers/organizations
        c1 = self.client.post("/api/customers", json={"name": "BioReference Labs", "city": "KL"}).get_json()["id"]
        c2 = self.client.post("/api/customers", json={"name": "Meridian Diagnostics", "city": "Penang"}).get_json()["id"]

        # 2. Both organizations have a Location/Department named "Main Laboratory"
        l1 = self.client.post("/api/locations", json={"name": "Main Laboratory", "customer_id": c1}).get_json()["id"]
        l2 = self.client.post("/api/locations", json={"name": "Main Laboratory", "customer_id": c2}).get_json()["id"]
        self.assertNotEqual(l1, l2)

        # 3. Both auto-create matching departments with the same name
        d1 = self.client.get(f"/api/departments?location_id={l1}").get_json()[0]
        d2 = self.client.get(f"/api/departments?location_id={l2}").get_json()[0]
        self.assertEqual(d1["name"], "Main Laboratory")
        self.assertEqual(d2["name"], "Main Laboratory")
        self.assertEqual(d1["customer_id"], c1)
        self.assertEqual(d2["customer_id"], c2)

        # 4. Scoped query by customer_id isolates each organization's location
        locs_c1 = self.client.get(f"/api/locations?customer_id={c1}").get_json()
        locs_c2 = self.client.get(f"/api/locations?customer_id={c2}").get_json()
        self.assertEqual(len(locs_c1), 1)
        self.assertEqual(len(locs_c2), 1)
        self.assertEqual(locs_c1[0]["customer_id"], c1)
        self.assertEqual(locs_c2[0]["customer_id"], c2)

    def test_shared_equipment_names_and_distinguishing_details(self):
        c1 = self.client.post("/api/customers", json={"name": "BioReference Labs"}).get_json()["id"]
        c2 = self.client.post("/api/customers", json={"name": "Meridian Diagnostics"}).get_json()["id"]
        l1 = self.client.post("/api/locations", json={"name": "Main Laboratory", "customer_id": c1}).get_json()["id"]
        l2 = self.client.post("/api/locations", json={"name": "Main Laboratory", "customer_id": c2}).get_json()["id"]

        # 1. Multiple equipment with identical name "Centrifuge" under the same customer
        eq1 = self.client.post("/api/equipment", json={
            "name": "Centrifuge", "customer_id": c1, "location_id": l1,
            "model": "5810 R", "serial_number": "CF-BIO-01"
        }).get_json()
        eq2 = self.client.post("/api/equipment", json={
            "name": "Centrifuge", "customer_id": c1, "location_id": l1,
            "model": "5424", "serial_number": "CF-BIO-02"
        }).get_json()
        self.assertEqual(eq1["name"], eq2["name"])
        self.assertNotEqual(eq1["serial_number"], eq2["serial_number"])

        # 2. Duplicate serial number for the same customer is rejected (409)
        dup = self.client.post("/api/equipment", json={
            "name": "Centrifuge", "customer_id": c1, "location_id": l1,
            "model": "Any", "serial_number": "CF-BIO-01"
        })
        self.assertEqual(dup.status_code, 409)

        # 3. Same serial number for DIFFERENT customer is permitted
        eq3 = self.client.post("/api/equipment", json={
            "name": "Centrifuge", "customer_id": c2, "location_id": l2,
            "model": "5810 R", "serial_number": "CF-BIO-01"
        })
        self.assertEqual(eq3.status_code, 201)

        # 4. Multiple equipment with blank serial numbers under same customer are permitted
        eq_un1 = self.client.post("/api/equipment", json={
            "name": "Vortex Mixer", "customer_id": c1, "location_id": l1, "serial_number": ""
        })
        eq_un2 = self.client.post("/api/equipment", json={
            "name": "Vortex Mixer", "customer_id": c1, "location_id": l1, "serial_number": ""
        })
        self.assertEqual(eq_un1.status_code, 201)
        self.assertEqual(eq_un2.status_code, 201)

        # 5. Ticket payloads clearly reflect details (name, model, serial)
        cmp = self.client.post("/api/complaints", json={
            "subject": "Rotor imbalance", "customer_id": c1, "location_id": l1, "equipment_id": eq1["id"]
        }).get_json()
        self.assertEqual(cmp["equipment_name"], "Centrifuge — 5810 R")
        self.assertEqual(cmp["equipment_serial"], "CF-BIO-01")
        self.assertEqual(cmp["equipment_model"], "5810 R")

        brk = self.client.post("/api/breakdowns", json={
            "fault_description": "Overheating", "customer_id": c1, "location_id": l1, "equipment_id": eq2["id"]
        }).get_json()
        self.assertEqual(brk["equipment_name"], "Centrifuge — 5424")
        self.assertEqual(brk["equipment_serial"], "CF-BIO-02")
        self.assertEqual(brk["equipment_model"], "5424")

        # 6. PM payload reflects details
        pm = self.client.post("/api/pms", json={
            "title": "Centrifuge quarterly maintenance", "customer_id": c1, "equipment_id": eq1["id"], "interval_days": 90
        }).get_json()
        self.assertEqual(pm["equipment_name"], "Centrifuge — 5810 R")
        self.assertEqual(pm["equipment_serial"], "CF-BIO-01")

    def test_customer_user_isolation_with_shared_names(self):
        # 1. Setup customers and identical locations and equipment
        c1 = self.client.post("/api/customers", json={"name": "Hospital Alpha"}).get_json()["id"]
        c2 = self.client.post("/api/customers", json={"name": "Hospital Beta"}).get_json()["id"]

        l1 = self.client.post("/api/locations", json={"name": "Main Laboratory", "customer_id": c1}).get_json()["id"]
        l2 = self.client.post("/api/locations", json={"name": "Main Laboratory", "customer_id": c2}).get_json()["id"]

        eq1 = self.client.post("/api/equipment", json={
            "name": "Centrifuge", "customer_id": c1, "location_id": l1,
            "model": "5810 R", "serial_number": "SN-001"
        }).get_json()

        eq2 = self.client.post("/api/equipment", json={
            "name": "Centrifuge", "customer_id": c2, "location_id": l2,
            "model": "Allegra X", "serial_number": "SN-002"
        }).get_json()

        # 2. Create customer users
        u1_res = self.client.post("/api/users", json={
            "name": "User Alpha", "email": "user.alpha@hospital.test", "password": "password123",
            "role": "customer", "customer_id": c1, "location_id": l1
        })
        self.assertEqual(u1_res.status_code, 201)

        u2_res = self.client.post("/api/users", json={
            "name": "User Beta", "email": "user.beta@hospital.test", "password": "password123",
            "role": "customer", "customer_id": c2, "location_id": l2
        })
        self.assertEqual(u2_res.status_code, 201)

        # 3. Log in as User Alpha -> sees ONLY customer 1's "Main Laboratory" and "Centrifuge (SN-001)"
        client1 = app.test_client()
        login1 = client1.post("/api/login", json={"email": "user.alpha@hospital.test", "password": "password123"})
        self.assertEqual(login1.status_code, 200)

        c1_locs = client1.get("/api/locations").get_json()
        self.assertEqual(len(c1_locs), 1)
        self.assertEqual(c1_locs[0]["id"], l1)
        self.assertEqual(c1_locs[0]["name"], "Main Laboratory")

        c1_eq = client1.get("/api/equipment").get_json()
        self.assertEqual(len(c1_eq), 1)
        self.assertEqual(c1_eq[0]["id"], eq1["id"])
        self.assertEqual(c1_eq[0]["name"], "Centrifuge")
        self.assertEqual(c1_eq[0]["serial_number"], "SN-001")

        # 4. Log in as User Beta -> sees ONLY customer 2's "Main Laboratory" and "Centrifuge (SN-002)"
        client2 = app.test_client()
        login2 = client2.post("/api/login", json={"email": "user.beta@hospital.test", "password": "password123"})
        self.assertEqual(login2.status_code, 200)

        c2_locs = client2.get("/api/locations").get_json()
        self.assertEqual(len(c2_locs), 1)
        self.assertEqual(c2_locs[0]["id"], l2)
        self.assertEqual(c2_locs[0]["name"], "Main Laboratory")

        c2_eq = client2.get("/api/equipment").get_json()
        self.assertEqual(len(c2_eq), 1)
        self.assertEqual(c2_eq[0]["id"], eq2["id"])
        self.assertEqual(c2_eq[0]["name"], "Centrifuge")
        self.assertEqual(c2_eq[0]["serial_number"], "SN-002")

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(test_db):
            os.remove(test_db)


if __name__ == "__main__":
    unittest.main()
