"""A fake Grid Manager: enough WAPI over real HTTP to test the client.

It serves networks, containers, network views and extensible attribute
definitions, filtered by network view, with WAPI's paging. It checks Basic
auth, hands out an `ibapauth` cookie like NIOS does, and records every
request's method so tests can prove only GETs were sent. Addresses are RFC
1918 and documentation ranges.
"""

import base64
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USER, PASSWORD = "ipam-reader", "not-a-real-password"
VERSION = "2.12"


def grid():
    """What the fake Grid holds."""
    return {
        "networkview": [{"name": "default"}, {"name": "aws-111122223333"}],
        "networkcontainer": [
            {"network": "10.0.0.0/8", "network_view": "default", "comment": "RFC 1918 space"},
        ],
        "network": [
            {"network": "10.0.2.0/24", "network_view": "default", "comment": "prod-app-b",
             "extattrs": {"AWS Account": {"value": "111122223333"}}},
            {"network": "10.3.0.0/16", "network_view": "default", "comment": "on-prem lab"},
            {"network": "192.168.0.0/24", "network_view": "aws-111122223333"},
        ],
        "ipv6networkcontainer": [],
        "ipv6network": [],
        "extensibleattributedef": [
            {"name": "AWS Account", "type": "STRING"},
            {"name": "Environment", "type": "ENUM",
             "list_values": [{"value": "prod"}, {"value": "dev"}]},
        ],
    }


class FakeGrid:
    def __init__(self, data=None, page_size=None):
        self.data = data if data is not None else grid()
        self.page_size = page_size
        self.requests = []          # (method, path, query)
        self.logins = 0
        self.pages = {}
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _record(self):
                parts = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(parts.query))
                fake.requests.append((self.command, parts.path, query))
                return parts.path, query

            def _send(self, status, body, cookie=False):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if cookie:
                    self.send_header("Set-Cookie", "ibapauth=session-1; Path=/; HttpOnly")
                self.end_headers()
                self.wfile.write(data)

            def _refuse(self):
                self._record()
                self._send(405, {"Error": "AdmConProtoError: read-only fake"})

            do_POST = do_PUT = do_DELETE = do_PATCH = _refuse

            def do_GET(self):
                path, query = self._record()
                session = "ibapauth=session-1" in (self.headers.get("Cookie") or "")
                expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
                if not session and self.headers.get("Authorization") != expected:
                    return self._send(401, {"Error": "AdmConAuthError: bad credentials"})
                if not session:
                    fake.logins += 1
                prefix = f"/wapi/v{VERSION}/"
                if not path.startswith(prefix):
                    return self._send(400, {"Error": "AdmConProtoError: unknown version",
                                            "text": "Unknown WAPI version"})
                obj = path[len(prefix):]
                if "_page_id" in query:
                    items, size = fake.pages.pop(query["_page_id"])
                elif obj in fake.data:
                    items = [i for i in fake.data[obj]
                             if "network_view" not in query
                             or i.get("network_view", "default") == query["network_view"]]
                    size = fake.page_size or int(query.get("_max_results", 1000))
                else:
                    return self._send(400, {"Error": f"AdmConProtoError: Unknown object type ({obj})",
                                            "text": f"Unknown object type ({obj})"})
                page, rest = items[:size], items[size:]
                body = {"result": page}
                if rest:
                    token = f"page-{len(fake.requests)}"
                    fake.pages[token] = (rest, size)
                    body["next_page_id"] = token
                self._send(200, body if query.get("_return_as_object") or "_page_id" in query
                           else page, cookie=not session)

        return Handler
