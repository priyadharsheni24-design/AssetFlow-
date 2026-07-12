"""
AssetFlow — Enterprise Asset & Resource Management System
Flask backend

What this file does
--------------------
1. Serves the login page and the booking dashboard (static HTML/CSS/JS
   files that were designed for the hackathon POC).
2. Authenticates users against data/employees.csv (Username + Password,
   Status must be "Active"). No self-elevation of roles: whatever Role
   is stored in the CSV (Employee / Manager / HR / Asset Manager) is
   what the session gets — matching the problem statement's "realistic
   account creation, not self-assigned admin roles" requirement.
3. Reads data/products.csv (the asset register) and groups the
   individual serialised units (e.g. AF-0001-01 .. AF-0001-08) into the
   asset "cards" the dashboard expects, with a live count of how many
   units are Available / Allocated / Under Maintenance.
4. Exposes small JSON APIs the dashboard's existing JavaScript calls
   instead of using its old hard-coded arrays.

Run it with:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000/login
"""

import csv
import itertools
import os
import secrets
from datetime import datetime
from functools import wraps

from flask import Flask, jsonify, redirect, request, send_from_directory, session

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATIC_DIR = os.path.join(BASE_DIR, "static")

EMPLOYEES_CSV = os.path.join(DATA_DIR, "employees.csv")
PRODUCTS_CSV = os.path.join(DATA_DIR, "products.csv")

app = Flask(__name__, static_folder=STATIC_DIR)
app.secret_key = os.environ.get("ASSETFLOW_SECRET_KEY", secrets.token_hex(16))


# ---------------------------------------------------------------------
# Category mapping — the dashboard UI groups everything into 4 boxes:
# laptops, headsets, meetinghall, vehicles. The CSV only tags things as
# Electronics / Meeting Room / Vehicle, so headsets are told apart from
# laptops by keyword.
# ---------------------------------------------------------------------
HEADSET_KEYWORDS = ("headset", "buds", "airdopes", "airpods", "earbud")


def classify_category(name: str, csv_category: str) -> str:
    csv_category = (csv_category or "").strip().lower()
    name_l = (name or "").strip().lower()
    if csv_category == "meeting room":
        return "meetinghall"
    if csv_category == "vehicle":
        return "vehicles"
    if any(k in name_l for k in HEADSET_KEYWORDS):
        return "headsets"
    return "laptops"


def asset_base_tag(asset_tag: str) -> str:
    """AF-0001-01 -> AF-0001 (a physical unit of a shared asset model).
    AF-0014 -> AF-0014 (a one-off asset like a meeting room or vehicle)."""
    parts = asset_tag.split("-")
    if len(parts) == 3:
        return f"{parts[0]}-{parts[1]}"
    return asset_tag


def unit_status_label(status: str) -> str:
    """Normalises the CSV's status text to what the dashboard's unit
    modal expects: Available / Allocated / Under Maintenance."""
    status = (status or "").strip()
    if status in ("Available", "Under Maintenance"):
        return status
    if status in ("Allocated", "Reserved", "Lost", "Retired", "Disposed"):
        return status if status == "Allocated" else "Allocated"
    return status or "Available"


def load_employees():
    employees = []
    with open(EMPLOYEES_CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            employees.append({k.strip(): (v or "").strip() for k, v in row.items()})
    return employees


def load_assets():
    """Reads the flat, one-row-per-unit product CSV and groups it into
    the asset-card shape the dashboard's JS (ASSETS / UNITS) uses."""
    groups = {}   # base_tag -> asset card dict
    units = {}    # base_tag -> list of unit dicts

    with open(PRODUCTS_CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row = {k.strip(): (v or "").strip() for k, v in row.items()}
            tag = row["asset_tag"]
            base = asset_base_tag(tag)
            category = classify_category(row["name"], row["category"])
            is_bookable = row.get("is_bookable", "").upper() == "TRUE"
            unit_status = unit_status_label(row["status"])

            if base not in groups:
                groups[base] = {
                    "id": base,
                    "model": row["name"],
                    "spec": f'{row["location"]} · {row["condition_notes"]} condition',
                    "category": category,
                    "quantity": 0,
                    "available": 0,
                    "isBookable": is_bookable,
                    "_maintenance_count": 0,
                    "_allocated_count": 0,
                }
                units[base] = []

            card = groups[base]
            card["quantity"] += 1
            if unit_status == "Available":
                card["available"] += 1
            elif unit_status == "Under Maintenance":
                card["_maintenance_count"] += 1
            else:
                card["_allocated_count"] += 1
            # a group is only bookable if at least one physical unit is
            card["isBookable"] = card["isBookable"] or is_bookable

            units[base].append({
                "serial": row["serial_number"] or tag,
                "status": unit_status,
                "condition": row["condition_notes"] or "Good",
            })

    # Resolve each card's overall status the same way the original POC
    # data did: fully available -> "available", nothing free because of
    # maintenance -> "maintenance", nothing free otherwise -> "allocated".
    assets = []
    for card in groups.values():
        if card["available"] > 0:
            card["status"] = "available"
        elif card["_maintenance_count"] > 0 and card["_maintenance_count"] >= card["_allocated_count"]:
            card["status"] = "maintenance"
        else:
            card["status"] = "allocated"
        del card["_maintenance_count"]
        del card["_allocated_count"]
        assets.append(card)

    assets.sort(key=lambda a: a["id"])
    return assets, units


# ---------------------------------------------------------------------
# Booking approval workflow
# ---------------------------------------------------------------------
# Roles that can see and act on pending booking requests. Per the
# problem statement, transfers/maintenance are approved by Asset
# Manager/Department Head; here HR and Manager are also treated as
# approvers for shared-resource bookings, since that's the workflow
# that was asked for.
APPROVER_ROLES = {"HR", "Manager", "Asset Manager"}

# In-memory demo storage. This resets when the server restarts — fine
# for a hackathon/demo scope, but swap for a real database if this
# needs to survive restarts.
BOOKINGS = []                       # list of booking dicts, newest first
_booking_id_counter = itertools.count(1)
NOTIFICATIONS = {}                  # employee_id -> list of notification dicts, newest first


def _timestamp():
    return datetime.now().isoformat(timespec="seconds")


def add_notification(employee_id, text):
    NOTIFICATIONS.setdefault(employee_id, []).insert(0, {
        "text": text,
        "time": datetime.now().strftime("%I:%M %p").lstrip("0"),
        "read": False,
    })


def find_booking(booking_id):
    for b in BOOKINGS:
        if b["id"] == booking_id:
            return b
    return None


# ---------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------
def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if "employee_id" not in session:
            return jsonify({"error": "Not authenticated"}), 401
        return view_func(*args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------
# Page routes (serve the existing static HTML/CSS/JS as-is)
# ---------------------------------------------------------------------
@app.route("/")
def root():
    if "employee_id" in session:
        return redirect("/dashboard")
    return redirect("/login")


@app.route("/login")
def login_page():
    return send_from_directory(STATIC_DIR, "login.html")


@app.route("/dashboard")
def dashboard_page():
    # The dashboard's own JS calls /api/me on load and bounces back to
    # /login client-side if there's no session, so this route just
    # serves the file; it doesn't need to gate on the server side too.
    return send_from_directory(STATIC_DIR, "dashboard.html")


# ---------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------
@app.route("/api/login", methods=["POST"])
def api_login():
    payload = request.get_json(silent=True) or {}
    identifier = (payload.get("username") or "").strip().lower()
    password = payload.get("password") or ""

    if not identifier or not password:
        return jsonify({"success": False, "message": "Please fill in all fields."}), 400

    for emp in load_employees():
        matches_id = (
            emp.get("Username", "").lower() == identifier
            or emp.get("Employee ID", "").lower() == identifier
            or emp.get("Email", "").lower() == identifier
        )
        if matches_id:
            if emp.get("Status") != "Active":
                return jsonify({"success": False, "message": "This account is inactive. Contact your Admin."}), 403
            if emp.get("Password") != password:
                return jsonify({"success": False, "message": "Incorrect username or password."}), 401

            session["employee_id"] = emp["Employee ID"]
            session["name"] = emp["Employee Name"]
            session["role"] = emp["Role"]
            session["department"] = emp["Department"]
            return jsonify({
                "success": True,
                "employeeId": emp["Employee ID"],
                "name": emp["Employee Name"],
                "role": emp["Role"],
                "department": emp["Department"],
            })

    return jsonify({"success": False, "message": "Incorrect username or password."}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"success": True})


@app.route("/api/me")
@login_required
def api_me():
    return jsonify({
        "employeeId": session["employee_id"],
        "name": session["name"],
        "role": session["role"],
        "department": session["department"],
        "isApprover": session["role"] in APPROVER_ROLES,
    })


@app.route("/api/assets")
@login_required
def api_assets():
    assets, units = load_assets()
    return jsonify({"assets": assets, "units": units})


@app.route("/api/categories")
@login_required
def api_categories():
    categories = [
        {"id": "laptops", "name": "Laptops", "icon": "laptop"},
        {"id": "headsets", "name": "Headsets & Earbuds", "icon": "headset"},
        {"id": "meetinghall", "name": "Meeting Halls", "icon": "building"},
        {"id": "vehicles", "name": "Vehicles", "icon": "taxi"},
    ]
    return jsonify(categories)


# ---------------------------------------------------------------------
# Booking approval workflow API
#
# Flow: an Employee books an asset -> POST /api/bookings creates it as
# "Pending". An approver (HR / Manager / Asset Manager) opens the
# Approvals page, which calls GET /api/bookings/pending, and approves
# or rejects it. That flips the booking's status and drops a
# notification for the requesting employee, whose browser is polling
# GET /api/bookings/mine and updates the asset card + fires a
# notification/toast as soon as it sees the change — before that, the
# employee only ever sees "Pending".
# ---------------------------------------------------------------------
@app.route("/api/bookings", methods=["POST"])
@login_required
def api_create_booking():
    payload = request.get_json(silent=True) or {}
    asset_id = (payload.get("assetId") or "").strip()
    date = (payload.get("date") or "").strip()
    start = (payload.get("start") or "").strip()
    end = (payload.get("end") or "").strip()
    purpose = (payload.get("purpose") or "").strip()

    if not asset_id or not date or not start or not end:
        return jsonify({"error": "Missing required booking fields."}), 400

    booking = {
        "id": next(_booking_id_counter),
        "assetId": asset_id,
        "employeeId": session["employee_id"],
        "employeeName": session["name"],
        "department": session["department"],
        "date": date,
        "start": start,
        "end": end,
        "purpose": purpose,
        "status": "Pending",
        "requestedAt": _timestamp(),
        "decidedBy": None,
        "decidedAt": None,
    }
    BOOKINGS.insert(0, booking)
    return jsonify(booking), 201


@app.route("/api/bookings/mine")
@login_required
def api_my_bookings():
    mine = [b for b in BOOKINGS if b["employeeId"] == session["employee_id"]]
    return jsonify(mine)


@app.route("/api/bookings/pending")
@login_required
def api_pending_bookings():
    if session["role"] not in APPROVER_ROLES:
        return jsonify({"error": "Not authorized."}), 403
    pending = [b for b in BOOKINGS if b["status"] == "Pending"]
    return jsonify(pending)


@app.route("/api/bookings/<int:booking_id>/approve", methods=["POST"])
@login_required
def api_approve_booking(booking_id):
    if session["role"] not in APPROVER_ROLES:
        return jsonify({"error": "Not authorized."}), 403
    booking = find_booking(booking_id)
    if not booking:
        return jsonify({"error": "Booking not found."}), 404
    if booking["status"] != "Pending":
        return jsonify({"error": "This booking has already been decided."}), 409

    booking["status"] = "Approved"
    booking["decidedBy"] = session["name"]
    booking["decidedAt"] = _timestamp()

    add_notification(
        booking["employeeId"],
        "✅ {} ({}) approved your booking for {}, {} {}\u2013{}.".format(
            session["name"], session["role"], booking["assetId"],
            booking["date"], booking["start"], booking["end"],
        ),
    )
    return jsonify(booking)


@app.route("/api/bookings/<int:booking_id>/reject", methods=["POST"])
@login_required
def api_reject_booking(booking_id):
    if session["role"] not in APPROVER_ROLES:
        return jsonify({"error": "Not authorized."}), 403
    booking = find_booking(booking_id)
    if not booking:
        return jsonify({"error": "Booking not found."}), 404
    if booking["status"] != "Pending":
        return jsonify({"error": "This booking has already been decided."}), 409

    payload = request.get_json(silent=True) or {}
    reason = (payload.get("reason") or "").strip()

    booking["status"] = "Rejected"
    booking["decidedBy"] = session["name"]
    booking["decidedAt"] = _timestamp()
    booking["reason"] = reason

    msg = "❌ {} ({}) rejected your booking for {}.".format(
        session["name"], session["role"], booking["assetId"]
    )
    if reason:
        msg += " Reason: " + reason
    add_notification(booking["employeeId"], msg)
    return jsonify(booking)


@app.route("/api/bookings/<int:booking_id>/cancel", methods=["POST"])
@login_required
def api_cancel_booking(booking_id):
    booking = find_booking(booking_id)
    if not booking:
        return jsonify({"error": "Booking not found."}), 404
    if booking["employeeId"] != session["employee_id"]:
        return jsonify({"error": "Not authorized."}), 403
    if booking["status"] != "Pending":
        return jsonify({"error": "Only a pending booking can be cancelled."}), 409

    booking["status"] = "Cancelled"
    booking["decidedAt"] = _timestamp()
    return jsonify(booking)


@app.route("/api/notifications")
@login_required
def api_notifications():
    return jsonify(NOTIFICATIONS.get(session["employee_id"], []))


if __name__ == "__main__":
    app.run(debug=True, port=5000)
