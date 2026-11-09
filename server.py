"""The Emporium — school store pickup ordering + staff inventory.

Zero dependencies: runs on the Python 3.9+ standard library.
    ADMIN_PASSWORD=yourpassword python3 server.py
Then open http://localhost:8000 (store) and http://localhost:8000/admin (staff).
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from http import cookies
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
MAX_MODEL_BYTES = 30 * 1024 * 1024
MODEL_TYPES = {"model/gltf-binary": ".glb", "application/octet-stream": ".glb"}
UPLOAD_MIME = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif", "glb": "model/gltf-binary"}
PORT = int(os.environ.get("PORT", "8000"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "emporium")
SECRET = os.environ.get("SECRET_KEY") or hashlib.sha256(("emporium:" + ADMIN_PASSWORD).encode()).hexdigest()
SESSION_HOURS = 12
ORDER_STATUSES = ("new", "ready", "picked_up", "cancelled")

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
        cols = {r["name"] for r in DB.execute("PRAGMA table_info(products)")}
        if "model_url" not in cols:
            DB.execute("ALTER TABLE products ADD COLUMN model_url TEXT NOT NULL DEFAULT ''")
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

def product_dict(row, admin=False):
    d = {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "category": row["category"],
        "price_cents": row["price_cents"],
        "stock": row["stock"],
        "image_url": row["image_url"],
        "model_url": row["model_url"],
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
    return d


def new_code():
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "EM-" + "".join(secrets.choice(alphabet) for _ in range(5))


def sign(value):
    return hmac.new(SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()


def make_session():
    exp = str(int(time.time() + SESSION_HOURS * 3600))
    return exp + "." + sign(exp)


def valid_session(token):
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    return hmac.compare_digest(sig, sign(exp)) and exp.isdigit() and int(exp) > time.time()


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
    if "model_url" in body:
        if body.get("model_url"):
            raise ApiError(400, "Upload 3D models with the 3D button.")
        out["model_url"] = ""
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
        for k, v in (extra_headers or {}).items():
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

    def is_admin(self):
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return valid_session(jar["em_session"].value if "em_session" in jar else None)

    def require_admin(self):
        if not self.is_admin():
            raise ApiError(401, "Please sign in.")

    def serve_static(self, path):
        if path.startswith("/uploads/"):
            name = path[len("/uploads/"):]
            target = UPLOAD_DIR / name
            if re.fullmatch(r"[A-Za-z0-9_-]+\.(jpg|png|webp|gif|glb)", name) and target.is_file():
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

def get_products(h, q):
    rows = DB.execute("SELECT * FROM products WHERE active=1 ORDER BY category, name").fetchall()
    h.send_json(200, {"products": [product_dict(r) for r in rows]})


def create_order(h, q):
    body = h.read_body()
    name = clean_text(body.get("name"), "Your name", 80, required=True)
    contact = clean_text(body.get("contact"), "Contact", 120)
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
            if p["stock"] < qty:
                left = p["stock"]
                msg = f"Only {left} {p['name']} left." if left else f"{p['name']} just sold out."
                raise ApiError(409, msg)
            lines.append((p, qty))
            total += p["price_cents"] * qty
        code = new_code()
        while DB.execute("SELECT 1 FROM orders WHERE code=?", (code,)).fetchone():
            code = new_code()
        cur = DB.execute(
            "INSERT INTO orders (code, customer_name, customer_contact, note, status, total_cents, created_at, updated_at)"
            " VALUES (?,?,?,?, 'new', ?,?,?)",
            (code, name, contact, note, total, now, now),
        )
        oid = cur.lastrowid
        for p, qty in lines:
            DB.execute(
                "INSERT INTO order_items (order_id, product_id, name, price_cents, qty) VALUES (?,?,?,?,?)",
                (oid, p["id"], p["name"], p["price_cents"], qty),
            )
            DB.execute("UPDATE products SET stock = stock - ? WHERE id=?", (qty, p["id"]))
        row = DB.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        h.send_json(201, {"order": order_dict(row)})


def get_order(h, q, code):
    row = DB.execute("SELECT * FROM orders WHERE code=?", (code.upper(),)).fetchone()
    if not row:
        raise ApiError(404, "We couldn't find that order code.")
    h.send_json(200, {"order": order_dict(row)})


def admin_login(h, q):
    body = h.read_body()
    if not hmac.compare_digest(str(body.get("password", "")), ADMIN_PASSWORD):
        time.sleep(0.5)
        raise ApiError(401, "Wrong password.")
    secure = "; Secure" if os.environ.get("SECURE_COOKIES") == "1" else ""
    cookie = f"em_session={make_session()}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_HOURS*3600}{secure}"
    h.send_json(200, {"ok": True}, {"Set-Cookie": cookie})


def admin_logout(h, q):
    h.send_json(200, {"ok": True}, {"Set-Cookie": "em_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})


def admin_me(h, q):
    h.send_json(200, {"signed_in": h.is_admin()})


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
        old = DB.execute("SELECT image_url, model_url FROM products WHERE id=?", (pid,)).fetchone()
        sets = ", ".join(f"{k}=?" for k in data)
        DB.execute(f"UPDATE products SET {sets} WHERE id=?", (*data.values(), pid))
        for col in ("image_url", "model_url"):
            if col in data and data[col] != old[col]:
                remove_uploaded(old[col])
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
        row = DB.execute("SELECT image_url, model_url FROM products WHERE id=?", (pid,)).fetchone()
        DB.execute("DELETE FROM products WHERE id=?", (pid,))
    if row:
        remove_uploaded(row["image_url"])
        remove_uploaded(row["model_url"])
    h.send_json(200, {"ok": True})


def save_upload(h, pid, column, types, max_bytes, kind, check=None):
    """Body is the raw file bytes; Content-Type says which kind."""
    h.require_admin()
    ext = types.get((h.headers.get("Content-Type") or "").split(";")[0].strip().lower())
    if not ext:
        raise ApiError(415, "Photos must be JPEG, PNG, WebP or GIF." if kind == "photo" else "3D models must be .glb files.")
    length = int(h.headers.get("Content-Length") or 0)
    if not length:
        raise ApiError(400, f"No {kind} received.")
    if length > max_bytes:
        raise ApiError(413, f"That {kind} is too big (max {max_bytes // (1024 * 1024)} MB).")
    data = h.rfile.read(length)
    if check:
        check(data)
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


def check_glb(data):
    if data[:4] != b"glTF":
        raise ApiError(415, "That isn't a .glb 3D model. In your scanning app, export as GLB.")


def admin_upload_model(h, q, pid):
    save_upload(h, pid, "model_url", MODEL_TYPES, MAX_MODEL_BYTES, "3D model", check_glb)


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
        # Cancelling returns items to stock; un-cancelling takes them back out.
        if old != "cancelled" and status == "cancelled":
            for it in DB.execute("SELECT product_id, qty FROM order_items WHERE order_id=?", (oid,)):
                DB.execute("UPDATE products SET stock = stock + ? WHERE id=?", (it["qty"], it["product_id"]))
        elif old == "cancelled" and status != "cancelled":
            for it in DB.execute("SELECT product_id, qty FROM order_items WHERE order_id=?", (oid,)):
                DB.execute("UPDATE products SET stock = MAX(stock - ?, 0) WHERE id=?", (it["qty"], it["product_id"]))
        DB.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?", (status, time.time(), oid))
        row = DB.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    h.send_json(200, {"order": order_dict(row, include_contact=True)})


ROUTES = [
    (r"/api/products", "GET", get_products),
    (r"/api/orders", "POST", create_order),
    (r"/api/orders/([A-Za-z0-9-]{3,20})", "GET", get_order),
    (r"/api/admin/login", "POST", admin_login),
    (r"/api/admin/logout", "POST", admin_logout),
    (r"/api/admin/me", "GET", admin_me),
    (r"/api/admin/products", "GET", admin_products),
    (r"/api/admin/products", "POST", admin_create_product),
    (r"/api/admin/products/(\d+)", "PATCH", admin_update_product),
    (r"/api/admin/products/(\d+)", "DELETE", admin_delete_product),
    (r"/api/admin/products/(\d+)/image", "POST", admin_upload_image),
    (r"/api/admin/products/(\d+)/model", "POST", admin_upload_model),
    (r"/api/admin/orders", "GET", admin_orders),
    (r"/api/admin/orders/(\d+)", "PATCH", admin_update_order),
]


if __name__ == "__main__":
    init_db()
    if "ADMIN_PASSWORD" not in os.environ:
        print("WARNING: ADMIN_PASSWORD not set — staff password is 'emporium'. Set it before going live.")
    class Server(ThreadingHTTPServer):
        request_queue_size = 128  # default is 5; a lunchtime rush needs more room
        daemon_threads = True

    server = Server(("0.0.0.0", PORT), Handler)
    print(f"The Emporium is open at http://localhost:{PORT}  (staff: /admin)")
    server.serve_forever()
