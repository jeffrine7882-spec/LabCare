"""LabCare — REST API (Flask)."""
import os
import sys
import json
import uuid
import mimetypes
from datetime import datetime

# allow running as `python server/app.py` or as a package
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, request, jsonify, g, send_from_directory, Response, make_response
from flask_cors import CORS

import mailer as email_mod
import report as report_mod
from database import conn, now, hash_password, init_db, next_code_for, rows_to_dicts

app = Flask(__name__, static_folder=None)
CORS(app)

# --------------------------------------------------------------------------
# FIFO storage & history log
# --------------------------------------------------------------------------
TICKET_CAP = int(os.environ.get("LABCARE_TICKET_CAP", 2000))   # max tickets per type
HISTORY_LOG = os.environ.get("LABCARE_HISTORY_LOG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ticket_history.log"))


def _append_jsonl(path, rec):
    """Append one JSON line to the history log file (append-only, FIFO-safe)."""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass  # logging must never break the ticket operation


def _archive_and_purge(kind, keep_first=None, limit=TICKET_CAP, log_path=HISTORY_LOG):
    """First-in-first-out storage policy for tickets.

    * The newest ``keep_first`` (default ``limit``) tickets stay in the live DB.
    * Any older tickets beyond the cap are moved into the append-only history
      log file (with their comments + attachments + audit trail) and then
      removed from the database so the cap always holds.
    Returns the number of tickets archived in this run.
    """
    if limit is None or limit < 1:
        return 0
    table = kind + "s"          # complaints | breakdowns
    entity = kind               # complaint | breakdown
    c = conn()
    total = c.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]
    if total <= limit:
        c.close()
        return 0
    # anchor so we only evict truly-old rows (avoid loops on degenerate clocks)
    keep_count = min(max(keep_first or limit, 1), limit)
    rows = c.execute(
        f"SELECT * FROM {table} ORDER BY created_at ASC, id ASC LIMIT ?",
        (total - keep_count,),
    ).fetchall()
    archived = 0
    for r in rows:
        try:
            rec = {"entity_type": entity, "archived_at": now(), "ticket": dict(r)}
            rec["comments"] = rows_to_dicts(c.execute(
                "SELECT cm.*, u.name AS user_name FROM comments cm "
                "LEFT JOIN users u ON u.id = cm.user_id "
                "WHERE cm.entity_type=? AND cm.entity_id=? ORDER BY cm.created_at",
                (entity, r["id"])).fetchall())
            rec["attachments"] = rows_to_dicts(c.execute(
                "SELECT id, entity_type, entity_id, filename, mime, size, uploaded_by, created_at "
                "FROM attachments WHERE entity_type=? AND entity_id=? ORDER BY id",
                (entity, r["id"])).fetchall())
            rec["audit"] = rows_to_dicts(c.execute(
                "SELECT * FROM audit_logs WHERE entity_type=? AND entity_id=? ORDER BY id",
                (entity, r["id"])).fetchall())
            _append_jsonl(log_path, rec)
            archived += 1
        except Exception:
            continue  # a broken row must not block the rest of the purge
        # unlink dependent breakdowns' source-complaint FK only when evicting complaints
        if kind == "complaint":
            c.execute("UPDATE breakdowns SET complaint_id=NULL WHERE complaint_id=?", (r["id"],))
        c.execute("DELETE FROM comments WHERE entity_type=? AND entity_id=?", (entity, r["id"]))
        c.execute("DELETE FROM notifications WHERE entity_type=? AND entity_id=?", (entity, r["id"]))
        c.execute("DELETE FROM attachments WHERE entity_type=? AND entity_id=?", (entity, r["id"]))
        c.execute("DELETE FROM audit_logs WHERE entity_type=? AND entity_id=?", (entity, r["id"]))
        c.execute(f"DELETE FROM {table} WHERE id=?", (r["id"],))
    c.commit()
    c.close()
    return archived

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static")
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ROLE_LABELS = {"admin": "Admin", "technician": "Technician", "customer": "Customer"}

STATUS_LABELS = {
    "open": "Open", "in_progress": "In Progress", "resolved": "Resolved", "closed": "Closed",
    "reported": "Reported", "diagnosed": "Diagnosed", "on_hold": "On Hold",
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def auth_user():
    # Accept the token from any of several channels: some reverse proxies /
    # sandboxed iframes strip the Authorization header or drop cookies, so we
    # try, in order: Authorization header, X-Auth-Token header, cookie, and a
    # ?token= query parameter (handy for plain <a>/<img> requests).
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token:
        token = request.headers.get("X-Auth-Token", "").strip()
    if not token:
        token = request.cookies.get("labcare_token", "").strip()
    if not token:
        token = (request.args.get("token") or "").strip()
    if not token:
        return None
    c = conn()
    row = c.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
        (token,),
    ).fetchone()
    c.close()
    return dict(row) if row else None


def require_role(*roles):
    u = auth_user()
    if not u:
        return None, jsonify({"error": "Not authenticated"}), 401
    if roles and u["role"] not in roles and u["role"] != "admin":
        return None, jsonify({"error": "Not authorised"}), 403
    return u, None, None


def public_user(u):
    """Strip password hash before returning user object."""
    u = dict(u)
    u.pop("password_hash", None)
    return u


def complaint_payload(c, row):
    d = dict(row)
    cust = c.execute("SELECT name FROM customers WHERE id=?", (d["customer_id"],)).fetchone()
    eq = c.execute("SELECT name, model, serial_number FROM equipment WHERE id=?",
                   (d["equipment_id"],)).fetchone() if d.get("equipment_id") else None
    creator = c.execute("SELECT name FROM users WHERE id=?", (d["created_by"],)).fetchone()
    assignee = c.execute("SELECT name FROM users WHERE id=?", (d["assigned_to"],)).fetchone() if d.get("assigned_to") else None
    d["customer_name"] = cust["name"] if cust else None
    d["location_name"] = _name_of(c, "locations", d.get("location_id"))
    d["department_name"] = _name_of(c, "departments", d.get("department_id"))
    d["equipment_name"] = f"{eq['name']} — {eq['model']}" if eq else None
    d["created_by_name"] = creator["name"] if creator else None
    d["assigned_to_name"] = assignee["name"] if assignee else None
    # portal submissions record who actually reported the issue
    d["reporter_name"] = d.get("reporter_name") or ""
    d["reporter_phone"] = d.get("reporter_phone") or ""
    return d


def breakdown_payload(c, row):
    d = dict(row)
    cust = c.execute("SELECT name FROM customers WHERE id=?", (d["customer_id"],)).fetchone()
    eq = c.execute("SELECT name, model, serial_number FROM equipment WHERE id=?",
                   (d["equipment_id"],)).fetchone() if d.get("equipment_id") else None
    reporter = c.execute("SELECT name FROM users WHERE id=?", (d["reported_by"],)).fetchone()
    assignee = c.execute("SELECT name FROM users WHERE id=?", (d["assigned_to"],)).fetchone() if d.get("assigned_to") else None
    d["customer_name"] = cust["name"] if cust else None
    d["location_name"] = _name_of(c, "locations", d.get("location_id"))
    d["department_name"] = _name_of(c, "departments", d.get("department_id"))
    d["equipment_name"] = f"{eq['name']} — {eq['model']}" if eq else None
    d["reported_by_name"] = reporter["name"] if reporter else None
    d["assigned_to_name"] = assignee["name"] if assignee else None
    return d


def get_body():
    return request.get_json(force=True, silent=True) or {}


def is_tech_user(u):
    return u["role"] in ("technician", "admin")


def _name_of(c, table, row_id):
    if not row_id:
        return None
    row = c.execute(f"SELECT name FROM {table} WHERE id=?", (row_id,)).fetchone()
    return row["name"] if row else None


def equipment_scope(c, equipment_id):
    """Return (customer_id, location_id, department_id) for an equipment id."""
    if not equipment_id:
        return (None, None, None)
    row = c.execute("SELECT customer_id, location_id, department_id FROM equipment WHERE id=?",
                    (equipment_id,)).fetchone()
    if not row:
        return (None, None, None)
    return (row["customer_id"], row["location_id"], row["department_id"])


def _validate_loc_dept(c, customer_id, location_id, department_id):
    """Ensure a location_id (and department_id) belong to the given customer.
    Returns (None, None) on success or (error_json, code)."""
    if location_id:
        loc = c.execute("SELECT customer_id FROM locations WHERE id=?", (location_id,)).fetchone()
        if not loc or loc["customer_id"] != customer_id:
            return jsonify({"error": "Location does not belong to the selected customer"}), 400
    if department_id:
        dept = c.execute("SELECT customer_id, location_id FROM departments WHERE id=?", (department_id,)).fetchone()
        if not dept or dept["customer_id"] != customer_id:
            return jsonify({"error": "Department does not belong to the selected customer"}), 400
        if location_id and dept["location_id"] != location_id:
            return jsonify({"error": "Department is not within the selected location"}), 400
    return None, None


# --------------------------------------------------------------------------
# Notifications (in-app + email)
# --------------------------------------------------------------------------
def _recipient_of(user_id):
    """Return {'email':..., 'name':...} for a user id, or None."""
    c = conn()
    row = c.execute("SELECT name, email FROM users WHERE id=?", (user_id,)).fetchone()
    c.close()
    return {"email": row["email"], "name": row["name"]} if row else None


def notify(user_id, text, entity_type="", entity_id=None, email_fn=None):
    """Create an in-app notification and optionally queue an email."""
    if not user_id:
        return
    c = conn()
    c.execute(
        "INSERT INTO notifications (user_id,entity_type,entity_id,text,read,created_at) VALUES (?,?,?,?,0,?)",
        (user_id, entity_type, entity_id, text, now()),
    )
    c.execute(
        "INSERT INTO notification_pings (user_id,updated_at) VALUES (?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET updated_at=excluded.updated_at",
        (user_id, now()),
    )
    c.commit()
    c.close()
    if email_fn:
        recip = _recipient_of(user_id)
        if recip and recip.get("email"):
            try:
                email_fn(recip)
            except Exception:
                pass


def _stakeholder_ids(kind, rec, include_team=False):
    """User ids that should hear about an event on this ticket."""
    ids = set()
    if kind == "complaint":
        if rec.get("assigned_to"): ids.add(rec["assigned_to"])
        if rec.get("created_by"): ids.add(rec["created_by"])
    else:
        if rec.get("assigned_to"): ids.add(rec["assigned_to"])
        if rec.get("reported_by"): ids.add(rec["reported_by"])
    if include_team:
        c = conn()
        for r in c.execute("SELECT id FROM users WHERE role IN ('technician','admin') AND active=1").fetchall():
            ids.add(r["id"])
        c.close()
    return ids


def ping_team(kind, actor_id, rec, text, email_fn):
    for uid in _stakeholder_ids(kind, rec, include_team=True):
        if uid == actor_id:
            continue
        notify(uid, text, kind, rec["id"], email_fn)


def ping_followers(kind, actor_id, rec, text, email_fn):
    for uid in _stakeholder_ids(kind, rec, include_team=False):
        if uid == actor_id:
            continue
        notify(uid, text, kind, rec["id"], email_fn)


def audit(entity_type, entity_id, user, action, detail=""):
    """Record a who/what/when entry in a ticket's history log.
    Uses a separate connection so it never nests inside another transaction."""
    if not entity_id:
        return
    c = conn()
    c.execute(
        "INSERT INTO audit_logs (entity_type, entity_id, user_id, user_name, action, detail, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (entity_type, entity_id, (user or {}).get("id"), (user or {}).get("name", ""),
         action, (detail or "")[:300], now()),
    )
    c.commit()
    c.close()


# --------------------------------------------------------------------------
# Health check
# --------------------------------------------------------------------------
@app.get("/api/ping")
def ping():
    """Liveness probe for nginx / systemd / monitoring (no auth required)."""
    try:
        c = conn()
        c.execute("SELECT 1").fetchone()
        c.close()
        db = "ok"
    except Exception:
        db = "error"
    return jsonify({"ok": True, "db": db})


@app.get("/api/health")
def health():
    """Deeper readiness probe: DB + ticket-row sanity."""
    status = {"ok": True, "db": "ok"}
    try:
        c = conn()
        c.execute("SELECT 1").fetchone()
        c.close()
    except Exception:
        status.update(ok=False, db="error")
    return jsonify(status), (200 if status["ok"] else 503)


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
@app.post("/api/login")
def login():
    body = get_body()
    email = (body.get("email") or "").strip().lower()
    pw = body.get("password") or ""
    c = conn()
    row = c.execute("SELECT * FROM users WHERE lower(email)=?", (email,)).fetchone()
    if not row:
        pend = c.execute("SELECT id FROM onboarding_apps WHERE lower(email)=? AND status='pending'", (email,)).fetchone()
        if pend:
            c.close()
            return jsonify({"error": "Your account is awaiting approval by an administrator"}), 403
        c.close()
        return jsonify({"error": "Invalid email or password"}), 401
    if row["password_hash"] != hash_password(pw):
        c.close()
        return jsonify({"error": "Invalid email or password"}), 401
    if row["pending"]:
        c.close()
        return jsonify({"error": "Your account is awaiting approval by an administrator"}), 403
    if not row["active"]:
        c.close()
        return jsonify({"error": "Account is disabled"}), 403
    token = uuid.uuid4().hex
    c.execute("INSERT INTO sessions (token,user_id,created_at) VALUES (?,?,?)", (token, row["id"], now()))
    c.commit()
    c.close()
    resp = make_response(jsonify({"token": token, "user": public_user(row)}))
    resp.set_cookie(
        "labcare_token", token,
        max_age=30 * 24 * 3600,  # 30 days
        httponly=True,
        samesite="Lax",
        # Behind an HTTPS reverse proxy set LABCARE_SECURE_COOKIES=1 so the
        # browser only ever sends the session cookie over TLS.
        secure=os.environ.get("LABCARE_SECURE_COOKIES") == "1",
    )
    return resp


@app.post("/api/logout")
def logout():
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip() or \
        request.cookies.get("labcare_token", "")
    c = conn()
    c.execute("DELETE FROM sessions WHERE token=?", (token,))
    c.commit()
    c.close()
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("labcare_token")
    return resp


@app.get("/api/me")
def me():
    u, err, code = require_role()
    if err:
        return err, code
    d = public_user(u)
    c = conn()
    d["customer_name"] = _name_of(c, "customers", u.get("customer_id"))
    d["location_name"] = _name_of(c, "locations", u.get("location_id"))
    d["department_name"] = _name_of(c, "departments", u.get("department_id"))
    c.close()
    return jsonify(d)


# --------------------------------------------------------------------------
# Onboarding — self-service sign-up, pre-approved by admin
# --------------------------------------------------------------------------
@app.get("/api/lookup/customers")
def lookup_customers():
    """Public helper for the sign-up form: id + name of every customer org."""
    c = conn()
    rows = c.execute("SELECT id, name FROM customers ORDER BY name").fetchall()
    c.close()
    return jsonify(rows_to_dicts(rows))


@app.get("/api/lookup/options")
def lookup_options():
    """Public helper for the sign-up form: customers, locations, departments."""
    c = conn()
    customers = rows_to_dicts(c.execute("SELECT id, name FROM customers ORDER BY name").fetchall())
    locations = rows_to_dicts(c.execute("SELECT id, customer_id, name FROM locations ORDER BY name").fetchall())
    departments = rows_to_dicts(c.execute("SELECT id, customer_id, location_id, name FROM departments ORDER BY name").fetchall())
    c.close()
    return jsonify({"customers": customers, "locations": locations, "departments": departments})


@app.post("/api/signup")
def signup():
    """Anyone can request a customer/technician account; it stays pending until an admin approves."""
    b = get_body()
    name = (b.get("name") or "").strip()
    email = (b.get("email") or "").strip().lower()
    pw = b.get("password") or ""
    role = b.get("role") or "customer"
    if not name or not email or not pw:
        return jsonify({"error": "Name, email and password are required"}), 400
    if len(pw) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    if role not in ("technician", "customer"):
        return jsonify({"error": "Invalid role"}), 400
    c = conn()
    if c.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone():
        c.close()
        return jsonify({"error": "An account with this email already exists"}), 409
    if c.execute("SELECT id FROM onboarding_apps WHERE lower(email)=? AND status='pending'", (email,)).fetchone():
        c.close()
        return jsonify({"error": "A request for this email is already awaiting approval"}), 409
    customer_id = b.get("customer_id") or None
    location_id = b.get("location_id") or None
    department_id = b.get("department_id") or None
    if role == "customer":
        if not customer_id or not location_id or not department_id:
            c.close()
            return jsonify({"error": "Select your organisation, location and department"}), 400
        err_r, code_r = _validate_loc_dept(c, customer_id, location_id, department_id)
        if err_r:
            c.close()
            return err_r, code_r
    else:
        customer_id = location_id = department_id = None
    cur = c.execute(
        "INSERT INTO onboarding_apps (name,email,phone,password_hash,role,customer_id,location_id,department_id,status,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (name, email, b.get("phone", ""), hash_password(pw), role, customer_id, location_id, department_id, "pending", now()),
    )
    c.commit()
    # Notify all admins that a new joiner is waiting for approval.
    for r in c.execute("SELECT id FROM users WHERE role='admin' AND active=1").fetchall():
        notify(r["id"], f"New join request from {name} ({email}) is awaiting your approval.",
               "onboarding", cur.lastrowid, None)
    c.close()
    return jsonify({"ok": True, "message": "Request submitted. An admin must approve it before you can sign in."}), 201


@app.get("/api/onboarding")
def list_onboarding():
    u, err, code = require_role("admin")
    if err:
        return err, code
    c = conn()
    rows = c.execute(
        "SELECT a.*, cu.name AS customer_name, l.name AS location_name, d.name AS department_name "
        "FROM onboarding_apps a "
        "LEFT JOIN customers cu ON cu.id=a.customer_id "
        "LEFT JOIN locations l ON l.id=a.location_id "
        "LEFT JOIN departments d ON d.id=a.department_id "
        "ORDER BY (a.status='pending') DESC, a.created_at DESC").fetchall()
    c.close()
    return jsonify(rows_to_dicts(rows))


@app.post("/api/onboarding/<int:aid>/review")
def review_onboarding(aid):
    u, err, code = require_role("admin")
    if err:
        return err, code
    b = get_body()
    decision = b.get("decision")
    if decision not in ("approve", "reject"):
        return jsonify({"error": "decision must be 'approve' or 'reject'"}), 400
    c = conn()
    app = c.execute("SELECT * FROM onboarding_apps WHERE id=?", (aid,)).fetchone()
    if not app:
        c.close()
        return jsonify({"error": "Not found"}), 404
    if app["status"] != "pending":
        c.close()
        return jsonify({"error": "This request has already been reviewed"}), 409
    if decision == "approve":
        if c.execute("SELECT id FROM users WHERE lower(email)=?", (app["email"],)).fetchone():
            c.close()
            return jsonify({"error": "An account with this email already exists"}), 409
        c.execute(
            "INSERT INTO users (name,email,phone,password_hash,role,customer_id,location_id,department_id,active,pending,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,1,0,?)",
            (app["name"], app["email"], app["phone"], app["password_hash"],
             app["role"], app["customer_id"], app["location_id"], app["department_id"], now()),
        )
        c.execute("UPDATE onboarding_apps SET status='approved', reviewed_by=?, reviewed_at=? WHERE id=?",
                  (u["id"], now(), aid))
    else:
        c.execute("UPDATE onboarding_apps SET status='rejected', reviewed_by=?, reviewed_at=? WHERE id=?",
                  (u["id"], now(), aid))
    c.commit()
    c.close()
    return jsonify({"ok": True, "decision": decision})


# --------------------------------------------------------------------------
# Customers
# --------------------------------------------------------------------------
@app.get("/api/customers")
def list_customers():
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    c = conn()
    where, params = "", []
    if u["role"] == "customer":
        # customers only see their own organisation
        where, params = " WHERE id=?", [u.get("customer_id")]
    rows = c.execute("SELECT * FROM customers" + where + " ORDER BY name", params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["equipment_count"] = c.execute("SELECT COUNT(*) n FROM equipment WHERE customer_id=?", (r["id"],)).fetchone()["n"]
        d["location_count"] = c.execute("SELECT COUNT(*) n FROM locations WHERE customer_id=?", (r["id"],)).fetchone()["n"]
        d["open_complaints"] = c.execute(
            "SELECT COUNT(*) n FROM complaints WHERE customer_id=? AND status IN ('open','in_progress')", (r["id"],)).fetchone()["n"]
        d["open_breakdowns"] = c.execute(
            "SELECT COUNT(*) n FROM breakdowns WHERE customer_id=? AND status NOT IN ('resolved')", (r["id"],)).fetchone()["n"]
        out.append(d)
    c.close()
    return jsonify(out)


@app.post("/api/customers")
def create_customer():
    u, err, code = require_role("admin")
    if err:
        return err, code
    b = get_body()
    if not (b.get("name") or "").strip():
        return jsonify({"error": "Customer name is required"}), 400
    c = conn()
    cur = c.execute(
        "INSERT INTO customers (name,contact_name,email,phone,address,city,created_at) VALUES (?,?,?,?,?,?,?)",
        (b["name"].strip(), b.get("contact_name", ""), b.get("email", ""), b.get("phone", ""),
         b.get("address", ""), b.get("city", ""), now()),
    )
    c.commit()
    row = c.execute("SELECT * FROM customers WHERE id=?", (cur.lastrowid,)).fetchone()
    c.close()
    return jsonify(dict(row)), 201


@app.put("/api/customers/<int:cid>")
def update_customer(cid):
    u, err, code = require_role("admin")
    if err:
        return err, code
    b = get_body()
    c = conn()
    c.execute(
        "UPDATE customers SET name=?,contact_name=?,email=?,phone=?,address=?,city=? WHERE id=?",
        (b.get("name", ""), b.get("contact_name", ""), b.get("email", ""), b.get("phone", ""),
         b.get("address", ""), b.get("city", ""), cid),
    )
    c.commit()
    row = c.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
    c.close()
    return jsonify(dict(row)) if row else (jsonify({"error": "Not found"}), 404)


@app.delete("/api/customers/<int:cid>")
def delete_customer(cid):
    u, err, code = require_role("admin")
    if err:
        return err, code
    c = conn()
    n_equip = c.execute("SELECT COUNT(*) n FROM equipment WHERE customer_id=?", (cid,)).fetchone()["n"]
    n_cmp = c.execute("SELECT COUNT(*) n FROM complaints WHERE customer_id=?", (cid,)).fetchone()["n"]
    n_loc = c.execute("SELECT COUNT(*) n FROM locations WHERE customer_id=?", (cid,)).fetchone()["n"]
    if n_equip or n_cmp or n_loc:
        c.close()
        return jsonify({"error": "Customer has linked locations, equipment or complaints; cannot delete."}), 409
    c.execute("DELETE FROM customers WHERE id=?", (cid,))
    c.commit()
    c.close()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Locations & departments
# --------------------------------------------------------------------------
def _loc_payload(c, r):
    d = dict(r)
    d["department_count"] = c.execute("SELECT COUNT(*) n FROM departments WHERE location_id=?",
                                      (r["id"],)).fetchone()["n"]
    d["equipment_count"] = c.execute("SELECT COUNT(*) n FROM equipment WHERE location_id=?",
                                     (r["id"],)).fetchone()["n"]
    return d


def _dept_payload(c, r):
    d = dict(r)
    d["equipment_count"] = c.execute("SELECT COUNT(*) n FROM equipment WHERE department_id=?",
                                     (r["id"],)).fetchone()["n"]
    return d


@app.get("/api/locations")
def list_locations():
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    c = conn()
    where, params = [], []
    if u["role"] == "customer":
        where.append("l.customer_id=?")
        params.append(u.get("customer_id"))
    else:
        cust = request.args.get("customer_id")
        if cust:
            where.append("l.customer_id=?")
            params.append(cust)
    q = ("SELECT l.*, cu.name AS customer_name FROM locations l JOIN customers cu ON cu.id=l.customer_id")
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY l.customer_id, l.name"
    rows = c.execute(q, params).fetchall()
    out = [_loc_payload(c, r) for r in rows]
    c.close()
    return jsonify(out)


@app.post("/api/locations")
def create_location():
    u, err, code = require_role("admin", "technician")
    if err:
        return err, code
    b = get_body()
    if not (b.get("name") or "").strip() or not b.get("customer_id"):
        return jsonify({"error": "Location name and customer are required"}), 400
    c = conn()
    cur = c.execute(
        "INSERT INTO locations (customer_id,name,address,city,created_at) VALUES (?,?,?,?,?)",
        (b["customer_id"], b["name"].strip(), b.get("address", ""), b.get("city", ""), now()),
    )
    c.commit()
    row = c.execute("SELECT l.*, cu.name AS customer_name FROM locations l JOIN customers cu ON cu.id=l.customer_id WHERE l.id=?",
                    (cur.lastrowid,)).fetchone()
    out = _loc_payload(c, row)
    c.close()
    return jsonify(out), 201


@app.put("/api/locations/<int:lid>")
def update_location(lid):
    u, err, code = require_role("admin", "technician")
    if err:
        return err, code
    b = get_body()
    c = conn()
    c.execute(
        "UPDATE locations SET name=?,address=?,city=?,customer_id=? WHERE id=?",
        (b.get("name", ""), b.get("address", ""), b.get("city", ""), b.get("customer_id"), lid),
    )
    c.commit()
    row = c.execute("SELECT l.*, cu.name AS customer_name FROM locations l JOIN customers cu ON cu.id=l.customer_id WHERE l.id=?",
                    (lid,)).fetchone()
    c.close()
    return jsonify(dict(row)) if row else (jsonify({"error": "Not found"}), 404)


@app.delete("/api/locations/<int:lid>")
def delete_location(lid):
    u, err, code = require_role("admin")
    if err:
        return err, code
    c = conn()
    n = c.execute("SELECT COUNT(*) n FROM departments WHERE location_id=?", (lid,)).fetchone()["n"]
    n2 = c.execute("SELECT COUNT(*) n FROM equipment WHERE location_id=?", (lid,)).fetchone()["n"]
    if n or n2:
        c.close()
        return jsonify({"error": "Location has departments or equipment; cannot delete."}), 409
    c.execute("DELETE FROM locations WHERE id=?", (lid,))
    c.commit()
    c.close()
    return jsonify({"ok": True})


@app.get("/api/departments")
def list_departments():
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    c = conn()
    where, params = [], []
    if u["role"] == "customer":
        where.append("d.customer_id=?")
        params.append(u.get("customer_id"))
    else:
        for f in ("customer_id", "location_id"):
            v = request.args.get(f)
            if v:
                where.append(f"d.{f}=?")
                params.append(v)
    q = ("SELECT d.*, cu.name AS customer_name, l.name AS location_name "
         "FROM departments d JOIN customers cu ON cu.id=d.customer_id JOIN locations l ON l.id=d.location_id")
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY d.customer_id, d.location_id, d.name"
    rows = c.execute(q, params).fetchall()
    out = [_dept_payload(c, r) for r in rows]
    c.close()
    return jsonify(out)


@app.post("/api/departments")
def create_department():
    u, err, code = require_role("admin", "technician")
    if err:
        return err, code
    b = get_body()
    if not (b.get("name") or "").strip() or not b.get("location_id"):
        return jsonify({"error": "Department name and location are required"}), 400
    c = conn()
    loc = c.execute("SELECT * FROM locations WHERE id=?", (b["location_id"],)).fetchone()
    if not loc:
        c.close()
        return jsonify({"error": "Location not found"}), 404
    cur = c.execute(
        "INSERT INTO departments (customer_id,location_id,name,created_at) VALUES (?,?,?,?)",
        (loc["customer_id"], b["location_id"], b["name"].strip(), now()),
    )
    c.commit()
    row = c.execute(
        "SELECT d.*, cu.name AS customer_name, l.name AS location_name FROM departments d "
        "JOIN customers cu ON cu.id=d.customer_id JOIN locations l ON l.id=d.location_id WHERE d.id=?",
        (cur.lastrowid,)).fetchone()
    c.close()
    return jsonify(dict(row)), 201


@app.put("/api/departments/<int:did>")
def update_department(did):
    u, err, code = require_role("admin", "technician")
    if err:
        return err, code
    b = get_body()
    c = conn()
    c.execute("UPDATE departments SET name=?,location_id=? WHERE id=?",
              (b.get("name", ""), b.get("location_id"), did))
    c.commit()
    row = c.execute(
        "SELECT d.*, cu.name AS customer_name, l.name AS location_name FROM departments d "
        "JOIN customers cu ON cu.id=d.customer_id JOIN locations l ON l.id=d.location_id WHERE d.id=?",
        (did,)).fetchone()
    c.close()
    return jsonify(dict(row)) if row else (jsonify({"error": "Not found"}), 404)


@app.delete("/api/departments/<int:did>")
def delete_department(did):
    u, err, code = require_role("admin")
    if err:
        return err, code
    c = conn()
    n = c.execute("SELECT COUNT(*) n FROM equipment WHERE department_id=?", (did,)).fetchone()["n"]
    if n:
        c.close()
        return jsonify({"error": "Department has equipment; cannot delete."}), 409
    c.execute("DELETE FROM departments WHERE id=?", (did,))
    c.commit()
    c.close()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Equipment categories (admin-managed)
# --------------------------------------------------------------------------
@app.get("/api/categories")
def list_categories():
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    c = conn()
    rows = c.execute("SELECT * FROM categories ORDER BY name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["equipment_count"] = c.execute(
            "SELECT COUNT(*) n FROM equipment WHERE category=?", (r["name"],)).fetchone()["n"]
        out.append(d)
    c.close()
    return jsonify(out)


@app.post("/api/categories")
def create_category():
    u, err, code = require_role("admin", "technician")
    if err:
        return err, code
    b = get_body()
    name = (b.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Category name is required"}), 400
    c = conn()
    if c.execute("SELECT id FROM categories WHERE lower(name)=lower(?)", (name,)).fetchone():
        c.close()
        return jsonify({"error": "Category already exists"}), 409
    cur = c.execute("INSERT INTO categories (name, created_at) VALUES (?,?)", (name, now()))
    c.commit()
    row = c.execute("SELECT * FROM categories WHERE id=?", (cur.lastrowid,)).fetchone()
    c.close()
    return jsonify(dict(row)), 201


@app.put("/api/categories/<int:catid>")
def rename_category(catid):
    u, err, code = require_role("admin")
    if err:
        return err, code
    b = get_body()
    new_name = (b.get("name") or "").strip()
    if not new_name:
        return jsonify({"error": "Category name is required"}), 400
    c = conn()
    row = c.execute("SELECT * FROM categories WHERE id=?", (catid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    if c.execute("SELECT id FROM categories WHERE lower(name)=lower(?) AND id!=?", (new_name, catid)).fetchone():
        c.close()
        return jsonify({"error": "Category already exists"}), 409
    # keep existing equipment pointing at the new name
    c.execute("UPDATE equipment SET category=? WHERE category=?", (new_name, row["name"]))
    c.execute("UPDATE categories SET name=? WHERE id=?", (new_name, catid))
    c.commit()
    out = c.execute("SELECT * FROM categories WHERE id=?", (catid,)).fetchone()
    c.close()
    return jsonify(dict(out))


@app.delete("/api/categories/<int:catid>")
def delete_category(catid):
    u, err, code = require_role("admin")
    if err:
        return err, code
    c = conn()
    row = c.execute("SELECT * FROM categories WHERE id=?", (catid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    if row["name"] == "Other":
        c.close()
        return jsonify({"error": "The 'Other' category cannot be deleted"}), 409
    # equipment in this category falls back to "Other"
    c.execute("UPDATE equipment SET category='Other' WHERE category=?", (row["name"],))
    c.execute("DELETE FROM categories WHERE id=?", (catid,))
    c.commit()
    c.close()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Equipment
# --------------------------------------------------------------------------
@app.get("/api/equipment")
def list_equipment():
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    c = conn()
    q = ("SELECT e.*, cu.name AS customer_name, l.name AS location_name, d.name AS department_name "
         "FROM equipment e JOIN customers cu ON cu.id=e.customer_id "
         "LEFT JOIN locations l ON l.id=e.location_id "
         "LEFT JOIN departments d ON d.id=e.department_id")
    where, params = [], []
    if u["role"] == "customer":
        if u.get("customer_id"):
            where.append("e.customer_id=?")
            params.append(u["customer_id"])
        if u.get("location_id"):
            where.append("e.location_id=?")
            params.append(u["location_id"])
        if u.get("department_id"):
            where.append("e.department_id=?")
            params.append(u["department_id"])
    else:
        for f in ("customer_id", "location_id", "department_id"):
            v = request.args.get(f)
            if v:
                where.append(f"e.{f}=?")
                params.append(v)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY e.name"
    rows = c.execute(q, params).fetchall()
    out = rows_to_dicts(rows)
    c.close()
    return jsonify(out)


@app.post("/api/equipment")
def create_equipment():
    u, err, code = require_role("admin", "technician")
    if err:
        return err, code
    b = get_body()
    if not (b.get("name") or "").strip() or not b.get("customer_id"):
        return jsonify({"error": "Equipment name and customer are required"}), 400
    c = conn()
    err_r, code_r = _validate_loc_dept(c, b["customer_id"], b.get("location_id"), b.get("department_id"))
    if err_r:
        c.close()
        return err_r, code_r
    serial = (b.get("serial_number") or "").strip()
    if serial:
        dup = c.execute("SELECT id FROM equipment WHERE customer_id=? AND serial_number=?",
                        (b["customer_id"], serial)).fetchone()
        if dup:
            c.close()
            return jsonify({"error": f"Serial number '{serial}' is already registered for this customer"}), 409
    cur = c.execute(
        "INSERT INTO equipment (customer_id,location_id,department_id,name,model,serial_number,category,installed_date,warranty_expiry,status,notes,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (b["customer_id"], b.get("location_id"), b.get("department_id"),
         b["name"].strip(), b.get("model", ""), serial, b.get("category", ""),
         b.get("installed_date", ""), b.get("warranty_expiry", ""), b.get("status", "active"),
         b.get("notes", ""), now()),
    )
    c.commit()
    row = c.execute(
        "SELECT e.*, cu.name AS customer_name, l.name AS location_name, d.name AS department_name "
        "FROM equipment e JOIN customers cu ON cu.id=e.customer_id "
        "LEFT JOIN locations l ON l.id=e.location_id LEFT JOIN departments d ON d.id=e.department_id WHERE e.id=?",
        (cur.lastrowid,)).fetchone()
    c.close()
    return jsonify(dict(row)), 201


@app.put("/api/equipment/<int:eid>")
def update_equipment(eid):
    u, err, code = require_role("admin", "technician")
    if err:
        return err, code
    b = get_body()
    c = conn()
    existing = c.execute("SELECT * FROM equipment WHERE id=?", (eid,)).fetchone()
    if not existing:
        c.close()
        return jsonify({"error": "Not found"}), 404
    customer_id = b.get("customer_id", existing["customer_id"])
    err_r, code_r = _validate_loc_dept(c, customer_id, b.get("location_id"), b.get("department_id"))
    if err_r:
        c.close()
        return err_r, code_r
    serial = (b.get("serial_number") or "").strip()
    if serial:
        dup = c.execute("SELECT id FROM equipment WHERE customer_id=? AND serial_number=? AND id!=?",
                        (customer_id, serial, eid)).fetchone()
        if dup:
            c.close()
            return jsonify({"error": f"Serial number '{serial}' is already registered for this customer"}), 409
    c.execute(
        "UPDATE equipment SET customer_id=?,location_id=?,department_id=?,name=?,model=?,serial_number=?,category=?,installed_date=?,warranty_expiry=?,status=?,notes=? WHERE id=?",
        (customer_id, b.get("location_id"), b.get("department_id"),
         b.get("name", ""), b.get("model", ""), serial, b.get("category", ""),
         b.get("installed_date", ""), b.get("warranty_expiry", ""), b.get("status", "active"),
         b.get("notes", ""), eid),
    )
    c.commit()
    row = c.execute(
        "SELECT e.*, cu.name AS customer_name, l.name AS location_name, d.name AS department_name "
        "FROM equipment e JOIN customers cu ON cu.id=e.customer_id "
        "LEFT JOIN locations l ON l.id=e.location_id LEFT JOIN departments d ON d.id=e.department_id WHERE e.id=?",
        (eid,)).fetchone()
    c.close()
    return jsonify(dict(row)) if row else (jsonify({"error": "Not found"}), 404)


@app.delete("/api/equipment/<int:eid>")
def delete_equipment(eid):
    u, err, code = require_role("admin", "technician")
    if err:
        return err, code
    c = conn()
    n = c.execute("SELECT COUNT(*) n FROM complaints WHERE equipment_id=?", (eid,)).fetchone()["n"]
    if n:
        c.close()
        return jsonify({"error": "Equipment is referenced by complaints; cannot delete."}), 409
    c.execute("DELETE FROM equipment WHERE id=?", (eid,))
    c.commit()
    c.close()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Complaints
# --------------------------------------------------------------------------
@app.get("/api/complaints")
def list_complaints():
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    c = conn()
    where, params = [], []
    if u["role"] == "customer":
        if u.get("customer_id"):
            where.append("cmp.customer_id=?")
            params.append(u["customer_id"])
        if u.get("location_id"):
            where.append("cmp.location_id=?")
            params.append(u["location_id"])
        if u.get("department_id"):
            where.append("cmp.department_id=?")
            params.append(u["department_id"])
    for f in ("status", "priority", "customer_id", "assigned_to", "equipment_id", "location_id", "department_id"):
        v = request.args.get(f)
        if v:
            where.append(f"cmp.{f}=?")
            params.append(v)
    q = "SELECT cmp.* FROM complaints cmp"
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY CASE cmp.status WHEN 'open' THEN 1 WHEN 'in_progress' THEN 2 WHEN 'resolved' THEN 3 ELSE 4 END, "
    q += "CASE cmp.priority WHEN 'critical' THEN 1 WHEN 'high' THEN 2 WHEN 'medium' THEN 3 ELSE 4 END, cmp.created_at DESC"
    rows = c.execute(q, params).fetchall()
    out = [complaint_payload(c, r) for r in rows]
    c.close()
    return jsonify(out)


@app.post("/api/complaints")
def create_complaint():
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    b = get_body()
    if not (b.get("subject") or "").strip():
        return jsonify({"error": "Subject is required"}), 400
    c = conn()
    equipment_id = b.get("equipment_id") or None
    eq_cust, eq_loc, eq_dept = equipment_scope(c, equipment_id)

    if u["role"] == "customer":
        # customer complaints are always bound to their own customer/location/dept
        customer_id = u.get("customer_id")
        location_id = u.get("location_id")
        department_id = u.get("department_id")
        if equipment_id and eq_cust is not None and eq_cust != customer_id:
            c.close()
            return jsonify({"error": "Equipment does not belong to your organization"}), 403
        if equipment_id and eq_loc and location_id and eq_loc != location_id:
            c.close()
            return jsonify({"error": "Equipment is not in your location"}), 403
        if equipment_id and eq_dept and department_id and eq_dept != department_id:
            c.close()
            return jsonify({"error": "Equipment is not in your department"}), 403
    else:
        customer_id = b.get("customer_id")
        # default ticket location/dept from the chosen equipment when not provided
        location_id = b.get("location_id") or eq_loc
        department_id = b.get("department_id") or eq_dept

    if not customer_id:
        c.close()
        return jsonify({"error": "Customer is required"}), 400
    code_ = next_code_for("complaints", "CMP")
    cur = c.execute(
        "INSERT INTO complaints (code,customer_id,equipment_id,location_id,department_id,subject,description,category,priority,status,created_by,assigned_to,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code_, customer_id, equipment_id, location_id, department_id,
         b["subject"].strip(), b.get("description", ""), b.get("category", "General"),
         b.get("priority", "medium"), "open", u["id"], b.get("assigned_to") or None, now(), now()),
    )
    c.commit()
    new_id = cur.lastrowid
    row = c.execute("SELECT * FROM complaints WHERE id=?", (new_id,)).fetchone()
    out = complaint_payload(c, row)
    c.close()
    audit("complaint", new_id, u, "created", f"Opened {out['code']} — {out['subject']}")
    _archive_and_purge("complaint")
    # notify team
    ping_team("complaint", u["id"], out,
              f"New complaint {out['code']} by {out.get('created_by_name')}: {out['subject']}",
              lambda r: email_mod.email_complaint_created(r, out))
    return jsonify(out), 201


def _customer_allowed(u, customer_id, location_id=None, department_id=None):
    """True if a customer-role user may access a record with the given scope.
    Admins/technicians are always allowed."""
    if u["role"] != "customer":
        return True
    if u.get("customer_id") and customer_id != u.get("customer_id"):
        return False
    if u.get("location_id") and location_id is not None and location_id != u.get("location_id"):
        return False
    if u.get("department_id") and department_id is not None and department_id != u.get("department_id"):
        return False
    return True


@app.get("/api/complaints/<int:cid>")
def get_complaint(cid):
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    c = conn()
    row = c.execute("SELECT * FROM complaints WHERE id=?", (cid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    if not _customer_allowed(u, row["customer_id"], row["location_id"], row["department_id"]):
        c.close()
        return jsonify({"error": "Not authorised"}), 403
    out = complaint_payload(c, row)
    comments = c.execute(
        "SELECT cm.*, u.name AS user_name FROM comments cm JOIN users u ON u.id=cm.user_id "
        "WHERE cm.entity_type='complaint' AND cm.entity_id=? ORDER BY cm.created_at", (cid,)).fetchall()
    out["comments"] = rows_to_dicts(comments)
    c.close()
    return jsonify(out)


@app.patch("/api/complaints/<int:cid>")
def update_complaint(cid):
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    b = get_body()
    c = conn()
    row = c.execute("SELECT * FROM complaints WHERE id=?", (cid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    if not _customer_allowed(u, row["customer_id"], row["location_id"], row["department_id"]):
        c.close()
        return jsonify({"error": "Not authorised"}), 403
    old_row = dict(row)

    fields = []
    params = []
    # a customer may only change their ticket's descriptive fields and priority,
    # and may only close it — never its customer/location/department/equipment
    # scope or assignment.
    if u["role"] == "customer":
        for f in ("subject", "description", "category"):
            if f in b:
                fields.append(f"{f}=?")
                params.append(b[f])
        if "equipment_id" in b and b["equipment_id"] != row["equipment_id"]:
            c.close()
            return jsonify({"error": "You cannot move this complaint to a different equipment"}), 403
        for f in ("location_id", "department_id", "customer_id", "assigned_to"):
            if f in b and b[f] != (row[f] if row[f] is not None else None):
                c.close()
                return jsonify({"error": "You cannot change the scope or assignment of this complaint"}), 403
        if "priority" in b:
            fields.append("priority=?")
            params.append(b["priority"])
        if "status" in b and b["status"] in ("closed",):
            fields.append("status=?")
            params.append(b["status"])
    else:
        for f in ("subject", "description", "category", "customer_id", "equipment_id", "assigned_to",
                  "location_id", "department_id"):
            if f in b:
                fields.append(f"{f}=?")
                params.append(b[f])
        if "priority" in b:
            fields.append("priority=?")
            params.append(b["priority"])
        if "status" in b:
            fields.append("status=?")
            params.append(b["status"])
            if b["status"] in ("resolved", "closed"):
                fields.append("resolved_at=?")
                params.append(now())
            else:
                fields.append("resolved_at=?")
                params.append(None)
    old_assignee = row["assigned_to"]
    old_status = row["status"]
    # Auto-assign: a technician/admin who starts working an unassigned ticket
    # (e.g. moves it out of "open") becomes its assignee — the assignee always
    # names the person who actually responded.
    if ("status" in b and "assigned_to" not in b
            and u["role"] in ("admin", "technician") and not row["assigned_to"]):
        fields.append("assigned_to=?")
        params.append(u["id"])
        old_assignee = None
    if fields:
        fields.append("updated_at=?")
        params.append(now())
        params.append(cid)
        c.execute(f"UPDATE complaints SET {', '.join(fields)} WHERE id=?", params)
        c.commit()
    row = c.execute("SELECT * FROM complaints WHERE id=?", (cid,)).fetchone()
    out = complaint_payload(c, row)
    c.close()

    # audit trail — log every meaningful change with who/when
    if "assigned_to" in b:
        if b["assigned_to"] and b["assigned_to"] != old_assignee:
            audit("complaint", cid, u, "assigned", f"→ {out.get('assigned_to_name') or '—'}")
        elif not b["assigned_to"] and old_assignee:
            audit("complaint", cid, u, "assigned", "Removed assignee")
    elif old_assignee is None and row["assigned_to"] == u["id"]:
        audit("complaint", cid, u, "assigned", f"Auto-assigned {u['name']} (responded)")
    if b.get("status") and b["status"] != old_status:
        audit("complaint", cid, u, "status",
              f"{STATUS_LABELS.get(old_status, old_status)} → {STATUS_LABELS.get(b['status'], b['status'])}")
    for label, key in (("Subject", "subject"), ("Description", "description"),
                       ("Category", "category"), ("Priority", "priority")):
        if key in b and (old_row.get(key) or "") != (b[key] or ""):
            audit("complaint", cid, u, "updated", f"{label} changed")
    # equipment / scope moves (admin/tech only)
    for label, key in (("Customer", "customer_id"), ("Equipment", "equipment_id"),
                       ("Location", "location_id"), ("Department", "department_id")):
        if key in b and (old_row.get(key) or None) != (b[key] or None):
            audit("complaint", cid, u, "updated", f"{label} changed")

    # notifications
    if "assigned_to" in b and b["assigned_to"] and b["assigned_to"] != old_assignee:
        notify(b["assigned_to"], f"You were assigned complaint {out['code']}: {out['subject']}",
               "complaint", cid, lambda r: email_mod.email_assigned("complaint", r, out, u["name"]))
    if "status" in b and b["status"] != old_status:
        label = STATUS_LABELS.get(b["status"], b["status"])
        ping_followers("complaint", u["id"], out,
                       f"Complaint {out['code']} is now {label}: {out['subject']}",
                       lambda r: email_mod.email_status_changed("complaint", r, out, b["status"]))
    return jsonify(out)


@app.delete("/api/complaints/<int:cid>")
def delete_complaint(cid):
    u, err, code = require_role("admin")
    if err:
        return err, code
    c = conn()
    row = c.execute("SELECT * FROM complaints WHERE id=?", (cid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    # nested breakdowns lose their source-complaint link, not themselves
    c.execute("UPDATE breakdowns SET complaint_id=NULL WHERE complaint_id=?", (cid,))
    c.execute("DELETE FROM comments WHERE entity_type='complaint' AND entity_id=?", (cid,))
    c.execute("DELETE FROM notifications WHERE entity_type='complaint' AND entity_id=?", (cid,))
    c.execute("DELETE FROM attachments WHERE entity_type='complaint' AND entity_id=?", (cid,))
    c.execute("DELETE FROM audit_logs WHERE entity_type='complaint' AND entity_id=?", (cid,))
    c.execute("DELETE FROM complaints WHERE id=?", (cid,))
    c.commit()
    c.close()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Breakdowns
# --------------------------------------------------------------------------
@app.get("/api/breakdowns")
def list_breakdowns():
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    c = conn()
    where, params = [], []
    if u["role"] == "customer":
        if u.get("customer_id"):
            where.append("brk.customer_id=?")
            params.append(u["customer_id"])
        if u.get("location_id"):
            where.append("brk.location_id=?")
            params.append(u["location_id"])
        if u.get("department_id"):
            where.append("brk.department_id=?")
            params.append(u["department_id"])
    for f in ("status", "priority", "customer_id", "assigned_to", "equipment_id", "location_id", "department_id"):
        v = request.args.get(f)
        if v:
            where.append(f"brk.{f}=?")
            params.append(v)
    q = "SELECT brk.* FROM breakdowns brk"
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY CASE brk.status WHEN 'reported' THEN 1 WHEN 'diagnosed' THEN 2 WHEN 'in_progress' THEN 3 WHEN 'on_hold' THEN 4 ELSE 5 END, "
    q += "CASE brk.priority WHEN 'critical' THEN 1 WHEN 'high' THEN 2 WHEN 'medium' THEN 3 ELSE 4 END, brk.created_at DESC"
    rows = c.execute(q, params).fetchall()
    out = [breakdown_payload(c, r) for r in rows]
    c.close()
    return jsonify(out)


@app.post("/api/breakdowns")
def create_breakdown():
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    b = get_body()
    if not (b.get("fault_description") or "").strip():
        return jsonify({"error": "Fault description is required"}), 400
    c = conn()
    equipment_id = b.get("equipment_id") or None
    eq_cust, eq_loc, eq_dept = equipment_scope(c, equipment_id)

    if u["role"] == "customer":
        customer_id = u.get("customer_id")
        location_id = u.get("location_id")
        department_id = u.get("department_id")
        if equipment_id and eq_cust is not None and eq_cust != customer_id:
            c.close()
            return jsonify({"error": "Equipment does not belong to your organization"}), 403
        if equipment_id and eq_loc and location_id and eq_loc != location_id:
            c.close()
            return jsonify({"error": "Equipment is not in your location"}), 403
        if equipment_id and eq_dept and department_id and eq_dept != department_id:
            c.close()
            return jsonify({"error": "Equipment is not in your department"}), 403
    else:
        customer_id = b.get("customer_id")
        location_id = b.get("location_id") or eq_loc
        department_id = b.get("department_id") or eq_dept

    if not customer_id:
        c.close()
        return jsonify({"error": "Customer is required"}), 400
    code_ = next_code_for("breakdowns", "BRK")
    cur = c.execute(
        "INSERT INTO breakdowns (code,equipment_id,customer_id,complaint_id,location_id,department_id,fault_description,root_cause,priority,status,reported_by,assigned_to,resolution_notes,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code_, equipment_id, customer_id, b.get("complaint_id") or None, location_id, department_id,
         b["fault_description"].strip(), b.get("root_cause", ""), b.get("priority", "medium"), "reported",
         u["id"], b.get("assigned_to") or None, b.get("resolution_notes", ""), now(), now()),
    )
    c.commit()
    new_id = cur.lastrowid
    row = c.execute("SELECT * FROM breakdowns WHERE id=?", (new_id,)).fetchone()
    out = breakdown_payload(c, row)
    c.close()
    audit("breakdown", new_id, u, "created", f"Opened {out['code']} — {out['equipment_name']}")
    _archive_and_purge("breakdown")
    ping_team("breakdown", u["id"], out,
              f"New breakdown {out['code']}: {out['equipment_name']} — {out['fault_description'][:90]}",
              lambda r: email_mod.email_status_changed("breakdown", r, out, "reported"))
    return jsonify(out), 201


@app.get("/api/breakdowns/<int:bid>")
def get_breakdown(bid):
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    c = conn()
    row = c.execute("SELECT * FROM breakdowns WHERE id=?", (bid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    if not _customer_allowed(u, row["customer_id"], row["location_id"], row["department_id"]):
        c.close()
        return jsonify({"error": "Not authorised"}), 403
    out = breakdown_payload(c, row)
    comments = c.execute(
        "SELECT cm.*, u.name AS user_name FROM comments cm JOIN users u ON u.id=cm.user_id "
        "WHERE cm.entity_type='breakdown' AND cm.entity_id=? ORDER BY cm.created_at", (bid,)).fetchall()
    out["comments"] = rows_to_dicts(comments)
    c.close()
    return jsonify(out)


@app.patch("/api/breakdowns/<int:bid>")
def update_breakdown(bid):
    u, err, code = require_role("admin", "technician")
    if err:
        return err, code
    b = get_body()
    c = conn()
    row = c.execute("SELECT * FROM breakdowns WHERE id=?", (bid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    old_row = dict(row)

    fields = []
    params = []
    for f in ("equipment_id", "customer_id", "complaint_id", "fault_description", "root_cause",
              "assigned_to", "resolution_notes", "location_id", "department_id"):
        if f in b:
            fields.append(f"{f}=?")
            params.append(b[f])
    if "priority" in b:
        fields.append("priority=?")
        params.append(b["priority"])
    if "status" in b:
        fields.append("status=?")
        params.append(b["status"])
        if b["status"] == "resolved":
            fields.append("resolved_at=?")
            params.append(now())
        else:
            fields.append("resolved_at=?")
            params.append(None)
    old_assignee = row["assigned_to"]
    old_status = row["status"]
    # Auto-assign: the technician/admin who starts working an unassigned
    # breakdown (e.g. changes its status) becomes its assignee.
    if ("status" in b and "assigned_to" not in b
            and u["role"] in ("admin", "technician") and not row["assigned_to"]):
        fields.append("assigned_to=?")
        params.append(u["id"])
        old_assignee = None
    if fields:
        fields.append("updated_at=?")
        params.append(now())
        params.append(bid)
        c.execute(f"UPDATE breakdowns SET {', '.join(fields)} WHERE id=?", params)
        c.commit()
    row = c.execute("SELECT * FROM breakdowns WHERE id=?", (bid,)).fetchone()
    out = breakdown_payload(c, row)
    c.close()

    # audit trail
    if "assigned_to" in b:
        if b["assigned_to"] and b["assigned_to"] != old_assignee:
            audit("breakdown", bid, u, "assigned", f"→ {out.get('assigned_to_name') or '—'}")
        elif not b["assigned_to"] and old_assignee:
            audit("breakdown", bid, u, "assigned", "Removed assignee")
    elif old_assignee is None and row["assigned_to"] == u["id"]:
        audit("breakdown", bid, u, "assigned", f"Auto-assigned {u['name']} (responded)")
    if b.get("status") and b["status"] != old_status:
        if b["status"] == "resolved":
            audit("breakdown", bid, u, "resolution", "Marked resolved")
        else:
            audit("breakdown", bid, u, "status",
                  f"{STATUS_LABELS.get(old_status, old_status)} → {STATUS_LABELS.get(b['status'], b['status'])}")
    for label, key in (("Fault description", "fault_description"), ("Root cause", "root_cause"),
                       ("Priority", "priority"), ("Equipment", "equipment_id"),
                       ("Customer", "customer_id"), ("Location", "location_id"),
                       ("Department", "department_id")):
        if key in b and (old_row.get(key) or "") != (b[key] or ""):
            audit("breakdown", bid, u, "updated", f"{label} changed")

    if "assigned_to" in b and b["assigned_to"] and b["assigned_to"] != old_assignee:
        notify(b["assigned_to"], f"You were assigned breakdown {out['code']}: {out['equipment_name']}",
               "breakdown", bid, lambda r: email_mod.email_assigned("breakdown", r, out, u["name"]))
    if "status" in b and b["status"] != old_status:
        label = STATUS_LABELS.get(b["status"], b["status"])
        ping_followers("breakdown", u["id"], out,
                       f"Breakdown {out['code']} is now {label}: {out['equipment_name']}",
                       lambda r: email_mod.email_status_changed("breakdown", r, out, b["status"]))
    return jsonify(out)


@app.delete("/api/breakdowns/<int:bid>")
def delete_breakdown(bid):
    u, err, code = require_role("admin")
    if err:
        return err, code
    c = conn()
    row = c.execute("SELECT * FROM breakdowns WHERE id=?", (bid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    c.execute("DELETE FROM comments WHERE entity_type='breakdown' AND entity_id=?", (bid,))
    c.execute("DELETE FROM notifications WHERE entity_type='breakdown' AND entity_id=?", (bid,))
    c.execute("DELETE FROM attachments WHERE entity_type='breakdown' AND entity_id=?", (bid,))
    c.execute("DELETE FROM audit_logs WHERE entity_type='breakdown' AND entity_id=?", (bid,))
    c.execute("DELETE FROM breakdowns WHERE id=?", (bid,))
    c.commit()
    c.close()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Comments
# --------------------------------------------------------------------------
@app.post("/api/comments")
def add_comment():
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    b = get_body()
    if not (b.get("text") or "").strip():
        return jsonify({"error": "Comment text is required"}), 400
    entity = b.get("entity_type")
    eid = b.get("entity_id")
    if entity not in ("complaint", "breakdown") or not eid:
        return jsonify({"error": "Invalid entity"}), 400
    # authorisation: customers only on their own in-scope tickets
    if u["role"] == "customer":
        c = conn()
        row = c.execute(f"SELECT customer_id, location_id, department_id FROM {entity}s WHERE id=?", (eid,)).fetchone()
        c.close()
        if not row or not _customer_allowed(u, row["customer_id"], row["location_id"], row["department_id"]):
            return jsonify({"error": "Not authorised"}), 403
    c = conn()
    # Auto-assign: the first technician/admin who RESPONDS to an unassigned
    # ticket becomes its assignee (only techs/admins who own the work should
    # answer, so the assignee field always names the actual responder).
    ticket = c.execute(f"SELECT * FROM {entity}s WHERE id=?", (eid,)).fetchone()
    auto_assigned = False
    if ticket and u["role"] in ("admin", "technician") and not ticket["assigned_to"]:
        c.execute(f"UPDATE {entity}s SET assigned_to=?, updated_at=? WHERE id=?",
                  (u["id"], now(), eid))
        auto_assigned = True
    cur = c.execute(
        "INSERT INTO comments (entity_type,entity_id,user_id,text,created_at) VALUES (?,?,?,?,?)",
        (entity, eid, u["id"], b["text"].strip(), now()),
    )
    c.commit()
    row = c.execute(
        "SELECT cm.*, u.name AS user_name FROM comments cm JOIN users u ON u.id=cm.user_id WHERE cm.id=?",
        (cur.lastrowid,)).fetchone()
    c.close()
    if auto_assigned:
        audit(entity, eid, u, "assigned", f"Auto-assigned {u['name']} (responded)")
        notify(u["id"], f"You are now assigned to {entity} {ticket['code']} because you responded to it.",
               entity, eid, None)
    audit(entity, eid, u, "comment", b["text"].strip()[:120])

    # notify ticket followers (team + participants), excluding the commenter
    if entity == "complaint":
        rc = conn()
        raw = rc.execute("SELECT * FROM complaints WHERE id=?", (eid,)).fetchone()
        if raw:
            rec = complaint_payload(rc, raw)
            rc.close()
            ping_team("complaint", u["id"], rec,
                      f"{u['name']} commented on complaint {rec['code']}: {b['text'].strip()[:80]}",
                      lambda r: email_mod.email_comment("complaint", r, rec, u["name"], b["text"].strip()))
        else:
            rc.close()
    else:
        rc = conn()
        raw = rc.execute("SELECT * FROM breakdowns WHERE id=?", (eid,)).fetchone()
        if raw:
            rec = breakdown_payload(rc, raw)
            rc.close()
            ping_team("breakdown", u["id"], rec,
                      f"{u['name']} commented on breakdown {rec['code']}: {b['text'].strip()[:80]}",
                      lambda r: email_mod.email_comment("breakdown", r, rec, u["name"], b["text"].strip()))
        else:
            rc.close()
    return jsonify(dict(row)), 201


# --------------------------------------------------------------------------
# Users & dashboard
# --------------------------------------------------------------------------
@app.get("/api/users")
def list_users():
    u, err, code = require_role("admin")
    if err:
        return err, code
    c = conn()
    rows = c.execute("SELECT * FROM users ORDER BY role, name").fetchall()
    out = []
    for r in rows:
        d = public_user(r)
        d["customer_name"] = None
        d["location_name"] = None
        d["department_name"] = None
        if r["customer_id"]:
            cu = c.execute("SELECT name FROM customers WHERE id=?", (r["customer_id"],)).fetchone()
            d["customer_name"] = cu["name"] if cu else None
        d["location_name"] = _name_of(c, "locations", r["location_id"])
        d["department_name"] = _name_of(c, "departments", r["department_id"])
        out.append(d)
    c.close()
    return jsonify(out)


@app.get("/api/technicians")
def list_technicians():
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    c = conn()
    rows = c.execute("SELECT id,name,email,role FROM users WHERE role IN ('technician','admin') AND active=1 ORDER BY name").fetchall()
    c.close()
    return jsonify(rows_to_dicts(rows))


@app.post("/api/users")
def create_user():
    u, err, code = require_role("admin")
    if err:
        return err, code
    b = get_body()
    if not (b.get("name") or "").strip() or not (b.get("email") or "").strip() or not (b.get("password") or ""):
        return jsonify({"error": "Name, email and password are required"}), 400
    role = b.get("role", "technician")
    if role not in ("admin", "technician", "customer"):
        return jsonify({"error": "Invalid role"}), 400
    customer_id = b.get("customer_id") if role == "customer" else None
    location_id = b.get("location_id") if role == "customer" else None
    department_id = b.get("department_id") if role == "customer" else None
    c = conn()
    exists = c.execute("SELECT id FROM users WHERE lower(email)=?", (b["email"].strip().lower(),)).fetchone()
    if exists:
        c.close()
        return jsonify({"error": "Email already in use"}), 409
    cur = c.execute(
        "INSERT INTO users (name,email,phone,password_hash,role,customer_id,location_id,department_id,active,created_at) VALUES (?,?,?,?,?,?,?,?,1,?)",
        (b["name"].strip(), b["email"].strip().lower(), b.get("phone", ""), hash_password(b["password"]),
         role, customer_id, location_id, department_id, now()),
    )
    c.commit()
    row = c.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    c.close()
    return jsonify(public_user(row)), 201


@app.patch("/api/users/<int:uid>")
def update_user(uid):
    u, err, code = require_role("admin")
    if err:
        return err, code
    b = get_body()
    c = conn()
    existing = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not existing:
        c.close()
        return jsonify({"error": "Not found"}), 404
    if "role" in b and b["role"] not in ("admin", "technician", "customer"):
        c.close()
        return jsonify({"error": "Invalid role"}), 400
    if "email" in b:
        new_email = (b["email"] or "").strip().lower()
        if not new_email:
            c.close()
            return jsonify({"error": "Email cannot be empty"}), 400
        dup = c.execute("SELECT id FROM users WHERE lower(email)=? AND id!=?", (new_email, uid)).fetchone()
        if dup:
            c.close()
            return jsonify({"error": "Email already in use"}), 409
    fields, params = [], []
    for f in ("name", "phone", "role", "customer_id", "location_id", "department_id", "active"):
        if f in b:
            fields.append(f"{f}=?")
            params.append(b[f])
    if "email" in b:
        fields.append("email=?")
        params.append(b["email"].strip().lower())
    if b.get("password"):
        fields.append("password_hash=?")
        params.append(hash_password(b["password"]))
    if fields:
        params.append(uid)
        c.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", params)
        c.commit()
    row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    out = public_user(row)
    if row and row["customer_id"]:
        cu = c.execute("SELECT name FROM customers WHERE id=?", (row["customer_id"],)).fetchone()
        out["customer_name"] = cu["name"] if cu else None
    out["location_name"] = _name_of(c, "locations", row["location_id"]) if row else None
    out["department_name"] = _name_of(c, "departments", row["department_id"]) if row else None
    c.close()
    return jsonify(out) if row else (jsonify({"error": "Not found"}), 404)


@app.delete("/api/users/<int:uid>")
def delete_user(uid):
    u, err, code = require_role("admin")
    if err:
        return err, code
    if uid == u["id"]:
        return jsonify({"error": "You cannot delete your own account"}), 400
    c = conn()
    existing = c.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
    if not existing:
        c.close()
        return jsonify({"error": "Not found"}), 404
    # Things that reference the user and would break if we delete them:
    refs = []
    for table, col in (("complaints", "assigned_to"), ("complaints", "created_by"),
                       ("breakdowns", "assigned_to"), ("breakdowns", "reported_by"),
                       ("comments", "user_id"), ("pm_schedules", "assigned_to"),
                       ("pm_logs", "performed_by")):
        n = c.execute(f"SELECT COUNT(*) n FROM {table} WHERE {col}=?", (uid,)).fetchone()["n"]
        if n:
            refs.append(f"{n} {table.rsplit('_',1)[-1]}")
    if refs:
        c.close()
        return jsonify({"error": "Cannot delete — user is referenced by: " + ", ".join(refs) +
                        ". Reassign or delete those first."}), 409
    # Clear the user's own sessions and notifications, then remove the account.
    c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    c.execute("DELETE FROM notifications WHERE user_id=?", (uid,))
    c.execute("DELETE FROM users WHERE id=?", (uid,))
    c.commit()
    c.close()
    return jsonify({"ok": True})


@app.get("/api/dashboard")
def dashboard():
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    c = conn()

    # For customer users, restrict every aggregate to their own
    # customer + location + department. Build a list of WHERE conditions.
    cust_scopes = []
    if u["role"] == "customer":
        if u.get("customer_id"):
            cust_scopes.append(("customer_id", u["customer_id"]))
        if u.get("location_id"):
            cust_scopes.append(("location_id", u["location_id"]))
        if u.get("department_id"):
            cust_scopes.append(("department_id", u["department_id"]))

    def scoped(sql, params=None, cols=None):
        """Append customer-scope conditions (as WHERE/AND) before any
        GROUP BY / ORDER BY / LIMIT clause. `cols` maps a scope key to the
        qualified column name (defaults to the bare column)."""
        params = list(params or [])
        if not cust_scopes:
            return sql, params
        cols = cols or {}
        conds = []
        for key, val in cust_scopes:
            col = cols.get(key, key)
            conds.append(f"{col} = ?")
            params.append(val)
        up = sql.upper()
        anchor = len(sql)
        for kw in ("GROUP BY", "ORDER BY", "LIMIT"):
            i = up.find(kw)
            if i != -1:
                anchor = min(anchor, i)
        joined = " AND ".join(conds)
        head, tail = sql[:anchor], sql[anchor:]
        if "WHERE" in up:
            sql = head + " AND " + joined + " " + tail
        else:
            sql = head + " WHERE " + joined + " " + tail
        return sql, params

    def one(sql, params=None, cols=None):
        sql, params = scoped(sql, params, cols)
        return c.execute(sql, params).fetchone()["n"]

    # Dashboard ticket data is scoped to a rolling 3-month window.
    WIN = "date('now','-3 months')"

    open_cmp = one("SELECT COUNT(*) n FROM complaints WHERE status IN ('open','in_progress') AND created_at >= " + WIN)
    resolved_cmp = one("SELECT COUNT(*) n FROM complaints WHERE status IN ('resolved','closed') AND created_at >= " + WIN)
    critical_cmp = one("SELECT COUNT(*) n FROM complaints WHERE status IN ('open','in_progress') AND priority='critical' AND created_at >= " + WIN)
    open_brk = one("SELECT COUNT(*) n FROM breakdowns WHERE status NOT IN ('resolved') AND created_at >= " + WIN)
    resolved_brk = one("SELECT COUNT(*) n FROM breakdowns WHERE status='resolved' AND created_at >= " + WIN)
    total_eq = one("SELECT COUNT(*) n FROM equipment")
    total_cust = c.execute("SELECT COUNT(*) n FROM customers").fetchone()["n"]

    # preventive maintenance due — pm_schedules has no location/department of its
    # own, so restrict via the linked equipment for location/department-scoped users.
    if u["role"] == "customer":
        if u.get("location_id") or u.get("department_id"):
            pm_conds = ["p.active=1"]
            pm_pp = []
            if u.get("customer_id"):
                pm_conds.append("p.customer_id = ?")
                pm_pp.append(u["customer_id"])
            if u.get("location_id"):
                pm_conds.append("e.location_id = ?")
                pm_pp.append(u["location_id"])
            if u.get("department_id"):
                pm_conds.append("e.department_id = ?")
                pm_pp.append(u["department_id"])
            pm_due_sql = ("SELECT COUNT(*) n FROM pm_schedules p LEFT JOIN equipment e ON e.id=p.equipment_id "
                          "WHERE " + " AND ".join(pm_conds) +
                          " AND (p.next_due_at IS NULL OR p.next_due_at <= ?)")
            pm_due = c.execute(pm_due_sql, pm_pp + [now()]).fetchone()["n"]
            pm_total_sql = ("SELECT COUNT(*) n FROM pm_schedules p LEFT JOIN equipment e ON e.id=p.equipment_id "
                            "WHERE " + " AND ".join(pm_conds))
            pm_total = c.execute(pm_total_sql, pm_pp).fetchone()["n"]
        elif u.get("customer_id"):
            pm_due = c.execute(
                "SELECT COUNT(*) n FROM pm_schedules WHERE active=1 AND customer_id=? AND (next_due_at IS NULL OR next_due_at <= ?)",
                [u["customer_id"], now()]).fetchone()["n"]
            pm_total = c.execute(
                "SELECT COUNT(*) n FROM pm_schedules WHERE active=1 AND customer_id=?",
                [u["customer_id"]]).fetchone()["n"]
        else:
            pm_due = c.execute(
                "SELECT COUNT(*) n FROM pm_schedules WHERE active=1 AND (next_due_at IS NULL OR next_due_at <= ?)",
                [now()]).fetchone()["n"]
            pm_total = c.execute("SELECT COUNT(*) n FROM pm_schedules WHERE active=1").fetchone()["n"]
    else:
        pm_due = c.execute(
            "SELECT COUNT(*) n FROM pm_schedules WHERE active=1 AND (next_due_at IS NULL OR next_due_at <= ?)",
            [now()]).fetchone()["n"]
        pm_total = c.execute("SELECT COUNT(*) n FROM pm_schedules WHERE active=1").fetchone()["n"]

    # status breakdowns for charts (3-month window)
    cmps_sql, cmps_pp = scoped(
        "SELECT status, COUNT(*) n FROM complaints WHERE created_at >= " + WIN + " GROUP BY status")
    cmp_by_status = rows_to_dicts(c.execute(cmps_sql, cmps_pp).fetchall())
    brks_sql, brks_pp = scoped(
        "SELECT status, COUNT(*) n FROM breakdowns WHERE created_at >= " + WIN + " GROUP BY status")
    brk_by_status = rows_to_dicts(c.execute(brks_sql, brks_pp).fetchall())
    cpri_sql, cpri_pp = scoped(
        "SELECT priority, COUNT(*) n FROM complaints WHERE status IN ('open','in_progress') AND created_at >= " + WIN + " GROUP BY priority")
    cmp_by_priority = rows_to_dicts(c.execute(cpri_sql, cpri_pp).fetchall())

    # monthly volumes (last 3 months)
    m_sql, m_pp = scoped(
        "SELECT substr(created_at,1,7) AS month, COUNT(*) n FROM complaints "
        "WHERE created_at >= date('now','-3 months') GROUP BY month ORDER BY month")
    monthly = c.execute(m_sql, m_pp).fetchall()
    mbrk_sql, mbrk_pp = scoped(
        "SELECT substr(created_at,1,7) AS month, COUNT(*) n FROM breakdowns "
        "WHERE created_at >= date('now','-3 months') GROUP BY month ORDER BY month")
    monthly_brk = c.execute(mbrk_sql, mbrk_pp).fetchall()

    # most problem-prone equipment in the 3-month window (filter on qualified b.* columns)
    te_sql = ("SELECT COALESCE(e.name,'General') AS name, COUNT(*) n FROM breakdowns b "
              "LEFT JOIN equipment e ON e.id=b.equipment_id "
              "WHERE b.created_at >= date('now','-3 months')")
    te_pp = []
    if cust_scopes:
        for key, val in cust_scopes:
            te_sql += f" AND b.{key} = ?"
            te_pp.append(val)
    te_sql += " GROUP BY e.name ORDER BY n DESC LIMIT 5"
    top_equip = c.execute(te_sql, te_pp).fetchall()

    # recent activity (3-month window)
    rc_sql, rc_pp = scoped(
        "SELECT id, code, subject, status, priority, created_at, customer_id FROM complaints "
        "WHERE created_at >= " + WIN + " ORDER BY created_at DESC LIMIT 5")
    recent_cmp = c.execute(rc_sql, rc_pp).fetchall()
    rb_sql, rb_pp = scoped(
        "SELECT id, code, fault_description, status, priority, created_at, customer_id FROM breakdowns "
        "WHERE created_at >= " + WIN + " ORDER BY created_at DESC LIMIT 5")
    recent_brk = c.execute(rb_sql, rb_pp).fetchall()

    c.close()
    return jsonify({
        "counts": {
            "open_complaints": open_cmp,
            "resolved_complaints": resolved_cmp,
            "critical_complaints": critical_cmp,
            "open_breakdowns": open_brk,
            "resolved_breakdowns": resolved_brk,
            "total_equipment": total_eq,
            "total_customers": total_cust,
            "pm_due": pm_due,
            "pm_total": pm_total,
        },
        "complaints_by_status": cmp_by_status,
        "breakdowns_by_status": brk_by_status,
        "complaints_by_priority": cmp_by_priority,
        "monthly_complaints": [dict(r) for r in monthly],
        "monthly_breakdowns": [dict(r) for r in monthly_brk],
        "top_equipment": [dict(r) for r in top_equip],
        "recent_complaints": [dict(r) for r in recent_cmp],
        "recent_breakdowns": [dict(r) for r in recent_brk],
    })


# --------------------------------------------------------------------------
# Notifications (in-app)
# --------------------------------------------------------------------------
@app.get("/api/notifications")
def list_notifications():
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    c = conn()
    rows = c.execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
        (u["id"],)).fetchall()
    c.close()
    return jsonify(rows_to_dicts(rows))


@app.get("/api/notifications/ping")
def notifications_ping():
    """Lightweight poll: unread count, a change stamp, and the latest unread
    notification so the client can play an audible alert for new tickets."""
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    c = conn()
    unread = c.execute("SELECT COUNT(*) n FROM notifications WHERE user_id=? AND read=0", (u["id"],)).fetchone()["n"]
    latest = c.execute(
        "SELECT id, text, entity_type, entity_id, created_at FROM notifications "
        "WHERE user_id=? AND read=0 ORDER BY id DESC LIMIT 1", (u["id"],)).fetchone()
    stamp = c.execute("SELECT updated_at FROM notification_pings WHERE user_id=?", (u["id"],)).fetchone()
    c.close()
    return jsonify({
        "unread": unread,
        "stamp": stamp["updated_at"] if stamp else None,
        "latest": dict(latest) if latest else None,
    })


@app.post("/api/notifications/read")
def mark_notifications_read():
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    b = get_body()
    c = conn()
    if b.get("id"):
        c.execute("UPDATE notifications SET read=1 WHERE id=? AND user_id=?", (b["id"], u["id"]))
    else:
        c.execute("UPDATE notifications SET read=1 WHERE user_id=?", (u["id"],))
    c.commit()
    c.close()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Attachments (photos)
# --------------------------------------------------------------------------
def _attachments_meta(c, entity, entity_id):
    rows = c.execute(
        "SELECT id, filename, mime, size, uploaded_by, created_at FROM attachments "
        "WHERE entity_type=? AND entity_id=? ORDER BY created_at", (entity, entity_id)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        up = c.execute("SELECT name FROM users WHERE id=?", (r["uploaded_by"],)).fetchone()
        d["uploaded_by_name"] = up["name"] if up else None
        out.append(d)
    return out


@app.post("/api/attachments")
def upload_attachment():
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    entity = request.form.get("entity_type")
    eid = request.form.get("entity_id")
    if entity not in ("complaint", "breakdown") or not eid:
        return jsonify({"error": "Invalid entity"}), 400
    eid = int(eid)
    # authorisation: customers only on their own tickets
    if u["role"] == "customer":
        c = conn()
        if entity == "complaint":
            row = c.execute("SELECT customer_id, location_id, department_id FROM complaints WHERE id=?", (eid,)).fetchone()
        else:
            row = c.execute("SELECT customer_id, location_id, department_id FROM breakdowns WHERE id=?", (eid,)).fetchone()
        c.close()
        if not row or not _customer_allowed(u, row["customer_id"], row["location_id"], row["department_id"]):
            return jsonify({"error": "Not authorised"}), 403

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file uploaded"}), 400
    data = f.read()
    # Some mobile browsers send a blank/generic content type. Derive the real
    # type from the filename extension when the client's MIME is unreliable.
    mime = (f.mimetype or "").strip().lower() or None
    if not mime or mime == "application/octet-stream" or mime == "binary/octet-stream":
        guessed = mimetypes.guess_type(f.filename or "")[0]
        if guessed:
            mime = guessed.lower()
    if not mime:
        mime = "application/octet-stream"
    allowed = (
        "image/",
        "application/pdf",
        # Office documents (service reports, worksheets, slides)
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "text/csv",
        "text/plain",
    )
    if not any(mime.startswith(x) for x in allowed):
        return jsonify({"error": "Unsupported file type (use an image, PDF, or Office document)"}), 400
    if len(data) > 8 * 1024 * 1024:
        return jsonify({"error": "File too large (max 8 MB)"}), 400

    c = conn()
    cur = c.execute(
        "INSERT INTO attachments (entity_type,entity_id,filename,mime,size,uploaded_by,data,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (entity, eid, f.filename[:200], mime, len(data), u["id"], data, now()),
    )
    c.commit()
    row = c.execute("SELECT id, filename, mime, size, uploaded_by, created_at FROM attachments WHERE id=?",
                    (cur.lastrowid,)).fetchone()
    d = dict(row)
    d["uploaded_by_name"] = u["name"]
    c.close()

    audit(entity, eid, u, "attachment", f"Added {f.filename[:120]}")

    # notify followers
    if entity == "complaint":
        rc = conn()
        raw = rc.execute("SELECT * FROM complaints WHERE id=?", (eid,)).fetchone()
        if raw:
            rec = complaint_payload(rc, raw)
            rc.close()
            ping_followers("complaint", u["id"], rec,
                           f"{u['name']} attached a file to complaint {rec['code']}",
                           lambda r: email_mod.email_comment("complaint", r, rec, u["name"], "📎 Attached a file."))
        else:
            rc.close()
    else:
        rc = conn()
        raw = rc.execute("SELECT * FROM breakdowns WHERE id=?", (eid,)).fetchone()
        if raw:
            rec = breakdown_payload(rc, raw)
            rc.close()
            ping_followers("breakdown", u["id"], rec,
                           f"{u['name']} attached a file to breakdown {rec['code']}",
                           lambda r: email_mod.email_comment("breakdown", r, rec, u["name"], "📎 Attached a file."))
        else:
            rc.close()
    return jsonify(d), 201


@app.get("/api/attachments")
def list_attachments():
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    entity = request.args.get("entity_type")
    eid = request.args.get("entity_id", type=int)
    if entity not in ("complaint", "breakdown") or not eid:
        return jsonify({"error": "Invalid entity"}), 400
    if u["role"] == "customer":
        c = conn()
        row = c.execute(f"SELECT customer_id, location_id, department_id FROM {entity}s WHERE id=?", (eid,)).fetchone()
        c.close()
        if not row or not _customer_allowed(u, row["customer_id"], row["location_id"], row["department_id"]):
            return jsonify({"error": "Not authorised"}), 403
    c = conn()
    out = _attachments_meta(c, entity, eid)
    c.close()
    return jsonify(out)


@app.get("/api/audit")
def list_audit():
    """Per-ticket history log: who did what and when."""
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    entity = request.args.get("entity_type")
    eid = request.args.get("entity_id", type=int)
    if entity not in ("complaint", "breakdown") or not eid:
        return jsonify({"error": "Invalid entity"}), 400
    # authorization: customers only on their own in-scope tickets
    c = conn()
    row = c.execute(f"SELECT customer_id, location_id, department_id FROM {entity}s WHERE id=?", (eid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    if not _customer_allowed(u, row["customer_id"], row["location_id"], row["department_id"]):
        c.close()
        return jsonify({"error": "Not authorised"}), 403
    rows = c.execute(
        "SELECT * FROM audit_logs WHERE entity_type=? AND entity_id=? ORDER BY id", (entity, eid)).fetchall()
    c.close()
    return jsonify(rows_to_dicts(rows))


@app.get("/api/attachments/<int:aid>/file")
def attachment_file(aid):
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    c = conn()
    row = c.execute("SELECT * FROM attachments WHERE id=?", (aid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    if u["role"] == "customer":
        tbl = row["entity_type"]
        trow = c.execute(f"SELECT customer_id, location_id, department_id FROM {tbl}s WHERE id=?", (row["entity_id"],)).fetchone()
        if not trow or not _customer_allowed(u, trow["customer_id"], trow["location_id"], trow["department_id"]):
            c.close()
            return jsonify({"error": "Not authorised"}), 403
    data = row["data"]
    mime = row["mime"]
    c.close()
    return Response(data, mimetype=mime, headers={
        "Content-Disposition": f'inline; filename="{row["filename"]}"'})


@app.delete("/api/attachments/<int:aid>")
def delete_attachment(aid):
    u, err, code = require_role("admin", "technician")
    if err:
        return err, code
    c = conn()
    c.execute("DELETE FROM attachments WHERE id=?", (aid,))
    c.commit()
    c.close()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Reports & export
# --------------------------------------------------------------------------
@app.get("/api/export.csv")
def export_csv():
    u, err, code = require_role("admin", "technician")
    if err:
        return err, code
    entity = request.args.get("type", "complaints")
    import csv as csv_mod
    c = conn()
    out = ""
    if entity == "breakdowns":
        rows = c.execute("SELECT * FROM breakdowns ORDER BY created_at DESC").fetchall()
        header = ["Code", "EquipmentID", "CustomerID", "ComplaintID", "FaultDescription", "RootCause",
                  "Priority", "Status", "ReportedBy", "AssignedTo", "ResolutionNotes", "CreatedAt", "ResolvedAt"]
        keys = ["code", "equipment_id", "customer_id", "complaint_id", "fault_description", "root_cause",
                "priority", "status", "reported_by", "assigned_to", "resolution_notes", "created_at", "resolved_at"]
    else:
        rows = c.execute("SELECT * FROM complaints ORDER BY created_at DESC").fetchall()
        header = ["Code", "CustomerID", "EquipmentID", "Subject", "Description", "Category",
                  "Priority", "Status", "CreatedBy", "AssignedTo", "CreatedAt", "ResolvedAt"]
        keys = ["code", "customer_id", "equipment_id", "subject", "description", "category",
                "priority", "status", "created_by", "assigned_to", "created_at", "resolved_at"]
    c.close()
    import io as _io
    buf = _io.StringIO()
    w = csv_mod.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([r[k] if r[k] is not None else "" for k in keys])
    data = buf.getvalue()
    fname = f"labcare_{entity}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return Response(data, mimetype="text/csv", headers={
        "Content-Disposition": f"attachment; filename={fname}"})


def _load_ticket_photos(entity_type, entity_id):
    """Return raw photo bytes for a ticket's attachments."""
    from database import conn as _c
    c = _c()
    rows = c.execute("SELECT data FROM attachments WHERE entity_type=? AND entity_id=? ORDER BY created_at LIMIT 4",
                     (entity_type, entity_id)).fetchall()
    c.close()
    return [r["data"] for r in rows]


@app.get("/api/complaints/<int:cid>/report.pdf")
def complaint_report_pdf(cid):
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    c = conn()
    row = c.execute("SELECT * FROM complaints WHERE id=?", (cid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    if not _customer_allowed(u, row["customer_id"], row["location_id"], row["department_id"]):
        c.close()
        return jsonify({"error": "Not authorised"}), 403
    comp = complaint_payload(c, row)
    brk_rows = c.execute("SELECT * FROM breakdowns WHERE complaint_id=?", (cid,)).fetchall()
    breakdowns = [breakdown_payload(c, b) for b in brk_rows]
    comments = rows_to_dicts(c.execute(
        "SELECT cm.*, u.name AS user_name FROM comments cm JOIN users u ON u.id=cm.user_id "
        "WHERE cm.entity_type='complaint' AND cm.entity_id=? ORDER BY cm.created_at", (cid,)).fetchall())
    c.close()

    photos = _load_ticket_photos("complaint", cid)
    pdf = report_mod.service_report(comp, breakdowns, comments, photos)
    return Response(pdf, mimetype="application/pdf", headers={
        "Content-Disposition": f'inline; filename="service_report_{comp["code"]}.pdf"'})


@app.get("/api/reports/trend.pdf")
def trend_report_pdf():
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    cust_scopes = []
    if u["role"] == "customer":
        if u.get("customer_id"):
            cust_scopes.append(u["customer_id"])
        if u.get("location_id"):
            cust_scopes.append(u["location_id"])
        if u.get("department_id"):
            cust_scopes.append(u["department_id"])

    def scope(sql, params=None, col="customer_id"):
        params = list(params or [])
        if not cust_scopes:
            return sql, params
        up = sql.upper()
        anchor = len(sql)
        for kw in ("GROUP BY", "ORDER BY", "LIMIT"):
            i = up.find(kw)
            if i != -1:
                anchor = min(anchor, i)
        cond = f"{col} = ?"
        if "WHERE" in up:
            sql = sql[:anchor] + " AND " + cond + " " + sql[anchor:]
            params.append(cust_scopes[0])
        else:
            sql = sql[:anchor] + " WHERE " + cond + " " + sql[anchor:]
            params.append(cust_scopes[0])
        return sql, params

    c = conn()
    cmp_sql, cmp_pp = scope("SELECT * FROM complaints ORDER BY created_at DESC")
    cmp_rows = c.execute(cmp_sql, cmp_pp).fetchall()
    brk_sql, brk_pp = scope("SELECT * FROM breakdowns ORDER BY created_at DESC")
    brk_rows = c.execute(brk_sql, brk_pp).fetchall()
    complaints = [complaint_payload(c, r) for r in cmp_rows]
    breakdowns = [breakdown_payload(c, r) for r in brk_rows]

    m_sql, m_pp = scope(
        "SELECT substr(created_at,1,7) AS month, COUNT(*) n FROM complaints "
        "WHERE created_at >= date('now','-6 months') GROUP BY month ORDER BY month")
    monthly = rows_to_dicts(c.execute(m_sql, m_pp).fetchall())

    cs_sql, cs_pp = scope("SELECT status, COUNT(*) n FROM complaints GROUP BY status")
    by_status_cmp = [(r["status"], r["n"]) for r in c.execute(cs_sql, cs_pp).fetchall()]
    bs_sql, bs_pp = scope("SELECT status, COUNT(*) n FROM breakdowns GROUP BY status")
    by_status_brk = [(r["status"], r["n"]) for r in c.execute(bs_sql, bs_pp).fetchall()]

    te_sql = ("SELECT COALESCE(e.name,'General') AS name, COUNT(*) n FROM breakdowns b "
              "LEFT JOIN equipment e ON e.id=b.equipment_id")
    te_pp = []
    if cust_scopes:
        te_sql += " WHERE b.customer_id = ?"
        te_pp.append(cust_scopes[0])
    te_sql += " GROUP BY e.name ORDER BY n DESC LIMIT 6"
    top_eq = c.execute(te_sql, te_pp).fetchall()
    c.close()

    pdf = report_mod.trend_report(complaints, breakdowns, monthly, by_status_cmp, by_status_brk,
                                  [(r["name"], r["n"]) for r in top_eq])
    return Response(pdf, mimetype="application/pdf", headers={
        "Content-Disposition": 'inline; filename="labcare_trend_report.pdf"'})


# --------------------------------------------------------------------------
# Preventive maintenance schedules
# --------------------------------------------------------------------------
def _pm_payload(c, row):
    d = dict(row)
    cust = c.execute("SELECT name FROM customers WHERE id=?", (d["customer_id"],)).fetchone()
    eq = c.execute("SELECT name, model, serial_number FROM equipment WHERE id=?",
                   (d["equipment_id"],)).fetchone() if d.get("equipment_id") else None
    assignee = c.execute("SELECT name FROM users WHERE id=?", (d["assigned_to"],)).fetchone() if d.get("assigned_to") else None
    d["customer_name"] = cust["name"] if cust else None
    d["equipment_name"] = f"{eq['name']} — {eq['model']}" if eq else None
    d["equipment_model"] = eq["model"] if eq else None
    d["assigned_to_name"] = assignee["name"] if assignee else None
    d["log_count"] = c.execute("SELECT COUNT(*) n FROM pm_logs WHERE schedule_id=?", (d["id"],)).fetchone()["n"]
    return d


@app.get("/api/pms")
def list_pms():
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    c = conn()
    where, params = [], []
    if u["role"] == "customer":
        # customers see PM schedules for their own location & department's equipment
        if u.get("customer_id"):
            where.append("p.customer_id=?")
            params.append(u["customer_id"])
    mine = request.args.get("mine")
    if mine and is_tech_user(u):
        where.append("(p.assigned_to=? OR p.assigned_to IS NULL)")
        params.append(u["id"])
    q = ("SELECT p.* FROM pm_schedules p "
         "LEFT JOIN equipment e ON e.id=p.equipment_id")
    if u["role"] == "customer" and (u.get("location_id") or u.get("department_id")):
        # only schedules tied to equipment in the customer's own location/dept,
        # plus any general (equipment-less) schedules for the customer
        eq_conds = []
        if u.get("location_id"):
            eq_conds.append("e.location_id = ?")
            params.append(u["location_id"])
        if u.get("department_id"):
            eq_conds.append("e.department_id = ?")
            params.append(u["department_id"])
        where.append("(p.equipment_id IS NULL OR (" + " AND ".join(eq_conds) + "))")
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY p.next_due_at IS NULL, p.next_due_at ASC, p.id DESC"
    rows = c.execute(q, params).fetchall()
    out = [_pm_payload(c, r) for r in rows]
    c.close()
    return jsonify(out)


@app.post("/api/pms")
def create_pm():
    u, err, code = require_role("admin", "technician")
    if err:
        return err, code
    b = get_body()
    if not (b.get("title") or "").strip():
        return jsonify({"error": "Title is required"}), 400
    if not b.get("customer_id"):
        return jsonify({"error": "Customer is required"}), 400
    interval_days = int(b.get("interval_days") or 90)
    if interval_days < 1:
        return jsonify({"error": "Interval must be at least 1 day"}), 400
    next_due = b.get("next_due_at") or None
    if next_due:
        next_due = (next_due[:10] + " 00:00:00") if len(next_due) <= 10 else next_due
    c = conn()
    cur = c.execute(
        "INSERT INTO pm_schedules (customer_id,equipment_id,title,description,interval_days,last_done_at,next_due_at,assigned_to,active,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,1,?)",
        (b["customer_id"], b.get("equipment_id") or None, b["title"].strip(), b.get("description", ""),
         interval_days, b.get("last_done_at") or None, next_due, b.get("assigned_to") or None, now()),
    )
    c.commit()
    row = c.execute("SELECT * FROM pm_schedules WHERE id=?", (cur.lastrowid,)).fetchone()
    out = _pm_payload(c, row)
    c.close()
    return jsonify(out), 201


@app.patch("/api/pms/<int:pid>")
def update_pm(pid):
    u, err, code = require_role("admin", "technician")
    if err:
        return err, code
    b = get_body()
    c = conn()
    row = c.execute("SELECT * FROM pm_schedules WHERE id=?", (pid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    fields, params = [], []
    for f in ("title", "description", "customer_id", "equipment_id", "assigned_to", "active", "last_done_at"):
        if f in b:
            fields.append(f"{f}=?")
            params.append(b[f])
    if "interval_days" in b:
        fields.append("interval_days=?")
        params.append(int(b["interval_days"]))
    if "next_due_at" in b:
        fields.append("next_due_at=?")
        v = b["next_due_at"]
        params.append((v[:10] + " 00:00:00") if v and len(v) <= 10 else v)
    if fields:
        params.append(pid)
        c.execute(f"UPDATE pm_schedules SET {', '.join(fields)} WHERE id=?", params)
        c.commit()
    row = c.execute("SELECT * FROM pm_schedules WHERE id=?", (pid,)).fetchone()
    out = _pm_payload(c, row)
    c.close()
    return jsonify(out)


@app.delete("/api/pms/<int:pid>")
def delete_pm(pid):
    u, err, code = require_role("admin", "technician")
    if err:
        return err, code
    c = conn()
    c.execute("DELETE FROM pm_schedules WHERE id=?", (pid,))
    c.commit()
    c.close()
    return jsonify({"ok": True})


@app.post("/api/pms/<int:pid>/complete")
def complete_pm(pid):
    u, err, code = require_role("admin", "technician")
    if err:
        return err, code
    b = get_body()
    c = conn()
    row = c.execute("SELECT * FROM pm_schedules WHERE id=?", (pid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    performed_at = b.get("performed_at") or now()
    due_in = row["interval_days"]
    # next_due = performed date + interval days
    next_str = performed_at[:10]
    import datetime as _dt
    try:
        nd = _dt.datetime.strptime(next_str, "%Y-%m-%d") + _dt.timedelta(days=due_in)
        next_due = nd.strftime("%Y-%m-%d 00:00:00")
    except Exception:
        next_due = None
    c.execute(
        "UPDATE pm_schedules SET last_done_at=?, next_due_at=? WHERE id=?",
        (performed_at, next_due, pid),
    )
    c.execute(
        "INSERT INTO pm_logs (schedule_id,equipment_id,performed_by,performed_at,notes) VALUES (?,?,?,?,?)",
        (pid, row["equipment_id"], u["id"], performed_at, b.get("notes", "")),
    )
    c.commit()
    row = c.execute("SELECT * FROM pm_schedules WHERE id=?", (pid,)).fetchone()
    out = _pm_payload(c, row)
    c.close()
    return jsonify(out)


@app.get("/api/pms/<int:pid>/logs")
def pm_logs(pid):
    u, err, code = require_role("admin", "technician", "customer")
    if err:
        return err, code
    c = conn()
    rows = c.execute(
        "SELECT l.*, u.name AS performed_by_name FROM pm_logs l JOIN users u ON u.id=l.performed_by "
        "WHERE l.schedule_id=? ORDER BY l.performed_at DESC", (pid,)).fetchall()
    c.close()
    return jsonify(rows_to_dicts(rows))


# --------------------------------------------------------------------------
# Customer portal (QR)
# --------------------------------------------------------------------------
def _portal_qr_png(token, label):
    import io as _io
    import qrcode as _qr
    from qrcode.image.pil import PilImage
    base = (request.host_url or "http://localhost:8000/").rstrip("/")
    url = f"{base}/portal.html?t={token}"
    img = _qr.make(url)
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@app.get("/api/portal-links")
def list_portal_links():
    u, err, code = require_role("admin", "technician")
    if err:
        return err, code
    c = conn()
    rows = c.execute(
        "SELECT pl.*, cu.name AS customer_name, e.name AS equipment_name, e.model AS equipment_model, e.serial_number AS equipment_serial "
        "FROM portal_links pl JOIN customers cu ON cu.id=pl.customer_id "
        "LEFT JOIN equipment e ON e.id=pl.equipment_id "
        "ORDER BY pl.created_at DESC").fetchall()
    c.close()
    return jsonify(rows_to_dicts(rows))


@app.post("/api/portal-links")
def create_portal_link():
    u, err, code = require_role("admin", "technician")
    if err:
        return err, code
    b = get_body()
    if not b.get("customer_id"):
        return jsonify({"error": "Customer is required"}), 400
    token = uuid.uuid4().hex[:16]
    c = conn()
    cur = c.execute(
        "INSERT INTO portal_links (token,customer_id,equipment_id,label,active,created_by,created_at) VALUES (?,?,?,?,1,?,?)",
        (token, b["customer_id"], b.get("equipment_id") or None, b.get("label", ""), u["id"], now()),
    )
    c.commit()
    row = c.execute(
        "SELECT pl.*, cu.name AS customer_name, e.name AS equipment_name FROM portal_links pl "
        "JOIN customers cu ON cu.id=pl.customer_id LEFT JOIN equipment e ON e.id=pl.equipment_id WHERE pl.id=?",
        (cur.lastrowid,)).fetchone()
    c.close()
    return jsonify(dict(row)), 201


@app.get("/api/portal-links/<int:lid>/qr")
def portal_link_qr(lid):
    u, err, code = require_role("admin", "technician")
    if err:
        return err, code
    c = conn()
    row = c.execute("SELECT * FROM portal_links WHERE id=?", (lid,)).fetchone()
    c.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    png = _portal_qr_png(row["token"], row["label"])
    return Response(png, mimetype="image/png", headers={
        "Content-Disposition": f'inline; filename="portal_{row["token"]}.png"'})


@app.patch("/api/portal-links/<int:lid>")
def update_portal_link(lid):
    u, err, code = require_role("admin", "technician")
    if err:
        return err, code
    b = get_body()
    c = conn()
    fields, params = [], []
    for f in ("label", "customer_id", "equipment_id", "active"):
        if f in b:
            fields.append(f"{f}=?")
            params.append(b[f])
    if fields:
        params.append(lid)
        c.execute(f"UPDATE portal_links SET {', '.join(fields)} WHERE id=?", params)
        c.commit()
    row = c.execute(
        "SELECT pl.*, cu.name AS customer_name, e.name AS equipment_name FROM portal_links pl "
        "JOIN customers cu ON cu.id=pl.customer_id LEFT JOIN equipment e ON e.id=pl.equipment_id WHERE pl.id=?",
        (lid,)).fetchone()
    c.close()
    return jsonify(dict(row)) if row else (jsonify({"error": "Not found"}), 404)


@app.delete("/api/portal-links/<int:lid>")
def delete_portal_link(lid):
    u, err, code = require_role("admin", "technician")
    if err:
        return err, code
    c = conn()
    c.execute("DELETE FROM portal_links WHERE id=?", (lid,))
    c.commit()
    c.close()
    return jsonify({"ok": True})


# --- public portal endpoints (no auth; token-scoped) -----------------------
@app.get("/api/portal/<token>")
def portal_info(token):
    c = conn()
    row = c.execute(
        "SELECT pl.*, cu.name AS customer_name FROM portal_links pl JOIN customers cu ON cu.id=pl.customer_id "
        "WHERE pl.token=? AND pl.active=1", (token,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Invalid or expired link"}), 404
    d = dict(row)
    if d.get("equipment_id"):
        eq = c.execute("SELECT * FROM equipment WHERE id=?", (d["equipment_id"],)).fetchone()
        if eq:
            d["equipment"] = dict(eq)
            # include open tickets for context
            open_cmp = c.execute(
                "SELECT id, code, subject, status, priority, created_at FROM complaints "
                "WHERE equipment_id=? AND status IN ('open','in_progress') ORDER BY created_at DESC LIMIT 5",
                (d["equipment_id"],)).fetchall()
            d["open_complaints"] = rows_to_dicts(open_cmp)
        else:
            d["equipment"] = None
            d["open_complaints"] = []
    else:
        d["equipment"] = None
        d["open_complaints"] = rows_to_dicts(c.execute(
            "SELECT id, code, subject, status, priority, created_at FROM complaints "
            "WHERE customer_id=? AND status IN ('open','in_progress') ORDER BY created_at DESC LIMIT 5",
            (d["customer_id"],)).fetchall())
    c.close()
    d.pop("created_by", None)
    return jsonify(d)


@app.post("/api/portal/<token>/complaints")
def portal_submit_complaint(token):
    c = conn()
    row = c.execute(
        "SELECT * FROM portal_links WHERE token=? AND active=1", (token,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Invalid or expired link"}), 404
    body = get_body()
    subject = (body.get("subject") or "").strip()
    if not subject:
        c.close()
        return jsonify({"error": "Subject is required"}), 400
    reporter_name = (body.get("name") or "").strip()
    reporter_phone = (body.get("phone") or "").strip()
    if not reporter_name:
        c.close()
        return jsonify({"error": "Your name is required"}), 400
    if not reporter_phone:
        c.close()
        return jsonify({"error": "Your phone number is required"}), 400
    # find a customer-role user to act as created_by (fallback: any active customer user, then system)
    creator = c.execute(
        "SELECT id FROM users WHERE role='customer' AND customer_id=? AND active=1 LIMIT 1",
        (row["customer_id"],)).fetchone()
    if not creator:
        creator = c.execute("SELECT id FROM users WHERE role='admin' AND active=1 LIMIT 1").fetchone()
    created_by = creator["id"] if creator else 1
    equip_id = row["equipment_id"] or body.get("equipment_id") or None
    eq_cust, eq_loc, eq_dept = equipment_scope(c, equip_id)
    code_ = next_code_for("complaints", "CMP")
    cur = c.execute(
        "INSERT INTO complaints (code,customer_id,equipment_id,location_id,department_id,subject,description,category,priority,status,created_by,assigned_to,created_at,updated_at,reporter_name,reporter_phone) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code_, row["customer_id"], equip_id, eq_loc, eq_dept, subject, body.get("description", ""),
         body.get("category", "General"), body.get("priority", "medium"), "open",
         created_by, None, now(), now(), reporter_name, reporter_phone),
    )
    c.commit()
    newrow = c.execute("SELECT * FROM complaints WHERE id=?", (cur.lastrowid,)).fetchone()
    out = complaint_payload(c, newrow)
    maker = c.execute("SELECT name FROM users WHERE id=?", (created_by,)).fetchone()
    creator_name = maker["name"] if maker else ""
    c.close()
    audit("complaint", out["id"], {"id": created_by, "name": creator_name}, "created", f"Opened {out['code']} via portal — {out['subject']}")
    _archive_and_purge("complaint")
    # notify team
    ping_team("complaint", created_by, out,
              f"New complaint {out['code']} via portal: {out['subject']}",
              lambda r: email_mod.email_complaint_created(r, out))
    return jsonify(out), 201


@app.get("/api/portal/<token>/history")
def portal_history(token):
    c = conn()
    row = c.execute("SELECT * FROM portal_links WHERE token=? AND active=1", (token,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Invalid or expired link"}), 404
    rows = c.execute(
        "SELECT id, code, subject, status, priority, created_at, equipment_id FROM complaints "
        "WHERE customer_id=? ORDER BY created_at DESC LIMIT 20", (row["customer_id"],)).fetchall()
    c.close()
    return jsonify(rows_to_dicts(rows))


# --------------------------------------------------------------------------
# Static (mobile web app)
# --------------------------------------------------------------------------
@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/<path:path>")
def static_files(path):
    full = os.path.join(STATIC_DIR, path)
    if os.path.isfile(full):
        return send_from_directory(STATIC_DIR, path)
    return send_from_directory(STATIC_DIR, "index.html")


# --------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    from seed import seed
    seed()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
