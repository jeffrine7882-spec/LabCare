"""A signed-in user stays signed in until they sign out.

seed() runs on every server start (run.py / wsgi.py), i.e. on every deploy,
container restart and Fly machine restart. It used to begin with
`DELETE FROM sessions`, which signed every user out of every device — web,
Android app and Windows app alike — each time the server came up. These tests
pin the rule from every side the server controls:

  * a session survives a simulated restart (init_db() + seed());
  * a session never expires on its own, however old it is;
  * the login cookie (the fallback channel) is long-lived and re-issued on
    every /api/me so it slides forward with use;
  * signing out — through any channel the token can travel — really ends it.

Run with:  python3 -m unittest test_session_persistence
"""
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

test_db = tempfile.mktemp(suffix=".db")
os.environ["LABCARE_DB"] = test_db
os.environ["LABCARE_SECRET"] = "test-secret"

from server.database import init_db, conn, hash_password
init_db()
from server.app import app, SESSION_COOKIE_MAX_AGE
from server import seed as seed_mod

PW = "Passw0rd!"
EMAIL = "keep@me.test"


def cookie_max_age(resp):
    for h in resp.headers.getlist("Set-Cookie"):
        if h.startswith("labcare_token="):
            m = re.search(r"Max-Age=(\d+)", h)
            return int(m.group(1)) if m else None
    return None


class SessionPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        c = conn()
        c.execute("PRAGMA foreign_keys = OFF")
        for t in ["notifications", "notification_pings", "sessions", "users", "customers", "categories"]:
            c.execute(f"DELETE FROM {t}")
        c.execute("PRAGMA foreign_keys = ON")
        c.execute(
            "INSERT INTO users (name, email, password_hash, role, pending, active, created_at) "
            "VALUES (?, ?, ?, ?, 0, 1, ?)",
            ("Keep Me", EMAIL, hash_password(PW), "engineer", "2026-01-01 00:00:00"))
        c.commit()
        c.close()

    def login(self):
        r = self.client.post("/api/login", json={"email": EMAIL, "password": PW})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r

    def me(self, token):
        return self.client.get("/api/me", headers={"Authorization": "Bearer " + token})

    def session_count(self):
        c = conn()
        n = c.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
        c.close()
        return n

    # ---- the regression -------------------------------------------------------
    def test_session_survives_a_server_restart(self):
        token = self.login().get_json()["token"]
        self.assertEqual(self.me(token).status_code, 200)
        # what run.py / wsgi.py do on every boot
        init_db()
        seed_mod.seed()
        self.assertEqual(self.session_count(), 1)
        self.assertEqual(self.me(token).status_code, 200, "restart must not sign the user out")

    def test_many_devices_survive_a_restart(self):
        tokens = [self.login().get_json()["token"] for _ in range(3)]
        init_db()
        seed_mod.seed()
        for t in tokens:
            self.assertEqual(self.me(t).status_code, 200)

    def test_seed_is_still_a_noop_on_a_populated_database(self):
        # Removing the DELETE must not turn seed() into something that touches
        # existing data: no new users, categories curated once, sessions intact.
        token = self.login().get_json()["token"]
        seed_mod.seed()
        c = conn()
        users = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        c.close()
        self.assertEqual(users, 1)
        self.assertEqual(self.session_count(), 1)
        self.assertEqual(self.me(token).status_code, 200)

    def test_session_never_expires_on_its_own(self):
        token = self.login().get_json()["token"]
        c = conn()
        c.execute("UPDATE sessions SET created_at='2020-01-01 00:00:00' WHERE token=?", (token,))
        c.commit()
        c.close()
        self.assertEqual(self.me(token).status_code, 200)

    # ---- the cookie fallback channel -----------------------------------------
    def test_login_cookie_is_long_lived(self):
        r = self.login()
        self.assertEqual(cookie_max_age(r), SESSION_COOKIE_MAX_AGE)
        self.assertGreaterEqual(SESSION_COOKIE_MAX_AGE, 365 * 24 * 3600)

    def test_me_reissues_the_cookie_so_it_slides(self):
        token = self.login().get_json()["token"]
        r = self.me(token)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(cookie_max_age(r), SESSION_COOKIE_MAX_AGE)

    def test_cookie_alone_authenticates(self):
        token = self.login().get_json()["token"]
        self.client.set_cookie("labcare_token", token)
        r = self.client.get("/api/me")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["email"], EMAIL)
        self.client.delete_cookie("labcare_token")

    # ---- the one way out: signing out ------------------------------------------
    def test_logout_ends_the_session(self):
        token = self.login().get_json()["token"]
        r = self.client.post("/api/logout", headers={"Authorization": "Bearer " + token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.session_count(), 0)
        self.assertEqual(self.me(token).status_code, 401)

    def test_logout_accepts_every_token_channel(self):
        for send in ("header", "x-auth", "query", "cookie"):
            token = self.login().get_json()["token"]
            if send == "header":
                r = self.client.post("/api/logout", headers={"Authorization": "Bearer " + token})
            elif send == "x-auth":
                r = self.client.post("/api/logout", headers={"X-Auth-Token": token})
            elif send == "query":
                r = self.client.post("/api/logout?token=" + token)
            else:
                self.client.set_cookie("labcare_token", token)
                r = self.client.post("/api/logout")
                self.client.delete_cookie("labcare_token")
            self.assertEqual(r.status_code, 200, send)
            self.assertEqual(self.me(token).status_code, 401, f"{send}: session must be gone")

    def test_logout_on_one_device_leaves_other_devices_signed_in(self):
        phone = self.login().get_json()["token"]
        laptop = self.login().get_json()["token"]
        self.client.post("/api/logout", headers={"Authorization": "Bearer " + phone})
        self.assertEqual(self.me(phone).status_code, 401)
        self.assertEqual(self.me(laptop).status_code, 200)

    def test_unauthenticated_me_is_the_json_401_clients_key_on(self):
        r = self.me("no-such-token")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json(), {"error": "Not authenticated"})


if __name__ == "__main__":
    unittest.main()
