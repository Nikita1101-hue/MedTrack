"""
MedTrack - Smart Healthcare Management, Connected Through the Cloud.

Stack: HTML + CSS (Jinja2 templates) | Python Flask | Amazon DynamoDB (boto3)
       | AWS EC2 (hosting) | AWS IAM (permissions)

Run locally:   python app.py
Create tables: python app.py init-db        (DynamoDB only)
"""
import json
import logging
import os
import random
import re
import sys
import threading
from datetime import date, datetime, timezone
from functools import wraps
from secrets import compare_digest, token_hex

from dotenv import load_dotenv
from flask import (Flask, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

# --------------------------------------------------------------------------
# 1. CONFIGURATION  (development mode / production mode)
# --------------------------------------------------------------------------
APP_MODE = os.getenv("APP_MODE", "development").strip().lower()
IS_PROD = APP_MODE == "production"
# Production always uses DynamoDB. Development uses a local JSON file unless
# USE_DYNAMODB=true is set.
USE_DYNAMODB = IS_PROD or os.getenv("USE_DYNAMODB", "false").lower() == "true"
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_DB_FILE = os.path.join(BASE_DIR, "data", "medtrack_local.json")

app = Flask(__name__)

secret = os.getenv("SECRET_KEY", "")
if not secret:
    if IS_PROD:
        raise RuntimeError("SECRET_KEY must be set when APP_MODE=production")
    secret = "dev-only-secret-key-change-me"
app.config.update(
    SECRET_KEY=secret,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Only enable when the site is served over HTTPS, otherwise login breaks.
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=2 * 60 * 60,  # 2 hours
    MAX_CONTENT_LENGTH=1024 * 1024,
)
logging.basicConfig(level=logging.INFO)

# --------------------------------------------------------------------------
# 2. DATABASE LAYER  (same small interface for DynamoDB and the local file)
# --------------------------------------------------------------------------
# logical table -> (DynamoDB table name, partition key)
TABLES = {
    "users": ("MedTrack_Users", "user_id"),
    "patients": ("MedTrack_Patients", "patient_id"),
    "doctors": ("MedTrack_Doctors", "doctor_id"),
    "appointments": ("MedTrack_Appointments", "appointment_id"),
    "diagnosis": ("MedTrack_Diagnosis", "diagnosis_id"),
    "notifications": ("MedTrack_Notifications", "notification_id"),
}


class LocalStore:
    """Development only: keeps every table in one JSON file."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.data = {t: {} for t in TABLES}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self.data.update(json.load(f))
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)

    def put(self, table, item):
        with self.lock:
            self.data[table][item[TABLES[table][1]]] = dict(item)
            self._save()

    def get(self, table, key):
        with self.lock:
            item = self.data[table].get(key)
            return dict(item) if item else None

    def delete(self, table, key):
        with self.lock:
            self.data[table].pop(key, None)
            self._save()

    def scan(self, table, **filters):
        with self.lock:
            return [dict(i) for i in self.data[table].values()
                    if all(i.get(k) == v for k, v in filters.items())]

    def update(self, table, key, **changes):
        with self.lock:
            self.data[table][key].update(changes)
            self._save()


class DynamoStore:
    """Production: Amazon DynamoDB through boto3.

    Credentials are NOT in the code. boto3 finds them automatically from the
    IAM role attached to the EC2 instance (or from environment variables).
    """

    def __init__(self, region):
        import boto3  # imported here so local development works without AWS
        self.db = boto3.resource("dynamodb", region_name=region)
        self.tables = {t: self.db.Table(name) for t, (name, _) in TABLES.items()}

    def put(self, table, item):
        self.tables[table].put_item(Item=item)

    def get(self, table, key):
        resp = self.tables[table].get_item(Key={TABLES[table][1]: key})
        return resp.get("Item")

    def delete(self, table, key):
        self.tables[table].delete_item(Key={TABLES[table][1]: key})

    def scan(self, table, **filters):
        from boto3.dynamodb.conditions import Attr
        kwargs = {}
        cond = None
        for k, v in filters.items():
            c = Attr(k).eq(v)
            cond = c if cond is None else cond & c
        if cond is not None:
            kwargs["FilterExpression"] = cond
        items = []
        while True:  # scan returns max 1 MB per call, so follow the pages
            resp = self.tables[table].scan(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                return items
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def update(self, table, key, **changes):
        names = {f"#a{i}": k for i, k in enumerate(changes)}
        values = {f":v{i}": v for i, v in enumerate(changes.values())}
        expr = "SET " + ", ".join(f"#a{i} = :v{i}" for i in range(len(changes)))
        self.tables[table].update_item(
            Key={TABLES[table][1]: key},
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )


def create_tables():
    """Create the six DynamoDB tables (run once: python app.py init-db)."""
    import boto3
    db = boto3.resource("dynamodb", region_name=AWS_REGION)
    existing = {t.name for t in db.tables.all()}
    for name, key in TABLES.values():
        if name in existing:
            print(f"Table {name} already exists")
            continue
        print(f"Creating table {name} ...")
        table = db.create_table(
            TableName=name,
            KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
    print("All tables are ready.")


store = DynamoStore(AWS_REGION) if USE_DYNAMODB else LocalStore(LOCAL_DB_FILE)

# --------------------------------------------------------------------------
# 3. HELPERS
# --------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[0-9]{10,15}$")
TIME_SLOTS = [datetime(2000, 1, 1, h, m).strftime("%I:%M %p")
              for h in range(9, 17) for m in (0, 30)]
GENDERS = ["Male", "Female", "Other"]
ID_PREFIX = {"users": "U", "patients": "P", "doctors": "D",
             "appointments": "A", "diagnosis": "G", "notifications": "N"}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(table):
    """Unique readable ID such as A10025 (prefix + 5 digits)."""
    for _ in range(20):
        candidate = ID_PREFIX[table] + str(random.randint(10000, 99999))
        if not store.get(table, candidate):
            return candidate
    raise RuntimeError("Could not generate a unique ID")


def clean(value, limit=200):
    return (value or "").strip()[:limit]


def strip_dr(name):
    return re.sub(r"^dr\.?\s+", "", name, flags=re.I).strip()


def pretty_date(value):
    try:
        d = datetime.strptime(value, "%Y-%m-%d")
        return f"{d.day} {d.strftime('%B %Y')}"
    except (TypeError, ValueError):
        return value or ""


def pretty_datetime(value):
    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y, %H:%M UTC")
    except (TypeError, ValueError):
        return value or ""


app.jinja_env.filters["pdate"] = pretty_date
app.jinja_env.filters["pdt"] = pretty_datetime
app.jinja_env.filters["dr"] = lambda n: "Dr. " + strip_dr(n or "")


def appt_key(a):
    try:
        t = datetime.strptime(a["time"], "%I:%M %p")
    except ValueError:
        t = datetime(2000, 1, 1)
    return (a["date"], t)


def newest_first(items):
    return sorted(items, key=lambda i: i.get("created_at", ""), reverse=True)


def notify(user_id, title, message, ntype="info"):
    store.put("notifications", {
        "notification_id": new_id("notifications"), "user_id": user_id,
        "title": title, "message": message, "type": ntype,
        "is_read": False, "created_at": now_iso(),
    })


def user_id_of_patient(patient_id):
    p = store.get("patients", patient_id)
    return p["user_id"] if p else None


def user_id_of_doctor(doctor_id):
    d = store.get("doctors", doctor_id)
    return d["user_id"] if d else None


def validate_account(name, email, phone, password, confirm=None):
    """Returns an error message, or None when everything is valid."""
    if not name or not email or not phone or not password:
        return "Please fill in all required fields."
    if not EMAIL_RE.match(email):
        return "Please enter a valid email address."
    if not PHONE_RE.match(re.sub(r"[\s-]", "", phone)):
        return "Phone number must contain 10 to 15 digits."
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if confirm is not None and password != confirm:
        return "Password and confirm password do not match."
    if store.scan("users", email=email):
        return "An account with this email already exists."
    return None


def create_account(name, email, phone, password, role, **profile):
    """Creates the Users row and the matching Patients / Doctors row."""
    user = {"user_id": new_id("users"), "name": name, "email": email,
            "phone": phone, "password_hash": generate_password_hash(password),
            "role": role, "created_at": now_iso()}
    store.put("users", user)
    if role == "patient":
        row = {"patient_id": new_id("patients"), "user_id": user["user_id"],
               "name": name, "email": email, "phone": phone, "age": "",
               "gender": "", "address": "", "created_at": user["created_at"]}
        store.put("patients", row)
    elif role == "doctor":
        row = {"doctor_id": new_id("doctors"), "user_id": user["user_id"],
               "name": strip_dr(name), "email": email, "phone": phone,
               "specialization": profile.get("specialization") or "General Physician",
               "qualification": profile.get("qualification") or "MBBS",
               "experience": profile.get("experience") or "0",
               "availability": profile.get("availability") or "Monday - Friday",
               "hospital": profile.get("hospital") or "MedTrack Clinic"}
        store.put("doctors", row)
    else:
        row = None
    return user, row


def bootstrap():
    """Create the first admin (and demo doctors in development mode)."""
    if not store.scan("users", role="admin"):
        email = os.getenv("ADMIN_EMAIL", "" if IS_PROD else "admin@medtrack.com").lower()
        pwd = os.getenv("ADMIN_PASSWORD", "" if IS_PROD else "Admin@123")
        if email and pwd:
            create_account("MedTrack Admin", email, "0000000000", pwd, "admin")
            app.logger.info("Admin account created: %s", email)
        else:
            app.logger.warning("No admin exists. Set ADMIN_EMAIL and ADMIN_PASSWORD.")
    if not IS_PROD and not store.scan("doctors"):
        demo = [
            ("Ananya Sharma", "ananya@medtrack.com", "Cardiologist", "MBBS, MD (Cardiology)", "8", "Monday - Friday", "MedTrack Heart Centre"),
            ("Rohan Mehta", "rohan@medtrack.com", "Pediatrician", "MBBS, DCH", "6", "Monday - Saturday", "MedTrack Children's Clinic"),
            ("Priya Nair", "priya@medtrack.com", "Dermatologist", "MBBS, MD (Dermatology)", "10", "Tuesday - Saturday", "MedTrack Skin Care"),
        ]
        for n, e, s, q, x, a, h in demo:
            create_account(n, e, "9876543210", "Doctor@123", "doctor",
                           specialization=s, qualification=q, experience=x,
                           availability=a, hospital=h)
        app.logger.info("Demo doctors created (password: Doctor@123)")


# --------------------------------------------------------------------------
# 4. SECURITY: CSRF, role protection, response headers
# --------------------------------------------------------------------------
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = token_hex(16)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def csrf_protect():
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        if not compare_digest(session.get("_csrf", ""), sent):
            flash("Your form expired. Please try again.", "error")
            return redirect(url_for("index"))


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    if "user_id" in session:
        resp.headers["Cache-Control"] = "no-store"
    return resp


def home_for(role):
    return url_for({"patient": "patient_dashboard", "doctor": "doctor_dashboard",
                    "admin": "admin_dashboard"}.get(role, "login"))


def login_required(role=None):
    """Protect a route. Optionally allow only one role."""
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            uid = session.get("user_id")
            user = store.get("users", uid) if uid else None
            if not user:
                session.clear()
                flash("Please log in to continue.", "error")
                return redirect(url_for("login"))
            if role and user["role"] != role:
                flash("You are not authorised to view that page.", "error")
                return redirect(home_for(user["role"]))
            g.user = user
            g.profile = None
            if user["role"] == "patient":
                g.profile = store.get("patients", session.get("profile_id"))
            elif user["role"] == "doctor":
                g.profile = store.get("doctors", session.get("profile_id"))
            if user["role"] != "admin" and not g.profile:
                session.clear()
                flash("Your profile could not be found. Please log in again.", "error")
                return redirect(url_for("login"))
            return view(*args, **kwargs)
        return wrapper
    return decorator


@app.context_processor
def inject_unread():
    def unread_count():
        if "user_id" not in session:
            return 0
        return len(store.scan("notifications", user_id=session["user_id"], is_read=False))
    return {"unread_count": unread_count}


# --------------------------------------------------------------------------
# 5. PUBLIC + AUTH ROUTES
# --------------------------------------------------------------------------
@app.route("/")
def index():
    doctors = sorted(store.scan("doctors"), key=lambda d: d["name"])[:3]
    return render_template("index.html", doctors=doctors)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form
        name, email = clean(f.get("name"), 100), clean(f.get("email"), 120).lower()
        phone, role = clean(f.get("phone"), 20), f.get("role", "")
        if role not in ("patient", "doctor"):  # admin can never self-register
            flash("Please choose Patient or Doctor.", "error")
        else:
            error = validate_account(name, email, phone, f.get("password", ""),
                                     f.get("confirm_password", ""))
            if error:
                flash(error, "error")
            else:
                create_account(name, email, phone, f["password"], role)
                flash("Registration successful. Please log in.", "success")
                return redirect(url_for("login"))
        return render_template("register.html", form=f)
    return render_template("register.html", form={})


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = clean(request.form.get("email"), 120).lower()
        password = request.form.get("password", "")
        users = store.scan("users", email=email) if email else []
        user = users[0] if users else None
        if not email or not password:
            flash("Please enter your email and password.", "error")
        elif not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid email or password.", "error")
        else:
            session.clear()  # new session after login
            session.permanent = True
            session["user_id"], session["role"] = user["user_id"], user["role"]
            session["name"] = user["name"]
            if user["role"] == "patient":
                p = store.scan("patients", user_id=user["user_id"])
                session["profile_id"] = p[0]["patient_id"] if p else None
            elif user["role"] == "doctor":
                d = store.scan("doctors", user_id=user["user_id"])
                session["profile_id"] = d[0]["doctor_id"] if d else None
            return redirect(home_for(user["role"]))
    return render_template("login.html", role=request.args.get("role", ""))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))


@app.route("/dashboard")
def dashboard():
    return redirect(home_for(session.get("role")))


@app.route("/notifications/read", methods=["POST"])
@login_required()
def mark_notifications_read():
    """Mark one (or all) of the current user's notifications as read."""
    nid = request.form.get("notification_id")
    for n in store.scan("notifications", user_id=g.user["user_id"], is_read=False):
        if not nid or n["notification_id"] == nid:
            store.update("notifications", n["notification_id"], is_read=True)
    target = {"patient": "patient_notifications", "doctor": "doctor_notifications",
              "admin": "admin_notifications"}[g.user["role"]]
    return redirect(url_for(target))


# --------------------------------------------------------------------------
# 6. PATIENT ROUTES
# --------------------------------------------------------------------------
@app.route("/patient/dashboard")
@login_required("patient")
def patient_dashboard():
    pid = g.profile["patient_id"]
    appts = store.scan("appointments", patient_id=pid)
    stats = {
        "total": len(appts),
        "upcoming": sum(a["status"] in ("Pending", "Confirmed") for a in appts),
        "completed": sum(a["status"] == "Completed" for a in appts),
        "diagnosis": len(store.scan("diagnosis", patient_id=pid)),
        "unread": len(store.scan("notifications", user_id=g.user["user_id"], is_read=False)),
    }
    upcoming = sorted([a for a in appts if a["status"] in ("Pending", "Confirmed")], key=appt_key)[:5]
    return render_template("patient_dashboard.html", stats=stats, upcoming=upcoming)


@app.route("/patient/profile", methods=["GET", "POST"])
@login_required("patient")
def patient_profile():
    if request.method == "POST":
        f = request.form
        name, phone = clean(f.get("name"), 100), clean(f.get("phone"), 20)
        age, gender = clean(f.get("age"), 3), f.get("gender", "")
        address = clean(f.get("address"), 300)
        if not name or not PHONE_RE.match(re.sub(r"[\s-]", "", phone)):
            flash("Name and a valid phone number are required.", "error")
        elif age and not (age.isdigit() and 0 < int(age) <= 120):
            flash("Age must be a number between 1 and 120.", "error")
        elif gender and gender not in GENDERS:
            flash("Please choose a valid gender.", "error")
        else:
            store.update("patients", g.profile["patient_id"], name=name, phone=phone,
                         age=age, gender=gender, address=address)
            store.update("users", g.user["user_id"], name=name, phone=phone)
            session["name"] = name
            flash("Profile updated.", "success")
        return redirect(url_for("patient_profile"))
    return render_template("patient_profile.html", p=g.profile, genders=GENDERS)


@app.route("/patient/doctors")
@login_required("patient")
def patient_doctors():
    q = clean(request.args.get("q"), 60).lower()
    doctors = sorted(store.scan("doctors"), key=lambda d: d["name"])
    if q:
        doctors = [d for d in doctors if q in d["name"].lower()
                   or q in d["specialization"].lower()]
    return render_template("doctors.html", doctors=doctors, q=q)


@app.route("/patient/doctors/<doctor_id>")
@login_required("patient")
def doctor_view(doctor_id):
    doctor = store.get("doctors", doctor_id)
    if not doctor:
        flash("Doctor not found.", "error")
        return redirect(url_for("patient_doctors"))
    return render_template("doctor_view.html", d=doctor)


@app.route("/patient/book-appointment", methods=["GET", "POST"])
@login_required("patient")
def book_appointment():
    doctors = sorted(store.scan("doctors"), key=lambda d: d["name"])
    form = {"doctor_id": request.args.get("doctor_id", "")}
    if request.method == "POST":
        form = request.form
        doctor = store.get("doctors", form.get("doctor_id", ""))
        day, slot, reason = form.get("date", ""), form.get("time", ""), clean(form.get("reason"), 500)
        error = None
        try:
            chosen = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            chosen = None
        if not doctor:
            error = "Please choose a valid doctor."
        elif not chosen or chosen < date.today():
            error = "Please choose today's date or a future date."
        elif slot not in TIME_SLOTS:
            error = "Please choose a valid time slot."
        elif not reason:
            error = "Please enter the reason for your visit."
        elif any(a["status"] in ("Pending", "Confirmed")
                 for a in store.scan("appointments", doctor_id=doctor["doctor_id"], date=day, time=slot)):
            error = "That time slot is already booked. Please choose another."
        if error:
            flash(error, "error")
        else:
            appt = {"appointment_id": new_id("appointments"),
                    "patient_id": g.profile["patient_id"], "doctor_id": doctor["doctor_id"],
                    "patient_name": g.profile["name"], "doctor_name": doctor["name"],
                    "date": day, "time": slot, "reason": reason,
                    "status": "Pending", "created_at": now_iso()}
            store.put("appointments", appt)
            when = f"{pretty_date(day)} at {slot}"
            notify(g.user["user_id"], "Appointment Booked",
                   f"Your appointment with Dr. {doctor['name']} on {when} was booked and is awaiting confirmation.",
                   "appointment")
            notify(doctor["user_id"], "New Appointment Request",
                   f"{g.profile['name']} requested an appointment on {when}.", "appointment")
            flash(f"Appointment {appt['appointment_id']} booked. Waiting for the doctor to confirm.", "success")
            return redirect(url_for("patient_appointments"))
    return render_template("book_appointment.html", doctors=doctors, slots=TIME_SLOTS,
                           form=form, today=date.today().isoformat())


@app.route("/patient/appointments")
@login_required("patient")
def patient_appointments():
    appts = sorted(store.scan("appointments", patient_id=g.profile["patient_id"]),
                   key=appt_key, reverse=True)
    return render_template("patient_appointments.html", appointments=appts)


@app.route("/patient/appointments/<appointment_id>/cancel", methods=["POST"])
@login_required("patient")
def cancel_appointment(appointment_id):
    a = store.get("appointments", appointment_id)
    if not a or a["patient_id"] != g.profile["patient_id"]:
        flash("Invalid appointment.", "error")
    elif a["status"] not in ("Pending", "Confirmed"):
        flash("This appointment can no longer be cancelled.", "error")
    else:
        store.update("appointments", appointment_id, status="Cancelled")
        when = f"{pretty_date(a['date'])} at {a['time']}"
        notify(g.user["user_id"], "Appointment Cancelled",
               f"You cancelled your appointment with Dr. {a['doctor_name']} on {when}.", "appointment")
        duid = user_id_of_doctor(a["doctor_id"])
        if duid:
            notify(duid, "Appointment Cancelled",
                   f"{a['patient_name']} cancelled the appointment on {when}.", "appointment")
        flash("Appointment cancelled.", "success")
    return redirect(url_for("patient_appointments"))


@app.route("/patient/diagnosis")
@login_required("patient")
def patient_diagnosis():
    records = newest_first(store.scan("diagnosis", patient_id=g.profile["patient_id"]))
    doctors = {d["doctor_id"]: d for d in store.scan("doctors")}
    appts = {a["appointment_id"]: a for a in store.scan("appointments", patient_id=g.profile["patient_id"])}
    return render_template("patient_diagnosis.html", records=records, doctors=doctors, appts=appts)


@app.route("/patient/notifications")
@login_required("patient")
def patient_notifications():
    items = newest_first(store.scan("notifications", user_id=g.user["user_id"]))
    return render_template("patient_notifications.html", notifications=items)


# --------------------------------------------------------------------------
# 7. DOCTOR ROUTES
# --------------------------------------------------------------------------
@app.route("/doctor/dashboard")
@login_required("doctor")
def doctor_dashboard():
    did = g.profile["doctor_id"]
    appts = store.scan("appointments", doctor_id=did)
    today = date.today().isoformat()
    stats = {
        "today": sum(a["date"] == today and a["status"] in ("Pending", "Confirmed") for a in appts),
        "pending": sum(a["status"] == "Pending" for a in appts),
        "completed": sum(a["status"] == "Completed" for a in appts),
        "patients": len({a["patient_id"] for a in appts}),
        "diagnosis": len(store.scan("diagnosis", doctor_id=did)),
    }
    todays = sorted([a for a in appts if a["date"] == today and a["status"] in ("Pending", "Confirmed")], key=appt_key)
    pending = sorted([a for a in appts if a["status"] == "Pending"], key=appt_key)[:5]
    return render_template("doctor_dashboard.html", stats=stats, todays=todays, pending=pending, today=today)


@app.route("/doctor/profile", methods=["GET", "POST"])
@login_required("doctor")
def doctor_profile():
    if request.method == "POST":
        f = request.form
        name, phone = strip_dr(clean(f.get("name"), 100)), clean(f.get("phone"), 20)
        spec, qual = clean(f.get("specialization"), 80), clean(f.get("qualification"), 120)
        exp, avail = clean(f.get("experience"), 2), clean(f.get("availability"), 100)
        hospital = clean(f.get("hospital"), 120)
        if not (name and spec and qual and avail and hospital):
            flash("Please fill in all required fields.", "error")
        elif not PHONE_RE.match(re.sub(r"[\s-]", "", phone)):
            flash("Please enter a valid phone number.", "error")
        elif not exp.isdigit() or int(exp) > 60:
            flash("Experience must be a number of years (0-60).", "error")
        else:
            store.update("doctors", g.profile["doctor_id"], name=name, phone=phone,
                         specialization=spec, qualification=qual, experience=exp,
                         availability=avail, hospital=hospital)
            store.update("users", g.user["user_id"], name=name, phone=phone)
            session["name"] = name
            flash("Profile updated.", "success")
        return redirect(url_for("doctor_profile"))
    return render_template("doctor_profile.html", d=g.profile)


@app.route("/doctor/appointments")
@login_required("doctor")
def doctor_appointments():
    status = request.args.get("status", "")
    appts = store.scan("appointments", doctor_id=g.profile["doctor_id"])
    if status:
        appts = [a for a in appts if a["status"] == status]
    appts.sort(key=appt_key, reverse=True)
    patients = {p["patient_id"]: p for p in store.scan("patients")}
    return render_template("doctor_appointments.html", appointments=appts, patients=patients,
                           status=status, statuses=["Pending", "Confirmed", "Rejected", "Completed", "Cancelled"])


# action -> (required current status, new status, notification title, wording)
DOCTOR_ACTIONS = {
    "confirm": ("Pending", "Confirmed", "Appointment Confirmed", "confirmed"),
    "reject": ("Pending", "Rejected", "Appointment Rejected", "rejected"),
    "cancel": ("Confirmed", "Cancelled", "Appointment Cancelled", "cancelled"),
}


@app.route("/doctor/appointments/<appointment_id>/<action>", methods=["POST"])
@login_required("doctor")
def doctor_appointment_action(appointment_id, action):
    a = store.get("appointments", appointment_id)
    rule = DOCTOR_ACTIONS.get(action)
    if not a or not rule or a["doctor_id"] != g.profile["doctor_id"]:
        flash("Invalid appointment.", "error")
    elif a["status"] != rule[0]:
        flash(f"Only {rule[0].lower()} appointments can be {rule[3]}.", "error")
    else:
        store.update("appointments", appointment_id, status=rule[1])
        puid = user_id_of_patient(a["patient_id"])
        if puid:
            notify(puid, rule[2],
                   f"Dr. {strip_dr(g.profile['name'])} has {rule[3]} your appointment for "
                   f"{pretty_date(a['date'])} at {a['time']}.", "appointment")
        flash(f"Appointment {rule[3]}.", "success")
    return redirect(request.form.get("next") == "dashboard" and url_for("doctor_dashboard")
                    or url_for("doctor_appointments"))


@app.route("/doctor/patients")
@login_required("doctor")
def doctor_patients():
    appts = store.scan("appointments", doctor_id=g.profile["doctor_id"])
    rows = {}
    for a in appts:
        r = rows.setdefault(a["patient_id"], {"count": 0, "last": ""})
        r["count"] += 1
        r["last"] = max(r["last"], a["date"])
    patients = []
    for pid, r in rows.items():
        p = store.get("patients", pid)
        if p:
            patients.append({**p, **r})
    patients.sort(key=lambda p: p["name"])
    return render_template("doctor_patients.html", patients=patients)


@app.route("/doctor/add-diagnosis", methods=["GET", "POST"])
@login_required("doctor")
def add_diagnosis():
    did = g.profile["doctor_id"]
    done = {d["appointment_id"] for d in store.scan("diagnosis", doctor_id=did)}
    eligible = sorted([a for a in store.scan("appointments", doctor_id=did)
                       if a["status"] == "Confirmed" and a["appointment_id"] not in done], key=appt_key)
    selected = request.values.get("appointment_id", "")
    if request.method == "POST":
        f = request.form
        diag, rx, notes = clean(f.get("diagnosis"), 1000), clean(f.get("prescription"), 1000), clean(f.get("medical_notes"), 2000)
        appt = store.get("appointments", selected)
        if not appt or appt["doctor_id"] != did:
            flash("Invalid appointment.", "error")
        elif appt["status"] != "Confirmed" or appt["appointment_id"] in done:
            flash("Only confirmed appointments without a diagnosis can be completed.", "error")
        elif not diag:
            flash("Diagnosis is required.", "error")
        else:
            store.put("diagnosis", {
                "diagnosis_id": new_id("diagnosis"), "patient_id": appt["patient_id"],
                "doctor_id": did, "appointment_id": appt["appointment_id"],
                "diagnosis": diag, "prescription": rx, "medical_notes": notes,
                "created_at": now_iso()})
            store.update("appointments", appt["appointment_id"], status="Completed")
            puid = user_id_of_patient(appt["patient_id"])
            if puid:
                doc = "Dr. " + strip_dr(g.profile["name"])
                notify(puid, "Diagnosis Added",
                       f"{doc} added a diagnosis for your visit on {pretty_date(appt['date'])}. "
                       "You can view it under Diagnosis.", "diagnosis")
                notify(puid, "Appointment Completed",
                       f"Your appointment with {doc} on {pretty_date(appt['date'])} is completed.", "appointment")
            flash("Diagnosis saved and appointment completed.", "success")
            return redirect(url_for("add_diagnosis"))
    chosen = next((a for a in eligible if a["appointment_id"] == selected), None)
    chosen_patient = store.get("patients", chosen["patient_id"]) if chosen else None
    records = newest_first(store.scan("diagnosis", doctor_id=did))
    patients = {p["patient_id"]: p for p in store.scan("patients")}
    appts = {a["appointment_id"]: a for a in store.scan("appointments", doctor_id=did)}
    return render_template("add_diagnosis.html", eligible=eligible, selected=selected,
                           chosen=chosen, chosen_patient=chosen_patient, records=records,
                           patients=patients, appts=appts)


@app.route("/doctor/notifications", methods=["GET", "POST"])
@login_required("doctor")
def doctor_notifications():
    did = g.profile["doctor_id"]
    my_patients = {a["patient_id"] for a in store.scan("appointments", doctor_id=did)}
    if request.method == "POST":
        pid, title, msg = request.form.get("patient_id", ""), clean(request.form.get("title"), 100), clean(request.form.get("message"), 500)
        puid = user_id_of_patient(pid) if pid in my_patients else None
        if not puid:
            flash("Please choose one of your patients.", "error")
        elif not title or not msg:
            flash("Title and message are required.", "error")
        else:
            notify(puid, title, f"From Dr. {strip_dr(g.profile['name'])}: {msg}", "message")
            flash("Notification sent.", "success")
        return redirect(url_for("doctor_notifications"))
    items = newest_first(store.scan("notifications", user_id=g.user["user_id"]))
    patients = sorted([p for p in (store.get("patients", i) for i in my_patients) if p], key=lambda p: p["name"])
    return render_template("doctor_notifications.html", notifications=items, patients=patients)


# --------------------------------------------------------------------------
# 8. ADMIN ROUTES
# --------------------------------------------------------------------------
@app.route("/admin/dashboard")
@login_required("admin")
def admin_dashboard():
    appts = store.scan("appointments")
    stats = {"patients": len(store.scan("patients")), "doctors": len(store.scan("doctors")),
             "appointments": len(appts),
             "completed": sum(a["status"] == "Completed" for a in appts),
             "pending": sum(a["status"] == "Pending" for a in appts)}
    recent = sorted(appts, key=lambda a: a["created_at"], reverse=True)[:6]
    return render_template("admin_dashboard.html", stats=stats, recent=recent)


def remove_user(user_id, profile_table, profile_id):
    store.delete(profile_table, profile_id)
    store.delete("users", user_id)
    for n in store.scan("notifications", user_id=user_id):
        store.delete("notifications", n["notification_id"])


@app.route("/admin/patients", methods=["GET", "POST"])
@login_required("admin")
def manage_patients():
    if request.method == "POST":
        p = store.get("patients", request.form.get("patient_id", ""))
        if not p:
            flash("Patient not found.", "error")
        else:
            for a in store.scan("appointments", patient_id=p["patient_id"]):
                if a["status"] in ("Pending", "Confirmed"):
                    store.update("appointments", a["appointment_id"], status="Cancelled")
            remove_user(p["user_id"], "patients", p["patient_id"])
            flash(f"Patient {p['name']} removed.", "success")
        return redirect(url_for("manage_patients"))
    patients = sorted(store.scan("patients"), key=lambda p: p["name"])
    return render_template("manage_patients.html", patients=patients)


@app.route("/admin/doctors", methods=["GET", "POST"])
@login_required("admin")
def manage_doctors():
    form = {}
    if request.method == "POST":
        f = request.form
        if f.get("action") == "delete":
            d = store.get("doctors", f.get("doctor_id", ""))
            if not d:
                flash("Doctor not found.", "error")
            else:
                for a in store.scan("appointments", doctor_id=d["doctor_id"]):
                    if a["status"] in ("Pending", "Confirmed"):
                        store.update("appointments", a["appointment_id"], status="Cancelled")
                        puid = user_id_of_patient(a["patient_id"])
                        if puid:
                            notify(puid, "Appointment Cancelled",
                                   f"Your appointment on {pretty_date(a['date'])} at {a['time']} was cancelled "
                                   "because the doctor is no longer available.", "appointment")
                remove_user(d["user_id"], "doctors", d["doctor_id"])
                flash(f"Doctor {d['name']} removed.", "success")
            return redirect(url_for("manage_doctors"))
        form = f
        name, email = clean(f.get("name"), 100), clean(f.get("email"), 120).lower()
        phone, exp = clean(f.get("phone"), 20), clean(f.get("experience"), 2) or "0"
        error = validate_account(name, email, phone, f.get("password", ""))
        if not error and not exp.isdigit():
            error = "Experience must be a number."
        if error:
            flash(error, "error")
        else:
            create_account(name, email, phone, f["password"], "doctor",
                           specialization=clean(f.get("specialization"), 80),
                           qualification=clean(f.get("qualification"), 120), experience=exp,
                           availability=clean(f.get("availability"), 100),
                           hospital=clean(f.get("hospital"), 120))
            flash("Doctor account created.", "success")
            return redirect(url_for("manage_doctors"))
    doctors = sorted(store.scan("doctors"), key=lambda d: d["name"])
    return render_template("manage_doctors.html", doctors=doctors, form=form)


@app.route("/admin/appointments", methods=["GET", "POST"])
@login_required("admin")
def manage_appointments():
    if request.method == "POST":
        a = store.get("appointments", request.form.get("appointment_id", ""))
        if not a or a["status"] not in ("Pending", "Confirmed"):
            flash("This appointment cannot be cancelled.", "error")
        else:
            store.update("appointments", a["appointment_id"], status="Cancelled")
            when = f"{pretty_date(a['date'])} at {a['time']}"
            for uid in (user_id_of_patient(a["patient_id"]), user_id_of_doctor(a["doctor_id"])):
                if uid:
                    notify(uid, "Appointment Cancelled",
                           f"The appointment on {when} was cancelled by the administrator.", "appointment")
            flash("Appointment cancelled.", "success")
        return redirect(url_for("manage_appointments"))
    appts = sorted(store.scan("appointments"), key=appt_key, reverse=True)
    return render_template("manage_appointments.html", appointments=appts)


@app.route("/admin/diagnosis")
@login_required("admin")
def admin_diagnosis():
    records = newest_first(store.scan("diagnosis"))
    patients = {p["patient_id"]: p for p in store.scan("patients")}
    doctors = {d["doctor_id"]: d for d in store.scan("doctors")}
    return render_template("admin_diagnosis.html", records=records, patients=patients, doctors=doctors)


@app.route("/admin/notifications")
@login_required("admin")
def admin_notifications():
    users = {u["user_id"]: u for u in store.scan("users")}
    items = newest_first(store.scan("notifications"))[:200]
    return render_template("admin_notifications.html", notifications=items, users=users)


# --------------------------------------------------------------------------
# 9. ERROR HANDLING
# --------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


@app.errorhandler(Exception)
def server_error(e):
    if isinstance(e, HTTPException):
        if e.code == 404:
            return not_found(e)
        return render_template("500.html", message=e.description), e.code
    if app.debug:
        raise e  # developers see the full traceback; users never do in production
    app.logger.exception("Unhandled error")
    return render_template("500.html", message=None), 500


# --------------------------------------------------------------------------
# 10. START-UP
# --------------------------------------------------------------------------
def safe_bootstrap():
    try:
        bootstrap()
    except Exception as exc:  # e.g. DynamoDB tables not created yet
        app.logger.error("Start-up check failed: %s. If you use DynamoDB run: python app.py init-db", exc)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "init-db":
        create_tables()
        safe_bootstrap()
    else:
        safe_bootstrap()
        app.run(host="0.0.0.0" if IS_PROD else "127.0.0.1",
                port=int(os.getenv("PORT", "5000")),
                debug=not IS_PROD and os.getenv("DEBUG", "true").lower() == "true")
else:
    safe_bootstrap()  # when started by gunicorn
