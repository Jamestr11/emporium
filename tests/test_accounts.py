"""End-to-end checks for student accounts, balances and orders.

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
    env = {**os.environ, "DB_PATH": os.path.join(tmp, "t.db"), "PORT": str(port), "ADMIN_PASSWORD": "test-only-pass",
           "GOOGLE_TOKENINFO_URL": f"http://127.0.0.1:{gport}/tokeninfo"}
    env.pop("GOOGLE_CLIENT_ID", None)
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

    try:
        staff, _ = client(); alice, ajar = client(); bob, _ = client(); anon, _ = client()
        check("config empty before setup", call(anon, "/api/config")[1]["google_client_id"] == "")
        r = call(alice, "/api/auth/google", {"credential": "tok-alice" + PAD}); check("sign-in blocked until set up", r[0] == 503, r)
        call(staff, "/api/admin/login", {"password": "test-only-pass"})
        r = call(staff, "/api/admin/settings", {"google_client_id": "not-a-client-id"}); check("bad client id rejected", r[0] == 400, r)
        r = call(staff, "/api/admin/settings", {"google_client_id": CID}); check("client id saved", r[0] == 200, r)
        r = call(staff, "/api/admin/settings", {}); check("weekly limit defaults to 1", r[1]["weekly_item_limit"] == 1, r)
        call(staff, "/api/admin/settings", {"weekly_item_limit": 0})  # off for the balance tests below
        for tok, code, label in [("tok-gmail", 403, "gmail rejected"), ("tok-lookalike", 403, "@notalpha.school rejected"),
                                 ("tok-wrongaud", 401, "token for another site rejected"), ("tok-unverified", 403, "unverified email rejected"),
                                 ("tok-expired", 401, "expired token rejected"), ("forged-token", 401, "forged token rejected")]:
            o, _ = client(); r = call(o, "/api/auth/google", {"credential": tok + PAD}); check(label, r[0] == code, r)
        r = call(alice, "/api/auth/google", {"credential": "tok-alice" + PAD}); check("alpha.school sign-in works", r[0] == 200, r)
        call(bob, "/api/auth/google", {"credential": "tok-bob" + PAD})
        r = call(anon, "/api/orders", {"items": [{"id": 1, "qty": 1}]}); check("anonymous order blocked", r[0] == 401, r)
        r = call(alice, "/api/orders", {"items": [{"id": 1, "qty": 1}]}); check("order blocked with $0 balance", r[0] == 409, r)
        sid = call(alice, "/api/me")[1]["student"]["id"]
        r = call(staff, f"/api/admin/students/{sid}/balance", {"amount": "10", "note": "Cash"}); check("staff adds $10", r[1]["student"]["balance_cents"] == 1000, r)
        r = call(staff, f"/api/admin/students/{sid}/balance", {"amount": "-50"}); check("can't go negative", r[0] == 409, r)
        r = call(alice, f"/api/admin/students/{sid}/balance", {"amount": "100"}); check("students can't add money", r[0] == 401, r)
        price = [p for p in call(anon, "/api/products")[1]["products"] if p["id"] == 1][0]["price_cents"]
        r = call(alice, "/api/orders", {"items": [{"id": 1, "qty": 1}]}); check("order paid from balance", r[0] == 201 and r[1]["balance_cents"] == 1000 - price, r)
        oid = r[1]["order"]["id"]
        check("bob can't see alice's orders", call(bob, "/api/me/orders")[1]["orders"] == [])
        call(staff, f"/api/admin/orders/{oid}", {"status": "cancelled"}, "PATCH")
        check("cancel refunds", call(alice, "/api/me")[1]["student"]["balance_cents"] == 1000)
        call(staff, f"/api/admin/orders/{oid}", {"status": "new"}, "PATCH")
        check("reopen charges again", call(alice, "/api/me")[1]["student"]["balance_cents"] == 1000 - price)
        # Spend down to $1, then race two $6 orders: at most one may go through.
        call(staff, f"/api/admin/students/{sid}/balance", {"amount": f"{-(1000 - price - 700) / 100}"})
        out = []
        def go(pid): out.append(call(alice, "/api/orders", {"items": [{"id": pid, "qty": 1}]})[0])
        ts = [threading.Thread(target=go, args=(p,)) for p in (3, 4)]; [t.start() for t in ts]; [t.join() for t in ts]
        check("simultaneous orders can't overspend", sorted(out) == [201, 409] and call(alice, "/api/me")[1]["student"]["balance_cents"] == 100, out)
        # ---- weekly limit ----
        call(staff, "/api/admin/settings", {"weekly_item_limit": 1})
        bid = call(bob, "/api/me")[1]["student"]["id"]
        call(staff, f"/api/admin/students/{bid}/balance", {"amount": "100"})
        r = call(bob, "/api/me"); check("bob sees 1 item left", r[1]["weekly"]["remaining"] == 1, r)
        r = call(bob, "/api/orders", {"items": [{"id": 2, "qty": 2}]}); check("2 items in one order blocked", r[0] == 409 and "1 item a week" in r[1]["error"], r)
        r = call(bob, "/api/orders", {"items": [{"id": 5, "qty": 1}]}); check("first item this week allowed", r[0] == 201, r)
        boid = r[1]["order"]["id"]
        r = call(bob, "/api/orders", {"items": [{"id": 6, "qty": 1}]}); check("second item this week blocked", r[0] == 409 and "resets on Monday" in r[1]["error"], r)
        check("bob sees 0 left", call(bob, "/api/me")[1]["weekly"]["remaining"] == 0)
        call(staff, f"/api/admin/orders/{boid}", {"status": "cancelled"}, "PATCH")
        r = call(bob, "/api/orders", {"items": [{"id": 6, "qty": 1}]}); check("cancelled order frees the week's item", r[0] == 201, r)
        r = call(staff, "/api/admin/settings", {"weekly_item_limit": 2}); check("staff can raise the limit", r[1]["weekly_item_limit"] == 2, r)
        r = call(bob, "/api/orders", {"items": [{"id": 7, "qty": 1}]}); check("raised limit allows one more", r[0] == 201, r)
        r = call(staff, "/api/admin/settings", {"weekly_item_limit": -1}); check("negative limit rejected", r[0] == 400, r)
        g = 'g_state={"i_l":0,"i_e":{"enable_itp_optimization":24}}'
        sess = [c for c in ajar if c.name == "em_student"][0]
        req = urllib.request.Request(B + "/api/me", headers={"Cookie": f"{g}; em_student={sess.value}"})
        check("sessions survive Google's g_state cookie", json.load(urllib.request.urlopen(req))["student"] is not None)
        p = sess.value.split("."); sess.value = f"{int(p[0]) + 1}.{p[1]}.{p[2]}"
        check("tampered session rejected", call(alice, "/api/me")[1]["student"] is None)
    finally:
        srv.terminate()
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
