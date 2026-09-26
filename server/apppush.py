"""LabSynch — native app push (Firebase Cloud Messaging v1).

Sends to the LabSynch mobile app so a phone rings even when the browser is
closed or the phone is locked. FCM v1 carries both Android and iOS tokens,
so one channel covers every device.

Credentials come from env vars only (never committed):
  * LABCARE_FCM_SERVICE_JSON  — full Google service-account JSON, single line
    (base64 of the JSON is also accepted); OR LABCARE_FCM_KEY_B64.
  * LABCARE_FCM_PROJECT_ID    — the Firebase project id (required).

When credentials are missing, send_app_push() logs a one-line warning once per
process and returns gracefully, so the rest of the notification pipeline
(in-app + email + web push) is unaffected.

The OAuth access token is cached and refreshed shortly before its expiry.
"""
import base64
import json
import logging
import os
import threading
import time

import urllib.parse
import urllib.error
import urllib.request

from database import conn, now

log = logging.getLogger("labcare.apppush")

_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_TOKEN_URL = "https://oauth2.googleapis.com/token"

# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------
def _service_account():
    """Return the service-account dict, or None if unconfigured."""
    project_id = (os.environ.get("LABCARE_FCM_PROJECT_ID") or "").strip()
    raw = None
    data = None
    for key in ("LABCARE_FCM_SERVICE_JSON", "LABCARE_FCM_KEY_B64"):
        val = (os.environ.get(key) or "").strip()
        if not val:
            continue
        try:
            data = json.loads(val)
            raw = val
            break
        except ValueError:
            # not JSON — maybe base64 of the JSON
            try:
                data = json.loads(base64.b64decode(val).decode("utf-8"))
                raw = val
                break
            except Exception:
                continue
    if not raw or not isinstance(data, dict):
        return None
    if not project_id:
        # derive from the key file if the explicit env var is absent
        project_id = data.get("project_id") or ""
    if not project_id:
        return None
    data["_project_id"] = project_id
    return data


# --------------------------------------------------------------------------
# OAuth access token (cached)
# --------------------------------------------------------------------------
_token_cache = {"token": None, "expires": 0}
_token_lock = threading.Lock()


def _make_jwt(sa):
    import time as _t
    header = {"alg": "RS256", "typ": "JWT"}
    now_i = int(_t.time())
    claims = {
        "iss": sa["client_email"],
        "scope": _SCOPE,
        "aud": _TOKEN_URL,
        "iat": now_i,
        "exp": now_i + 3600,
    }
    def b64url(b):
        return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")
    seg = lambda o: b64url(json.dumps(o, separators=(",", ":")).encode("utf-8"))
    signing_input = seg(header) + "." + seg(claims)
    try:
        from cryptography.hazmat.primitives import serialization, hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        key = serialization.load_pem_private_key(sa["private_key"].encode("utf-8"), password=None)
        sig = key.sign(signing_input.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    except Exception:
        log.exception("could not sign JWT for Firebase service account")
        raise
    return signing_input + "." + b64url(sig)


def _access_token(sa):
    with _token_lock:
        if _token_cache["token"] and _token_cache["expires"] > time.time() + 120:
            return _token_cache["token"]
        assertion = _make_jwt(sa)
        body = urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        }).encode("utf-8")
        req = urllib.request.Request(
            _TOKEN_URL, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        access = data["access_token"]
        _token_cache["token"] = access
        _token_cache["expires"] = time.time() + int(data.get("expires_in", 3600))
        return access


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------
_warned = False


def send_app_push(user_id, text, entity_type="", entity_id=None):
    """Firebase-push one message to every active device for user_id.

    Best-effort; never raises. A device whose token is rejected by FCM
    (UNREGISTERED) is deactivated so it can't keep failing.
    """
    sa = _service_account()
    if not sa:
        global _warned
        if not _warned:
            _warned = True
            log.warning(
                "Firebase FCM not configured (LABCARE_FCM_SERVICE_JSON + "
                "LABCARE_FCM_PROJECT_ID) — native app pushes skipped. "
                "Web push is unaffected.")
        return
    tokens = _active_tokens(user_id)
    if not tokens:
        return
    message = {
        "message": {
            "notification": {
                "title": "LabSynch",
                "body": (text or "")[:180],
            },
            "android": {"priority": "high"},
            "apns": {
                "headers": {"apns-priority": "10"},
                "payload": {"aps": {"sound": "default", "badge": 1}},
            },
            "data": {
                "entity_type": entity_type or "",
                "entity_id": str(entity_id or ""),
                "url": "/",
            },
        }
    }
    try:
        access = _access_token(sa)
    except Exception:
        log.exception("could not obtain Firebase access token")
        return
    url = ("https://fcm.googleapis.com/v1/projects/%s/messages:send"
           % sa["_project_id"])
    dead = []
    for tok in tokens:
        msg = json.loads(json.dumps(message))
        msg["message"]["token"] = tok
        req = urllib.request.Request(
            url,
            data=json.dumps(msg).encode("utf-8"),
            headers={"Authorization": "Bearer " + access,
                     "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            log.warning("FCM send %s: %s %s", tok[:12] + "…", e.code, body[:160])
            if "UNREGISTERED" in body or e.code == 404:
                dead.append(tok)
            elif e.code in (401, 403):
                # auth problem — stop, don't mark devices dead
                return
        except Exception as e:
            log.warning("FCM send error: %s", e)
    if dead:
        _deactivate(user_id, dead)


def _active_tokens(user_id):
    c = conn()
    try:
        rows = c.execute(
            "SELECT push_token FROM app_devices WHERE user_id=? AND active=1 AND alert_on=1",
            (user_id,)).fetchall()
        return [r["push_token"] for r in rows]
    finally:
        c.close()


def _deactivate(user_id, tokens):
    if not tokens:
        return
    c = conn()
    try:
        marks = ", ".join("?" for _ in tokens)
        c.execute(
            f"UPDATE app_devices SET active=0 WHERE user_id=? AND push_token IN ({marks})",
            (user_id, *tokens))
        c.commit()
    except Exception:
        log.exception("failed to deactivate dead FCM tokens")
    finally:
        c.close()


def register_device(user_id, body):
    """Upsert a device registration for user_id and return its id."""
    platform = (body.get("platform") or "").lower() in ("ios", "android") \
        and (body.get("platform") or "").lower() or "unknown"
    token = (body.get("push_token") or "").strip()
    alert_on = 1 if body.get("alert_on", True) else 0
    name = (body.get("device_name") or "")[:80]
    if not token or platform == "unknown":
        raise ValueError("push_token and platform (android|ios) are required")
    if len(token) > 4096:
        raise ValueError("push_token too long")
    c = conn()
    c.execute(
        "INSERT INTO app_devices (user_id, platform, push_token, active, alert_on, device_name, created_at) "
        "VALUES (?,?,?,1,?,?,?) "
        "ON CONFLICT(push_token) DO UPDATE SET user_id=excluded.user_id, "
        "  platform=excluded.platform, active=1, alert_on=excluded.alert_on, "
        "  device_name=excluded.device_name, created_at=excluded.created_at",
        (user_id, platform, token, alert_on, name, now()))
    c.commit()
    row = c.execute("SELECT id FROM app_devices WHERE push_token=?", (token,)).fetchone()
    c.close()
    return {"id": row["id"]}


def remove_device(user_id, token):
    if not token:
        return
    c = conn()
    c.execute("DELETE FROM app_devices WHERE user_id=? AND push_token=?",
              (user_id, token))
    c.commit()
    c.close()
