#!/usr/bin/env python3
"""Local proposal API for ORCA. No execution endpoint, no automatic cloud fallback."""

import argparse
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from providers import ProviderError
from routing import Router


def handler_for(router, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, status, body):
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def authorized(self):
            if token and not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
                self.send_json(401, {"error": "Unauthorized"})
                return False
            # Browser traffic must go through ORCA's runtime, not this loopback API.
            if self.headers.get("Origin"):
                self.send_json(403, {"error": "Use the ORCA runtime bridge"})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            if self.path == "/health":
                self.send_json(200, {"status": "ready", "routes": len(router.routes)})
            else:
                self.send_json(404, {"error": "Not found"})

        def do_POST(self):
            if not self.authorized():
                return
            if self.path != "/v1/routing/propose":
                self.send_json(404, {"error": "Not found"})
                return
            try:
                self.connection.settimeout(20)
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 256000:
                    raise ValueError("Request body must be between 1 and 256,000 bytes")
                body = json.loads(self.rfile.read(length))
                self.send_json(200, router.propose(body))
            except (ValueError, UnicodeError) as exc:
                self.send_json(400, {"error": str(exc)})
            except ProviderError as exc:
                self.send_json(502, {"error": str(exc)})
            except TimeoutError:
                self.send_json(408, {"error": "Request timed out"})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routes", required=True, help="JSON file listing verified harness/model routes")
    parser.add_argument("--port", type=int, default=8093)
    args = parser.parse_args()
    with open(args.routes, encoding="utf-8") as source:
        router = Router(json.load(source))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(router, os.environ.get("ORCA_ROUTER_TOKEN")))
    print(f"ORCA router listening on 127.0.0.1:{args.port} ({len(router.routes)} routes)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
