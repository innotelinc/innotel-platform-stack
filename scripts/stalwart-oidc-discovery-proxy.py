#!/usr/bin/env python3
"""Serve Stalwart's OIDC discovery document locally.

WHY
---
Stalwart 0.16.27 fetches its directory's discovery document once at start-up
and caches a failure for the life of the process: a single blip leaves every
bearer token answering 500 and the webmail's SSO failing *after* a correct
sign-in (docs/mail-sso-directory-2026-11-06.md). The watchdog restarts Stalwart
to recover; this removes the dependency that caused it.

The directory's issuer is https://auth.cerulean.innotel.us/application/o/stalwart-webui/
and this build has no separate discovery-URL setting — it appends
/.well-known/openid-configuration to the issuer and rejects a document whose
issuer differs. So the document has to be served *as that hostname*: this
process holds the last good copy, answers the well-known path from it, and
forwards every other request upstream unchanged.

The container redirects that hostname to this port for Stalwart only (a
per-service hosts file and an iptables DNAT), so ordinary container traffic is
untouched and this process's own refresh — exempted by uid — still reaches the
real IdP.
"""
from __future__ import annotations

import http.client
import json
import os
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "auth.cerulean.innotel.us"
DOC_PATH = "/application/o/stalwart-webui/.well-known/openid-configuration"
CACHE = "/etc/stalwart-oidc/cache/discovery.json"
CERT = "/etc/stalwart-oidc/tls/server.pem"
KEY = "/etc/stalwart-oidc/tls/server.key"
REFRESH_SECONDS = 900
LISTEN = ("127.0.0.1", 8443)
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "content-length"}


def refresh_forever() -> None:
    """Keep the last good copy; a failed refresh changes nothing."""
    context = ssl.create_default_context()
    while True:
        try:
            conn = http.client.HTTPSConnection(UPSTREAM, 443, timeout=20, context=context)
            conn.request("GET", DOC_PATH, headers={"User-Agent": "stalwart-oidc-cache"})
            response = conn.getresponse()
            body = response.read()
            if response.status == 200:
                document = json.loads(body)
                if document.get("issuer") and document.get("jwks_uri"):
                    tmp = CACHE + ".tmp"
                    with open(tmp, "wb") as handle:
                        handle.write(body)
                    os.replace(tmp, CACHE)
            conn.close()
        except Exception:  # noqa: BLE001 - a failed refresh must never stop serving
            pass
        time.sleep(REFRESH_SECONDS)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "stalwart-oidc-cache"

    def log_message(self, fmt: str, *args) -> None:  # keep the journal quiet
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send(self, status: int, headers: dict, body: bytes) -> None:
        self.send_response(status)
        for name, value in headers.items():
            if name.lower() in HOP_BY_HOP:
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _handle(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        if method == "GET" and path == DOC_PATH:
            try:
                with open(CACHE, "rb") as handle:
                    body = handle.read()
                json.loads(body)
            except (OSError, ValueError):
                body = b""
            if body:
                self._send(200, {"Content-Type": "application/json"}, body)
                return
        host = (self.headers.get("Host") or UPSTREAM).split(":")[0]
        try:
            context = ssl.create_default_context()
            conn = http.client.HTTPSConnection(host, 443, timeout=20, context=context)
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
            payload = self._read_body()
            conn.request(method, self.path, body=payload or None, headers=headers)
            upstream = conn.getresponse()
            body = upstream.read()
            self._send(upstream.status, dict(upstream.getheaders()), body)
            conn.close()
        except Exception as error:  # noqa: BLE001
            self._send(502, {"Content-Type": "text/plain"}, f"upstream unavailable: {error}".encode())

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_HEAD(self) -> None:
        self._handle("HEAD")


def main() -> None:
    threading.Thread(target=refresh_forever, daemon=True).start()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERT, KEY)
    server = ThreadingHTTPServer(LISTEN, Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
