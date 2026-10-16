import csv
import hmac
import io
import os
import secrets
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import urlsplit

import pymysql
import qrcode
from flask import (
    Flask, abort, flash, g, redirect, render_template, request,
    send_file, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

import notify
import reports

app = Flask(__name__)
# No built-in fallback: a key that ships in the source lets anyone forge an
# admin session. Without SECRET_KEY a random one is made per process, which is
# safe but signs everyone out on every restart.
SECRET_KEY_FROM_ENV = bool(os.environ.get("SECRET_KEY"))
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
# Lax keeps the session cookie off cross-site POSTs; the CSRF token below
# covers the rest.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
# Only sent over HTTPS. Opt-in, because local development runs on plain HTTP.
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE") == "1"

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "database": os.environ.get("DB_NAME", "assettrack"),
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
    "autocommit": False,
}

# Tag prefixes keep printed labels readable at a glance.
ASSET_TYPES = {
    "Laptop": "LAP",
    "Desktop": "DSK",
    "Monitor": "MON",
    "Phone": "PHN",
    "Tablet": "TAB",
    "Printer": "PRN",
    "Network device": "NET",
    "Software licence": "LIC",
    "Other": "OTH",
}
CONDITIONS = ["New", "Good", "Fair", "Poor", "Faulty"]
# Fit to hand to a new starter. Anything worse waits for repair or replacement.
ISSUABLE_CONDITIONS = ("New", "Good")
STATUSES = ["In Use", "In Storage", "Under Repair", "Retired"]
EMPLOYMENT_STATES = ["Active", "Leaving", "Left"]
DISPOSAL_METHODS = ["Sold", "Recycled", "Donated", "Destroyed",
                    "Returned to supplier", "Lost", "Stolen"]

# Lifecycle. An asset only moves along these edges, and never straight into
# "In Use" — that happens by allocating it to someone, which is a different
# action with a different record.
TRANSITIONS = {
    "In Storage":   ["Under Repair", "Retired"],
    "In Use":       ["In Storage"],
    "Under Repair": ["In Storage", "Retired"],
    "Retired":      ["In Storage"],
}
REQUEST_TYPES = ["Additional device", "Replacement", "Keep longer"]

# How far ahead to warn. Tunable in one place so the report can justify them.
DUE_SOON_DAYS = 7          # loan deadline approaching
DUE_REMINDER_MARKS = (7, 3, 1, 0)   # days-before-due that trigger an email
WARRANTY_SOON_DAYS = 30


# ------------------------------------------------------------------
# Database helpers
# ------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = pymysql.connect(**DB_CONFIG)
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, args=(), one=False):
    with get_db().cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def lock_asset(cur, asset_id):
    """
    Lock one asset row until commit. Anything that hands an asset to someone
    takes this first, so two people acting at once queue up instead of both
    seeing "nobody holds it" and creating two loans.
    """
    cur.execute("""SELECT id, asset_tag, status, item_condition FROM assets
                   WHERE id = %s FOR UPDATE""",
                (asset_id,))
    return cur.fetchone()


def open_loan_of(cur, asset_id):
    """
    The asset's open loan, read with a lock. A locking read sees the latest
    committed rows, where a plain SELECT would see this transaction's older
    snapshot and miss a loan someone else has just created.
    """
    cur.execute("""SELECT id, person_id, due_date FROM assignments
                   WHERE asset_id = %s AND returned_date IS NULL FOR UPDATE""", (asset_id,))
    return cur.fetchone()


@app.errorhandler(pymysql.err.IntegrityError)
def integrity_clash(exc):
    """
    The database refused a write that the checks above should have stopped —
    almost always someone else's change landing first (the one-open-loan
    rule, a duplicate serial). Undo and say so rather than show a crash page.
    """
    db = g.get("db")
    if db is not None:
        db.rollback()
    app.logger.warning("Integrity error on %s: %s", request.path, exc)
    flash("That clashed with a change someone else just made, so nothing was saved. "
          "Check the record and try again.", "ok")
    return redirect(safe_next(request.referrer and urlsplit(request.referrer).path)
                    or url_for("dashboard"))


# ------------------------------------------------------------------
# Audit trail
# ------------------------------------------------------------------
def log_event(cursor, asset_id, event_type, *, asset_tag=None, from_value=None,
              to_value=None, note=None, person_id=None):
    """
    Record one thing that happened to one asset.

    asset_tag is stored as text alongside the foreign key so the trail still
    reads correctly after an asset row is deleted.
    """
    if asset_tag is None and asset_id:
        cursor.execute("SELECT asset_tag FROM assets WHERE id = %s", (asset_id,))
        row = cursor.fetchone()
        asset_tag = row["asset_tag"] if row else None
    cursor.execute(
        """INSERT INTO asset_events
           (asset_id, asset_tag, event_type, from_value, to_value, note, person_id, user_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (asset_id, asset_tag, event_type,
         str(from_value)[:120] if from_value is not None else None,
         str(to_value)[:120] if to_value is not None else None,
         note[:500] if note else None, person_id, session.get("user_id")),
    )


# Fields worth recording a before/after for when an asset is edited.
TRACKED_FIELDS = {
    "asset_type": "Type", "make": "Make", "model": "Model",
    "serial_number": "Serial number", "location": "Location",
    "purchase_date": "Purchase date", "purchase_cost": "Purchase cost",
    "supplier": "Supplier", "invoice_ref": "Invoice reference",
    "warranty_expiry": "Warranty expiry", "warranty_provider": "Warranty provider",
    "warranty_ref": "Warranty reference", "item_condition": "Condition",
}


def diff_fields(before, after):
    """Yield (label, old, new) for each tracked field that actually changed."""
    for key, label in TRACKED_FIELDS.items():
        old, new = before.get(key), after.get(key)
        old_s = "" if old in (None, "") else str(old)
        new_s = "" if new in (None, "") else str(new)
        if old_s != new_s:
            yield label, old_s or "(blank)", new_s or "(blank)"


# ------------------------------------------------------------------
# CSRF — every POST form carries the session's token
# ------------------------------------------------------------------
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def check_csrf():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.form.get("_csrf", "")
        expected = session.get("_csrf", "")
        if not expected or not hmac.compare_digest(sent, expected):
            abort(400, "The form expired or came from somewhere else. Go back, reload and try again.")


def safe_next(target):
    """Only follow same-site paths: '/assets' yes, '//evil.com' or 'http://…' no."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


# ------------------------------------------------------------------
# Throttling — slows password guessing and form spam. Kept in memory, so
# it is per process and resets on restart; enough for a single-server app.
# ------------------------------------------------------------------
_attempts = {}


def throttled(bucket, limit, window_seconds):
    """True if this client has already used up `limit` attempts in the window."""
    key = (bucket, request.remote_addr)
    cutoff = datetime.now() - timedelta(seconds=window_seconds)
    recent = [t for t in _attempts.get(key, []) if t > cutoff]
    _attempts[key] = recent
    return len(recent) >= limit


def record_attempt(bucket):
    _attempts.setdefault((bucket, request.remote_addr), []).append(datetime.now())


LOGIN_LIMIT = (5, 15 * 60)        # failed sign-ins per 15 minutes
REQUEST_FORM_LIMIT = (10, 60 * 60)  # public applications per hour


# ------------------------------------------------------------------
# Auth
# ------------------------------------------------------------------
def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapped


@app.context_processor
def inject_globals():
    pending = 0
    if "user_id" in session:
        # Header badge only; never let it take a page down.
        try:
            pending = query("SELECT COUNT(*) AS n FROM requests WHERE status = 'Pending'",
                            one=True)["n"]
        except Exception:
            pending = 0
    return {"current_user": session.get("full_name"), "today": date.today(),
            "pending_requests": pending}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if throttled("login", *LOGIN_LIMIT):
            return render_template(
                "login.html",
                error="Too many failed attempts. Wait 15 minutes and try again.",
                username=username,
            ), 429
        user = query("SELECT * FROM users WHERE username = %s", (username,), one=True)

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["full_name"] = user["full_name"]
            return redirect(safe_next(request.args.get("next")) or url_for("dashboard"))

        record_attempt("login")
        # Deliberately vague: don't reveal whether the username exists.
        return render_template(
            "login.html",
            error="That username and password don't match a record.",
            username=username,
        ), 401

    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------------
# Shared state helpers used by templates
# ------------------------------------------------------------------
def warranty_state(expiry):
    """(label, css class) for a warranty date."""
    if not expiry:
        return "Not recorded", "flag-none"
    days = (expiry - date.today()).days
    if days < 0:
        return f"Expired {abs(days)}d ago", "flag-alert"
    if days <= WARRANTY_SOON_DAYS:
        return f"{days}d left", "flag-alert"
    if days <= 90:
        return f"{days}d left", "flag-warn"
    return expiry.strftime("%d %b %Y"), "flag-ok"


def due_state(due, returned=None):
    """(label, css class) for a loan deadline."""
    if returned:
        return "Returned " + returned.strftime("%d %b %Y"), "flag-none"
    if not due:
        return "Open-ended", "flag-none"
    days = (due - date.today()).days
    if days < 0:
        return f"Overdue by {abs(days)}d", "flag-alert"
    if days == 0:
        return "Due today", "flag-alert"
    if days <= DUE_SOON_DAYS:
        return f"Due in {days}d", "flag-warn"
    return "Due " + due.strftime("%d %b %Y"), "flag-ok"


app.jinja_env.globals["warranty_state"] = warranty_state
app.jinja_env.globals["due_state"] = due_state


# ------------------------------------------------------------------
# Alerts — one query set, used by both the dashboard and the daily job
# ------------------------------------------------------------------
OPEN_LOAN_SELECT = """
    SELECT s.id AS assignment_id, s.assigned_date, s.due_date, s.times_extended,
           a.id AS asset_id, a.asset_tag, a.asset_type, a.make, a.model,
           p.id AS person_id, p.employee_id, p.name, p.email, p.department,
           p.employment_status, p.exit_date
    FROM assignments s
    JOIN assets a ON a.id = s.asset_id
    JOIN people p ON p.id = s.person_id
    WHERE s.returned_date IS NULL
"""


def collect_alerts(runner=query):
    """Everything currently needing attention, grouped by kind."""
    today = date.today()
    soon = today + timedelta(days=DUE_SOON_DAYS)

    overdue = runner(OPEN_LOAN_SELECT + """
        AND s.due_date IS NOT NULL AND s.due_date < %s
        ORDER BY s.due_date""", (today,))

    due_soon = runner(OPEN_LOAN_SELECT + """
        AND s.due_date IS NOT NULL AND s.due_date BETWEEN %s AND %s
        ORDER BY s.due_date""", (today, soon))

    leaving = runner(OPEN_LOAN_SELECT + """
        AND p.employment_status IN ('Leaving','Left')
        ORDER BY p.exit_date IS NULL, p.exit_date, p.name""")

    warranty = runner("""
        SELECT * FROM assets
        WHERE status <> 'Retired' AND warranty_expiry IS NOT NULL
          AND warranty_expiry <= %s
        ORDER BY warranty_expiry""", (today + timedelta(days=WARRANTY_SOON_DAYS),))

    pending = runner("""
        SELECT r.*, p.name, p.employee_id, p.department,
               a.asset_tag AS current_tag, a.model AS current_model
        FROM requests r
        JOIN people p ON p.id = r.person_id
        LEFT JOIN assets a ON a.id = r.current_asset_id
        WHERE r.status IN ('Pending','Approved')
        ORDER BY r.status DESC, r.created_at""")

    return {
        "overdue": overdue,
        "due_soon": due_soon,
        "leaving": leaving,
        "warranty": warranty,
        "requests": pending,
    }


@app.route("/")
@login_required
def dashboard():
    alerts = collect_alerts()
    counts = query("""
        SELECT
          (SELECT COUNT(*) FROM assets)                          AS assets,
          (SELECT COUNT(*) FROM assets WHERE status = 'In Use')  AS in_use,
          (SELECT COUNT(*) FROM people WHERE employment_status = 'Active') AS staff,
          (SELECT COUNT(*) FROM requests WHERE status = 'Pending') AS pending
    """, one=True)

    # Group leavers so the admin sees one row per person, not per item.
    by_person = {}
    for row in alerts["leaving"]:
        by_person.setdefault(row["person_id"], {"person": row, "items": []})["items"].append(row)

    return render_template("dashboard.html", alerts=alerts, counts=counts,
                           leavers=list(by_person.values()))


# ------------------------------------------------------------------
# Asset register
# ------------------------------------------------------------------
@app.route("/assets")
@login_required
def asset_list():
    status = request.args.get("status", "")
    atype = request.args.get("type", "")
    search = request.args.get("q", "").strip()

    sql = """SELECT a.*, p.name AS holder, p.employee_id, s.due_date, s.id AS assignment_id
             FROM assets a
             LEFT JOIN assignments s
               ON s.asset_id = a.id AND s.returned_date IS NULL
             LEFT JOIN people p ON p.id = s.person_id
             WHERE 1 = 1"""
    args = []
    if status:
        sql += " AND a.status = %s"
        args.append(status)
    if atype:
        sql += " AND a.asset_type = %s"
        args.append(atype)
    if search:
        sql += """ AND (a.asset_tag LIKE %s OR a.serial_number LIKE %s
                        OR a.model LIKE %s OR p.employee_id LIKE %s OR p.name LIKE %s)"""
        args += [f"%{search}%"] * 5
    sql += " ORDER BY a.created_at DESC"

    assets = query(sql, args)
    return render_template("asset_list.html", assets=assets, types=ASSET_TYPES,
                           statuses=STATUSES, f_status=status, f_type=atype, f_q=search)


def form_values():
    f = request.form
    return {
        "asset_type": f.get("asset_type", ""),
        "make": f.get("make", "").strip(),
        "model": f.get("model", "").strip(),
        "serial_number": f.get("serial_number", "").strip() or None,
        "location": f.get("location", "").strip(),
        "purchase_date": f.get("purchase_date") or None,
        "purchase_cost": f.get("purchase_cost") or None,
        "supplier": f.get("supplier", "").strip(),
        "invoice_ref": f.get("invoice_ref", "").strip(),
        "warranty_expiry": f.get("warranty_expiry") or None,
        "warranty_provider": f.get("warranty_provider", "").strip(),
        "warranty_ref": f.get("warranty_ref", "").strip(),
        "item_condition": f.get("item_condition", "Good"),
        "status": f.get("status", "In Storage"),
        "notes": f.get("notes", "").strip(),
        "person_id": f.get("person_id") or None,
        "due_date": f.get("due_date") or None,
    }


def parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# Column widths from schema.sql, so an over-long entry is a form error
# rather than a database exception.
ASSET_LIMITS = {"make": 60, "model": 100, "serial_number": 100, "location": 100,
                "supplier": 100, "invoice_ref": 60, "warranty_provider": 100,
                "warranty_ref": 60}
PERSON_LIMITS = {"employee_id": 20, "name": 100, "department": 100, "job_title": 100,
                 "email": 150, "phone": 40}
MAX_MONEY = 99_999_999.99   # DECIMAL(10,2)


def check_lengths(v, limits, errors):
    for field, limit in limits.items():
        if v.get(field) and len(v[field]) > limit and field not in errors:
            errors[field] = f"Keep this under {limit} characters."


def parse_money(value):
    """A finite, non-negative amount that fits DECIMAL(10,2), or raise ValueError."""
    amount = float(value)
    if amount != amount or amount in (float("inf"), float("-inf")):
        raise ValueError("not a number")
    return amount


def validate(v, current=None, asset_status=None):
    """
    current: the open loan when editing ({"person_id", "due_date"}), so an
    unchanged holder or deadline isn't re-judged by today's rules.
    asset_status: the asset's lifecycle status when editing.
    """
    errors = {}
    if v["asset_type"] not in ASSET_TYPES:
        errors["asset_type"] = "Pick an asset type."
    if not v["model"]:
        errors["model"] = "Model is required — it's what identifies the item on a shelf."
    if v["item_condition"] not in CONDITIONS:
        errors["item_condition"] = "Pick a condition."

    for field in ("purchase_date", "warranty_expiry", "due_date"):
        if v[field] and not parse_date(v[field]):
            errors[field] = "Use the date picker (YYYY-MM-DD)."

    if not errors.get("purchase_date") and not errors.get("warranty_expiry"):
        if v["purchase_date"] and v["warranty_expiry"] and v["warranty_expiry"] < v["purchase_date"]:
            errors["warranty_expiry"] = "Warranty can't expire before the purchase date."

    if v["purchase_cost"]:
        try:
            cost = parse_money(v["purchase_cost"])
            if cost < 0:
                errors["purchase_cost"] = "Cost can't be negative."
            elif cost > MAX_MONEY:
                errors["purchase_cost"] = "That's larger than the register can store."
        except ValueError:
            errors["purchase_cost"] = "Enter a number, e.g. 8500.00"

    check_lengths(v, ASSET_LIMITS, errors)

    # Holder: must be a real, active person. Someone who already holds it and
    # has since become a leaver can stay on it until it's returned.
    current_holder = current["person_id"] if current else None
    if v["person_id"]:
        try:
            pid = int(v["person_id"])
        except ValueError:
            pid = None
        person = query("SELECT id, employment_status FROM people WHERE id = %s",
                       (pid,), one=True) if pid else None
        if not person:
            errors["person_id"] = "Pick someone from the list."
        elif pid != current_holder:
            if person["employment_status"] != "Active":
                errors["person_id"] = "Only active staff can be given equipment."
            elif asset_status in ("Retired", "Under Repair"):
                errors["person_id"] = (f"It's {asset_status.lower()}. Move it back to "
                                       f"storage from the asset record before lending it.")

    if v["due_date"] and not v["person_id"]:
        errors["due_date"] = "A return date needs someone to return it."
    if v["due_date"] and not errors.get("due_date"):
        d = parse_date(v["due_date"])
        if d and d < date.today():
            # An overdue loan being left as it is shouldn't block other edits.
            same_loan = (current and not errors.get("person_id")
                         and int(v["person_id"]) == current_holder
                         and current["due_date"] == d)
            if not same_loan:
                errors["due_date"] = "That date has already passed."
    return errors


def make_tag(cursor, asset_type, asset_id):
    """LAP-00014 — prefix by type, padded row id. Unique and readable."""
    tag = f"{ASSET_TYPES[asset_type]}-{asset_id:05d}"
    cursor.execute("UPDATE assets SET asset_tag = %s WHERE id = %s", (tag, asset_id))
    return tag


@app.route("/assets/new", methods=["GET", "POST"])
@login_required
def asset_new():
    people = query("""SELECT * FROM people WHERE employment_status = 'Active'
                      ORDER BY name""")

    if request.method == "POST":
        v = form_values()
        # A new asset is in use exactly when it's given to someone.
        v["status"] = "In Use" if v["person_id"] else "In Storage"
        errors = validate(v)

        if not errors and v["serial_number"]:
            dup = query("SELECT asset_tag FROM assets WHERE serial_number = %s",
                        (v["serial_number"],), one=True)
            if dup:
                errors["serial_number"] = f"Already registered as {dup['asset_tag']}."

        if errors:
            return render_template("asset_form.html", v=v, errors=errors, people=people,
                                   types=ASSET_TYPES, conditions=CONDITIONS,
                                   statuses=STATUSES, mode="new"), 400

        db = get_db()
        try:
            with db.cursor() as cur:
                cur.execute(
                    """INSERT INTO assets
                       (asset_type, make, model, serial_number, location, purchase_date,
                        purchase_cost, supplier, invoice_ref, warranty_expiry,
                        warranty_provider, warranty_ref, item_condition, status, notes)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (v["asset_type"], v["make"], v["model"], v["serial_number"],
                     v["location"], v["purchase_date"], v["purchase_cost"],
                     v["supplier"], v["invoice_ref"], v["warranty_expiry"],
                     v["warranty_provider"], v["warranty_ref"],
                     v["item_condition"], v["status"], v["notes"]),
                )
                asset_id = cur.lastrowid
                tag = make_tag(cur, v["asset_type"], asset_id)
                log_event(cur, asset_id, "Registered", asset_tag=tag,
                          to_value=v["status"],
                          note=f"{v['asset_type']} — {(v['make'] + ' ') if v['make'] else ''}{v['model']}")

                if v["person_id"]:
                    cur.execute(
                        """INSERT INTO assignments
                           (asset_id, person_id, assigned_date, due_date)
                           VALUES (%s,%s,%s,%s)""",
                        (asset_id, v["person_id"], date.today(), v["due_date"]),
                    )
                    log_event(cur, asset_id, "Allocated", asset_tag=tag,
                              to_value="On loan", person_id=v["person_id"],
                              note=("Due " + v["due_date"]) if v["due_date"] else "Open-ended")
            db.commit()
        except Exception:
            db.rollback()
            raise

        holder = next((p["name"] for p in people if str(p["id"]) == str(v["person_id"])), None)
        flash(f"Registered {tag}" + (f" and issued it to {holder}" if holder else "")
              + ". Print the label and stick it on the item.", "ok")
        return redirect(url_for("asset_detail", asset_id=asset_id))

    blank = {k: "" for k in (
        "asset_type", "make", "model", "serial_number", "location", "purchase_date",
        "purchase_cost", "warranty_expiry", "notes", "person_id", "due_date")}
    blank.update(item_condition="Good", status="In Storage")
    # Arriving from a staff record with no suitable stock: a new purchase for
    # that person, so it's new and goes straight to them.
    if request.args.get("type") in ASSET_TYPES:
        blank["asset_type"] = request.args["type"]
    if any(str(p["id"]) == request.args.get("person_id") for p in people):
        blank.update(person_id=request.args["person_id"], item_condition="New",
                     status="In Use")
    return render_template("asset_form.html", v=blank, errors={}, people=people,
                           types=ASSET_TYPES, conditions=CONDITIONS,
                           statuses=STATUSES, mode="new")


@app.route("/assets/<int:asset_id>/edit", methods=["GET", "POST"])
@login_required
def asset_edit(asset_id):
    asset = query("SELECT * FROM assets WHERE id = %s", (asset_id,), one=True)
    if not asset:
        return render_template("not_found.html", tag=None), 404
    current = query("""SELECT id, person_id, due_date FROM assignments
                       WHERE asset_id = %s AND returned_date IS NULL""",
                    (asset_id,), one=True)
    # Keep the current holder selectable even if they've since become a
    # leaver, or saving the form would silently hand the item back.
    people = query("""SELECT * FROM people
                      WHERE employment_status = 'Active' OR id = %s
                      ORDER BY name""", (current["person_id"] if current else None,))

    if request.method == "POST":
        v = form_values()
        v["status"] = asset["status"]   # moved only from the asset record
        errors = validate(v, current=current, asset_status=asset["status"])
        if not errors and v["serial_number"]:
            dup = query("""SELECT asset_tag FROM assets
                           WHERE serial_number = %s AND id <> %s""",
                        (v["serial_number"], asset_id), one=True)
            if dup:
                errors["serial_number"] = f"Already registered as {dup['asset_tag']}."
        if errors:
            return render_template("asset_form.html", v=v, errors=errors, people=people,
                                   types=ASSET_TYPES, conditions=CONDITIONS,
                                   statuses=STATUSES, mode="edit", asset=asset), 400

        db = get_db()
        try:
            with db.cursor() as cur:
                # The form was judged against the loan as it stood when the page
                # loaded. If someone returned or issued it since, stop rather
                # than act on stale information.
                lock_asset(cur, asset_id)
                now_open = open_loan_of(cur, asset_id)
                if (now_open["id"] if now_open else None) != (current["id"] if current else None):
                    db.rollback()
                    flash("Someone changed who holds this item while you were editing. "
                          "Nothing was saved — check the record and try again.", "ok")
                    return redirect(url_for("asset_detail", asset_id=asset_id))
                cur.execute(
                    """UPDATE assets SET asset_type=%s, make=%s, model=%s, serial_number=%s,
                       location=%s, purchase_date=%s, purchase_cost=%s, supplier=%s,
                       invoice_ref=%s, warranty_expiry=%s, warranty_provider=%s,
                       warranty_ref=%s, item_condition=%s, notes=%s WHERE id=%s""",
                    (v["asset_type"], v["make"], v["model"], v["serial_number"],
                     v["location"], v["purchase_date"], v["purchase_cost"],
                     v["supplier"], v["invoice_ref"], v["warranty_expiry"],
                     v["warranty_provider"], v["warranty_ref"], v["item_condition"],
                     v["notes"], asset_id),
                )
                for label, old, new in diff_fields(asset, v):
                    log_event(cur, asset_id, "Edited", from_value=old,
                              to_value=new, note=label)
                # The tag is deliberately left alone when the type changes: it is
                # printed on a label and in a QR code stuck to the item, and
                # reissuing it would leave those pointing at nothing.

                held_by = current["person_id"] if current else None
                new_holder = int(v["person_id"]) if v["person_id"] else None
                if new_holder != held_by:
                    if held_by:
                        cur.execute("""UPDATE assignments SET returned_date = %s
                                       WHERE asset_id = %s AND returned_date IS NULL""",
                                    (date.today(), asset_id))
                        log_event(cur, asset_id, "Returned", person_id=held_by,
                                  from_value="On loan", to_value="In Storage",
                                  note="Holder changed on the asset form")
                    if new_holder:
                        cur.execute("""INSERT INTO assignments
                                       (asset_id, person_id, assigned_date, due_date)
                                       VALUES (%s,%s,%s,%s)""",
                                    (asset_id, new_holder, date.today(), v["due_date"]))
                        cur.execute("UPDATE assets SET status = 'In Use' WHERE id = %s",
                                    (asset_id,))
                        log_event(cur, asset_id, "Allocated", person_id=new_holder,
                                  to_value="On loan",
                                  note=("Due " + v["due_date"]) if v["due_date"] else "Open-ended")
                    else:
                        cur.execute("""UPDATE assets SET status = 'In Storage'
                                       WHERE id = %s AND status = 'In Use'""", (asset_id,))
                elif current and v["due_date"] != (
                        current["due_date"].isoformat() if current["due_date"] else None):
                    # Same holder, new deadline — the same as using the extend button.
                    new_due = parse_date(v["due_date"]) if v["due_date"] else None
                    if new_due:
                        extend_loan(cur, current["id"], current["due_date"], new_due,
                                    "Changed on the asset form")
                    else:
                        cur.execute("UPDATE assignments SET due_date = NULL WHERE id = %s",
                                    (current["id"],))
                        clear_due_reminders(cur, current["id"])
                        log_event(cur, asset_id, "Due date removed",
                                  from_value=current["due_date"], to_value="Open-ended",
                                  person_id=current["person_id"],
                                  note="Changed on the asset form")
            db.commit()
        except Exception:
            db.rollback()
            raise

        flash("Changes saved.", "ok")
        return redirect(url_for("asset_detail", asset_id=asset_id))

    v = dict(asset)
    v["purchase_date"] = asset["purchase_date"].isoformat() if asset["purchase_date"] else ""
    v["warranty_expiry"] = asset["warranty_expiry"].isoformat() if asset["warranty_expiry"] else ""
    v["person_id"] = str(current["person_id"]) if current else ""
    v["due_date"] = (current["due_date"].isoformat()
                     if current and current["due_date"] else "")
    return render_template("asset_form.html", v=v, errors={}, people=people,
                           types=ASSET_TYPES, conditions=CONDITIONS,
                           statuses=STATUSES, mode="edit", asset=asset)


@app.route("/assets/<int:asset_id>/delete", methods=["POST"])
@login_required
def asset_delete(asset_id):
    asset = query("SELECT asset_tag, status FROM assets WHERE id = %s", (asset_id,), one=True)
    if not asset:
        return redirect(url_for("asset_list"))
    db = get_db()
    with db.cursor() as cur:
        # Deleting takes its loans with it (the foreign key cascades), which
        # would erase the ownership history. So only something that has never
        # been lent out — typically registered by mistake — can be deleted.
        # Anything else is retired, which keeps the full record.
        lock_asset(cur, asset_id)
        cur.execute("""SELECT COUNT(*) AS loans, SUM(returned_date IS NULL) AS open_loans
                       FROM assignments WHERE asset_id = %s FOR UPDATE""", (asset_id,))
        loans = cur.fetchone()
        if loans["loans"]:
            db.rollback()
            if loans["open_loans"]:
                flash(f"{asset['asset_tag']} is on loan. Mark it returned, then retire it "
                      f"rather than deleting it.", "ok")
            else:
                flash(f"{asset['asset_tag']} has been lent out before, and deleting it would "
                      f"erase that history. Retire it instead.", "ok")
            return redirect(url_for("asset_detail", asset_id=asset_id))
        # Logged first so the audit trail outlives the row. asset_id is set to
        # NULL by the foreign key, but asset_tag stays readable.
        log_event(cur, asset_id, "Deleted", asset_tag=asset["asset_tag"],
                  from_value=asset["status"], to_value="Deleted",
                  note=request.form.get("reason", "").strip() or "No reason given")
        cur.execute("DELETE FROM assets WHERE id = %s", (asset_id,))
    db.commit()
    flash(f"{asset['asset_tag']} deleted. The audit trail keeps the record.", "ok")
    return redirect(url_for("asset_list"))


@app.route("/assets/<int:asset_id>")
@login_required
def asset_detail(asset_id):
    asset = query("SELECT * FROM assets WHERE id = %s", (asset_id,), one=True)
    if not asset:
        return render_template("not_found.html", tag=None), 404
    history = query("""SELECT s.*, p.name, p.department, p.employee_id
                       FROM assignments s JOIN people p ON p.id = s.person_id
                       WHERE s.asset_id = %s
                       ORDER BY s.assigned_date DESC, s.id DESC""", (asset_id,))
    open_loan = next((h for h in history if not h["returned_date"]), None)
    events = query("""SELECT e.*, u.full_name AS by_whom, p.name AS person_name
                      FROM asset_events e
                      LEFT JOIN users u ON u.id = e.user_id
                      LEFT JOIN people p ON p.id = e.person_id
                      WHERE e.asset_id = %s
                      ORDER BY e.created_at DESC, e.id DESC LIMIT 50""", (asset_id,))
    return render_template("asset_detail.html", a=asset, history=history, open_loan=open_loan,
                           events=events, next_states=allowed_transitions(asset, open_loan),
                           methods=DISPOSAL_METHODS,
                           tag_url=url_for("tag_lookup", tag=asset["asset_tag"], _external=True))


def allowed_transitions(asset, open_loan):
    """
    Where an asset can go next. An item on loan can only move by being
    returned, which keeps the allocation record and the status in step.
    """
    if open_loan:
        return []
    return TRANSITIONS.get(asset["status"], [])


@app.route("/assets/<int:asset_id>/qr.png")
@login_required
def asset_qr(asset_id):
    asset = query("SELECT asset_tag FROM assets WHERE id = %s", (asset_id,), one=True)
    if not asset:
        return "Not found", 404
    return png_of(url_for("tag_lookup", tag=asset["asset_tag"], _external=True),
                  f"{asset['asset_tag']}-qr.png")


def png_of(payload, filename):
    img = qrcode.make(payload, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", download_name=filename)


@app.route("/t/<tag>")
@login_required
def tag_lookup(tag):
    """Where a scanned asset QR code lands."""
    asset = query("""SELECT a.*, p.name AS holder, p.department, p.employee_id,
                            p.id AS holder_person_id, s.due_date, s.id AS assignment_id
                     FROM assets a
                     LEFT JOIN assignments s
                       ON s.asset_id = a.id AND s.returned_date IS NULL
                     LEFT JOIN people p ON p.id = s.person_id
                     WHERE a.asset_tag = %s""", (tag.upper(),), one=True)
    if not asset:
        # Older versions reissued the tag when an asset's type changed. Follow
        # that so labels printed before the change still scan.
        moved = query("""SELECT a.asset_tag FROM asset_events e
                         JOIN assets a ON a.id = e.asset_id
                         WHERE e.event_type = 'Retagged' AND e.from_value = %s
                         ORDER BY e.id DESC LIMIT 1""", (tag.upper(),), one=True)
        if moved:
            return redirect(url_for("tag_lookup", tag=moved["asset_tag"]))
        return render_template("not_found.html", tag=tag.upper()), 404
    return render_template("tag_lookup.html", a=asset)


# ------------------------------------------------------------------
# Lifecycle: move an asset's status along an allowed transition
# ------------------------------------------------------------------
@app.route("/assets/<int:asset_id>/status", methods=["POST"])
@login_required
def asset_status(asset_id):
    asset = query("SELECT * FROM assets WHERE id = %s", (asset_id,), one=True)
    if not asset:
        return render_template("not_found.html", tag=None), 404

    open_loan = query("""SELECT id, person_id FROM assignments
                         WHERE asset_id = %s AND returned_date IS NULL""",
                      (asset_id,), one=True)
    target = request.form.get("status", "")
    reason = request.form.get("reason", "").strip()

    if target not in allowed_transitions(asset, open_loan):
        if open_loan:
            flash("It's on loan. Mark it returned first, then change its status.", "ok")
        else:
            flash(f"An asset that's {asset['status'].lower()} can't move straight "
                  f"to {target.lower()}.", "ok")
        return redirect(url_for("asset_detail", asset_id=asset_id))

    if not reason:
        flash("Give a reason — it's what makes the lifecycle report worth reading.", "ok")
        return redirect(url_for("asset_detail", asset_id=asset_id))

    disposal = {}
    if target == "Retired":
        method = request.form.get("disposal_method", "")
        if method not in DISPOSAL_METHODS:
            flash("Say how the asset was disposed of.", "ok")
            return redirect(url_for("asset_detail", asset_id=asset_id))
        value = request.form.get("disposal_value", "").strip() or None
        if value:
            try:
                amount = parse_money(value)
            except ValueError:
                flash("Recovered value has to be a number.", "ok")
                return redirect(url_for("asset_detail", asset_id=asset_id))
            if amount < 0:
                flash("Recovered value can't be negative.", "ok")
                return redirect(url_for("asset_detail", asset_id=asset_id))
            if amount > MAX_MONEY:
                flash("That's larger than the register can store.", "ok")
                return redirect(url_for("asset_detail", asset_id=asset_id))
        disposal = {"method": method, "value": round(amount, 2) if value else None,
                    "date": parse_date(request.form.get("disposal_date", "")) or date.today()}

    db = get_db()
    with db.cursor() as cur:
        # Re-check under lock: the item may have been lent out or moved since
        # the checks above read it.
        locked = lock_asset(cur, asset_id)
        if not locked or locked["status"] != asset["status"] or open_loan_of(cur, asset_id):
            db.rollback()
            flash("This item changed while you were looking at it. Nothing was saved — "
                  "check its record and try again.", "ok")
            return redirect(url_for("asset_detail", asset_id=asset_id))
        if target == "Retired":
            cur.execute("""UPDATE assets SET status = 'Retired', disposal_date = %s,
                           disposal_method = %s, disposal_value = %s, disposal_note = %s
                           WHERE id = %s""",
                        (disposal["date"], disposal["method"], disposal["value"],
                         reason[:255], asset_id))
            log_event(cur, asset_id, "Disposed", from_value=asset["status"],
                      to_value=f"Retired — {disposal['method']}", note=reason)
        else:
            # Coming back from Retired clears the disposal record, otherwise the
            # reports would show a live asset that was also disposed of.
            if asset["status"] == "Retired":
                cur.execute("""UPDATE assets SET status = %s, disposal_date = NULL,
                               disposal_method = NULL, disposal_value = NULL,
                               disposal_note = NULL WHERE id = %s""", (target, asset_id))
            else:
                cur.execute("UPDATE assets SET status = %s WHERE id = %s", (target, asset_id))
            log_event(cur, asset_id, "Status changed", from_value=asset["status"],
                      to_value=target, note=reason)
    db.commit()
    flash(f"{asset['asset_tag']} is now {target.lower()}.", "ok")
    return redirect(url_for("asset_detail", asset_id=asset_id))


# ------------------------------------------------------------------
# Loans: return an item, or extend its deadline
# ------------------------------------------------------------------
@app.route("/assignments/<int:assignment_id>/return", methods=["POST"])
@login_required
def assignment_return(assignment_id):
    loan = query("""SELECT s.*, a.asset_tag FROM assignments s
                    JOIN assets a ON a.id = s.asset_id WHERE s.id = %s""",
                 (assignment_id,), one=True)
    if not loan or loan["returned_date"]:
        flash("That loan is already closed.", "ok")
        return redirect(request.referrer or url_for("dashboard"))

    condition = request.form.get("item_condition")
    db = get_db()
    with db.cursor() as cur:
        # Guarded on returned_date so a double click returns it once, not twice.
        cur.execute("""UPDATE assignments SET returned_date = %s
                       WHERE id = %s AND returned_date IS NULL""",
                    (date.today(), assignment_id))
        if cur.rowcount == 0:
            db.rollback()
            flash("That loan is already closed.", "ok")
            return redirect(request.referrer or url_for("dashboard"))
        if condition in CONDITIONS:
            cur.execute("""UPDATE assets SET item_condition = %s,
                           status = CASE WHEN %s IN ('Poor','Faulty')
                                         THEN 'Under Repair' ELSE 'In Storage' END
                           WHERE id = %s""", (condition, condition, loan["asset_id"]))
        else:
            cur.execute("UPDATE assets SET status = 'In Storage' WHERE id = %s",
                        (loan["asset_id"],))
        overdue = (loan["due_date"] and loan["due_date"] < date.today())
        log_event(cur, loan["asset_id"], "Returned", asset_tag=loan["asset_tag"],
                  from_value="On loan",
                  to_value="Under Repair" if condition in ("Poor", "Faulty") else "In Storage",
                  person_id=loan["person_id"],
                  note=(f"Returned {(date.today() - loan['due_date']).days} day(s) late"
                        if overdue else "Returned on time")
                       + (f", condition {condition.lower()}" if condition in CONDITIONS else ""))
    db.commit()
    if condition in ("Poor", "Faulty"):
        flash(f"{loan['asset_tag']} returned and sent for repair.", "ok")
    else:
        flash(f"{loan['asset_tag']} returned and back in storage.", "ok")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/assignments/<int:assignment_id>/extend", methods=["POST"])
@login_required
def assignment_extend(assignment_id):
    loan = query("""SELECT s.*, a.asset_tag FROM assignments s
                    JOIN assets a ON a.id = s.asset_id WHERE s.id = %s""",
                 (assignment_id,), one=True)
    if not loan or loan["returned_date"]:
        flash("That loan is closed, so there's nothing to extend.", "ok")
        return redirect(request.referrer or url_for("dashboard"))

    new_due = parse_date(request.form.get("new_due", ""))
    reason = request.form.get("reason", "").strip()
    if not new_due:
        flash("Pick a new return date.", "ok")
        return redirect(request.referrer or url_for("dashboard"))
    if new_due <= date.today():
        flash("An extension has to move the deadline forward.", "ok")
        return redirect(request.referrer or url_for("dashboard"))

    db = get_db()
    with db.cursor() as cur:
        extend_loan(cur, assignment_id, loan["due_date"], new_due,
                    reason or "Extended by admin")
    db.commit()
    flash(f"{loan['asset_tag']} now due {new_due.strftime('%d %b %Y')}.", "ok")
    return redirect(request.referrer or url_for("dashboard"))


def extend_loan(cur, assignment_id, previous_due, new_due, reason):
    """
    Move a deadline and leave a trail. Used by the extend button, the asset
    form and approvals, so all three count and record it the same way.

    Doesn't commit: the caller decides what else belongs in the same
    transaction (an approval must not extend the loan without also closing
    the request).

    Only a later date counts towards times_extended; pulling a deadline in
    is recorded, but it isn't an extension.
    """
    later = previous_due is None or new_due > previous_due
    cur.execute("""UPDATE assignments
                   SET due_date = %s, times_extended = times_extended + %s
                   WHERE id = %s""", (new_due, 1 if later else 0, assignment_id))
    cur.execute("""INSERT INTO assignment_extensions
                   (assignment_id, previous_due, new_due, reason, decided_by)
                   VALUES (%s,%s,%s,%s,%s)""",
                (assignment_id, previous_due, new_due, reason[:255],
                 session.get("user_id")))
    cur.execute("""SELECT asset_id, person_id FROM assignments WHERE id = %s""",
                (assignment_id,))
    row = cur.fetchone()
    if row:
        log_event(cur, row["asset_id"], "Extended" if later else "Due date brought forward",
                  from_value=previous_due, to_value=new_due,
                  person_id=row["person_id"], note=reason)
    clear_due_reminders(cur, assignment_id)


def clear_due_reminders(cur, assignment_id):
    """
    Drop due-soon and overdue reminders still waiting to go out for a loan
    whose deadline has just moved, so nobody is chased for the old date.
    """
    cur.execute("""DELETE FROM notifications
                   WHERE status = 'Queued' AND kind IN ('due_soon','overdue')
                     AND dedupe_key LIKE %s""", (f"%:{assignment_id}:%",))


# ------------------------------------------------------------------
# People and employee IDs
# ------------------------------------------------------------------
def person_form_values():
    f = request.form
    return {
        "employee_id": f.get("employee_id", "").strip().upper(),
        "name": f.get("name", "").strip(),
        "department": f.get("department", "").strip(),
        "job_title": f.get("job_title", "").strip(),
        "email": f.get("email", "").strip(),
        "phone": f.get("phone", "").strip(),
        "start_date": f.get("start_date") or None,
        "exit_date": f.get("exit_date") or None,
        "employment_status": f.get("employment_status", "Active"),
    }


def validate_person(v, person_id=None):
    errors = {}
    if not v["name"]:
        errors["name"] = "A name is required."
    if v["employment_status"] not in EMPLOYMENT_STATES:
        errors["employment_status"] = "Pick an employment status."
    if v["email"] and "@" not in v["email"]:
        errors["email"] = "That doesn't look like an email address."
    for field in ("start_date", "exit_date"):
        if v[field] and not parse_date(v[field]):
            errors[field] = "Use the date picker (YYYY-MM-DD)."
    if v["employment_status"] in ("Leaving", "Left") and not v["exit_date"]:
        errors["exit_date"] = "A leaver needs a last day, so reminders know when to chase."
    start, end = parse_date(v["start_date"] or ""), parse_date(v["exit_date"] or "")
    if start and end and end < start and "exit_date" not in errors:
        errors["exit_date"] = "The last day can't be before the start date."
    check_lengths(v, PERSON_LIMITS, errors)
    if v["employee_id"]:
        dup = query("SELECT id, name FROM people WHERE employee_id = %s", (v["employee_id"],), one=True)
        if dup and dup["id"] != person_id:
            errors["employee_id"] = f"{v['employee_id']} already belongs to {dup['name']}."
    return errors


@app.route("/people")
@login_required
def people_list():
    search = request.args.get("q", "").strip()
    state = request.args.get("state", "")
    sql = """SELECT p.*,
                    COUNT(s.id) AS holding,
                    SUM(s.due_date IS NOT NULL AND s.due_date < CURDATE()) AS overdue
             FROM people p
             LEFT JOIN assignments s
               ON s.person_id = p.id AND s.returned_date IS NULL
             WHERE 1 = 1"""
    args = []
    if search:
        sql += " AND (p.name LIKE %s OR p.employee_id LIKE %s OR p.department LIKE %s)"
        args += [f"%{search}%"] * 3
    if state:
        sql += " AND p.employment_status = %s"
        args.append(state)
    sql += " GROUP BY p.id ORDER BY FIELD(p.employment_status,'Leaving','Active','Left'), p.name"
    return render_template("people_list.html", people=query(sql, args),
                           f_q=search, f_state=state, states=EMPLOYMENT_STATES)


def next_employee_id(cur, pid):
    """
    EMP-0042 from the row id. An ID typed in by hand may already have taken
    that value, so step past any collision instead of failing the insert.
    """
    n = pid
    while True:
        candidate = f"EMP-{n:04d}"
        cur.execute("SELECT 1 FROM people WHERE employee_id = %s", (candidate,))
        if not cur.fetchone():
            return candidate
        n += 1


def give_missing_employee_id(cur, pid):
    """
    Give someone an employee ID if they have none (a row added straight into
    the database, or from before IDs were compulsory). Their badge, scan
    links and the request form all depend on one. Returns the new ID, or
    None if they already had one.
    """
    cur.execute("""SELECT 1 FROM people WHERE id = %s
                     AND (employee_id IS NULL OR employee_id = '') FOR UPDATE""", (pid,))
    if not cur.fetchone():
        return None
    new_id = next_employee_id(cur, pid)
    cur.execute("UPDATE people SET employee_id = %s WHERE id = %s", (new_id, pid))
    return new_id


@app.route("/people/new", methods=["GET", "POST"])
@login_required
def person_new():
    if request.method == "POST":
        v = person_form_values()
        errors = validate_person(v)
        if errors:
            return render_template("person_form.html", v=v, errors=errors,
                                   states=EMPLOYMENT_STATES, mode="new"), 400
        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                """INSERT INTO people
                   (employee_id, name, department, job_title, email, phone,
                    start_date, exit_date, employment_status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (v["employee_id"] or None, v["name"], v["department"], v["job_title"],
                 v["email"], v["phone"], v["start_date"], v["exit_date"],
                 v["employment_status"]),
            )
            pid = cur.lastrowid
            if not v["employee_id"]:
                give_missing_employee_id(cur, pid)
        db.commit()
        if v["employment_status"] == "Active":
            flash("Added to the staff list. Issue their equipment below.", "ok")
            return redirect(url_for("person_detail", person_id=pid, _anchor="issue"))
        flash("Added to the staff list.", "ok")
        return redirect(url_for("person_detail", person_id=pid))

    blank = {k: "" for k in ("employee_id", "name", "department", "job_title",
                             "email", "phone", "start_date", "exit_date")}
    blank["employment_status"] = "Active"
    return render_template("person_form.html", v=blank, errors={},
                           states=EMPLOYMENT_STATES, mode="new")


@app.route("/people/<int:person_id>/edit", methods=["GET", "POST"])
@login_required
def person_edit(person_id):
    person = query("SELECT * FROM people WHERE id = %s", (person_id,), one=True)
    if not person:
        return render_template("not_found.html", tag=None), 404

    if request.method == "POST":
        v = person_form_values()
        errors = validate_person(v, person_id)
        if errors:
            return render_template("person_form.html", v=v, errors=errors,
                                   states=EMPLOYMENT_STATES, mode="edit",
                                   person=person), 400
        was_active = person["employment_status"] == "Active"
        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                """UPDATE people SET employee_id=%s, name=%s, department=%s, job_title=%s,
                   email=%s, phone=%s, start_date=%s, exit_date=%s, employment_status=%s
                   WHERE id=%s""",
                (v["employee_id"] or person["employee_id"], v["name"], v["department"],
                 v["job_title"], v["email"], v["phone"], v["start_date"], v["exit_date"],
                 v["employment_status"], person_id),
            )
            # Leaving the field blank keeps the current ID, or issues one if
            # there wasn't one.
            give_missing_employee_id(cur, person_id)
        db.commit()

        if was_active and v["employment_status"] in ("Leaving", "Left"):
            raise_offboarding_alert(person_id)
        flash("Details updated.", "ok")
        return redirect(url_for("person_detail", person_id=person_id))

    v = dict(person)
    v["start_date"] = person["start_date"].isoformat() if person["start_date"] else ""
    v["exit_date"] = person["exit_date"].isoformat() if person["exit_date"] else ""
    return render_template("person_form.html", v=v, errors={},
                           states=EMPLOYMENT_STATES, mode="edit", person=person)


@app.route("/people/<int:person_id>/offboard", methods=["POST"])
@login_required
def person_offboard(person_id):
    """Mark someone as leaving and immediately raise the recovery alert."""
    person = query("SELECT * FROM people WHERE id = %s", (person_id,), one=True)
    if not person:
        return render_template("not_found.html", tag=None), 404

    state = request.form.get("employment_status", "Leaving")
    exit_date = parse_date(request.form.get("exit_date", ""))
    if state not in ("Leaving", "Left"):
        state = "Leaving"
    if not exit_date:
        flash("Set a last day first — the reminders count backwards from it.", "ok")
        return redirect(url_for("person_detail", person_id=person_id))

    db = get_db()
    with db.cursor() as cur:
        cur.execute("""UPDATE people SET employment_status=%s, exit_date=%s
                       WHERE id=%s""", (state, exit_date, person_id))
    db.commit()

    n = raise_offboarding_alert(person_id)
    if n:
        flash(f"{person['name']} marked as {state.lower()}. "
              f"{n} item{'s' if n != 1 else ''} flagged for return.", "ok")
    else:
        flash(f"{person['name']} marked as {state.lower()}. Nothing outstanding to recover.", "ok")
    return redirect(url_for("person_detail", person_id=person_id))


def raise_offboarding_alert(person_id):
    """
    Queue notifications to both the admin and the leaver listing everything
    still held. Returns the number of outstanding items.
    """
    person = query("SELECT * FROM people WHERE id = %s", (person_id,), one=True)
    items = query("""SELECT a.asset_tag, a.asset_type, a.make, a.model, s.due_date
                     FROM assignments s JOIN assets a ON a.id = s.asset_id
                     WHERE s.person_id = %s AND s.returned_date IS NULL
                     ORDER BY a.asset_tag""", (person_id,))
    if not items:
        return 0

    lines = "\n".join(
        f"  - {i['asset_tag']}  {(i['make'] or '')} {i['model']}".rstrip()
        for i in items
    )
    last_day = person["exit_date"].strftime("%d %b %Y") if person["exit_date"] else "their last day"
    stamp = person["exit_date"].isoformat() if person["exit_date"] else "tbc"

    admin_body = (
        f"{person['name']} ({person['employee_id']}, {person['department'] or 'no department'}) "
        f"is leaving on {last_day} and still holds {len(items)} item(s):\n\n{lines}\n\n"
        f"Collect these before the last day, then mark each one returned in the register."
    )
    staff_body = (
        f"Hello {person['name'].split()[0]},\n\n"
        f"Our records show you still have {len(items)} item(s) belonging to the organisation:\n\n"
        f"{lines}\n\n"
        f"Please return them to the IT office on or before {last_day}.\n"
        f"If any of this is wrong, reply and we'll correct the register."
    )

    db = get_db()
    with db.cursor() as cur:
        notify.queue(cur, kind="offboarding", audience="Admin",
                     subject=f"Recover {len(items)} item(s) from {person['name']} before {last_day}",
                     body=admin_body, person_id=person_id,
                     dedupe_key=f"offboard-start:admin:{person_id}:{stamp}")
        notify.queue(cur, kind="offboarding", audience="Employee",
                     subject=f"Please return {len(items)} item(s) before you leave",
                     body=staff_body, person_id=person_id, email=person["email"],
                     dedupe_key=f"offboard-start:staff:{person_id}:{stamp}")
    db.commit()
    return len(items)


@app.route("/people/<int:person_id>")
@login_required
def person_detail(person_id):
    person = query("SELECT * FROM people WHERE id = %s", (person_id,), one=True)
    if not person:
        return render_template("not_found.html", tag=None), 404
    return render_person(person)


@app.route("/e/<employee_id>")
@login_required
def employee_lookup(employee_id):
    """Employee ID -> full record. Also where a scanned staff badge lands."""
    person = query("SELECT * FROM people WHERE employee_id = %s",
                   (employee_id.upper(),), one=True)
    if not person:
        return render_template("not_found.html", tag=employee_id.upper()), 404
    return render_person(person)


def render_person(person):
    if not person["employee_id"]:
        db = get_db()
        with db.cursor() as cur:
            new_id = give_missing_employee_id(cur, person["id"])
        db.commit()
        if new_id:
            flash(f"{person['name']} had no employee ID, so they've been given {new_id}.", "ok")
        person = query("SELECT * FROM people WHERE id = %s", (person["id"],), one=True)
    holdings = query("""SELECT s.*, a.asset_tag, a.asset_type, a.make, a.model,
                               a.serial_number, a.item_condition
                        FROM assignments s JOIN assets a ON a.id = s.asset_id
                        WHERE s.person_id = %s AND s.returned_date IS NULL
                        ORDER BY s.due_date IS NULL, s.due_date""", (person["id"],))
    history = query("""SELECT s.*, a.asset_tag, a.model, a.make
                       FROM assignments s JOIN assets a ON a.id = s.asset_id
                       WHERE s.person_id = %s AND s.returned_date IS NOT NULL
                       ORDER BY s.returned_date DESC""", (person["id"],))
    requests_ = query("""SELECT r.*, a.asset_tag AS current_tag
                         FROM requests r LEFT JOIN assets a ON a.id = r.current_asset_id
                         WHERE r.person_id = %s ORDER BY r.created_at DESC""",
                      (person["id"],))
    # Equipment to hand over: only for current staff, Laptop unless asked otherwise.
    issue_type = request.args.get("type") if request.args.get("type") in ASSET_TYPES else "Laptop"
    stock = issuable_stock(issue_type) if person["employment_status"] == "Active" else []
    return render_template("person_detail.html", p=person, holdings=holdings,
                           history=history, requests=requests_, conditions=CONDITIONS,
                           types=ASSET_TYPES, issue_type=issue_type, stock=stock,
                           badge_url=url_for("employee_lookup",
                                             employee_id=person["employee_id"], _external=True))


def issuable_stock(asset_type):
    """
    Existing items of this type that can go straight to someone: in storage,
    in good condition, not on loan. Used ones ("Good") come first so stock
    already in circulation is reused before anything new is unboxed; within
    that, the most warranty left first.
    """
    return query("""SELECT a.id, a.asset_tag, a.make, a.model, a.serial_number,
                           a.item_condition, a.warranty_expiry, a.location
                    FROM assets a
                    LEFT JOIN assignments s ON s.asset_id = a.id AND s.returned_date IS NULL
                    WHERE a.asset_type = %s AND a.status = 'In Storage' AND s.id IS NULL
                      AND a.item_condition IN %s
                    ORDER BY FIELD(a.item_condition, 'Good', 'New'),
                             a.warranty_expiry IS NULL, a.warranty_expiry DESC, a.asset_tag""",
                 (asset_type, ISSUABLE_CONDITIONS))


@app.route("/people/<int:person_id>/issue", methods=["POST"])
@login_required
def person_issue(person_id):
    """Hand an item from storage straight to someone — e.g. a new starter's laptop."""
    back = url_for("person_detail", person_id=person_id, _anchor="issue")
    asset_id = request.form.get("asset_id", "")
    due_date = parse_date(request.form.get("due_date", "")) or None
    if not asset_id.isdigit():
        flash("Pick an item to issue.", "ok")
        return redirect(back)
    if due_date and due_date < date.today():
        flash("The return date has already passed. Pick a later one, or leave it blank.", "ok")
        return redirect(back)

    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM people WHERE id = %s FOR UPDATE", (person_id,))
        person = cur.fetchone()
        if not person:
            db.rollback()
            return render_template("not_found.html", tag=None), 404
        if person["employment_status"] != "Active":
            db.rollback()
            flash(f"{person['name']} is marked as {person['employment_status'].lower()}, "
                  f"so nothing new can be issued.", "ok")
            return redirect(back)

        # Re-checked under lock: someone may have issued it, or marked it
        # faulty, since the page was loaded.
        asset = lock_asset(cur, asset_id)
        if (not asset or asset["status"] != "In Storage"
                or asset["item_condition"] not in ISSUABLE_CONDITIONS
                or open_loan_of(cur, asset_id)):
            db.rollback()
            flash("That item is no longer in storage in good condition. Pick another.", "ok")
            return redirect(back)

        cur.execute("""INSERT INTO assignments (asset_id, person_id, assigned_date, due_date, note)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (asset_id, person_id, date.today(), due_date, "Issued from stock"))
        cur.execute("UPDATE assets SET status = 'In Use' WHERE id = %s", (asset_id,))
        log_event(cur, asset_id, "Allocated", asset_tag=asset["asset_tag"],
                  to_value="On loan", person_id=person_id,
                  note=f"Issued from stock, condition {asset['item_condition'].lower()}"
                       + (f", due {due_date}" if due_date else ", open-ended"))
        notify.queue(cur, kind="request", audience="Employee",
                     subject=f"{asset['asset_tag']} is ready to collect",
                     body=(f"Hello {person['name'].split()[0]},\n\n{asset['asset_tag']} has been "
                           f"assigned to you"
                           + (f" and is due back on {due_date.strftime('%d %b %Y')}."
                              if due_date else ".")
                           + "\n\nCollect it from the IT office."),
                     person_id=person_id, email=person["email"],
                     dedupe_key=f"issued-stock:{asset_id}:{person_id}:{date.today()}")
    db.commit()
    flash(f"{asset['asset_tag']} issued to {person['name']}.", "ok")
    return redirect(url_for("person_detail", person_id=person_id))


@app.route("/people/<int:person_id>/badge.png")
@login_required
def person_badge(person_id):
    person = query("SELECT employee_id FROM people WHERE id = %s", (person_id,), one=True)
    if not person or not person["employee_id"]:
        return "Not found", 404
    return png_of(url_for("employee_lookup", employee_id=person["employee_id"], _external=True),
                  f"{person['employee_id']}-badge.png")


# ------------------------------------------------------------------
# Requests and approvals
# ------------------------------------------------------------------
@app.route("/request", methods=["GET", "POST"])
def request_new():
    """
    Public application form. Employees have no login in v1, so they identify
    themselves with their employee ID; an admin approves before anything moves.

    Because anyone can reach this page, it never lists what an employee ID
    holds. Someone asking about an item they have types the tag printed on
    its label instead, and that is checked against their open loans.
    """
    prefill = request.args.get("id", "").strip().upper()

    if request.method == "POST":
        if throttled("request", *REQUEST_FORM_LIMIT):
            return render_template("request_form.html", errors={"form": True},
                                   types=ASSET_TYPES, request_types=REQUEST_TYPES,
                                   v=request.form, prefill="",
                                   throttled=True), 429
        record_attempt("request")

        employee_id = request.form.get("employee_id", "").strip().upper()
        rtype = request.form.get("request_type", "")
        asset_type = request.form.get("asset_type", "")
        reason = request.form.get("reason", "").strip()
        current_tag = request.form.get("current_tag", "").strip().upper()
        requested_until = request.form.get("requested_until") or None

        errors = {}
        person = query("SELECT * FROM people WHERE employee_id = %s", (employee_id,), one=True)
        if not person:
            errors["employee_id"] = "No one on the staff list has that ID. Check your badge."
        elif person["employment_status"] == "Left":
            errors["employee_id"] = "That record is closed. Speak to the IT office."
        if rtype not in REQUEST_TYPES:
            errors["request_type"] = "Pick what you're asking for."
        if len(reason) < 15:
            errors["reason"] = "Give a bit more detail — one line on why you need it."
        if rtype == "Keep longer":
            if not current_tag:
                errors["current_tag"] = "Enter the tag of the item you want to keep."
            until = parse_date(requested_until or "")
            if not requested_until:
                errors["requested_until"] = "Say how long you need it."
            elif not until:
                errors["requested_until"] = "Use the date picker (YYYY-MM-DD)."
            elif until <= date.today():
                errors["requested_until"] = "Pick a date in the future."
        elif rtype in ("Additional device", "Replacement") and asset_type not in ASSET_TYPES:
            errors["asset_type"] = "Pick the kind of device."
        if rtype != "Keep longer":
            requested_until = None      # the field is hidden for other types
        if rtype == "Additional device":
            current_tag = ""
        if rtype == "Keep longer":
            asset_type = ""

        # The item named has to be one this person is actually holding. The
        # message is the same whether the tag exists or not, so the form
        # can't be used to probe the register.
        loan = None
        if current_tag and person and "current_tag" not in errors:
            loan = query("""SELECT s.id, s.asset_id FROM assignments s
                            JOIN assets a ON a.id = s.asset_id
                            WHERE a.asset_tag = %s AND s.person_id = %s
                              AND s.returned_date IS NULL""",
                         (current_tag, person["id"]), one=True)
            if not loan:
                errors["current_tag"] = ("That tag isn't on loan to this employee ID. "
                                         "Copy it from the label on the item.")
        if len(reason) > 2000:
            errors["reason"] = "Keep it under 2,000 characters."

        if errors:
            return render_template("request_form.html", errors=errors, types=ASSET_TYPES,
                                   request_types=REQUEST_TYPES,
                                   v=request.form, prefill=employee_id), 400

        current_asset_id = loan["asset_id"] if loan else None
        assignment_id = loan["id"] if loan else None

        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                """INSERT INTO requests
                   (person_id, request_type, asset_type, current_asset_id,
                    assignment_id, requested_until, reason)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (person["id"], rtype, asset_type or None, current_asset_id,
                 assignment_id, requested_until, reason),
            )
            rid = cur.lastrowid
            reference = f"REQ-{rid:04d}"
            cur.execute("UPDATE requests SET reference = %s WHERE id = %s", (reference, rid))
            notify.queue(cur, kind="request", audience="Admin",
                         subject=f"{reference}: {rtype} requested by {person['name']}",
                         body=(f"{person['name']} ({person['employee_id']}) has applied for "
                               f"a {rtype.lower()}.\n\nReason given:\n{reason}\n\n"
                               f"Approve or decline it in the register."),
                         person_id=person["id"], dedupe_key=f"request:{rid}")
        db.commit()
        return render_template("request_sent.html", reference=reference,
                               first_name=person["name"].split()[0],
                               masked_email=mask_email(person["email"]))

    return render_template("request_form.html", errors={}, types=ASSET_TYPES,
                           request_types=REQUEST_TYPES, v={}, prefill=prefill)


def mask_email(email):
    """a.rahman@example.org -> a*******@example.org, for pages anyone can see."""
    if not email or "@" not in email:
        return None
    local, domain = email.split("@", 1)
    return f"{local[0]}{'*' * max(len(local) - 1, 3)}@{domain}"


@app.route("/requests")
@login_required
def request_list():
    state = request.args.get("state", "open")
    sql = """SELECT r.*, p.name, p.employee_id, p.department, p.email,
                    a.asset_tag AS current_tag, a.model AS current_model,
                    f.asset_tag AS issued_tag
             FROM requests r
             JOIN people p ON p.id = r.person_id
             LEFT JOIN assets a ON a.id = r.current_asset_id
             LEFT JOIN assets f ON f.id = r.fulfilled_asset_id"""
    if state == "open":
        sql += " WHERE r.status IN ('Pending','Approved')"
    elif state in ("Pending", "Approved", "Declined", "Fulfilled"):
        sql += f" WHERE r.status = '{state}'"
    sql += " ORDER BY FIELD(r.status,'Pending','Approved','Declined','Fulfilled'), r.created_at DESC"

    available = query("""SELECT a.id, a.asset_tag, a.asset_type, a.make, a.model
                         FROM assets a
                         LEFT JOIN assignments s
                           ON s.asset_id = a.id AND s.returned_date IS NULL
                         WHERE a.status = 'In Storage' AND s.id IS NULL
                         ORDER BY a.asset_type, a.asset_tag""")
    return render_template("request_list.html", requests=query(sql), f_state=state,
                           available=available)


REQUEST_FOR_UPDATE = """
    SELECT r.*, p.name, p.email, p.employee_id, p.employment_status, p.exit_date
    FROM requests r JOIN people p ON p.id = r.person_id
    WHERE r.id = %s FOR UPDATE"""


def back_to_requests(message):
    """Abandon whatever this request started, releasing its row locks."""
    get_db().rollback()
    flash(message, "ok")
    return redirect(url_for("request_list"))


@app.route("/requests/<int:request_id>/decide", methods=["POST"])
@login_required
def request_decide(request_id):
    decision = request.form.get("decision")
    note = request.form.get("decision_note", "").strip()

    db = get_db()
    with db.cursor() as cur:
        # Locked for the rest of the transaction, so a double click or two
        # admins at once can't both decide it (and extend a loan twice).
        cur.execute(REQUEST_FOR_UPDATE, (request_id,))
        req = cur.fetchone()
        if not req or req["status"] not in ("Pending", "Approved"):
            return back_to_requests("That request has already been decided.")

        if decision == "decline":
            cur.execute("""UPDATE requests SET status='Declined', decision_note=%s,
                           decided_by=%s, decided_at=NOW() WHERE id=%s""",
                        (note, session.get("user_id"), request_id))
            notify.queue(cur, kind="request", audience="Employee",
                         subject=f"{req['reference']}: not approved",
                         body=(f"Hello {req['name'].split()[0]},\n\nYour request "
                               f"({req['request_type'].lower()}) wasn't approved.\n\n"
                               f"{note or 'No further detail was given.'}\n\n"
                               f"Speak to the IT office if you need to discuss it."),
                         person_id=req["person_id"], email=req["email"],
                         dedupe_key=f"decision:{request_id}:declined")
            db.commit()
            flash(f"{req['reference']} declined.", "ok")
            return redirect(url_for("request_list"))

        # Employment may have changed since they applied.
        blocked = leaver_block(req)
        if blocked:
            return back_to_requests(blocked)

        if req["request_type"] == "Keep longer":
            if not (req["assignment_id"] and req["requested_until"]):
                return back_to_requests("That extension request doesn't name a loan and a new "
                                        "date. Extend the loan from the asset record instead.")
            # "Keep longer" resolves immediately by moving the due date — in the
            # same transaction as closing the request, so it can't half-happen.
            cur.execute("""SELECT due_date, returned_date FROM assignments
                           WHERE id = %s FOR UPDATE""", (req["assignment_id"],))
            loan = cur.fetchone()
            new_due = req["requested_until"]
            if not loan or loan["returned_date"]:
                return back_to_requests("That item has already been returned, so there's "
                                        "nothing to extend. Decline the request instead.")
            if new_due <= date.today():
                return back_to_requests(
                    f"The date asked for ({new_due.strftime('%d %b %Y')}) has passed. "
                    f"Extend the loan from the asset record with a new date.")
            extend_loan(cur, req["assignment_id"], loan["due_date"], new_due,
                        f"Approved request {req['reference']}")
            cur.execute("""UPDATE requests SET status='Fulfilled', decision_note=%s,
                           decided_by=%s, decided_at=NOW() WHERE id=%s""",
                        (note, session.get("user_id"), request_id))
            notify.queue(cur, kind="request", audience="Employee",
                         subject=f"{req['reference']}: approved, keep it until "
                                 f"{new_due.strftime('%d %b %Y')}",
                         body=(f"Hello {req['name'].split()[0]},\n\nYour request to keep the "
                               f"item longer was approved. The new return date is "
                               f"{new_due.strftime('%d %b %Y')}.\n\n{note}").strip(),
                         person_id=req["person_id"], email=req["email"],
                         dedupe_key=f"decision:{request_id}:approved")
            db.commit()
            flash(f"{req['reference']} approved — deadline moved.", "ok")
            return redirect(url_for("request_list"))

        # Device requests are approved first, then fulfilled when stock is picked.
        cur.execute("""UPDATE requests SET status='Approved', decision_note=%s,
                       decided_by=%s, decided_at=NOW() WHERE id=%s""",
                    (note, session.get("user_id"), request_id))
        notify.queue(cur, kind="request", audience="Employee",
                     subject=f"{req['reference']}: approved",
                     body=(f"Hello {req['name'].split()[0]},\n\nYour request for a "
                           f"{(req['asset_type'] or 'device').lower()} was approved. "
                           f"The IT office will let you know when it's ready to collect."
                           f"\n\n{note}").strip(),
                     person_id=req["person_id"], email=req["email"],
                     dedupe_key=f"decision:{request_id}:approved")
    db.commit()
    flash(f"{req['reference']} approved. Issue an item to close it off.", "ok")
    return redirect(url_for("request_list"))


def leaver_block(req):
    """
    Why a request can't go ahead given the applicant's employment now, or None.
    Leavers get nothing new, and can only keep an item up to their last day.
    """
    state = req["employment_status"]
    if state == "Active":
        return None
    if req["request_type"] == "Keep longer" and state == "Leaving":
        if req["exit_date"] and req["requested_until"] and req["requested_until"] > req["exit_date"]:
            return (f"{req['name']} leaves on {req['exit_date'].strftime('%d %b %Y')}, before "
                    f"the date asked for. Decline it, or extend to their last day from the "
                    f"asset record.")
        return None
    return (f"{req['name']} is now marked as {state.lower()}, so nothing new can be "
            f"issued. Decline the request instead.")


@app.route("/requests/<int:request_id>/issue", methods=["POST"])
@login_required
def request_issue(request_id):
    """Approved request + a chosen item from storage = a new loan."""
    asset_id = request.form.get("asset_id", "")
    due_date = parse_date(request.form.get("due_date", "")) or None
    if not asset_id.isdigit():
        flash("Pick an item to issue.", "ok")
        return redirect(url_for("request_list"))
    if due_date and due_date < date.today():
        flash("The return date has already passed. Pick a later one, or leave it blank.", "ok")
        return redirect(url_for("request_list"))

    db = get_db()
    with db.cursor() as cur:
        # Lock the request and the item, so neither can be issued twice.
        cur.execute(REQUEST_FOR_UPDATE, (request_id,))
        req = cur.fetchone()
        if not req or req["status"] != "Approved":
            return back_to_requests("Approve the request before issuing an item.")
        blocked = leaver_block(req)
        if blocked:
            return back_to_requests(blocked)

        asset = lock_asset(cur, asset_id)
        if not asset or asset["status"] != "In Storage":
            return back_to_requests("Only an item that's in storage can be issued. Pick another.")
        if open_loan_of(cur, asset_id):
            return back_to_requests("Someone already holds that item — pick another.")

        cur.execute("""INSERT INTO assignments (asset_id, person_id, assigned_date, due_date, note)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (asset_id, req["person_id"], date.today(), due_date,
                     f"Issued against {req['reference']}"))
        cur.execute("UPDATE assets SET status = 'In Use' WHERE id = %s", (asset_id,))
        cur.execute("""UPDATE requests SET status='Fulfilled', fulfilled_asset_id=%s
                       WHERE id=%s""", (asset_id, request_id))
        log_event(cur, asset_id, "Allocated", asset_tag=asset["asset_tag"],
                  to_value="On loan", person_id=req["person_id"],
                  note=f"Issued against approved request {req['reference']}"
                       + (f", due {due_date}" if due_date else ", open-ended"))
        notify.queue(cur, kind="request", audience="Employee",
                     subject=f"{req['reference']}: {asset['asset_tag']} is ready to collect",
                     body=(f"Hello {req['name'].split()[0]},\n\n{asset['asset_tag']} has been "
                           f"assigned to you"
                           + (f" and is due back on {due_date.strftime('%d %b %Y')}."
                              if due_date else ".")
                           + "\n\nCollect it from the IT office."),
                     person_id=req["person_id"], email=req["email"],
                     dedupe_key=f"issued:{request_id}")
    db.commit()
    flash(f"{asset['asset_tag']} issued to {req['name']}.", "ok")
    return redirect(url_for("request_list"))


# ------------------------------------------------------------------
# Reporting and CSV export
# ------------------------------------------------------------------
def cell(value, kind):
    """
    Format one table cell for display. Returns (text, css class) so the
    warranty and due-date columns carry the same colour coding they do
    everywhere else in the app.
    """
    if value is None or value == "":
        return "—", "muted"
    if kind == "warranty":
        return warranty_state(value)
    if kind == "due":
        return due_state(value)
    if kind == "date":
        return value.strftime("%d %b %Y"), ""
    if kind == "datetime":
        return value.strftime("%d %b %Y %H:%M"), "mono"
    if kind == "money":
        return f"{float(value):,.2f}", "mono"
    if kind == "int":
        return f"{int(value):,}", "mono"
    if kind == "tag":
        return value, "mono"
    return str(value), ""


def sort_value(value, kind):
    """What the client-side column sort compares: ISO dates, bare numbers, folded text."""
    if value is None or value == "":
        return ""
    if kind in ("date", "datetime", "warranty", "due") and hasattr(value, "isoformat"):
        return value.isoformat()
    if kind in ("money", "int"):
        return f"{float(value):.4f}"
    return str(value).lower()


app.jinja_env.globals["cell"] = cell
app.jinja_env.globals["sort_value"] = sort_value


def csv_value(value, kind):
    """Machine-readable equivalent: ISO dates, bare numbers, no colour."""
    if value is None:
        return ""
    if kind in ("date", "warranty", "due"):
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    if kind == "datetime":
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if kind == "money":
        return f"{float(value):.2f}"
    return str(value)


def report_params(spec=None):
    """
    Only the filters a report declares are read, and blanks are dropped,
    so URLs (and the CSV link built from them) stay short and predictable.
    """
    keys = spec["filters"] if spec else reports.FILTER_LABELS.keys()
    params = {}
    for k in keys:
        v = request.args.get(k, "").strip()
        if v:
            params[k] = v
    return params


def run_report(key, params):
    spec = reports.REPORTS[key]
    sql, args = spec["build"](params)
    return spec, query(sql, args)


def column_totals(spec, rows):
    """
    One aggregate per numeric column: money and counts are summed,
    durations averaged. Returns {key: (value, 'sum'|'avg')}.
    """
    totals = {}
    for col_key, _, kind in spec["columns"]:
        if kind not in ("money", "int"):
            continue
        vals = [float(r[col_key]) for r in rows if r.get(col_key) is not None]
        if not vals:
            continue
        if col_key in reports.AVERAGED:
            totals[col_key] = (sum(vals) / len(vals), "avg")
        else:
            totals[col_key] = (sum(vals), "sum")
    return totals


def active_filters(key, spec, params):
    """Chips for the filters in play, each with a link that drops just that one."""
    windows = dict(reports.WINDOWS)
    chips = []
    for k in spec["filters"]:
        if k not in params:
            continue
        value = params[k]
        if k == "window":
            value = windows.get(value, value)
        elif k == "overdue":
            value = "Yes"
        rest = {pk: pv for pk, pv in params.items() if pk != k}
        chips.append({"label": reports.FILTER_LABELS[k], "value": value,
                      "remove_url": url_for("report_view", key=key, **rest)})
    return chips


@app.route("/reports")
@login_required
def report_hub():
    totals = query("""
        SELECT
          (SELECT COUNT(*) FROM assets)                                    AS assets,
          (SELECT COUNT(*) FROM assets WHERE status <> 'Retired')          AS live,
          (SELECT COALESCE(SUM(purchase_cost),0) FROM assets
             WHERE status <> 'Retired')                                    AS value,
          (SELECT COUNT(*) FROM assignments WHERE returned_date IS NULL)   AS on_loan,
          (SELECT COUNT(*) FROM assignments WHERE returned_date IS NULL
             AND due_date IS NOT NULL AND due_date < CURDATE())            AS overdue,
          (SELECT COUNT(*) FROM assets WHERE status <> 'Retired'
             AND warranty_expiry IS NOT NULL AND warranty_expiry < CURDATE()) AS out_of_warranty,
          (SELECT COUNT(*) FROM assets WHERE status <> 'Retired'
             AND warranty_expiry BETWEEN CURDATE()
                 AND CURDATE() + INTERVAL 90 DAY)                          AS expiring,
          (SELECT COUNT(*) FROM asset_events)                              AS events
    """, one=True)

    # Status split, always in lifecycle order so the bar never reshuffles
    counts = {r["status"]: r["n"] for r in query(
        "SELECT status, COUNT(*) AS n FROM assets GROUP BY status")}
    status_total = sum(counts.values())
    by_status = [{"status": s, "n": counts.get(s, 0),
                  "pct": (counts.get(s, 0) / status_total * 100) if status_total else 0}
                 for s in STATUSES]

    by_type = query("""
        SELECT asset_type, COUNT(*) AS n, COALESCE(SUM(purchase_cost),0) AS value
        FROM assets WHERE status <> 'Retired'
        GROUP BY asset_type ORDER BY value DESC, n DESC
    """)
    type_max = max((float(r["value"]) for r in by_type), default=0)

    return render_template("reports.html", groups=reports.GROUPS,
                           group_blurbs=reports.GROUP_BLURBS,
                           catalogue=reports.REPORTS, labels=reports.FILTER_LABELS,
                           totals=totals, by_status=by_status, status_total=status_total,
                           by_type=by_type, type_max=type_max)


@app.route("/reports/<key>")
@login_required
def report_view(key):
    if key not in reports.REPORTS:
        return render_template("not_found.html", tag=None), 404
    spec = reports.REPORTS[key]
    params = report_params(spec)
    spec, rows = run_report(key, params)

    # Options for whichever filters this report declares
    options = {}
    if "status" in spec["filters"]:
        options["status"] = STATUSES
    if "type" in spec["filters"]:
        options["type"] = list(ASSET_TYPES)
    if "department" in spec["filters"]:
        options["department"] = [r["department"] for r in query(
            """SELECT DISTINCT department FROM people
               WHERE department IS NOT NULL AND department <> ''
               ORDER BY department""")]
    if "window" in spec["filters"]:
        options["window"] = reports.WINDOWS
    if "event" in spec["filters"]:
        options["event"] = [r["event_type"] for r in query(
            "SELECT DISTINCT event_type FROM asset_events ORDER BY event_type")]

    # Scale for in-cell bars: each bar column against its own maximum
    bar_max = {c: max((float(r.get(c) or 0) for r in rows), default=0)
               for c in spec.get("bars", [])}

    limit = spec.get("limit")
    return render_template("report.html", key=key, spec=spec, rows=rows,
                           params=params, options=options,
                           totals=column_totals(spec, rows),
                           chips=active_filters(key, spec, params),
                           bar_max=bar_max,
                           truncated=bool(limit and len(rows) >= limit),
                           generated=datetime.now())


@app.route("/reports/<key>.csv")
@login_required
def report_csv(key):
    if key not in reports.REPORTS:
        return "Unknown report", 404
    spec, rows = run_report(key, report_params(reports.REPORTS[key]))

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for _, label, _ in spec["columns"]])
    for row in rows:
        writer.writerow([csv_value(row.get(k), kind) for k, _, kind in spec["columns"]])

    # A provenance line so a printed CSV can be traced back to when it was run.
    writer.writerow([])
    writer.writerow([f"{spec['title']} — {len(rows)} row(s)",
                     f"generated {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                     f"by {session.get('full_name', 'unknown')}"])

    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))  # BOM keeps Excel happy
    return send_file(data, mimetype="text/csv", as_attachment=True,
                     download_name=f"{key}-{date.today().isoformat()}.csv")


# ------------------------------------------------------------------
# Notification outbox
# ------------------------------------------------------------------
@app.route("/notifications")
@login_required
def notifications():
    rows = query("""SELECT n.*, p.name, p.employee_id FROM notifications n
                    LEFT JOIN people p ON p.id = n.person_id
                    ORDER BY n.created_at DESC LIMIT 200""")
    return render_template("notifications.html", rows=rows,
                           smtp_on=bool(notify.SMTP_HOST))


@app.route("/notifications/send", methods=["POST"])
@login_required
def notifications_send():
    sent, skipped, failed = notify.send_queued(get_db())
    if not notify.SMTP_HOST:
        flash("No mail server configured, so queued alerts were marked skipped. "
              "They're still listed here as a record.", "ok")
    else:
        flash(f"{sent} sent, {skipped} skipped, {failed} failed.", "ok")
    return redirect(url_for("notifications"))


# ------------------------------------------------------------------
# Daily job:  python app.py run-checks
# ------------------------------------------------------------------
def run_checks(send=True):
    """
    Raise reminders for deadlines, leavers and warranties.
    Safe to run repeatedly — dedupe keys stop duplicate alerts.
    """
    today = date.today()
    created = 0

    with app.app_context():
        db = get_db()
        alerts = collect_alerts()

        with db.cursor() as cur:
            # 0. Anyone without an employee ID gets one, so emails and the
            #    request form can identify them.
            cur.execute("""SELECT id FROM people
                           WHERE employee_id IS NULL OR employee_id = ''""")
            for row in cur.fetchall():
                give_missing_employee_id(cur, row["id"])

            # 1. Deadlines coming up, at fixed marks rather than every day
            for loan in alerts["due_soon"]:
                left = (loan["due_date"] - today).days
                if left not in DUE_REMINDER_MARKS:
                    continue
                when = "today" if left == 0 else f"in {left} day{'s' if left != 1 else ''}"
                created += notify.queue(
                    cur, kind="due_soon", audience="Employee",
                    subject=f"{loan['asset_tag']} is due back {when}",
                    body=(f"Hello {loan['name'].split()[0]},\n\n{loan['asset_tag']} "
                          f"({(loan['make'] or '')} {loan['model']}".strip() + ") is due back "
                          f"{when}, on {loan['due_date'].strftime('%d %b %Y')}.\n\n"
                          f"Return it to the IT office, or apply to keep it longer at "
                          f"/request if you still need it."),
                    person_id=loan["person_id"], email=loan["email"],
                    dedupe_key=f"due:{loan['assignment_id']}:{loan['due_date']}:{left}")

            # 2. Overdue — chase weekly, and copy the admin
            for loan in alerts["overdue"]:
                over = (today - loan["due_date"]).days
                week = over // 7
                created += notify.queue(
                    cur, kind="overdue", audience="Employee",
                    subject=f"{loan['asset_tag']} is {over} day(s) overdue",
                    body=(f"Hello {loan['name'].split()[0]},\n\n{loan['asset_tag']} was due "
                          f"back on {loan['due_date'].strftime('%d %b %Y')} and is now "
                          f"{over} day(s) overdue.\n\nPlease return it, or apply for an "
                          f"extension at /request."),
                    person_id=loan["person_id"], email=loan["email"],
                    dedupe_key=f"overdue:{loan['assignment_id']}:{loan['due_date']}:w{week}")
                created += notify.queue(
                    cur, kind="overdue", audience="Admin",
                    subject=f"Overdue: {loan['asset_tag']} with {loan['name']} ({over}d)",
                    body=(f"{loan['asset_tag']} ({(loan['make'] or '')} {loan['model']}".strip()
                          + f") has been with {loan['name']} ({loan['employee_id']}) since "
                          f"{loan['assigned_date'].strftime('%d %b %Y')} and is {over} day(s) "
                          f"past its return date."),
                    person_id=loan["person_id"],
                    dedupe_key=f"overdue-admin:{loan['assignment_id']}:{loan['due_date']}:w{week}")

            # 3. Leavers still holding equipment — nudge weekly until it's back
            per_person = {}
            for row in alerts["leaving"]:
                per_person.setdefault(row["person_id"], []).append(row)
            iso = today.isocalendar()
            for pid, items in per_person.items():
                first = items[0]
                lines = "\n".join(f"  - {i['asset_tag']}  {(i['make'] or '')} {i['model']}".rstrip()
                                  for i in items)
                last_day = (first["exit_date"].strftime("%d %b %Y")
                            if first["exit_date"] else "their last day")
                created += notify.queue(
                    cur, kind="offboarding", audience="Admin",
                    subject=f"Still to recover from {first['name']}: {len(items)} item(s)",
                    body=(f"{first['name']} ({first['employee_id']}) is marked as "
                          f"{first['employment_status'].lower()}, last day {last_day}, and "
                          f"still holds:\n\n{lines}"),
                    person_id=pid,
                    dedupe_key=f"offboard-nudge:admin:{pid}:{iso[0]}W{iso[1]}")
                created += notify.queue(
                    cur, kind="offboarding", audience="Employee",
                    subject=f"Reminder: {len(items)} item(s) to return before {last_day}",
                    body=(f"Hello {first['name'].split()[0]},\n\nBefore you leave on "
                          f"{last_day}, please return:\n\n{lines}\n\nDrop them at the IT office."),
                    person_id=pid, email=first["email"],
                    dedupe_key=f"offboard-nudge:staff:{pid}:{iso[0]}W{iso[1]}")

            # 4. Warranties — admin only, once per 30/7/expired bucket
            for asset in alerts["warranty"]:
                left = (asset["warranty_expiry"] - today).days
                bucket = "expired" if left < 0 else ("7" if left <= 7 else "30")
                label = (f"expired {abs(left)} day(s) ago" if left < 0
                         else f"expires in {left} day(s)")
                created += notify.queue(
                    cur, kind="warranty", audience="Admin",
                    subject=f"Warranty {label}: {asset['asset_tag']}",
                    body=(f"{asset['asset_tag']} ({(asset['make'] or '')} {asset['model']}".strip()
                          + f") — warranty {label} "
                          f"({asset['warranty_expiry'].strftime('%d %b %Y')}). "
                          f"Decide whether to extend cover, replace it, or accept the risk."),
                    dedupe_key=f"warranty:{asset['id']}:{bucket}")
        db.commit()

        result = (0, 0, 0)
        if send:
            result = notify.send_queued(db)

    return created, result


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------
def create_admin():
    import getpass
    username = input("Username: ").strip()
    full_name = input("Full name: ").strip()
    password = getpass.getpass("Password: ")
    if len(password) < 8:
        print("Use at least 8 characters.")
        return
    with app.app_context():
        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, full_name) VALUES (%s,%s,%s)",
                (username, generate_password_hash(password), full_name),
            )
        db.commit()
    print(f"Created admin '{username}'.")


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "create-admin":
        create_admin()
    elif cmd == "run-checks":
        made, (sent, skipped, failed) = run_checks()
        print(f"{made} new notification(s) queued.")
        print(f"Delivery: {sent} sent, {skipped} skipped, {failed} failed.")
    else:
        if not SECRET_KEY_FROM_ENV:
            print("Warning: SECRET_KEY is not set, so a random one is in use and everyone "
                  "is signed out whenever the app restarts. Set it before real use.")
        # The Werkzeug debugger runs code from the browser, so it is opt-in.
        app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
