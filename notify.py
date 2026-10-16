import os
import smtplib
from email.message import EmailMessage

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER or "assets@example.org")


def queue(cursor, *, kind, audience, subject, body,
          person_id=None, email=None, dedupe_key=None):
    """
    Write one notification. Returns True if it was newly created.

    dedupe_key makes the daily check idempotent — running it twice on the
    same day won't send someone two copies of the same reminder.
    """
    if audience == "Admin" and not email:
        email = ADMIN_EMAIL

    cursor.execute(
        """INSERT IGNORE INTO notifications
           (dedupe_key, kind, audience, person_id, email, subject, body)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (dedupe_key, kind, audience, person_id, email, subject, body),
    )
    return cursor.rowcount > 0


def send_queued(connection, limit=50):
    """
    Attempt delivery of queued notifications.
    Returns (sent, skipped, failed).
    """
    with connection.cursor() as cur:
        cur.execute(
            """SELECT id, email, subject, body FROM notifications
               WHERE status = 'Queued' ORDER BY created_at LIMIT %s""",
            (limit,),
        )
        pending = cur.fetchall()

    if not pending:
        return 0, 0, 0

    # No mail server, or no address to send to: mark and move on, don't pretend.
    if not SMTP_HOST:
        with connection.cursor() as cur:
            cur.executemany(
                """UPDATE notifications SET status = 'Skipped',
                   error = 'No SMTP_HOST configured'
                   WHERE id = %s AND status = 'Queued'""",
                [(n["id"],) for n in pending],
            )
        connection.commit()
        return 0, len(pending), 0

    sent = skipped = failed = 0
    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
        server.starttls()
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASSWORD)
    except Exception as exc:
        # The server being unreachable says nothing about the messages, so
        # leave them queued for the next attempt and just record why.
        with connection.cursor() as cur:
            cur.executemany(
                "UPDATE notifications SET error=%s WHERE id=%s AND status='Queued'",
                [(f"Connection failed: {exc}"[:255], n["id"]) for n in pending],
            )
        connection.commit()
        return 0, 0, len(pending)

    with server:
        for note in pending:
            if not note["email"]:
                with connection.cursor() as cur:
                    cur.execute(
                        """UPDATE notifications SET status='Skipped',
                           error='No email address on file' WHERE id=%s""",
                        (note["id"],),
                    )
                skipped += 1
                continue
            try:
                msg = EmailMessage()
                msg["Subject"] = note["subject"]
                msg["From"] = SMTP_FROM
                msg["To"] = note["email"]
                msg.set_content(note["body"])
                server.send_message(msg)
                with connection.cursor() as cur:
                    cur.execute(
                        """UPDATE notifications SET status='Sent', sent_at=NOW()
                           WHERE id=%s""",
                        (note["id"],),
                    )
                sent += 1
            except Exception as exc:
                with connection.cursor() as cur:
                    cur.execute(
                        "UPDATE notifications SET status='Failed', error=%s WHERE id=%s",
                        (str(exc)[:255], note["id"]),
                    )
                failed += 1

    connection.commit()
    return sent, skipped, failed
