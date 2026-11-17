#!/usr/bin/env python3
"""Mock of the Skylight AUTH flow (OAuth2+PKCE, Rails/Doorkeeper-style) + API,
for end-to-end testing of skylight_lists.py login/refresh. Mirrors live server
behavior probed on app.ourskylight.com: GET /oauth/authorize -> 302 -> /auth/session/new."""
import base64
import hashlib
import json
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from urllib.parse import urlparse, parse_qs

PORT = 8766
LOG = []
STATE = {}          # state -> {challenge, client_id, redirect_uri}
CODES = {}          # code -> {challenge, state, client_id, redirect_uri}
REFRESH = {}        # refresh_token -> client_id
NEXT_CODE = [0]

API_DB = {
    "lists": [{"id": 101, "name": "Groceries"}, {"id": 102, "name": "To-Do"}],
    "items": {
        101: [{"id": 5001, "name": "Milk", "checked": False, "section": "Dairy", "position": 0},
              {"id": 5002, "name": "Eggs", "checked": True, "section": "Dairy", "position": 1}],
        102: [{"id": 6001, "name": "Fix bike", "checked": False, "position": 0}],
    },
}

LOGIN_HTML = """<!DOCTYPE html><html><head>
<meta name="csrf-token" content="mock-csrf-token">
</head><body><form action="/auth/session" method="post">
<input type="hidden" name="authenticity_token" value="mock-csrf-token">
<input name="email"><input type="password" name="password"></form></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _log(self):
        LOG.append(f"{self.command} {self.path}")

    def _send(self, code, obj):
        body = json.dumps({"data": obj}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_raw(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, loc, hdr="location"):
        self.send_response(302)
        self.send_header(hdr, loc)  # lowercase: real Skylight/Cloudflare sends lowercase header names
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _html(self, code, html):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _form(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode()
        return urllib.parse.parse_qs(raw)

    # ---- auth endpoints ----
    def _authorize(self):
        q = parse_qs(urlparse(self.path).query)
        if "code" in q:
            # Doorkeeper hands off to redirect_uri with the code
            return self._redirect(f"https://ourskylight.com/welcome?code={q['code'][0]}&state={q.get('state',[''])[0]}")
        NEXT_CODE[0] += 1
        state = q.get("state", [""])[0]
        STATE[state] = {"challenge": q.get("code_challenge", [""])[0],
                        "client_id": q.get("client_id", [""])[0],
                        "redirect_uri": q.get("redirect_uri", [""])[0]}
        self.send_response(302)
        self.send_header("Location", "/auth/session/new")
        self.send_header("Set-Cookie", "mock_sid=abc123; Path=/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _session_new(self):
        self._html(200, LOGIN_HTML)

    def _session_create(self):
        f = self._form()
        csrf = (f.get("authenticity_token") or [""])[0]
        email = (f.get("email") or [""])[0]
        pw = (f.get("password") or [""])[0]
        if csrf != "mock-csrf-token":
            return self._html(422, LOGIN_HTML)
        if email == "2fa@example.com":
            # 2FA accounts re-render the login page with 200
            return self._html(200, LOGIN_HTML)
        if email == "noloc@example.com":
            self.send_response(302)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if email == "test@example.com" and pw == "hunter2":
            state = next(reversed(STATE)) if STATE else ""
            s = STATE.get(state, {"challenge": "", "client_id": "skylight-mobile",
                                  "redirect_uri": "https://ourskylight.com/welcome"})
            code = f"mockcode{NEXT_CODE[0]}"
            CODES[code] = dict(s)
            return self._redirect(f"/oauth/authorize?client_id={s['client_id']}&state={state}&code={code}")
        return self._html(401, "<html>Unauthorized</html>")

    def _token(self):
        f = self._form()
        grant = (f.get("grant_type") or [""])[0]
        if grant == "authorization_code":
            code = (f.get("code") or [""])[0]
            s = CODES.get(code)
            if not s:
                return self._send_raw(400, {"error": "invalid_grant"})
            verifier = (f.get("code_verifier") or [""])[0]
            digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if digest != s["challenge"]:
                return self._send_raw(400, {"error": "invalid_grant", "detail": "PKCE verifier mismatch"})
            refresh = f"mock-refresh-{len(CODES)}"
            REFRESH[refresh] = (f.get("client_id") or [""])[0]
            self._send_raw(200, {"access_token": f"mock-access-{len(CODES)}",
                             "refresh_token": refresh, "expires_in": 3600,
                             "token_type": "Bearer"})
        elif grant == "refresh_token":
            refresh = (f.get("refresh_token") or [""])[0]
            if refresh not in REFRESH:
                return self._send_raw(400, {"error": "invalid_grant"})
            self._send_raw(200, {"access_token": f"mock-access-r{len(LOG)}",
                             "refresh_token": refresh, "expires_in": 3600,
                             "token_type": "Bearer"})
        else:
            self._send_raw(400, {"error": "unsupported_grant_type"})

    # ---- API endpoints (mirrors mock_skylight.py) ----
    def _api(self, method):
        auth = self.headers.get("Authorization", "")
        if not (auth.startswith("Bearer mock-access") or auth.startswith("Bearer test")):
            self.send_response(401); self.end_headers(); return
        path = urlparse(self.path).path
        parts = [p for p in path.split("/") if p]
        if parts == ["api", "frames"] and method == "GET":
            return self._send(200, [{"id": 5455113, "name": "Kitchen"}])
        if len(parts) >= 3 and parts[:3] == ["api", "frames", "5455113"]:
            rest = parts[3:]
            if rest == ["lists"] and method == "GET":
                return self._send(200, API_DB["lists"])
            if len(rest) >= 2 and rest[0] == "lists" and rest[2:] == ["list_items"] and method == "GET":
                return self._send(200, API_DB["items"].get(int(rest[1]), []))
        self.send_response(404); self.end_headers()

    def _route(self, method):
        self._log()
        path = urlparse(self.path).path
        if path == "/oauth/authorize" and method == "GET":
            return self._authorize()
        if path == "/auth/session/new" and method == "GET":
            return self._session_new()
        if path == "/auth/session" and method == "POST":
            return self._session_create()
        if path == "/oauth/token" and method == "POST":
            return self._token()
        if path.startswith("/api/"):
            return self._api(method)
        self.send_response(404); self.end_headers()

    def do_GET(self): self._route("GET")
    def do_POST(self): self._route("POST")
    def do_PUT(self): self._route("PUT")
    def do_DELETE(self): self._route("DELETE")


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
