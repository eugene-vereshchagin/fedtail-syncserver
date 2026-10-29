"""fedtail-sync doing its job: whose journal is whose (its key's, made with an invite),
the limits an honest client meets, the key under a password, invites given and taken
back, old journals going, the config and the program itself."""

import http.client
import io
import json
import logging
import socket
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from .live import CODE, ME, YOU, Live, Tmp, creds


class JournalTest(Live):
    def setUp(self):
        super().setUp()
        self.start(quota_mb=1, max_blob_kb=64)

    def test_whose_journal_is_whose(self):
        hello = {"fedtail-sync": 2, "compact": 1, "escrow": 1, "media_kb": 4096}
        self.assertEqual(self.call("GET", "/v2"), (200, hello))
        self.assertEqual(self.call("GET", "/v1"), (200, hello))  # protocol 1 learns of 2
        self.assertEqual(self.call("GET", "/v1/")[1]["fedtail-sync"], 2)
        self.assertEqual(self.call("GET", "/.well-known/fedtail-sync")[0], 404)  # gone
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/v1/blobs")  # protocol 1's paths: gone
        self.assertEqual(c.getresponse().status, 404)
        c.close()
        self.assertEqual(self.call("GET", "/v2/nothing", "t-me")[0], 404)
        self.assertEqual(self.call("POST", "/v1/blobs", "t-me", b"one",
                                   {"X-Fedtail-Device": "d1"}), (200, {"seq": 1}))
        self.assertEqual(self.call("POST", "/v1/blobs", "t-me2", b"two",
                                   {"X-Fedtail-Device": "d2"})[0], 200)
        self.assertEqual(self.call("POST", "/v1/blobs", "t-you", b"theirs")[0], 200)
        code, got = self.call("GET", "/v1/blobs?after=0&exclude=d1", "t-me")
        self.assertEqual(code, 200)
        self.assertEqual([b["device"] for b in got["blobs"]], ["d2"])  # not d1, not you
        self.assertEqual(got["last"], 2)
        self.assertEqual(self.call("GET", "/v1/blobs?after=2", "t-me")[1]["blobs"], [])
        self.assertEqual(self.call("DELETE", "/v1/blobs", "t-me"), (200, {"deleted": 2}))
        self.assertEqual(len(self.call("GET", "/v1/blobs", "t-you")[1]["blobs"]), 1)

    def test_pages(self):
        for i in range(5):
            self.call("POST", "/v1/blobs", "t-me", f"b{i}".encode(), {"X-Fedtail-Device": "a"})
        code, got = self.call("GET", "/v1/blobs?after=0&limit=2", "t-me")
        self.assertEqual((len(got["blobs"]), got["last"], got["more"]), (2, 2, True))
        got = self.call("GET", "/v1/blobs?after=4&limit=0", "t-me")[1]  # at least one
        self.assertEqual((len(got["blobs"]), got["more"]), (1, False))
        # the asker's own blobs aren't sent, but the cursor moves past them
        got = self.call("GET", "/v1/blobs?after=0&exclude=a", "t-me")[1]
        self.assertEqual((got["blobs"], got["last"]), ([], 5))

    def test_strangers_refused(self):
        self.assertEqual(self.call("GET", "/v1/blobs")[0], 401)
        self.assertEqual(self.call("GET", "/v1/blobs", "t-me")[0], 200)  # made, with CODE
        self.assertEqual(self.call("GET", "/v1/blobs", "nope")[0], 401)  # its secret, wrong
        self.assertEqual(self.call("GET", "/v1/blobs", "t-me", invite=None)[0], 200)  # there
        self.assertEqual(self.call("GET", "/v1/blobs", "t-you", invite=None)[0], 403)
        self.assertEqual(self.call("GET", "/v1/blobs", "t-you", invite="NOPE")[0], 403)
        jid, secret = creds("t-me")
        for h in ({"Authorization": "Basic dDp0", "X-Fedtail-Journal": jid},
                  {"Authorization": f"Bearer {secret}", "X-Fedtail-Journal": "../x"},
                  {"Authorization": "Bearer short", "X-Fedtail-Journal": jid}):
            self.assertEqual(self.call("GET", "/v1/blobs", headers=h)[0], 401)
        # an invite code as people type it: case, spaces and dashes don't count
        self.assertEqual(self.call("GET", "/v1/blobs", "t-you",
                                   invite=" test code 0001 ".lower())[0], 200)

    def test_the_quota_and_the_size(self):
        self.assertEqual(self.call("POST", "/v1/blobs", "t-me", b"x" * 70 * 1024)[0], 413)
        for _ in range(17):
            self.call("POST", "/v1/blobs", "t-me", b"x" * 60 * 1024)
        self.assertEqual(self.call("POST", "/v1/blobs", "t-me", b"x" * 60 * 1024)[0], 507)
        self.assertEqual(self.call("POST", "/v1/blobs", "t-you", b"x" * 60 * 1024)[0], 200)

    def test_put_checks_on_its_own_too(self):
        with self.assertRaises(self.mod.Refused) as e:
            self.app.put(ME, "d", b"")
        self.assertEqual(e.exception.status, 400)
        with self.assertRaises(self.mod.Refused) as e:
            self.app.put(ME, "d", b"x" * 65 * 1024)
        self.assertEqual(e.exception.status, 413)

    def test_an_empty_body_is_refused(self):
        self.assertEqual(self.call("POST", "/v1/blobs", "t-me", b"")[0], 400)

    def test_a_body_cut_short_by_the_client(self):
        jid, secret = creds("t-me")
        self.call("GET", "/v1/blobs", "t-me")  # the journal made
        out = self.raw(f"POST /v2/blobs HTTP/1.1\r\nAuthorization: Bearer {secret}\r\n"
                       f"X-Fedtail-Journal: {jid}\r\nContent-Length: 100\r\n\r\n"
                       f"abc".encode(), half_close=True)
        self.assertIn(b" 400 ", out)
        self.assertIn(b"shorter than promised", out)

    def test_other_methods_arent_served(self):
        self.assertEqual(self.call("PUT", "/v1/blobs", "t-me", b"x")[0], 501)

    def test_a_device_packs_its_own_journal(self):
        """DELETE /v1/devices/<d>?before=N: that device's blobs older than N go — its
        snapshot (N and on) says all they did; the others' blobs stay, the quota frees."""
        for dev, body in (("d1", b"old1"), ("d2", b"theirs"), ("d1", b"old2"),
                          ("d1", b"snapshot")):
            self.call("POST", "/v1/blobs", "t-me", body, {"X-Fedtail-Device": dev})
        used = self.app.used[ME]
        self.assertEqual(self.call("DELETE", "/v1/devices/d1?before=4", "t-me"),
                         (200, {"deleted": 2}))
        got = self.call("GET", "/v1/blobs?after=0", "t-me")[1]
        self.assertEqual([(b["seq"], b["device"]) for b in got["blobs"]],
                         [(2, "d2"), (4, "d1")])
        self.assertEqual(self.app.used[ME], used - 8)
        self.assertEqual(self.call("DELETE", "/v1/devices/d1?before=4", "t-you"),
                         (200, {"deleted": 0}))  # whose: another journal's d1 is not mine
        self.assertEqual(self.call("DELETE", "/v1/devices/d1", "t-me"),
                         (200, {"deleted": 0}))  # no before: nothing
        self.assertEqual(self.call("DELETE", "/v1/devices/bad!?before=4", "t-me")[0], 400)
        self.assertEqual(self.call("DELETE", "/v1/devices/?before=4", "t-me")[0], 400)
        self.assertEqual(self.call("DELETE", "/v1/devices/d1?before=x", "t-me")[0], 400)
        self.assertEqual(self.call("GET", "/v1/devices/d1", "t-me")[0], 501)
        self.assertEqual(self.call("DELETE", "/v1/devices/d1?before=4")[0], 401)
        with self.assertRaises(self.mod.Refused) as e:  # checked when called directly too
            self.app.compact(ME, "", 4)
        self.assertEqual(e.exception.status, 400)


N1, N2, N3 = "a" * 32, "b" * 40, "c" * 64  # pictures' names: the client's keyed hashes


class MediaTest(Live):
    """The pictures of drafts: sealed bytes under a name; a quota of their own where the
    least used go first, so a loop of new drafts can't fill the disk or lock the
    account's journal; a pace, a size, an age."""

    def setUp(self):
        super().setUp()
        self.start(quota_mb=1, max_blob_kb=64, max_media_kb=100, media_quota_mb=1,
                   media_per_minute=1000)

    def test_put_get_delete(self):
        self.assertEqual(self.call("PUT", f"/v1/media/{N1}", "t-me", b"pic1"),
                         (200, {"stored": True}))
        self.assertEqual(self.call("PUT", f"/v1/media/{N1}", "t-me", b"pic1"),
                         (200, {"stored": False}))  # there already: counted once
        self.assertEqual(self.call("GET", f"/v1/media/{N1}", "t-me"), (200, b"pic1"))
        self.assertEqual(self.call("GET", f"/v1/media/{N1}", "t-you")[0], 404)  # whose
        self.assertEqual(self.call("DELETE", f"/v1/media/{N1}", "t-me"),
                         (200, {"deleted": 1}))
        self.assertEqual(self.call("DELETE", f"/v1/media/{N1}", "t-me"),
                         (200, {"deleted": 0}))
        self.assertEqual(self.call("GET", f"/v1/media/{N1}", "t-me")[0], 404)
        self.assertEqual(self.app.media_used[ME], 0)

    def test_names_sizes_methods_and_tokens(self):
        self.assertEqual(self.call("PUT", "/v1/media/NOTHEX", "t-me", b"x")[0], 400)
        self.assertNotIn(ME, self.app.used)  # nonsense made no journal
        self.assertEqual(self.call("PUT", f"/v1/media/{N1}", None, b"x")[0], 401)
        self.assertEqual(self.call("PUT", f"/v1/media/{N1}", "t-me", b"")[0], 400)
        self.assertEqual(self.call("PUT", f"/v1/media/{N2}", "t-me", b"x" * 101 * 1024)[0],
                         413)
        self.assertEqual(self.call("POST", f"/v1/media/{N1}", "t-me", b"x")[0], 501)
        with self.assertRaises(self.mod.Refused) as e:  # the size, when called directly
            self.app.media_put(ME, N3, b"x" * 101 * 1024)
        self.assertEqual(e.exception.status, 413)

    def test_a_loop_of_drafts_churns_only_its_own(self):
        """1 MB of pictures at most: the 11th of 100 KB pushes out the least used — the
        journal and the other accounts don't feel it."""
        for i in range(11):
            name = f"{i:032x}"
            self.assertEqual(self.call("PUT", f"/v1/media/{name}", "t-me",
                                       b"p" * 100 * 1024)[0], 200)
            if i == 9:  # the first one used again: the second is now the least used
                self.call("GET", f"/v1/media/{0:032x}", "t-me")
        self.assertLessEqual(self.app.media_used[ME], 1024 * 1024)
        self.assertEqual(self.call("GET", f"/v1/media/{0:032x}", "t-me")[0], 200)
        self.assertEqual(self.call("GET", f"/v1/media/{1:032x}", "t-me")[0], 404)  # gone
        self.assertEqual(self.call("POST", "/v1/blobs", "t-me", b"journal")[0], 200)
        self.assertEqual(self.call("PUT", f"/v1/media/{N1}", "t-you", b"theirs")[0], 200)

    def test_the_whole_server_full(self):
        self.app.cfg["total_mb"] = 1
        self.app.total = 1024 * 1024
        self.assertEqual(self.call("PUT", f"/v1/media/{N1}", "t-me", b"x")[0], 507)

    def test_the_pace(self):
        self.app.media_puts = self.mod.Window(2)
        for i in range(2):
            self.assertEqual(self.call("PUT", f"/v1/media/{i:032x}", "t-me", b"x")[0], 200)
        self.assertEqual(self.call("PUT", f"/v1/media/{N1}", "t-me", b"x")[0], 429)

    def test_the_journal_wiped_takes_its_pictures(self):
        self.call("PUT", f"/v1/media/{N1}", "t-me", b"pic")
        self.call("POST", "/v1/blobs", "t-me", b"one")
        self.assertEqual(self.call("DELETE", "/v1/blobs", "t-me"), (200, {"deleted": 1}))
        self.assertEqual(self.call("GET", f"/v1/media/{N1}", "t-me")[0], 404)
        self.assertEqual(self.app.total, 0)

    def test_unused_pictures_expire(self):
        self.call("PUT", f"/v1/media/{N1}", "t-me", b"old")
        self.call("PUT", f"/v1/media/{N2}", "t-me", b"new")
        self.app.db.execute("UPDATE media SET at = at - 40 * 86400000 WHERE name=?", (N1,))
        self.app.expire()
        self.assertEqual(self.call("GET", f"/v1/media/{N1}", "t-me")[0], 404)
        self.assertEqual(self.call("GET", f"/v1/media/{N2}", "t-me")[0], 200)
        self.assertEqual(self.app.total, 3)
        self.app.cfg["media_keep_days"] = 0
        self.app.expire()  # 0: kept until deleted
        self.assertEqual(self.call("GET", f"/v1/media/{N2}", "t-me")[0], 200)

    def test_counted_again_at_start(self):
        self.call("PUT", f"/v1/media/{N1}", "t-me", b"pic")
        again = self.mod.Server({"db": str(self.tmp / "s.db")})
        self.addCleanup(again.db.close)
        self.assertEqual(again.media_used, {ME: 3})
        self.assertEqual(again.total, 3)


class NoMediaTest(Live):
    def test_off(self):
        self.start(media_quota_mb=0)
        self.assertEqual(self.call("GET", "/v2"), (200, {"fedtail-sync": 2, "compact": 1,
                                                    "escrow": 1}))
        self.assertEqual(self.call("PUT", f"/v1/media/{N1}", "t-me", b"x")[0], 404)


class AccountLimitTest(Live):
    def test_one_account_has_its_limit_whatever_its_addresses(self):
        self.start(requests_per_minute=3, client_ip_header="X-Real-IP")
        codes = [self.call("GET", "/v1/blobs", "t-me",
                           headers={"X-Real-IP": f"198.51.100.{i}"})[0] for i in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])
        self.assertEqual(self.call("GET", "/v1/blobs", "t-you",
                                   headers={"X-Real-IP": "198.51.100.9"})[0], 200)  # others

    def test_a_zero_limit_is_no_limit(self):
        w = self.mod.Window(0)
        self.assertTrue(all(w.hit("k") for _ in range(1000)))
        self.assertEqual(w.counts, {})

    def test_the_minute_passes(self):
        w = self.mod.Window(1)
        with mock.patch.object(self.mod.time, "monotonic", return_value=1000.0):
            self.assertEqual([w.hit("k"), w.hit("k")], [True, False])
        with mock.patch.object(self.mod.time, "monotonic", return_value=1061.0):
            self.assertTrue(w.hit("k"))


class InviteTest(Live):
    """Invite codes: one per person, shown once, any number of them; taken back, the
    journals made with it go — the others stay."""

    def test_given_listed_and_taken_back(self):
        self.start()
        anna = self.app.invite_add("anna")
        self.assertRegex(anna, r"^[0-9A-F]{8}-[0-9A-F]{8}-[0-9A-F]{8}$")
        with self.assertRaises(ValueError):
            self.app.invite_add("anna")  # one of a name
        with self.assertRaises(ValueError):
            self.app.invite_add("no spaces please")
        self.assertNotIn(anna, str(self.app.db.execute("SELECT * FROM invites").fetchall()))
        self.call("POST", "/v1/blobs", "t-me", b"mine")              # with CODE ("tests")
        self.call("POST", "/v1/blobs", "t-you", b"hers", invite=anna)
        self.call("PUT", "/v2/escrow", "t-you", b"her key", headers={"X-Fedtail-Escrow": "e" * 64})
        self.assertEqual([(n, j) for n, _m, j in self.app.invites()],
                         [("anna", 1), ("tests", 1)])
        self.assertEqual(self.app.invite_revoke("anna"), 1)
        self.assertIsNone(self.app.invite_revoke("anna"))
        self.assertEqual(self.call("GET", "/v1/blobs", "t-you", invite=None)[0], 403)
        self.assertEqual(self.call("GET", "/v1/blobs", "t-you", invite=anna)[0], 403)
        self.assertEqual(self.call("GET", "/v2/escrow", headers={"X-Fedtail-Escrow": "e" * 64})[0],
                         404)
        self.assertNotIn(YOU, self.app.used)
        self.assertEqual(len(self.call("GET", "/v1/blobs", "t-me")[1]["blobs"]), 1)

    def test_new_journals_have_a_pace(self):
        self.start(new_journals_per_minute_per_ip=1)
        self.assertEqual(self.call("GET", "/v1/blobs", "t-me")[0], 200)
        self.assertEqual(self.call("GET", "/v1/blobs", "t-you")[0], 429)
        self.assertEqual(self.call("GET", "/v1/blobs", "t-me")[0], 200)  # made: no pace

    def test_one_code_makes_so_many_journals(self):
        """journals_per_invite: a code given to one person can't fill the server with
        journals (each has its quota); the journals it made go on working."""
        self.start(journals_per_invite=1)
        self.assertEqual(self.call("GET", "/v1/blobs", "t-me")[0], 200)
        got = self.call("GET", "/v1/blobs", "t-you")
        self.assertEqual(got[0], 403)
        self.assertIn("all the journals", got[1]["error"])
        self.assertEqual(self.call("GET", "/v1/blobs", "t-me")[0], 200)

    def test_two_at_once_one_journal(self):
        """Made at the same moment by two with different secrets: the second is told it
        isn't this journal's, not let in."""
        self.start()
        self.app.account({"Authorization": "Bearer " + "1" * 64, "X-Fedtail-Journal": "a" * 32,
                          "X-Fedtail-Invite": CODE})
        real = self.app.db.execute
        calls = []

        def racing(sql, args=()):  # someone else's row lands between the look and the insert
            if sql.startswith("INSERT OR IGNORE INTO journals") and not calls:
                calls.append(1)
                real("INSERT INTO journals VALUES (?, ?, ?, ?)",
                     ("b" * 32, self.mod._hash("2" * 64), "tests", 0))
            return real(sql, args)
        self.app.db = type("Db", (), {"execute": staticmethod(racing),
                                      "close": real.__self__.close})()
        with self.assertRaises(self.mod.Refused) as e:
            self.app._make("b" * 32, "3" * 64, CODE, "?")
        self.assertEqual(e.exception.status, 401)

    def test_protocol_1_journals_go_at_start(self):
        self.start()
        self.app.put("1@me.example", "d", b"old")
        self.app.db.execute("INSERT INTO media VALUES ('1@me.example', ?, 0, ?)",
                            ("a" * 32, b"pic"))
        with self.assertLogs("fedtail-sync", "INFO") as logs:
            again = self.mod.Server({"db": str(self.tmp / "s.db")})
        self.addCleanup(again.db.close)
        self.assertIn("2 blobs and pictures of protocol 1", logs.output[0])
        self.assertEqual(again.total, 0)


class EscrowTest(Live):
    """The key under a password: put by a device of the journal, taken by anyone with
    the id derived from the password — slowly, per address."""

    def test_put_and_taken(self):
        self.start(escrow_gets_per_minute_per_ip=2)
        eid = "e" * 64

        def esc(method, eid, token=None, body=None):
            return self.call(method, "/v2/escrow", token, body,
                             headers={"X-Fedtail-Escrow": eid} if eid is not None else {})
        self.assertEqual(esc("PUT", eid, "t-me", b"sealed"), (200, {"stored": True}))
        self.assertEqual(esc("GET", eid), (200, b"sealed"))
        # another journal can't take its place under the same id (a guessed one)
        self.assertEqual(esc("PUT", eid, "t-you", b"theirs")[0], 409)
        esc("PUT", "f" * 64, "t-me", b"anew")  # one per journal: the old id goes
        self.assertEqual(esc("GET", eid)[0], 404)
        self.assertEqual(esc("GET", "f" * 64)[0], 429)  # the 3rd from this address
        self.assertEqual(esc("GET", "nothex")[0], 400)
        self.assertEqual(esc("GET", None)[0], 400)  # no id: the header is the place
        self.assertEqual(self.call("GET", "/v2/escrow/" + "f" * 64)[0], 404)  # not the URL
        self.assertEqual(esc("PUT", "f" * 64, None, b"x")[0], 401)
        self.assertEqual(esc("DELETE", eid, "t-me")[0], 501)
        self.assertEqual(esc("PUT", eid, "t-me", b"x" * 5000)[0], 413)
        for bad in ((ME, "nothex", b"x"), (ME, eid, b"")):
            with self.assertRaises(self.mod.Refused) as e:
                self.app.escrow_put(*bad)
            self.assertEqual(e.exception.status, 400)
        with self.assertRaises(self.mod.Refused):
            self.app.escrow_get("nothex")
        self.call("DELETE", "/v1/blobs", "t-me")  # the journal wiped: its key under a
        self.assertEqual(self.app.db.execute(  # password with it
            "SELECT COUNT(*) FROM escrow").fetchone()[0], 0)

    def test_asked_slowly_by_everyone_together(self):
        """Not only per address: an IPv6 /64 is one, and all the askers together have a
        ceiling of their own (many addresses can't sweep the ids)."""
        self.start(escrow_gets_per_minute_per_ip=100, escrow_gets_per_minute=2)
        for _ in range(2):
            with self.assertRaises(self.mod.Refused) as e:
                self.app.escrow_get("a" * 64, "2001:db8::1")
            self.assertEqual(e.exception.status, 404)
        with self.assertRaises(self.mod.Refused) as e:
            self.app.escrow_get("a" * 64, "198.51.100.7")  # another address: still no
        self.assertEqual(e.exception.status, 429)


class NamesTest(Tmp):
    def test_the_real_address(self):
        r = self.mod.real_ip
        self.assertEqual(r("127.0.0.1", "203.0.113.9"), "203.0.113.9")
        self.assertEqual(r("::1", " 2001:db8::1 "), "2001:db8::1")
        self.assertEqual(r("203.0.113.5", "198.51.100.1"), "203.0.113.5")  # not from a proxy
        self.assertEqual(r("127.0.0.1", "nonsense"), "127.0.0.1")
        self.assertEqual(r("127.0.0.1", None), "127.0.0.1")
        self.assertEqual(r("/run/sync.sock", "198.51.100.1"), "/run/sync.sock")  # no IP

    def test_what_a_limit_counts_by(self):
        n = self.mod.network
        self.assertEqual(n("2001:db8:1:2:3:4:5:6"), "2001:db8:1:2::/64")
        self.assertEqual(n("2001:db8:1:2::9"), n("2001:db8:1:2:ffff::1"))  # one /64
        self.assertEqual(n("::ffff:198.51.100.7"), "198.51.100.7")
        self.assertEqual(n("198.51.100.7"), "198.51.100.7")
        self.assertEqual(n("fe80::1%eth0"), "fe80::/64")
        self.assertEqual(n("/run/sync.sock"), "/run/sync.sock")


class ExpireTest(Tmp):
    def test_old_journals_go_and_the_count_follows(self):
        app = self.mod.Server({"db": str(self.tmp / "s.db"), "keep_days": 7})
        self.addCleanup(app.db.close)
        app.put(ME, "a", b"old" * 100)
        app.put(ME, "a", b"new")
        app.put(YOU, "b", b"old too")
        app.db.execute("UPDATE blobs SET at = ? WHERE data != ?",
                       (int(time.time()) - 8 * 86400, b"new"))
        with self.assertLogs("fedtail-sync", "INFO") as logs:
            app.expire()
        self.assertIn("expired 2", logs.output[0])
        self.assertEqual(app.db.execute("SELECT data FROM blobs").fetchall(), [(b"new",)])
        self.assertEqual((app.used, app.total), ({ME: 3}, 3))
        app.expire()  # nothing old: nothing said
        keep = self.mod.Server({"db": str(self.tmp / "s.db")})
        self.addCleanup(keep.db.close)
        keep.expire()  # keep_days = 0: kept for ever
        self.assertEqual(keep.total, 3)


class PlumbingTest(Live):
    def test_a_thread_that_wont_start_gives_its_slot_back(self):
        self.start(max_connections=1)
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        with mock.patch("socketserver.ThreadingMixIn.process_request",
                        side_effect=RuntimeError("no threads left")):
            with self.assertRaises(RuntimeError):
                self.httpd.process_request(a, ("127.0.0.1", 1))
        self.assertTrue(self.httpd.slots.acquire(blocking=False))  # the one slot is free
        self.httpd.slots.release()

    def test_cut_never_fails(self):
        s = socket.socket()
        s.close()
        self.mod.cut(s)  # closed already: nothing to do, nothing raised
        a, b = socket.socketpair()
        self.mod.cut(a)
        self.assertEqual(b.recv(1), b"")
        a.close()
        b.close()

    def test_a_dropped_client_is_one_quiet_line(self):
        self.start()
        with self.assertLogs("fedtail-sync", "DEBUG") as logs:
            try:
                raise ConnectionResetError
            except ConnectionResetError:
                self.httpd.handle_error(None, ("198.51.100.4", 5))
        self.assertIn("198.51.100.4: connection error", logs.output[0])

    def test_listening_beyond_this_machine_is_warned_about(self):
        app = self.mod.Server({"db": ":memory:", "listen": "0.0.0.0:0"})
        self.addCleanup(app.db.close)
        with self.assertLogs("fedtail-sync", "WARNING") as logs:
            srv = self.mod.make_http_server(app)
        srv.server_close()
        self.assertIn("plain HTTP", logs.output[0])
        named = self.mod.Server({"db": ":memory:", "listen": "localhost:0"})
        self.addCleanup(named.db.close)
        self.mod.make_http_server(named).server_close()  # a name: nothing to judge

    @unittest.skipUnless(socket.has_ipv6, "no IPv6 here")
    def test_ipv6(self):
        try:
            self.start(listen="[::1]:0")
        except OSError:
            self.skipTest("::1 can't be bound here")
        c = http.client.HTTPConnection("::1", self.port, timeout=5)
        c.request("GET", "/v2")
        self.assertEqual(json.loads(c.getresponse().read())["fedtail-sync"], 2)
        c.close()


class ConfigTest(Tmp):
    def test_an_older_config_still_starts(self):
        cfg = self.tmp / "s.toml"
        cfg.write_text('instances = ["me.example"]\npublic_url = "https://x.example"\n'
                       'verifications_per_minute = 5\nquota_mb = 3\n')
        with self.assertLogs("fedtail-sync", "WARNING") as logs:
            self.assertEqual(self.mod.load_config(cfg), {"quota_mb": 3})
        self.assertEqual(len(logs.output), 3)
        cfg.write_text('instancez = ["me.example"]\n')
        with self.assertRaises(SystemExit):
            self.mod.load_config(cfg)


class MainTest(Tmp):
    """The program: options, the config file, starting, the hourly clean-up, stopping."""

    def run_main(self, *argv):
        made = []
        real = self.mod.make_http_server

        def fake(app):
            srv = real(app)
            made.append((app, srv))
            srv.serve_forever = mock.Mock(side_effect=KeyboardInterrupt)  # Ctrl+C at once
            return srv
        with mock.patch.object(self.mod, "make_http_server", fake), \
                mock.patch.object(logging, "basicConfig"):
            self.mod.main(list(argv))
        app, srv = made[0]
        self.addCleanup(app.db.close)
        return app, srv

    def test_from_a_config_file(self):
        cfg = self.tmp / "c.toml"
        cfg.write_text(f'listen = "127.0.0.1:0"\ndb = "{self.tmp / "a.db"}"\nkeep_days = 3\n')
        with self.assertLogs("fedtail-sync", "INFO") as logs, \
                mock.patch.object(self.mod.Server, "expire") as expire:
            app, srv = self.run_main("--config", str(cfg))
        self.assertEqual(app.cfg["keep_days"], 3)
        expire.assert_called()  # the clean-up ran (and stopped with the server)
        self.assertTrue(any("no invite codes yet" in line for line in logs.output))
        self.assertEqual(srv.socket.fileno(), -1)  # closed on the way out

    def test_invites_from_the_command_line(self):
        db = str(self.tmp / "i.db")

        def run(*words):
            out = io.StringIO()
            with mock.patch.object(logging, "basicConfig"):
                code = self.mod.main(["--db", db, "invite", *words], out=out)
            return code, out.getvalue()
        code, said = run("add", "anna")
        self.assertEqual(code, 0)
        self.assertIn("invite anna: ", said)
        self.assertEqual(run("add", "anna")[0], 1)
        self.assertEqual(run("add", "bob", "my-own-code")[0], 0)
        code, said = run("list")
        self.assertIn("anna\tmade", said)
        self.assertIn("journals 0", said)
        self.assertEqual(run("revoke", "anna"), (0, "invite anna taken back, 0 journal(s) "
                                                   "made with it gone\n"))
        self.assertEqual(run("revoke", "anna")[0], 1)
        self.assertEqual(run("bogus")[0], 2)
        run("revoke", "bob")
        self.assertIn("no invites yet", run("list")[1])
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                mock.patch("sys.stderr", io.StringIO()):
            self.mod.main(["--db", db, "serve-it"])
        app, _ = self.run_main("--listen", "127.0.0.1:0", "--db", db)
        self.assertEqual(app.invites(), [])
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch.object(logging, "basicConfig"):
            self.mod.main(["--db", db, "invite", "list"])  # out: stdout by default
        self.assertIn("no invites yet", out.getvalue())

    def test_version(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            self.mod.main(["--version"])
        self.assertIn(f"fedtail-sync {self.mod.__version__} (protocol 2)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
