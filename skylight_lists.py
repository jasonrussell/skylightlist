#!/usr/bin/env python3
"""
skylight-lists — CLI to interact with Lists (grocery / to-do) on the Skylight
Calendar (ourskylight.com) via its (unofficial) REST API.

API reference used: https://gist.github.com/dknowles2/b8eab833eb23eb388c3d78999a3565f8
Base URL: https://app.ourskylight.com/api
Auth:   OAuth2 + PKCE against the Rails auth server at app.ourskylight.com
        (client_id "skylight-mobile"), or a manually-supplied bearer token.

Credential cache: ~/.config/skylight-cli.json (chmod 600)

Examples:
  ./skylight_lists.py login                       # interactive email/password login
  ./skylight_lists.py token                       # show cached access token
  ./skylight_lists.py token --set <TOKEN>         # paste a token captured from the web app
  ./skylight_lists.py frames                      # list your frames (get frame ids)
  ./skylight_lists.py lists                       # list all lists on the frame
  ./skylight_lists.py lists --create "Groceries"
  ./skylight_lists.py lists --delete "Hardware Run"
  ./skylight_lists.py items "Groceries"           # show items (+ids)
  ./skylight_lists.py add "Groceries" Milk Eggs --section Dairy
  ./skylight_lists.py check "Groceries" Milk      # by name (if unique) or: check "Groceries" 12345
  ./skylight_lists.py uncheck "Groceries" 12345
  ./skylight_lists.py rm "Groceries" 12345 12346
  ./skylight_lists.py rm "Groceries" --all-checked
  ./skylight_lists.py move "Groceries" 12345 --after 12340
  ./skylight_lists.py move-section "Groceries" 12345 12346 --section Dairy
  ./skylight_lists.py api GET /frames/5455113/lists   # raw escape hatch

Note: Skylight's API is undocumented/unofficial. Field names for item
payloads (name/checked/section) follow the app's JS function names in the
reverse-engineered docs; if the server rejects one, use --data '<json>' to
override the payload, or the `api` escape hatch.
"""

import argparse
import base64
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import stat
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

AUTH_BASE = os.environ.get("SKYLIGHT_API_BASE", "https://app.ourskylight.com")
CLIENT_ID = "skylight-mobile"
REDIRECT_URI = "https://ourskylight.com/welcome"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 SkylightCLI/1.0")
CONFIG_PATH = os.environ.get("SKYLIGHT_CONFIG", os.path.expanduser("~/.config/skylight-cli.json"))


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status, body, url):
        self.status, self.body, self.url = status, body, url
        super().__init__(f"HTTP {status} for {url}\n{body[:800]}")


def http_request(url, method="GET", data=None, headers=None, cookies=None,
                 timeout=30, follow_redirects=True):
    """Low-level HTTP. If cookies (CookieJar) given and follow_redirects=False,
    returns (status, headers, body, final_url)."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9",
           **(headers or {})}
    body = None
    if data is not None:
        if isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode()
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        else:
            body = data if isinstance(data, bytes) else str(data).encode()
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)

    if cookies is not None:
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookies),
            NoAutomaticRedirects() if not follow_redirects else urllib.request.HTTPRedirectHandler(),
        )
    else:
        opener = urllib.request.build_opener(
            NoAutomaticRedirects() if not follow_redirects else urllib.request.HTTPRedirectHandler(),
        )

    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace"), resp.geturl()
    except urllib.error.HTTPError as e:
        # urllib raises HTTPError for 3xx when redirect_request() returns None;
        # when the caller asked for follow_redirects=False, surface the 3xx as a
        # normal (status, headers, body) so manual redirect-chasing works.
        if not follow_redirects and 300 <= e.code < 400:
            return e.code, dict(e.headers), e.read().decode("utf-8", "replace"), url
        raise ApiError(e.code, e.read().decode("utf-8", "replace"), url) from None


class NoAutomaticRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_header(hdrs, name):
    """Case-insensitive header lookup: dict(resp.headers) preserves the exact
    casing on the wire (Skylight/Cloudflare send 'location' lowercase)."""
    v = hdrs.get(name)
    if v is None:
        lname = name.lower()
        for k, val in hdrs.items():
            if k.lower() == lname:
                return val
    return v


def api_call(access_token, method, path, payload=None, params=None):
    url = AUTH_BASE + "/api" + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    # Skylight's Cloudflare 403-blocks ("error code: 1010", browser-signature)
    # requests carrying bare urllib defaults (no Accept, Python-urllib UA).
    # Send the same browser-ish headers the login flow uses.
    headers = {"User-Agent": USER_AGENT,
               "Accept": "application/json, text/html;q=0.9",
               "Accept-Language": "en-US,en;q=0.9",
               "Authorization": f"Bearer {access_token}"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener()
    try:
        with opener.open(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        if "error code: 1010" in body:
            body += ("\n(Cloudflare browser-signature block — if this reappears, "
                     "the WAF is flagging this client; wait a few minutes and retry.)")
        raise ApiError(e.code, body, url) from None
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def normalize(obj):
    """Flatten JSON:API-style resources ({id, type, attributes: {...}}) so
    lookups like item['name'] / lst['id'] work regardless of shape, and map
    the live API's vocabulary: label -> name, status(completed/pending) -> checked."""
    if isinstance(obj, dict):
        attrs = obj.get("attributes")
        if isinstance(attrs, dict):
            obj = {**{k: v for k, v in obj.items() if k != "attributes"}, **attrs}
        if "label" in obj and "name" not in obj:
            obj["name"] = obj["label"]
        status = obj.get("status")
        if isinstance(status, str) and "checked" not in obj:
            obj["checked"] = status == "completed"
        return {k: normalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize(x) for x in obj]
    return obj


def unwrap(data):
    """Skylight JSON responses are often {data: ...}, {data: {list: [...]}} or
    {data: {list_item: {...}}}; resources themselves may be JSON:API-shaped —
    normalize all of these."""
    if isinstance(data, dict) and "data" in data:
        inner = data["data"]
        if isinstance(inner, dict) and len(inner) == 1:
            only = next(iter(inner.values()))
            if isinstance(only, (list, dict)):
                return normalize(only)
        return normalize(inner)
    return normalize(data)


# --------------------------------------------------------------------------
# Credential store
# --------------------------------------------------------------------------

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        sys.exit(f"ERROR: credential cache {CONFIG_PATH} is corrupt — delete it and login again.")


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 600
    os.replace(tmp, CONFIG_PATH)


def get_access_token(cfg, auto_refresh=True):
    tok = cfg.get("access_token")
    if not tok:
        return None
    expiry = cfg.get("token_expiry")
    if expiry and time.time() > expiry - 60 and auto_refresh and cfg.get("refresh_token"):
        try:
            refresh_tokens(cfg)
            return cfg["access_token"]
        except ApiError:
            pass  # fall back to existing (maybe still valid) token
    return tok


# --------------------------------------------------------------------------
# Auth: OAuth2 + PKCE login, refresh, manual token
# --------------------------------------------------------------------------

def pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def login(email, password):
    jar = http.cookiejar.CookieJar()
    # Step 0/1: fetch login page (through /oauth/authorize) to get CSRF + session cookie
    state = secrets.token_urlsafe(16)
    verifier, challenge = pkce_pair()
    authorize_qs = urllib.parse.urlencode({
        "client_id": CLIENT_ID, "response_type": "code", "scope": "everything",
        "redirect_uri": REDIRECT_URI, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    status, hdrs, html, _ = http_request(f"{AUTH_BASE}/oauth/authorize?{authorize_qs}",
                                         cookies=jar)
    if status == 302:
        loc = get_header(hdrs, "Location") or ""
        if "session/new" in loc:
            status, hdrs, html, _ = http_request(loc if loc.startswith("http") else AUTH_BASE + loc,
                                                 cookies=jar)
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    if not m:
        m = re.search(r'name="authenticity_token"[^>]*value="([^"]+)"', html)
    if not m:
        sys.exit("ERROR: could not find CSRF token on login page — flow may have changed.")
    csrf = m.group(1)

    # Step 2: submit credentials
    try:
        status, hdrs, body, _ = http_request(f"{AUTH_BASE}/auth/session", method="POST",
                                             cookies=jar, follow_redirects=False,
                                             data={"authenticity_token": csrf,
                                                  "email": email, "password": password})
    except ApiError as e:
        if e.status == 401:
            sys.exit("ERROR: login rejected — bad email/password (or 2FA required; use `token --set`).")
        raise
    loc0 = get_header(hdrs, "Location") or ""
    if status == 401 or (status == 302 and "session/new" in loc0 and "code=" not in loc0):
        sys.exit("ERROR: login rejected — bad email/password (or 2FA required; use `token --set`).")

    # Follow redirects manually to catch the auth code
    code = None
    seen = 0
    while status in (301, 302, 303, 307, 308) and seen < 10:
        seen += 1
        loc = get_header(hdrs, "Location")
        if not loc:
            sys.exit(f"ERROR: server returned HTTP {status} with no Location header "
                     f"(headers: {', '.join(hdrs) or 'none'}) — login flow changed.")
        nxt = loc if loc.startswith("http") else AUTH_BASE + loc
        qs = urllib.parse.urlparse(nxt).query
        qd = urllib.parse.parse_qs(qs)
        if "code" in qd:
            code = qd["code"][0]
            if qd.get("state", [None])[0] not in (None, state):
                sys.exit("ERROR: OAuth state mismatch — aborting.")
            break
        status, hdrs, body, _ = http_request(nxt, cookies=jar, follow_redirects=False)
    if not code:
        if status == 200 and ("authenticity_token" in (body or "") or "csrf-token" in (body or "")):
            sys.exit("ERROR: login rejected — login page re-rendered (bad email/password, or 2FA; use `token --set`).")
        sys.exit("ERROR: no authorization code found in redirect chain — login flow changed.")

    # Step 4: exchange code for tokens
    status, hdrs, body, _ = http_request(f"{AUTH_BASE}/oauth/token", method="POST",
                                         data={"grant_type": "authorization_code",
                                               "client_id": CLIENT_ID, "code": code,
                                               "redirect_uri": REDIRECT_URI,
                                               "code_verifier": verifier})
    tok = json.loads(body)
    cfg = load_config()
    cfg.update({
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "token_expiry": time.time() + int(tok.get("expires_in", 3600)),
    })
    save_config(cfg)
    print("Login OK — token cached in", CONFIG_PATH)


def refresh_tokens(cfg):
    status, hdrs, body, _ = http_request(f"{AUTH_BASE}/oauth/token", method="POST",
                                         data={"grant_type": "refresh_token",
                                               "client_id": CLIENT_ID,
                                               "refresh_token": cfg["refresh_token"]})
    tok = json.loads(body)
    cfg.update({
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", cfg.get("refresh_token")),
        "token_expiry": time.time() + int(tok.get("expires_in", 3600)),
    })
    save_config(cfg)


def require_token(cfg):
    tok = get_access_token(cfg)
    if not tok:
        sys.exit("ERROR: not authenticated. Run: login   (or: token --set <TOKEN>)")
    return tok


# --------------------------------------------------------------------------
# Frame / list / item resolution
# --------------------------------------------------------------------------

def resolve_frame(cfg, token, frame_arg):
    if frame_arg:
        return int(frame_arg)
    if cfg.get("default_frame"):
        return int(cfg["default_frame"])
    frames = unwrap(api_call(token, "GET", "/frames"))
    if isinstance(frames, list) and frames:
        if len(frames) == 1:
            fid = frames[0].get("id")
            cfg["default_frame"] = fid
            save_config(cfg)
            return fid
        print("Multiple frames — pick one with --frame:")
        for f in frames:
            print(f"  {f.get('id')}  {f.get('name', '')}")
        sys.exit(1)
    sys.exit("ERROR: no frames found for this account.")


def norm(s):
    s = unicodedata.normalize("NFKD", str(s)).casefold().strip()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def find_list(token, frame_id, name_or_id):
    lists = unwrap(api_call(token, "GET", f"/frames/{frame_id}/lists")) or []
    if str(name_or_id).isdigit():
        for l in lists:
            if str(l.get("id")) == str(name_or_id):
                return l
    target = norm(name_or_id)
    matches = [l for l in lists if norm(l.get("name", "")) == target]
    if not matches:  # substring fallback
        matches = [l for l in lists if target in norm(l.get("name", ""))]
    if not matches:
        names = ", ".join(f"{l.get('name')} ({l.get('id')})" for l in lists) or "none"
        sys.exit(f"ERROR: no list matching {name_or_id!r}. Existing lists: {names}")
    if len(matches) > 1:
        sys.exit(f"ERROR: ambiguous list {name_or_id!r}: "
                 + ", ".join(f"{m.get('name')} (id {m.get('id')})" for m in matches)
                 + " — use the id.")
    return matches[0]


def get_items(token, frame_id, list_id):
    return unwrap(api_call(token, "GET", f"/frames/{frame_id}/lists/{list_id}/list_items")) or []


def resolve_items(token, frame_id, list_id, refs, item_name_field):
    items = get_items(token, frame_id, list_id)
    resolved = []
    for ref in refs:
        if str(ref).isdigit():
            hit = [it for it in items if str(it.get("id")) == str(ref)]
        else:
            hit = [it for it in items if norm(it.get(item_name_field, "")) == norm(ref)]
        if not hit:
            avail = ", ".join(f"{it.get(item_name_field)} ({it.get('id')})" for it in items)
            sys.exit(f"ERROR: no item matching {ref!r} in list. Items: {avail}")
        if len(hit) > 1 and not str(ref).isdigit():
            sys.exit(f"ERROR: ambiguous item {ref!r} — use item ids: "
                     + ", ".join(str(h.get("id")) for h in hit))
        resolved.append(hit[0])
    return resolved


def item_field(item, *candidates, default=None):
    for c in candidates:
        if c in item:
            return c
    return default


def print_items(items, name_field=None, checked_field=None):
    if not items:
        print("(empty list)")
        return
    name_field = name_field or item_field(items[0], "name", "title", "text", default="name")
    checked_field = checked_field or item_field(items[0], "checked", "is_checked", "completed", "done", default="checked")
    for it in items:
        mark = "x" if it.get(checked_field) else " "
        extra = ""
        sec = it.get("section") or (it.get("list_section") or {}).get("name")
        if sec:
            extra = f"  [{sec}]"
        print(f"[{mark}] {it.get('id'):>10}  {it.get(name_field, '')}{extra}")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_login(args):
    import getpass
    email = args.email or input("Skylight email: ").strip()
    password = args.password or getpass.getpass("Skylight password: ")
    login(email, password)


def cmd_token(args):
    cfg = load_config()
    if args.set:
        cfg.update({"access_token": args.set, "token_expiry": time.time() + 3600 * 24,
                    "refresh_token": None})
        save_config(cfg)
        print("Token saved to", CONFIG_PATH)
        return
    tok = cfg.get("access_token")
    if not tok:
        sys.exit("No cached token. Run: login  (or: token --set <TOKEN>)")
    print(json.dumps({"access_token": tok, "token_expiry": cfg.get("token_expiry"),
                      "has_refresh_token": bool(cfg.get("refresh_token"))}, indent=2))


def cmd_frames(args):
    cfg, token = load_config(), None
    token = require_token(cfg)
    frames = unwrap(api_call(token, "GET", "/frames"))
    if args.json:
        print(json.dumps(frames, indent=2))
        return
    for f in frames or []:
        print(f"{f.get('id')}  {f.get('name', '')}")


def cmd_lists(args):
    cfg = load_config()
    token = require_token(cfg)
    frame = resolve_frame(cfg, token, args.frame)
    if args.create:
        res = api_call(token, "POST", f"/frames/{frame}/lists",
                       {"label": args.create, "kind": args.kind, "color": args.color})
        print("Created:", json.dumps(unwrap(res)))
        return
    if args.delete:
        lst = find_list(token, frame, args.delete)
        api_call(token, "DELETE", f"/frames/{frame}/lists/{lst['id']}")
        print(f"Deleted list {lst.get('name')!r} (id {lst['id']})")
        return
    if args.rename:
        old, new = args.rename
        lst = find_list(token, frame, old)
        res = api_call(token, "PUT", f"/frames/{frame}/lists/{lst['id']}", {"label": new})
        print(f"Renamed {lst.get('name')!r} -> {new!r}", json.dumps(unwrap(res)))
        return
    lists = unwrap(api_call(token, "GET", f"/frames/{frame}/lists")) or []
    if args.json:
        print(json.dumps(lists, indent=2))
        return
    if not lists:
        print("(no lists)")
    for l in lists:
        print(f"{l.get('id'):>10}  {l.get('name', '')}")


def cmd_items(args):
    cfg = load_config()
    token = require_token(cfg)
    frame = resolve_frame(cfg, token, args.frame)
    lst = find_list(token, frame, args.list)
    items = get_items(token, frame, lst["id"])
    if args.json:
        print(json.dumps(items, indent=2))
        return
    print(f"# {lst.get('name')} (list id {lst['id']}) — {len(items)} items")
    print_items(items)


def cmd_add(args):
    cfg = load_config()
    token = require_token(cfg)
    frame = resolve_frame(cfg, token, args.frame)
    lst = find_list(token, frame, args.list)
    for name in args.names:
        payload = {"label": name}
        if args.section:
            payload["section"] = args.section
        if args.data:
            payload.update(json.loads(args.data))
        res = unwrap(api_call(token, "POST", f"/frames/{frame}/lists/{lst['id']}/list_items", payload))
        new_id = res.get("id") if isinstance(res, dict) else None
        print(f"Added {name!r}" + (f" (id {new_id})" if new_id else ""))


def _update_item(token, frame, list_id, item, payload):
    path = f"/frames/{frame}/lists/{list_id}/list_items/{item['id']}"
    return unwrap(api_call(token, "PUT", path, payload))


def cmd_check(args, checked=True):
    cfg = load_config()
    token = require_token(cfg)
    frame = resolve_frame(cfg, token, args.frame)
    lst = find_list(token, frame, args.list)
    items = resolve_items(token, frame, lst["id"], args.items, "name")
    payload = {"status": "completed" if checked else "pending"}
    if args.data:
        payload.update(json.loads(args.data))
    for it in items:
        _update_item(token, frame, lst["id"], it, payload)
        print(f"{'Checked' if checked else 'Unchecked'}: {it.get('name')} (id {it['id']})")


def cmd_rm(args):
    cfg = load_config()
    token = require_token(cfg)
    frame = resolve_frame(cfg, token, args.frame)
    lst = find_list(token, frame, args.list)
    if args.all_checked:
        all_items = get_items(token, frame, lst["id"])
        checked_key = item_field(all_items[0], "checked", "is_checked", "completed", "done",
                                 default="checked") if all_items else "checked"
        ids = [it["id"] for it in all_items if it.get(checked_key)]
        if not ids:
            print("Nothing checked to remove.")
            return
        api_call(token, "DELETE", f"/frames/{frame}/lists/{lst['id']}/list_items/bulk_destroy",
                 {"ids": ids})
        print(f"Bulk-deleted {len(ids)} checked items.")
        return
    items = resolve_items(token, frame, lst["id"], args.items, "name")
    for it in items:
        api_call(token, "DELETE", f"/frames/{frame}/lists/{lst['id']}/list_items/{it['id']}")
        print(f"Deleted: {it.get('name')} (id {it['id']})")


def cmd_move(args):
    cfg = load_config()
    token = require_token(cfg)
    frame = resolve_frame(cfg, token, args.frame)
    lst = find_list(token, frame, args.list)
    items = resolve_items(token, frame, lst["id"], [args.item], "name")
    it = items[0]
    payload = {}
    if args.after is not None:
        after = resolve_items(token, frame, lst["id"], [args.after], "name")[0]
        payload = {"after_list_item_id": after["id"]}
    elif args.before is not None:
        before = resolve_items(token, frame, lst["id"], [args.before], "name")[0]
        payload = {"before_list_item_id": before["id"]}
    elif args.position is not None:
        payload = {"position": args.position}
    if args.data:
        payload.update(json.loads(args.data))
    api_call(token, "POST", f"/frames/{frame}/lists/{lst['id']}/list_items/{it['id']}/move", payload)
    print(f"Moved: {it.get('name')} (id {it['id']})")


def cmd_move_section(args):
    cfg = load_config()
    token = require_token(cfg)
    frame = resolve_frame(cfg, token, args.frame)
    lst = find_list(token, frame, args.list)
    items = resolve_items(token, frame, lst["id"], args.items, "name")
    payload = {"item_ids": [it["id"] for it in items], "section": args.section}
    if args.data:
        payload.update(json.loads(args.data))
    api_call(token, "PUT", f"/frames/{frame}/lists/{lst['id']}/list_items/bulk_update_section", payload)
    print(f"Moved {len(items)} item(s) to section {args.section!r}")


def cmd_api(args):
    cfg = load_config()
    token = require_token(cfg)
    payload = json.loads(args.data) if args.data else None
    res = api_call(token, args.method, args.path, payload)
    print(json.dumps(res, indent=2) if not isinstance(res, str) else res)


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def build_parser():
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--frame", help="Skylight frame id (default: cached/single-frame auto)")
    p = argparse.ArgumentParser(prog="skylight-lists",
                                description="Interact with Skylight Calendar lists (grocery/to-do).",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__.split("Examples:")[-1])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("login", parents=[parent], help="Interactive email/password login (OAuth PKCE)")
    s.add_argument("--email")
    s.add_argument("--password", help="Omit to be prompted (kept off history)")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("token", parents=[parent], help="Show cached token, or save one with --set")
    s.add_argument("--set", metavar="TOKEN")
    s.set_defaults(func=cmd_token)

    s = sub.add_parser("frames", parents=[parent], help="List frames (device/calendar ids)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_frames)

    s = sub.add_parser("lists", parents=[parent], help="List lists, or --create/--delete/--rename")
    s.add_argument("--create", metavar="NAME")
    s.add_argument("--kind", default="to_do", choices=["to_do", "shopping"],
                   help="list kind for --create (default: to_do)")
    s.add_argument("--color", default="#FBD97E",
                   help="hex color for --create (default: #FBD97E)")
    s.add_argument("--delete", metavar="LIST")
    s.add_argument("--rename", nargs=2, metavar=("OLD", "NEW"))
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_lists)

    s = sub.add_parser("items", parents=[parent], help="Show items of a list (with ids)")
    s.add_argument("list")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_items)

    s = sub.add_parser("add", parents=[parent], help="Add one or more items to a list")
    s.add_argument("list")
    s.add_argument("names", nargs="+")
    s.add_argument("--section", help="Section/category to file the items under")
    s.add_argument("--data", help="Extra/override JSON payload")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("check", parents=[parent], help="Mark item(s) checked (buy/do them)")
    s.add_argument("list")
    s.add_argument("items", nargs="+", help="Item ids or unique names")
    s.add_argument("--data")
    s.set_defaults(func=lambda a: cmd_check(a, True))

    s = sub.add_parser("uncheck", parents=[parent], help="Mark item(s) unchecked")
    s.add_argument("list")
    s.add_argument("items", nargs="+")
    s.add_argument("--data")
    s.set_defaults(func=lambda a: cmd_check(a, False))

    s = sub.add_parser("rm", parents=[parent], help="Delete item(s), or --all-checked")
    s.add_argument("list")
    s.add_argument("items", nargs="*", help="Item ids or unique names")
    s.add_argument("--all-checked", action="store_true", help="Bulk-delete all checked items")
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("move", parents=[parent], help="Reposition an item in the list")
    s.add_argument("list")
    s.add_argument("item")
    grp = s.add_mutually_exclusive_group()
    grp.add_argument("--after")
    grp.add_argument("--before")
    grp.add_argument("--position", type=int)
    s.add_argument("--data")
    s.set_defaults(func=cmd_move)

    s = sub.add_parser("move-section", parents=[parent], help="Bulk-move items to a section")
    s.add_argument("list")
    s.add_argument("items", nargs="+")
    s.add_argument("--section", required=True)
    s.add_argument("--data")
    s.set_defaults(func=cmd_move_section)

    s = sub.add_parser("api", parents=[parent], help="Raw escape hatch: GET/POST/PUT/DELETE any /api path")
    s.add_argument("method")
    s.add_argument("path", help="Path beginning with /, e.g. /frames/5455113/lists")
    s.add_argument("--data", help="JSON payload for POST/PUT")
    s.set_defaults(func=cmd_api)

    return p


def main():
    args = build_parser().parse_args()
    try:
        args.func(args)
    except ApiError as e:
        print(f"API error: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
