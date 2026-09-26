"""LabSynch — REST API (Flask)."""
import os
import sys
import json
import uuid
import threading
import mimetypes

# allow running as `python server/app.py` or as a package
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, request, jsonify, g, send_from_directory, Response, make_response
from flask_cors import CORS

import mailer as email_mod
import report as report_mod
import push as push_mod
import apppush as apppush_mod
from database import conn, now, now_dt, hash_password, init_db, next_code_for, rows_to_dicts

app = Flask(__name__, static_folder=None)
CORS(app)

# The one, single Master System Admin. Everything "master-only" is decided by
# comparing the actor against this account, not by the bare shape of the record,
# so an accidental second unbound admin can never gain master powers.
MASTER_ADMIN_EMAIL = "admin@labcare.com"
# Reserved account that absorbs references from deleted users: immutable
# history columns (complaints.created_by, breakdowns.reported_by, comments,
# pm_logs) are NOT NULL with enforced FKs, so they cannot simply
# be unlinked — they are reassigned to this inert placeholder. It can never
# log in, is hidden from user lists, and cannot itself be deleted.
FORMER_USER_EMAIL = "former-user@labcare.invalid"

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
      log file (with their comments + audit trail) and then
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
        c.execute("DELETE FROM audit_logs WHERE entity_type=? AND entity_id=?", (entity, r["id"]))
        c.execute(f"DELETE FROM {table} WHERE id=?", (r["id"],))
    c.commit()
    c.close()
    return archived

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static")
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ROLE_LABELS = {"admin": "Admin", "engineer": "Engineer", "application": "Application", "customer": "Customer"}

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


def is_master_admin(u):
    """True only for the one Master System Admin account (admin@labcare.com).

    Keeping this identity-bound (rather than just "admin with no customer") means
    a second, accidental unbound admin can never act as the master."""
    return (u.get("role") == "admin"
            and not u.get("customer_id")
            and (u.get("email") or "").strip().lower() == MASTER_ADMIN_EMAIL)


def is_tenant_admin(u):
    """True for any administrator who is not the one Master System Admin.

    A tenant admin may be created WITHOUT a linked customer (unbound): after
    first login they create their own organization, which lands in their care
    list; until then their scope is an empty list (they see nothing)."""
    return u.get("role") == "admin" and not is_master_admin(u)


def _tenant_admin_ids(c, exclude_id=None):
    """Ids of every active tenant admin (the master is identified by email).

    Used when a join request creates a brand-new organization: all of them are
    asked whether it is under their care, and the first to claim it wins."""
    rows = c.execute(
        "SELECT id FROM users WHERE role='admin' AND active=1 AND lower(email)<>?",
        (MASTER_ADMIN_EMAIL,)).fetchall()
    return [r["id"] for r in rows if r["id"] != exclude_id]


def _other_admin_ids(c, exclude_id=None):
    """Ids of every active admin except one — the master included.

    Used to tell the others that a pending organization has been settled, so
    their notification lists stop offering an action that is no longer open."""
    rows = c.execute("SELECT id FROM users WHERE role='admin' AND active=1").fetchall()
    return [r["id"] for r in rows if r["id"] != exclude_id]


def _admin_scope_ids(c, admin_id, user_row=None):
    """Customer ids a tenant admin cares for (primary + linked), primary first.

    Reads the admin_customer_links join table; the admin's own customers.customer_id
    (its PRIMARY customer) is always included.

    `user_row` may be a dict or a sqlite3.Row."""
    primary = None
    if user_row is not None:
        primary = dict(user_row).get("customer_id")
    else:
        row = c.execute("SELECT customer_id FROM users WHERE id=?", (admin_id,)).fetchone()
        primary = row["customer_id"] if row else None
    ids = [primary] if primary else []
    rows = c.execute("SELECT customer_id FROM admin_customer_links WHERE admin_id=?",
                     (admin_id,)).fetchall()
    linked = [r["customer_id"] for r in rows if r["customer_id"] not in ids]
    return ids + linked


def scoped_customer_id(u):
    """The PRIMARY customer an admin/engineer is bound to, else None.

    Master admin and provider engineers have customer_id NULL and are unscoped."""
    if u.get("role") in ("admin", "engineer", "application"):
        return u.get("customer_id") or None
    return None


def tenant_scope(u, c):
    """The customer ids a tenant-scoped staff member may access, or None.

    * Tenant admin: their PRIMARY customer plus any added to their care list.
    * Tenant engineer: their single customer.
    * Engineer/application with NO customer but a linked tenant admin: that
      admin's care list — the account belongs to the tenant, not to one
      organization, so it must not become system-wide.
    * Master admin / provider engineer (no customer, no tenant admin): None =
      unscoped (all)."""
    if not u:
        return None
    if u.get("role") == "admin":
        if is_master_admin(u):
            return None
        # tenant admin — empty list when they have no organizations yet
        return _admin_scope_ids(c, u["id"], u)
    if u.get("role")  in ("engineer", "application"):
        if u.get("customer_id"):
            return [u["customer_id"]]
        # No organization of their own: they were placed under a tenant admin's
        # care, so they inherit exactly that admin's care list. Deliberately NOT
        # unscoped — otherwise a tenant admin could mint an account that sees
        # every organization on the system. The master's own LabSynch-wide staff
        # have no link (or are linked to the master) and stay unscoped.
        ra = u.get("responsible_admin_id")
        if ra:
            row = c.execute("SELECT * FROM users WHERE id=?", (ra,)).fetchone()
            if row and row["role"] == "admin" and not is_master_admin(dict(row)):
                return _admin_scope_ids(c, row["id"], row)
        return None
    return None


def in_scope(col, scope):
    """Return (sql, params) appending `col IN (?, …)` for a scope list."""
    marks = ", ".join("?" for _ in scope)
    return f"{col} IN ({marks})", list(scope)


def customer_scope_filter(c, u, col="customer_id"):
    """Return (sql_where, params) limiting a query to the actor's customers.

    * Customer users -> their one organization.
    * Tenant engineer -> its one organization.
    * Engineer with no organization -> the care list of the tenant admin they
      are linked to (empty list = they see nothing, never everything).
    * Tenant admin -> every organization in their care list.
    * Master / LabSynch-wide engineer -> no restriction ("", []).
    """
    role = u.get("role")
    if role == "customer":
        cid = u.get("customer_id")
        return (f"{col} = ?", [cid]) if cid else ("", [])
    if role  in ("engineer", "application"):
        cid = u.get("customer_id")
        if cid:
            return (f"{col} = ?", [cid])
        # Customer-less staff: mirror tenant_scope rather than falling through to
        # "no restriction", which would hand them every organization system-wide.
        scope = tenant_scope(u, c)
        if scope is None:
            return "", []
        if not scope:
            return "0=1", []
        return in_scope(col, scope)
    # admin
    if is_master_admin(u):
        return "", []
    scope = tenant_scope(u, c)
    if scope is None:
        return "", []
    if not scope:
        # tenant admin with no organizations yet sees nothing (not everything)
        return "0=1", []
    return in_scope(col, scope)


def require_master():
    """Admin auth plus an identity check: only the Master System Admin
    (admin@labcare.com) may manage customers, categories, onboarding requests or
    admin accounts."""
    u = auth_user()
    if not u:
        return None, jsonify({"error": "Not authenticated"}), 401
    if not is_master_admin(u):
        return None, jsonify({"error": "Only the master administrator can perform this action"}), 403
    return u, None, None


def tenant_guard(u, customer_id, c=None):
    """Reject tenant-scoped staff touching a customer outside their care list.
    Returns (error_json, code) or (None, None)."""
    # Normalize customer_id to int for comparison (frontend sends string)
    try:
        cid_int = int(customer_id) if customer_id is not None and str(customer_id).strip() != "" else None
    except (ValueError, TypeError):
        cid_int = None
    # For non-admin roles, compare against their bound customer
    if u.get("role") != "admin":
        bound = scoped_customer_id(u)
        # Normalize bound too
        try:
            bound_int = int(bound) if bound is not None else None
        except (ValueError, TypeError):
            bound_int = bound
        if bound_int is not None and cid_int is not None and cid_int != bound_int:
            return jsonify({"error": "Not authorised — this record belongs to another organization"}), 403
        # If bound is None (unscoped engineer), allow
        return None, None
    if is_master_admin(u):
        return None, None
    own = c is None
    if own:
        c = conn()
    try:
        scope = tenant_scope(u, c)
        # Normalize scope to ints
        scope_ints = []
        if scope:
            for s in scope:
                try:
                    scope_ints.append(int(s))
                except (ValueError, TypeError):
                    scope_ints.append(s)
        # Check both original and int version for safety
        if scope_ints:
            if cid_int is not None and cid_int not in scope_ints and customer_id not in scope_ints and str(customer_id) not in [str(x) for x in scope_ints]:
                # Also check original scope for backward compat
                if customer_id not in (scope or []) and cid_int not in (scope or []):
                    return jsonify({"error": "Not authorised — this record belongs to another organization"}), 403
        else:
            # Empty scope -> tenant admin with no orgs yet
            if cid_int is not None:
                # Allow if they are trying to create first org? No, locations require existing org, so block
                # But we check scope empty -> they have nothing, so any customer_id is not allowed
                # However we already handled scope empty case below
                pass
            if not scope:
                # If scope is empty list, they have no organizations yet -> cannot create location for any org
                # But if scope is None (master), we already returned
                # For empty list, block any attempt
                if scope == []:
                    return jsonify({"error": "Not authorised — this record belongs to another organization"}), 403
        # Final check with original scope for safety
        if scope is not None and scope != []:
            if customer_id not in scope and (cid_int not in scope if cid_int is not None else True):
                # If still not found, try string comparison
                if str(customer_id) not in [str(x) for x in scope]:
                    return jsonify({"error": "Not authorised — this record belongs to another organization"}), 403
        return None, None
    finally:
        if own:
            c.close()


def assignee_allowed(u, assignee_id, c):
    """Can the actor assign work to assignee_id?

    Master/admin-without-customer may assign anyone. Tenant-scoped staff may only
    assign their own team (tenant staff across their care-list customers) or the
    provider's (unbound) engineers — never users from other customers and never
    the master admin."""
    if not assignee_id:
        return True
    scope = tenant_scope(u, c)
    if scope is None:
        return True            # master / provider engineer actor
    if not scope:
        return False           # tenant admin with no organizations yet
    row = c.execute("SELECT role, customer_id FROM users WHERE id=?", (assignee_id,)).fetchone()
    if not row or row["role"] not in ("engineer", "application", "admin"):
        return False
    if row["customer_id"] is None:
        return row["role"]  in ("engineer", "application")
    return row["customer_id"] in scope


def validate_responsible_admin(c, customer_id, responsible_admin_id, customerless_user=False):
    """Validate an explicit "responsible tenant admin" for a record.

    The value must be an active account with role=admin, and that admin must be
    linked to the record's customer (primary or care list) — or be LabSynch-wide
    when the record itself is customer-less.

    `customerless_user` relaxes that last rule for USER ACCOUNTS only: a staff
    account with no organization is placed under a tenant admin's care, so a
    bound tenant admin is exactly the right link (their care list becomes the
    account's scope). Tickets and equipment keep the strict LabSynch-wide rule.
    Returns (error_json, code) on failure, or (None, None) when valid/empty."""
    if responsible_admin_id in (None, "", 0, "0"):
        return None, None
    row = c.execute("SELECT * FROM users WHERE id=?", (responsible_admin_id,)).fetchone()
    if not row:
        return jsonify({"error": "Responsible tenant admin not found"}), 400
    if row["active"] != 1:
        return jsonify({"error": "Responsible tenant admin account is disabled"}), 400
    if row["role"] != "admin":
        return jsonify({"error": "Responsible account must be an administrator"}), 400
    if customer_id is None:
        if row["customer_id"] is not None and not customerless_user:
            return jsonify({"error": "A LabSynch-wide record needs a LabSynch-wide administrator"}), 400
    else:
        admin_custs = _admin_scope_ids(c, row["id"], row)
        if customer_id not in admin_custs:
            return jsonify({"error": "Responsible tenant admin does not care for the selected organization"}), 400
    return None, None


def _responsible_admin_name(c, responsible_admin_id):
    """Name of a record's responsible tenant admin (or None)."""
    if not responsible_admin_id:
        return None
    row = c.execute("SELECT name FROM users WHERE id=?", (responsible_admin_id,)).fetchone()
    return row["name"] if row else None


def _customer_tenant_admin_id(c, customer_id):
    """The id of a customer's tenant admin when it is unambiguous (exactly one
    active tenant admin for that customer), else None."""
    ids = _customer_tenant_admin_ids(c, customer_id)
    return ids[0] if len(ids) == 1 else None


def _customer_tenant_admin_ids(c, customer_id):
    """Ids of a customer's active tenant admins (primary or care-list linked)."""
    if not customer_id:
        return []
    rows = c.execute(
        "SELECT id FROM users WHERE role='admin' AND customer_id=? AND active=1",
        (customer_id,)).fetchall()
    ids = [r["id"] for r in rows]
    for r in c.execute(
            "SELECT l.admin_id AS id FROM admin_customer_links l JOIN users u ON u.id=l.admin_id "
            "WHERE l.customer_id=? AND u.role='admin' AND u.active=1", (customer_id,)).fetchall():
        if r["id"] not in ids:
            ids.append(r["id"])
    return ids


def resolve_responsible_admin(c, u, customer_id, provided, customerless_user=False):
    """Resolve and validate the responsible tenant admin for a new/updated record.

    * Tenant admins default to themselves; tenant engineers defer to their
      customer's tenant admin.
    * The master / provider staff: auto-fill the customer's tenant admin when it
      is unambiguous; when a customer has several tenant admins one MUST be
      chosen explicitly (enforced), otherwise the field may stay empty.
    * Customer users may leave it empty (their admin is resolved later).
    Returns (ra_id, error_json, code).
    """
    ra_id = provided or None
    if not ra_id:
        if is_tenant_admin(u):
            # a tenant admin performing the action is responsible for anything
            # they set up themselves — for their primary customer and every
            # customer on their care list
            scope = tenant_scope(u, c)
            if customer_id is None or scope is None or customer_id in scope:
                ra_id = u["id"]
        elif customer_id:
            ra_id = _customer_tenant_admin_id(c, customer_id)
    if not ra_id and customer_id and u["role"] != "customer":
        if len(_customer_tenant_admin_ids(c, customer_id)) > 1:
            return None, jsonify({
                "error": "This organization has several tenant admins — please choose the responsible one"}), 400
    err, code = validate_responsible_admin(c, customer_id, ra_id, customerless_user)
    if err:
        return None, err, code
    return ra_id, None, None


def public_user(u, c=None):
    """Strip password hash before returning user object.

    For tenant admins, also expose customer_ids (primary + care list) so the
    frontend can scope pickers without extra round-trips."""
    u = dict(u)
    u.pop("password_hash", None)
    if is_tenant_admin(u) and c is not None:
        u["customer_ids"] = _admin_scope_ids(c, u["id"], u)
    return u


def _user_contact(c, user_id):
    if not user_id:
        return None
    row = c.execute("SELECT name, phone, email FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def complaint_payload(c, row):
    d = dict(row)
    cust = c.execute("SELECT name FROM customers WHERE id=?", (d["customer_id"],)).fetchone()
    eq = c.execute("SELECT name, model, serial_number FROM equipment WHERE id=?",
                   (d["equipment_id"],)).fetchone() if d.get("equipment_id") else None
    creator = _user_contact(c, d.get("created_by"))
    assignee = _user_contact(c, d.get("assigned_to"))
    d["customer_name"] = cust["name"] if cust else None
    d["location_name"] = _name_of(c, "locations", d.get("location_id"))
    d["department_name"] = _name_of(c, "departments", d.get("department_id"))
    d["equipment_name"] = f"{eq['name']} — {eq['model']}" if eq and eq["model"] else (eq["name"] if eq else None)
    d["equipment_serial"] = eq["serial_number"] if eq else None
    d["equipment_model"] = eq["model"] if eq else None
    d["created_by_name"] = creator["name"] if creator else None
    d["created_by_phone"] = (creator.get("phone") or "") if creator else ""
    d["created_by_email"] = (creator.get("email") or "") if creator else ""
    d["assigned_to_name"] = assignee["name"] if assignee else None
    d["assigned_to_phone"] = (assignee.get("phone") or "") if assignee else ""
    # portal submissions record who actually reported the issue
    d["reporter_name"] = d.get("reporter_name") or ""
    d["reporter_phone"] = d.get("reporter_phone") or ""
    d["responsible_admin_name"] = _responsible_admin_name(c, d.get("responsible_admin_id"))
    ra = _user_contact(c, d.get("responsible_admin_id"))
    d["responsible_admin_phone"] = (ra.get("phone") or "") if ra else ""
    closer = _user_contact(c, d.get("closed_by"))
    d["closed_by_name"] = closer["name"] if closer else None
    d["closed_by_phone"] = (closer.get("phone") or "") if closer else ""
    acceptor = _user_contact(c, d.get("accepted_by"))
    d["accepted_by_name"] = acceptor["name"] if acceptor else None
    d["accepted_by_phone"] = (acceptor.get("phone") or "") if acceptor else ""
    d["accepted_by_email"] = (acceptor.get("email") or "") if acceptor else ""
    return d


def breakdown_payload(c, row):
    d = dict(row)
    cust = c.execute("SELECT name FROM customers WHERE id=?", (d["customer_id"],)).fetchone()
    eq = c.execute("SELECT name, model, serial_number FROM equipment WHERE id=?",
                   (d["equipment_id"],)).fetchone() if d.get("equipment_id") else None
    reporter = _user_contact(c, d.get("reported_by"))
    assignee = _user_contact(c, d.get("assigned_to"))
    d["customer_name"] = cust["name"] if cust else None
    d["location_name"] = _name_of(c, "locations", d.get("location_id"))
    d["department_name"] = _name_of(c, "departments", d.get("department_id"))
    d["equipment_name"] = f"{eq['name']} — {eq['model']}" if eq and eq["model"] else (eq["name"] if eq else None)
    d["equipment_serial"] = eq["serial_number"] if eq else None
    d["equipment_model"] = eq["model"] if eq else None
    d["reported_by_name"] = reporter["name"] if reporter else None
    d["reported_by_phone"] = (reporter.get("phone") or "") if reporter else ""
    d["reported_by_email"] = (reporter.get("email") or "") if reporter else ""
    d["assigned_to_name"] = assignee["name"] if assignee else None
    d["assigned_to_phone"] = (assignee.get("phone") or "") if assignee else ""
    d["reporter_name"] = d.get("reporter_name") or ""
    d["reporter_phone"] = d.get("reporter_phone") or ""
    acceptor = _user_contact(c, d.get("accepted_by"))
    d["accepted_by_name"] = acceptor["name"] if acceptor else None
    d["accepted_by_phone"] = (acceptor.get("phone") or "") if acceptor else ""
    d["accepted_by_email"] = (acceptor.get("email") or "") if acceptor else ""
    d["responsible_admin_name"] = _responsible_admin_name(c, d.get("responsible_admin_id"))
    ra = _user_contact(c, d.get("responsible_admin_id"))
    d["responsible_admin_phone"] = (ra.get("phone") or "") if ra else ""
    closer = _user_contact(c, d.get("closed_by"))
    d["closed_by_name"] = closer["name"] if closer else None
    d["closed_by_phone"] = (closer.get("phone") or "") if closer else ""
    return d


def get_body():
    return request.get_json(force=True, silent=True) or {}


def _id(v):
    """Coerce a JSON-supplied foreign key to int, or None when unset.

    Browsers send <select> values as strings, while these ids are compared
    against integer care-list scopes. Without this, an organization a tenant
    admin legitimately picked reads as "not an organization you care for" and the
    request is rejected. "", None, 0 and "0" all mean "not set"."""
    if v in (None, "", 0, "0"):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _staff_in_tenant_care(c, admin_id, user_row, scope):
    """True when a customer-less staff account belongs to this tenant admin.

    An engineer/application account with no organization of its own is owned by
    the tenant admin named in its responsible_admin_id. The actor may manage it
    when they ARE that admin, or when that admin is a peer who cares for at least
    one organization the actor also cares for."""
    row = dict(user_row)
    if row.get("role") not in ("engineer", "application"):
        return False
    ra = row.get("responsible_admin_id")
    if not ra:
        return False
    if ra == admin_id:
        return True
    ar = c.execute("SELECT * FROM users WHERE id=?", (ra,)).fetchone()
    if not ar or ar["role"] != "admin":
        return False
    return bool(set(_admin_scope_ids(c, ra, ar)) & set(scope or []))


def is_tech_user(u):
    return u["role"] in ("engineer", "application", "admin")


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
            return jsonify({"error": "Location does not belong to the selected organization"}), 400
    if department_id:
        dept = c.execute("SELECT customer_id, location_id FROM departments WHERE id=?", (department_id,)).fetchone()
        if not dept or dept["customer_id"] != customer_id:
            return jsonify({"error": "Department does not belong to the selected organization"}), 400
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
    """Create an in-app notification, a Web Push (so the device rings even when
    the app/tab is closed), and optionally queue an email."""
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
    # Web Push: every bell notification also rings on the recipient's device,
    # even with the app/tab closed. Sent on a background daemon thread so a
    # slow or unreachable push service never delays the API response, and any
    # failure is logged + dropped silently by the send.
    try:
        threading.Thread(
            target=push_mod.send_push,
            args=(user_id, text, entity_type, entity_id),
            daemon=True,
        ).start()
    except Exception:
        pass
    # Native app push (Firebase FCM): rings on the phone even with the browser
    # closed or the phone locked. Best-effort; skipped when FCM isn't configured.
    try:
        threading.Thread(
            target=apppush_mod.send_app_push,
            args=(user_id, text, entity_type, entity_id),
            daemon=True,
        ).start()
    except Exception:
        pass
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
        cust = rec.get("customer_id")
        for r in c.execute(
                "SELECT id, email, customer_id, role FROM users WHERE role IN ('engineer','application','admin') AND active=1").fetchall():
            if cust is None or r["customer_id"] == cust:
                ids.add(r["id"])
                continue
            if r["customer_id"] is None:
                # provider engineers hear everything; among customer-less
                # admins only the MASTER does — unbound tenant admins rely
                # solely on their care list (checked below).
                if r["role"]  in ("engineer", "application") or (r["email"] or "").strip().lower() == MASTER_ADMIN_EMAIL:
                    ids.add(r["id"])
                    continue
            # tenant admins also hear tickets for customers they care for
            # via admin_customer_links (not just their primary customer)
            if r["role"] == "admin":
                link = c.execute(
                    "SELECT 1 FROM admin_customer_links WHERE admin_id=? AND customer_id=?",
                    (r["id"], cust)).fetchone()
                if link:
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
    user_payload = public_user(row, c)
    if user_payload.get("customer_ids"):
        q = "SELECT id, name FROM customers WHERE id IN ({})".format(
            ", ".join("?" for _ in user_payload["customer_ids"]))
        user_payload["care_customers"] = rows_to_dicts(
            c.execute(q, user_payload["customer_ids"]).fetchall())
        user_payload["customer_ids"] = [x["id"] for x in user_payload["care_customers"]]
    else:
        user_payload["care_customers"] = []
    resp = make_response(jsonify({"token": token, "user": user_payload}))
    resp.set_cookie(
        "labcare_token", token,
        max_age=30 * 24 * 3600,  # 30 days
        httponly=True,
        samesite="Lax",
        # Behind an HTTPS reverse proxy set LABCARE_SECURE_COOKIES=1 so the
        # browser only ever sends the session cookie over TLS.
        secure=os.environ.get("LABCARE_SECURE_COOKIES") == "1",
    )
    c.close()
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
    c = conn()
    d = public_user(u, c)
    d["customer_name"] = _name_of(c, "customers", u.get("customer_id"))
    d["location_name"] = _name_of(c, "locations", u.get("location_id"))
    d["department_name"] = _name_of(c, "departments", u.get("department_id"))
    cust_rows = []
    if d.get("customer_ids"):
        q = "SELECT id, name FROM customers WHERE id IN ({})".format(
            ", ".join("?" for _ in d["customer_ids"]))
        cust_rows = rows_to_dicts(c.execute(q, d["customer_ids"]).fetchall())
        d["customer_ids"] = [x["id"] for x in cust_rows]
    d["care_customers"] = cust_rows
    c.close()
    return jsonify(d)


@app.get("/api/my-customers")
def my_customers():
    """The customers a tenant admin cares for (primary + linked).

    Tenant admins self-select extra customers from the full customer directory;
    their primary customer is always included and cannot be removed."""
    u, err, code = require_role("admin")
    if err:
        return err, code
    c = conn()
    if is_master_admin(u):
        c.close()
        return jsonify({"error": "Master admin already manages every organization"}), 400
    scope = tenant_scope(u, c)
    ids = scope or []
    rows = c.execute(
        "SELECT id, name FROM customers WHERE id IN ({}) ORDER BY name".format(
            ", ".join("?" for _ in ids)), ids).fetchall()
    c.close()
    return jsonify({"customer_ids": [r["id"] for r in rows],
                    "primary_id": u.get("customer_id"),
                    "customers": rows_to_dicts(rows)})


@app.post("/api/my-customers/<int:cid>")
def add_my_customer(cid):
    """A tenant admin self-selects an extra customer to care for.

    The customer must exist; the tenancy is recorded in admin_customer_links."""
    u, err, code = require_role("admin")
    if err:
        return err, code
    c = conn()
    cust = c.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
    if not cust:
        c.close()
        return jsonify({"error": "Organization not found"}), 404
    c.execute(
        "INSERT OR IGNORE INTO admin_customer_links (admin_id, customer_id, created_at) VALUES (?,?,?)",
        (u["id"], cid, now()))
    c.commit()
    c.close()
    return jsonify({"ok": True})


@app.delete("/api/my-customers/<int:cid>")
def remove_my_customer(cid):
    """Remove a customer from the tenant admin's care list.

    The primary customer cannot be removed (it is the admin's own organization)."""
    u, err, code = require_role("admin")
    if err:
        return err, code
    if u.get("customer_id") == cid:
        return jsonify({"error": "Your primary organization cannot be removed"}), 400
    c = conn()
    c.execute("DELETE FROM admin_customer_links WHERE admin_id=? AND customer_id=?", (u["id"], cid))
    c.commit()
    c.close()
    return jsonify({"ok": True})


@app.get("/api/customers/pending-care")
def pending_care_list():
    """Organizations created by a join request that nobody has claimed yet.

    Every tenant admin sees them, minus the ones they already answered "not
    mine" — a decline only hides it from that admin, so the others and the
    master can still act. The master sees all of them plus the tenant admins
    available to take an assignment."""
    u, err, code = require_role("admin")
    if err:
        return err, code
    c = conn()
    where, params = "WHERE cu.pending_care=1", []
    if not is_master_admin(u):
        where += (" AND cu.id NOT IN "
                  "(SELECT customer_id FROM pending_care_declines WHERE admin_id=?)")
        params.append(u["id"])
    rows = c.execute(
        "SELECT cu.id, cu.name, cu.contact_name, cu.email, cu.phone, cu.city, cu.created_at, "
        "(SELECT a.name FROM onboarding_apps a WHERE a.customer_id=cu.id ORDER BY a.id DESC LIMIT 1) "
        "AS requested_by, "
        "(SELECT a.email FROM onboarding_apps a WHERE a.customer_id=cu.id ORDER BY a.id DESC LIMIT 1) "
        "AS requested_by_email, "
        "(SELECT COUNT(*) FROM pending_care_declines d WHERE d.customer_id=cu.id) AS declines "
        f"FROM customers cu {where} ORDER BY cu.created_at DESC", params).fetchall()
    out = rows_to_dicts(rows)
    resp = {"customers": out}
    if is_master_admin(u):
        resp["tenant_admins"] = rows_to_dicts(c.execute(
            "SELECT id, name, email, customer_id FROM users "
            "WHERE role='admin' AND active=1 AND lower(email)<>? ORDER BY name",
            (MASTER_ADMIN_EMAIL,)).fetchall())
    c.close()
    return jsonify(resp)


@app.post("/api/customers/<int:cid>/take-care")
def take_care(cid):
    """A tenant admin claims a join-request organization: first to claim wins.

    Once claimed it leaves the pending list for everyone at the same moment, so
    two admins cannot both take it. After that the organization behaves like any
    other and the usual shared-care rules apply again."""
    u, err, code = require_role("admin")
    if err:
        return err, code
    if not is_tenant_admin(u):
        return jsonify({"error": "Only a tenant admin can take an organization into their care"}), 403
    c = conn()
    cust = c.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
    if not cust:
        c.close()
        return jsonify({"error": "Organization not found"}), 404
    if not cust["pending_care"]:
        c.close()
        return jsonify({"error": "This organization is no longer awaiting a care decision"}), 409
    # The guarded UPDATE is what makes the claim exclusive: whoever reaches it
    # first flips the flag, and everyone else matches zero rows.
    cur = c.execute("UPDATE customers SET pending_care=0 WHERE id=? AND pending_care=1", (cid,))
    if (cur.rowcount or 0) < 1:
        c.close()
        return jsonify({"error": "Another tenant admin took this organization first"}), 409
    c.execute("INSERT OR IGNORE INTO admin_customer_links (admin_id, customer_id, created_at) VALUES (?,?,?)",
              (u["id"], cid, now()))
    c.execute("DELETE FROM pending_care_declines WHERE customer_id=?", (cid,))
    c.commit()
    for aid in _other_admin_ids(c, exclude_id=u["id"]):
        notify(aid, f"{u['name']} took {cust['name']} into their care.", "pending_care", cid, None)
    c.close()
    return jsonify({"ok": True, "customer_id": cid})


@app.post("/api/customers/<int:cid>/decline-care")
def decline_care(cid):
    """A tenant admin says a join-request organization is not under their care.

    It leaves their list only — the other tenant admins and the master can still
    claim or assign it, so nobody can make a decision disappear for everyone."""
    u, err, code = require_role("admin")
    if err:
        return err, code
    if not is_tenant_admin(u):
        return jsonify({"error": "Only a tenant admin can decline an organization"}), 403
    c = conn()
    cust = c.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
    if not cust:
        c.close()
        return jsonify({"error": "Organization not found"}), 404
    if not cust["pending_care"]:
        c.close()
        return jsonify({"error": "This organization is no longer awaiting a care decision"}), 409
    c.execute("INSERT OR IGNORE INTO pending_care_declines (admin_id, customer_id, created_at) VALUES (?,?,?)",
              (u["id"], cid, now()))
    c.commit()
    # The master hears about it, since assigning it is now the likely next step.
    for r in c.execute("SELECT id FROM users WHERE role='admin' AND active=1 AND lower(email)=?",
                       (MASTER_ADMIN_EMAIL,)).fetchall():
        notify(r["id"], f"{u['name']} said {cust['name']} is not under their care.",
               "pending_care", cid, None)
    c.close()
    return jsonify({"ok": True})


@app.post("/api/customers/<int:cid>/assign-care")
def assign_care(cid):
    """The master decides which tenant admin looks after a join-request
    organization."""
    u, err, code = require_master()
    if err:
        return err, code
    b = get_body()
    admin_id = _id(b.get("admin_id"))
    if not admin_id:
        return jsonify({"error": "Choose the tenant admin who will care for this organization"}), 400
    c = conn()
    cust = c.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
    if not cust:
        c.close()
        return jsonify({"error": "Organization not found"}), 404
    if not cust["pending_care"]:
        c.close()
        return jsonify({"error": "This organization is no longer awaiting a care decision"}), 409
    target = c.execute("SELECT * FROM users WHERE id=?", (admin_id,)).fetchone()
    # Rows are sqlite3.Row objects here (no .get), so the master check compares
    # the email directly rather than calling is_master_admin().
    if (not target or target["role"] != "admin" or not target["active"]
            or (target["email"] or "").strip().lower() == MASTER_ADMIN_EMAIL):
        c.close()
        return jsonify({"error": "Choose an active tenant admin"}), 400
    c.execute("UPDATE customers SET pending_care=0 WHERE id=?", (cid,))
    c.execute("INSERT OR IGNORE INTO admin_customer_links (admin_id, customer_id, created_at) VALUES (?,?,?)",
              (admin_id, cid, now()))
    c.execute("DELETE FROM pending_care_declines WHERE customer_id=?", (cid,))
    c.commit()
    notify(admin_id, f"The master assigned {cust['name']} to your care.", "pending_care", cid, None)
    for aid in _other_admin_ids(c, exclude_id=admin_id):
        if aid == u["id"]:
            continue
        notify(aid, f"{cust['name']} was assigned to {target['name']}.", "pending_care", cid, None)
    c.close()
    return jsonify({"ok": True, "admin_id": admin_id})


@app.get("/api/customer-directory")
def customer_directory():
    """Admin-only: every customer organization, flagged with whether the
    requesting tenant admin already cares for it (used by the self-select UI)."""
    u, err, code = require_role("admin")
    if err:
        return err, code
    c = conn()
    linked = set()
    if not is_master_admin(u):
        ids = tenant_scope(u, c) or []
        q = "SELECT id FROM customers WHERE id IN ({})".format(", ".join("?" for _ in ids))
        linked = set(r["id"] for r in c.execute(q, ids).fetchall())
    rows = c.execute("SELECT id, name FROM customers ORDER BY name").fetchall()
    c.close()
    out = []
    for r in rows:
        d = dict(r)
        d["linked"] = r["id"] in linked
        out.append(d)
    return jsonify(out)


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
    """Anyone can request a customer/engineer account; it stays pending until an admin approves."""
    b = get_body()
    name = (b.get("name") or "").strip()
    email = (b.get("email") or "").strip().lower()
    phone = (b.get("phone") or "").strip()
    pw = b.get("password") or ""
    role = b.get("role") or "customer"
    if not name or not email or not pw:
        return jsonify({"error": "Name, email and password are required"}), 400
    if not phone:
        return jsonify({"error": "A phone number is required"}), 400
    if len(pw) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    if role not in ("engineer", "application", "customer"):
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
    new_cust_name = (b.get("new_customer_name") or b.get("new_organization_name") or b.get("new_customer") or "").strip()
    new_loc_name = (b.get("new_location_name") or b.get("new_location") or "").strip()
    # Set when this signup creates an organization that did not exist before:
    # that one has no tenant admin yet, so it needs a care decision.
    new_org_created = False
    if role == "customer":
        if new_cust_name:
            existing_cust = c.execute(
                "SELECT id FROM customers WHERE lower(name)=?",
                (new_cust_name.lower(),)
            ).fetchone()
            if existing_cust:
                customer_id = existing_cust["id"]
            else:
                cur_cust = c.execute(
                    "INSERT INTO customers (name,contact_name,email,phone,address,city,pending_care,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (new_cust_name, name, email, phone, "", "", 1, now()),
                )
                customer_id = cur_cust.lastrowid
                new_org_created = True
            if location_id and not new_loc_name:
                ref_loc = c.execute("SELECT name FROM locations WHERE id=?", (location_id,)).fetchone()
                if ref_loc:
                    new_loc_name = ref_loc["name"]
                    location_id = None
                    department_id = None
        elif not customer_id:
            c.close()
            return jsonify({"error": "Select your organization or create a new one"}), 400

        if new_loc_name:
            # Location and department are unified: create or resolve location/department for this customer
            existing_loc = c.execute(
                "SELECT id FROM locations WHERE customer_id=? AND lower(name)=?",
                (customer_id, new_loc_name.lower())).fetchone()
            if existing_loc:
                location_id = existing_loc["id"]
                dept = c.execute("SELECT id FROM departments WHERE location_id=?", (location_id,)).fetchone()
                department_id = dept["id"] if dept else None
            else:
                cur_loc = c.execute(
                    "INSERT INTO locations (customer_id,name,address,city,created_at) VALUES (?,?,?,?,?)",
                    (customer_id, new_loc_name, "", "", now()),
                )
                location_id = cur_loc.lastrowid
                cur_dept = c.execute(
                    "INSERT INTO departments (customer_id,location_id,name,created_at) VALUES (?,?,?,?)",
                    (customer_id, location_id, new_loc_name, now()),
                )
                department_id = cur_dept.lastrowid
        elif new_cust_name and not location_id:
            cur_loc = c.execute(
                "INSERT INTO locations (customer_id,name,address,city,created_at) VALUES (?,?,?,?,?)",
                (customer_id, "Main Lab", "", "", now()),
            )
            location_id = cur_loc.lastrowid
            cur_dept = c.execute(
                "INSERT INTO departments (customer_id,location_id,name,created_at) VALUES (?,?,?,?)",
                (customer_id, location_id, "Main Lab", now()),
            )
            department_id = cur_dept.lastrowid
        else:
            if not location_id:
                c.close()
                return jsonify({"error": "Select your location/department or create a new one"}), 400
            if location_id and not department_id:
                dept = c.execute("SELECT id FROM departments WHERE location_id=?", (location_id,)).fetchone()
                if dept:
                    department_id = dept["id"]
            elif department_id and not location_id:
                loc = c.execute("SELECT location_id FROM departments WHERE id=?", (department_id,)).fetchone()
                if loc:
                    location_id = loc["location_id"]
            err_r, code_r = _validate_loc_dept(c, customer_id, location_id, department_id)
            if err_r:
                c.close()
                return err_r, code_r
    else:
        customer_id = location_id = department_id = None
    cur = c.execute(
        "INSERT INTO onboarding_apps (name,email,phone,password_hash,role,customer_id,location_id,department_id,status,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (name, email, phone, hash_password(pw), role, customer_id, location_id, department_id, "pending", now()),
    )
    c.commit()
    # Notify the master admin that a new joiner is waiting for approval
    # (tenant admins do not review onboarding requests).
    for r in c.execute("SELECT id FROM users WHERE role='admin' AND active=1 AND lower(email)=?",
                       (MASTER_ADMIN_EMAIL,)).fetchall():
        notify(r["id"], f"New join request from {name} ({email}) is awaiting your approval.",
               "onboarding", cur.lastrowid, None)
    # A brand-new organization has no tenant admin yet, so every tenant admin
    # is asked whether it is under their care and the first to claim it wins.
    # The master is told as well, because they may assign it instead.
    if new_org_created:
        for aid in _tenant_admin_ids(c):
            notify(aid,
                   f"{name} ({email}) asked to join a new organization, {new_cust_name}. "
                   f"Is it under your care?",
                   "pending_care", customer_id, None)
        for r in c.execute("SELECT id FROM users WHERE role='admin' AND active=1 AND lower(email)=?",
                           (MASTER_ADMIN_EMAIL,)).fetchall():
            notify(r["id"],
                   f"{new_cust_name} is a new organization awaiting a care decision. "
                   f"You can assign it to a tenant admin.",
                   "pending_care", customer_id, None)
    c.close()
    return jsonify({"ok": True, "message": "Request submitted. An admin must approve it before you can sign in."}), 201


@app.get("/api/onboarding")
def list_onboarding():
    u, err, code = require_master()
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
    u, err, code = require_master()
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
    withdrawn = None
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
        # A rejected request must not leave its organization sitting in the
        # pending-care list for tenant admins to claim. The organization row
        # itself is kept: it already has a location and department, and a later
        # signup naming the same organization reuses it.
        if app["customer_id"]:
            pc = c.execute("SELECT id, name FROM customers WHERE id=? AND pending_care=1",
                           (app["customer_id"],)).fetchone()
            if pc:
                c.execute("UPDATE customers SET pending_care=0 WHERE id=?", (pc["id"],))
                c.execute("DELETE FROM pending_care_declines WHERE customer_id=?", (pc["id"],))
                withdrawn = pc["name"]
    c.commit()
    if withdrawn:
        for aid_ in _other_admin_ids(c, exclude_id=u["id"]):
            notify(aid_,
                   f"The join request for {withdrawn} was rejected, so it no longer needs a care decision.",
                   "pending_care", app["customer_id"], None)
    c.close()
    return jsonify({"ok": True, "decision": decision})


# --------------------------------------------------------------------------
# Customers
# --------------------------------------------------------------------------
@app.get("/api/customers")
def list_customers():
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    c = conn()
    if u["role"] == "customer":
        where, params = " WHERE id=?", [u.get("customer_id")]
    elif u["role"]  in ("engineer", "application") and u.get("customer_id"):
        where, params = " WHERE id=?", [u["customer_id"]]
    elif u["role"] == "admin" and not is_master_admin(u):
        scope = tenant_scope(u, c)
        if scope:
            where = " WHERE id IN ({})".format(", ".join("?" for _ in scope))
            params = list(scope)
        else:
            where, params = " WHERE 0=1", []   # unbound tenant admin: nothing yet
    else:
        where, params = "", []
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
    """Create a customer organization.

    The master may create any organization. A tenant admin may also create one;
    it is automatically added to that admin's care list so they can immediately
    set up locations, equipment, users and tickets for it."""
    u, err, code = require_role("admin")
    if err:
        return err, code
    b = get_body()
    if not (b.get("name") or "").strip():
        return jsonify({"error": "Organization name is required"}), 400
    c = conn()
    cur = c.execute(
        "INSERT INTO customers (name,contact_name,email,phone,address,city,created_at) VALUES (?,?,?,?,?,?,?)",
        (b["name"].strip(), b.get("contact_name", ""), b.get("email", ""), b.get("phone", ""),
         b.get("address", ""), b.get("city", ""), now()),
    )
    new_id = cur.lastrowid
    # A tenant admin who creates an organization automatically starts caring for
    # it (the master already manages every customer).
    if is_tenant_admin(u):
        c.execute(
            "INSERT OR IGNORE INTO admin_customer_links (admin_id, customer_id, created_at) VALUES (?,?,?)",
            (u["id"], new_id, now()),
        )
    c.commit()
    row = c.execute("SELECT * FROM customers WHERE id=?", (new_id,)).fetchone()
    c.close()
    return jsonify(dict(row)), 201


@app.put("/api/customers/<int:cid>")
def update_customer(cid):
    u, err, code = require_role("admin")
    if err:
        return err, code
    b = get_body()
    c = conn()
    if not is_master_admin(u):
        err_t, code_t = tenant_guard(u, cid, c)
        if err_t:
            c.close()
            return err_t, code_t
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
    if not is_master_admin(u):
        err_t, code_t = tenant_guard(u, cid, c)
        if err_t:
            c.close()
            return err_t, code_t
        if u.get("customer_id") == cid:
            c.close()
            return jsonify({"error": "You cannot delete your own primary organization"}), 400
    n_equip = c.execute("SELECT COUNT(*) n FROM equipment WHERE customer_id=?", (cid,)).fetchone()["n"]
    n_cmp = c.execute("SELECT COUNT(*) n FROM complaints WHERE customer_id=?", (cid,)).fetchone()["n"]
    n_brk = c.execute("SELECT COUNT(*) n FROM breakdowns WHERE customer_id=?", (cid,)).fetchone()["n"]
    n_loc = c.execute("SELECT COUNT(*) n FROM locations WHERE customer_id=?", (cid,)).fetchone()["n"]
    n_pm = c.execute("SELECT COUNT(*) n FROM pm_schedules WHERE customer_id=?", (cid,)).fetchone()["n"]
    n_portal = c.execute("SELECT COUNT(*) n FROM portal_links WHERE customer_id=?", (cid,)).fetchone()["n"]
    if n_equip or n_cmp or n_brk or n_loc or n_pm or n_portal:
        c.close()
        return jsonify({"error": "Organization has linked locations, equipment, tickets, PM schedules or portal links; cannot delete."}), 409
    c.execute("DELETE FROM customers WHERE id=?", (cid,))
    # drop any care-list links to the removed organization
    c.execute("DELETE FROM admin_customer_links WHERE customer_id=?", (cid,))
    c.commit()
    c.close()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Locations & departments (Location and department are unified)
# --------------------------------------------------------------------------
def _loc_payload(c, r):
    d = dict(r)
    dept = c.execute("SELECT id FROM departments WHERE location_id=?", (r["id"],)).fetchone()
    d["department_id"] = dept["id"] if dept else None
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
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    c = conn()
    where, params = [], []
    scoped_where, scoped_params = customer_scope_filter(c, u, "l.customer_id")
    if scoped_where:
        where.append(scoped_where)
        params += scoped_params
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
    u, err, code = require_role("admin", "engineer", "application")
    if err:
        return err, code
    b = get_body()
    if not (b.get("name") or "").strip() or not b.get("customer_id"):
        return jsonify({"error": "Location/department name and organization are required"}), 400
    # Normalize customer_id to int (frontend sends string)
    try:
        cust_id = int(b["customer_id"])
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid organization id"}), 400
    err_t, code_t = tenant_guard(u, cust_id)
    if err_t:
        return err_t, code_t
    c = conn()
    name = b["name"].strip()
    cur = c.execute(
        "INSERT INTO locations (customer_id,name,address,city,created_at) VALUES (?,?,?,?,?)",
        (cust_id, name, b.get("address", ""), b.get("city", ""), now()),
    )
    loc_id = cur.lastrowid
    # Location and department are the same: automatically create corresponding department
    c.execute(
        "INSERT INTO departments (customer_id,location_id,name,created_at) VALUES (?,?,?,?)",
        (cust_id, loc_id, name, now()),
    )
    c.commit()
    row = c.execute("SELECT l.*, cu.name AS customer_name FROM locations l JOIN customers cu ON cu.id=l.customer_id WHERE l.id=?",
                    (loc_id,)).fetchone()
    out = _loc_payload(c, row)
    c.close()
    return jsonify(out), 201


@app.put("/api/locations/<int:lid>")
def update_location(lid):
    u, err, code = require_role("admin", "engineer", "application")
    if err:
        return err, code
    b = get_body()
    c = conn()
    existing = c.execute("SELECT customer_id FROM locations WHERE id=?", (lid,)).fetchone()
    if not existing:
        c.close()
        return jsonify({"error": "Not found"}), 404
    err_t, code_t = tenant_guard(u, existing["customer_id"])
    if err_t:
        c.close()
        return err_t, code_t
    raw_new_cid = b.get("customer_id", existing["customer_id"])
    try:
        new_customer_id = int(raw_new_cid) if raw_new_cid is not None else existing["customer_id"]
    except (ValueError, TypeError):
        c.close()
        return jsonify({"error": "Invalid organization id"}), 400
    err_t, code_t = tenant_guard(u, new_customer_id)
    if err_t:
        c.close()
        return err_t, code_t
    name = b.get("name", "").strip()
    c.execute(
        "UPDATE locations SET name=?,address=?,city=?,customer_id=? WHERE id=?",
        (name, b.get("address", ""), b.get("city", ""), new_customer_id, lid),
    )
    # Location and department are the same: keep department in sync
    dept = c.execute("SELECT id FROM departments WHERE location_id=?", (lid,)).fetchone()
    if dept:
        c.execute("UPDATE departments SET name=?,customer_id=? WHERE id=?",
                  (name, new_customer_id, dept["id"]))
    else:
        c.execute("INSERT INTO departments (customer_id,location_id,name,created_at) VALUES (?,?,?,?)",
                  (new_customer_id, lid, name, now()))
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
    if is_tenant_admin(u):
        row = c.execute("SELECT customer_id FROM locations WHERE id=?", (lid,)).fetchone()
        if not row:
            c.close()
            return jsonify({"error": "Not found"}), 404
        err_t, code_t = tenant_guard(u, row["customer_id"], c)
        if err_t:
            c.close()
            return err_t, code_t
    # Location and department are the same: departments of this location are deleted with it
    n2 = c.execute("SELECT COUNT(*) n FROM equipment WHERE location_id=?", (lid,)).fetchone()["n"]
    n3 = c.execute("SELECT COUNT(*) n FROM complaints WHERE location_id=?", (lid,)).fetchone()["n"]
    n4 = c.execute("SELECT COUNT(*) n FROM breakdowns WHERE location_id=?", (lid,)).fetchone()["n"]
    n5 = c.execute("SELECT COUNT(*) n FROM users WHERE location_id=?", (lid,)).fetchone()["n"]
    if n2 or n3 or n4 or n5:
        c.close()
        return jsonify({"error": "Location/department has equipment, users or tickets; cannot delete."}), 409
    c.execute("DELETE FROM departments WHERE location_id=?", (lid,))
    c.execute("DELETE FROM locations WHERE id=?", (lid,))
    c.commit()
    c.close()
    return jsonify({"ok": True})


@app.get("/api/departments")
def list_departments():
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    c = conn()
    where, params = [], []
    scoped_where, scoped_params = customer_scope_filter(c, u, "d.customer_id")
    if scoped_where:
        where.append(scoped_where)
        params += scoped_params
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
    u, err, code = require_role("admin", "engineer", "application")
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
    err_t, code_t = tenant_guard(u, loc["customer_id"])
    if err_t:
        c.close()
        return err_t, code_t
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
    u, err, code = require_role("admin", "engineer", "application")
    if err:
        return err, code
    b = get_body()
    c = conn()
    existing = c.execute("SELECT customer_id, location_id FROM departments WHERE id=?", (did,)).fetchone()
    if not existing:
        c.close()
        return jsonify({"error": "Not found"}), 404
    err_t, code_t = tenant_guard(u, existing["customer_id"])
    if err_t:
        c.close()
        return err_t, code_t
    new_loc_id = b.get("location_id", existing["location_id"])
    new_loc = c.execute("SELECT customer_id FROM locations WHERE id=?", (new_loc_id,)).fetchone() if new_loc_id else None
    if new_loc_id and (not new_loc or new_loc["customer_id"] != existing["customer_id"]):
        c.close()
        return jsonify({"error": "Location does not belong to the selected organization"}), 400
    c.execute("UPDATE departments SET name=?,location_id=? WHERE id=?",
              (b.get("name", ""), new_loc_id, did))
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
    if is_tenant_admin(u):
        row = c.execute("SELECT customer_id FROM departments WHERE id=?", (did,)).fetchone()
        if not row:
            c.close()
            return jsonify({"error": "Not found"}), 404
        err_t, code_t = tenant_guard(u, row["customer_id"], c)
        if err_t:
            c.close()
            return err_t, code_t
    n = c.execute("SELECT COUNT(*) n FROM equipment WHERE department_id=?", (did,)).fetchone()["n"]
    n2 = c.execute("SELECT COUNT(*) n FROM complaints WHERE department_id=?", (did,)).fetchone()["n"]
    n3 = c.execute("SELECT COUNT(*) n FROM breakdowns WHERE department_id=?", (did,)).fetchone()["n"]
    if n or n2 or n3:
        c.close()
        return jsonify({"error": "Department has equipment or tickets; cannot delete."}), 409
    c.execute("DELETE FROM departments WHERE id=?", (did,))
    c.commit()
    c.close()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Equipment categories (admin-managed)
# --------------------------------------------------------------------------
@app.get("/api/categories")
def list_categories():
    """Every equipment category, so the Add equipment form always offers the
    full prepared list rather than only the categories already in use.

    The list is shared, but each caller's equipment_count tallies only equipment
    they may see — a tenant admin is not told how many instruments another
    tenant has in a category."""
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    c = conn()
    scope = tenant_scope(u, c)
    marks = ", ".join("?" for _ in scope) if scope else ""
    rows = c.execute("SELECT * FROM categories ORDER BY name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if scope:
            d["equipment_count"] = c.execute(
                f"SELECT COUNT(*) n FROM equipment WHERE category=? AND customer_id IN ({marks})",
                [r["name"]] + list(scope)).fetchone()["n"]
        else:
            d["equipment_count"] = c.execute(
                "SELECT COUNT(*) n FROM equipment WHERE category=?", (r["name"],)).fetchone()["n"]
        out.append(d)
    c.close()
    return jsonify(out)


@app.post("/api/categories")
def create_category():
    # Categories are global (not per customer), and anyone who may add equipment
    # may also add a category when the one they need is missing — otherwise they
    # would have to mislabel the instrument as "Other" and wait for the master.
    # Renaming and deleting stay master-only: those rewrite records belonging to
    # every tenant, while adding one can only lengthen a shared pick-list.
    u, err, code = require_role("admin", "engineer", "application")
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
    u, err, code = require_master()
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
    u, err, code = require_master()
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
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    c = conn()
    q = ("SELECT e.*, cu.name AS customer_name, l.name AS location_name, d.name AS department_name, "
         "ra.name AS responsible_admin_name "
         "FROM equipment e JOIN customers cu ON cu.id=e.customer_id "
         "LEFT JOIN locations l ON l.id=e.location_id "
         "LEFT JOIN departments d ON d.id=e.department_id "
         "LEFT JOIN users ra ON ra.id=e.responsible_admin_id")
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
        sw, sp = customer_scope_filter(c, u, "e.customer_id")
        if sw:
            where.append(sw)
            params += sp
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
    u, err, code = require_role("admin", "engineer", "application")
    if err:
        return err, code
    b = get_body()
    if not (b.get("name") or "").strip() or not b.get("customer_id"):
        return jsonify({"error": "Equipment name and organization are required"}), 400
    err_t, code_t = tenant_guard(u, b["customer_id"])
    if err_t:
        return err_t, code_t
    c = conn()
    loc_id = b.get("location_id") or None
    dept_id = b.get("department_id") or None
    if loc_id and not dept_id:
        dept = c.execute("SELECT id FROM departments WHERE location_id=?", (loc_id,)).fetchone()
        if dept:
            dept_id = dept["id"]
    elif dept_id and not loc_id:
        loc = c.execute("SELECT location_id FROM departments WHERE id=?", (dept_id,)).fetchone()
        if loc:
            loc_id = loc["location_id"]
    err_r, code_r = _validate_loc_dept(c, b["customer_id"], loc_id, dept_id)
    if err_r:
        c.close()
        return err_r, code_r
    ra_id, err_r, code_r = resolve_responsible_admin(c, u, b["customer_id"], b.get("responsible_admin_id"))
    if err_r:
        c.close()
        return err_r, code_r
    serial = (b.get("serial_number") or "").strip()
    if serial:
        dup = c.execute("SELECT id FROM equipment WHERE serial_number=?",
                        (serial,)).fetchone()
        if dup:
            c.close()
            return jsonify({"error": f"Serial number '{serial}' is already registered"}), 409
    cur = c.execute(
        "INSERT INTO equipment (customer_id,location_id,department_id,name,model,serial_number,category,installed_date,warranty_expiry,status,notes,responsible_admin_id,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (b["customer_id"], loc_id, dept_id,
         b["name"].strip(), b.get("model", ""), serial or None, b.get("category", ""),
         b.get("installed_date", ""), b.get("warranty_expiry", ""), b.get("status", "active"),
         b.get("notes", ""), ra_id, now()),
    )
    c.commit()
    row = c.execute(
        "SELECT e.*, cu.name AS customer_name, l.name AS location_name, d.name AS department_name, ra.name AS responsible_admin_name "
        "FROM equipment e JOIN customers cu ON cu.id=e.customer_id "
        "LEFT JOIN locations l ON l.id=e.location_id LEFT JOIN departments d ON d.id=e.department_id "
        "LEFT JOIN users ra ON ra.id=e.responsible_admin_id "
        "WHERE e.id=?",
        (cur.lastrowid,)).fetchone()
    c.close()
    return jsonify(dict(row)), 201


@app.put("/api/equipment/<int:eid>")
def update_equipment(eid):
    u, err, code = require_role("admin", "engineer", "application")
    if err:
        return err, code
    b = get_body()
    c = conn()
    existing = c.execute("SELECT * FROM equipment WHERE id=?", (eid,)).fetchone()
    if not existing:
        c.close()
        return jsonify({"error": "Not found"}), 404
    err_t, code_t = tenant_guard(u, existing["customer_id"])
    if err_t:
        c.close()
        return err_t, code_t
    customer_id = b.get("customer_id", existing["customer_id"])
    err_t, code_t = tenant_guard(u, customer_id)
    if err_t:
        c.close()
        return err_t, code_t
    loc_id = b.get("location_id", existing["location_id"])
    dept_id = b.get("department_id", existing["department_id"])
    if loc_id and not dept_id:
        dept = c.execute("SELECT id FROM departments WHERE location_id=?", (loc_id,)).fetchone()
        if dept:
            dept_id = dept["id"]
    elif dept_id and not loc_id:
        loc = c.execute("SELECT location_id FROM departments WHERE id=?", (dept_id,)).fetchone()
        if loc:
            loc_id = loc["location_id"]
    err_r, code_r = _validate_loc_dept(c, customer_id, loc_id, dept_id)
    if err_r:
        c.close()
        return err_r, code_r
    ra_id = b.get("responsible_admin_id", existing["responsible_admin_id"])
    err_r, code_r = validate_responsible_admin(c, customer_id, ra_id)
    if err_r:
        c.close()
        return err_r, code_r
    serial = (b.get("serial_number") or "").strip()
    if serial:
        dup = c.execute("SELECT id FROM equipment WHERE serial_number=? AND id!=?",
                        (serial, eid)).fetchone()
        if dup:
            c.close()
            return jsonify({"error": f"Serial number '{serial}' is already registered"}), 409
    c.execute(
        "UPDATE equipment SET customer_id=?,location_id=?,department_id=?,name=?,model=?,serial_number=?,category=?,installed_date=?,warranty_expiry=?,status=?,notes=?,responsible_admin_id=? WHERE id=?",
        (customer_id, loc_id, dept_id,
         (b.get("name") or "").strip() or existing["name"], b.get("model", ""), serial or None, b.get("category", ""),
         b.get("installed_date", ""), b.get("warranty_expiry", ""), b.get("status", "active"),
         b.get("notes", ""), ra_id, eid),
    )
    c.commit()
    row = c.execute(
        "SELECT e.*, cu.name AS customer_name, l.name AS location_name, d.name AS department_name, ra.name AS responsible_admin_name "
        "FROM equipment e JOIN customers cu ON cu.id=e.customer_id "
        "LEFT JOIN locations l ON l.id=e.location_id LEFT JOIN departments d ON d.id=e.department_id "
        "LEFT JOIN users ra ON ra.id=e.responsible_admin_id "
        "WHERE e.id=?",
        (eid,)).fetchone()
    c.close()
    return jsonify(dict(row)) if row else (jsonify({"error": "Not found"}), 404)


@app.delete("/api/equipment/<int:eid>")
def delete_equipment(eid):
    u, err, code = require_role("admin", "engineer", "application")
    if err:
        return err, code
    c = conn()
    existing = c.execute("SELECT customer_id FROM equipment WHERE id=?", (eid,)).fetchone()
    if not existing:
        c.close()
        return jsonify({"error": "Not found"}), 404
    err_t, code_t = tenant_guard(u, existing["customer_id"])
    if err_t:
        c.close()
        return err_t, code_t
    n = c.execute("SELECT COUNT(*) n FROM complaints WHERE equipment_id=?", (eid,)).fetchone()["n"]
    n2 = c.execute("SELECT COUNT(*) n FROM breakdowns WHERE equipment_id=?", (eid,)).fetchone()["n"]
    n3 = c.execute("SELECT COUNT(*) n FROM pm_schedules WHERE equipment_id=?", (eid,)).fetchone()["n"]
    n4 = c.execute("SELECT COUNT(*) n FROM portal_links WHERE equipment_id=?", (eid,)).fetchone()["n"]
    if n or n2 or n3 or n4:
        c.close()
        return jsonify({"error": "Equipment is referenced by tickets, PM schedules or portal links; cannot delete."}), 409
    c.execute("DELETE FROM equipment WHERE id=?", (eid,))
    c.commit()
    c.close()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Complaints
# --------------------------------------------------------------------------
@app.get("/api/complaints")
def list_complaints():
    u, err, code = require_role("admin", "engineer", "application", "customer")
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
    else:
        sw, sp = customer_scope_filter(c, u, "cmp.customer_id")
        if sw:
            where.append(sw)
            params += sp
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
    u, err, code = require_role("admin", "engineer", "application", "customer")
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
        err_t, code_t = tenant_guard(u, customer_id, c)
        if err_t:
            c.close()
            return err_t, code_t
        scope = tenant_scope(u, c)
        if scope:
            if customer_id not in scope:
                c.close()
                return jsonify({"error": "You can only raise complaints for your own organization"}), 403
            if equipment_id and eq_cust is not None and eq_cust != customer_id:
                c.close()
                return jsonify({"error": "Equipment does not belong to your organization"}), 403
        if not assignee_allowed(u, b.get("assigned_to") or None, c):
            c.close()
            return jsonify({"error": "You can only assign to your own team"}), 403
        # default ticket location/dept from the chosen equipment when not provided
        location_id = b.get("location_id") or eq_loc
        department_id = b.get("department_id") or eq_dept
        if location_id and not department_id:
            dept = c.execute("SELECT id FROM departments WHERE location_id=?", (location_id,)).fetchone()
            if dept:
                department_id = dept["id"]
        elif department_id and not location_id:
            loc = c.execute("SELECT location_id FROM departments WHERE id=?", (department_id,)).fetchone()
            if loc:
                location_id = loc["location_id"]

    if not customer_id:
        c.close()
        return jsonify({"error": "Organization is required"}), 400
    ra_id, err_r, code_r = resolve_responsible_admin(c, u, customer_id, b.get("responsible_admin_id"))
    if err_r:
        c.close()
        return err_r, code_r
    code_ = next_code_for("complaints", "CMP")
    cur = c.execute(
        "INSERT INTO complaints (code,customer_id,equipment_id,location_id,department_id,subject,description,category,priority,status,created_by,assigned_to,created_at,updated_at,responsible_admin_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code_, customer_id, equipment_id, location_id, department_id,
         b["subject"].strip(), b.get("description", ""), b.get("category", "General"),
         b.get("priority", "medium"), "open", u["id"], b.get("assigned_to") or None, now(), now(), ra_id),
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


def _customer_allowed(u, customer_id, location_id=None, department_id=None, c=None):
    """True if a user may access a record with the given scope.

    * Master admin (admin, no customer): always allowed.
    * Tenant admin: any customer in their care list (primary + linked).
    * Tenant engineer: their single customer.
    * Customer users: their own customer + location + department.
    """
    own = c is None
    if own:
        c = conn()
    try:
        return _in_customer_allowed(u, customer_id, location_id, department_id, c)
    finally:
        if own:
            c.close()


def _in_customer_allowed(u, customer_id, location_id, department_id, c):
    scope = tenant_scope(u, c)
    if u.get("role") == "admin":
        # master = unscoped; tenant admin = care-list scoped
        if is_master_admin(u):
            return True
        return customer_id in (scope or [])
    if u.get("role")  in ("engineer", "application"):
        if scope is None:
            return True  # provider engineer, unscoped
        return customer_id in scope
    # customer-role user: scope to customer/location/department
    if u.get("customer_id") and customer_id != u.get("customer_id"):
        return False
    if u.get("location_id") and location_id is not None and location_id != u.get("location_id"):
        return False
    if u.get("department_id") and department_id is not None and department_id != u.get("department_id"):
        return False
    return True


@app.get("/api/complaints/<int:cid>")
def get_complaint(cid):
    u, err, code = require_role("admin", "engineer", "application", "customer")
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
        "SELECT cm.*, COALESCE(u.name, 'Former user') AS user_name FROM comments cm LEFT JOIN users u ON u.id=cm.user_id "
        "WHERE cm.entity_type='complaint' AND cm.entity_id=? ORDER BY cm.created_at", (cid,)).fetchall()
    out["comments"] = rows_to_dicts(comments)
    out["feedback"] = _feedback_payload(c, "complaint", cid)
    out["feedback_open"] = row["status"] in FEEDBACK_STATUSES["complaint"]
    c.close()
    return jsonify(out)


@app.post("/api/complaints/<int:cid>/accept")
def accept_complaint(cid):
    """An engineer/admin accepts a complaint on the sender's behalf.

    Records WHO accepted (always visible on the ticket and in the portal), sets
    an optional reply for the sender, and makes the acceptor the ticket's
    assignee. Notifies the sender via the portal and the team via in-app
    notifications."""
    u, err, code = require_role("admin", "engineer", "application")
    if err:
        return err, code
    b = get_body()
    reply = (b.get("reply") or "").strip()
    c = conn()
    row = c.execute("SELECT * FROM complaints WHERE id=?", (cid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    if not _customer_allowed(u, row["customer_id"], row["location_id"], row["department_id"], c):
        c.close()
        return jsonify({"error": "Not authorised"}), 403
    if row["status"] in ("resolved", "closed"):
        c.close()
        return jsonify({"error": "A resolved or closed complaint cannot be accepted"}), 409
    if row["accepted_by"]:
        c.close()
        return jsonify({"error": "This complaint has already been accepted"}), 409
    old_status = row["status"]
    # optional status to move the ticket to on acceptance (defaults to current)
    new_status = (b.get("status") or "").strip() or old_status
    if new_status not in ("open", "in_progress", "resolved", "closed"):
        new_status = old_status
    resolved_at, closed_by = row["resolved_at"], row["closed_by"]
    if new_status in ("resolved", "closed"):
        resolved_at = now()
        if new_status == "closed" or (new_status == "resolved" and not row["closed_by"]):
            closed_by = u["id"]
    else:
        resolved_at, closed_by = None, None
    # the acceptor automatically becomes the assignee
    c.execute(
        "UPDATE complaints SET accepted_by=?, accepted_at=?, accept_reply=?, assigned_to=?, "
        "status=?, resolved_at=?, closed_by=?, updated_at=? WHERE id=?",
        (u["id"], now(), reply, u["id"], new_status, resolved_at, closed_by, now(), cid),
    )
    c.commit()
    row = c.execute("SELECT * FROM complaints WHERE id=?", (cid,)).fetchone()
    out = complaint_payload(c, row)
    c.close()

    audit("complaint", cid, u, "accepted",
          f"Accepted by {u['name']}" + (f" — reply sent to reporter" if reply else ""))
    if new_status != old_status:
        audit("complaint", cid, u, "status",
              f"{STATUS_LABELS.get(old_status, old_status)} → {STATUS_LABELS.get(new_status, new_status)} (on acceptance)")
    # everyone following the ticket (reporter + team) hears that it was accepted
    # and by whom; the reply itself is visible to the sender in the QR portal.
    ping_followers("complaint", u["id"], out,
                   f"Complaint {out['code']} accepted by {u['name']}: {out['subject']}",
                   None)
    return jsonify(out)


@app.post("/api/breakdowns/<int:bid>/accept")
def accept_breakdown(bid):
    """An engineer/admin accepts a breakdown on the sender's behalf.

    Mirrors complaint acceptance: records WHO accepted and an optional reply for
    the reporter, and makes the acceptor the ticket's assignee. Notifies the
    sender via the portal and the team via in-app notifications."""
    u, err, code = require_role("admin", "engineer", "application")
    if err:
        return err, code
    b = get_body()
    reply = (b.get("reply") or "").strip()
    c = conn()
    row = c.execute("SELECT * FROM breakdowns WHERE id=?", (bid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    if not _customer_allowed(u, row["customer_id"], row["location_id"], row["department_id"], c):
        c.close()
        return jsonify({"error": "Not authorised"}), 403
    if row["status"] == "resolved":
        c.close()
        return jsonify({"error": "A resolved breakdown cannot be accepted"}), 409
    if row["accepted_by"]:
        c.close()
        return jsonify({"error": "This breakdown has already been accepted"}), 409
    old_status = row["status"]
    # optional status to move the ticket to on acceptance (defaults to current)
    new_status = (b.get("status") or "").strip() or old_status
    if new_status not in ("reported", "diagnosed", "in_progress", "on_hold", "resolved"):
        new_status = old_status
    resolved_at, closed_by = row["resolved_at"], row["closed_by"]
    if new_status == "resolved":
        resolved_at = now()
        if not row["closed_by"]:
            closed_by = u["id"]
    else:
        resolved_at, closed_by = None, None
    # the acceptor automatically becomes the assignee
    c.execute(
        "UPDATE breakdowns SET accepted_by=?, accepted_at=?, accept_reply=?, assigned_to=?, "
        "status=?, resolved_at=?, closed_by=?, updated_at=? WHERE id=?",
        (u["id"], now(), reply, u["id"], new_status, resolved_at, closed_by, now(), bid),
    )
    c.commit()
    row = c.execute("SELECT * FROM breakdowns WHERE id=?", (bid,)).fetchone()
    out = breakdown_payload(c, row)
    c.close()

    audit("breakdown", bid, u, "accepted",
          f"Accepted by {u['name']}" + (f" — reply sent to reporter" if reply else ""))
    if new_status != old_status:
        audit("breakdown", bid, u, "status",
              f"{STATUS_LABELS.get(old_status, old_status)} → {STATUS_LABELS.get(new_status, new_status)} (on acceptance)")
    ping_followers("breakdown", u["id"], out,
                   f"Breakdown {out['code']} accepted by {u['name']}: {out['fault_description'][:90]}",
                   None)
    return jsonify(out)


@app.patch("/api/complaints/<int:cid>")
def update_complaint(cid):
    u, err, code = require_role("admin", "engineer", "application", "customer")
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
    # tenant staff cannot move a ticket outside their care list, use another
    # customer's equipment, or assign it to anyone outside their team
    scope = tenant_scope(u, c)
    if scope:
        if "customer_id" in b and b["customer_id"] not in scope:
            c.close()
            return jsonify({"error": "You cannot move this complaint to another organization"}), 403
        if "equipment_id" in b and b["equipment_id"]:
            e_row = c.execute("SELECT customer_id FROM equipment WHERE id=?", (b["equipment_id"],)).fetchone()
            if not e_row or e_row["customer_id"] not in scope:
                c.close()
                return jsonify({"error": "Equipment does not belong to this organization"}), 403
        if "assigned_to" in b and not assignee_allowed(u, b["assigned_to"] or None, c):
            c.close()
            return jsonify({"error": "You can only assign to your own team"}), 403
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
                  "location_id", "department_id", "responsible_admin_id"):
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
                # record who performed the resolve/close
                if b["status"] == "closed" or (b["status"] == "resolved" and not row["closed_by"]):
                    fields.append("closed_by=?")
                    params.append(u["id"])
            else:
                fields.append("resolved_at=?")
                params.append(None)
                fields.append("closed_by=?")
                params.append(None)
    # validate the responsible tenant admin against the resulting customer scope
    target_customer = b.get("customer_id", row["customer_id"])
    ra_id = b.get("responsible_admin_id", row["responsible_admin_id"])
    err_r, code_r = validate_responsible_admin(c, target_customer, ra_id)
    if err_r:
        c.close()
        return err_r, code_r
    old_assignee = row["assigned_to"]
    old_status = row["status"]
    # Auto-assign: an engineer/admin who starts working an unassigned ticket
    # (e.g. moves it out of "open") becomes its assignee — the assignee always
    # names the person who actually responded.
    if ("status" in b and "assigned_to" not in b
            and u["role"] in ("admin", "engineer", "application") and not row["assigned_to"]):
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
        if b["status"] == "resolved":
            audit("complaint", cid, u, "resolution", f"Marked resolved by {u['name']}")
        elif b["status"] == "closed":
            audit("complaint", cid, u, "resolution", f"Closed by {u['name']}")
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
    u, err, code = require_master()
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
    u, err, code = require_role("admin", "engineer", "application", "customer")
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
    else:
        sw, sp = customer_scope_filter(c, u, "brk.customer_id")
        if sw:
            where.append(sw)
            params += sp
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
    u, err, code = require_role("admin", "engineer", "application", "customer")
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
        err_t, code_t = tenant_guard(u, customer_id, c)
        if err_t:
            c.close()
            return err_t, code_t
        scope = tenant_scope(u, c)
        if scope:
            if customer_id not in scope:
                c.close()
                return jsonify({"error": "You can only raise breakdowns for your own organization"}), 403
            if equipment_id and eq_cust is not None and eq_cust != customer_id:
                c.close()
                return jsonify({"error": "Equipment does not belong to your organization"}), 403
        if not assignee_allowed(u, b.get("assigned_to") or None, c):
            c.close()
            return jsonify({"error": "You can only assign to your own team"}), 403
        location_id = b.get("location_id") or eq_loc
        department_id = b.get("department_id") or eq_dept
        if location_id and not department_id:
            dept = c.execute("SELECT id FROM departments WHERE location_id=?", (location_id,)).fetchone()
            if dept:
                department_id = dept["id"]
        elif department_id and not location_id:
            loc = c.execute("SELECT location_id FROM departments WHERE id=?", (department_id,)).fetchone()
            if loc:
                location_id = loc["location_id"]

    if not customer_id:
        c.close()
        return jsonify({"error": "Organization is required"}), 400
    ra_id, err_r, code_r = resolve_responsible_admin(c, u, customer_id, b.get("responsible_admin_id"))
    if err_r:
        c.close()
        return err_r, code_r
    code_ = next_code_for("breakdowns", "BRK")
    cur = c.execute(
        "INSERT INTO breakdowns (code,equipment_id,customer_id,complaint_id,location_id,department_id,fault_description,root_cause,priority,status,reported_by,assigned_to,resolution_notes,created_at,updated_at,responsible_admin_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code_, equipment_id, customer_id, b.get("complaint_id") or None, location_id, department_id,
         b["fault_description"].strip(), b.get("root_cause", ""), b.get("priority", "medium"), "reported",
         u["id"], b.get("assigned_to") or None, b.get("resolution_notes", ""), now(), now(), ra_id),
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
    u, err, code = require_role("admin", "engineer", "application", "customer")
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
        "SELECT cm.*, COALESCE(u.name, 'Former user') AS user_name FROM comments cm LEFT JOIN users u ON u.id=cm.user_id "
        "WHERE cm.entity_type='breakdown' AND cm.entity_id=? ORDER BY cm.created_at", (bid,)).fetchall()
    out["comments"] = rows_to_dicts(comments)
    out["feedback"] = _feedback_payload(c, "breakdown", bid)
    out["feedback_open"] = row["status"] in FEEDBACK_STATUSES["breakdown"]
    c.close()
    return jsonify(out)


@app.patch("/api/breakdowns/<int:bid>")
def update_breakdown(bid):
    u, err, code = require_role("admin", "engineer", "application")
    if err:
        return err, code
    b = get_body()
    c = conn()
    row = c.execute("SELECT * FROM breakdowns WHERE id=?", (bid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    if not _customer_allowed(u, row["customer_id"], row["location_id"], row["department_id"]):
        c.close()
        return jsonify({"error": "Not authorised"}), 403
    scope = tenant_scope(u, c)
    if scope:
        if "customer_id" in b and b["customer_id"] not in scope:
            c.close()
            return jsonify({"error": "You cannot move this breakdown to another organization"}), 403
        if "equipment_id" in b and b["equipment_id"]:
            e_row = c.execute("SELECT customer_id FROM equipment WHERE id=?", (b["equipment_id"],)).fetchone()
            if not e_row or e_row["customer_id"] not in scope:
                c.close()
                return jsonify({"error": "Equipment does not belong to this organization"}), 403
        if "assigned_to" in b and not assignee_allowed(u, b["assigned_to"] or None, c):
            c.close()
            return jsonify({"error": "You can only assign to your own team"}), 403
    old_row = dict(row)

    fields = []
    params = []
    for f in ("equipment_id", "customer_id", "complaint_id", "fault_description", "root_cause",
              "assigned_to", "resolution_notes", "location_id", "department_id", "responsible_admin_id"):
        if f in b:
            fields.append(f"{f}=?")
            params.append(b[f])
    # validate the responsible tenant admin against the resulting customer scope
    target_customer = b.get("customer_id", row["customer_id"])
    ra_id = b.get("responsible_admin_id", row["responsible_admin_id"])
    err_r, code_r = validate_responsible_admin(c, target_customer, ra_id)
    if err_r:
        c.close()
        return err_r, code_r
    if "priority" in b:
        fields.append("priority=?")
        params.append(b["priority"])
    if "status" in b:
        fields.append("status=?")
        params.append(b["status"])
        if b["status"] == "resolved":
            fields.append("resolved_at=?")
            params.append(now())
            # first resolve records who did it; keep the original closer on re-resolve
            if not row["closed_by"]:
                fields.append("closed_by=?")
                params.append(u["id"])
        else:
            fields.append("resolved_at=?")
            params.append(None)
            fields.append("closed_by=?")
            params.append(None)
    old_assignee = row["assigned_to"]
    old_status = row["status"]
    # Auto-assign: the engineer/admin who starts working an unassigned
    # breakdown (e.g. changes its status) becomes its assignee.
    if ("status" in b and "assigned_to" not in b
            and u["role"] in ("admin", "engineer", "application") and not row["assigned_to"]):
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
            audit("breakdown", bid, u, "resolution", f"Marked resolved by {u['name']}")
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
    u, err, code = require_master()
    if err:
        return err, code
    c = conn()
    row = c.execute("SELECT * FROM breakdowns WHERE id=?", (bid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    c.execute("DELETE FROM comments WHERE entity_type='breakdown' AND entity_id=?", (bid,))
    c.execute("DELETE FROM notifications WHERE entity_type='breakdown' AND entity_id=?", (bid,))
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
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    b = get_body()
    if not (b.get("text") or "").strip():
        return jsonify({"error": "Comment text is required"}), 400
    entity = b.get("entity_type")
    eid = b.get("entity_id")
    if entity not in ("complaint", "breakdown") or not eid:
        return jsonify({"error": "Invalid entity"}), 400
    # authorisation: scope the actor to the ticket (customer, tenant staff, or master)
    c = conn()
    row = c.execute(f"SELECT customer_id, location_id, department_id FROM {entity}s WHERE id=?", (eid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    if not _customer_allowed(u, row["customer_id"], row["location_id"], row["department_id"]):
        c.close()
        return jsonify({"error": "Not authorised"}), 403
    # Auto-assign: the first engineer/admin who RESPONDS to an unassigned
    # ticket becomes its assignee (only techs/admins who own the work should
    # answer, so the assignee field always names the actual responder).
    ticket = c.execute(f"SELECT * FROM {entity}s WHERE id=?", (eid,)).fetchone()
    auto_assigned = False
    if ticket and u["role"] in ("admin", "engineer", "application") and not ticket["assigned_to"]:
        c.execute(f"UPDATE {entity}s SET assigned_to=?, updated_at=? WHERE id=?",
                  (u["id"], now(), eid))
        auto_assigned = True
    cur = c.execute(
        "INSERT INTO comments (entity_type,entity_id,user_id,text,created_at) VALUES (?,?,?,?,?)",
        (entity, eid, u["id"], b["text"].strip(), now()),
    )
    c.commit()
    row = c.execute(
        "SELECT cm.*, COALESCE(u.name, 'Former user') AS user_name FROM comments cm LEFT JOIN users u ON u.id=cm.user_id WHERE cm.id=?",
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
# Ticket history (audit log)
# --------------------------------------------------------------------------
AUDIT_TABLES = {"complaint": "complaints", "breakdown": "breakdowns"}


@app.get("/api/audit")
def get_audit():
    """One ticket's history timeline: who did what, when, newest first.

    This is the read side of audit(). The History card on both ticket detail
    views has always called it, but the route never existed, so every ticket
    rendered "History unavailable." while the rows accumulated unread.

    Scoped exactly like the ticket itself — a customer only their own
    organization's tickets, tenant staff only their care list, the master
    everything — so it cannot be used to read another organization's activity
    by guessing an id.

    Nothing is filtered out: unlike the public portal's activity feed, which
    hides a reporter's own submissions so they are not alarmed by them, this is
    an audit trail and shows every recorded action, including the customer's
    own ratings and feedback."""
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    entity = (request.args.get("entity_type") or "").strip().lower()
    eid = _id(request.args.get("entity_id"))
    if entity not in AUDIT_TABLES or not eid:
        return jsonify({"error": "Invalid entity"}), 400
    c = conn()
    row = c.execute(
        "SELECT customer_id, location_id, department_id FROM %s WHERE id=?"
        % AUDIT_TABLES[entity], (eid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    if not _customer_allowed(u, row["customer_id"], row["location_id"], row["department_id"]):
        c.close()
        return jsonify({"error": "Not authorised"}), 403
    # Newest first: the card is a timeline of recent activity, not a transcript.
    # created_at is a text timestamp, so ties (same second) fall back to the id.
    rows = c.execute(
        "SELECT action, user_name, detail, created_at FROM audit_logs "
        "WHERE entity_type=? AND entity_id=? ORDER BY created_at DESC, id DESC",
        (entity, eid)).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])


# --------------------------------------------------------------------------
# Customer feedback on settled tickets
# --------------------------------------------------------------------------
# A ticket accepts feedback only once it is settled: a complaint when resolved
# or closed, a breakdown when resolved (breakdowns have no closed status).
# Feedback is deliberately inert — it never changes a ticket's status,
# assignment or position in any list, and a ticket nobody rates or comments on
# behaves exactly as it did before.
FEEDBACK_STATUSES = {"complaint": ("resolved", "closed"), "breakdown": ("resolved",)}
FEEDBACK_TABLES = {"complaint": "complaints", "breakdown": "breakdowns"}
FEEDBACK_MAX_LEN = 2000


def _feedback_ticket(c, kind, tid):
    return c.execute("SELECT * FROM %s WHERE id=?" % FEEDBACK_TABLES[kind], (tid,)).fetchone()


def _feedback_settled(row, kind):
    return row["status"] in FEEDBACK_STATUSES[kind]


def _feedback_payload(c, kind, tid):
    """The rating and comment thread for one ticket, to embed in its payload.

    Portal visitors have no user row, so a stored display name is the fallback
    and an unnamed visitor simply reads as "Customer"."""
    rating = c.execute(
        "SELECT r.rating, r.updated_at, "
        "COALESCE(NULLIF(u.name,''), NULLIF(r.rated_by_name,''), 'Customer') AS rated_by "
        "FROM ticket_ratings r LEFT JOIN users u ON u.id=r.user_id "
        "WHERE r.entity_type=? AND r.entity_id=?", (kind, tid)).fetchone()
    comments = c.execute(
        "SELECT f.id, f.text, f.created_at, "
        "COALESCE(NULLIF(u.name,''), NULLIF(f.author_name,''), 'Customer') AS author_name "
        "FROM ticket_feedback f LEFT JOIN users u ON u.id=f.user_id "
        "WHERE f.entity_type=? AND f.entity_id=? ORDER BY f.created_at, f.id", (kind, tid)).fetchall()
    return {"rating": dict(rating) if rating else None, "comments": rows_to_dicts(comments)}


def _feedback_summary(c, kind, ids):
    """Ratings and comment counts for a batch of tickets, in two queries."""
    if not ids:
        return {}
    marks = ", ".join("?" for _ in ids)
    out = {}
    for r in c.execute(
            "SELECT entity_id, rating FROM ticket_ratings "
            "WHERE entity_type=? AND entity_id IN (%s)" % marks, [kind] + list(ids)).fetchall():
        out.setdefault(r["entity_id"], {})["rating"] = r["rating"]
    for r in c.execute(
            "SELECT entity_id, COUNT(*) AS n FROM ticket_feedback "
            "WHERE entity_type=? AND entity_id IN (%s) GROUP BY entity_id" % marks,
            [kind] + list(ids)).fetchall():
        out.setdefault(r["entity_id"], {})["comments"] = r["n"]
    return out


def _save_rating(c, kind, tid, customer_id, rating, user_id, display_name):
    """Insert or replace the one rating a ticket has. Returns (id, created)."""
    existing = c.execute(
        "SELECT id FROM ticket_ratings WHERE entity_type=? AND entity_id=?", (kind, tid)).fetchone()
    if existing:
        c.execute("UPDATE ticket_ratings SET rating=?, user_id=?, rated_by_name=?, updated_at=? WHERE id=?",
                  (rating, user_id, display_name, now(), existing["id"]))
        return existing["id"], False
    cur = c.execute(
        "INSERT INTO ticket_ratings (entity_type,entity_id,customer_id,user_id,rated_by_name,rating,"
        "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (kind, tid, customer_id, user_id, display_name, rating, now(), now()))
    return cur.lastrowid, True


def _rating_value(b):
    """A submitted star rating if it is a whole number of stars from 1 to 5.

    Deliberately not routed through _id(): that coerces with int(), which would
    quietly truncate 3.5 stars to 3 and store a rating nobody chose. Browsers
    send values as strings, so "4" and 4.0 are accepted; fractions, zero,
    negatives and anything out of range are refused."""
    raw = b.get("rating")
    if isinstance(raw, bool) or raw is None or raw == "":
        return None
    try:
        v = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if not v.is_integer():
        return None
    v = int(v)
    return v if 1 <= v <= 5 else None


def _require_customer_side(u):
    """Feedback is the customer's to give — staff do the work being rated.

    require_role() waves any admin through whatever roles it was given, so the
    customer-only rule has to be stated explicitly here."""
    if u["role"] != "customer":
        return jsonify({"error": "Only the customer side can leave feedback on a ticket"}), 403
    return None


def _feedback_guard(u, row, kind, c):
    """Shared checks for the authenticated feedback endpoints."""
    err = _require_customer_side(u)
    if err:
        return err
    if not _customer_allowed(u, row["customer_id"], row["location_id"], row["department_id"], c):
        return jsonify({"error": "Not authorised"}), 403
    if not _feedback_settled(row, kind):
        return jsonify({"error": "Feedback opens once this ticket is resolved or closed"}), 409
    return None


@app.post("/api/tickets/<kind>/<int:tid>/rating")
def rate_ticket(kind, tid):
    """A customer rates a settled ticket from 1 to 5 stars.

    Optional: nothing depends on it. Re-rating replaces the previous score, so a
    ticket carries one current rating rather than an accumulating vote."""
    if kind not in FEEDBACK_TABLES:
        return jsonify({"error": "Invalid ticket type"}), 400
    u, err, code = require_role("customer")
    if err:
        return err, code
    rating = _rating_value(get_body())
    if not rating:
        return jsonify({"error": "Choose a rating from 1 to 5 stars"}), 400
    c = conn()
    row = _feedback_ticket(c, kind, tid)
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    guard = _feedback_guard(u, row, kind, c)
    if guard:
        c.close()
        return guard
    rid, created = _save_rating(c, kind, tid, row["customer_id"], rating, u["id"], u.get("name") or "")
    c.commit()
    c.close()
    audit(kind, tid, u, "rating", "%d star%s" % (rating, "" if rating == 1 else "s"))
    return jsonify({"ok": True, "id": rid, "rating": rating, "created": created}), (201 if created else 200)


@app.post("/api/tickets/<kind>/<int:tid>/feedback")
def comment_ticket_feedback(kind, tid):
    """A customer adds a comment to a settled ticket's feedback thread.

    The thread stays open after the ticket is settled, so a customer can add
    that the fix held — or that it did not — without reopening anything."""
    if kind not in FEEDBACK_TABLES:
        return jsonify({"error": "Invalid ticket type"}), 400
    u, err, code = require_role("customer")
    if err:
        return err, code
    text = (get_body().get("text") or "").strip()
    if not text:
        return jsonify({"error": "Please write your feedback"}), 400
    if len(text) > FEEDBACK_MAX_LEN:
        return jsonify({"error": "Feedback is limited to %d characters" % FEEDBACK_MAX_LEN}), 400
    c = conn()
    row = _feedback_ticket(c, kind, tid)
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    guard = _feedback_guard(u, row, kind, c)
    if guard:
        c.close()
        return guard
    cur = c.execute(
        "INSERT INTO ticket_feedback (entity_type,entity_id,customer_id,user_id,author_name,text,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (kind, tid, row["customer_id"], u["id"], u.get("name") or "", text, now()))
    c.commit()
    new_id = cur.lastrowid
    c.close()
    audit(kind, tid, u, "feedback", text[:120])
    return jsonify({"ok": True, "id": new_id, "author_name": u.get("name") or "Customer",
                    "text": text}), 201


# --------------------------------------------------------------------------
# Users & dashboard
# --------------------------------------------------------------------------
@app.get("/api/users")
def list_users():
    u, err, code = require_role("admin")
    if err:
        return err, code
    c = conn()
    rows = c.execute("SELECT * FROM users WHERE lower(email) != ? ORDER BY role, name",
                     (FORMER_USER_EMAIL,)).fetchall()
    out = []
    tenant_filter = (u["role"] == "admin" and not is_master_admin(u))
    scope = tenant_scope(u, c) if tenant_filter else None
    for r in rows:
        if tenant_filter:
            # tenant admins only see their care-list customers' users, plus the
            # provider's unbound engineers they may assign work to (never other admins).
            if r["role"] == "admin":
                continue
            if r["customer_id"] is not None and r["customer_id"] not in scope:
                continue
        d = public_user(r)
        d["customer_name"] = None
        d["location_name"] = None
        d["department_name"] = None
        if r["customer_id"]:
            cu = c.execute("SELECT name FROM customers WHERE id=?", (r["customer_id"],)).fetchone()
            d["customer_name"] = cu["name"] if cu else None
        d["location_name"] = _name_of(c, "locations", r["location_id"])
        d["department_name"] = _name_of(c, "departments", r["department_id"])
        d["responsible_admin_name"] = _responsible_admin_name(c, r["responsible_admin_id"])
        out.append(d)
    c.close()
    return jsonify(out)


@app.get("/api/engineers")
def list_engineers():
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    c = conn()
    # assignee picker: only the player's own tenant staff + provider engineers.
    # The master admin is never listed as an assignable engineer.
    q = "SELECT id,name,email,role FROM users WHERE role IN ('engineer','application','admin') AND active=1"
    params = []
    scope = tenant_scope(u, c)
    if u["role"] == "admin" and not is_master_admin(u):
        # tenant admin: own tenant staff + provider engineers only — even with
        # an empty care list (IN (NULL) matches nothing, provider techs still show)
        marks = ", ".join("?" for _ in scope) or "NULL"
        q += f" AND (customer_id IN ({marks}) OR (customer_id IS NULL AND role IN ('engineer','application')))"
        params += list(scope)
    elif scope:
        marks = ", ".join("?" for _ in scope)
        q += f" AND (customer_id IN ({marks}) OR (customer_id IS NULL AND role IN ('engineer','application')))"
        params += list(scope)
    q += " ORDER BY name"
    rows = c.execute(q, params).fetchall()
    c.close()
    return jsonify(rows_to_dicts(rows))


@app.get("/api/tenant-admins")
def list_tenant_admins():
    """Administrators available as a record's 'responsible tenant admin'.

    * customer_id=<n>  -> the tenant admin(s) of that customer
    * global=1         -> LabSynch-wide (unbound) administrators only
    * (neither)        -> scoped to the actor's customer; master gets all admins
    """
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    c = conn()
    cust = request.args.get("customer_id")
    if request.args.get("global") == "1":
        # "LabSynch-wide" means exactly the Master System Admin — unbound tenant
        # admins are NOT LabSynch-wide (they simply haven't created an org yet).
        q = ("SELECT id, name, email, role, customer_id FROM users WHERE role='admin' AND active=1 "
             "AND lower(email)=? ORDER BY name")
        rows = c.execute(q, (MASTER_ADMIN_EMAIL,)).fetchall()
    elif cust:
        # every active admin whose PRIMARY customer or care-list link is `cust`
        q = ("SELECT DISTINCT u.id, u.name, u.email, u.role, u.customer_id FROM users u "
             "LEFT JOIN admin_customer_links l ON l.admin_id=u.id "
             "WHERE u.role='admin' AND u.active=1 AND (u.customer_id=? OR l.customer_id=?) "
             "ORDER BY u.name")
        rows = c.execute(q, (cust, cust)).fetchall()
    else:
        scope = tenant_scope(u, c)
        if u["role"] == "customer":
            scope = [u.get("customer_id")] if u.get("customer_id") else []
        if u["role"] == "admin" and not is_master_admin(u) and not scope:
            # unbound tenant admin: the only responsible admin they can name is themselves
            rows = c.execute(
                "SELECT id, name, email, role, customer_id FROM users WHERE id=?", (u["id"],)).fetchall()
        elif scope:
            marks = ", ".join("?" for _ in scope)
            q = ("SELECT DISTINCT u.id, u.name, u.email, u.role, u.customer_id FROM users u "
                 "LEFT JOIN admin_customer_links l ON l.admin_id=u.id "
                 f"WHERE u.role='admin' AND u.active=1 AND (u.customer_id IN ({marks}) OR l.customer_id IN ({marks})) "
                 "ORDER BY u.name")
            rows = c.execute(q, list(scope) + list(scope)).fetchall()
        else:
            q = "SELECT id, name, email, role, customer_id FROM users WHERE role='admin' AND active=1 ORDER BY name"
            rows = c.execute(q).fetchall()
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
    role = b.get("role", "engineer")
    if role not in ("admin", "engineer", "application", "customer"):
        return jsonify({"error": "Invalid role"}), 400

    is_tenant = is_tenant_admin(u)
    scope = None
    c = None
    if is_tenant:
        c = conn()
        scope = tenant_scope(u, c)
        if role == "admin":
            c.close()
            return jsonify({"error": "Only the master administrator can create admin accounts"}), 403

    if role == "customer":
        customer_id = _id(b.get("customer_id"))
        location_id = _id(b.get("location_id"))
        department_id = _id(b.get("department_id"))
        _conn_tmp = c or conn()
        if location_id and not department_id:
            dept = _conn_tmp.execute("SELECT id FROM departments WHERE location_id=?", (location_id,)).fetchone()
            if dept:
                department_id = dept["id"]
        elif department_id and not location_id:
            loc = _conn_tmp.execute("SELECT location_id FROM departments WHERE id=?", (department_id,)).fetchone()
            if loc:
                location_id = loc["location_id"]
        if c is None:
            _conn_tmp.close()
    elif role  in ("engineer", "application"):
        # A tenant admin may leave the organization EMPTY: the account then sits
        # under the linked tenant admin's care and inherits that admin's care
        # list (see tenant_scope) rather than being tied to one organization.
        # When an organization IS named it must be on their care list.
        # The master may optionally bind an engineer to a customer (tenant engineer).
        if scope is not None:
            customer_id = _id(b.get("customer_id"))
            if customer_id and customer_id not in scope:
                if c: c.close()
                return jsonify({"error": "You can only create accounts for an organization you care for"}), 403
        else:
            customer_id = (u.get("customer_id") if u.get("role")  in ("engineer", "application") else
                           _id(b.get("customer_id")))
        location_id = department_id = None
    else:  # admin — only the master may create one, and it is always a tenant admin.
        # The tenant admin may deliberately be left UNLINKED: after first login
        # they create their own organization (auto-added to their care list),
        # then its locations, departments, equipment and user accounts.
        customer_id = _id(b.get("customer_id"))
        location_id = department_id = None

    # A customer-less staff account is allowed — it belongs to the tenant admin's
    # care instead of to one organization. Customer-role accounts still have to
    # name one (enforced just below), and any organization that IS named must be
    # on the actor's care list.
    if scope is not None and customer_id and customer_id not in scope:
        if c: c.close()
        return jsonify({"error": "You can only create accounts for an organization you care for"}), 403
    if role == "customer" and not customer_id:
        if c: c.close()
        return jsonify({"error": "Linked organization is required for customer accounts"}), 400

    if c is None:
        c = conn()
    if customer_id:
        err_r, code_r = _validate_loc_dept(c, customer_id, location_id, department_id)
        if err_r:
            c.close()
            return err_r, code_r
    ra_id, err_r, code_r = resolve_responsible_admin(
        c, u, customer_id, _id(b.get("responsible_admin_id")),
        customerless_user=(role != "customer"))
    if err_r:
        c.close()
        return err_r, code_r
    # A tenant admin may only hand a customer-less account to themselves or to a
    # peer who cares for at least one of the same organizations — otherwise they
    # could push an account into a tenant they do not manage.
    if scope is not None and not customer_id and ra_id and ra_id != u["id"]:
        peer = c.execute("SELECT * FROM users WHERE id=?", (ra_id,)).fetchone()
        peer_scope = _admin_scope_ids(c, ra_id, peer) if peer else []
        if not (set(peer_scope) & set(scope)):
            c.close()
            return jsonify({"error": "That tenant admin does not care for any organization you manage"}), 403
    if (b["email"] or "").strip().lower() == FORMER_USER_EMAIL:
        c.close()
        return jsonify({"error": "That email is reserved for the system placeholder account"}), 400
    exists = c.execute("SELECT id FROM users WHERE lower(email)=?", (b["email"].strip().lower(),)).fetchone()
    if exists:
        c.close()
        return jsonify({"error": "Email already in use"}), 409
    cur = c.execute(
        "INSERT INTO users (name,email,phone,password_hash,role,customer_id,location_id,department_id,active,responsible_admin_id,created_at) VALUES (?,?,?,?,?,?,?,?,1,?,?)",
        (b["name"].strip(), b["email"].strip().lower(), b.get("phone", ""), hash_password(b["password"]),
         role, customer_id, location_id, department_id, ra_id, now()),
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

    # The Master System Admin account is fixed: its role and scope cannot be
    # changed and it can never be disabled or demoted.
    if (existing["email"] or "").strip().lower() == MASTER_ADMIN_EMAIL:
        if b.get("role") and b["role"] != "admin":
            c.close()
            return jsonify({"error": "The Master System Admin account cannot change role"}), 403
        if "customer_id" in b and b["customer_id"]:
            c.close()
            return jsonify({"error": "The Master System Admin account cannot be linked to an organization"}), 403
        if b.get("active") is not None and b["active"] != 1:
            c.close()
            return jsonify({"error": "The Master System Admin account cannot be disabled"}), 403

    if u["role"] == "admin" and not is_master_admin(u):
        # Tenant admins cannot touch admin accounts, nor anyone outside their care list.
        if existing["role"] == "admin":
            c.close()
            return jsonify({"error": "Only the master administrator can manage admin accounts"}), 403
        scope = tenant_scope(u, c)
        new_role = b.get("role", existing["role"])
        # An account with no organization of its own belongs to whichever tenant
        # admin it is linked to, so it stays manageable by that admin (or a peer
        # who shares an organization with them) rather than falling out of scope.
        in_care = (existing["customer_id"] in scope if existing["customer_id"]
                   else _staff_in_tenant_care(c, u["id"], existing, scope))
        if not in_care:
            c.close()
            return jsonify({"error": "Not authorised — user belongs to an organization you do not care for"}), 403
        if new_role == "admin":
            c.close()
            return jsonify({"error": "Only the master administrator can create admin accounts"}), 403
        if "customer_id" in b:
            wanted = _id(b["customer_id"])
            if wanted and wanted not in scope:
                c.close()
                return jsonify({"error": "You can only link accounts to an organization you care for"}), 403
            # Clearing the organization is fine for staff (they move under the
            # linked tenant admin's care) but not for a customer-role account.
            if not wanted and new_role == "customer":
                c.close()
                return jsonify({"error": "Linked organization is required for customer accounts"}), 400
    elif u["role"]  in ("engineer", "application") and u.get("customer_id"):
        # tenant engineers: same restrictions, single customer
        if existing["role"] == "admin":
            c.close()
            return jsonify({"error": "Only the master administrator can manage admin accounts"}), 403
        if existing["customer_id"] != u["customer_id"]:
            c.close()
            return jsonify({"error": "Not authorised — user belongs to another organization"}), 403
        if b.get("role") == "admin":
            c.close()
            return jsonify({"error": "Only the master administrator can create admin accounts"}), 403

    # Resolve the customer scope that would result from this update, so we can
    # validate any location/department pairing even when role stays 'customer'/'engineer'.
    new_customer_id = existing["customer_id"]
    if "customer_id" in b:
        new_customer_id = _id(b["customer_id"])
    elif "role" in b:
        if b["role"]  in ("engineer", "application"):
            new_customer_id = u.get("customer_id") if u.get("role")  in ("engineer", "application") else None
        elif b["role"] == "admin":
            new_customer_id = None
    # Admins other than the Master are tenant admins; customer-less (unbound,
    # created that way by the master) and customer-linked are both valid states.
    if new_customer_id:
        new_loc = b.get("location_id", existing["location_id"])
        new_dept = b.get("department_id", existing["department_id"])
        if new_loc and not new_dept:
            dept = c.execute("SELECT id FROM departments WHERE location_id=?", (new_loc,)).fetchone()
            if dept:
                new_dept = dept["id"]
        elif new_dept and not new_loc:
            loc = c.execute("SELECT location_id FROM departments WHERE id=?", (new_dept,)).fetchone()
            if loc:
                new_loc = loc["location_id"]
        err_r, code_r = _validate_loc_dept(c, new_customer_id, new_loc, new_dept)
        if err_r:
            c.close()
            return err_r, code_r

    # validate the responsible tenant admin against the resulting customer scope
    ra_id = (_id(b["responsible_admin_id"]) if "responsible_admin_id" in b
             else existing["responsible_admin_id"])
    err_r, code_r = validate_responsible_admin(
        c, new_customer_id, ra_id,
        customerless_user=(b.get("role", existing["role"]) != "customer"))
    if err_r:
        c.close()
        return err_r, code_r

    if "role" in b and b["role"] not in ("admin", "engineer", "application", "customer"):
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
    # Id columns are coerced so a string from a <select> is never written as TEXT
    # (which would silently stop matching integer scope lists). `active` is not an
    # id and must keep 0 as a real value, so it is deliberately excluded.
    id_fields = ("customer_id", "location_id", "department_id", "responsible_admin_id")
    for f in ("name", "phone", "role", "customer_id", "location_id", "department_id", "active", "responsible_admin_id"):
        if f in b:
            fields.append(f"{f}=?")
            params.append(_id(b[f]) if f in id_fields else b[f])
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
    out = public_user(row, c)
    if row and row["customer_id"]:
        cu = c.execute("SELECT name FROM customers WHERE id=?", (row["customer_id"],)).fetchone()
        out["customer_name"] = cu["name"] if cu else None
    out["location_name"] = _name_of(c, "locations", row["location_id"]) if row else None
    out["department_name"] = _name_of(c, "departments", row["department_id"]) if row else None
    out["responsible_admin_name"] = _responsible_admin_name(c, row["responsible_admin_id"]) if row else None

    # Care-list maintenance for tenant admins:
    #  * primary customer changes keep the old one in links (explicit removal
    #    drops it) but ensure the new primary is mirrored;
    #  * the master can replace a tenant admin's whole customer list via
    #    `customer_ids` (list of ids; primary must be present).
    if row and row["role"] == "admin" and row["customer_id"] and \
            (row["email"] or "").strip().lower() != MASTER_ADMIN_EMAIL:
        if is_master_admin(u) and "customer_ids" in b:
            wanted = [int(x) for x in b["customer_ids"]]
            if row["customer_id"] not in wanted:
                wanted.append(row["customer_id"])
            c.execute("DELETE FROM admin_customer_links WHERE admin_id=?", (uid,))
            for cid in wanted:
                if cid != row["customer_id"]:
                    c.execute(
                        "INSERT OR IGNORE INTO admin_customer_links (admin_id,customer_id,created_at) VALUES (?,?,?)",
                        (uid, cid, now()))
        else:
            c.execute(
                "INSERT OR IGNORE INTO admin_customer_links (admin_id, customer_id, created_at) VALUES (?,?,?)",
                (uid, row["customer_id"], now()))
        c.commit()
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
    existing = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not existing:
        c.close()
        return jsonify({"error": "Not found"}), 404
    if (existing["email"] or "").strip().lower() == MASTER_ADMIN_EMAIL:
        c.close()
        return jsonify({"error": "The Master System Admin account cannot be deleted"}), 403
    if is_tenant_admin(u):
        if existing["role"] == "admin":
            c.close()
            return jsonify({"error": "Only the master administrator can manage admin accounts"}), 403
        err_t, code_t = tenant_guard(u, existing["customer_id"], c)
        if err_t:
            c.close()
            return err_t, code_t
    email = (existing["email"] or "").strip().lower()
    if email == FORMER_USER_EMAIL:
        c.close()
        return jsonify({"error": "The 'Former user' placeholder is a system account and cannot be deleted"}), 400
    # The master may delete ANY user (tenant admins: any non-admin in their
    # scope). Deleting never cascades: every record that references the user is
    # UNLINKED (nullable reference columns set to NULL) — tickets, equipment,
    # PM schedules, care lists and history are all kept; immutable history
    # references move to the "Former user" placeholder.
    former = c.execute("SELECT id FROM users WHERE lower(email)=?", (FORMER_USER_EMAIL,)).fetchone()
    if former:
        former_id = former["id"]
    else:
        former_id = c.execute(
            "INSERT INTO users (name,email,phone,password_hash,role,customer_id,location_id,department_id,active,pending,created_at) "
            "VALUES ('Former user', ?, '', '!no-login!', 'customer', NULL, NULL, NULL, 0, 0, ?)",
            (FORMER_USER_EMAIL, now())).lastrowid
    for table, col in (("complaints", "created_by"), ("breakdowns", "reported_by"),
                       ("comments", "user_id"),
                       ("pm_logs", "performed_by")):
        c.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (former_id, uid))
    for table, col in (
            ("complaints", "assigned_to"), ("complaints", "accepted_by"),
            ("complaints", "closed_by"), ("complaints", "responsible_admin_id"),
            ("breakdowns", "assigned_to"), ("breakdowns", "accepted_by"),
            ("breakdowns", "closed_by"), ("breakdowns", "responsible_admin_id"),
            ("users", "responsible_admin_id"), ("equipment", "responsible_admin_id"),
            ("pm_schedules", "assigned_to"),
            ("portal_links", "created_by"), ("onboarding_apps", "reviewed_by"),
            ("audit_logs", "user_id")):
        c.execute(f"UPDATE {table} SET {col}=NULL WHERE {col}=?", (uid,))
    # The user's own artifacts are removed outright.
    c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    c.execute("DELETE FROM notifications WHERE user_id=?", (uid,))
    c.execute("DELETE FROM notification_pings WHERE user_id=?", (uid,))
    c.execute("DELETE FROM push_subscriptions WHERE user_id=?", (uid,))
    c.execute("DELETE FROM app_devices WHERE user_id=?", (uid,))
    c.execute("DELETE FROM admin_customer_links WHERE admin_id=?", (uid,))
    c.execute("DELETE FROM users WHERE id=?", (uid,))
    c.commit()
    c.close()
    return jsonify({"ok": True, "unlinked": True})


@app.get("/api/dashboard")
def dashboard():
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    c = conn()

    # For customer users, restrict every aggregate to their own
    # customer + location + department. Tenant-bound staff (engineers/admins)
    # are scoped to their care-list customer(s).
    cust_scopes = []
    if u["role"] == "customer":
        if u.get("customer_id"):
            cust_scopes.append(("customer_id", u["customer_id"]))
        if u.get("location_id"):
            cust_scopes.append(("location_id", u["location_id"]))
        if u.get("department_id"):
            cust_scopes.append(("department_id", u["department_id"]))
    else:
        scope = tenant_scope(u, c)
        if scope:
            cust_scopes.append(("customer_id", scope))
    total_cust = 1 if (u["role"] == "customer") else (
        len(scope) if scope else c.execute("SELECT COUNT(*) n FROM customers").fetchone()["n"])

    def scoped(sql, params=None, cols=None):
        """Append customer-scope conditions (as WHERE/AND) before any
        GROUP BY / ORDER BY / LIMIT clause. `cols` maps a scope key to the
        qualified column name (defaults to the bare column). A list value
        becomes an IN (...) clause."""
        params = list(params or [])
        if not cust_scopes:
            return sql, params
        cols = cols or {}
        conds = []
        for key, val in cust_scopes:
            col = cols.get(key, key)
            if isinstance(val, (list, tuple)):
                marks = ", ".join("?" for _ in val)
                conds.append(f"{col} IN ({marks})")
                params.extend(val)
            else:
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
    elif scope:
        marks = ", ".join("?" for _ in scope)
        pm_due = c.execute(
            f"SELECT COUNT(*) n FROM pm_schedules WHERE active=1 AND customer_id IN ({marks}) AND (next_due_at IS NULL OR next_due_at <= ?)",
            list(scope) + [now()]).fetchone()["n"]
        pm_total = c.execute(
            f"SELECT COUNT(*) n FROM pm_schedules WHERE active=1 AND customer_id IN ({marks})",
            list(scope)).fetchone()["n"]
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
            if isinstance(val, (list, tuple)):
                marks = ", ".join("?" for _ in val)
                te_sql += f" AND b.{key} IN ({marks})"
                te_pp.extend(val)
            else:
                te_sql += f" AND b.{key} = ?"
                te_pp.append(val)
    te_sql += " GROUP BY e.name ORDER BY n DESC LIMIT 5"
    top_equip = c.execute(te_sql, te_pp).fetchall()

    # Customer satisfaction on settled tickets. Ratings are all-time rather than
    # windowed like the counts above: this is a cumulative service-quality
    # figure, and letting it slide over a 3-month window would make the average
    # move as tickets age out rather than as service changes.
    fb_ratings = []
    for fb_kind, fb_table in (("complaint", "complaints"), ("breakdown", "breakdowns")):
        fb_sql, fb_pp = scoped(
            "SELECT r.rating AS rating FROM ticket_ratings r "
            "JOIN %s t ON t.id=r.entity_id AND r.entity_type='%s'" % (fb_table, fb_kind),
            cols={"customer_id": "t.customer_id", "location_id": "t.location_id",
                  "department_id": "t.department_id"})
        fb_ratings += [x["rating"] for x in c.execute(fb_sql, fb_pp).fetchall()]
    fb_count = len(fb_ratings)
    fb_avg = round(sum(fb_ratings) / float(fb_count), 2) if fb_count else None

    # recent activity (3-month window)
    rc_sql, rc_pp = scoped(
        "SELECT cmp.id, cmp.code, cmp.subject, cmp.status, cmp.priority, cmp.created_at, cmp.customer_id, "
        "au.name AS accepted_by_name FROM complaints cmp LEFT JOIN users au ON au.id=cmp.accepted_by "
        "WHERE cmp.created_at >= " + WIN + " ORDER BY cmp.created_at DESC LIMIT 5",
        cols={"customer_id": "cmp.customer_id", "location_id": "cmp.location_id", "department_id": "cmp.department_id"})
    recent_cmp = c.execute(rc_sql, rc_pp).fetchall()
    rb_sql, rb_pp = scoped(
        "SELECT brk.id, brk.code, brk.fault_description, brk.status, brk.priority, brk.created_at, brk.customer_id, "
        "au.name AS accepted_by_name FROM breakdowns brk LEFT JOIN users au ON au.id=brk.accepted_by "
        "WHERE brk.created_at >= " + WIN + " ORDER BY brk.created_at DESC LIMIT 5",
        cols={"customer_id": "brk.customer_id", "location_id": "brk.location_id", "department_id": "brk.department_id"})
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
        "customer_feedback": {
            "average_rating": fb_avg,
            "rating_count": fb_count,
        },
    })


# --------------------------------------------------------------------------
# Notifications (in-app)
# --------------------------------------------------------------------------
@app.get("/api/notifications")
def list_notifications():
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    c = conn()
    rows = c.execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
        (u["id"],)).fetchall()
    out = rows_to_dicts(rows)
    # annotate tickets referenced by these notifications so the client can flag
    # jobs that have already been accepted
    cp_ids = [r["entity_id"] for r in out if r.get("entity_type") == "complaint" and r.get("entity_id")]
    br_ids = [r["entity_id"] for r in out if r.get("entity_type") == "breakdown" and r.get("entity_id")]
    accepted = set()
    if cp_ids:
        marks = ", ".join("?" for _ in cp_ids)
        for rr in c.execute(
                f"SELECT id FROM complaints WHERE id IN ({marks}) AND accepted_by IS NOT NULL",
                cp_ids).fetchall():
            accepted.add(("complaint", rr["id"]))
    if br_ids:
        marks = ", ".join("?" for _ in br_ids)
        for rr in c.execute(
                f"SELECT id FROM breakdowns WHERE id IN ({marks}) AND accepted_by IS NOT NULL",
                br_ids).fetchall():
            accepted.add(("breakdown", rr["id"]))
    # Care decisions: say whether the organization is still unclaimed, who took
    # it, or whether the request was withdrawn — so the notification stops
    # offering buttons for a decision that is already settled.
    pc_ids = [r["entity_id"] for r in out if r.get("entity_type") == "pending_care" and r.get("entity_id")]
    care = {}
    if pc_ids:
        marks = ", ".join("?" for _ in pc_ids)
        for rr in c.execute(f"SELECT id, pending_care FROM customers WHERE id IN ({marks})", pc_ids).fetchall():
            care[rr["id"]] = {"pending": bool(rr["pending_care"]), "claimed_by": None}
        still = [i for i, v in care.items() if not v["pending"]]
        if still:
            marks2 = ", ".join("?" for _ in still)
            for rr in c.execute(
                    f"SELECT l.customer_id, u.name AS admin_name FROM admin_customer_links l "
                    f"LEFT JOIN users u ON u.id=l.admin_id WHERE l.customer_id IN ({marks2}) "
                    f"ORDER BY l.created_at DESC", still).fetchall():
                if not care[rr["customer_id"]]["claimed_by"]:
                    care[rr["customer_id"]]["claimed_by"] = rr["admin_name"] or "another admin"
    c.close()
    for r in out:
        r["accepted"] = (r.get("entity_type"), r.get("entity_id")) in accepted
        st = care.get(r.get("entity_id")) if r.get("entity_type") == "pending_care" else None
        r["care_pending"] = bool(st and st["pending"])
        r["care_claimed_by"] = (st or {}).get("claimed_by")
    return jsonify(out)


@app.get("/api/notifications/ping")
def notifications_ping():
    """Lightweight poll: unread count, a change stamp, and the latest unread
    notification so the client can play an audible alert for new tickets."""
    u, err, code = require_role("admin", "engineer", "application", "customer")
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
    u, err, code = require_role("admin", "engineer", "application", "customer")
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
# Web Push (bell alerts that ring even when the app / tab is closed)
# --------------------------------------------------------------------------
@app.get("/api/push/vapid-key")
def push_vapid_key():
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    return jsonify({"public_key": push_mod.vapid_public_key()})


@app.get("/api/push/status")
def push_status():
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    c = conn()
    rows = c.execute(
        "SELECT id, endpoint, alert_on, created_at FROM push_subscriptions "
        "WHERE user_id=? ORDER BY id DESC", (u["id"],)).fetchall()
    c.close()
    return jsonify({
        "enabled": bool(push_mod._vapid_private_env()),
        "devices": rows_to_dicts(rows),
    })


@app.post("/api/push/subscribe")
def push_subscribe():
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    b = get_body()
    try:
        out = push_mod.save_subscription(u["id"], b)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, **out})


@app.post("/api/push/unsubscribe")
def push_unsubscribe():
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    b = get_body() or {}
    push_mod.remove_subscription(u["id"], (b.get("endpoint") or "").strip())
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Native app devices (Firebase FCM)
# --------------------------------------------------------------------------
@app.get("/api/app/devices")
def app_devices():
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    c = conn()
    rows = c.execute(
        "SELECT id, platform, active, alert_on, device_name, created_at "
        "FROM app_devices WHERE user_id=? ORDER BY id DESC", (u["id"],)).fetchall()
    c.close()
    return jsonify(rows_to_dicts(rows))


@app.post("/api/app/register")
def app_register():
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    b = get_body()
    try:
        out = apppush_mod.register_device(u["id"], b)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, **out})


@app.post("/api/app/unregister")
def app_unregister():
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    b = get_body() or {}
    apppush_mod.remove_device(u["id"], (b.get("push_token") or "").strip())
    return jsonify({"ok": True})


@app.get("/api/app/config")
def app_config():
    """Public bootstrap for the LabSynch native app (no auth)."""
    cfg = {
        "api_base": (os.environ.get("LABCARE_APP_URL") or "").strip()
                    or "https://labcare.insforge.site",
        "app_name": "LabSynch",
    }
    return jsonify(cfg)


# --------------------------------------------------------------------------
# Reports & export
# --------------------------------------------------------------------------
@app.get("/api/export.csv")
def export_csv():
    u, err, code = require_role("admin", "engineer", "application")
    if err:
        return err, code
    entity = request.args.get("type", "complaints")
    import csv as csv_mod
    c = conn()
    out = ""
    scope = tenant_scope(u, c)
    where_sql, where_params = "", []
    if scope:
        marks = ", ".join("?" for _ in scope)
        where_sql = f" WHERE customer_id IN ({marks})"
        where_params = list(scope)
    if entity == "breakdowns":
        rows = c.execute(
            "SELECT * FROM breakdowns" + where_sql + " ORDER BY created_at DESC",
            where_params).fetchall()
        header = ["Code", "EquipmentID", "CustomerID", "ComplaintID", "FaultDescription", "RootCause",
                  "Priority", "Status", "ReportedBy", "ReporterName", "ReporterPhone", "AssignedTo",
                  "ResolutionNotes", "CreatedAt", "ResolvedAt"]
        keys = ["code", "equipment_id", "customer_id", "complaint_id", "fault_description", "root_cause",
                "priority", "status", "reported_by", "reporter_name", "reporter_phone", "assigned_to",
                "resolution_notes", "created_at", "resolved_at"]
    else:
        rows = c.execute(
            "SELECT * FROM complaints" + where_sql + " ORDER BY created_at DESC",
            where_params).fetchall()
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
    fname = f"labcare_{entity}_{now_dt().strftime('%Y%m%d_%H%M')}.csv"
    return Response(data, mimetype="text/csv", headers={
        "Content-Disposition": f"attachment; filename={fname}"})


@app.get("/api/complaints/<int:cid>/report.pdf")
def complaint_report_pdf(cid):
    u, err, code = require_role("admin", "engineer", "application", "customer")
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
    feedback = _feedback_payload(c, "complaint", cid)
    c.close()

    pdf = report_mod.service_report(comp, breakdowns, comments, feedback=feedback)
    return Response(pdf, mimetype="application/pdf", headers={
        "Content-Disposition": f'inline; filename="service_report_{comp["code"]}.pdf"'})


@app.get("/api/breakdowns/<int:bid>/report.pdf")
def breakdown_report_pdf(bid):
    """Service report PDF for a single breakdown ticket — the breakdown
    counterpart of complaint_report_pdf(). Breakdown tickets no longer take
    file attachments, so this report is what the ticket offers instead."""
    u, err, code = require_role("admin", "engineer", "application", "customer")
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
    brk = breakdown_payload(c, row)
    comments = rows_to_dicts(c.execute(
        "SELECT cm.*, u.name AS user_name FROM comments cm JOIN users u ON u.id=cm.user_id "
        "WHERE cm.entity_type='breakdown' AND cm.entity_id=? ORDER BY cm.created_at", (bid,)).fetchall())
    # Name the complaint this work order was raised from, if there is one.
    src = None
    if row["complaint_id"]:
        crow = c.execute("SELECT code, subject FROM complaints WHERE id=?",
                         (row["complaint_id"],)).fetchone()
        src = dict(crow) if crow else None
    feedback = _feedback_payload(c, "breakdown", bid)
    c.close()

    pdf = report_mod.breakdown_report(brk, comments, src, feedback=feedback)
    return Response(pdf, mimetype="application/pdf", headers={
        "Content-Disposition": f'inline; filename="service_report_{brk["code"]}.pdf"'})


@app.get("/api/reports/trend.pdf")
def trend_report_pdf():
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    c = conn()
    if u["role"] == "customer":
        cust_scope = [u["customer_id"]] if u.get("customer_id") else None
    else:
        cust_scope = tenant_scope(u, c)

    def scope(sql, params=None, col="customer_id"):
        params = list(params or [])
        if not cust_scope:
            return sql, params
        if isinstance(cust_scope, (list, tuple)):
            marks = ", ".join("?" for _ in cust_scope)
            cond = f"{col} IN ({marks})"
            extra = list(cust_scope)
        else:
            cond = f"{col} = ?"
            extra = [cust_scope]
        up = sql.upper()
        anchor = len(sql)
        for kw in ("GROUP BY", "ORDER BY", "LIMIT"):
            i = up.find(kw)
            if i != -1:
                anchor = min(anchor, i)
        if "WHERE" in up:
            sql = sql[:anchor] + " AND " + cond + " " + sql[anchor:]
        else:
            sql = sql[:anchor] + " WHERE " + cond + " " + sql[anchor:]
        params.extend(extra)
        return sql, params

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
    if cust_scope:
        if isinstance(cust_scope, (list, tuple)):
            marks = ", ".join("?" for _ in cust_scope)
            te_sql += f" WHERE b.customer_id IN ({marks})"
            te_pp.extend(cust_scope)
        else:
            te_sql += " WHERE b.customer_id = ?"
            te_pp.append(cust_scope)
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
    d["equipment_name"] = f"{eq['name']} — {eq['model']}" if eq and eq["model"] else (eq["name"] if eq else None)
    d["equipment_serial"] = eq["serial_number"] if eq else None
    d["equipment_model"] = eq["model"] if eq else None
    d["assigned_to_name"] = assignee["name"] if assignee else None
    d["log_count"] = c.execute("SELECT COUNT(*) n FROM pm_logs WHERE schedule_id=?", (d["id"],)).fetchone()["n"]
    return d


@app.get("/api/pms")
def list_pms():
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    c = conn()
    where, params = [], []
    if u["role"] == "customer":
        # customers see PM schedules for their own location & department's equipment
        if u.get("customer_id"):
            where.append("p.customer_id=?")
            params.append(u["customer_id"])
    else:
        scope = tenant_scope(u, c)
        if scope:
            marks = ", ".join("?" for _ in scope)
            where.append(f"p.customer_id IN ({marks})")
            params.extend(scope)
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
    u, err, code = require_role("admin", "engineer", "application")
    if err:
        return err, code
    b = get_body()
    if not (b.get("title") or "").strip():
        return jsonify({"error": "Title is required"}), 400
    if not b.get("customer_id"):
        return jsonify({"error": "Organization is required"}), 400
    err_t, code_t = tenant_guard(u, b["customer_id"])
    if err_t:
        return err_t, code_t
    interval_days = int(b.get("interval_days") or 90)
    if interval_days < 1:
        return jsonify({"error": "Interval must be at least 1 day"}), 400
    next_due = b.get("next_due_at") or None
    if next_due:
        next_due = (next_due[:10] + " 00:00:00") if len(next_due) <= 10 else next_due
    c = conn()
    if not assignee_allowed(u, b.get("assigned_to") or None, c):
        c.close()
        return jsonify({"error": "You can only assign to your own team"}), 403
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
    u, err, code = require_role("admin", "engineer", "application")
    if err:
        return err, code
    b = get_body()
    c = conn()
    row = c.execute("SELECT * FROM pm_schedules WHERE id=?", (pid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    err_t, code_t = tenant_guard(u, row["customer_id"])
    if err_t:
        c.close()
        return err_t, code_t
    if "customer_id" in b:
        err_t, code_t = tenant_guard(u, b["customer_id"])
        if err_t:
            c.close()
            return err_t, code_t
    if "assigned_to" in b and not assignee_allowed(u, b["assigned_to"] or None, c):
        c.close()
        return jsonify({"error": "You can only assign to your own team"}), 403
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
    u, err, code = require_role("admin", "engineer", "application")
    if err:
        return err, code
    c = conn()
    row = c.execute("SELECT customer_id FROM pm_schedules WHERE id=?", (pid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    err_t, code_t = tenant_guard(u, row["customer_id"])
    if err_t:
        c.close()
        return err_t, code_t
    c.execute("DELETE FROM pm_schedules WHERE id=?", (pid,))
    c.commit()
    c.close()
    return jsonify({"ok": True})


@app.post("/api/pms/<int:pid>/complete")
def complete_pm(pid):
    u, err, code = require_role("admin", "engineer", "application")
    if err:
        return err, code
    b = get_body()
    c = conn()
    row = c.execute("SELECT * FROM pm_schedules WHERE id=?", (pid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    err_t, code_t = tenant_guard(u, row["customer_id"])
    if err_t:
        c.close()
        return err_t, code_t
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
    u, err, code = require_role("admin", "engineer", "application", "customer")
    if err:
        return err, code
    c = conn()
    row = c.execute("SELECT customer_id FROM pm_schedules WHERE id=?", (pid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    if not _customer_allowed(u, row["customer_id"]):
        c.close()
        return jsonify({"error": "Not authorised"}), 403
    rows = c.execute(
        "SELECT l.*, COALESCE(u.name, 'Former user') AS performed_by_name FROM pm_logs l LEFT JOIN users u ON u.id=l.performed_by "
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
    base = (os.environ.get("LABCARE_PORTAL_URL") or "").strip() or _public_base()
    url = f"{base.rstrip('/')}/portal.html?t={token}"
    img = _qr.make(url)
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _public_base():
    """Best-effort public origin for links that users open on their own devices
    (QR codes). Prefer an explicit LABCARE_PORTAL_URL; otherwise derive from the
    Host header, forcing https when the request arrived via a TLS proxy."""
    scheme = "https" if request.headers.get("X-Forwarded-Proto") == "https" else request.scheme
    host = request.headers.get("X-Forwarded-Host") or request.host
    return f"{scheme}://{host}"


@app.get("/api/portal-links")
def list_portal_links():
    u, err, code = require_role("admin", "engineer", "application")
    if err:
        return err, code
    c = conn()
    where, params = "", []
    scope = tenant_scope(u, c)
    if scope:
        marks = ", ".join("?" for _ in scope)
        where = f" WHERE pl.customer_id IN ({marks})"
        params = list(scope)
    rows = c.execute(
        "SELECT pl.*, cu.name AS customer_name, e.name AS equipment_name, e.model AS equipment_model, e.serial_number AS equipment_serial "
        "FROM portal_links pl JOIN customers cu ON cu.id=pl.customer_id "
        "LEFT JOIN equipment e ON e.id=pl.equipment_id " + where +
        " ORDER BY pl.created_at DESC", params).fetchall()
    c.close()
    return jsonify(rows_to_dicts(rows))


@app.post("/api/portal-links")
def create_portal_link():
    u, err, code = require_role("admin", "engineer", "application")
    if err:
        return err, code
    b = get_body()
    if not b.get("customer_id"):
        return jsonify({"error": "Organization is required"}), 400
    err_t, code_t = tenant_guard(u, b["customer_id"])
    if err_t:
        return err_t, code_t
    token = uuid.uuid4().hex[:16]
    c = conn()
    cur = c.execute(
        "INSERT INTO portal_links (token,customer_id,equipment_id,label,active,created_by,created_at) VALUES (?,?,?,?,1,?,?)",
        (token, b["customer_id"], b.get("equipment_id") or None, b.get("label", ""), u["id"], now()),
    )
    c.commit()
    row = c.execute(
        "SELECT pl.*, cu.name AS customer_name, e.name AS equipment_name, e.model AS equipment_model, e.serial_number AS equipment_serial FROM portal_links pl "
        "JOIN customers cu ON cu.id=pl.customer_id LEFT JOIN equipment e ON e.id=pl.equipment_id WHERE pl.id=?",
        (cur.lastrowid,)).fetchone()
    c.close()
    return jsonify(dict(row)), 201


@app.get("/api/portal-links/<int:lid>/qr")
def portal_link_qr(lid):
    u, err, code = require_role("admin", "engineer", "application")
    if err:
        return err, code
    c = conn()
    row = c.execute("SELECT * FROM portal_links WHERE id=?", (lid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    err_t, code_t = tenant_guard(u, row["customer_id"])
    if err_t:
        c.close()
        return err_t, code_t
    c.close()
    png = _portal_qr_png(row["token"], row["label"])
    return Response(png, mimetype="image/png", headers={
        "Content-Disposition": f'inline; filename="portal_{row["token"]}.png"'})


@app.patch("/api/portal-links/<int:lid>")
def update_portal_link(lid):
    u, err, code = require_role("admin", "engineer", "application")
    if err:
        return err, code
    b = get_body()
    c = conn()
    existing = c.execute("SELECT customer_id FROM portal_links WHERE id=?", (lid,)).fetchone()
    if not existing:
        c.close()
        return jsonify({"error": "Not found"}), 404
    err_t, code_t = tenant_guard(u, existing["customer_id"])
    if err_t:
        c.close()
        return err_t, code_t
    if "customer_id" in b:
        err_t, code_t = tenant_guard(u, b["customer_id"])
        if err_t:
            c.close()
            return err_t, code_t
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
        "SELECT pl.*, cu.name AS customer_name, e.name AS equipment_name, e.model AS equipment_model, e.serial_number AS equipment_serial FROM portal_links pl "
        "JOIN customers cu ON cu.id=pl.customer_id LEFT JOIN equipment e ON e.id=pl.equipment_id WHERE pl.id=?",
        (lid,)).fetchone()
    c.close()
    return jsonify(dict(row)) if row else (jsonify({"error": "Not found"}), 404)


@app.delete("/api/portal-links/<int:lid>")
def delete_portal_link(lid):
    u, err, code = require_role("admin", "engineer", "application")
    if err:
        return err, code
    c = conn()
    row = c.execute("SELECT customer_id FROM portal_links WHERE id=?", (lid,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    err_t, code_t = tenant_guard(u, row["customer_id"])
    if err_t:
        c.close()
        return err_t, code_t
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
    open_sql = ("SELECT c.id, c.code, c.subject, c.status, c.priority, c.created_at, "
                "c.accepted_by, c.accepted_at, c.accept_reply, u.name AS accepted_by_name, u.phone AS accepted_by_phone "
                "FROM complaints c LEFT JOIN users u ON u.id=c.accepted_by "
                "WHERE {col}=? AND c.status IN ('open','in_progress') "
                "ORDER BY c.created_at DESC LIMIT 5")
    brk_sql = ("SELECT b.id, b.code, b.fault_description AS subject, b.status, b.priority, b.created_at, "
               "b.accepted_by, b.accepted_at, b.accept_reply, u.name AS accepted_by_name, u.phone AS accepted_by_phone "
               "FROM breakdowns b LEFT JOIN users u ON u.id=b.accepted_by "
               "WHERE {col}=? AND b.status != 'resolved' "
               "ORDER BY b.created_at DESC LIMIT 5")
    if d.get("equipment_id"):
        eq = c.execute("SELECT * FROM equipment WHERE id=?", (d["equipment_id"],)).fetchone()
        if eq:
            d["equipment"] = dict(eq)
            # include open tickets for context
            open_cmp = c.execute(open_sql.format(col="c.equipment_id"), (d["equipment_id"],)).fetchall()
            open_brk = c.execute(brk_sql.format(col="b.equipment_id"), (d["equipment_id"],)).fetchall()
            d["open_complaints"] = [dict(r, kind="complaint") for r in open_cmp]
            d["open_breakdowns"] = [dict(r, kind="breakdown") for r in open_brk]
        else:
            d["equipment"] = None
            d["open_complaints"] = []
            d["open_breakdowns"] = []
    else:
        d["equipment"] = None
        d["open_complaints"] = [dict(r, kind="complaint") for r in
            c.execute(open_sql.format(col="c.customer_id"), (d["customer_id"],)).fetchall()]
        d["open_breakdowns"] = [dict(r, kind="breakdown") for r in
            c.execute(brk_sql.format(col="b.customer_id"), (d["customer_id"],)).fetchall()]
    c.close()
    d.pop("created_by", None)
    return jsonify(d)


@app.post("/api/portal/<token>/complaints")
def portal_submit_complaint(token):
    """Public submission entry point. The reporter picks what they are filing.

    kind == 'complaint' → a complaints row (subject + description).
    kind == 'breakdown' → a breakdowns row (fault_description).
    Both capture the caller's name + phone and notify the customer's team.
    """
    c = conn()
    row = c.execute(
        "SELECT * FROM portal_links WHERE token=? AND active=1", (token,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Invalid or expired link"}), 404
    body = get_body()
    kind = (body.get("kind") or "complaint").strip().lower()
    if kind not in ("complaint", "breakdown"):
        kind = "complaint"
    subject = (body.get("subject") or "").strip()
    if not subject:
        c.close()
        return jsonify({"error": "Please describe the problem"}), 400
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
    # portal tickets are owned by the customer's tenant admin (unambiguous = auto)
    ra_id = _customer_tenant_admin_id(c, row["customer_id"])

    if kind == "breakdown":
        code_ = next_code_for("breakdowns", "BRK")
        cur = c.execute(
            "INSERT INTO breakdowns (code,customer_id,equipment_id,location_id,department_id,"
            "fault_description,priority,status,reported_by,assigned_to,created_at,updated_at,"
            "reporter_name,reporter_phone,responsible_admin_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code_, row["customer_id"], equip_id, eq_loc, eq_dept, subject,
             body.get("priority", "medium"), "reported",
             created_by, None, now(), now(), reporter_name, reporter_phone, ra_id),
        )
        c.commit()
        newrow = c.execute("SELECT * FROM breakdowns WHERE id=?", (cur.lastrowid,)).fetchone()
        out = breakdown_payload(c, newrow)
        maker = c.execute("SELECT name FROM users WHERE id=?", (created_by,)).fetchone()
        creator_name = maker["name"] if maker else ""
        c.close()
        audit("breakdown", out["id"], {"id": created_by, "name": creator_name},
              "created", f"Opened {out['code']} via portal — {out['fault_description']}")
        _archive_and_purge("breakdown")
        ping_team("breakdown", created_by, out,
                  f"New breakdown {out['code']} via portal: {out['fault_description'][:90]}",
                  lambda r: email_mod.email_status_changed("breakdown", r, out, "reported"))
        return jsonify(out), 201

    code_ = next_code_for("complaints", "CMP")
    cur = c.execute(
        "INSERT INTO complaints (code,customer_id,equipment_id,location_id,department_id,subject,description,category,priority,status,created_by,assigned_to,created_at,updated_at,reporter_name,reporter_phone,responsible_admin_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code_, row["customer_id"], equip_id, eq_loc, eq_dept, subject, body.get("description", ""),
         body.get("category", "General"), body.get("priority", "medium"), "open",
         created_by, None, now(), now(), reporter_name, reporter_phone, ra_id),
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
    cmp_rows = c.execute(
        "SELECT c.id, c.code, c.subject, c.status, c.priority, c.created_at, c.equipment_id, "
        "c.accepted_by, c.accepted_at, c.accept_reply, u.name AS accepted_by_name, u.phone AS accepted_by_phone "
        "FROM complaints c LEFT JOIN users u ON u.id=c.accepted_by "
        "WHERE c.customer_id=? ORDER BY c.created_at DESC LIMIT 20", (row["customer_id"],)).fetchall()
    brk_rows = c.execute(
        "SELECT b.id, b.code, b.fault_description AS subject, b.status, b.priority, b.created_at, b.equipment_id, "
        "b.accepted_by, b.accepted_at, b.accept_reply, u.name AS accepted_by_name, u.phone AS accepted_by_phone "
        "FROM breakdowns b LEFT JOIN users u ON u.id=b.accepted_by "
        "WHERE b.customer_id=? ORDER BY b.created_at DESC LIMIT 20", (row["customer_id"],)).fetchall()
    out = [dict(r, kind="complaint") for r in cmp_rows] + [dict(r, kind="breakdown") for r in brk_rows]
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    out = out[:20]
    # Satisfaction rides along with the history rows so the portal can show a
    # rating and offer the feedback form without a request per ticket.
    for kind in ("complaint", "breakdown"):
        ids = [r["id"] for r in out if r["kind"] == kind]
        summ = _feedback_summary(c, kind, ids)
        for r in out:
            if r["kind"] != kind:
                continue
            s = summ.get(r["id"], {})
            r["rating"] = s.get("rating")
            r["feedback_comments"] = s.get("comments", 0)
            r["feedback_open"] = r["status"] in FEEDBACK_STATUSES[kind]
    c.close()
    return jsonify(out)

def _portal_link(c, token):
    return c.execute("SELECT * FROM portal_links WHERE token=? AND active=1", (token,)).fetchone()


def _portal_ticket(c, link, kind, tid):
    """The ticket a portal visitor may give feedback on, or None.

    Scoped exactly like the portal's own history list — every ticket belonging
    to the link's organization — so a visitor can respond to any settled ticket
    they are shown, and never to another organization's."""
    if kind not in FEEDBACK_TABLES:
        return None
    row = _feedback_ticket(c, kind, tid)
    if not row or row["customer_id"] != link["customer_id"]:
        return None
    return row


@app.get("/api/portal/<token>/feedback")
def portal_feedback(token):
    """One settled ticket's rating and feedback thread, for the public portal."""
    kind = (request.args.get("kind") or "complaint").strip().lower()
    tid = _id(request.args.get("id"))
    if kind not in FEEDBACK_TABLES or not tid:
        return jsonify({"error": "Invalid ticket"}), 400
    c = conn()
    link = _portal_link(c, token)
    if not link:
        c.close()
        return jsonify({"error": "Invalid or expired link"}), 404
    row = _portal_ticket(c, link, kind, tid)
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    out = _feedback_payload(c, kind, tid)
    out["settled"] = _feedback_settled(row, kind)
    out["status"] = row["status"]
    out["code"] = row["code"]
    c.close()
    return jsonify(out)


@app.post("/api/portal/<token>/rating")
def portal_rate(token):
    """Rate a settled ticket from the QR portal — no account needed.

    The visitor's name is optional; without one the rating is attributed to
    "Customer"."""
    body = get_body()
    kind = (body.get("kind") or "complaint").strip().lower()
    tid = _id(body.get("id"))
    if kind not in FEEDBACK_TABLES or not tid:
        return jsonify({"error": "Invalid ticket"}), 400
    rating = _rating_value(body)
    if not rating:
        return jsonify({"error": "Choose a rating from 1 to 5 stars"}), 400
    c = conn()
    link = _portal_link(c, token)
    if not link:
        c.close()
        return jsonify({"error": "Invalid or expired link"}), 404
    row = _portal_ticket(c, link, kind, tid)
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    if not _feedback_settled(row, kind):
        c.close()
        return jsonify({"error": "Feedback opens once this ticket is resolved or closed"}), 409
    name = (body.get("name") or "").strip()[:80]
    rid, created = _save_rating(c, kind, tid, row["customer_id"], rating, None, name)
    c.commit()
    c.close()
    audit(kind, tid, {"id": None, "name": name or "Customer"}, "rating",
          "%d star%s" % (rating, "" if rating == 1 else "s"))
    return jsonify({"ok": True, "id": rid, "rating": rating, "created": created}), (201 if created else 200)


@app.post("/api/portal/<token>/feedback")
def portal_comment(token):
    """Add a comment to a settled ticket's feedback thread from the QR portal."""
    body = get_body()
    kind = (body.get("kind") or "complaint").strip().lower()
    tid = _id(body.get("id"))
    if kind not in FEEDBACK_TABLES or not tid:
        return jsonify({"error": "Invalid ticket"}), 400
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Please write your feedback"}), 400
    if len(text) > FEEDBACK_MAX_LEN:
        return jsonify({"error": "Feedback is limited to %d characters" % FEEDBACK_MAX_LEN}), 400
    c = conn()
    link = _portal_link(c, token)
    if not link:
        c.close()
        return jsonify({"error": "Invalid or expired link"}), 404
    row = _portal_ticket(c, link, kind, tid)
    if not row:
        c.close()
        return jsonify({"error": "Not found"}), 404
    if not _feedback_settled(row, kind):
        c.close()
        return jsonify({"error": "Feedback opens once this ticket is resolved or closed"}), 409
    name = (body.get("name") or "").strip()[:80]
    cur = c.execute(
        "INSERT INTO ticket_feedback (entity_type,entity_id,customer_id,user_id,author_name,text,created_at) "
        "VALUES (?,?,?,?,?,?,?)", (kind, tid, row["customer_id"], None, name, text, now()))
    c.commit()
    new_id = cur.lastrowid
    c.close()
    audit(kind, tid, {"id": None, "name": name or "Customer"}, "feedback", text[:120])
    return jsonify({"ok": True, "id": new_id, "author_name": name or "Customer",
                    "text": text}), 201



@app.get("/api/portal/<token>/events")
def portal_events(token):
    """Staff-activity feed for an open portal page: poll with ?since_id=<n>
    to get new in-app responses (comment / accepted / status / resolution /
    assigned) on the portal scope's tickets since the last seen audit id.

    Only staff actions are returned — 'created', 'updated' and 'attachment'
    entries are excluded, so a reporter never alarms for their own
    submission. Scoped like the portal itself: its equipment, else customer.
    """
    c = conn()
    row = c.execute(
        "SELECT * FROM portal_links WHERE token=? AND active=1", (token,)).fetchone()
    if not row:
        c.close()
        return jsonify({"error": "Invalid or expired link"}), 404
    try:
        since_id = int(request.args.get("since_id", "0") or 0)
    except ValueError:
        since_id = 0
    scope_col = "t.equipment_id" if row["equipment_id"] else "t.customer_id"
    scope_val = row["equipment_id"] if row["equipment_id"] else row["customer_id"]
    actions = ("comment", "accepted", "status", "resolution", "assigned")
    marks = ", ".join("?" for _ in actions)
    q = ("SELECT au.id, au.entity_type AS kind, au.action, COALESCE(au.user_name, '') AS actor, "
         "au.detail, au.created_at AS at, t.code "
         "FROM audit_logs au JOIN {table} t ON au.entity_type=? AND au.entity_id=t.id "
         f"WHERE au.user_id IS NOT NULL AND au.id > ? AND {scope_col}=? "
         f"AND au.action IN ({marks}) ORDER BY au.id LIMIT 30")
    events = [
        dict(r) for r in c.execute(
            q.format(table="complaints"),
            ("complaint", since_id, scope_val, *actions)).fetchall()
    ] + [
        dict(r) for r in c.execute(
            q.format(table="breakdowns"),
            ("breakdown", since_id, scope_val, *actions)).fetchall()
    ]
    events.sort(key=lambda e: e["id"])
    events = events[:30]
    latest = c.execute("SELECT MAX(id) AS m FROM audit_logs").fetchone()["m"] or 0
    c.close()
    return jsonify({"events": events, "latest_id": latest})


# --------------------------------------------------------------------------
# Version check
# --------------------------------------------------------------------------
APP_VERSION = "49"

@app.get("/api/version")
def api_version():
    return jsonify({"version": APP_VERSION, "ok": True})


@app.after_request
def set_no_cache_headers(response):
    if request.path == "/" or request.path.endswith(".html") or request.path.endswith(".js"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# --------------------------------------------------------------------------
# Static (mobile web app)
# --------------------------------------------------------------------------
@app.get("/")
def index():
    resp = send_from_directory(STATIC_DIR, "index.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/<path:path>")
def static_files(path):
    full = os.path.join(STATIC_DIR, path)
    if os.path.isfile(full):
        resp = send_from_directory(STATIC_DIR, path)
    else:
        resp = send_from_directory(STATIC_DIR, "index.html")
    if path == "index.html" or path.endswith(".html") or path.endswith(".js"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


# --------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    from seed import seed
    seed()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
