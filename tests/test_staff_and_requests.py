"""Checks for staff sign-in with Google (ADMIN_EMAILS) and product requests.

Runs the real server against a stand-in for Google's token check, so no Google setup is needed:
    python3 tests/test_accounts.py
"""
import http.cookiejar, json, os, socket, subprocess, sys, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CID = "1234567890-abc123.apps.googleusercontent.com"
PAD = "-" + "x" * 30


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


def claims(tok):
    base = {"iss": "https://accounts.google.com", "aud": CID, "exp": str(int(time.time()) + 3600), "email_verified": "true"}
    table = {
        "tok-alice": {"sub": "111", "email": "Alice.Kim@alpha.school", "name": "Alice Kim", "hd": "alpha.school"},
        "tok-bob": {"sub": "222", "email": "bob.lee@alpha.school", "name": "Bob Lee", "hd": "alpha.school"},
        "tok-gmail": {"sub": "333", "email": "kid@gmail.com"},
        "tok-lookalike": {"sub": "444", "email": "x@notalpha.school"},
        "tok-wrongaud": {"sub": "555", "email": "a@alpha.school", "aud": "999-other.apps.googleusercontent.com"},
        "tok-unverified": {"sub": "666", "email": "u@alpha.school", "email_verified": "false"},
        "tok-expired": {"sub": "777", "email": "e@alpha.school", "exp": str(int(time.time()) - 10)},
    }
    for k, v in table.items():
        if tok.startswith(k):
            return {**base, **v}
    return None


class FakeGoogle(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        c = claims(parse_qs(urlparse(self.path).query).get("id_token", [""])[0])
        body = json.dumps(c or {"error": "invalid_token"}).encode()
        self.send_response(200 if c else 400); self.end_headers(); self.wfile.write(body)


def main():
    gport, port = free_port(), free_port()
    threading.Thread(target=HTTPServer(("127.0.0.1", gport), FakeGoogle).serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp()
    env = {**os.environ, "DB_PATH": os.path.join(tmp, "t.db"), "PORT": str(port), "ADMIN_EMAILS": "Bob.Lee@alpha.school, other@alpha.school",
           "GOOGLE_TOKENINFO_URL": f"http://127.0.0.1:{gport}/tokeninfo"}
    env.pop("ADMIN_PASSWORD", None)
    env["GOOGLE_CLIENT_ID"] = CID
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    B = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try: urllib.request.urlopen(B + "/api/config"); break
        except OSError: time.sleep(0.1)
    results = []

    def client():
        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar

    def call(c, path, body=None, method=None):
        req = urllib.request.Request(B + path, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method=method)
        try: r = c.open(req); return r.status, json.load(r)
        except urllib.error.HTTPError as e: return e.code, json.load(e)

    def check(name, cond, detail=""):
        results.append(cond); print("PASS" if cond else "FAIL", name, "" if cond else detail)

    def call_h(c, path, body, headers):
        req = urllib.request.Request(B + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers}, method="POST")
        try: r = c.open(req); return r.status, json.load(r)
        except urllib.error.HTTPError as e: return e.code, json.load(e)

    try:
        alice, _ = client(); bob, bjar = client(); anon, _ = client(); carol, _ = client()
        r = call(anon, "/api/products"); check("store hidden when signed out", r[0] == 401, r)
        r = call(anon, "/api/requests"); check("requests hidden when signed out", r[0] == 401, r)
        me = call(anon, "/api/admin/me")[1]
        check("staff page knows Google admins are on", me["google_admins"] and me["needs_setup"] and not me["signed_in"], me)
        r = call(anon, "/api/admin/setup", {"password": "someone-elses"}); check("stranger can't claim the staff password", r[0] == 403, r)
        call(alice, "/api/auth/google", {"credential": "tok-alice" + PAD})
        r = call(alice, "/api/products"); check("signed-in student sees the store", r[0] == 200 and len(r[1]["products"]) == 30, r)
        r = call(alice, "/api/admin/setup", {"password": "alice-password"}); check("non-admin student can't claim it", r[0] == 403, r)
        check("non-admin student isn't staff", not call(alice, "/api/admin/me")[1]["signed_in"])
        r = call(alice, "/api/admin/students"); check("non-admin student blocked from staff data", r[0] == 401, r)
        r = call(bob, "/api/auth/google", {"credential": "tok-bob" + PAD}); check("admin signs in with Google", r[0] == 200, r)
        me = call(bob, "/api/admin/me")[1]
        check("admin email (any case) is staff", me["signed_in"] and me["google_admin"] == "bob.lee@alpha.school", me)
        studs = call(bob, "/api/admin/students")[1]["students"]
        aid = next(s["id"] for s in studs if s["email"] == "alice.kim@alpha.school")
        r = call(bob, f"/api/admin/students/{aid}/balance", {"amount": 40, "note": "cash"}); check("admin adds $40", r[0] == 200 and r[1]["student"]["balance_cents"] == 4000, r)
        r = call(bob, "/api/admin/products", {"name": "Test thing", "price": "2.50", "stock": "3", "category": "Candy"}); check("admin adds an item", r[0] == 201, r)
        r = call_h(bob, f"/api/admin/students/{aid}/balance", {"amount": 5}, {"Origin": "https://evil.example", "Host": "x"})
        check("change from another site refused", r[0] == 403, r)
        r = call(bob, "/api/admin/password", {"new": "bob-staff-pass"}); check("admin can set first staff password", r[0] == 200, r)
        r = call(anon, "/api/admin/login", {"password": "bob-staff-pass"}); check("staff password then works", r[0] == 200, r)
        call(bob, "/api/admin/logout", {})
        check("staff sign out also ends Google session", not call(bob, "/api/admin/me")[1]["signed_in"])
        call(bob, "/api/auth/google", {"credential": "tok-bob" + PAD})

        # ---- admins recognized by account ----
        r = call(alice, "/api/me")[1]["student"]; check("kid account is not admin", r["is_admin"] is False, r)
        r = call(bob, "/api/me")[1]["student"]; check("admin account recognized on the store", r["is_admin"] and r["admin_locked"], r)
        r = call(alice, f"/api/admin/students/{aid}/role", {"admin": True}); check("kid can't make themself admin", r[0] == 401, r)
        r = call(bob, f"/api/admin/students/{aid}/role", {"admin": True}); check("admin makes Alice an admin", r[1]["student"]["is_admin"], r)
        check("Alice now gets the staff page", call(alice, "/api/admin/me")[1]["signed_in"] and call(alice, "/api/admin/students")[0] == 200)
        bid = next(s["id"] for s in call(bob, "/api/admin/students")[1]["students"] if s["email"] == "bob.lee@alpha.school")
        r = call(alice, f"/api/admin/students/{bid}/role", {"admin": False}); check("server-set admin can't be removed", r[0] == 409, r)
        r = call(alice, f"/api/admin/students/{aid}/role", {"admin": False}); check("can't remove your own admin", r[0] == 409, r)
        r = call(bob, f"/api/admin/students/{aid}/role", {"admin": False}); check("admin removes Alice", not r[1]["student"]["is_admin"], r)
        check("Alice is a kid again", not call(alice, "/api/admin/me")[1]["signed_in"] and call(alice, "/api/admin/students")[0] == 401)

        # ---- requests ----
        r = call(anon, "/api/requests", {"name": "Sour Patch Kids"}); check("must sign in to request", r[0] == 401, r)
        r = call(alice, "/api/requests", {"name": "  Sour   Patch Kids ", "note": "watermelon"}); check("student requests", r[0] == 200 and r[1]["request"]["votes"] == 1 and r[1]["request"]["name"] == "Sour Patch Kids", r)
        rid = r[1]["request"]["id"]
        r = call(bob, "/api/requests", {"name": "sour patch kids"}); check("same request merges as a vote", r[1]["merged"] and r[1]["request"]["votes"] == 2, r)
        r = call(bob, f"/api/requests/{rid}/vote", {}); check("vote toggles off", r[1]["request"]["votes"] == 1 and not r[1]["request"]["mine"], r)
        r = call(anon, f"/api/requests/{rid}/vote", {}); check("anon can't vote", r[0] == 401, r)
        r = call(alice, "/api/requests", {"name": "x"}); check("too-short request rejected", r[0] == 400, r)
        call(alice, "/api/requests", {"name": "Phone stand"}); call(alice, "/api/requests", {"name": "Gel pens"})
        r = call(alice, "/api/requests", {"name": "Fourth thing"}); check("3 new requests a day", r[0] == 429, r)
        lst = call(alice, "/api/requests")[1]["requests"]
        check("public list shows requests, no emails", len(lst) == 3 and "student_email" not in lst[0], lst)
        r = call(alice, "/api/admin/requests"); check("students can't see staff list", r[0] == 401, r)
        r = call(bob, "/api/admin/requests"); check("staff list has who asked", r[1]["open"] == 3 and bool(r[1]["requests"][0]["student_email"]), r)
        gel = next(x["id"] for x in r[1]["requests"] if x["name"] == "Gel pens")
        call(bob, f"/api/admin/requests/{gel}", {"status": "declined"}, "PATCH")
        call(bob, f"/api/admin/requests/{rid}", {"status": "added"}, "PATCH")
        lst = call(alice, "/api/requests")[1]["requests"]
        check("declined hidden, added shown", [x["name"] for x in lst if x["status"] == "added"] == ["Sour Patch Kids"] and all(x["name"] != "Gel pens" for x in lst), lst)
        r = call(alice, f"/api/requests/{gel}/vote", {}); check("can't vote on declined", r[0] == 404, r)
        r = call(bob, f"/api/admin/requests/{gel}", {"status": "bogus"}, "PATCH"); check("bad status rejected", r[0] == 400, r)
        r = call(bob, f"/api/admin/requests/{gel}", {"delete": True}, "PATCH"); check("staff can delete", r[0] == 200 and len(call(bob, "/api/admin/requests")[1]["requests"]) == 2, r)
    finally:
        srv.terminate()
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
