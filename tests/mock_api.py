#!/usr/bin/env python3
"""Mock Skylight API on :8765 — mirrors the LIVE shapes verified 2026-11-16:
- responses are JSON:API: {id, type, attributes:{...}} in {data:{list:[...]}} envelopes
- single-resource POST/PUT responses nest under {data:{list:{...}}}/{data:{list_item:{...}}}
- writes take FLAT payloads with the live keys: label/kind/color (list),
  label/section (item), status (check), ids (bulk_destroy), item_ids (bulk_update_section)
- invalid payloads return 422 {"errors":[...]} like Rails does live."""
import json
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FRAME = "5455113"
LISTS = {
    101: {"id": 101, "type": "list", "attributes": {
        "label": "Groceries", "kind": "shopping", "color": "#FBD97E",
        "position": 1, "items_enabled": True}},
    102: {"id": 102, "type": "list", "attributes": {
        "label": "To-Do List", "kind": "to_do", "color": "#555555",
        "position": 2, "items_enabled": True}},
    103: {"id": 103, "type": "list", "attributes": {
        "label": "Apple List", "kind": "to_do", "color": "#00aa00",
        "position": 3, "items_enabled": True}},
    104: {"id": 104, "type": "list", "attributes": {
        "label": "Apple List", "kind": "shopping", "color": "#aa0000",
        "position": 4, "items_enabled": True}},
}
ITEMS = {
    101: [
        {"id": 5001, "type": "list_item", "attributes": {
            "id": 5001, "label": "Milk", "status": "pending",
            "section": "Dairy", "position": 0}},
        {"id": 5002, "type": "list_item", "attributes": {
            "id": 5002, "label": "Eggs", "status": "completed",
            "section": "Dairy", "position": 1}},
        {"id": 5003, "type": "list_item", "attributes": {
            "id": 5003, "label": "Bread", "status": "pending",
            "section": "Bakery", "position": 2}},
    ],
    102: [{"id": 6001, "type": "list_item", "attributes": {
        "id": 6001, "label": "Fix bike", "status": "pending",
        "section": None, "position": 0}}],
    103: [], 104: [],
}
NEXT_ID = {"list": 200, "item": 7000}
AUTH_BASE = "http://127.0.0.1:8765"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, *msgs):
        self._send(code, {"errors": list(msgs)})

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return {}

    def _auth_ok(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or len(auth) < 8:
            self._err(401, "Token invalid")
            return False
        return True

    def _resolve_list(self, frame, name_or_id):
        lists = [l for l in LISTS.values()]
        exact = [l for l in lists if l["attributes"]["label"] == name_or_id]
        if exact:
            return exact[0]
        cands = [l for l in lists
                 if name_or_id.lower() in l["attributes"]["label"].lower()]
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:
            return {"ambiguous": [l["id"] for l in cands]}
        if str(name_or_id).isdigit():
            return LISTS.get(int(name_or_id))
        return None

    # ------------------------------------------------------------- GET
    def do_GET(self):
        if not self._auth_ok():
            return
        p = urllib.parse.urlparse(self.path).path
        if p == "/api/frames":
            self._send(200, {"data": {"list": [
                {"id": FRAME, "type": "frame", "attributes": {"name": "Kitchen", "kind": "family"}}]}})
            return
        m = re.match(rf"^/api/frames/{FRAME}/lists/?$", p)
        if m:
            self._send(200, {"data": {"list": list(LISTS.values())}})
            return
        m = re.match(rf"^/api/frames/{FRAME}/lists/(\d+)/list_items/?$", p)
        if m:
            lid = int(m.group(1))
            if lid not in LISTS:
                self._err(404, "Record not found")
                return
            self._send(200, {"data": {"list": ITEMS.get(lid, [])}})
            return
        m = re.match(rf"^/api/frames/{FRAME}/lists/(\d+)/?$", p)
        if m:
            lid = int(m.group(1))
            if lid not in LISTS:
                self._err(404, "Record not found")
                return
            self._send(200, {"data": {"list": LISTS[lid]}})
            return
        self._err(404, "Not Found")

    # ------------------------------------------------------------- POST
    def do_POST(self):
        if not self._auth_ok():
            return
        p = urllib.parse.urlparse(self.path).path
        b = self._body()
        m = re.match(rf"^/api/frames/{FRAME}/lists/?$", p)
        if m:
            missing = [k for k in ("label", "kind", "color") if k not in b]
            if missing:
                self._err(422, *[f"{k.capitalize()} can't be blank" for k in missing])
                return
            if b["kind"] not in ("to_do", "shopping"):
                self._err(422, "Kind is invalid")
                return
            if not re.match(r"^#[0-9a-fA-F]{6}$", str(b["color"])):
                self._err(422, "Color is invalid")
                return
            lid = NEXT_ID["list"]; NEXT_ID["list"] += 1
            LISTS[lid] = {"id": lid, "type": "list", "attributes": {
                "label": b["label"], "kind": b["kind"], "color": b["color"],
                "position": len(LISTS) + 1, "items_enabled": True}}
            ITEMS[lid] = []
            self._send(201, {"data": {"list": LISTS[lid]}})
            return
        m = re.match(rf"^/api/frames/{FRAME}/lists/(\d+)/list_items/?$", p)
        if m:
            lid = int(m.group(1))
            if lid not in LISTS:
                self._err(404, "Record not found")
                return
            if "label" not in b:
                self._err(422, "Label can't be blank")
                return
            iid = NEXT_ID["item"]; NEXT_ID["item"] += 1
            it = {"id": iid, "type": "list_item", "attributes": {
                "id": iid, "label": b["label"], "status": "pending",
                "section": b.get("section"), "position": len(ITEMS.get(lid, []))}}
            ITEMS.setdefault(lid, []).append(it)
            self._send(201, {"data": {"list_item": it}})
            return
        m = re.match(rf"^/api/frames/{FRAME}/lists/(\d+)/list_items/(\d+)/move/?$", p)
        if m:
            lid, iid = int(m.group(1)), int(m.group(2))
            items = ITEMS.get(lid, [])
            it = next((x for x in items if x["id"] == iid), None)
            if not it:
                self._err(404, "Record not found")
                return
            items.remove(it)
            if "after_list_item_id" in b:
                idx = next((i for i, x in enumerate(items)
                            if x["id"] == int(b["after_list_item_id"])), None)
                items.insert((idx + 1) if idx is not None else len(items), it)
            elif "before_list_item_id" in b:
                idx = next((i for i, x in enumerate(items)
                            if x["id"] == int(b["before_list_item_id"])), len(items))
                items.insert(idx, it)
            elif "position" in b:
                items.insert(int(b["position"]), it)
            else:
                items.append(it)
            for pos, x in enumerate(items):
                x["attributes"]["position"] = pos
            self._send(200, {"data": {"list": items}})
            return
        self._err(404, "Not Found")

    # -------------------------------------------------------------- PUT
    def do_PUT(self):
        if not self._auth_ok():
            return
        p = urllib.parse.urlparse(self.path).path
        b = self._body()
        m = re.match(rf"^/api/frames/{FRAME}/lists/(\d+)/list_items/bulk_update_section/?$", p)
        if m:
            lid = int(m.group(1))
            if lid not in LISTS:
                self._err(404, "Record not found")
                return
            if "item_ids" not in b:
                self._err(422, "Item ids is required.")
                return
            ids = [int(x) for x in b["item_ids"]]
            for it in ITEMS.get(lid, []):
                if it["id"] in ids:
                    it["attributes"]["section"] = b.get("section")
            self._send(200, {"data": {"list": ITEMS.get(lid, [])}})
            return
        m = re.match(rf"^/api/frames/{FRAME}/lists/(\d+)/list_items/(\d+)/?$", p)
        if m:
            lid, iid = int(m.group(1)), int(m.group(2))
            it = next((x for x in ITEMS.get(lid, []) if x["id"] == iid), None)
            if not it:
                self._err(404, "Record not found")
                return
            status = b.get("status")
            if status not in ("pending", "completed"):
                self._err(422, "Status is invalid")
                return
            it["attributes"]["status"] = status
            self._send(200, {"data": {"list_item": it}})
            return
        m = re.match(rf"^/api/frames/{FRAME}/lists/(\d+)/?$", p)
        if m:
            lid = int(m.group(1))
            if lid not in LISTS:
                self._err(404, "Record not found")
                return
            if "label" not in b:
                self._err(422, "Label can't be blank")
                return
            LISTS[lid]["attributes"]["label"] = b["label"]
            self._send(200, {"data": {"list": LISTS[lid]}})
            return
        self._err(404, "Not Found")

    # ----------------------------------------------------------- DELETE
    def do_DELETE(self):
        if not self._auth_ok():
            return
        p = urllib.parse.urlparse(self.path).path
        b = self._body()
        m = re.match(rf"^/api/frames/{FRAME}/lists/(\d+)/list_items/bulk_destroy/?$", p)
        if m:
            lid = int(m.group(1))
            if lid not in LISTS:
                self._err(404, "Record not found")
                return
            if "ids" not in b:
                self._err(422, "Ids is required.")
                return
            ids = [int(x) for x in b["ids"]]
            ITEMS[lid] = [x for x in ITEMS.get(lid, []) if x["id"] not in ids]
            self._send(200, {"data": {"list": ITEMS[lid]}})
            return
        m = re.match(rf"^/api/frames/{FRAME}/lists/(\d+)/list_items/(\d+)/?$", p)
        if m:
            lid, iid = int(m.group(1)), int(m.group(2))
            before = len(ITEMS.get(lid, []))
            ITEMS[lid] = [x for x in ITEMS.get(lid, []) if x["id"] != iid]
            if len(ITEMS[lid]) == before:
                self._err(404, "Record not found")
                return
            self._send(200, {"data": {"list": []}})
            return
        m = re.match(rf"^/api/frames/{FRAME}/lists/(\d+)/?$", p)
        if m:
            lid = int(m.group(1))
            if lid not in LISTS:
                self._err(404, "Record not found")
                return
            LISTS.pop(lid); ITEMS.pop(lid, None)
            self._send(200, {"data": {}})
            return
        self._err(404, "Not Found")


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8765), H).serve_forever()
