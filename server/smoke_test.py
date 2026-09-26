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

# 7b. breakdown tickets no longer take attachments — they get a Service Report
# PDF instead (see the breakdown pdf check below). Rejected at the API, not just
# hidden in the UI.
r = j("POST", "/api/attachments", None, tok,
      raw={"entity_type": "breakdown", "entity_id": str(bid), "file": (io.BytesIO(png), "shot.png")})
check("breakdown attachment rejected (function removed)", r.status_code == 400,
      (r.get_json() or {}).get("error"))

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

# 9. users + engineer + PM
r = j("POST", "/api/users", {"name": "Tech One", "email": "tech1@smoke.test", "password": "Demo123!",
                              "role": "engineer", "customer_id": cid}, tok)
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
r = c.get(f"/api/breakdowns/{bid}/report.pdf", headers={"Authorization": "Bearer " + tok})
check("breakdown pdf (service report, replaces attachments)",
      r.status_code == 200 and r.data[:4] == b"%PDF", len(r.data))
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
r = j("DELETE", f"/api/complaints/{cpid}", None, tok)
check("delete linked complaint (breakdown kept, link nulled)", r.status_code == 200)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL SMOKE TESTS PASSED")
