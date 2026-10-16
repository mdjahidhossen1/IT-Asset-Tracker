"""
Store report details and keep the page and CSV file same
New reports can be added

"""

from datetime import date, timedelta

# Columns are (key, label, kind). kind drives formatting:
#   text | date | datetime | money | int | tag | person | status |
#   employment | tenure | event | due | warranty
INVENTORY_COLUMNS = [
    ("asset_tag", "Tag", "tag"),
    ("asset_type", "Type", "text"),
    ("make", "Make", "text"),
    ("model", "Model", "text"),
    ("serial_number", "Serial", "text"),
    ("location", "Location", "text"),
    ("status", "Status", "status"),
    ("item_condition", "Condition", "text"),
    ("holder", "Held by", "text"),
    ("holder_id", "Employee ID", "person"),
    ("purchase_date", "Purchased", "date"),
    ("purchase_cost", "Cost", "money"),
    ("warranty_expiry", "Warranty", "warranty"),
]

BASE_INVENTORY = """
    SELECT a.*, p.name AS holder, p.employee_id AS holder_id, p.department,
           s.due_date, s.id AS assignment_id
    FROM assets a
    LEFT JOIN assignments s ON s.asset_id = a.id AND s.returned_date IS NULL
    LEFT JOIN people p ON p.id = s.person_id
"""


def _inventory(params):
    where, args = [], []
    if params.get("status"):
        where.append("a.status = %s")
        args.append(params["status"])
    if params.get("type"):
        where.append("a.asset_type = %s")
        args.append(params["type"])
    if params.get("department"):
        where.append("p.department = %s")
        args.append(params["department"])
    sql = BASE_INVENTORY
    if where:
        sql += " WHERE " + " AND ".join(where)
    return sql + " ORDER BY a.asset_type, a.asset_tag", args


def _warranty(params):
    """Expiry window: 30 / 60 / 90 days ahead, or everything already expired."""
    window = params.get("window", "90")
    today = date.today()
    if window == "expired":
        clause = "a.warranty_expiry < %s"
        args = [today]
    else:
        days = int(window) if window in ("30", "60", "90", "180", "365") else 90
        clause = "a.warranty_expiry BETWEEN %s AND %s"
        args = [today, today + timedelta(days=days)]
    sql = (BASE_INVENTORY +
           " WHERE a.status <> 'Retired' AND a.warranty_expiry IS NOT NULL AND " +
           clause + " ORDER BY a.warranty_expiry")
    return sql, args


def _allocation(params):
    where, args = ["s.returned_date IS NULL"], []
    if params.get("department"):
        where.append("p.department = %s")
        args.append(params["department"])
    if params.get("overdue") == "yes":
        where.append("s.due_date IS NOT NULL AND s.due_date < CURDATE()")
    sql = """
        SELECT a.asset_tag, a.asset_type, a.make, a.model, a.status,
               p.employee_id AS holder_id, p.name AS holder, p.department,
               p.employment_status, s.assigned_date, s.due_date, s.times_extended,
               DATEDIFF(CURDATE(), s.assigned_date) AS days_held
        FROM assignments s
        JOIN assets a ON a.id = s.asset_id
        JOIN people p ON p.id = s.person_id
        WHERE """ + " AND ".join(where)
    return sql + " ORDER BY s.due_date IS NULL, s.due_date, p.name", args


def _ownership(params):
    where, args = [], []
    if params.get("tag"):
        where.append("a.asset_tag LIKE %s")
        args.append(f"%{params['tag']}%")
    if params.get("employee"):
        where.append("(p.employee_id LIKE %s OR p.name LIKE %s)")
        args += [f"%{params['employee']}%"] * 2
    sql = """
        SELECT a.asset_tag, a.asset_type, a.model,
               p.employee_id AS holder_id, p.name AS holder, p.department,
               s.assigned_date, s.due_date, s.returned_date, s.times_extended,
               CASE WHEN s.returned_date IS NULL THEN 'Current' ELSE 'Past' END AS tenure,
               COALESCE(DATEDIFF(s.returned_date, s.assigned_date),
                        DATEDIFF(CURDATE(), s.assigned_date)) AS days_held
        FROM assignments s
        JOIN assets a ON a.id = s.asset_id
        JOIN people p ON p.id = s.person_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    return sql + " ORDER BY a.asset_tag, s.assigned_date DESC", args


def _lifecycle(params):
    where, args = ["e.event_type IN ('Registered','Status changed','Disposed','Deleted')"], []
    if params.get("tag"):
        where.append("e.asset_tag LIKE %s")
        args.append(f"%{params['tag']}%")
    sql = """
        SELECT e.created_at, e.asset_tag, e.event_type, e.from_value, e.to_value,
               e.note, u.full_name AS by_whom, a.asset_type, a.model
        FROM asset_events e
        LEFT JOIN users u ON u.id = e.user_id
        LEFT JOIN assets a ON a.id = e.asset_id
        WHERE """ + " AND ".join(where)
    return sql + " ORDER BY e.created_at DESC LIMIT 1000", args


def _disposals(params):
    sql = """
        SELECT a.asset_tag, a.asset_type, a.make, a.model, a.serial_number,
               a.purchase_date, a.purchase_cost, a.disposal_date,
               a.disposal_method, a.disposal_value, a.disposal_note,
               DATEDIFF(a.disposal_date, a.purchase_date) AS days_in_service
        FROM assets a
        WHERE a.status = 'Retired'
        ORDER BY a.disposal_date DESC, a.asset_tag
    """
    return sql, []


def _audit(params):
    where, args = [], []
    if params.get("event"):
        where.append("e.event_type = %s")
        args.append(params["event"])
    if params.get("tag"):
        where.append("e.asset_tag LIKE %s")
        args.append(f"%{params['tag']}%")
    sql = """
        SELECT e.created_at, e.asset_tag, e.event_type, e.from_value, e.to_value,
               e.note, p.name AS person, p.employee_id, u.full_name AS by_whom
        FROM asset_events e
        LEFT JOIN people p ON p.id = e.person_id
        LEFT JOIN users u ON u.id = e.user_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    return sql + " ORDER BY e.created_at DESC LIMIT 1000", args


def _by_status(params):
    sql = """
        SELECT a.asset_type, a.status, COUNT(*) AS items,
               SUM(a.purchase_cost) AS total_cost,
               SUM(a.warranty_expiry IS NOT NULL AND a.warranty_expiry < CURDATE()) AS out_of_warranty
        FROM assets a
        GROUP BY a.asset_type, a.status
        ORDER BY a.asset_type, FIELD(a.status,'In Use','In Storage','Under Repair','Retired')
    """
    return sql, []


def _by_department(params):
    sql = """
        SELECT COALESCE(p.department, '(unassigned)') AS department,
               COUNT(DISTINCT a.id) AS items,
               COUNT(DISTINCT p.id) AS holders,
               SUM(a.purchase_cost) AS total_cost,
               SUM(s.due_date IS NOT NULL AND s.due_date < CURDATE()) AS overdue
        FROM assets a
        LEFT JOIN assignments s ON s.asset_id = a.id AND s.returned_date IS NULL
        LEFT JOIN people p ON p.id = s.person_id
        WHERE a.status <> 'Retired'
        GROUP BY COALESCE(p.department, '(unassigned)')
        ORDER BY items DESC
    """
    return sql, []


REPORTS = {
    "inventory": {
        "title": "Full inventory",
        "blurb": "Every asset with its current holder, condition and warranty. "
                 "The one to hand an auditor.",
        "group": "Audit",
        "columns": INVENTORY_COLUMNS,
        "build": _inventory,
        "filters": ["status", "type", "department"],
    },
    "allocation": {
        "title": "Current allocations",
        "blurb": "Who holds what right now, how long they've had it, and whether "
                 "it's past its return date.",
        "group": "Allocation",
        "columns": [
            ("asset_tag", "Tag", "tag"),
            ("asset_type", "Type", "text"),
            ("model", "Model", "text"),
            ("holder_id", "Employee ID", "person"),
            ("holder", "Held by", "text"),
            ("department", "Department", "text"),
            ("employment_status", "Employment", "employment"),
            ("assigned_date", "Since", "date"),
            ("days_held", "Days held", "int"),
            ("due_date", "Due back", "due"),
            ("times_extended", "Extended", "int"),
        ],
        "build": _allocation,
        "filters": ["department", "overdue"],
    },
    "ownership": {
        "title": "Ownership history",
        "blurb": "Every allocation ever made, current and past. Answers "
                 "\"who had this in March?\" and \"what has this person ever held?\"",
        "group": "Allocation",
        "columns": [
            ("asset_tag", "Tag", "tag"),
            ("asset_type", "Type", "text"),
            ("model", "Model", "text"),
            ("holder_id", "Employee ID", "person"),
            ("holder", "Held by", "text"),
            ("department", "Department", "text"),
            ("tenure", "Tenure", "tenure"),
            ("assigned_date", "From", "date"),
            ("returned_date", "To", "date"),
            ("days_held", "Days held", "int"),
            ("times_extended", "Extended", "int"),
        ],
        "build": _ownership,
        "filters": ["tag", "employee"],
    },
    "warranty": {
        "title": "Warranty expiry",
        "blurb": "Cover running out inside the chosen window, or already gone. "
                 "Retired assets are left out.",
        "group": "Warranty",
        "columns": [
            ("asset_tag", "Tag", "tag"),
            ("asset_type", "Type", "text"),
            ("make", "Make", "text"),
            ("model", "Model", "text"),
            ("serial_number", "Serial", "text"),
            ("warranty_expiry", "Expires", "warranty"),
            ("warranty_provider", "Provider", "text"),
            ("warranty_ref", "Reference", "text"),
            ("purchase_cost", "Cost", "money"),
            ("holder", "Held by", "text"),
            ("status", "Status", "status"),
        ],
        "build": _warranty,
        "filters": ["window"],
    },
    "lifecycle": {
        "title": "Lifecycle movements",
        "blurb": "Registrations, status changes and disposals in date order — "
                 "the story of each asset's working life.",
        "group": "Lifecycle",
        "columns": [
            ("created_at", "When", "datetime"),
            ("asset_tag", "Tag", "tag"),
            ("asset_type", "Type", "text"),
            ("event_type", "Event", "event"),
            ("from_value", "From", "text"),
            ("to_value", "To", "text"),
            ("note", "Reason", "text"),
            ("by_whom", "By", "text"),
        ],
        "build": _lifecycle,
        "filters": ["tag"],
        "limit": 1000,
    },
    "disposals": {
        "title": "Disposals",
        "blurb": "Retired assets with how they went, what they fetched, and how "
                 "long they lasted in service.",
        "group": "Lifecycle",
        "columns": [
            ("asset_tag", "Tag", "tag"),
            ("asset_type", "Type", "text"),
            ("make", "Make", "text"),
            ("model", "Model", "text"),
            ("serial_number", "Serial", "text"),
            ("purchase_date", "Purchased", "date"),
            ("purchase_cost", "Cost", "money"),
            ("disposal_date", "Disposed", "date"),
            ("disposal_method", "Method", "text"),
            ("disposal_value", "Recovered", "money"),
            ("days_in_service", "Days in service", "int"),
            ("disposal_note", "Note", "text"),
        ],
        "build": _disposals,
        "filters": [],
    },
    "by-status": {
        "title": "Holdings by type and status",
        "blurb": "Counts and value per asset type, split by lifecycle status. "
                 "The summary table for the front of a report.",
        "group": "Audit",
        "columns": [
            ("asset_type", "Type", "text"),
            ("status", "Status", "status"),
            ("items", "Items", "int"),
            ("total_cost", "Total cost", "money"),
            ("out_of_warranty", "Out of warranty", "int"),
        ],
        "build": _by_status,
        "filters": [],
        "bars": ["items"],
    },
    "by-department": {
        "title": "Holdings by department",
        "blurb": "Where the equipment actually sits, by the department of "
                 "whoever holds it.",
        "group": "Audit",
        "columns": [
            ("department", "Department", "text"),
            ("items", "Items", "int"),
            ("holders", "Holders", "int"),
            ("total_cost", "Total cost", "money"),
            ("overdue", "Overdue", "int"),
        ],
        "build": _by_department,
        "filters": [],
        "bars": ["items"],
    },
    "audit": {
        "title": "Audit trail",
        "blurb": "Everything that has happened to every asset, newest first, "
                 "with who did it.",
        "group": "Audit",
        "columns": [
            ("created_at", "When", "datetime"),
            ("asset_tag", "Tag", "tag"),
            ("event_type", "Event", "event"),
            ("from_value", "From", "text"),
            ("to_value", "To", "text"),
            ("person", "Person", "text"),
            ("employee_id", "Employee ID", "person"),
            ("note", "Note", "text"),
            ("by_whom", "By", "text"),
        ],
        "build": _audit,
        "filters": ["event", "tag"],
        "limit": 1000,
    },
}

GROUPS = ["Audit", "Allocation", "Lifecycle", "Warranty"]

GROUP_BLURBS = {
    "Audit": "What the register holds and what has happened to it.",
    "Allocation": "Who has what, and who had it before.",
    "Lifecycle": "How assets arrive, change state and leave.",
    "Warranty": "Cover that is running out or already gone.",
}

# Footer and summary aggregation. Money and counts add up; durations don't,
# so those are averaged instead.
AVERAGED = {"days_held", "days_in_service"}

FILTER_LABELS = {
    "status": "Status", "type": "Type", "department": "Department",
    "window": "Window", "overdue": "Overdue only", "tag": "Tag",
    "employee": "Employee", "event": "Event",
}

WINDOWS = [("30", "Next 30 days"), ("60", "Next 60 days"), ("90", "Next 90 days"),
           ("180", "Next 6 months"), ("365", "Next year"), ("expired", "Already expired")]
