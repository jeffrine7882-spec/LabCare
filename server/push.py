"""LabCare — Web Push (service-worker push notifications).

Every bell notification also fires a browser push, so a user receives an
OS-level alert (with the browser's notification sound) even when the app,
the tab, or even the browser window is closed — as long as the browser is
running and the user has enabled alerts for this device.

Design:
  * Subscriptions are stored per user + browser/device and carry the
    per-user alert preference at the time they were created.
  * The VAPID private key (Base64 of the PEM) lives in LABCARE_VAPID_PRIVATE;
    the public key is derived from it and served to clients.
  * Sending is best-effort and never raises into the notification path:
    a push that fails (expired subscription, endpoint gone, ...) is dropped
    from the DB so it can't keep failing on every future notification.
"""
import base64
import json
import logging
import os

from database import conn, now

try:
    from pywebpush import webpush, WebPushException
except Exception:  # pragma: no cover - library missing
    webpush = None
    WebPushException = Exception

log = logging.getLogger("labcare.push")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _vapid_private_env() -> str:
    """VAPID private key (Base64 of the PEM).

    Resolution order:
      1. LABCARE_VAPID_PRIVATE env var (production override);
      2. server/vapid.json — the stable keypair committed to the repo so
         redeploys and fresh sandboxes never invalidate browser subscriptions.
    """
    pk = (os.environ.get("LABCARE_VAPID_PRIVATE") or "").strip()
    if not pk:
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(here, "vapid.json"), "r", encoding="utf-8") as fh:
                pk = (json.load(fh).get("private_b64") or "").strip()
        except (OSError, ValueError):
            pk = ""
    return pk


def _vapid_instance():
    """Return a py_vapid.Vapid for the configured private key, or None."""
    pk = _vapid_private_env()
    if not pk:
        return None
    try:
        pem = base64.b64decode(pk).decode("utf-8")
        from py_vapid import Vapid
        return Vapid.from_pem(pem.encode("utf-8"))
    except Exception:
        log.exception("failed to load VAPID key")
        return None


def vapid_public_key() -> str:
    v = _vapid_instance()
    if v is not None:
        from cryptography.hazmat.primitives import serialization
        raw = v.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return _b64url(raw)
    known = os.environ.get("LABCARE_VAPID_PUBLIC", "").strip()
    if not known:
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(here, "vapid.json"), "r", encoding="utf-8") as fh:
                known = (json.load(fh).get("public") or "").strip()
        except (OSError, ValueError):
            known = ""
    if known:
        return known
    # No configured key anywhere: generate a throwaway (dev-only nicety).
    from py_vapid import Vapid
    v2 = Vapid(); v2.generate_keys()
    from cryptography.hazmat.primitives import serialization
    raw = v2.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64url(raw)


def _send_one(sub: dict, payload: dict) -> bool:
    """Send one push. Returns True if the subscription should be kept."""
    if webpush is None:
        return True  # no library — keep the subscription, can't know it's dead
    sub_info = {
        "endpoint": sub["endpoint"],
        "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
    }
    vapid = _vapid_instance()
    if vapid is None:
        return True  # can't sign — keep the subscription rather than drop it
    try:
        webpush(
            subscription_info=sub_info,
            data=json.dumps(payload),
            vapid_private_key=vapid,
            vapid_claims={"sub": "mailto:admin@labcare.com"},
            ttl=86400,
            timeout=10,
        )
        return True
    except WebPushException as exc:
        # 404 / 410: the subscription no longer exists on the push service —
        # drop it. Anything else is transient; keep it for a later retry.
        status = getattr(exc, "response", None)
        code = getattr(status, "status_code", None) if status is not None else None
        if code in (404, 410):
            return False
        log.warning("push failed (%s): %s", code, exc)
        return True
    except Exception as exc:  # network / library hiccup
        log.warning("push error: %s", exc)
        return True


def send_push(user_id, text, entity_type="", entity_id=None):
    """Queue a push to every subscription for user_id (fire-and-forget, but
    synchronous enough to be reliable on a single worker). Never raises.

    Respects the user's per-device alert preference captured at subscribe time:
    a device subscribed with alerts OFF receives no push.
    """
    if not user_id or webpush is None or not _vapid_private_env():
        return
    c = conn()
    try:
        rows = c.execute(
            "SELECT id, endpoint, p256dh, auth, alert_on FROM push_subscriptions "
            "WHERE user_id=? AND active=1",
            (user_id,),
        ).fetchall()
        dead = []
        for r in rows:
            if not r["alert_on"]:
                continue
            payload = {
                "type": entity_type or "notification",
                "entityType": entity_type,
                "entityId": entity_id,
                "title": "LabCare",
                "body": (text or "")[:180],
                "icon": "/icons/icon-192.png",
                "badge": "/icons/icon-192.png",
                "tag": f"labcare-{entity_type or 'msg'}-{entity_id or 0}-{user_id}",
                "data": {"url": "/"},
                "timestamp": now(),
            }
            if not _send_one(r, payload):
                dead.append(r["id"])
        for did in dead:
            c.execute("UPDATE push_subscriptions SET active=0 WHERE id=?", (did,))
        if dead:
            c.commit()
    except Exception:
        log.exception("send_push failed")
    finally:
        c.close()


def save_subscription(user_id, body):
    """Upsert a subscription for (user_id, endpoint) and return its id + alert_on."""
    endpoint = (body.get("endpoint") or "").strip()
    keys = body.get("keys") or {}
    p256dh = (keys.get("p256dh") or "").strip()
    auth = (keys.get("auth") or "").strip()
    alert_on = 1 if body.get("alert_on", True) else 0
    if not endpoint or not p256dh or not auth:
        raise ValueError("incomplete subscription")
    ua = (body.get("user_agent") or "")[:300]
    c = conn()
    c.execute(
        "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, alert_on, user_agent, created_at) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(endpoint) DO UPDATE SET "
        "  user_id=excluded.user_id, p256dh=excluded.p256dh, auth=excluded.auth, "
        "  alert_on=excluded.alert_on, user_agent=excluded.user_agent, active=1, "
        "  created_at=excluded.created_at",
        (user_id, endpoint, p256dh, auth, alert_on, ua, now()),
    )
    c.commit()
    row = c.execute(
        "SELECT id, alert_on FROM push_subscriptions WHERE endpoint=?",
        (endpoint,),
    ).fetchone()
    c.close()
    return {"id": row["id"], "alert_on": row["alert_on"]}


def remove_subscription(user_id, endpoint):
    c = conn()
    c.execute("DELETE FROM push_subscriptions WHERE user_id=? AND endpoint=?",
              (user_id, endpoint))
    c.commit()
    c.close()
