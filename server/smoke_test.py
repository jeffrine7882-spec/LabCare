"""End-to-end smoke test of the Flask API against InsForge Postgres.

Run with LABCARE_DATABASE_URL set. Uses Flask's test client (in-process).
Exercises every table + the tricky SQL paths (INSERT OR IGNORE, lastrowid,
date('now',...), attachments BYTEA, trends, PDFs, portal).
"""
import io, os, sys, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("LABCARE_TICKET_CAP", "20")
os.environ.setdefault("LABCARE_HISTORY_LOG", "/tmp/labcare_smoke_history.log")
if os.path.exists("/tmp/labcare_smoke_history.log"):
    os.remove("/tmp/labcare_smoke_history.log")

from app import app

c = app.test_client()
PASS = []
FAIL = []


def check(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + ((" — " + str(extra)) if extra else ""))


def j(method, path, body=None, auth=None, raw=None):
    headers = {}
    if auth:
        headers["Authorization"] = "Bearer " + auth
    if raw is not None:
        return c.open(path, method=method, data=raw, headers=headers,
                      content_type="multipart/form-data")
    kwargs = dict(method=method, path=path, headers=headers)
    if body is not None:
        kwargs["data"] = json.dumps(body)
        kwargs["content_type"] = "application/json"
    return c.open(**kwargs)


# 0. health (no auth)
r = c.get("/api/health")
check("health", r.status_code == 200, r.get_data(as_text=True)[:80])

# 1. login as Master Admin
r = j("POST", "/api/login", {"email": "admin@labcare.com", "password": "Demo123!"})
ok = r.status_code == 200 and r.get_json().get("token")
tok = r.get_json().get("token") if r.status_code == 200 else None
me = r.get_json().get("user") if r.status_code == 200 else {}
check("login master", ok, f"user={me.get('email')} role={me.get('role')}")

# 2. create customer / location / department / equipment
r = j("POST", "/api/customers", {"name": "Smoke Labs"}, tok)
cid = r.get_json().get("id") if r.status_code == 201 else None
check("create customer", r.status_code == 201, cid)

r = j("POST", "/api/locations", {"customer_id": cid, "name": "Main Lab"}, tok)
lid = r.get_json().get("id") if r.status_code == 201 else None
check("create location", r.status_code == 201, lid)

r = j("POST", "/api/departments", {"location_id": lid, "name": "Pathology"}, tok)
did = r.get_json().get("id") if r.status_code == 201 else None
check("create department", r.status_code == 201, did)

r = j("POST", "/api/equipment", {
    "customer_id": cid, "location_id": lid, "department_id": did,
    "name": "Centrifuge", "model": "Epp 5810", "serial_number": "SMK-001",
    "category": "Centrifuges"}, tok)
eid = r.get_json().get("id") if r.status_code == 201 else None
check("create equipment", r.status_code == 201 and r.get_json().get("serial_number") == "SMK-001", eid)

# unique serial per customer enforced
r = j("POST", "/api/equipment", {"customer_id": cid, "name": "Dup", "serial_number": "SMK-001"}, tok)
check("duplicate serial blocked", r.status_code == 409, r.get_json())

# 3. create complaints — exercises next_code_for + INSERT + lastrowid
r = j("POST", "/api/complaints", {
    "customer_id": cid, "equipment_id": eid, "location_id": lid, "department_id": did,
    "subject": "Freezer alarm", "priority": "high"}, tok)
cpid = r.get_json().get("id") if r.status_code == 201 else None
check("create complaint", r.status_code == 201 and r.get_json().get("code", "").startswith("CMP-"), r.get_json().get("code"))

r = j("POST", "/api/complaints", {"customer_id": cid, "subject": "Second complaint"}, tok)
cpid2 = r.get_json().get("id") if r.status_code == 201 else None
check("create complaint 2", r.status_code == 201, r.get_json().get("code"))

# 4. breakdown (linked to complaint)
r = j("POST", "/api/breakdowns", {
    "customer_id": cid, "equipment_id": eid, "complaint_id": cpid,
    "fault_description": "Seal leaking"}, tok)
bid = r.get_json().get("id") if r.status_code == 201 else None
check("create breakdown", r.status_code == 201 and r.get_json().get("code", "").startswith("BRK-"), r.get_json().get("code"))

# 5. accept a complaint (status-on-accept + self-assign)
r = j("POST", f"/api/complaints/{cpid}/accept", {"reply": "On it.", "status": "in_progress"}, tok)
acc = r.get_json() if r.status_code == 200 else {}
check("accept complaint", r.status_code == 200 and acc.get("accepted_by") and acc.get("assigned_to"), dict(accepted_by=acc.get('accepted_by'), status=acc.get('status')))
r = j("POST", f"/api/complaints/{cpid}/accept", {"reply": "again"}, tok)
check("re-accept 409", r.status_code == 409)

# 6. comment (auto-assign path)
r = j("POST", "/api/comments", {"entity_type": "complaint", "entity_id": cpid2, "text": "hello"}, tok)
check("add comment", r.status_code == 201)

# 7. attachment (BYTEA)
png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
r = j("POST", "/api/attachments", None, tok,
      raw={"entity_type": "complaint", "entity_id": str(cpid), "file": (io.BytesIO(png), "shot.png")})
aid = r.get_json().get("id") if r.status_code == 201 else None
check("upload attachment", r.status_code == 201, aid)
r = c.get(f"/api/attachments/{aid}/file", headers={"Authorization": "Bearer " + tok})
check("download attachment (bytes equal)", r.status_code == 200 and r.data == png)

# 8. portal link + public submission (complaint + breakdown kinds)
r = j("POST", "/api/portal-links", {"customer_id": cid, "equipment_id": eid, "label": "QR"}, tok)
token = r.get_json().get("token") if r.status_code == 201 else None
check("create portal link", r.status_code == 201 and token)
r = j("POST", f"/api/portal/{token}/complaints", {"kind": "complaint", "subject": "Portal CMP", "name": "Ali", "phone": "012-345 6789"}, None)
check("portal complaint", r.status_code == 201 and r.get_json().get("code", "").startswith("CMP-"), r.get_json().get("reporter_name"))
r = j("POST", f"/api/portal/{token}/complaints", {"kind": "breakdown", "subject": "Portal BRK", "name": "Ali", "phone": "012-345 6789"}, None)
check("portal breakdown", r.status_code == 201 and r.get_json().get("code", "").startswith("BRK-"), r.get_json().get("reporter_name"))
r = c.get(f"/api/portal/{token}/history")
check("portal history", r.status_code == 200 and isinstance(r.get_json(), list))

# 9. users + technician + PM
r = j("POST", "/api/users", {"name": "Tech One", "email": "tech1@smoke.test", "password": "Demo123!",
                              "role": "technician", "customer_id": cid}, tok)
uid = r.get_json().get("id") if r.status_code == 201 else None
check("create user", r.status_code == 201, uid)

r = j("POST", "/api/pms", {"customer_id": cid, "equipment_id": eid, "title": "Annual PM",
                            "interval_days": 90, "assigned_to": uid}, tok)
pid = r.get_json().get("id") if r.status_code == 201 else None
check("create pm", r.status_code == 201, pid)
r = j("POST", f"/api/pms/{pid}/complete", {"notes": "done"}, tok)
check("complete pm", r.status_code == 200 and r.get_json().get("last_done_at"))

# 10. dashboard + notifications (exercises date('now'), substr trends)
r = j("GET", "/api/dashboard", None, tok)
check("dashboard", r.status_code == 200, json.dumps(r.get_json())[:100] if r.status_code == 200 else r.get_data(as_text=True)[:200])
r = j("GET", "/api/notifications", None, tok)
check("notifications", r.status_code == 200)
r = j("GET", "/api/notifications/ping", None, tok)
check("notifications ping", r.status_code == 200)

# 11. exports & regimes
r = j("GET", "/api/export.csv?entity=complaint", None, tok)
check("export csv", r.status_code == 200)
r = c.get(f"/api/complaints/{cpid}/report.pdf", headers={"Authorization": "Bearer " + tok})
check("complaint pdf", r.status_code == 200 and r.data[:4] == b"%PDF", len(r.data))
r = c.get("/api/reports/trend.pdf", headers={"Authorization": "Bearer " + tok})
check("trend pdf", r.status_code == 200 and r.data[:4] == b"%PDF", len(r.data))

# 12. flow-out: resolve a complaint, then delete (master only)
r = j("PATCH", f"/api/complaints/{cpid2}", {"status": "resolved"}, tok)
check("resolve complaint", r.status_code == 200 and r.get_json().get("status") == "resolved")
r = j("DELETE", f"/api/complaints/{cpid2}", None, tok)
check("delete complaint (master)", r.status_code == 200)

# 13. FIFO archive + purge path
for i in range(30):
    j("POST", "/api/complaints", {"customer_id": cid, "subject": f"bulk {i}"}, tok)
hist = os.path.exists("/tmp/labcare_smoke_history.log")
check("FIFO purge wrote history log", hist)

# 14. archive a breakdown (complaint_id NULL then delete)
#     (fresh pair — CMP-0001 may already have rolled off through the FIFO cap above)
r = j("POST", "/api/complaints", {"customer_id": cid, "subject": "Parent complaint"}, tok)
pcpid = r.get_json().get("id") if r.status_code == 201 else None
r = j("POST", "/api/breakdowns", {"customer_id": cid, "complaint_id": pcpid,
                                  "fault_description": "Linked fault"}, tok)
pbid = r.get_json().get("id") if r.status_code == 201 else None
check("create linked complaint + breakdown", bool(pcpid and pbid), (pcpid, pbid))
r = j("DELETE", f"/api/complaints/{pcpid}", None, tok)
check("delete linked complaint (master)", r.status_code == 200)
r = j("GET", f"/api/breakdowns/{pbid}", None, tok)
check("breakdown kept, complaint link nulled",
      r.status_code == 200 and r.get_json().get("complaint_id") is None,
      r.get_json().get("complaint_id") if r.status_code == 200 else r.status_code)

# 15. tenant-admin isolation: an admin manages ONE organisation and nothing else
r = j("POST", "/api/customers", {"name": "Other Labs"}, tok)
ocid = r.get_json().get("id") if r.status_code == 201 else None
r = j("POST", "/api/locations", {"customer_id": ocid, "name": "Other Site"}, tok)
olid = r.get_json().get("id") if r.status_code == 201 else None
r = j("POST", "/api/complaints", {"customer_id": ocid, "subject": "Other tenant ticket"}, tok)
ocpid = r.get_json().get("id") if r.status_code == 201 else None
r = j("POST", "/api/users", {"name": "Smoke Tenant Admin", "email": "tadmin@smoke.test",
                             "password": "Demo123!", "role": "admin", "customer_id": cid}, tok)
check("master creates tenant admin", r.status_code == 201)
r = j("POST", "/api/login", {"email": "tadmin@smoke.test", "password": "Demo123!"})
tok2 = r.get_json().get("token") if r.status_code == 200 else None
tme = r.get_json().get("user") if r.status_code == 200 else {}
check("tenant admin login", bool(tok2), f"customer_id={tme.get('customer_id')}")
check("tenant admin sees only own customer_id", tme.get("customer_ids") == [cid],
      tme.get("customer_ids"))

r = j("GET", "/api/customers", None, tok2)
custs = r.get_json() if r.status_code == 200 else []
check("tenant admin /customers = own org only",
      r.status_code == 200 and [x["id"] for x in custs] == [cid], [x.get("id") for x in custs])
check("tenant admin cannot edit another org",
      j("PUT", f"/api/customers/{ocid}", {"name": "Hacked"}, tok2).status_code == 403)
check("tenant admin can edit own org",
      j("PUT", f"/api/customers/{cid}", {"name": "Smoke Labs"}, tok2).status_code == 200)
check("tenant admin cannot create an org",
      j("POST", "/api/customers", {"name": "Rogue Labs"}, tok2).status_code == 403)
check("tenant admin cannot delete an org",
      j("DELETE", f"/api/customers/{cid}", None, tok2).status_code == 403)
# NB: unknown /api paths fall through to the SPA catch-all (HTML, 200), so a
# removed endpoint shows up as HTML rather than JSON.
def endpoint_removed(path, auth):
    r = j("GET", path, None, auth)
    return r.status_code == 404 or "text/html" in (r.content_type or "")

check("care-list self-select endpoint is gone", endpoint_removed("/api/my-customers", tok2))
check("customer-directory endpoint is gone", endpoint_removed("/api/customer-directory", tok2))
check("tenant admin cannot expose care-list state",
      not str(j("GET", "/api/my-customers", None, tok2).get_data(as_text=True)).startswith("["))

r = j("GET", "/api/complaints", None, tok2)
rows = r.get_json() if r.status_code == 200 else []
check("tenant admin sees only own org tickets",
      r.status_code == 200 and rows and all(x["customer_id"] == cid for x in rows),
      sorted({x.get("customer_id") for x in rows}))
check("tenant admin cannot read another org's ticket",
      j("GET", f"/api/complaints/{ocpid}", None, tok2).status_code == 403)
r = j("POST", "/api/complaints", {"customer_id": cid, "subject": "Tenant-owned ticket"}, tok2)
tcpid = r.get_json().get("id") if r.status_code == 201 else None
check("tenant admin creates a ticket in their own org", r.status_code == 201, tcpid)
check("tenant admin cannot move a ticket to another org",
      j("PATCH", f"/api/complaints/{tcpid}", {"customer_id": ocid}, tok2).status_code == 403)
check("tenant admin cannot file a ticket for another org",
      j("POST", "/api/complaints", {"customer_id": ocid, "subject": "Cross-tenant"}, tok2).status_code == 403)
check("tenant admin cannot add equipment to another org",
      j("POST", "/api/equipment", {"customer_id": ocid, "name": "Sneaky pump"}, tok2).status_code == 403)
check("tenant admin cannot add a location to another org",
      j("POST", "/api/locations", {"customer_id": ocid, "name": "Sneaky site"}, tok2).status_code == 403)
check("tenant admin cannot file a breakdown for another org",
      j("POST", "/api/breakdowns", {"customer_id": ocid, "fault_description": "x"}, tok2).status_code == 403)

r = j("GET", "/api/equipment", None, tok2)
eqs = r.get_json() if r.status_code == 200 else []
check("tenant admin sees only own org equipment",
      r.status_code == 200 and all(x["customer_id"] == cid for x in eqs))
r = j("GET", "/api/locations", None, tok2)
locs = r.get_json() if r.status_code == 200 else []
check("tenant admin sees only own org locations",
      r.status_code == 200 and locs and all(x["customer_id"] == cid for x in locs),
      [(x.get("id"), x.get("customer_id")) for x in locs])

r = j("GET", "/api/users", None, tok2)
users = r.get_json() if r.status_code == 200 else []
check("tenant admin team list = own org only",
      r.status_code == 200 and all(x.get("customer_id") in (None, cid) for x in users),
      sorted({x.get("customer_id") for x in users}))
check("tenant admin cannot create a user for another org",
      j("POST", "/api/users", {"name": "Sneaky", "email": "sneaky@smoke.test",
                               "password": "Demo123!", "role": "technician",
                               "customer_id": ocid}, tok2).status_code == 403)
check("tenant admin creates own-org technician",
      j("POST", "/api/users", {"name": "Tenant Tech", "email": "ttech@smoke.test",
                               "password": "Demo123!", "role": "technician",
                               "customer_id": cid}, tok2).status_code == 201)
r = j("GET", "/api/users", None, tok2)
tu = [x for x in (r.get_json() or []) if x["email"] == "ttech@smoke.test"]
check("new technician is scoped to the admin's own org",
      len(tu) == 1 and tu[0]["customer_id"] == cid, tu)
check("tenant admin can edit their own org's technician",
      j("PATCH", f"/api/users/{uid}", {"phone": "011-222 3333"}, tok2).status_code == 200)
check("tenant admin cannot touch an admin account",
      j("PATCH", "/api/users/1", {"name": "Hacked"}, tok2).status_code == 403)
check("tenant admin cannot query another org's admins",
      j("GET", f"/api/tenant-admins?customer_id={ocid}", None, tok2).status_code == 403)
check("tenant admin can query own org's admins",
      j("GET", f"/api/tenant-admins?customer_id={cid}", None, tok2).status_code == 200)
check("tenant admin cannot delete another org's location",
      j("DELETE", f"/api/locations/{olid}", None, tok2).status_code == 403)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL SMOKE TESTS PASSED")
