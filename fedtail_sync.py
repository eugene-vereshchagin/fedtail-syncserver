"""fedtail-sync: the meeting point of one account's devices (fedtail/core/sync).

A small HTTP server on the standard library alone: one file, Python 3.11+, SQLite. It
keeps sealed blobs — each device's journal of read marks and thread states — and hands
a journal's blobs to its other devices. It can't read them: they are encrypted with a
key only the devices have. All it learns is how big they are and when.

Whose: a journal is its key's. The devices derive from the key a journal id and a secret
(the key itself never comes here); whoever has the key has the journal. A journal is made
only with an invite code the admin gave (`invite add <name>`), and a code taken back
(`invite revoke <name>`) takes the journals made with it away — no accounts, no
passwords, no fediverse server asked anything.

    python3 fedtail_sync.py --config /etc/fedtail-sync.toml        # serve
    python3 fedtail_sync.py --config … invite add anna             # a code for Anna
    python3 fedtail_sync.py --config … invite list
    python3 fedtail_sync.py --config … invite revoke anna          # and her journals

It lives apart from the fedtail package on purpose: an admin copies this one file to
the server, nothing else of fedtail is needed there.

Behind nginx/Caddy or a Cloudflare tunnel for TLS, on a name of its own: fedtail finds
it only by the address the user puts in sync.url. README.md (next to this file) has the
config, a systemd unit, the proxy lines; PROTOCOL.md what it speaks.

It faces the internet, so it guards itself (each a config option, README.md):
a connection is dropped after `timeout_seconds` of silence and only `max_connections`
are served at once; every client address has `requests_per_minute_per_ip`, every
journal `requests_per_minute`, and new journals `new_journals_per_minute_per_ip`;
bodies are read only with an honest Content-Length; the disk is capped per journal
(`quota_mb`) and in all (`total_mb`); the pictures of drafts have their own size, quota
and pace (`max_media_kb`, `media_quota_mb`, `media_per_minute`) — past the quota the least
used ones go, so a loop making drafts churns only its own; the key under a password is
asked for at `escrow_gets_per_minute_per_ip`. Behind a proxy on the same machine
(cloudflared, nginx), `client_ip_header` names the header with the real address —
believed only from a loopback peer.

API (JSON; PROTOCOL.md — every call but the first two names its journal):

    GET    /v2                         {"fedtail-sync": 2, …what it keeps}
    POST   /v2/blobs                   body: one blob; X-Fedtail-Device → {"seq": n}
    GET    /v2/blobs?after=N&limit=M&exclude=DEVICE
                                       {"blobs": [{"seq", "device", "data": base64}],
                                        "last": N, "more": bool}
    DELETE /v2/blobs                   the journal (its pictures, its key) gone
    DELETE /v2/devices/<d>?before=N    that device's older blobs gone (a snapshot sent)
    PUT    /v2/media/<name>            body: one sealed picture of a draft → {"stored"}
    GET    /v2/media/<name>            its bytes
    DELETE /v2/media/<name>            → {"deleted": 0 | 1}
    PUT    /v2/escrow                  the key sealed by a password (one per journal),
                                       its id in X-Fedtail-Escrow (never in the URL)
    GET    /v2/escrow                  its bytes — no journal named: that's what it's for
"""

import argparse
import base64
import hashlib
import ipaddress
import json
import logging
import re
import secrets
import socket
import sqlite3
import threading
import time
import tomllib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

__version__ = "0.22.0.dev0"  # the same as fedtail's: they are released together
VERSION = 2            # the protocol (GET /v2 says it); a client checks this one
log = logging.getLogger("fedtail-sync")

DEFAULTS = {
    "listen": "127.0.0.1:8471",
    "db": "fedtail-sync.db",
    "quota_mb": 20,             # per journal
    "max_blob_kb": 512,
    "requests_per_minute": 120,  # per journal
    "keep_days": 0,             # 0: blobs are kept until the journal is deleted
    "total_mb": 1024,           # all journals together; 0 — no cap
    "timeout_seconds": 30,      # a silent connection is dropped after this
    "max_connections": 64,      # served at once; more are closed at the door
    "requests_per_minute_per_ip": 240,
    "new_journals_per_minute_per_ip": 3,   # journals made (each needs an invite code)
    "escrow_gets_per_minute_per_ip": 5,    # the key under a password asked for (an
                                           # IPv6 address counts by its /64)
    "escrow_gets_per_minute": 60,          # …by everyone together; 0 — no cap
    "journals_per_invite": 5,              # journals one invite code may make; 0 — any
    "client_ip_header": "",     # e.g. "CF-Connecting-IP"; believed from loopback only
    # the pictures of drafts (sealed like the blobs): one at most max_media_kb; an
    # account keeps media_quota_mb of them — the least used go first to make room, so no
    # loop of new drafts fills more; unused media_keep_days — gone; media_per_minute puts
    "max_media_kb": 4096,
    "media_quota_mb": 50,       # 0 — no pictures here
    "media_keep_days": 30,
    "media_per_minute": 30,     # per journal
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    device  TEXT NOT NULL,
    at      INTEGER NOT NULL,
    data    BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS blobs_account ON blobs (account, seq);
CREATE TABLE IF NOT EXISTS media (
    account TEXT NOT NULL,
    name    TEXT NOT NULL,
    at      INTEGER NOT NULL,   -- ms: put or taken last
    data    BLOB NOT NULL,
    PRIMARY KEY (account, name)
);
CREATE INDEX IF NOT EXISTS media_used ON media (account, at);
-- a journal is its key's: the id and a hash of the secret the devices derive from it,
-- and the invite it was made with (taken back — the journal goes)
CREATE TABLE IF NOT EXISTS journals (
    id      TEXT PRIMARY KEY,
    secret  TEXT NOT NULL,
    invite  TEXT NOT NULL,
    made    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    name    TEXT PRIMARY KEY,
    code    TEXT NOT NULL UNIQUE,   -- sha256 of the code: the code itself is shown once
    made    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS escrow (
    id      TEXT PRIMARY KEY,       -- derived from a password by the client
    journal TEXT NOT NULL UNIQUE,
    data    BLOB NOT NULL
);
"""
# options of older versions, said and ignored: public_url fed /.well-known/fedtail-sync
# (gone with fedtail 0.19); the fediverse token check went with protocol 2 (0.22)
RETIRED = {"public_url": "no more /.well-known: fedtail takes the address from sync.url",
           "instances": "protocol 2: a journal is its key's, made with an invite code",
           "verifications_per_minute_per_ip": "protocol 2: no token checks any more",
           "verifications_per_minute": "protocol 2: no token checks any more"}
PAGE_MAX = 500
SEQ_MAX = 2 ** 62    # beyond any real sequence; bigger numbers are nonsense
DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{0,32}$")
MEDIA_RE = re.compile(r"^[0-9a-f]{32,64}$")  # a picture's name: the client's keyed hash
JOURNAL_RE = re.compile(r"^[0-9a-f]{32}$")     # a journal's id, derived from its key
SECRET_RE = re.compile(r"^[0-9a-f]{64}$")      # its secret, derived from the key too
ESCROW_RE = re.compile(r"^[0-9a-f]{64}$")      # the key under a password: its id
NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")  # an invite's name: whom it was for
ESCROW_MAX = 4096


class Refused(Exception):
    def __init__(self, status, text):
        super().__init__(text)
        self.status = status


class Window:
    """Counts per key in one-minute windows, thread-safe, never growing without end."""

    def __init__(self, limit_per_minute, max_keys=50_000):
        self.limit = limit_per_minute
        self.max_keys = max_keys
        self.counts = {}
        self.lock = threading.Lock()

    def hit(self, key):
        """Count one; → False when the key is over its limit this minute (0: no limit)."""
        if not self.limit:
            return True
        now = time.monotonic()
        with self.lock:
            w = self.counts.get(key)
            if w is None or now - w[0] > 60:
                if len(self.counts) >= self.max_keys:
                    self.counts = {k: v for k, v in self.counts.items() if now - v[0] <= 60}
                    if len(self.counts) >= self.max_keys:
                        return False  # flooded with keys: no one new this minute
                w = self.counts[key] = [now, 0]
            w[1] += 1
            return w[1] <= self.limit


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _code_of(text):
    """An invite code as people type it: case, spaces and dashes don't count."""
    return re.sub(r"[\s-]", "", str(text or "")).upper()


class Server:
    def __init__(self, cfg):
        self.cfg = {**DEFAULTS, **cfg}
        self.db = sqlite3.connect(self.cfg["db"], check_same_thread=False,
                                  isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()
        # journals of protocol 1 were filed under a fediverse account ("id@host"): nobody
        # can reach them any more — the devices send their state anew under protocol 2
        old = self.db.execute("DELETE FROM blobs WHERE account LIKE '%@%'").rowcount
        old += self.db.execute("DELETE FROM media WHERE account LIKE '%@%'").rowcount
        if old:
            log.info("%d blobs and pictures of protocol 1 journals dropped", old)
        self.per_account = Window(self.cfg["requests_per_minute"])
        self.per_ip = Window(self.cfg["requests_per_minute_per_ip"])
        self.new_journals = Window(self.cfg["new_journals_per_minute_per_ip"])
        self.escrow_gets = Window(self.cfg["escrow_gets_per_minute_per_ip"])
        self.escrow_gets_all = Window(self.cfg["escrow_gets_per_minute"])
        # bytes kept: per journal and in all (checked on every put without a scan)
        self.used = dict(self.db.execute(
            "SELECT account, SUM(LENGTH(data)) FROM blobs GROUP BY account").fetchall())
        self.media_used = dict(self.db.execute(
            "SELECT account, SUM(LENGTH(data)) FROM media GROUP BY account").fetchall())
        self.total = sum(self.used.values()) + sum(self.media_used.values())
        self.media_puts = Window(self.cfg["media_per_minute"])
        self._media_clock = 0  # ms, never twice the same: the least used is exact

    # ---- invites (the admin's, from the command line) ---------------------------------
    def invite_add(self, name, code=None):
        """A new code for `name`. → the code, shown once (only its hash is kept)."""
        if not NAME_RE.match(name or ""):
            raise ValueError("an invite's name: letters, digits, _ . @ -")
        code = _code_of(code) if code else "-".join(
            secrets.token_hex(2).upper() + secrets.token_hex(2).upper() for _ in range(3))
        with self.lock:
            if self.db.execute("SELECT 1 FROM invites WHERE name=?", (name,)).fetchone():
                raise ValueError(f"{name}: there is an invite of that name")
            self.db.execute("INSERT INTO invites VALUES (?, ?, ?)",
                            (name, _hash(_code_of(code)), int(time.time())))
        return code

    def invites(self):
        """[(name, made, journals made with it)]."""
        with self.lock:
            return self.db.execute(
                "SELECT i.name, i.made, (SELECT COUNT(*) FROM journals j WHERE j.invite = "
                "i.name) FROM invites i ORDER BY i.name").fetchall()

    def invite_revoke(self, name):
        """The code taken back and the journals made with it gone. → how many journals,
        or None — no such invite."""
        with self.lock:
            if not self.db.execute("SELECT 1 FROM invites WHERE name=?", (name,)).fetchone():
                return None
            ids = [r[0] for r in self.db.execute("SELECT id FROM journals WHERE invite=?",
                                                 (name,))]
            self.db.execute("DELETE FROM invites WHERE name=?", (name,))
        for jid in ids:
            self.wipe(jid, journal_too=True)
        return len(ids)

    # ---- who is asking -----------------------------------------------------------------
    def let_in(self, ip):
        """The door: every request counts against its address."""
        if not self.per_ip.hit(ip):
            raise Refused(HTTPStatus.TOO_MANY_REQUESTS, "too many requests from you, slow down")

    def account(self, headers, ip="?"):
        """The journal a request names, its secret checked; made now with an invite code
        if it isn't here yet. Refused otherwise."""
        auth = headers.get("Authorization") or ""
        secret = auth[7:].strip().lower() if auth.lower().startswith("bearer ") else ""
        jid = (headers.get("X-Fedtail-Journal") or "").strip().lower()
        if not JOURNAL_RE.match(jid) or not SECRET_RE.match(secret):
            raise Refused(HTTPStatus.UNAUTHORIZED, "X-Fedtail-Journal and its secret, please")
        with self.lock:
            row = self.db.execute("SELECT secret FROM journals WHERE id=?", (jid,)).fetchone()
        if row is None:
            self._make(jid, secret, headers.get("X-Fedtail-Invite"), ip)
        elif not secrets.compare_digest(row[0], _hash(secret)):
            raise Refused(HTTPStatus.UNAUTHORIZED, "not this journal's secret")
        if not self.per_account.hit(jid):
            raise Refused(HTTPStatus.TOO_MANY_REQUESTS, "too many requests, slow down")
        return jid

    def _make(self, jid, secret, code, ip):
        if not code:
            raise Refused(HTTPStatus.FORBIDDEN, "a new journal needs an invite code")
        if not self.new_journals.hit(ip):
            raise Refused(HTTPStatus.TOO_MANY_REQUESTS, "too many new journals, later")
        with self.lock:
            row = self.db.execute("SELECT name FROM invites WHERE code=?",
                                  (_hash(_code_of(code)),)).fetchone()
            if row is None:
                raise Refused(HTTPStatus.FORBIDDEN, "no such invite code (or taken back)")
            most = self.cfg["journals_per_invite"]
            if most and self.db.execute("SELECT COUNT(*) FROM journals WHERE invite=?",
                                        (row[0],)).fetchone()[0] >= most:
                raise Refused(HTTPStatus.FORBIDDEN, "this invite code has made all the "
                                                    "journals it may")
            self.db.execute("INSERT OR IGNORE INTO journals VALUES (?, ?, ?, ?)",
                            (jid, _hash(secret), row[0], int(time.time())))
            got = self.db.execute("SELECT secret FROM journals WHERE id=?", (jid,)).fetchone()
        if not secrets.compare_digest(got[0], _hash(secret)):  # made at once by another
            raise Refused(HTTPStatus.UNAUTHORIZED, "not this journal's secret")
        log.info("a journal made with the invite %s", row[0])

    # ---- the journal -----------------------------------------------------------------
    def put(self, acc, device, blob):
        if not blob:
            raise Refused(HTTPStatus.BAD_REQUEST, "an empty blob")
        if len(blob) > self.cfg["max_blob_kb"] * 1024:
            raise Refused(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "the blob is too big")
        if not DEVICE_RE.match(device):
            raise Refused(HTTPStatus.BAD_REQUEST, "X-Fedtail-Device: letters and digits")
        with self.lock:
            if self.used.get(acc, 0) + len(blob) > self.cfg["quota_mb"] * 1024 * 1024:
                raise Refused(HTTPStatus.INSUFFICIENT_STORAGE, "over the quota")
            cap = self.cfg["total_mb"] * 1024 * 1024
            if cap and self.total + len(blob) > cap:
                log.warning("the server is full (total_mb = %d)", self.cfg["total_mb"])
                raise Refused(HTTPStatus.INSUFFICIENT_STORAGE, "the server is full")
            cur = self.db.execute("INSERT INTO blobs (account, device, at, data) "
                                  "VALUES (?, ?, ?, ?)", (acc, device, int(time.time()), blob))
            self.used[acc] = self.used.get(acc, 0) + len(blob)
            self.total += len(blob)
            return cur.lastrowid

    def get(self, acc, after, limit, exclude):
        limit = max(1, min(int(limit), PAGE_MAX))
        after = max(0, min(int(after), SEQ_MAX))
        with self.lock:
            rows = self.db.execute(
                "SELECT seq, device, data FROM blobs WHERE account=? AND seq>? "
                "ORDER BY seq LIMIT ?", (acc, after, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        # the cursor moves past the asker's own blobs too, though they aren't sent
        last = rows[-1][0] if rows else after
        return {"blobs": [{"seq": s, "device": d, "data": base64.b64encode(b).decode()}
                          for s, d, b in rows if not exclude or d != exclude],
                "last": last, "more": more}

    def compact(self, acc, device, before):
        """A device's own blobs older than `before` gone: it has just sent everything they
        said, packed into a snapshot, and wants the old ones off the quota."""
        if not device or not DEVICE_RE.match(device):
            raise Refused(HTTPStatus.BAD_REQUEST, "a device id")
        before = max(0, min(int(before), SEQ_MAX))
        with self.lock:
            freed, n = self.db.execute(
                "SELECT COALESCE(SUM(LENGTH(data)), 0), COUNT(*) FROM blobs "
                "WHERE account=? AND device=? AND seq<?", (acc, device, before)).fetchone()
            self.db.execute("DELETE FROM blobs WHERE account=? AND device=? AND seq<?",
                            (acc, device, before))
            self.used[acc] = self.used.get(acc, 0) - freed
            self.total -= freed
            return n

    def wipe(self, acc, journal_too=False):
        """The journal's blobs gone, its pictures and its key under a password with them;
        journal_too — the journal itself (its invite taken back)."""
        with self.lock:
            n = self.db.execute("DELETE FROM blobs WHERE account=?", (acc,)).rowcount
            self.db.execute("DELETE FROM media WHERE account=?", (acc,))
            self.db.execute("DELETE FROM escrow WHERE journal=?", (acc,))
            if journal_too:
                self.db.execute("DELETE FROM journals WHERE id=?", (acc,))
            self.total -= self.used.pop(acc, 0) + self.media_used.pop(acc, 0)
            return n

    # ---- the key under a password ------------------------------------------------------
    def escrow_put(self, acc, eid, data):
        """The journal's key sealed by a password, under an id the client derived from
        the password and the account: one per journal, a new one replaces the journal's
        own — never another journal's (409): an id guessed can't take one's place."""
        if not ESCROW_RE.match(eid):
            raise Refused(HTTPStatus.BAD_REQUEST, "an escrow id: 64 hex digits")
        if not data or len(data) > ESCROW_MAX:
            raise Refused(HTTPStatus.BAD_REQUEST, "a key under a password: 1…4096 bytes")
        with self.lock:
            row = self.db.execute("SELECT journal FROM escrow WHERE id=?", (eid,)).fetchone()
            if row is not None and row[0] != acc:
                raise Refused(HTTPStatus.CONFLICT, "this id is another journal's")
            self.db.execute("DELETE FROM escrow WHERE journal=?", (acc,))
            self.db.execute("INSERT INTO escrow VALUES (?, ?, ?)", (eid, acc, data))
        return True

    def escrow_get(self, eid, ip="?"):
        """Its bytes, asked without a journal (a new device has only the password): the
        id is all there is, so asking is slow per address."""
        if not ESCROW_RE.match(eid):
            raise Refused(HTTPStatus.BAD_REQUEST, "an escrow id: 64 hex digits")
        if not self.escrow_gets.hit(network(ip)) or not self.escrow_gets_all.hit("*"):
            raise Refused(HTTPStatus.TOO_MANY_REQUESTS, "too many tries, later")
        with self.lock:
            row = self.db.execute("SELECT data FROM escrow WHERE id=?", (eid,)).fetchone()
        if row is None:
            raise Refused(HTTPStatus.NOT_FOUND, "no such key here")
        return row[0]

    # ---- the pictures of drafts --------------------------------------------------------
    def media_on(self):
        return self.cfg["media_quota_mb"] > 0

    def _media_now(self):
        """When a picture was last put or taken, in ms (under self.lock)."""
        self._media_clock = max(int(time.time() * 1000), self._media_clock + 1)
        return self._media_clock

    def _media_name(self, name):
        if not self.media_on():
            raise Refused(HTTPStatus.NOT_FOUND, "no pictures here")
        if not MEDIA_RE.match(name):
            raise Refused(HTTPStatus.BAD_REQUEST, "a picture's name: 32 to 64 hex digits")

    def media_put(self, acc, name, data):
        """One sealed picture under its name. → True: stored; False: it was there (its
        time touched). Past the account's quota the least used ones go first — a loop of
        new drafts only ever churns its own pictures."""
        self._media_name(name)
        if not data:
            raise Refused(HTTPStatus.BAD_REQUEST, "an empty picture")
        quota = self.cfg["media_quota_mb"] * 1024 * 1024
        if len(data) > self.cfg["max_media_kb"] * 1024 or len(data) > quota:
            raise Refused(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "the picture is too big")
        if not self.media_puts.hit(acc):
            raise Refused(HTTPStatus.TOO_MANY_REQUESTS, "too many pictures, slow down")
        with self.lock:
            now = self._media_now()
            if self.db.execute("UPDATE media SET at=? WHERE account=? AND name=?",
                               (now, acc, name)).rowcount:
                return False
            used = self.media_used.get(acc, 0)
            while used + len(data) > quota:
                old, size = self.db.execute(
                    "SELECT name, LENGTH(data) FROM media WHERE account=? ORDER BY at "
                    "LIMIT 1", (acc,)).fetchone()
                self.db.execute("DELETE FROM media WHERE account=? AND name=?", (acc, old))
                used -= size
                self.total -= size
            self.media_used[acc] = used
            cap = self.cfg["total_mb"] * 1024 * 1024
            if cap and self.total + len(data) > cap:
                log.warning("the server is full (total_mb = %d)", self.cfg["total_mb"])
                raise Refused(HTTPStatus.INSUFFICIENT_STORAGE, "the server is full")
            self.db.execute("INSERT INTO media (account, name, at, data) VALUES (?, ?, ?, ?)",
                            (acc, name, now, data))
            self.media_used[acc] = used + len(data)
            self.total += len(data)
            return True

    def media_get(self, acc, name):
        """The sealed bytes; its time touched (a picture in use doesn't expire)."""
        self._media_name(name)
        with self.lock:
            row = self.db.execute("SELECT data FROM media WHERE account=? AND name=?",
                                  (acc, name)).fetchone()
            if row is None:
                raise Refused(HTTPStatus.NOT_FOUND, "no such picture")
            self.db.execute("UPDATE media SET at=? WHERE account=? AND name=?",
                            (self._media_now(), acc, name))
        return row[0]

    def media_delete(self, acc, name):
        self._media_name(name)
        with self.lock:
            row = self.db.execute("SELECT LENGTH(data) FROM media WHERE account=? AND name=?",
                                  (acc, name)).fetchone()
            if row is None:
                return 0
            self.db.execute("DELETE FROM media WHERE account=? AND name=?", (acc, name))
            self.media_used[acc] = self.media_used.get(acc, 0) - row[0]
            self.total -= row[0]
            return 1

    def expire(self):
        days = self.cfg["keep_days"]
        if days:
            with self.lock:
                n = self.db.execute("DELETE FROM blobs WHERE at < ?",
                                    (int(time.time()) - days * 86400,)).rowcount
                if n:
                    self.used = dict(self.db.execute(
                        "SELECT account, SUM(LENGTH(data)) FROM blobs GROUP BY account"))
                    self.total = sum(self.used.values()) + sum(self.media_used.values())
            if n:
                log.info("expired %d blobs older than %d days", n, days)
        days = self.cfg["media_keep_days"]
        if days:
            with self.lock:
                n = self.db.execute("DELETE FROM media WHERE at < ?",  # at: ms
                                    (int(time.time() * 1000) - days * 86_400_000,)).rowcount
                if n:
                    self.media_used = dict(self.db.execute(
                        "SELECT account, SUM(LENGTH(data)) FROM media GROUP BY account"))
                    self.total = sum(self.used.values()) + sum(self.media_used.values())
            if n:
                log.info("expired %d pictures unused for %d days", n, days)


def network(ip):
    """What a limit counts an address by: an IPv6 address by its /64 (one machine is
    given a whole /64), any other as it is."""
    try:
        addr = ipaddress.ip_address(str(ip).split("%", 1)[0])
    except ValueError:
        return str(ip)
    if addr.version == 6:
        if addr.ipv4_mapped:  # ::ffff:a.b.c.d — an IPv4 address, counted as one
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def real_ip(peer, given):
    """The address requests count against: what a proxy on this machine says (`given`,
    from the header client_ip_header names) when the peer is loopback and it holds an
    address; the peer itself otherwise — so a client can't name itself someone else."""
    try:
        local = ipaddress.ip_address(peer.split("%", 1)[0]).is_loopback
    except ValueError:
        local = False
    given = (given or "").strip()
    if local and given:
        try:
            return str(ipaddress.ip_address(given))
        except ValueError:
            pass
    return peer


def cut(sock):
    """Close a connection from another thread, whatever state it is in."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # closed already


def _number(q, name, default):
    text = q.get(name, "")
    if text == "":
        return default
    if not re.fullmatch(r"\d{1,19}", text):
        raise Refused(HTTPStatus.BAD_REQUEST, f"{name}: a number was expected")
    return min(int(text), SEQ_MAX)


class Handler(BaseHTTPRequestHandler):
    server_version = f"fedtail-sync/{VERSION}"
    sys_version = ""
    protocol_version = "HTTP/1.0"  # one request per connection: nothing kept open
    app = None  # the Server, set by make_http_server
    timeout = 30  # set from the config by make_http_server

    def log_message(self, fmt, *args):  # one line per request, never a token
        log.info("%s %s", getattr(self, "client_ip", self.client_address[0]), fmt % args)

    def _send(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _real_ip(self):
        name = self.app.cfg["client_ip_header"]
        return real_ip(self.client_address[0], self.headers.get(name) if name else None)

    def _bytes(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _body(self, max_kb=None):
        """Exactly Content-Length bytes, or Refused: no length, a strange one, too big,
        or fewer bytes than promised before the timeout."""
        text = self.headers.get("Content-Length")
        if text is None or self.headers.get("Transfer-Encoding"):
            raise Refused(HTTPStatus.LENGTH_REQUIRED, "Content-Length, please")
        if not re.fullmatch(r"\d{1,12}", text.strip()):
            raise Refused(HTTPStatus.BAD_REQUEST, "Content-Length: a number")
        n = int(text)
        if n > (max_kb or self.app.cfg["max_blob_kb"]) * 1024:
            raise Refused(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "the body is too big")
        data = self.rfile.read(n)
        if len(data) != n:
            raise Refused(HTTPStatus.BAD_REQUEST, "the body is shorter than promised")
        return data

    def _route(self, method):
        self.client_ip = self._real_ip()
        url = urlsplit(self.path)
        try:
            q = {k: v[-1] for k, v in parse_qs(url.query, max_num_fields=10).items()}
            self.app.let_in(self.client_ip)
            path = url.path
            if method == "GET" and path in ("/v1", "/v1/", "/v2", "/v2/"):
                # a client of protocol 1 asking /v1 learns there's another one here
                hello = {"fedtail-sync": VERSION, "compact": 1, "escrow": 1}
                if self.app.media_on():
                    hello["media_kb"] = self.app.cfg["max_media_kb"]
                return self._send(200, hello)
            if not path.startswith("/v2/"):
                return self._send(404, {"error": "no such thing (protocol 2: /v2)"})
            path = path[len("/v2"):]
            if path == "/escrow":
                return self._escrow(method, (self.headers.get("X-Fedtail-Escrow") or "")
                                    .strip().lower())
            if path.startswith("/media/"):
                return self._media(method, path[len("/media/"):])
            if path.startswith("/devices/"):
                if method != "DELETE":
                    return self._send(501, {"error": "not this method"})
                device = path[len("/devices/"):]
                if not device or not DEVICE_RE.match(device):  # before the journal
                    raise Refused(HTTPStatus.BAD_REQUEST, "a device id")
                acc = self.app.account(self.headers, self.client_ip)
                return self._send(200, {"deleted": self.app.compact(
                    acc, device, _number(q, "before", 0))})
            if path != "/blobs":
                return self._send(404, {"error": "no such thing"})
            if method == "PUT":
                return self._send(501, {"error": "not this method"})
            acc = self.app.account(self.headers, self.client_ip)
            if method == "POST":
                blob = self._body()
                device = self.headers.get("X-Fedtail-Device") or ""
                return self._send(200, {"seq": self.app.put(acc, device, blob)})
            if method == "GET":
                exclude = q.get("exclude", "")
                if not DEVICE_RE.match(exclude):
                    raise Refused(HTTPStatus.BAD_REQUEST, "exclude: a device id")
                return self._send(200, self.app.get(acc, _number(q, "after", 0),
                                                    _number(q, "limit", 200), exclude))
            return self._send(200, {"deleted": self.app.wipe(acc)})
        except Refused as e:
            if e.status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE:
                self.close_connection = True  # its body stays unread
            return self._send(e.status, {"error": str(e)})
        except (TimeoutError, ConnectionError):
            self.close_connection = True  # the client went quiet or away: nothing to say
        except ValueError:
            return self._send(400, {"error": "a bad request"})
        except Exception:  # noqa: BLE001 — logged here, never shown to the client
            log.exception("%s %s failed", method, url.path)
            return self._send(500, {"error": "internal error"})

    def _escrow(self, method, eid):
        """PUT (a device of the journal) / GET (anyone with the id) /v2/escrow, the id in
        X-Fedtail-Escrow: a header, so no log of a request line holds it."""
        app = self.app
        if method == "GET":
            return self._bytes(app.escrow_get(eid, self.client_ip))
        if method != "PUT":
            return self._send(501, {"error": "not this method"})
        acc = app.account(self.headers, self.client_ip)
        return self._send(200, {"stored": app.escrow_put(acc, eid, self._body(4))})

    def _media(self, method, name):
        """PUT / GET / DELETE /v2/media/<name>: one sealed picture of a draft."""
        app = self.app
        app._media_name(name)  # before the journal: no work spent on nonsense
        acc = app.account(self.headers, self.client_ip)
        if method == "PUT":
            return self._send(200, {"stored": app.media_put(
                acc, name, self._body(app.cfg["max_media_kb"]))})
        if method == "GET":
            return self._bytes(app.media_get(acc, name))
        if method == "DELETE":
            return self._send(200, {"deleted": app.media_delete(acc, name)})
        return self._send(501, {"error": "not this method"})

    def do_GET(self):  # noqa: N802 — http.server's names
        self._route("GET")

    def do_POST(self):  # noqa: N802
        self._route("POST")

    def do_DELETE(self):  # noqa: N802
        self._route("DELETE")

    def do_PUT(self):  # noqa: N802
        self._route("PUT")


class HTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a cap: past `max_connections` at once, a new connection
    is closed at the door instead of getting a thread of its own."""

    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, handler, max_connections):
        self.slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            log.warning("%s: too many connections, closed", client_address[0])
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        # a whole request in twice the timeout: a client trickling a byte now and then
        # (each under the timeout) is cut all the same
        timer = threading.Timer(2 * self.RequestHandlerClass.timeout, cut, (request,))
        timer.daemon = True
        timer.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            timer.cancel()
            self.slots.release()

    def handle_error(self, request, client_address):  # a dropped client: one quiet line
        log.debug("%s: connection error", client_address[0], exc_info=True)


def make_http_server(app):
    host, _, port = app.cfg["listen"].rpartition(":")
    host = host.strip("[]") or "127.0.0.1"
    handler = type("BoundHandler", (Handler,), {"app": app,
                                                 "timeout": app.cfg["timeout_seconds"]})
    try:
        if not ipaddress.ip_address(host).is_loopback:
            log.warning("listening on %s: plain HTTP to the network — put it behind a "
                        "proxy that does TLS and listen on 127.0.0.1", host)
    except ValueError:
        pass
    cls = type("HTTPServer6", (HTTPServer,), {"address_family": socket.AF_INET6}) \
        if ":" in host else HTTPServer
    return cls((host, int(port)), handler, app.cfg["max_connections"])


def load_config(path):
    with open(path, "rb") as f:
        data = tomllib.load(f)
    for gone in set(RETIRED) & set(data):  # an older config: said, not fatal
        log.warning("option %s is gone (%s) — ignored", gone, RETIRED[gone])
        del data[gone]
    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise SystemExit(f"fedtail-sync: unknown options {sorted(unknown)} in {path}")
    return data


def invite_command(app, words, out):
    """`invite add <name> [code]` / `invite list` / `invite revoke <name>`. → exit code."""
    what, rest = (words[0] if words else ""), words[1:]
    if what == "add" and rest:
        try:
            code = app.invite_add(rest[0], rest[1] if len(rest) > 1 else None)
        except ValueError as e:
            print(f"fedtail-sync: {e}", file=out)
            return 1
        print(f"invite {rest[0]}: {code}", file=out)
        print("(shown this once: only its hash is kept; give it to the person — their "
              "first device puts it in ≡ → Sync)", file=out)
        return 0
    if what == "list" and not rest:
        rows = app.invites()
        for name, made, n in rows:
            when = time.strftime("%Y-%m-%d", time.gmtime(made))
            print(f"{name}\tmade {when}\tjournals {n}", file=out)
        if not rows:
            print("no invites yet: invite add <name>", file=out)
        return 0
    if what == "revoke" and len(rest) == 1:
        n = app.invite_revoke(rest[0])
        if n is None:
            print(f"fedtail-sync: no invite {rest[0]}", file=out)
            return 1
        print(f"invite {rest[0]} taken back, {n} journal(s) made with it gone", file=out)
        return 0
    print("invite add <name> [code] | invite list | invite revoke <name>", file=out)
    return 2


def main(argv=None, out=None):
    import sys
    p = argparse.ArgumentParser(prog="fedtail-sync", description=__doc__.split("\n")[0])
    p.add_argument("command", nargs="*", help="none — serve; invite add <name> [code] | "
                   "invite list | invite revoke <name>")
    p.add_argument("--config", help="a TOML file with the options (see README.md)")
    p.add_argument("--listen", help=f"address:port (default {DEFAULTS['listen']})")
    p.add_argument("--db", help="the SQLite file")
    p.add_argument("--version", action="version",
                   version=f"fedtail-sync {__version__} (protocol {VERSION})")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = load_config(a.config) if a.config else {}
    for k in ("listen", "db"):
        if getattr(a, k):
            cfg[k] = getattr(a, k)
    app = Server(cfg)
    if a.command:
        if a.command[0] != "invite":
            p.error(f"unknown command {a.command[0]!r}")
        try:
            return invite_command(app, a.command[1:], out or sys.stdout)
        finally:
            app.db.close()
    if not app.invites():
        log.warning("no invite codes yet: nobody can make a journal (invite add <name>)")
    httpd = make_http_server(app)
    log.info("fedtail-sync %s (protocol %d) on %s", __version__, VERSION, app.cfg["listen"])

    stop = threading.Event()

    def expiring():  # keep_days: old journals go, once an hour
        while True:
            app.expire()
            if stop.wait(3600):
                return
    keeper = threading.Thread(target=expiring, daemon=True, name="expire")
    keeper.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        keeper.join(5)
        httpd.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover — run as a program, not imported
    raise SystemExit(main())
