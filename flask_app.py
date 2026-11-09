"""
All Events - ticketing & reservations platform (Flask + SQLite + Paynow)

SECRETS: nothing sensitive lives in this file any more. Provide them as
environment variables (PythonAnywhere: WSGI file / Web tab) or in an optional
`config_local.py` next to this file (never commit it):

    SECRET_KEY, PAYNOW_ID, PAYNOW_KEY, SMTP_EMAIL, SMTP_PASSWORD,
    ADMIN_PASSWORD (only used the first time the admin user is created),
    DOMAIN_URL (optional), LOCAL_DEV=1 (optional, allows cookies over http)
"""
import base64
import csv
import hmac
import html
import io
import logging
import os
import re
import secrets
import smtplib
import sqlite3
import string
import threading
import time
import uuid

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
from urllib.parse import urlparse

import qrcode
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for
from paynow import Paynow
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("allevents")

# ----------------------------------------------------------------------------
# 1. CONFIGURATION
# ----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "brandit_tickets_v1.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

try:
    import config_local as _local  # optional, keep out of version control
except ImportError:
    _local = None


def cfg(name, default=""):
    return os.environ.get(name) or getattr(_local, name, None) or default


DOMAIN_URL = cfg("DOMAIN_URL", "https://anesu.pythonanywhere.com").rstrip("/")
PAYNOW_ID = cfg("PAYNOW_ID")
PAYNOW_KEY = cfg("PAYNOW_KEY")
SMTP_SERVER = cfg("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(cfg("SMTP_PORT", "587"))
SMTP_EMAIL = cfg("SMTP_EMAIL")
SMTP_PASSWORD = cfg("SMTP_PASSWORD")

# Behaviour switches
HOLD_MINUTES = 20            # unpaid reservations are released after this long
MAX_TICKETS_PER_ORDER = 10
SHOW_PAST_EVENTS = False     # False = events whose date has passed are hidden and can't be bought
TZ_OFFSET_HOURS = 2          # Zimbabwe (CAT) - used to decide what "today" is
SESSION_SECONDS = 3600
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
VALID_ROLES = {"Admin", "Cashier", "Bouncer"}
# (stored value, label shown in the admin forms). Concert/Restaurant/Marathon/Walkathon/Conference match the public filter bar.
CATEGORY_CHOICES = [("Concert", "Concert / Music"), ("Conference", "Conference"), ("Marathon", "Marathon"),
                    ("Walkathon", "Walkathon"), ("Restaurant", "Table booking"), ("Sports", "Sports"),
                    ("General", "General")]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def load_secret_key():
    key = cfg("SECRET_KEY")
    if key:
        return key
    path = os.path.join(BASE_DIR, ".secret_key")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


app = Flask(__name__)
app.secret_key = load_secret_key()
app.config.update(
    UPLOAD_FOLDER=UPLOAD_FOLDER,
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=DOMAIN_URL.startswith("https") and not cfg("LOCAL_DEV"),
)
app.permanent_session_lifetime = timedelta(seconds=SESSION_SECONDS)

paynow = None
if PAYNOW_ID and PAYNOW_KEY:
    paynow = Paynow(PAYNOW_ID, PAYNOW_KEY, f"{DOMAIN_URL}/paynow/return", f"{DOMAIN_URL}/paynow/callback")
else:
    log.warning("PAYNOW_ID / PAYNOW_KEY not set - online payments are disabled.")

# ----------------------------------------------------------------------------
# 2. SMALL HELPERS
# ----------------------------------------------------------------------------
def utc_now():
    return datetime.now(timezone.utc)


def utc_str(dt=None):
    return (dt or utc_now()).strftime("%Y-%m-%d %H:%M:%S")


def local_today():
    return (utc_now() + timedelta(hours=TZ_OFFSET_HOURS)).date()


def to_float(value, default=0.0, maximum=1_000_000.0):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return default
    return x if 0 <= x <= maximum else default


def to_int(value, default=0, minimum=0, maximum=100_000):
    try:
        x = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(x, minimum), maximum)


def simple_page(title, message, color="#222", status=200, back_url="/", back_label="Go back to Home", refresh=None):
    meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    body = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{meta}<title>{html.escape(title)}</title></head>
<body style="font-family:sans-serif;text-align:center;margin:0;padding:60px 20px;background:#f6f4f1;color:#222">
<h2 style="color:{color}">{html.escape(title)}</h2>
<p style="max-width:520px;margin:12px auto">{html.escape(message)}</p>
<a href="{html.escape(back_url)}" style="display:inline-block;margin-top:20px;padding:10px 20px;background:#222;color:#fff;text-decoration:none;border-radius:5px">{html.escape(back_label)}</a>
</body></html>"""
    return Response(body, status=status, mimetype="text/html")


# ----------------------------------------------------------------------------
# 3. DATABASE
# ----------------------------------------------------------------------------
def get_db_connection():
    # isolation_level=None -> autocommit; we open explicit transactions where needed.
    conn = sqlite3.connect(DB_NAME, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = get_db_connection()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn):
    """BEGIN IMMEDIATE takes the write lock up front, so 'check then update' is race-free."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def add_column_if_missing(conn, table, column, ddl):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS Events (
            EventID INTEGER PRIMARY KEY AUTOINCREMENT, Title TEXT NOT NULL, EventDate TEXT, Venue TEXT,
            Description TEXT, ImageURL TEXT, Category TEXT DEFAULT 'General', IsProMode INTEGER DEFAULT 0,
            Subdomain TEXT, ThemeColor TEXT DEFAULT '#c0ff02', IsActive INTEGER DEFAULT 1,
            CreatedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS EventPrices (
            PriceID INTEGER PRIMARY KEY AUTOINCREMENT, EventID INTEGER NOT NULL, TicketType TEXT NOT NULL,
            Price REAL NOT NULL, FOREIGN KEY(EventID) REFERENCES Events(EventID))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS EventTickets (
            TicketID INTEGER PRIMARY KEY AUTOINCREMENT, EventID INTEGER NOT NULL, TicketCode TEXT NOT NULL UNIQUE,
            TicketType TEXT DEFAULT 'Standard', GuestName TEXT, GuestEmail TEXT, AccessPin TEXT,
            IsUsed INTEGER DEFAULT 0, ScannedAt TIMESTAMP, PaynowRef TEXT, PaymentStatus TEXT DEFAULT 'Pending',
            CreatedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(EventID) REFERENCES Events(EventID))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS Users (
            UserID INTEGER PRIMARY KEY AUTOINCREMENT, Username TEXT UNIQUE NOT NULL, PasswordHash TEXT NOT NULL,
            Role TEXT DEFAULT 'Bouncer', AssignedEventID INTEGER, FailedAttempts INTEGER DEFAULT 0,
            IsLocked INTEGER DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS EventAds (
            AdID INTEGER PRIMARY KEY AUTOINCREMENT, Title TEXT, ImageURL TEXT, Description TEXT,
            EventDate TEXT, IsActive INTEGER DEFAULT 1)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS Subscribers (
            SubID INTEGER PRIMARY KEY AUTOINCREMENT, Email TEXT UNIQUE NOT NULL,
            JoinedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

        # Migrations
        add_column_if_missing(conn, "EventTickets", "PaynowRef", "TEXT")
        add_column_if_missing(conn, "EventTickets", "HeldAt", "TEXT")
        add_column_if_missing(conn, "Users", "LastFailedAt", "TEXT")
        add_column_if_missing(conn, "Events", "SourceURL", "TEXT")

        # EventPrices had no uniqueness, so every "INSERT OR REPLACE" on edit added a duplicate row.
        conn.execute("""DELETE FROM EventPrices WHERE PriceID NOT IN
                        (SELECT MAX(PriceID) FROM EventPrices GROUP BY EventID, TicketType)""")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_price_unique ON EventPrices(EventID, TicketType)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_lookup ON EventTickets(EventID, TicketType, PaymentStatus)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tickets_ref ON EventTickets(PaynowRef)")

        admin = conn.execute("SELECT PasswordHash FROM Users WHERE Username='admin'").fetchone()
        if not admin:
            password = cfg("ADMIN_PASSWORD")
            generated = not password
            password = password or secrets.token_urlsafe(12)
            conn.execute("INSERT INTO Users (Username, PasswordHash, Role) VALUES ('admin', ?, 'Admin')",
                         (generate_password_hash(password),))
            if generated:
                log.warning("Created user 'admin' with temporary password: %s  (change it at /account/password)", password)
        elif check_password_hash(admin["PasswordHash"], "admin123"):
            log.warning("SECURITY: the 'admin' account still uses the default password. Change it at /account/password")


init_db()

# ----------------------------------------------------------------------------
# 4. SESSIONS, ROLES & REQUEST GUARDS
# ----------------------------------------------------------------------------
CSRF_EXEMPT_ENDPOINTS = {"paynow_callback"}


@app.before_request
def request_guard():
    # Same-origin check for state-changing requests (CSRF mitigation without touching templates).
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.endpoint not in CSRF_EXEMPT_ENDPOINTS:
        source = request.headers.get("Origin") or request.headers.get("Referer")
        if source and urlparse(source).netloc != request.host:
            abort(403)

    if "user_id" in session:
        now = time.time()
        last = session.get("last_active")
        if not isinstance(last, (int, float)) or now - last > SESSION_SECONDS:
            session.clear()
            return None
        session["last_active"] = now
        session.permanent = True
    return None


def assigned_event_id():
    try:
        value = int(session.get("assigned_event"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


@app.context_processor
def inject_staff_flags():
    role = session.get("role")
    return {"current_role": role, "is_global_admin": role == "Admin" and not assigned_event_id()}


def can_access_event(event_id):
    assigned = assigned_event_id()
    return assigned is None or assigned == event_id


def home_for_role(role):
    if role == "Admin":
        return url_for("admin_events")
    if role == "Cashier":
        return url_for("pos_sell")
    return url_for("scan_home")


def roles_required(*roles):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if session.get("role") not in roles:
                if request.method == "GET":
                    session["next_url"] = request.full_path.rstrip("?")
                return redirect(url_for("login"))
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def global_admin_required(fn):
    @wraps(fn)
    @roles_required("Admin")
    def wrapper(*args, **kwargs):
        if assigned_event_id():
            return simple_page("Access denied", "Only global admins can do this.", "#c0392b", 403, "/admin", "Back")
        return fn(*args, **kwargs)
    return wrapper


def same_origin_only(fn):
    """For admin actions still reachable by GET (delete, mark paid...): the click must come from this site."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        ref = request.headers.get("Referer") or request.headers.get("Origin") or ""
        if urlparse(ref).netloc != request.host:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def clean_category(value, fallback="General"):
    allowed = {v for v, _ in CATEGORY_CHOICES}
    value = (value or "").strip()
    if value in allowed or (fallback and value == fallback):
        return value
    return fallback or "General"


def clean_date(value):
    """<input type=datetime-local> sends 2026-10-25T18:00; store it as '2026-10-25 18:00'."""
    return (value or "").strip().replace("T", " ")


def clean_subdomain(value):
    return re.sub(r"[^a-z0-9-]", "", (value or "").lower())[:40] or None


def safe_back(default="/"):
    ref = request.referrer or ""
    if ref and urlparse(ref).netloc == request.host:
        return ref
    return default


# ----------------------------------------------------------------------------
# 5. EVENT DATE HANDLING
# ----------------------------------------------------------------------------
DATE_FORMATS = [
    ("%Y-%m-%d", False), ("%Y-%m-%d %H:%M", True), ("%Y-%m-%dT%H:%M", True),
    ("%Y-%m-%d %H:%M:%S", True), ("%Y-%m-%dT%H:%M:%S", True),
    ("%d %B %Y", False), ("%d %b %Y", False), ("%d %B %Y %H:%M", True), ("%d %b %Y %H:%M", True),
    ("%d/%m/%Y", False), ("%d/%m/%Y %H:%M", True), ("%d-%m-%Y", False),
]


def parse_event_date(text):
    text = (text or "").strip()
    for fmt, has_time in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt), has_time
        except ValueError:
            continue
    return None, False


def enrich_event(row):
    """Row -> dict with display-ready date fields used by the templates."""
    ev = dict(row)
    dt, has_time = parse_event_date(ev.get("EventDate"))
    ev["_dt"] = dt
    raw = (ev.get("EventDate") or "").strip()
    if dt:
        ev["DayNum"] = f"{dt.day:02d}"
        ev["MonthShort"] = dt.strftime("%b")
        ev["ShortDate"] = f"{dt.day} {dt.strftime('%b')}"
        ev["DateLabel"] = dt.strftime("%a %d %b %Y") + (dt.strftime(", %H:%M") if has_time else "")
        ev["IsPast"] = dt.date() < local_today()
    else:  # unknown format: never hide the event, show the text as typed
        ev["DayNum"] = (raw.split(" ")[0][:2] if raw else "--")
        ev["MonthShort"] = ""
        ev["ShortDate"] = raw[:12]
        ev["DateLabel"] = raw
        ev["IsPast"] = False
    return ev


EVENT_SELECT = """
    SELECT e.*,
    (SELECT COUNT(*) FROM EventTickets t WHERE t.EventID = e.EventID AND t.PaymentStatus='Available') AS AvailableCount
    FROM Events e
"""


def load_events(conn, category=None, include_past=SHOW_PAST_EVENTS):
    sql, params = EVENT_SELECT + " WHERE e.IsActive=1", ()
    if category and category != "All":
        sql += " AND e.Category=?"
        params = (category,)
    events = [enrich_event(r) for r in conn.execute(sql, params).fetchall()]
    if not include_past:
        events = [e for e in events if not e["IsPast"]]
    events.sort(key=lambda e: (e["_dt"] is None, e["_dt"] or datetime.max, e["EventID"]))
    return events


# ----------------------------------------------------------------------------
# 6. TICKETS, QR CODES & EMAIL
# ----------------------------------------------------------------------------
def make_qr_png(code):
    buf = io.BytesIO()
    qrcode.make(f"{DOMAIN_URL}/gate/{code}").save(buf, format="PNG")
    return buf.getvalue()


def make_qr_b64(code):
    return base64.b64encode(make_qr_png(code)).decode()


def issue_ticket(conn, ticket_id):
    """Give a ticket its public code + secret PIN and mark it Paid. Call inside a transaction."""
    pin = "".join(secrets.choice(string.digits) for _ in range(4))
    for _ in range(25):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        try:
            conn.execute(
                "UPDATE EventTickets SET TicketCode=?, AccessPin=?, PaymentStatus='Paid', HeldAt=NULL WHERE TicketID=?",
                (code, pin, ticket_id))
            return code, pin
        except sqlite3.IntegrityError:  # code collision, try another
            continue
    raise RuntimeError("Could not generate a unique ticket code")


def send_ticket_email(to_email, guest_name, ticket_code, ticket_type, pin, event_title=""):
    if not to_email or not (SMTP_EMAIL and SMTP_PASSWORD):
        log.warning("Ticket email not sent (missing recipient or SMTP settings)")
        return False
    try:
        title = re.sub(r"[\r\n]+", " ", event_title or "").strip()
        msg = MIMEMultipart("related")
        msg["Subject"] = f"Your ticket for {title}" if title else "Your event ticket"
        msg["From"] = SMTP_EMAIL
        msg["To"] = to_email
        body = f"""<html><body style="background:#111;color:#fff;padding:24px;font-family:sans-serif">
<h2>EVENT ACCESS</h2>
<p>Hello {html.escape(guest_name or 'guest')},</p>
<p>{html.escape(title)} &mdash; {html.escape(ticket_type or '')}</p>
<p>Show this QR code at the gate:</p>
<p><img src="cid:qrcode_image" alt="Ticket QR code" width="220" height="220" style="background:#fff;padding:8px"></p>
<p>Ticket code: <b>{html.escape(ticket_code)}</b></p>
<p>Your secret access PIN (the gate will ask for it):</p>
<h1 style="letter-spacing:6px">{html.escape(pin)}</h1>
</body></html>"""
        msg.attach(MIMEText(body, "html"))
        image = MIMEImage(make_qr_png(ticket_code))
        image.add_header("Content-ID", "<qrcode_image>")
        image.add_header("Content-Disposition", "inline", filename="ticket-qr.png")
        msg.attach(image)
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception:
        log.exception("Failed to send ticket email to %s", to_email)
        return False


def send_ticket_batch(items):
    for item in items:
        send_ticket_email(**item)


def send_order_email(client_name, client_email, service_type, details):
    if not (SMTP_EMAIL and SMTP_PASSWORD):
        return False
    try:
        msg = MIMEMultipart()
        msg["Subject"] = re.sub(r"[\r\n]+", " ", f"New Inquiry: {service_type}")
        msg["From"] = SMTP_EMAIL
        msg["To"] = SMTP_EMAIL
        msg.attach(MIMEText(f"Client: {client_name}\nEmail: {client_email}\nDetails: {details}", "plain"))
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception:
        log.exception("Failed to send order email")
        return False


def run_async(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


# ----------------------------------------------------------------------------
# 7. PAYMENT SETTLEMENT (shared by the return page, the Paynow callback and hold expiry)
# ----------------------------------------------------------------------------
PAID_STATES = {"paid", "awaiting delivery", "delivered"}
DEAD_STATES = {"cancelled", "refunded", "disputed", "failed"}
ORDER_ROWS_SQL = """SELECT t.*, e.Title AS EventTitle FROM EventTickets t
                    JOIN Events e ON e.EventID = t.EventID WHERE t.PaynowRef=? ORDER BY t.TicketID"""


def release_by_ref(conn, ref):
    conn.execute("""UPDATE EventTickets
                    SET GuestName=NULL, GuestEmail=NULL, PaynowRef=NULL, PaymentStatus='Available', HeldAt=NULL
                    WHERE PaynowRef=? AND PaymentStatus='Initiated'""", (ref,))


def settle_order(poll_url):
    """
    Ask Paynow (server to server) what happened and update the tickets accordingly.
    Safe to call many times / concurrently: tickets are only issued once.
    Returns (state, rows) where state is 'paid' | 'cancelled' | 'pending' | 'unknown'.
    """
    with db() as conn:
        rows = conn.execute(ORDER_ROWS_SQL, (poll_url,)).fetchall()
        if rows and all(r["PaymentStatus"] == "Paid" for r in rows):
            return "paid", rows
        if paynow is None:
            return ("pending" if rows else "unknown"), rows
        try:
            status = paynow.check_transaction_status(poll_url)
            state = (getattr(status, "status", "") or "").lower()
            paid = bool(getattr(status, "paid", False)) or state in PAID_STATES
        except Exception:
            log.exception("Paynow status check failed")
            return ("pending" if rows else "unknown"), rows

        if not rows:
            if paid:
                log.error("PAID ORDER WITH NO TICKETS (released too early?) poll_url=%s", poll_url)
            return "unknown", []

        if paid:
            emails = []
            with transaction(conn):
                for r in conn.execute(ORDER_ROWS_SQL, (poll_url,)).fetchall():
                    if r["PaymentStatus"] == "Initiated":
                        code, pin = issue_ticket(conn, r["TicketID"])
                        emails.append(dict(to_email=r["GuestEmail"], guest_name=r["GuestName"], ticket_code=code,
                                           ticket_type=r["TicketType"], pin=pin, event_title=r["EventTitle"]))
            rows = conn.execute(ORDER_ROWS_SQL, (poll_url,)).fetchall()
            if emails:
                run_async(send_ticket_batch, emails)
            return "paid", rows

        if state in DEAD_STATES:
            release_by_ref(conn, poll_url)
            return "cancelled", []
        return "pending", rows


def release_expired_holds():
    """Unpaid reservations older than HOLD_MINUTES go back on sale (after one last check with Paynow)."""
    try:
        cutoff = utc_str(utc_now() - timedelta(minutes=HOLD_MINUTES))
        with db() as conn:
            refs = [r["PaynowRef"] for r in conn.execute(
                """SELECT DISTINCT PaynowRef FROM EventTickets
                   WHERE PaymentStatus='Initiated' AND PaynowRef IS NOT NULL AND (HeldAt IS NULL OR HeldAt < ?)""",
                (cutoff,)).fetchall()]
        for ref in refs:
            if ref.startswith("http"):
                state, _ = settle_order(ref)
                if state != "pending":
                    continue
            with db() as conn:
                release_by_ref(conn, ref)
    except Exception:
        log.exception("release_expired_holds failed")


# ----------------------------------------------------------------------------
# 8. PUBLIC ROUTES
# ----------------------------------------------------------------------------
@app.route("/status")
def server_status():
    return "OK", 200


@app.route("/")
def landing_page():
    host = (request.headers.get("Host") or "").split(":")[0]
    parts = host.split(".")
    subdomain = None
    if len(parts) >= 3 and parts[0].lower() != "www" and parts[1] == "branditevents":
        subdomain = parts[0]

    release_expired_holds()

    with db() as conn:
        if subdomain:
            event = conn.execute(
                "SELECT * FROM Events WHERE Subdomain=? AND IsProMode=1 AND IsActive=1", (subdomain,)).fetchone()
            if not event:
                return simple_page("Event not found", "We couldn't find that event.", "#c0392b", 404)
            if event["Category"] == "Restaurant":
                return render_template("smokehouse_landing.html", events=load_events(conn, "Restaurant"))
            prices = {r["TicketType"]: r["Price"] for r in
                      conn.execute("SELECT * FROM EventPrices WHERE EventID=?", (event["EventID"],))}
            return render_template("pro_landing.html", event=enrich_event(event), prices=prices)

        category = request.args.get("category")
        upcoming = load_events(conn)
        events = upcoming if (not category or category == "All") else [e for e in upcoming if e["Category"] == category]
        ads = conn.execute("SELECT * FROM EventAds WHERE IsActive=1 ORDER BY AdID DESC").fetchall()

    # The ticker always shows the next events across ALL categories, whatever filter is active.
    return render_template("landing.html", events=events, ticker_events=upcoming[:10], ads=ads,
                           active_category=category)


@app.route("/subscribe", methods=["POST"])
def subscribe_newsletter():
    email = (request.form.get("email") or "").strip().lower()
    if email and len(email) <= 254 and EMAIL_RE.match(email):
        with db() as conn:
            try:
                conn.execute("INSERT INTO Subscribers (Email) VALUES (?)", (email,))
            except sqlite3.IntegrityError:
                pass  # already subscribed
    return redirect(safe_back("/"))


# ----------------------------------------------------------------------------
# 9. BUYING & PAYMENT
# ----------------------------------------------------------------------------
@app.route("/buy", defaults={"event_id": 1}, methods=["GET", "POST"])
@app.route("/buy/<int:event_id>", methods=["GET", "POST"])
def buy_ticket(event_id):
    release_expired_holds()
    with db() as conn:
        row = conn.execute("SELECT * FROM Events WHERE EventID=? AND IsActive=1", (event_id,)).fetchone()
        if not row:
            if event_id == 1:
                return redirect(url_for("select_event_page"))
            return simple_page("Event not found", "We couldn't find that event.", "#c0392b", 404)
        event = enrich_event(row)
        if event.get("SourceURL"):
            if request.method == "POST":
                abort(400)
            return redirect(event["SourceURL"])
        if event["IsPast"] and not SHOW_PAST_EVENTS:
            return simple_page("This event has ended", "Tickets are no longer on sale for this event.", "#c0392b",
                               back_url="/", back_label="See upcoming events")

        prices = conn.execute("SELECT * FROM EventPrices WHERE EventID=? ORDER BY PriceID", (event_id,)).fetchall()
        price_dict = {p["TicketType"]: p["Price"] for p in prices}
        counts = {r["TicketType"]: r["c"] for r in conn.execute(
            """SELECT TicketType, COUNT(*) AS c FROM EventTickets
               WHERE EventID=? AND PaymentStatus='Available' GROUP BY TicketType""", (event_id,))}
        inventory = {t: counts.get(t, 0) for t in price_dict}

        if request.method == "POST":
            name = (request.form.get("name") or "").strip()[:100]
            email = (request.form.get("email") or "").strip()
            t_type = request.form.get("type")
            quantity = to_int(request.form.get("quantity"), 1, 1, MAX_TICKETS_PER_ORDER)
            extra = (request.form.get("requests") or "").strip()[:100]
            back = request.path

            if not name or not EMAIL_RE.match(email):
                return simple_page("Check your details", "Please enter your name and a valid email address.",
                                   "#c0392b", 400, back, "Go back")
            if t_type not in price_dict:
                return simple_page("Unknown ticket type", "Please choose a ticket type from the list.",
                                   "#c0392b", 400, back, "Go back")
            unit_price = float(price_dict[t_type])
            if unit_price <= 0:
                return simple_page("Not available online", "This ticket type can't be bought online.",
                                   "#c0392b", 400, back, "Go back")
            if paynow is None:
                return simple_page("Payments unavailable", "Online payment is temporarily unavailable. Please try again later.",
                                   "#c0392b", 503, back, "Go back")

            guest_name = f"{name} (Club/Church: {extra})" if extra else name
            total = round(unit_price * quantity, 2)
            order_ref = uuid.uuid4().hex

            # 1) Reserve the tickets atomically so two buyers can never get the same ticket.
            with transaction(conn):
                free = conn.execute(
                    """SELECT TicketID FROM EventTickets
                       WHERE EventID=? AND TicketType=? AND PaymentStatus='Available'
                       ORDER BY TicketID LIMIT ?""", (event_id, t_type, quantity)).fetchall()
                if len(free) >= quantity:
                    held_at = utc_str()
                    conn.executemany(
                        """UPDATE EventTickets SET GuestName=?, GuestEmail=?, PaynowRef=?, PaymentStatus='Initiated', HeldAt=?
                           WHERE TicketID=? AND PaymentStatus='Available'""",
                        [(guest_name, email, order_ref, held_at, f["TicketID"]) for f in free])
            if len(free) < quantity:
                return simple_page("Not enough tickets left",
                                   f"Only {len(free)} {t_type} ticket(s) are still available.",
                                   "#c0392b", 409, back, "Go back")

            # 2) Create the Paynow payment; on any failure put the tickets back on sale.
            try:
                payment = paynow.create_payment(f"EV{event_id}-{order_ref[:10]}", email)
                payment.add(f"{event['Title']} - {t_type} (x{quantity})", total)
                response = paynow.send(payment)
            except Exception:
                log.exception("Paynow send failed")
                release_by_ref(conn, order_ref)
                return simple_page("Payment error", "We couldn't start your payment. Please try again.",
                                   "#c0392b", 502, back, "Go back")

            if not response.success:
                release_by_ref(conn, order_ref)
                log.error("Paynow rejected payment: %s", getattr(response, "error", ""))
                return simple_page("Payment could not be started",
                                   "Paynow didn't accept the request. Please try again.", "#c0392b", 502, back, "Go back")

            conn.execute("UPDATE EventTickets SET PaynowRef=? WHERE PaynowRef=?", (response.poll_url, order_ref))
            session["last_order"] = response.poll_url
            return redirect(response.redirect_url)

    return render_template("buy_ticket.html", event=event, price_rows=prices, inventory=inventory,
                           max_tickets=MAX_TICKETS_PER_ORDER, payments_enabled=paynow is not None,
                           tickets_available=any(p["Price"] > 0 and inventory[p["TicketType"]] > 0 for p in prices))


@app.route("/buy/list")
def select_event_page():
    with db() as conn:
        events = load_events(conn)
    return render_template("select_event.html", events=events)


@app.route("/paynow/return")
def paynow_return():
    poll_url = session.get("last_order")
    if not poll_url:
        return simple_page("We couldn't find your order",
                           "If you already paid, your tickets will be emailed to you shortly.")

    state, rows = settle_order(poll_url)

    if state == "paid":
        paid_rows = [r for r in rows if r["PaymentStatus"] == "Paid"]
        tickets = [{"code": r["TicketCode"], "pin": r["AccessPin"], "qr": make_qr_b64(r["TicketCode"])}
                   for r in paid_rows]
        return render_template("buy_success.html", guest_name=paid_rows[0]["GuestName"], tickets=tickets,
                               ticket_type=paid_rows[0]["TicketType"])
    if state == "cancelled":
        session.pop("last_order", None)
        return simple_page("Payment cancelled",
                           "Your transaction was not completed. The tickets have been released back into inventory.",
                           "#c0392b")
    if state == "pending":
        return simple_page("Waiting for your payment",
                           "We haven't received confirmation yet. This page refreshes automatically; "
                           "if you've approved the payment on your phone, it can take a minute.",
                           "#9e5a38", refresh=6, back_url="/", back_label="Back to Home")
    return simple_page("We couldn't find your order",
                       "If you already paid, your tickets will be emailed to you shortly.")


@app.route("/paynow/callback", methods=["GET", "POST"])
def paynow_callback():
    """Paynow calls this when the payment status changes. We never trust the payload: we re-check with Paynow."""
    poll_url = request.values.get("pollurl")
    if poll_url:
        try:
            settle_order(poll_url)
        except Exception:
            log.exception("paynow_callback failed")
    return "OK", 200


# ----------------------------------------------------------------------------
# 10. ADMIN: EVENTS
# ----------------------------------------------------------------------------
def save_upload(file_storage):
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(secure_filename(file_storage.filename))[1].lower()
    if ext not in ALLOWED_IMAGE_EXT:
        raise ValueError("Only JPG, PNG, WEBP or GIF images are allowed.")
    name = f"{uuid.uuid4().hex}{ext}"
    file_storage.save(os.path.join(UPLOAD_FOLDER, name))
    return f"/static/uploads/{name}"


@app.route("/admin")
@roles_required("Admin")
def admin_home():
    return redirect(url_for("admin_events"))


@app.route("/admin/events")
@roles_required("Admin")
def admin_events():
    sql = """
        SELECT e.*,
        (SELECT COUNT(*) FROM EventTickets t WHERE t.EventID = e.EventID AND t.PaymentStatus='Paid') AS SoldCount,
        (SELECT COUNT(*) FROM EventTickets t WHERE t.EventID = e.EventID) AS TotalCapacity
        FROM Events e
    """
    assigned = assigned_event_id()
    with db() as conn:
        if assigned:
            rows = conn.execute(sql + " WHERE e.EventID=?", (assigned,)).fetchall()
        else:
            rows = conn.execute(sql).fetchall()
    events = [enrich_event(r) for r in rows]  # adds DateLabel / IsPast for the cards
    events.sort(key=lambda e: e["_dt"] or datetime.min, reverse=True)
    return render_template("admin_events.html", events=events)


@app.route("/admin/event/create", methods=["GET", "POST"])
@global_admin_required
def admin_create_event():
    if request.method == "GET":
        return render_template("admin_create_event.html", categories=CATEGORY_CHOICES)

    back = request.path
    title = (request.form.get("title") or "").strip()
    if not title:
        return simple_page("Missing title", "Please enter an event title.", "#c0392b", 400, back, "Go back")
    try:
        image_url = save_upload(request.files.get("image"))
    except ValueError as e:
        return simple_page("Image not accepted", str(e), "#c0392b", 400, back, "Go back")

    tiers = [("Standard", to_float(request.form.get("std_price")), to_int(request.form.get("std_qty"), 0, 0, 5000), "S"),
             ("VIP", to_float(request.form.get("vip_price")), to_int(request.form.get("vip_qty"), 0, 0, 5000), "V")]
    subdomain = clean_subdomain(request.form.get("subdomain"))

    with db() as conn:
        try:
            with transaction(conn):
                cur = conn.execute(
                    """INSERT INTO Events (Title, EventDate, Venue, Description, ImageURL, Category, IsProMode, Subdomain)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (title[:150], clean_date(request.form.get("date")), request.form.get("venue"),
                     request.form.get("description"), image_url, clean_category(request.form.get("category")),
                     1 if request.form.get("is_pro") else 0, subdomain))
                event_id = cur.lastrowid
                for t_type, price, qty, letter in tiers:
                    if qty > 0 or price > 0:  # don't create empty "VIP R0" tiers
                        conn.execute("INSERT INTO EventPrices (EventID, TicketType, Price) VALUES (?, ?, ?)",
                                     (event_id, t_type, price))
                    for _ in range(qty):
                        conn.execute(
                            "INSERT INTO EventTickets (EventID, TicketCode, TicketType, PaymentStatus) VALUES (?, ?, ?, 'Available')",
                            (event_id, f"{event_id}{letter}-{uuid.uuid4().hex[:8].upper()}", t_type))
        except Exception as e:
            log.exception("Event creation failed")
            return simple_page("Could not create event", str(e), "#c0392b", 500, back, "Go back")
    return redirect(url_for("admin_events"))


@app.route("/admin/event/edit/<int:event_id>", methods=["GET", "POST"])
@app.route("/admin/edit/<int:event_id>", methods=["GET", "POST"])
@app.route("/admin/edit_event/<int:event_id>", methods=["GET", "POST"])
@roles_required("Admin")
def admin_edit_event(event_id):
    if not can_access_event(event_id):
        return simple_page("Access denied", "This isn't your event.", "#c0392b", 403, "/admin", "Back")

    with db() as conn:
        event = conn.execute("SELECT * FROM Events WHERE EventID=?", (event_id,)).fetchone()
        if not event:
            abort(404)

        if request.method == "POST":
            try:
                new_image = save_upload(request.files.get("image"))
            except ValueError as e:
                return simple_page("Image not accepted", str(e), "#c0392b", 400, request.path, "Go back")
            image_url = new_image or (request.form.get("image_url") or "").strip() or event["ImageURL"]

            existing_types = [r["TicketType"] for r in
                              conn.execute("SELECT TicketType FROM EventPrices WHERE EventID=?", (event_id,))]
            with transaction(conn):
                conn.execute(
                    """UPDATE Events SET Title=?, EventDate=?, Venue=?, Description=?, ImageURL=?, Category=?
                       WHERE EventID=?""",
                    ((request.form.get("title") or event["Title"]).strip()[:150],
                     clean_date(request.form.get("date")) or event["EventDate"] or "",
                     request.form.get("venue", event["Venue"]), request.form.get("description", event["Description"]),
                     image_url, clean_category(request.form.get("category"), event["Category"]), event_id))
                # These fields only change when the form that owns them was submitted. Older forms that
                # didn't send them used to silently switch off Pro pages on every save.
                if request.form.get("edit_form"):
                    conn.execute("UPDATE Events SET IsActive=? WHERE EventID=?",
                                 (1 if request.form.get("is_active") else 0, event_id))
                if request.form.get("pro_fields") and not assigned_event_id():
                    conn.execute("UPDATE Events SET IsProMode=?, Subdomain=? WHERE EventID=?",
                                 (1 if request.form.get("is_pro") else 0,
                                  clean_subdomain(request.form.get("subdomain")), event_id))
                # Only touch price fields that were actually submitted (blank = leave as is).
                for t_type in sorted(set(existing_types) | {"Standard", "VIP"}):
                    raw = request.form.get(f"price_{t_type.lower()}")
                    if raw is None or raw.strip() == "":
                        continue
                    conn.execute(
                        """INSERT INTO EventPrices (EventID, TicketType, Price) VALUES (?, ?, ?)
                           ON CONFLICT(EventID, TicketType) DO UPDATE SET Price=excluded.Price""",
                        (event_id, t_type, to_float(raw)))
            return redirect(url_for("admin_events"))

        prices = {r["TicketType"]: r["Price"] for r in
                  conn.execute("SELECT * FROM EventPrices WHERE EventID=?", (event_id,))}
    dt, _has_time = parse_event_date(event["EventDate"])
    date_value = dt.strftime("%Y-%m-%dT%H:%M") if dt else ""
    return render_template("admin_edit_event.html", event=event, prices=prices, categories=CATEGORY_CHOICES,
                           date_value=date_value)


@app.route("/admin/event/delete/<int:event_id>", methods=["GET", "POST"])
@app.route("/admin/delete/<int:event_id>", methods=["GET", "POST"])
@global_admin_required
@same_origin_only
def admin_delete_event(event_id):
    with db() as conn:
        paid = conn.execute("SELECT COUNT(*) FROM EventTickets WHERE EventID=? AND PaymentStatus='Paid'",
                            (event_id,)).fetchone()[0]
        if paid:
            # Never destroy sold tickets: hide the event instead.
            conn.execute("UPDATE Events SET IsActive=0 WHERE EventID=?", (event_id,))
            return simple_page("Event hidden, not deleted",
                               f"This event has {paid} paid ticket(s), so it was deactivated instead of deleted.",
                               "#9e5a38", back_url="/admin", back_label="Back to admin")
        try:
            with transaction(conn):
                conn.execute("DELETE FROM EventTickets WHERE EventID=?", (event_id,))
                conn.execute("DELETE FROM EventPrices WHERE EventID=?", (event_id,))
                conn.execute("UPDATE Users SET AssignedEventID=NULL WHERE AssignedEventID=?", (event_id,))
                conn.execute("DELETE FROM Events WHERE EventID=?", (event_id,))
        except Exception as e:
            log.exception("Event delete failed")
            return simple_page("Error deleting event", str(e), "#c0392b", 500, "/admin", "Back")
    return redirect(url_for("admin_events"))


@app.route("/admin/event/<int:event_id>/dashboard")
@roles_required("Admin")
def admin_event_dashboard(event_id):
    if not can_access_event(event_id):
        return simple_page("Access denied", "This isn't your event.", "#c0392b", 403, "/admin", "Back")
    with db() as conn:
        event = conn.execute("SELECT * FROM Events WHERE EventID=?", (event_id,)).fetchone()
        if not event:
            abort(404)
        total = conn.execute("SELECT COUNT(*) FROM EventTickets WHERE EventID=? AND PaymentStatus='Paid'",
                             (event_id,)).fetchone()[0]
        inside = conn.execute("SELECT COUNT(*) FROM EventTickets WHERE EventID=? AND IsUsed=1",
                              (event_id,)).fetchone()[0]
        tickets = conn.execute("""SELECT * FROM EventTickets WHERE EventID=? AND PaymentStatus='Paid'
                                  ORDER BY CreatedAt DESC LIMIT 20""", (event_id,)).fetchall()
    return render_template("admin.html", event_id=event_id, event_name=event["Title"], total=total, inside=inside,
                           pending=total - inside, tickets=tickets)


@app.route("/admin/ticket/mark_paid/<int:ticket_id>")
@roles_required("Admin")
@same_origin_only
def admin_mark_paid(ticket_id):
    email_job = None
    with db() as conn:
        with transaction(conn):
            row = conn.execute("""SELECT t.*, e.Title AS EventTitle FROM EventTickets t
                                  JOIN Events e ON e.EventID=t.EventID WHERE t.TicketID=?""", (ticket_id,)).fetchone()
            if not row:
                abort(404)
            if not can_access_event(row["EventID"]):
                abort(403)
            if row["PaymentStatus"] != "Paid":
                code, pin = issue_ticket(conn, ticket_id)
                if row["GuestEmail"]:
                    email_job = dict(to_email=row["GuestEmail"], guest_name=row["GuestName"], ticket_code=code,
                                     ticket_type=row["TicketType"], pin=pin, event_title=row["EventTitle"])
    if email_job:
        run_async(send_ticket_batch, [email_job])
    return redirect(safe_back(url_for("admin_events")))


@app.route("/admin/ticket/resend/<int:ticket_id>")
@roles_required("Admin")
@same_origin_only
def admin_resend_email(ticket_id):
    with db() as conn:
        row = conn.execute("""SELECT t.*, e.Title AS EventTitle FROM EventTickets t
                              JOIN Events e ON e.EventID=t.EventID WHERE t.TicketID=?""", (ticket_id,)).fetchone()
    if not row:
        abort(404)
    if not can_access_event(row["EventID"]):
        abort(403)
    if row["PaymentStatus"] == "Paid":
        run_async(send_ticket_batch, [dict(to_email=row["GuestEmail"], guest_name=row["GuestName"],
                                           ticket_code=row["TicketCode"], ticket_type=row["TicketType"],
                                           pin=row["AccessPin"], event_title=row["EventTitle"])])
    return redirect(safe_back(url_for("admin_events")))


@app.route("/admin/newsletter", methods=["GET", "POST"])
@global_admin_required
def admin_newsletter():
    with db() as conn:
        if request.args.get("action") == "export":
            subscribers = conn.execute("SELECT Email, JoinedAt FROM Subscribers").fetchall()
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["Email", "JoinedAt"])
            for s in subscribers:
                email = s["Email"]
                writer.writerow([("'" + email) if email[:1] in "=+-@" else email, s["JoinedAt"]])
            return Response(output.getvalue(), mimetype="text/csv",
                            headers={"Content-Disposition": "attachment; filename=subscribers.csv"})

        recipients = [r["Email"] for r in conn.execute("SELECT Email FROM Subscribers")]

    if request.method == "POST":
        subject = re.sub(r"[\r\n]+", " ", (request.form.get("subject") or "").strip())
        body = (request.form.get("message") or "").strip()
        if not subject or not body:
            return render_template("admin_newsletter.html", count=len(recipients),
                                   error="Please enter a subject and a message.")

        def send_blast(emails, subj, text):
            if not (SMTP_EMAIL and SMTP_PASSWORD):
                log.warning("Newsletter not sent: SMTP settings missing")
                return
            try:
                with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
                    server.starttls()
                    server.login(SMTP_EMAIL, SMTP_PASSWORD)
                    for address in emails:
                        try:
                            msg = MIMEMultipart()
                            msg["Subject"], msg["From"], msg["To"] = subj, SMTP_EMAIL, address
                            msg.attach(MIMEText(text, "plain"))
                            server.send_message(msg)
                        except Exception:
                            log.exception("Newsletter failed for %s", address)
            except Exception:
                log.exception("Newsletter run failed")

        run_async(send_blast, recipients, subject, body)
        return render_template("admin_newsletter.html", success="Emails are being sent!", count=len(recipients))
    return render_template("admin_newsletter.html", count=len(recipients))


# ----------------------------------------------------------------------------
# 11. ADMIN: USERS
# ----------------------------------------------------------------------------
@app.route("/admin/users")
@global_admin_required
def admin_users():
    with db() as conn:
        users = conn.execute("""SELECT u.*, e.Title AS EventName FROM Users u
                                LEFT JOIN Events e ON u.AssignedEventID = e.EventID""").fetchall()
    return render_template("admin_user.html", users=users)


@app.route("/admin/users/create", methods=["GET", "POST"])
@global_admin_required
def admin_create_user():
    with db() as conn:
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            role = request.form.get("role")
            event_id = to_int(request.form.get("event_id"), 0) or None
            back = request.path
            if not username or role not in VALID_ROLES:
                return simple_page("Invalid user", "Enter a username and choose a valid role.",
                                   "#c0392b", 400, back, "Go back")
            if len(password) < 8:
                return simple_page("Password too short", "Use at least 8 characters.", "#c0392b", 400, back, "Go back")
            try:
                conn.execute("INSERT INTO Users (Username, PasswordHash, Role, AssignedEventID) VALUES (?, ?, ?, ?)",
                             (username, generate_password_hash(password), role, event_id))
            except sqlite3.IntegrityError:
                return simple_page("Username taken", "Please choose a different username.", "#c0392b", 409, back, "Go back")
            return redirect(url_for("admin_users", created=username))
        events = conn.execute("SELECT * FROM Events WHERE IsActive=1").fetchall()
    return render_template("admin_create_user.html", events=events)


@app.route("/admin/users/reset/<int:user_id>", methods=["POST"])
@global_admin_required
def admin_reset_password(user_id):
    """Admin sets a new password for a user (also clears any login lockout)."""
    password = request.form.get("password") or ""
    if len(password) < 8:
        return simple_page("Password too short", "Use at least 8 characters.", "#c0392b", 400, "/admin/users", "Back")
    with db() as conn:
        user = conn.execute("SELECT Username FROM Users WHERE UserID=?", (user_id,)).fetchone()
        if not user:
            abort(404)
        conn.execute("UPDATE Users SET PasswordHash=?, FailedAttempts=0, LastFailedAt=NULL, IsLocked=0 WHERE UserID=?",
                     (generate_password_hash(password), user_id))
    return redirect(url_for("admin_users", reset=user["Username"]))


@app.route("/admin/users/delete/<int:user_id>", methods=["POST"])
@global_admin_required
def admin_delete_user(user_id):
    if user_id == session.get("user_id"):
        return simple_page("Not allowed", "You can't delete your own account.", "#c0392b", 400, "/admin/users", "Back")
    with db() as conn:
        conn.execute("DELETE FROM Users WHERE UserID=?", (user_id,))
    return redirect(url_for("admin_users"))


# ----------------------------------------------------------------------------
# 12. AUTH
# ----------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("user_id"):
            return redirect(home_for_role(session.get("role")))
        return render_template("login.html")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    with db() as conn:
        user = conn.execute("SELECT * FROM Users WHERE Username=?", (username,)).fetchone()
        if user and user["IsLocked"]:
            return render_template("login.html", error="This account is locked. Contact an administrator.")
        if user and (user["FailedAttempts"] or 0) >= MAX_FAILED_LOGINS and user["LastFailedAt"]:
            try:
                last = datetime.strptime(user["LastFailedAt"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                last = None
            if last and utc_now() - last < timedelta(minutes=LOCKOUT_MINUTES):
                return render_template("login.html",
                                       error=f"Too many attempts. Try again in {LOCKOUT_MINUTES} minutes.")
        if not (user and check_password_hash(user["PasswordHash"], password)):
            if user:
                conn.execute("UPDATE Users SET FailedAttempts=COALESCE(FailedAttempts,0)+1, LastFailedAt=? WHERE UserID=?",
                             (utc_str(), user["UserID"]))
            return render_template("login.html", error="Invalid Credentials")
        conn.execute("UPDATE Users SET FailedAttempts=0, LastFailedAt=NULL WHERE UserID=?", (user["UserID"],))

    next_url = session.pop("next_url", None)
    session.clear()  # fresh session on login (prevents session fixation)
    session["user_id"] = user["UserID"]
    session["role"] = user["Role"]
    session["assigned_event"] = user["AssignedEventID"]
    session["last_active"] = time.time()
    session.permanent = True
    if next_url and next_url.startswith("/") and not next_url.startswith("//") and "\\" not in next_url:
        return redirect(next_url)
    return redirect(home_for_role(user["Role"]))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("landing_page"))


@app.route("/account/password", methods=["GET", "POST"])
@roles_required("Admin", "Cashier", "Bouncer")
def change_password():
    def form(message="", color="#c0392b"):
        note = f'<p style="color:{color}">{html.escape(message)}</p>' if message else ""
        return Response(f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Change password</title></head>
<body style="font-family:sans-serif;max-width:360px;margin:60px auto;padding:0 20px">
<h2>Change password</h2>{note}
<form method="post">
<p><input type="password" name="current" placeholder="Current password" required style="width:100%;padding:10px"></p>
<p><input type="password" name="new" placeholder="New password (8+ characters)" required minlength="8" style="width:100%;padding:10px"></p>
<p><button style="padding:10px 20px">Save password</button></p>
</form><p><a href="/">Back</a></p></body></html>""", mimetype="text/html")

    if request.method == "GET":
        return form()
    current, new = request.form.get("current") or "", request.form.get("new") or ""
    with db() as conn:
        user = conn.execute("SELECT * FROM Users WHERE UserID=?", (session["user_id"],)).fetchone()
        if not user or not check_password_hash(user["PasswordHash"], current):
            return form("Current password is incorrect.")
        if len(new) < 8:
            return form("New password must be at least 8 characters.")
        conn.execute("UPDATE Users SET PasswordHash=? WHERE UserID=?", (generate_password_hash(new), user["UserID"]))
    return form("Password updated.", "#2e7d32")


# ----------------------------------------------------------------------------
# 13. GATE SCANNING & POS (staff only)
# ----------------------------------------------------------------------------
@app.route("/scan_home")
@app.route("/scanner")
@roles_required("Admin", "Bouncer")
def scan_home():
    """Bouncers land here after login: the camera opens straight away (see templates/scanner.html)."""
    event_name = inside = total = None
    assigned = assigned_event_id()
    if assigned:
        with db() as conn:
            ev = conn.execute("SELECT Title FROM Events WHERE EventID=?", (assigned,)).fetchone()
            event_name = ev["Title"] if ev else None
            inside, total = _gate_counts(conn, assigned)
    return render_template("scanner.html", event_name=event_name, inside=inside, total=total)


def _gate_counts(conn, event_id):
    inside = conn.execute("SELECT COUNT(*) FROM EventTickets WHERE EventID=? AND IsUsed=1", (event_id,)).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM EventTickets WHERE EventID=? AND PaymentStatus='Paid'", (event_id,)).fetchone()[0]
    return inside, total


def api_staff_required(fn):
    """Like roles_required, but answers JSON 401 so the scanner page can send the bouncer back to login."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get("role") not in ("Admin", "Bouncer"):
            return jsonify(status="auth", message="Session expired. Please log in again."), 401
        return fn(*args, **kwargs)
    return wrapper


def extract_ticket_code(raw):
    """Accepts a scanned QR URL (.../gate/CODE) or a hand-typed code."""
    raw = (raw or "").strip()
    m = re.search(r"/gate/([A-Za-z0-9\-]+)", raw)
    return (m.group(1) if m else raw).upper()[:40]


def gate_state(row):
    if not row or row["PaymentStatus"] != "Paid":
        return "fake", "INVALID TICKET"
    if row["IsUsed"] == 1:
        return "used", "ALREADY USED"
    if not can_access_event(row["EventID"]):
        return "wrong_event", "WRONG EVENT"
    return "ok", ""


@app.route("/api/gate/lookup", methods=["POST"])
@api_staff_required
def api_gate_lookup():
    code = extract_ticket_code((request.get_json(silent=True) or {}).get("code"))
    with db() as conn:
        row = _gate_ticket(conn, code)
    status, message = gate_state(row)
    resp = {"status": status, "message": message}
    if row and status in ("ok", "used", "wrong_event"):
        resp.update(guest=row["GuestName"], ticket_type=row["TicketType"])
        if status == "used":
            resp["scanned_at"] = row["ScannedAt"]
    return jsonify(resp)


@app.route("/api/gate/verify", methods=["POST"])
@api_staff_required
def api_gate_verify():
    data = request.get_json(silent=True) or {}
    code, pin = extract_ticket_code(data.get("code")), str(data.get("pin") or "").strip()
    with db() as conn:
        row = _gate_ticket(conn, code)
        status, message = gate_state(row)
        if status != "ok":
            return jsonify(status=status, message=message, guest=row["GuestName"] if row else None)
        if not hmac.compare_digest(str(row["AccessPin"] or ""), pin):
            return jsonify(status="wrong_pin", message="WRONG PIN", guest=row["GuestName"])
        # Atomic: only the first successful scan can flip IsUsed from 0 to 1.
        cur = conn.execute("UPDATE EventTickets SET IsUsed=1, ScannedAt=CURRENT_TIMESTAMP WHERE TicketID=? AND IsUsed=0",
                           (row["TicketID"],))
        if cur.rowcount == 0:
            return jsonify(status="used", message="ALREADY USED", guest=row["GuestName"])
        inside, total = _gate_counts(conn, row["EventID"])
    return jsonify(status="granted", message="ACCESS GRANTED", guest=row["GuestName"],
                   ticket_type=row["TicketType"], inside=inside, total=total)


def _gate_ticket(conn, code):
    return conn.execute("SELECT * FROM EventTickets WHERE TicketCode=?", ((code or "").strip().upper(),)).fetchone()


@app.route("/gate/<ticket_code>")
@roles_required("Admin", "Bouncer")
def gate_check(ticket_code):
    with db() as conn:
        row = _gate_ticket(conn, ticket_code)
    if not row or row["PaymentStatus"] != "Paid":
        return render_template("gate_result.html", status="FAKE", message="INVALID TICKET", color="red")
    if row["IsUsed"] == 1:
        return render_template("gate_result.html", status="DENIED", message="ALREADY USED",
                               guest=row["GuestName"], color="red")
    if not can_access_event(row["EventID"]):
        return render_template("gate_result.html", status="DENIED", message="WRONG EVENT",
                               guest=f"Ticket for Event #{row['EventID']}", color="orange")
    return render_template("gate_pin.html", ticket_code=row["TicketCode"], guest_name=row["GuestName"],
                           ticket_type=row["TicketType"])


@app.route("/gate/verify_pin", methods=["POST"])
@roles_required("Admin", "Bouncer")
def verify_pin():
    code, pin = request.form.get("ticket_code"), request.form.get("pin") or ""
    with db() as conn:
        row = _gate_ticket(conn, code)
        if not row or row["PaymentStatus"] != "Paid":
            return render_template("gate_result.html", status="FAKE", message="INVALID TICKET", color="red")
        if not can_access_event(row["EventID"]):
            return render_template("gate_result.html", status="DENIED", message="WRONG EVENT",
                                   guest=f"Ticket for Event #{row['EventID']}", color="orange")
        if not hmac.compare_digest(str(row["AccessPin"] or ""), pin.strip()):
            return render_template("gate_result.html", status="DENIED", message="WRONG PIN",
                                   guest=row["GuestName"], color="red")
        # Atomic: only the first scan can flip IsUsed from 0 to 1.
        cur = conn.execute("UPDATE EventTickets SET IsUsed=1, ScannedAt=CURRENT_TIMESTAMP WHERE TicketID=? AND IsUsed=0",
                           (row["TicketID"],))
        if cur.rowcount == 0:
            return render_template("gate_result.html", status="DENIED", message="ALREADY USED",
                                   guest=row["GuestName"], color="red")
    return render_template("gate_result.html", status="GRANTED", message="ACCESS GRANTED",
                           guest=row["GuestName"], color="green")


@app.route("/pos", methods=["GET", "POST"])
@roles_required("Admin", "Cashier")
def pos_sell():
    with db() as conn:
        event_id = assigned_event_id() or to_int(request.values.get("event_id"), 0) or 1
        if not conn.execute("SELECT 1 FROM Events WHERE EventID=? AND IsActive=1", (event_id,)).fetchone():
            first = conn.execute("SELECT EventID FROM Events WHERE IsActive=1 ORDER BY EventID LIMIT 1").fetchone()
            if not first:
                return simple_page("No events", "Create an event first.", "#c0392b", 404, "/admin", "Back")
            event_id = first["EventID"]
        prices = {r["TicketType"]: r["Price"] for r in
                  conn.execute("SELECT * FROM EventPrices WHERE EventID=?", (event_id,))}
        event = enrich_event(conn.execute("SELECT * FROM Events WHERE EventID=?", (event_id,)).fetchone())
        counts = {r["TicketType"]: r["c"] for r in conn.execute(
            """SELECT TicketType, COUNT(*) AS c FROM EventTickets
               WHERE EventID=? AND PaymentStatus='Available' GROUP BY TicketType""", (event_id,))}
        inventory = {t: counts.get(t, 0) for t in prices}
        switch_events = load_events(conn) if (session.get("role") == "Admin" and not assigned_event_id()) else []

        if request.method == "POST":
            guest_name = (request.form.get("guest_name") or "").strip()[:100] or "Walk-in"
            t_type = request.form.get("ticket_type")
            if t_type not in prices:
                return simple_page("Unknown ticket type", "Choose a ticket type from the list.", "#c0392b", 400, request.path, "Back")
            with transaction(conn):
                free = conn.execute("""SELECT TicketID FROM EventTickets
                                       WHERE EventID=? AND TicketType=? AND PaymentStatus='Available'
                                       ORDER BY TicketID LIMIT 1""", (event_id, t_type)).fetchone()
                if free:
                    code, _pin = issue_ticket(conn, free["TicketID"])  # also sets the PIN the gate needs
                    conn.execute("UPDATE EventTickets SET GuestName=? WHERE TicketID=?", (guest_name, free["TicketID"]))
            if not free:
                return simple_page("SOLD OUT", "No tickets available to sell.", "#c0392b", 409, request.path, "Back")
            return redirect(url_for("pos_print", ticket_code=code))
    return render_template("pos_sell.html", prices=prices, event=event, event_id=event_id,
                           inventory=inventory, switch_events=switch_events)


@app.route("/pos/print/<ticket_code>")
@roles_required("Admin", "Cashier")
def pos_print(ticket_code):
    with db() as conn:
        ticket = _gate_ticket(conn, ticket_code)
    if not ticket or ticket["PaymentStatus"] != "Paid":
        abort(404)
    if not can_access_event(ticket["EventID"]):
        abort(403)
    with db() as conn:
        ev = conn.execute("SELECT * FROM Events WHERE EventID=?", (ticket["EventID"],)).fetchone()
    return render_template("receipt.html", ticket=ticket, event=enrich_event(ev) if ev else None,
                           qr_b64=make_qr_b64(ticket["TicketCode"]))


@app.route("/smokehouse")
def smokehouse_demo():
    with db() as conn:
        events = load_events(conn, "Restaurant")
    return render_template("smokehouse_landing.html", events=events)


# ----------------------------------------------------------------------------
# 14. DEMO SEEDERS (admin only, safe to run more than once, dates are relative to today)
# ----------------------------------------------------------------------------
def seed_event(conn, title, days_ahead, venue, description, image, category, tiers):
    if conn.execute("SELECT 1 FROM Events WHERE Title=?", (title,)).fetchone():
        return False
    date = (local_today() + timedelta(days=days_ahead)).isoformat()
    with transaction(conn):
        eid = conn.execute(
            """INSERT INTO Events (Title, EventDate, Venue, Description, ImageURL, Category, IsProMode)
               VALUES (?, ?, ?, ?, ?, ?, 0)""", (title, date, venue, description, image, category)).lastrowid
        for t_type, price, qty in tiers:
            conn.execute("INSERT INTO EventPrices (EventID, TicketType, Price) VALUES (?, ?, ?)", (eid, t_type, price))
            for _ in range(qty):
                conn.execute(
                    "INSERT INTO EventTickets (EventID, TicketCode, TicketType, PaymentStatus) VALUES (?, ?, ?, 'Available')",
                    (eid, f"{eid}{t_type[0]}-{uuid.uuid4().hex[:8].upper()}", t_type))
    return True


@app.route("/seed_smokehouse")
@roles_required("Admin")
def seed_smokehouse_data():
    with db() as conn:
        seed_event(conn, "Harare: Friday Night Grill", 7, "Greenfields, Harare",
                   "Reserve a table for the Friday Night Grill at Greenfields. Includes live music and platter specials. $5 Deposit secures your table.",
                   "https://images.unsplash.com/photo-1555939594-58d7cb561ad1?q=80&w=800", "Restaurant",
                   [("Table Deposit", 5.00, 20)])
        seed_event(conn, "Bulawayo: Sunday Roast", 9, "Busters Sports Club, Byo",
                   "The legendary Busters Sunday Roast. Perfect for families. Kids under 12 eat free. Deposit required for tables of 4+.",
                   "https://images.unsplash.com/photo-1594041680534-e8c8cdebd659?q=80&w=800", "Restaurant",
                   [("Family Table", 10.00, 15)])
        seed_event(conn, "Vic Falls: Sunset Brews", 8, "River Brewing Co, Vic Falls",
                   "Watch the sunset with craft beers and smoky ribs. Best view in town.",
                   "https://images.unsplash.com/photo-1514362545857-3bc16c4c7d1b?q=80&w=800", "Restaurant",
                   [("Couple Deposit", 5.00, 10)])
    return "Smokehouse demo data created. Go to /smokehouse to see it."


@app.route("/seed_main_demo")
@roles_required("Admin")
def seed_main_demo():
    with db() as conn:
        seed_event(conn, "St. George's vs Peterhouse: T20 Cricket", 10, "St. George's College, Harare",
                   "The biggest derby of the season. Come support the Saints or the Kings. Food stalls and bar available at the Pavilion. Match starts 14:00.",
                   "https://images.unsplash.com/photo-1531415074984-61e663ba384d?q=80&w=800", "Sports",
                   [("General Access", 2.00, 50), ("Pavilion Pass", 5.00, 20)])
        seed_event(conn, "Unplugged Zimbabwe: Season Finale", 21, "Borrowdale Race Course",
                   "Zimbabwe's premier family-friendly music festival. Live performances by Jah Prayzah, Winky D, and Mokoomba. Cooler boxes allowed before 2pm.",
                   "https://images.unsplash.com/photo-1459749411177-287ce3808bca?q=80&w=800", "Concert",
                   [("Early Bird", 15.00, 50), ("VIP Marquee", 40.00, 20)])
        seed_event(conn, "Econet Victoria Falls Marathon", 45, "Victoria Falls Primary School",
                   "Run across the bridge and experience the thunder! Full Marathon (42km) and Half Marathon (21km). International runners welcome.",
                   "https://images.unsplash.com/photo-1552674605-5d226a58b357?q=80&w=800", "Marathon",
                   [("Full Marathon Reg", 20.00, 50), ("Half Marathon Reg", 15.00, 50)])
        seed_event(conn, "Digital Marketing Africa Summit", 14, "HICC, Harare",
                   "Join 500+ industry leaders to discuss the future of AI and Marketing in Africa. Includes lunch and networking cocktails.",
                   "https://images.unsplash.com/photo-1544531320-dadbed291404?q=80&w=800", "Conference",
                   [("Delegate Pass", 50.00, 30)])
    return "Main site demo data created. Go to the homepage to view."


# ----------------------------------------------------------------------------
# 15. ERROR PAGES
# ----------------------------------------------------------------------------
@app.errorhandler(403)
def forbidden(_e):
    return simple_page("Not allowed", "You don't have permission to do that.", "#c0392b", 403)


@app.errorhandler(404)
def not_found(_e):
    return simple_page("Page not found", "That page doesn't exist.", "#c0392b", 404)


@app.errorhandler(413)
def too_large(_e):
    return simple_page("File too large", "Uploads are limited to 8 MB.", "#c0392b", 413, back_url="javascript:history.back()", back_label="Go back")


@app.errorhandler(500)
def server_error(_e):
    return simple_page("Something went wrong", "Please try again in a moment.", "#c0392b", 500)


if __name__ == "__main__":
    app.run(debug=False)
