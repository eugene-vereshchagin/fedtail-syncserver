"""What the tests share: the server module loaded from its file, a temporary folder, a
live server on a free port with an invite code made, journals by name, and raw sockets."""

import hashlib
import http.client
import importlib.util
import json
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path

SERVER_FILE = Path(__file__).resolve().parent.parent / "fedtail_sync.py"
# a "token" of the tests names a device's journal: t-me and t-me2 — two devices of one
# key; t-you — another; anything else — my journal with a wrong secret
GOOD = {"t-me": "me", "t-me2": "me", "t-you": "you"}
CODE = "TEST-CODE-0001"


def creds(token):
    """(journal id, secret) a device with this "token" derives from its key."""
    name = GOOD.get(token, "me")
    jid = hashlib.sha256(f"j:{name}".encode()).hexdigest()[:32]
    secret = hashlib.sha256(f"s:{name}".encode()).hexdigest()
    if token not in GOOD:
        secret = hashlib.sha256(f"wrong:{token}".encode()).hexdigest()
    return jid, secret


ME, YOU = creds("t-me")[0], creds("t-you")[0]  # the journals' ids


def load_server():
    """fedtail_sync.py as a module (it is a single file, not a package)."""
    import sys
    mod = sys.modules.get("fedtail_sync")
    if mod is None:
        spec = importlib.util.spec_from_file_location("fedtail_sync", SERVER_FILE)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["fedtail_sync"] = mod
        spec.loader.exec_module(mod)
    return mod


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fedtail-sync-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.mod = load_server()


class Live(Tmp):
    """A server with the options given and an invite code (CODE) to make journals."""

    def start(self, **cfg):
        conf = {"db": str(self.tmp / "s.db"), "listen": "127.0.0.1:0", **cfg}
        self.app = self.mod.Server(conf)
        try:
            self.app.invite_add("tests", CODE)
        except ValueError:  # started again over the same database: it is there
            pass
        self.addCleanup(self.app.db.close)
        self.httpd = self.mod.make_http_server(self.app)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        return self.app

    def call(self, method, path, token=None, body=None, headers=None, invite=CODE):
        """path "/v1/…" is asked as "/v2/…" (the tests were written for protocol 1);
        token: whose journal (GOOD), its invite sent along unless invite is None."""
        if path.startswith("/v1/"):
            path = "/v2/" + path[len("/v1/"):]
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if token:
            jid, secret = creds(token)
            h.setdefault("Authorization", f"Bearer {secret}")
            h.setdefault("X-Fedtail-Journal", jid)
            if invite:
                h.setdefault("X-Fedtail-Invite", invite)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        try:
            return r.status, json.loads(data)
        except ValueError:
            return r.status, data

    def raw(self, data, wait=5.0, half_close=False):
        """Bytes sent as they are; → what came back before the server closed."""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=wait)
        s.sendall(data)
        if half_close:
            s.shutdown(socket.SHUT_WR)  # "that's all I send"
        out = b""
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
        except TimeoutError:
            out += b"<still open>"
        except ConnectionError:
            pass
        s.close()
        return out

    @staticmethod
    def closed_within(sock, seconds):
        sock.settimeout(seconds)
        try:
            return sock.recv(1) == b""
        except TimeoutError:
            return False
        except ConnectionError:
            return True
