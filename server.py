"""The Emporium — school store pickup ordering + staff inventory.

Zero dependencies: runs on the Python 3.9+ standard library.
    python3 server.py
Then open http://localhost:8000 (store) and http://localhost:8000/admin (staff).
"""
import hashlib
import ipaddress
import socket
import urllib.request
import urllib.error
import urllib.parse
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).parent
STATIC = ROOT / "static"
DB_PATH = os.environ.get("DB_PATH", str(ROOT / "emporium.db"))
# Photos live next to the database so one persistent disk holds everything.
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR") or Path(DB_PATH).resolve().parent / "uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
UPLOAD_MIME = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}
PORT = int(os.environ.get("PORT", "8000"))
# Optional: a fixed staff password from the environment. When unset (the normal case), the
# first person to open /admin creates the password, and only a salted hash is stored.
ENV_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or None
SESSION_HOURS = 12
ORDER_STATUSES = ("new", "ready", "picked_up", "cancelled")

# Student accounts: Sign in with Google, limited to the school's email domain.
STUDENT_DOMAIN = os.environ.get("STUDENT_EMAIL_DOMAIN", "alpha.school").lower().lstrip("@")
STUDENT_SESSION_DAYS = 30
GOOGLE_TOKENINFO_URL = os.environ.get("GOOGLE_TOKENINFO_URL", "https://oauth2.googleapis.com/tokeninfo")
# Staff by Google account: these school emails can open /admin by signing in with Google,
# no staff password needed. Comma-separated, set on the server (not in the code).
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
MAX_BALANCE_CENTS = 100_000  # $1,000 cap guards against typos when staff add money

# Weekly limit: each student may buy this many items per week (staff can change it in Settings;
# 0 = no limit). Weeks start Monday 00:00 in the school's time zone; cancelled orders don't count.
DEFAULT_WEEKLY_ITEMS = 1
STORE_TZ = os.environ.get("STORE_TIMEZONE", "America/Chicago")

# Writes are serialized with db_lock so stock checks and decrements can't interleave.
db_lock = threading.Lock()


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer
    return conn


class _ThreadDB:
    """Each request thread gets its own SQLite connection (sharing one across threads breaks
    when two requests overlap). Behaves like a connection: DB.execute(...), `with DB:`."""

    _local = threading.local()

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = connect()
        return conn

    def __getattr__(self, name):
        return getattr(self._conn(), name)

    def __enter__(self):
        return self._conn().__enter__()

    def __exit__(self, *exc):
        return self._conn().__exit__(*exc)


DB = _ThreadDB()


def init_db():
    with db_lock, DB:
        DB.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'Other',
                price_cents INTEGER NOT NULL,
                stock INTEGER NOT NULL DEFAULT 0,
                image_url TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                customer_name TEXT NOT NULL,
                customer_contact TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new',
                total_cents INTEGER NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                google_sub TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                picture TEXT NOT NULL DEFAULT '',
                balance_cents INTEGER NOT NULL DEFAULT 0 CHECK (balance_cents >= 0),
                created_at REAL NOT NULL,
                last_login REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL REFERENCES students(id),
                delta_cents INTEGER NOT NULL,
                balance_after INTEGER NOT NULL,
                kind TEXT NOT NULL,            -- deposit | order | refund | adjust
                order_id INTEGER,
                note TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL REFERENCES students(id),
                name TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',   -- open | added | declined
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS request_votes (
                request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
                student_id INTEGER NOT NULL REFERENCES students(id),
                created_at REAL NOT NULL,
                PRIMARY KEY (request_id, student_id)
            );
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                product_id INTEGER,
                name TEXT NOT NULL,
                price_cents INTEGER NOT NULL,
                qty INTEGER NOT NULL
            );
            """
        )
        if "is_admin" not in {r["name"] for r in DB.execute("PRAGMA table_info(students)")}:
            DB.execute("ALTER TABLE students ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        if "stock_taken" not in {r["name"] for r in DB.execute("PRAGMA table_info(orders)")}:
            # Orders placed before approvals existed already took their items out of stock.
            DB.execute("ALTER TABLE orders ADD COLUMN stock_taken INTEGER NOT NULL DEFAULT 1")
        if "student_id" not in {r["name"] for r in DB.execute("PRAGMA table_info(orders)")}:
            DB.execute("ALTER TABLE orders ADD COLUMN student_id INTEGER REFERENCES students(id)")
        count = DB.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        seed = ROOT / "products.json"
        if count == 0 and seed.exists():
            for p in json.loads(seed.read_text()):
                DB.execute(
                    "INSERT INTO products (name, description, category, price_cents, stock, image_url, active, created_at)"
                    " VALUES (?,?,?,?,?,?,1,?)",
                    (
                        p["name"],
                        p.get("description", ""),
                        p.get("category", "Other"),
                        round(float(p["price"]) * 100),
                        int(p.get("stock", 0)),
                        p.get("image_url", ""),
                        time.time(),
                    ),
                )


# ---------- helpers ----------

def on_hold(product_id):
    """Items in orders waiting for staff approval: paid for, but not yet taken out of stock."""
    return DB.execute(
        "SELECT COALESCE(SUM(i.qty),0) FROM order_items i JOIN orders o ON o.id=i.order_id "
        "WHERE i.product_id=? AND o.stock_taken=0 AND o.status != 'cancelled'", (product_id,)).fetchone()[0]


def product_dict(row, admin=False):
    held = on_hold(row["id"])
    d = {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "category": row["category"],
        "price_cents": row["price_cents"],
        # Students see what's still orderable; staff see the real shelf count.
        "stock": row["stock"] if admin else max(0, row["stock"] - held),
        "on_hold": held,
        "image_url": row["image_url"],
    }
    if admin:
        d["active"] = bool(row["active"])
    return d


def order_dict(row, include_contact=False):
    items = DB.execute(
        "SELECT product_id, name, price_cents, qty FROM order_items WHERE order_id=? ORDER BY id", (row["id"],)
    ).fetchall()
    d = {
        "id": row["id"],
        "code": row["code"],
        "customer_name": row["customer_name"],
        "status": row["status"],
        "total_cents": row["total_cents"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "items": [dict(i) for i in items],
    }
    if include_contact:
        d["customer_contact"] = row["customer_contact"]
        d["note"] = row["note"]
        d["student_id"] = row["student_id"]
    return d


def weekly_limit():
    v = get_setting("weekly_item_limit")
    return int(v) if v is not None and v.isdigit() else DEFAULT_WEEKLY_ITEMS


def week_bounds(now=None):
    """(start, next_start) of the current Monday-to-Monday week, as Unix timestamps."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(STORE_TZ)
    local = datetime.fromtimestamp(now or time.time(), tz)
    start = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    nxt = start + timedelta(days=7)
    # rebuild from wall-clock dates so a daylight-saving change mid-week doesn't shift Monday midnight
    start = datetime(start.year, start.month, start.day, tzinfo=tz)
    nxt = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
    return start.timestamp(), nxt.timestamp()


def items_this_week(student_id):
    start, _ = week_bounds()
    return DB.execute(
        "SELECT COALESCE(SUM(oi.qty), 0) FROM order_items oi JOIN orders o ON o.id = oi.order_id "
        "WHERE o.student_id=? AND o.status != 'cancelled' AND o.created_at >= ?", (student_id, start)
    ).fetchone()[0]


def weekly_status(student_id):
    limit = weekly_limit()
    used = items_this_week(student_id)
    _, resets = week_bounds()
    return {"limit": limit, "used": used, "remaining": None if limit == 0 else max(0, limit - used), "resets_at": resets}


def account_is_admin(row):
    """Staff are recognized by their Google account: listed in ADMIN_EMAILS on the server,
    or made admin by another admin on the Students tab."""
    return bool(row) and (row["email"].lower() in ADMIN_EMAILS or bool(row["is_admin"]))


def student_dict(row):
    return {
        "id": row["id"], "email": row["email"], "name": row["name"], "picture": row["picture"],
        "balance_cents": row["balance_cents"], "created_at": row["created_at"], "last_login": row["last_login"],
        "is_admin": account_is_admin(row), "admin_locked": row["email"].lower() in ADMIN_EMAILS,
    }


def move_balance(student_id, delta, kind, note="", order_id=None):
    """Change a balance and record why. Call inside `with db_lock, DB:`. Never goes below zero."""
    row = DB.execute("SELECT balance_cents FROM students WHERE id=?", (student_id,)).fetchone()
    if not row:
        raise ApiError(404, "Student not found.")
    new = row["balance_cents"] + delta
    if new < 0:
        raise ApiError(409, "Not enough balance.")
    if new > MAX_BALANCE_CENTS:
        raise ApiError(400, f"Balances are capped at ${MAX_BALANCE_CENTS // 100}.")
    DB.execute("UPDATE students SET balance_cents=? WHERE id=?", (new, student_id))
    DB.execute("INSERT INTO ledger (student_id, delta_cents, balance_after, kind, order_id, note, created_at) VALUES (?,?,?,?,?,?,?)",
               (student_id, delta, new, kind, order_id, note, time.time()))
    return new


def new_code():
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "EM-" + "".join(secrets.choice(alphabet) for _ in range(5))


def get_setting(key):
    row = DB.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key, value):
    with db_lock, DB:
        DB.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def secret_key():
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    key = get_setting("secret_key")
    if not key:
        with db_lock, DB:
            DB.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('secret_key', ?)", (secrets.token_hex(32),))
        key = get_setting("secret_key")
    return key


PBKDF2_ROUNDS = 200_000


def hash_password(pw):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS).hex()
    return f"pbkdf2${PBKDF2_ROUNDS}${salt}${digest}"


def check_password(pw):
    if ENV_ADMIN_PASSWORD:
        return hmac.compare_digest(pw.encode(), ENV_ADMIN_PASSWORD.encode())
    stored = get_setting("admin_pw_hash")
    if not stored:
        return False
    _, rounds, salt, digest = stored.split("$")
    test = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(rounds)).hex()
    return hmac.compare_digest(test, digest)


def needs_setup():
    return not ENV_ADMIN_PASSWORD and not get_setting("admin_pw_hash")


def sign(value):
    # Keyed on the current password too, so changing the password signs everyone out.
    key = secret_key() + (get_setting("admin_pw_hash") or ENV_ADMIN_PASSWORD or "")
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()


def make_session():
    exp = str(int(time.time() + SESSION_HOURS * 3600))
    return exp + "." + sign(exp)


def valid_session(token):
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    return hmac.compare_digest(sig, sign(exp)) and exp.isdigit() and int(exp) > time.time()


def sign_student(value):
    return hmac.new((secret_key() + ":student").encode(), value.encode(), hashlib.sha256).hexdigest()


def make_student_session(student_id):
    value = f"{student_id}.{int(time.time() + STUDENT_SESSION_DAYS * 86400)}"
    return value + "." + sign_student(value)


def student_from_token(token):
    try:
        sid, exp, sig = token.split(".")
    except (AttributeError, ValueError):
        return None
    if not (sid.isdigit() and exp.isdigit()) or int(exp) < time.time():
        return None
    if not hmac.compare_digest(sig, sign_student(f"{sid}.{exp}")):
        return None
    return DB.execute("SELECT * FROM students WHERE id=?", (int(sid),)).fetchone()


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def clean_text(value, field, max_len, required=False):
    value = (value or "").strip() if isinstance(value, str) else ""
    if required and not value:
        raise ApiError(400, f"{field} is required.")
    if len(value) > max_len:
        raise ApiError(400, f"{field} is too long (max {max_len} characters).")
    return value


def parse_product(body, partial=False):
    out = {}
    if not partial or "name" in body:
        out["name"] = clean_text(body.get("name"), "Name", 120, required=True)
    for key, label, limit in (("description", "Description", 500), ("category", "Category", 60), ("image_url", "Image URL", 1000)):
        if key in body:
            out[key] = clean_text(body.get(key), label, limit)
    if "category" in out and not out["category"]:
        out["category"] = "Other"
    if not partial or "price" in body:
        try:
            price = round(float(body.get("price")) * 100)
        except (TypeError, ValueError):
            raise ApiError(400, "Price must be a number, like 2.50.")
        if price < 0 or price > 1_000_000:
            raise ApiError(400, "Price is out of range.")
        out["price_cents"] = price
    if "stock" in body:
        try:
            stock = int(body.get("stock"))
        except (TypeError, ValueError):
            raise ApiError(400, "Stock must be a whole number.")
        if stock < 0 or stock > 100_000:
            raise ApiError(400, "Stock is out of range.")
        out["stock"] = stock
    if "active" in body:
        out["active"] = 1 if body.get("active") else 0
    return out


# ---------- request handler ----------

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".json": "application/json",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "Emporium/1.0"

    def log_message(self, fmt, *args):
        print("%s %s" % (self.command, fmt % args))

    # --- plumbing ---
    def send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        headers = extra_headers or {}
        for k, v in (headers.items() if isinstance(headers, dict) else headers):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 100_000:
            raise ApiError(413, "Request too large.")
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            raise ApiError(400, "Invalid JSON.")
        if not isinstance(data, dict):
            raise ApiError(400, "Expected a JSON object.")
        return data

    def cookie(self, name):
        # Read one cookie by hand. http.cookies.SimpleCookie gives up on the whole header when any
        # cookie has characters it dislikes (Google's sign-in sets one full of JSON), which would
        # silently sign everyone out.
        for part in self.headers.get("Cookie", "").split(";"):
            key, sep, value = part.strip().partition("=")
            if sep and key == name:
                return value.strip().strip('"')
        return None

    def google_admin(self):
        """The signed-in Google account, if it's an admin."""
        st = self.student()
        return st if account_is_admin(st) else None

    def is_admin(self):
        return valid_session(self.cookie("em_session")) or bool(self.google_admin())

    def require_admin(self):
        if not self.is_admin():
            raise ApiError(401, "Please sign in.")
        # The Google sign-in cookie is SameSite=Lax, so also refuse changes sent from other sites.
        origin = self.headers.get("Origin")
        if self.command != "GET" and origin and origin.split("://", 1)[-1] != self.headers.get("Host", ""):
            raise ApiError(403, "Request from another site refused.")

    def student(self):
        token = self.cookie("em_student")
        return student_from_token(token) if token else None

    def require_student(self):
        st = self.student()
        if not st:
            raise ApiError(401, "Sign in with your school Google account to order.")
        return st

    def serve_static(self, path):
        if path.startswith("/uploads/"):
            name = path[len("/uploads/"):]
            target = UPLOAD_DIR / name
            if re.fullmatch(r"[A-Za-z0-9_-]+\.(jpg|png|webp|gif)", name) and target.is_file():
                data = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", UPLOAD_MIME[name.rsplit(".", 1)[1]])
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.end_headers()
                self.wfile.write(data)
                return
            path = "/__missing__"
        if path in ("/", ""):
            path = "/index.html"
        elif path in ("/admin", "/admin/"):
            path = "/admin.html"
        target = (STATIC / path.lstrip("/")).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Not found")
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def dispatch(self, method):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        if not path.startswith("/api/"):
            if method != "GET":
                raise ApiError(405, "Method not allowed.")
            return self.serve_static(url.path)
        query = parse_qs(url.query)

        for pattern, handler_method, fn in ROUTES:
            m = re.fullmatch(pattern, path)
            if m and handler_method == method:
                return fn(self, query, *m.groups())
        raise ApiError(404, "Not found.")

    def handle_any(self, method):
        try:
            self.dispatch(method)
        except ApiError as e:
            self.send_json(e.status, {"error": e.message})
        except Exception as e:  # noqa: BLE001
            print("ERROR", repr(e))
            self.send_json(500, {"error": "Something went wrong on the server."})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PATCH(self):
        self.handle_any("PATCH")

    def do_DELETE(self):
        self.handle_any("DELETE")


# ---------- routes ----------

def require_viewer(h):
    """The store is only for signed-in students (and staff)."""
    if not (h.student() or h.is_admin()):
        raise ApiError(401, "Sign in with your school Google account to see the store.")


def get_products(h, q):
    require_viewer(h)
    rows = DB.execute("SELECT * FROM products WHERE active=1 ORDER BY category, name").fetchall()
    h.send_json(200, {"products": [product_dict(r) for r in rows]})


def create_order(h, q):
    student = h.require_student()
    body = h.read_body()
    name, contact = student["name"] or student["email"], student["email"]
    note = clean_text(body.get("note"), "Note", 300)
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise ApiError(400, "Your cart is empty.")
    wanted = {}
    for it in items:
        try:
            pid, qty = int(it["id"]), int(it["qty"])
        except (KeyError, TypeError, ValueError):
            raise ApiError(400, "Invalid cart item.")
        if qty < 1 or qty > 50:
            raise ApiError(400, "Quantities must be between 1 and 50.")
        wanted[pid] = wanted.get(pid, 0) + qty

    now = time.time()
    with db_lock, DB:
        lines, total = [], 0
        for pid, qty in wanted.items():
            p = DB.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
            if not p:
                raise ApiError(409, "An item in your cart is no longer available.")
            if p["stock"] - on_hold(pid) < qty:
                left = max(0, p["stock"] - on_hold(pid))
                msg = f"Only {left} {p['name']} left." if left else f"{p['name']} just sold out."
                raise ApiError(409, msg)
            lines.append((p, qty))
            total += p["price_cents"] * qty
        limit = weekly_limit()
        if limit:
            used, qty = items_this_week(student["id"]), sum(wanted.values())
            if used + qty > limit:
                left = max(0, limit - used)
                each = "item" if limit == 1 else "items"
                raise ApiError(409, (f"You can buy {limit} {each} a week. " +
                    (f"You have {left} left this week." if left else "You've used this week's — it resets on Monday.")))
        balance = DB.execute("SELECT balance_cents FROM students WHERE id=?", (student["id"],)).fetchone()["balance_cents"]
        if balance < total:
            raise ApiError(409, f"Not enough balance: this order is ${total / 100:.2f} and you have ${balance / 100:.2f}. "
                                "Ask the Emporium staff to add money to your account.")
        code = new_code()
        while DB.execute("SELECT 1 FROM orders WHERE code=?", (code,)).fetchone():
            code = new_code()
        cur = DB.execute(
            "INSERT INTO orders (code, customer_name, customer_contact, note, status, total_cents, created_at, updated_at, student_id)"
            " VALUES (?,?,?,?, 'new', ?,?,?,?)",  # 'new' = waiting for staff approval
            (code, name, contact, note, total, now, now, student["id"]),
        )
        oid = cur.lastrowid
        move_balance(student["id"], -total, "order", f"Order {code}", oid)
        for p, qty in lines:
            DB.execute(
                "INSERT INTO order_items (order_id, product_id, name, price_cents, qty) VALUES (?,?,?,?,?)",
                (oid, p["id"], p["name"], p["price_cents"], qty),
            )
        DB.execute("UPDATE orders SET stock_taken=0 WHERE id=?", (oid,))  # stock comes off when staff approve
        row = DB.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        bal = DB.execute("SELECT balance_cents FROM students WHERE id=?", (student["id"],)).fetchone()["balance_cents"]
        h.send_json(201, {"order": order_dict(row), "balance_cents": bal})


def my_orders(h, q):
    student = h.require_student()
    rows = DB.execute("SELECT * FROM orders WHERE student_id=? ORDER BY created_at DESC LIMIT 50", (student["id"],)).fetchall()
    h.send_json(200, {"orders": [order_dict(r) for r in rows]})


# ---------- student sign-in ----------

def google_client_id():
    return os.environ.get("GOOGLE_CLIENT_ID") or get_setting("google_client_id") or ""


def public_config(h, q):
    h.send_json(200, {"google_client_id": google_client_id(), "student_domain": STUDENT_DOMAIN})


def verify_google_credential(credential):
    """Check a Sign in with Google ID token with Google, and that it's a verified school account."""
    client_id = google_client_id()
    if not client_id:
        raise ApiError(503, "Student sign-in isn't set up yet. Staff: add the Google client ID in Settings.")
    if not isinstance(credential, str) or not (20 < len(credential) < 5000):
        raise ApiError(400, "Sign-in failed. Please try again.")
    url = GOOGLE_TOKENINFO_URL + "?" + urllib.parse.urlencode({"id_token": credential})
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            claims = json.loads(resp.read())
    except urllib.error.HTTPError:
        raise ApiError(401, "Google couldn't confirm that sign-in. Please try again.")
    except (urllib.error.URLError, OSError, ValueError):
        raise ApiError(502, "Couldn't reach Google to check the sign-in. Try again in a moment.")
    if claims.get("aud") != client_id or claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise ApiError(401, "That sign-in wasn't meant for this site.")
    if int(claims.get("exp", 0)) < time.time():
        raise ApiError(401, "That sign-in expired. Please try again.")
    email = str(claims.get("email", "")).lower()
    if str(claims.get("email_verified")).lower() != "true" or not email.endswith("@" + STUDENT_DOMAIN):
        raise ApiError(403, f"Only @{STUDENT_DOMAIN} accounts can use the Emporium. Sign in with your school Google account.")
    if claims.get("hd") and str(claims["hd"]).lower() != STUDENT_DOMAIN:
        raise ApiError(403, f"Only @{STUDENT_DOMAIN} accounts can use the Emporium.")
    return claims


def auth_google(h, q):
    claims = verify_google_credential(h.read_body().get("credential"))
    now = time.time()
    name = str(claims.get("name") or claims.get("given_name") or "")[:80]
    picture = str(claims.get("picture") or "")
    picture = picture if picture.startswith("https://") else ""
    with db_lock, DB:
        DB.execute(
            "INSERT INTO students (google_sub, email, name, picture, created_at, last_login) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(google_sub) DO UPDATE SET email=excluded.email, name=excluded.name, picture=excluded.picture, last_login=excluded.last_login",
            (str(claims["sub"]), str(claims["email"]).lower(), name, picture[:500], now, now),
        )
        st = DB.execute("SELECT * FROM students WHERE google_sub=?", (str(claims["sub"]),)).fetchone()
    secure = "; Secure" if os.environ.get("SECURE_COOKIES") == "1" else ""
    cookie = f"em_student={make_student_session(st['id'])}; Path=/; HttpOnly; SameSite=Lax; Max-Age={STUDENT_SESSION_DAYS * 86400}{secure}"
    h.send_json(200, {"student": student_dict(st)}, {"Set-Cookie": cookie})


def auth_logout(h, q):
    h.send_json(200, {"ok": True}, {"Set-Cookie": "em_student=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})


def me(h, q):
    st = h.student()
    h.send_json(200, {"student": student_dict(st) if st else None, "weekly": weekly_status(st["id"]) if st else None})


def session_cookie():
    secure = "; Secure" if os.environ.get("SECURE_COOKIES") == "1" else ""
    return {"Set-Cookie": f"em_session={make_session()}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_HOURS*3600}{secure}"}


def valid_new_password(pw):
    if not isinstance(pw, str) or len(pw) < 8:
        raise ApiError(400, "Use at least 8 characters.")
    if len(pw) > 200:
        raise ApiError(400, "That password is too long.")
    return pw


def admin_login(h, q):
    body = h.read_body()
    if needs_setup():
        raise ApiError(409, "No staff password yet — create one first.")
    if not check_password(str(body.get("password", ""))):
        time.sleep(0.5)
        raise ApiError(401, "Wrong password.")
    h.send_json(200, {"ok": True}, session_cookie())


def admin_setup(h, q):
    """First run only: whoever opens /admin first chooses the staff password. When admin Google
    accounts are configured, only they can create it, so nobody else can claim the staff page."""
    if (ADMIN_EMAILS or DB.execute("SELECT 1 FROM students WHERE is_admin=1").fetchone()) and not h.google_admin():
        raise ApiError(403, "Sign in with an admin Google account first.")
    pw = valid_new_password(h.read_body().get("password"))
    with db_lock, DB:
        if ENV_ADMIN_PASSWORD or DB.execute("SELECT 1 FROM settings WHERE key='admin_pw_hash'").fetchone():
            raise ApiError(409, "A staff password already exists.")
        DB.execute("INSERT INTO settings (key, value) VALUES ('admin_pw_hash', ?)", (hash_password(pw),))
    h.send_json(200, {"ok": True}, session_cookie())


def admin_change_password(h, q):
    h.require_admin()
    if ENV_ADMIN_PASSWORD:
        raise ApiError(409, "This password is set by the server's ADMIN_PASSWORD setting; change it there.")
    body = h.read_body()
    if needs_setup():
        pass  # a Google admin is setting the first staff password; there's no current one
    elif not check_password(str(body.get("current", ""))):
        time.sleep(0.5)
        raise ApiError(403, "Current password is wrong.")
    set_setting("admin_pw_hash", hash_password(valid_new_password(body.get("new"))))
    h.send_json(200, {"ok": True}, session_cookie())  # other devices get signed out


def admin_logout(h, q):
    h.send_json(200, {"ok": True}, [
        ("Set-Cookie", "em_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"),
        ("Set-Cookie", "em_student=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"),
    ])


def admin_me(h, q):
    ga = h.google_admin()
    h.send_json(200, {
        "signed_in": h.is_admin(),
        "needs_setup": needs_setup(),
        "google_admins": bool(ADMIN_EMAILS) or bool(DB.execute("SELECT 1 FROM students WHERE is_admin=1").fetchone()),
        "google_admin": ga["email"] if ga else None,
    })


def admin_products(h, q):
    h.require_admin()
    rows = DB.execute("SELECT * FROM products ORDER BY category, name").fetchall()
    h.send_json(200, {"products": [product_dict(r, admin=True) for r in rows]})


def admin_create_product(h, q):
    h.require_admin()
    data = parse_product(h.read_body())
    data.setdefault("stock", 0)
    data.setdefault("active", 1)
    data.setdefault("category", "Other")
    data["created_at"] = time.time()
    cols = ", ".join(data)
    with db_lock, DB:
        cur = DB.execute(f"INSERT INTO products ({cols}) VALUES ({','.join('?' * len(data))})", tuple(data.values()))
        row = DB.execute("SELECT * FROM products WHERE id=?", (cur.lastrowid,)).fetchone()
    h.send_json(201, {"product": product_dict(row, admin=True)})


def admin_update_product(h, q, pid):
    h.require_admin()
    data = parse_product(h.read_body(), partial=True)
    if not data:
        raise ApiError(400, "Nothing to update.")
    with db_lock, DB:
        if not DB.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone():
            raise ApiError(404, "Product not found.")
        old_image = DB.execute("SELECT image_url FROM products WHERE id=?", (pid,)).fetchone()["image_url"]
        sets = ", ".join(f"{k}=?" for k in data)
        DB.execute(f"UPDATE products SET {sets} WHERE id=?", (*data.values(), pid))
        if "image_url" in data and data["image_url"] != old_image:
            remove_uploaded(old_image)
        row = DB.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    h.send_json(200, {"product": product_dict(row, admin=True)})


def remove_uploaded(url):
    if url and url.startswith("/uploads/"):
        try:
            (UPLOAD_DIR / Path(url).name).unlink()
        except OSError:
            pass


def admin_delete_product(h, q, pid):
    h.require_admin()
    with db_lock, DB:
        row = DB.execute("SELECT image_url FROM products WHERE id=?", (pid,)).fetchone()
        DB.execute("DELETE FROM products WHERE id=?", (pid,))
    if row:
        remove_uploaded(row["image_url"])
    h.send_json(200, {"ok": True})


def save_upload(h, pid, column, types, max_bytes, kind):
    """Body is the raw file bytes; Content-Type says which kind."""
    h.require_admin()
    ext = types.get((h.headers.get("Content-Type") or "").split(";")[0].strip().lower())
    if not ext:
        raise ApiError(415, "Photos must be JPEG, PNG, WebP or GIF.")
    length = int(h.headers.get("Content-Length") or 0)
    if not length:
        raise ApiError(400, f"No {kind} received.")
    if length > max_bytes:
        raise ApiError(413, f"That {kind} is too big (max {max_bytes // (1024 * 1024)} MB).")
    data = h.rfile.read(length)
    with db_lock, DB:
        row = DB.execute(f"SELECT {column} FROM products WHERE id=?", (pid,)).fetchone()
        if not row:
            raise ApiError(404, "Product not found.")
        name = f"p{pid}-{secrets.token_hex(4)}{ext}"
        (UPLOAD_DIR / name).write_bytes(data)
        try:
            DB.execute(f"UPDATE products SET {column}=? WHERE id=?", ("/uploads/" + name, pid))
            updated = DB.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        except Exception:
            (UPLOAD_DIR / name).unlink(missing_ok=True)  # don't leave an orphaned file behind
            raise
    remove_uploaded(row[column])
    h.send_json(200, {"product": product_dict(updated, admin=True)})


def admin_upload_image(h, q, pid):
    save_upload(h, pid, "image_url", IMAGE_TYPES, MAX_IMAGE_BYTES, "photo")


# ---------- auto-listing ----------

def ai_key():
    return os.environ.get("ANTHROPIC_API_KEY") or get_setting("anthropic_api_key")


def admin_get_settings(h, q):
    h.require_admin()
    key = ai_key()
    h.send_json(200, {
        "ai_key_set": bool(key),
        "ai_key_hint": ("…" + key[-4:]) if key else "",
        "ai_key_from_env": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "google_client_id": google_client_id(),
        "google_client_id_from_env": bool(os.environ.get("GOOGLE_CLIENT_ID")),
        "student_domain": STUDENT_DOMAIN,
        "weekly_item_limit": weekly_limit(),
    })


def admin_save_settings(h, q):
    h.require_admin()
    body = h.read_body()
    if "ai_key" in body:
        key = str(body.get("ai_key") or "").strip()
        if key and (len(key) > 300 or not key.startswith("sk-")):
            raise ApiError(400, "That doesn't look like an Anthropic API key (they start with sk-).")
        with db_lock, DB:
            if key:
                DB.execute("INSERT INTO settings (key, value) VALUES ('anthropic_api_key', ?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key,))
            else:
                DB.execute("DELETE FROM settings WHERE key='anthropic_api_key'")
    if "weekly_item_limit" in body:
        try:
            n = int(body.get("weekly_item_limit"))
        except (TypeError, ValueError):
            raise ApiError(400, "Items per week must be a whole number (0 means no limit).")
        if not 0 <= n <= 100:
            raise ApiError(400, "Items per week must be between 0 and 100.")
        set_setting("weekly_item_limit", str(n))
    if "google_client_id" in body:
        cid = str(body.get("google_client_id") or "").strip()
        if cid and not re.fullmatch(r"[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com", cid):
            raise ApiError(400, "That doesn't look like a Google client ID (it ends in .apps.googleusercontent.com).")
        with db_lock, DB:
            if cid:
                DB.execute("INSERT INTO settings (key, value) VALUES ('google_client_id', ?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (cid,))
            else:
                DB.execute("DELETE FROM settings WHERE key='google_client_id'")
    admin_get_settings(h, q)


def admin_lookup(h, q):
    h.require_admin()
    query = clean_text(h.read_body().get("query"), "Product", 500, required=True)
    key = ai_key()
    if not key:
        raise ApiError(409, "Add an AI key first (Settings on the staff page).")
    import lookup
    cats = [r["category"] for r in DB.execute("SELECT DISTINCT category FROM products")]
    try:
        listing = lookup.lookup_product(query, key, cats)
    except lookup.ListingLookupError as e:
        raise ApiError(502, str(e))
    h.send_json(200, {"listing": listing})


def _public_host(host):
    """True only if every address the host resolves to is on the public internet."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            return False
    return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def admin_fetch_image(h, q):
    """Fetch a product photo from the web for the staff page (browsers block most cross-site
    image downloads). Staff-only, public hosts only, images only, size-capped."""
    h.require_admin()
    url = (q.get("url") or [""])[0]
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(4):  # follow up to 3 redirects, re-checking each hop
        parts = urllib.parse.urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or not _public_host(parts.hostname):
            raise ApiError(400, "That isn't a public web address.")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (EmporiumStore image fetch)", "Accept": "image/*"})
        try:
            resp = opener.open(req, timeout=15)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                continue
            raise ApiError(502, f"The image site answered {e.code}.")
        except (urllib.error.URLError, OSError):
            raise ApiError(502, "Couldn't download that image from this server.")
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype not in IMAGE_TYPES:
            raise ApiError(415, "That link isn't a JPEG, PNG, WebP or GIF image.")
        data = resp.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise ApiError(413, "That image is too big (max 8 MB).")
        h.send_response(200)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        h.wfile.write(data)
        return
    raise ApiError(502, "Too many redirects.")


def admin_orders(h, q):
    h.require_admin()
    status = (q.get("status") or [""])[0]
    if status and status not in ORDER_STATUSES:
        raise ApiError(400, "Unknown status.")
    if status:
        rows = DB.execute("SELECT * FROM orders WHERE status=? ORDER BY created_at DESC LIMIT 300", (status,)).fetchall()
    else:
        rows = DB.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 300").fetchall()
    counts = {s: 0 for s in ORDER_STATUSES}
    for r in DB.execute("SELECT status, COUNT(*) c FROM orders GROUP BY status"):
        counts[r["status"]] = r["c"]
    h.send_json(200, {"orders": [order_dict(r, include_contact=True) for r in rows], "counts": counts})


def admin_update_order(h, q, oid):
    h.require_admin()
    status = h.read_body().get("status")
    if status not in ORDER_STATUSES:
        raise ApiError(400, "Unknown status.")
    with db_lock, DB:
        row = DB.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not row:
            raise ApiError(404, "Order not found.")
        old = row["status"]
        items = DB.execute("SELECT product_id, name, qty FROM order_items WHERE order_id=?", (oid,)).fetchall()
        # Money: held from the moment the student orders; cancelling (declining) refunds it.
        if old != "cancelled" and status == "cancelled":
            if row["student_id"]:
                move_balance(row["student_id"], row["total_cents"], "refund", f"Order {row['code']} cancelled", int(oid))
        elif old == "cancelled" and status != "cancelled":
            if row["student_id"]:
                try:
                    move_balance(row["student_id"], -row["total_cents"], "order", f"Order {row['code']} reopened", int(oid))
                except ApiError:
                    raise ApiError(409, "The student doesn't have enough balance to reopen this order.")
        # Stock: comes off the shelf only once staff approve (ready / picked up).
        take = status in ("ready", "picked_up")
        if take and not row["stock_taken"]:
            for it in items:
                p = DB.execute("SELECT stock FROM products WHERE id=?", (it["product_id"],)).fetchone()
                if p and p["stock"] < it["qty"]:
                    raise ApiError(409, f"Not enough {it['name']} in stock to approve this (have {p['stock']}).")
            for it in items:
                DB.execute("UPDATE products SET stock = stock - ? WHERE id=?", (it["qty"], it["product_id"]))
            DB.execute("UPDATE orders SET stock_taken=1 WHERE id=?", (oid,))
        elif not take and row["stock_taken"]:
            for it in items:
                DB.execute("UPDATE products SET stock = stock + ? WHERE id=?", (it["qty"], it["product_id"]))
            DB.execute("UPDATE orders SET stock_taken=0 WHERE id=?", (oid,))
        if status == "new" and old == "cancelled":
            # Back to waiting: make sure the items are still there to hold.
            for it in items:
                p = DB.execute("SELECT stock FROM products WHERE id=?", (it["product_id"],)).fetchone()
                if p and p["stock"] - on_hold(it["product_id"]) < it["qty"]:
                    raise ApiError(409, f"Not enough {it['name']} left to reopen this order.")
        DB.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?", (status, time.time(), oid))
        row = DB.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    h.send_json(200, {"order": order_dict(row, include_contact=True)})


# ---------- product requests ----------
# Students ask for things the store doesn't carry. Others tap "Me too", so staff can see what's wanted.

REQUEST_STATUSES = ("open", "added", "declined")
REQUESTS_PER_DAY = 3


def _request_rows(student_id, where="", args=(), limit=200):
    return DB.execute(
        f"""SELECT r.*, s.name AS student_name, s.email AS student_email,
                   (SELECT COUNT(*) FROM request_votes v WHERE v.request_id=r.id) AS votes,
                   EXISTS(SELECT 1 FROM request_votes v WHERE v.request_id=r.id AND v.student_id=?) AS mine
            FROM requests r JOIN students s ON s.id=r.student_id {where}
            ORDER BY (r.status='open') DESC, votes DESC, r.created_at DESC LIMIT {int(limit)}""",
        (student_id or 0, *args),
    ).fetchall()


def request_dict(r, staff=False):
    d = {"id": r["id"], "name": r["name"], "note": r["note"], "status": r["status"],
         "votes": r["votes"], "mine": bool(r["mine"]), "created_at": r["created_at"]}
    if staff:
        d.update(student_name=r["student_name"], student_email=r["student_email"], updated_at=r["updated_at"])
    return d


def list_requests(h, q):
    require_viewer(h)
    st = h.student()
    rows = _request_rows(st["id"] if st else None, "WHERE r.status != 'declined'", limit=100)
    h.send_json(200, {"requests": [request_dict(r) for r in rows]})


def create_request(h, q):
    st = h.student()
    if not st:
        raise ApiError(401, "Sign in with your school Google account to request something.")
    body = h.read_body()
    name = " ".join(str(body.get("name") or "").split())[:100]
    note = str(body.get("note") or "").strip()[:300]
    if len(name) < 2:
        raise ApiError(400, "Tell us what you'd like the store to get.")
    now = time.time()
    with db_lock, DB:
        same = DB.execute("SELECT id FROM requests WHERE status='open' AND lower(name)=lower(?)", (name,)).fetchone()
        if same:  # someone already asked: count it as a "me too"
            DB.execute("INSERT OR IGNORE INTO request_votes (request_id, student_id, created_at) VALUES (?,?,?)", (same["id"], st["id"], now))
            rid, merged = same["id"], True
        else:
            recent = DB.execute("SELECT COUNT(*) FROM requests WHERE student_id=? AND created_at>?", (st["id"], now - 86400)).fetchone()[0]
            if recent >= REQUESTS_PER_DAY:
                raise ApiError(429, f"You can make {REQUESTS_PER_DAY} requests a day. Tap \"Me too\" on ones you like instead.")
            rid = DB.execute("INSERT INTO requests (student_id, name, note, created_at, updated_at) VALUES (?,?,?,?,?)",
                             (st["id"], name, note, now, now)).lastrowid
            DB.execute("INSERT INTO request_votes (request_id, student_id, created_at) VALUES (?,?,?)", (rid, st["id"], now))
            merged = False
        row = _request_rows(st["id"], "WHERE r.id=?", (rid,), 1)[0]
    h.send_json(200, {"request": request_dict(row), "merged": merged})


def vote_request(h, q, rid):
    st = h.student()
    if not st:
        raise ApiError(401, "Sign in to say you want this too.")
    with db_lock, DB:
        r = DB.execute("SELECT status FROM requests WHERE id=?", (rid,)).fetchone()
        if not r or r["status"] == "declined":
            raise ApiError(404, "Request not found.")
        if DB.execute("SELECT 1 FROM request_votes WHERE request_id=? AND student_id=?", (rid, st["id"])).fetchone():
            DB.execute("DELETE FROM request_votes WHERE request_id=? AND student_id=?", (rid, st["id"]))
        else:
            DB.execute("INSERT INTO request_votes (request_id, student_id, created_at) VALUES (?,?,?)", (rid, st["id"], time.time()))
        row = _request_rows(st["id"], "WHERE r.id=?", (rid,), 1)[0]
    h.send_json(200, {"request": request_dict(row)})


def admin_requests(h, q):
    h.require_admin()
    rows = _request_rows(None, limit=500)
    open_count = DB.execute("SELECT COUNT(*) FROM requests WHERE status='open'").fetchone()[0]
    h.send_json(200, {"requests": [request_dict(r, staff=True) for r in rows], "open": open_count})


def admin_update_request(h, q, rid):
    h.require_admin()
    body = h.read_body()
    with db_lock, DB:
        if body.get("delete"):
            DB.execute("DELETE FROM request_votes WHERE request_id=?", (rid,))
            DB.execute("DELETE FROM requests WHERE id=?", (rid,))
            return h.send_json(200, {"ok": True})
        status = body.get("status")
        if status not in REQUEST_STATUSES:
            raise ApiError(400, "Unknown status.")
        if not DB.execute("UPDATE requests SET status=?, updated_at=? WHERE id=?", (status, time.time(), rid)).rowcount:
            raise ApiError(404, "Request not found.")
    h.send_json(200, {"ok": True})


def admin_students(h, q):
    h.require_admin()
    term = (q.get("q") or [""])[0].strip().lower()
    if term:
        like = f"%{term}%"
        rows = DB.execute("SELECT * FROM students WHERE lower(email) LIKE ? OR lower(name) LIKE ? ORDER BY lower(COALESCE(NULLIF(name, ''), email))", (like, like)).fetchall()
    else:
        rows = DB.execute("SELECT * FROM students ORDER BY lower(COALESCE(NULLIF(name, ''), email))").fetchall()
    total = DB.execute("SELECT COUNT(*) c, COALESCE(SUM(balance_cents),0) b FROM students").fetchone()
    h.send_json(200, {"students": [student_dict(r) for r in rows], "count": total["c"], "total_balance_cents": total["b"]})


def admin_student(h, q, sid):
    h.require_admin()
    st = DB.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
    if not st:
        raise ApiError(404, "Student not found.")
    ledger = DB.execute("SELECT * FROM ledger WHERE student_id=? ORDER BY id DESC LIMIT 100", (sid,)).fetchall()
    orders = DB.execute("SELECT * FROM orders WHERE student_id=? ORDER BY created_at DESC LIMIT 50", (sid,)).fetchall()
    h.send_json(200, {"student": student_dict(st), "ledger": [dict(r) for r in ledger],
                      "orders": [order_dict(r, include_contact=True) for r in orders]})


def admin_student_balance(h, q, sid):
    """Staff add money (e.g. cash handed over) or correct a mistake (negative amount)."""
    h.require_admin()
    body = h.read_body()
    try:
        cents = round(float(body.get("amount")) * 100)
    except (TypeError, ValueError):
        raise ApiError(400, "Amount must be a number, like 10 or 2.50.")
    if cents == 0 or abs(cents) > MAX_BALANCE_CENTS:
        raise ApiError(400, "Enter an amount between $0.01 and $1,000.")
    note = clean_text(body.get("note"), "Note", 200)
    with db_lock, DB:
        try:
            move_balance(int(sid), cents, "deposit" if cents > 0 else "adjust", note)
        except ApiError as e:
            if e.message == "Not enough balance.":
                raise ApiError(409, "That would make the balance negative.")
            raise
        st = DB.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
    h.send_json(200, {"student": student_dict(st)})


def admin_student_role(h, q, sid):
    """An admin makes another account an admin, or takes it away."""
    h.require_admin()
    make = bool(h.read_body().get("admin"))
    with db_lock, DB:
        st = DB.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
        if not st:
            raise ApiError(404, "Student not found.")
        if not make and st["email"].lower() in ADMIN_EMAILS:
            raise ApiError(409, "This admin is set on the server and can't be removed here.")
        me = h.google_admin()
        if not make and me and me["id"] == st["id"]:
            raise ApiError(409, "You can't remove your own admin access.")
        DB.execute("UPDATE students SET is_admin=? WHERE id=?", (1 if make else 0, sid))
        st = DB.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
    h.send_json(200, {"student": student_dict(st)})


ROUTES = [
    (r"/api/products", "GET", get_products),
    (r"/api/orders", "POST", create_order),
    (r"/api/me/orders", "GET", my_orders),
    (r"/api/me", "GET", me),
    (r"/api/config", "GET", public_config),
    (r"/api/auth/google", "POST", auth_google),
    (r"/api/auth/logout", "POST", auth_logout),
    (r"/api/requests", "GET", list_requests),
    (r"/api/requests", "POST", create_request),
    (r"/api/requests/(\d+)/vote", "POST", vote_request),
    (r"/api/admin/requests", "GET", admin_requests),
    (r"/api/admin/requests/(\d+)", "PATCH", admin_update_request),
    (r"/api/admin/students", "GET", admin_students),
    (r"/api/admin/students/(\d+)", "GET", admin_student),
    (r"/api/admin/students/(\d+)/balance", "POST", admin_student_balance),
    (r"/api/admin/students/(\d+)/role", "POST", admin_student_role),
    (r"/api/admin/login", "POST", admin_login),
    (r"/api/admin/logout", "POST", admin_logout),
    (r"/api/admin/me", "GET", admin_me),
    (r"/api/admin/setup", "POST", admin_setup),
    (r"/api/admin/password", "POST", admin_change_password),
    (r"/api/admin/products", "GET", admin_products),
    (r"/api/admin/products", "POST", admin_create_product),
    (r"/api/admin/products/(\d+)", "PATCH", admin_update_product),
    (r"/api/admin/products/(\d+)", "DELETE", admin_delete_product),
    (r"/api/admin/products/(\d+)/image", "POST", admin_upload_image),
    (r"/api/admin/settings", "GET", admin_get_settings),
    (r"/api/admin/settings", "POST", admin_save_settings),
    (r"/api/admin/lookup", "POST", admin_lookup),
    (r"/api/admin/fetch-image", "GET", admin_fetch_image),
    (r"/api/admin/orders", "GET", admin_orders),
    (r"/api/admin/orders/(\d+)", "PATCH", admin_update_order),
]


if __name__ == "__main__":
    init_db()
    if needs_setup():
        print("No staff password yet: open /admin to create one.")
    class Server(ThreadingHTTPServer):
        request_queue_size = 128  # default is 5; a lunchtime rush needs more room
        daemon_threads = True

    server = Server(("0.0.0.0", PORT), Handler)
    print(f"The Emporium is open at http://localhost:{PORT}  (staff: /admin)")
    server.serve_forever()
