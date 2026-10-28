"""fedtail-sync faces the internet: each way to hang it, flood it, fill it or make it
say what it shouldn't, tried on a live server on localhost."""

import socket
import time
import unittest
from unittest import mock

from .live import Live, creds, load_server


class HangingTest(Live):
    def test_a_silent_connection_is_dropped(self):
        self.start(timeout_seconds=1)
        s = socket.create_connection(("127.0.0.1", self.port))
        self.addCleanup(s.close)
        t = time.monotonic()
        self.assertTrue(self.closed_within(s, 4))
        self.assertLess(time.monotonic() - t, 4)

    def test_a_trickling_client_is_cut_all_the_same(self):
        self.start(timeout_seconds=1)
        s = socket.create_connection(("127.0.0.1", self.port))
        self.addCleanup(s.close)
        s.sendall(b"GET /v1 HTTP/1.1\r\n")
        t = time.monotonic()
        cut = False
        while time.monotonic() - t < 6:
            time.sleep(0.5)  # each byte well under the timeout
            try:
                s.sendall(b"X")
            except OSError:
                cut = True
                break
        self.assertTrue(cut or self.closed_within(s, 1))
        self.assertLess(time.monotonic() - t, 5)  # cut at about twice the timeout

    def test_connections_over_the_cap_are_closed_at_the_door(self):
        self.start(max_connections=2, timeout_seconds=5)
        hold = [socket.create_connection(("127.0.0.1", self.port)) for _ in range(2)]
        for s in hold:
            self.addCleanup(s.close)
        time.sleep(0.3)  # both taken in
        extra = socket.create_connection(("127.0.0.1", self.port))
        self.addCleanup(extra.close)
        self.assertTrue(self.closed_within(extra, 2))
        for s in hold:
            s.close()
        time.sleep(0.3)
        self.assertEqual(self.call("GET", "/v1")[0], 200)  # the slots come back

    def test_a_body_shorter_than_promised_doesnt_hold_a_thread(self):
        self.start(timeout_seconds=1)
        jid, secret = creds("t-me")
        out = self.raw(f"POST /v2/blobs HTTP/1.1\r\nAuthorization: Bearer {secret}\r\n"
                       f"X-Fedtail-Journal: {jid}\r\nX-Fedtail-Invite: TEST-CODE-0001\r\n"
                       f"Content-Length: 100\r\n\r\nabc".encode(), wait=5)
        self.assertNotIn(b"<still open>", out)
        self.assertEqual(self.app.total, 0)


class BodyTest(Live):
    def setUp(self):
        super().setUp()
        self.start(max_blob_kb=1)

    def post_raw(self, headers):
        jid, secret = creds("t-me")
        head = f"POST /v2/blobs HTTP/1.1\r\nAuthorization: Bearer {secret}\r\n" \
               f"X-Fedtail-Journal: {jid}\r\nX-Fedtail-Invite: TEST-CODE-0001\r\n"
        return self.raw(head.encode() + headers + b"\r\n", wait=3)

    def test_strange_lengths_are_refused(self):
        self.assertIn(b" 400 ", self.post_raw(b"Content-Length: -1\r\n"))
        self.assertIn(b" 400 ", self.post_raw(b"Content-Length: abc\r\n"))
        self.assertIn(b" 400 ", self.post_raw(b"Content-Length: 99999999999999999999\r\n"))
        self.assertIn(b" 411 ", self.post_raw(b""))
        self.assertIn(b" 411 ", self.post_raw(b"Transfer-Encoding: chunked\r\n"))
        self.assertIn(b" 413 ", self.post_raw(b"Content-Length: 5000\r\n"))  # unread
        self.assertEqual(self.app.total, 0)

    def test_bad_numbers_and_ids_are_refused(self):
        for q in ("after=-1", "after=1e9", "after=" + "9" * 30, "limit=abc",
                  "exclude=../../x", "a=1&" * 20):
            self.assertEqual(self.call("GET", "/v1/blobs?" + q, "t-me")[0], 400, q)
        code, _ = self.call("POST", "/v1/blobs", "t-me", b"x",
                            {"X-Fedtail-Device": "<script>"})
        self.assertEqual(code, 400)
        self.assertEqual(self.call("GET", "/v1/blobs?after=" + "9" * 19, "t-me")[0], 200)

    def test_a_long_secret_is_not_even_looked_up(self):
        jid, _secret = creds("t-me")
        self.assertEqual(self.call("GET", "/v1/blobs", headers={
            "Authorization": "Bearer " + "a" * 600, "X-Fedtail-Journal": jid})[0], 401)
        self.assertNotIn(jid, self.app.used)


class FloodTest(Live):
    def test_one_address_has_its_limit(self):
        self.start(requests_per_minute_per_ip=5)
        codes = [self.call("GET", "/v1")[0] for _ in range(7)]
        self.assertEqual(codes[:5], [200] * 5)
        self.assertEqual(codes[5:], [429, 429])

    def test_guessing_a_journal_gets_nowhere_fast(self):
        """A journal's secret is 256 bits: no guessing it — but asking costs all the
        same, per address and per journal."""
        self.start(requests_per_minute=5)
        codes = [self.call("GET", "/v1/blobs", f"junk{i}", invite=None)[0] for i in range(4)]
        self.assertEqual(codes, [403, 403, 403, 403])  # no journal yet, and no invite
        self.call("GET", "/v1/blobs", "t-me")  # made
        codes = [self.call("GET", "/v1/blobs", f"junk{i}")[0] for i in range(4)]
        self.assertEqual(codes, [401] * 4)  # wrong secrets for it
        self.assertEqual(self.call("GET", "/v1/blobs", "t-me")[0], 200)

    def test_the_proxy_header_is_believed_only_when_named(self):
        self.start(requests_per_minute_per_ip=3)
        codes = [self.call("GET", "/v1", headers={"CF-Connecting-IP": f"203.0.113.{i}"})[0]
                 for i in range(5)]
        self.assertEqual(codes.count(429), 2)  # not named: everyone is 127.0.0.1
        self.start(requests_per_minute_per_ip=3, client_ip_header="CF-Connecting-IP")
        codes = [self.call("GET", "/v1", headers={"CF-Connecting-IP": f"203.0.113.{i}"})[0]
                 for i in range(5)]
        self.assertEqual(codes.count(429), 0)
        junk = [self.call("GET", "/v1", headers={"CF-Connecting-IP": "not an ip"})[0]
                for _ in range(4)]
        self.assertEqual(junk.count(429), 1)  # nonsense in it: the peer counts

    def test_the_window_never_grows_without_end(self):
        w = load_server().Window(5, max_keys=100)
        for i in range(500):
            w.hit(f"k{i}")
        self.assertLessEqual(len(w.counts), 100)


class DiskTest(Live):
    def test_the_whole_server_has_a_cap(self):
        self.start(quota_mb=1, total_mb=1, max_blob_kb=64)
        blob = b"x" * 60 * 1024
        while self.call("POST", "/v1/blobs", "t-me", blob)[0] == 200:
            pass
        code, err = self.call("POST", "/v1/blobs", "t-you", blob)
        self.assertEqual(code, 507)
        self.assertIn("full", err["error"])
        # the count survives a restart, and a wipe gives the room back
        used = self.app.total
        again = load_server().Server({"db": str(self.tmp / "s.db")})
        self.addCleanup(again.db.close)
        self.assertEqual(again.total, used)
        self.assertEqual(self.call("DELETE", "/v1/blobs", "t-me")[0], 200)
        self.assertEqual(self.app.total, 0)
        self.assertEqual(self.call("POST", "/v1/blobs", "t-you", blob)[0], 200)


class ErrorTest(Live):
    def test_a_failure_inside_says_nothing_of_itself(self):
        self.start()
        with mock.patch.object(self.mod.Server, "get",
                               side_effect=RuntimeError("secret detail /srv/sync.db")), \
                self.assertLogs("fedtail-sync", "ERROR"):
            code, body = self.call("GET", "/v1/blobs", "t-me")
        self.assertEqual((code, body), (500, {"error": "internal error"}))


if __name__ == "__main__":
    unittest.main()
