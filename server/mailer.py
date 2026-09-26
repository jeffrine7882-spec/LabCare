"""LabSynch — email notifications.

By default email is disabled (outbox mode): messages are written to
server/outbox.log so you can preview them. Enable SMTP through environment
variables to actually send, e.g. on a deployment server.

  LABCARE_SMTP_HOST, LABCARE_SMTP_PORT, LABCARE_SMTP_USER, LABCARE_SMTP_PASS
  LABCARE_MAIL_FROM  (default: LabSynch <no-reply@labcare.local>)

SMTP sending is best-effort and never raises into the request path.
"""
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTBOX = os.path.join(BASE_DIR, "outbox.log")

_APP_URL = os.environ.get("LABCARE_APP_URL", "https://labcare.example.com")

STATUS_LABELS = {
    "open": "Open", "in_progress": "In Progress", "resolved": "Resolved", "closed": "Closed",
    "reported": "Reported", "diagnosed": "Diagnosed", "on_hold": "On Hold",
}
PRIORITY_LABELS = {
    "low": "Low", "medium": "Medium", "high": "High", "critical": "Critical",
}


def _smtp_enabled():
    return bool(os.environ.get("LABCARE_SMTP_HOST"))


def _render(to_email, recipient_name, subject, lines):
    body = "\n".join(lines)
    html = f"""
    <div style="font-family:Arial,Helvetica,sans-serif;max-width:600px;margin:0 auto;
                border:1px solid #e2e8f0;border-radius:14px;overflow:hidden">
      <div style="background:#0f766e;color:#fff;padding:18px 22px">
        <h2 style="margin:0;font-size:18px">🔬 LabSynch</h2>
        <div style="font-size:12px;opacity:.85">Complaints &amp; Breakdowns</div>
      </div>
      <div style="padding:22px">
        <p style="margin:0 0 6px">Hi {recipient_name},</p>
        {''.join(f'<p style="margin:8px 0;color:#0f172a">{l}</p>' for l in lines)}
        <p style="margin-top:18px;color:#64748b;font-size:12px">
          This is an automated message from LabSynch.
          <a href="{_APP_URL}" style="color:#0f766e">Open LabSynch</a>
        </p>
      </div>
    </div>"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = os.environ.get("LABCARE_MAIL_FROM", "LabSynch <no-reply@labcare.local>")
    msg["To"] = to_email
    msg.attach(MIMEText(body, "plain"))
    msg.attach(MIMEText(html, "html"))
    return msg


def send(to_email, to_name, subject, lines):
    """Queue/send one notification email. Never raises."""
    subject = f"[LabSynch] {subject}"
    try:
        # Always log to the outbox for preview/debug
        with open(OUTBOX, "a") as fh:
            fh.write("=" * 60 + "\n")
            fh.write(f"TO: {to_email} ({to_name})\n")
            fh.write(f"SUBJECT: {subject}\n")
            fh.write("\n".join(lines) + "\n\n")

        if not _smtp_enabled():
            return

        msg = _render(to_email, to_name, subject, lines)
        host = os.environ["LABCARE_SMTP_HOST"]
        port = int(os.environ.get("LABCARE_SMTP_PORT", "587"))
        user = os.environ.get("LABCARE_SMTP_USER")
        pw = os.environ.get("LABCARE_SMTP_PASS")
        with smtplib.SMTP(host, port, timeout=10) as s:
            s.ehlo()
            if port == 587:
                s.starttls()
                s.ehlo()
            if user and pw:
                s.login(user, pw)
            s.send_message(msg)
    except Exception as e:  # pragma: no cover - best effort
        try:
            with open(OUTBOX, "a") as fh:
                fh.write(f"[SMTP ERROR] {e}\n\n")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Notification builders — these also create in-app notifications (see app.py)
# ---------------------------------------------------------------------------
def _to(recip):
    return (recip["email"], recip["name"])


def email_complaint_created(recip, complaint):
    send(
        *_to(recip),
        f"New complaint {complaint['code']} logged",
        [
            f"A new complaint has been logged by {complaint.get('created_by_name') or 'a customer'}.",
            f"<b>{complaint['subject']}</b>",
            f"Organization: {complaint.get('customer_name')}  ·  Equipment: {complaint.get('equipment_name') or 'General'}",
            f"Priority: {PRIORITY_LABELS.get(complaint.get('priority'), 'Medium')}  ·  Status: {STATUS_LABELS.get('open', 'Open')}",
        ],
    )


def email_status_changed(kind, recip, rec, new_status):
    label = STATUS_LABELS.get(new_status, new_status)
    if kind == "complaint":
        send(
            *_to(recip),
            f"Complaint {rec['code']} is now {label}",
            [
                f"A complaint you are following has changed status to <b>{label}</b>.",
                f"<b>{rec['subject']}</b>",
                f"Organization: {rec.get('customer_name')}  ·  Equipment: {rec.get('equipment_name') or 'General'}",
            ],
        )
    else:
        send(
            *_to(recip),
            f"Breakdown {rec['code']} is now {label}",
            [
                f"A breakdown you are following has changed status to <b>{label}</b>.",
                f"<b>{rec.get('equipment_name', '')}</b> — {rec['fault_description'][:120]}",
                f"Organization: {rec.get('customer_name')}",
            ],
        )


def email_assigned(kind, recip, rec, assigner_name):
    if kind == "complaint":
        send(
            *_to(recip),
            f"You were assigned complaint {rec['code']}",
            [
                f"You have been assigned a complaint by {assigner_name or 'your team'}.",
                f"<b>{rec['subject']}</b>",
                f"Organization: {rec.get('customer_name')}  ·  Equipment: {rec.get('equipment_name') or 'General'}",
                f"Priority: {PRIORITY_LABELS.get(rec.get('priority'), 'Medium')}",
            ],
        )
    else:
        send(
            *_to(recip),
            f"You were assigned breakdown {rec['code']}",
            [
                f"You have been assigned a breakdown work order by {assigner_name or 'your team'}.",
                f"<b>{rec.get('equipment_name', '')}</b> — {rec['fault_description'][:120]}",
                f"Organization: {rec.get('customer_name')}",
                f"Priority: {PRIORITY_LABELS.get(rec.get('priority'), 'Medium')}",
            ],
        )


def email_comment(kind, recip, rec, commenter_name, text):
    if kind == "complaint":
        send(
            *_to(recip),
            f"New comment on complaint {rec['code']}",
            [
                f"{commenter_name} commented on complaint <b>{rec['subject']}</b>:",
                f"“{text[:400]}”",
                f"Organization: {rec.get('customer_name')}",
            ],
        )
    else:
        send(
            *_to(recip),
            f"New update on breakdown {rec['code']}",
            [
                f"{commenter_name} commented on breakdown <b>{rec.get('equipment_name', '')}</b>:",
                f"“{text[:400]}”",
                f"Organization: {rec.get('customer_name')}",
            ],
        )
