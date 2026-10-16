"""
Populate the register with synthetic data for testing and the demo.
Clearly fake: no real serials, no real staff.

Deliberately seeds a few awkward cases so the alerts page has something to
show on day one: two overdue loans, one due this week, one leaver still
holding equipment, and two pending requests.

Usage:  python seed_data.py
"""
import random
from datetime import date, timedelta

from app import ASSET_TYPES, app, get_db, raise_offboarding_alert

PEOPLE = [
    ("Amina Rahman", "Administration", "Office Manager", "Active"),
    ("Daniel Okoro", "Teaching", "Science Teacher", "Active"),
    ("Priya Nair", "Teaching", "Head of Maths", "Active"),
    ("Tomas Berg", "Finance", "Accounts Assistant", "Active"),
    ("Grace Mensah", "Reception", "Receptionist", "Leaving"),
    ("Hasan Ali", "IT", "IT Coordinator", "Active"),
]

CATALOGUE = [
    ("Laptop", "Dell", "Latitude 5420"), ("Laptop", "Lenovo", "ThinkPad E14"),
    ("Laptop", "HP", "ProBook 450 G9"), ("Desktop", "Dell", "OptiPlex 3000"),
    ("Monitor", "Dell", "P2422H"), ("Monitor", "Samsung", "S24R350"),
    ("Phone", "Samsung", "Galaxy A15"), ("Tablet", "Apple", "iPad 9th gen"),
    ("Printer", "Brother", "HL-L2350DW"), ("Network device", "TP-Link", "EAP225 access point"),
    ("Software licence", "Microsoft", "Microsoft 365 Business Basic"),
    ("Software licence", "Adobe", "Acrobat Standard"),
]

SUPPLIERS = ["Star Tech", "Ryans Computers", "Global Brand", "Computer Source",
             "Smart Technologies"]

CONDITION_BY_STATUS = {
    "In Use": ["New", "Good", "Good", "Fair"],
    "In Storage": ["New", "Good", "Fair"],
    "Under Repair": ["Poor", "Faulty"],
    "Retired": ["Poor", "Faulty"],
}

# Loan ages, in days from today. Negative = overdue.
DUE_OFFSETS = [-21, -6, 3, 12, 40, 95, None, None]


def fake_serial():
    return "SYN-" + "".join(random.choices("ABCDEFGHJKMNPQRSTUVWXYZ23456789", k=8))


def main(count=24):
    random.seed(25)
    today = date.today()
    with app.app_context():
        db = get_db()
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM assets")
            if cur.fetchone()["n"]:
                print("Assets already exist — clear the tables first for a fresh seed.")
                return

            people = {}
            for name, dept, title, state in PEOPLE:
                cur.execute(
                    """INSERT INTO people
                       (name, department, job_title, email, start_date,
                        exit_date, employment_status)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (name, dept, title,
                     name.lower().replace(" ", ".") + "@example.org",
                     today - timedelta(days=random.randint(200, 2000)),
                     today + timedelta(days=14) if state != "Active" else None,
                     state),
                )
                pid = cur.lastrowid
                cur.execute("UPDATE people SET employee_id = %s WHERE id = %s",
                            (f"EMP-{pid:04d}", pid))
                people[name] = pid

            issued = 0
            for _ in range(count):
                atype, make, model = random.choice(CATALOGUE)
                status = random.choices(
                    ["In Use", "In Storage", "Under Repair", "Retired"],
                    weights=[6, 3, 1, 1],
                )[0]
                purchased = today - timedelta(days=random.randint(90, 1500))
                warranty = purchased + timedelta(days=random.choice([365, 730, 1095, 1460]))
                # Repair and disposal happen between purchase and today, never in
                # the future, whatever the warranty date is.
                span = (today - purchased).days
                repaired_on = purchased + timedelta(days=span // 2)
                disposed_on = purchased + timedelta(days=max(span - random.randint(1, 30), span // 2 + 1))

                cost = round(random.uniform(120, 1400), 2)
                cur.execute(
                    """INSERT INTO assets
                       (asset_type, make, model, serial_number, location, purchase_date,
                        purchase_cost, supplier, invoice_ref, warranty_expiry,
                        warranty_provider, warranty_ref, item_condition, status, notes,
                        disposal_date, disposal_method, disposal_value, disposal_note)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (atype, make, model,
                     None if atype == "Software licence" else fake_serial(),
                     random.choice(["Room 12", "Room 4", "Store cupboard", "Front office", "Library"]),
                     purchased, cost,
                     random.choice(SUPPLIERS), f"INV-{purchased.year}-{random.randint(1000,9999)}",
                     warranty, f"{make} standard cover",
                     "WR-" + "".join(random.choices("0123456789", k=7)),
                     random.choice(CONDITION_BY_STATUS[status]), status,
                     "Synthetic test record.",
                     # Retired items get a disposal record, so that report isn't empty
                     *( (disposed_on,
                         random.choice(["Recycled", "Sold", "Donated", "Destroyed"]),
                         round(cost * random.uniform(0, 0.15), 2),
                         "End of useful life.") if status == "Retired"
                        else (None, None, None, None) )),
                )
                asset_id = cur.lastrowid
                tag = f"{ASSET_TYPES[atype]}-{asset_id:05d}"
                cur.execute("UPDATE assets SET asset_tag = %s WHERE id = %s", (tag, asset_id))

                # Audit trail: registration, then the moves that got it to
                # today's status, so the lifecycle report reads like a history
                cur.execute("""INSERT INTO asset_events
                               (asset_id, asset_tag, event_type, to_value, note, created_at)
                               VALUES (%s,%s,'Registered',%s,%s,%s)""",
                            (asset_id, tag, "In Storage",
                             f"{atype} — {make} {model}", purchased))
                if status in ("Under Repair", "Retired"):
                    cur.execute("""INSERT INTO asset_events
                                   (asset_id, asset_tag, event_type, from_value, to_value,
                                    note, created_at)
                                   VALUES (%s,%s,'Status changed','In Storage','Under Repair',%s,%s)""",
                                (asset_id, tag, "Reported faulty by the holder",
                                 repaired_on))
                if status == "Retired":
                    cur.execute("""INSERT INTO asset_events
                                   (asset_id, asset_tag, event_type, from_value, to_value,
                                    note, created_at)
                                   VALUES (%s,%s,'Disposed','Under Repair','Retired',%s,%s)""",
                                (asset_id, tag, "Beyond economic repair.",
                                 disposed_on))

                if status == "In Use":
                    # Give the leaver a couple of items so offboarding has teeth
                    holder = ("Grace Mensah" if issued in (1, 4)
                              else random.choice(list(people)))
                    offset = DUE_OFFSETS[issued % len(DUE_OFFSETS)]
                    due = today + timedelta(days=offset) if offset is not None else None
                    assigned_on = max(purchased, today - timedelta(days=random.randint(30, 300)))
                    cur.execute(
                        """INSERT INTO assignments
                           (asset_id, person_id, assigned_date, due_date)
                           VALUES (%s,%s,%s,%s)""",
                        (asset_id, people[holder], assigned_on, due),
                    )
                    cur.execute("""INSERT INTO asset_events
                                   (asset_id, asset_tag, event_type, to_value, note,
                                    person_id, created_at)
                                   VALUES (%s,%s,'Allocated','On loan',%s,%s,%s)""",
                                (asset_id, tag,
                                 f"Due {due.isoformat()}" if due else "Open-ended",
                                 people[holder], assigned_on))
                    issued += 1

            # A few closed loans, so the ownership-history report has past rows
            cur.execute("""SELECT id, asset_tag FROM assets
                           WHERE status = 'In Storage' LIMIT 3""")
            for row in cur.fetchall():
                start = today - timedelta(days=random.randint(400, 700))
                end = start + timedelta(days=random.randint(60, 300))
                holder = random.choice(list(people))
                cur.execute("""INSERT INTO assignments
                               (asset_id, person_id, assigned_date, due_date, returned_date)
                               VALUES (%s,%s,%s,%s,%s)""",
                            (row["id"], people[holder], start, end, end))
                cur.execute("""INSERT INTO asset_events
                               (asset_id, asset_tag, event_type, to_value, note,
                                person_id, created_at)
                               VALUES (%s,%s,'Allocated','On loan','Open-ended',%s,%s)""",
                            (row["id"], row["asset_tag"], people[holder], start))
                cur.execute("""INSERT INTO asset_events
                               (asset_id, asset_tag, event_type, from_value, to_value,
                                note, person_id, created_at)
                               VALUES (%s,%s,'Returned','On loan','In Storage',
                                       'Returned on time',%s,%s)""",
                            (row["id"], row["asset_tag"], people[holder], end))

            # Two applications waiting on a decision
            cur.execute("""SELECT a.id, s.id AS assignment_id FROM assets a
                           JOIN assignments s ON s.asset_id = a.id AND s.returned_date IS NULL
                           WHERE s.person_id = %s LIMIT 1""", (people["Daniel Okoro"],))
            held = cur.fetchone()
            cur.execute(
                """INSERT INTO requests
                   (person_id, request_type, asset_type, current_asset_id, reason)
                   VALUES (%s,'Additional device','Laptop',%s,%s)""",
                (people["Daniel Okoro"], held["id"] if held else None,
                 "I run the after-school science club in the hall and can't keep "
                 "carrying my classroom laptop across the site."),
            )
            cur.execute("UPDATE requests SET reference = %s WHERE id = %s",
                        (f"REQ-{cur.lastrowid:04d}", cur.lastrowid))

            cur.execute(
                """INSERT INTO requests
                   (person_id, request_type, asset_type, reason)
                   VALUES (%s,'Replacement','Phone',%s)""",
                (people["Tomas Berg"],
                 "The handset stopped charging last week and the cable isn't the problem."),
            )
            cur.execute("UPDATE requests SET reference = %s WHERE id = %s",
                        (f"REQ-{cur.lastrowid:04d}", cur.lastrowid))
        db.commit()

        # Raise the offboarding alert for the seeded leaver
        raise_offboarding_alert(people["Grace Mensah"])

    print(f"Seeded {len(PEOPLE)} people and {count} synthetic assets, with overdue "
          f"loans, a leaver, disposals, closed loans, an audit trail and two "
          f"pending requests.")


if __name__ == "__main__":
    main()
