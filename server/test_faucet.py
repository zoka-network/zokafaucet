"""Tests of the faucet with a fake wallet engine, a fake Turnstile and a
temporary database. Run with: python -m unittest discover -s server"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import faucet
from faucet import ATOMS, App, Config, State, Store, Wallet, WalletError, Worker

ALICE = "zqtest2YVVQP8Qx2W3oSUNvVhJGz6Y1occM4Tzh14mkrYQC3u1GjorYg"
BOB = "zqtest22TPzeg3HUWdHrcbuZPTWHgb1tSb3BEmpQFmT8ADXtNueehssTj"
CAROL = "zqtest2245FcQcWQaHxUkCSB1j9j8n5yT6x1DQwBEweHDFzwrux3qvTp6"


def store(tmp: str) -> Store:
    return Store(Path(tmp) / "f.sqlite", b"salt")


class Helpers(unittest.TestCase):
    def test_ip_keys(self):
        self.assertEqual(faucet.ip_key("203.0.113.7"), "203.0.113.7")
        self.assertEqual(faucet.ip_key("2001:db8:1:2:3:4:5:6"), "2001:db8:1:2::/64")
        self.assertEqual(faucet.ip_key("2001:db8:1:2:ffff::9"), "2001:db8:1:2::/64")
        self.assertEqual(faucet.ip_key("::ffff:203.0.113.7"), "203.0.113.7")
        self.assertEqual(faucet.ip_key("nonsense"), "invalid")

    def test_amounts(self):
        self.assertEqual(faucet.format_zka(10 * ATOMS), "10")
        self.assertEqual(faucet.format_zka(1_234_500_000), "1,234.5")
        self.assertEqual(faucet.format_zka(1), "0.000001")

    def test_short_addresses(self):
        for good in (ALICE, BOB, CAROL):
            self.assertTrue(faucet.SHORT_ADDRESS.match(good))
        for bad in ("", "zqtest2", "zq1" + ALICE[7:], ALICE + "0", ALICE.replace("Y", "l"), "zqtest2" + "F" * 1700):
            self.assertFalse(faucet.SHORT_ADDRESS.match(bad), bad)


class Limits(unittest.TestCase):
    def test_one_claim_per_wallet_and_per_ip_a_day(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            s = store(tmp)
            now = 1_000_000.0
            first, _ = s.admit(ALICE, "203.0.113.7", 10 * ATOMS, 400 * ATOMS, now)
            self.assertIsNotNone(first)
            again, why = s.admit(ALICE, "198.51.100.1", 10 * ATOMS, 400 * ATOMS, now + 60)
            self.assertIsNone(again)
            self.assertTrue(why.startswith("wallet:"))
            same_ip, why = s.admit(BOB, "203.0.113.7", 10 * ATOMS, 400 * ATOMS, now + 60)
            self.assertIsNone(same_ip)
            self.assertTrue(why.startswith("ip:"))
            later, _ = s.admit(ALICE, "203.0.113.7", 10 * ATOMS, 400 * ATOMS, now + faucet.DAY + 1)
            self.assertIsNotNone(later)

    def test_ipv6_counts_by_64(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            s = store(tmp)
            self.assertIsNotNone(s.admit(ALICE, "2001:db8:1:2::10", 10 * ATOMS, 400 * ATOMS, 1.0)[0])
            self.assertIsNone(s.admit(BOB, "2001:db8:1:2::99", 10 * ATOMS, 400 * ATOMS, 2.0)[0])
            self.assertIsNotNone(s.admit(BOB, "2001:db8:1:3::99", 10 * ATOMS, 400 * ATOMS, 3.0)[0])

    def test_daily_cap_and_failed_claims(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            s = store(tmp)
            a, _ = s.admit(ALICE, "203.0.113.1", 10 * ATOMS, 20 * ATOMS, 1.0)
            s.admit(BOB, "203.0.113.2", 10 * ATOMS, 20 * ATOMS, 2.0)
            capped, why = s.admit(CAROL, "203.0.113.3", 10 * ATOMS, 20 * ATOMS, 3.0)
            self.assertEqual((capped, why), (None, "cap"))
            # A claim that failed gives its place back, and its wallet can try again.
            s.set(a, "failed", error="x")
            self.assertIsNotNone(s.admit(CAROL, "203.0.113.3", 10 * ATOMS, 20 * ATOMS, 4.0)[0])
            self.assertIsNotNone(s.admit(ALICE, "203.0.113.1", 10 * ATOMS, 30 * ATOMS, 5.0)[0])

    def test_positions_and_ips_are_not_stored(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            s = store(tmp)
            a, _ = s.admit(ALICE, "203.0.113.1", ATOMS, 99 * ATOMS, 1.0)
            b, _ = s.admit(BOB, "203.0.113.2", ATOMS, 99 * ATOMS, 2.0)
            self.assertEqual(s.get(a)["position"], 1)
            self.assertEqual(s.get(b)["position"], 2)
            raw = (Path(tmp) / "f.sqlite").read_bytes()
            self.assertNotIn(b"203.0.113", raw)

    def test_a_claim_being_paid_is_not_paid_again_after_a_restart(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            s = store(tmp)
            a, _ = s.admit(ALICE, "203.0.113.1", ATOMS, 99 * ATOMS, 1.0)
            s.set(a, "sending")
            s.close()
            again = store(tmp)
            self.assertEqual(again.get(a)["status"], "sent")
            self.assertIsNone(again.next_queued())
            again.close()


class FakeEngine:
    """Plays `zoka quantic-pq-wallet`: answers balance and send."""

    def __init__(self):
        self.balance = {"spendable_atoms": 20_000 * ATOMS, "max_sendable_atoms": 19_999 * ATOMS, "pending_payment": None, "node_behind": False}
        self.calls: list[list[str]] = []
        self.send_error = ""
        # What the node code prints before the answer when the mirror takes a checkpoint.
        self.before = '{"checkpoint_accepted":5326,"hash":"ab","from":"8aa55aa80f"}' + "\n"
        self.send_reply = json.dumps({"txid": "ab" * 32, "fee_atoms": 200})

    def __call__(self, args, **kw):
        self.calls.append(args)
        self.env = kw["env"]
        action = args[2]
        if action == "balance":
            return subprocess.CompletedProcess(args, 0, self.before + json.dumps(self.balance), "")
        if self.send_error:
            return subprocess.CompletedProcess(args, 1, "", self.send_error)
        self.balance["pending_payment"] = {"txid": "ab" * 32}
        self.balance["reserved_atoms"] = 20_000 * ATOMS
        self.balance["spendable_atoms"] = 0
        return subprocess.CompletedProcess(args, 0, self.before + self.send_reply, "")


def config(tmp: str) -> Config:
    return Config(data=Path(tmp), peer=Path(tmp) / "seed2.json", password="pw", turnstile_secret="sk")


class Paying(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.config = config(self.tmp.name)
        self.store = store(self.tmp.name)
        self.engine = FakeEngine()
        self.state = State()
        self.worker = Worker(self.config, self.store, Wallet(self.config, run=self.engine), self.state)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pays_one_claim_and_waits_for_its_block(self):
        a, _ = self.store.admit(ALICE, "203.0.113.1", 10 * ATOMS, 400 * ATOMS, time.time())
        b, _ = self.store.admit(BOB, "203.0.113.2", 10 * ATOMS, 400 * ATOMS, time.time() + 1)
        self.worker.step()
        self.assertEqual(self.store.get(a)["status"], "sent")
        self.assertEqual(self.store.get(a)["txid"], "ab" * 32)
        send = self.engine.calls[-1]
        self.assertEqual(send[send.index("--to") + 1], ALICE)
        self.assertEqual(send[send.index("--amount") + 1], "10")
        self.assertEqual(send[send.index("--peer") + 1], str(Path(self.tmp.name) / "seed2.json"))
        self.assertEqual(self.engine.env["FAUCET_PASSWORD"], "pw")
        self.assertNotIn("pw", " ".join(send))
        # The payment waits for its block: Bob waits too.
        self.worker.step()
        self.assertEqual(self.store.get(b)["status"], "queued")
        self.engine.balance["pending_payment"] = None
        self.worker.step()
        self.assertEqual(self.store.get(b)["status"], "sent")

    def test_waits_while_the_mirror_reads_the_chain(self):
        a, _ = self.store.admit(ALICE, "203.0.113.1", 10 * ATOMS, 400 * ATOMS, time.time())
        self.engine.balance["node_behind"] = True
        self.worker.step()
        self.assertEqual(self.store.get(a)["status"], "queued")
        self.assertFalse(any(c[2] == "send" for c in self.engine.calls))

    def test_transient_errors_retry_and_others_fail(self):
        a, _ = self.store.admit(ALICE, "203.0.113.1", 10 * ATOMS, 400 * ATOMS, time.time())
        self.engine.send_error = "the wallet's node is not in sync (lag); send when it is"
        self.worker.step()
        self.assertEqual(self.store.get(a)["status"], "queued")
        self.engine.send_error = "--to: no published address"
        self.worker.step()
        found = self.store.get(a)
        self.assertEqual(found["status"], "failed")
        self.assertIn("could not pay that address", found["error"])

    def test_a_sent_payment_whose_reply_cannot_be_read_still_counts_as_sent(self):
        a, _ = self.store.admit(ALICE, "203.0.113.1", 10 * ATOMS, 400 * ATOMS, time.time())
        self.engine.send_reply = "Sent. (not JSON)"
        self.worker.step()
        found = self.store.get(a)
        self.assertEqual(found["status"], "sent")
        self.assertEqual(found["txid"], "ab" * 32)  # from the wallet's pending payment
        # The reserved note still counts in the faucet's balance.
        self.assertEqual(self.state.spendable_atoms + self.state.reserved_atoms, 20_000 * ATOMS)

    def test_reads_the_answer_after_other_json_lines(self):
        self.assertEqual(faucet.last_json_line('{"checkpoint_accepted":1}' + "\n" + '{"txid":"ab"}' + "\n"), {"txid": "ab"})
        self.assertIsNone(faucet.last_json_line("no json here"))
        self.assertEqual(faucet.last_json_line('{"a":1}' + "\n" + "{broken"), {"a": 1})

    def test_an_empty_faucet_keeps_the_queue(self):
        a, _ = self.store.admit(ALICE, "203.0.113.1", 10 * ATOMS, 400 * ATOMS, time.time())
        self.engine.balance.update(spendable_atoms=3 * ATOMS, max_sendable_atoms=2 * ATOMS)
        self.worker.step()
        self.assertTrue(self.state.empty)
        self.assertEqual(self.store.get(a)["status"], "queued")


class Http(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.config = config(self.tmp.name)
        self.store = store(self.tmp.name)
        self.state = State(spendable_atoms=20_000 * ATOMS, max_sendable_atoms=19_999 * ATOMS, in_sync=True, checked=1.0)
        self.tokens: list[str] = []
        self.published = {ALICE, BOB}

        def turnstile(cfg, token, ip):
            self.tokens.append(token)
            return (token == "good", "" if token == "good" else "refused")

        def published(api, short):
            if short == CAROL:
                raise WalletError("node API: down")
            return short in self.published

        app = App(self.config, self.store, self.state, turnstile=turnstile, published=published)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), faucet.make_handler(app))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json", "Origin": "https://faucet.zoka.network", "CF-Connecting-IP": "203.0.113.7"}
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), dict(e.headers)

    def test_a_claim_from_start_to_queue(self):
        status, body, headers = self.call("POST", "/faucet/claim", {"address": ALICE, "token": "good"})
        self.assertEqual(status, 202)
        self.assertEqual((body["status"], body["position"], body["amount"]), ("queued", 1, "10"))
        self.assertEqual(headers["Access-Control-Allow-Origin"], "https://faucet.zoka.network")
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, found, _ = self.call("GET", f"/faucet/claims/{body['id']}")
        self.assertEqual((status, found["status"]), (200, "queued"))
        status, again, _ = self.call("POST", "/faucet/claim", {"address": ALICE, "token": "good"})
        self.assertEqual(status, 429)
        self.assertIn("already got its ZKA today", again["error"])
        status, figures, _ = self.call("GET", "/faucet/status")
        self.assertEqual((figures["amount"], figures["daily_cap"], figures["left_24h"], figures["queue"]), ("10", "400", "390", 1))
        self.assertTrue(figures["ready"])

    def test_refusals(self):
        status, body, _ = self.call("POST", "/faucet/claim", {"address": "zqtest2bad", "token": "good"})
        self.assertEqual((status, body["field"]), (400, "address"))
        status, body, _ = self.call("POST", "/faucet/claim", {"address": ALICE, "token": ""})
        self.assertEqual((status, body["field"]), (400, "token"))
        status, body, _ = self.call("POST", "/faucet/claim", {"address": ALICE, "token": "bad"})
        self.assertEqual((status, body["field"]), (403, "token"))
        # An unpublished address does not spend the check.
        spent = len(self.tokens)
        unknown = "zqtest2" + "9" * 48
        status, body, _ = self.call("POST", "/faucet/claim", {"address": unknown, "token": "good"}, {"CF-Connecting-IP": "203.0.113.8"})
        self.assertEqual((status, body["field"]), (400, "address"))
        self.assertEqual(len(self.tokens), spent)
        status, body, _ = self.call("POST", "/faucet/claim", {"address": CAROL, "token": "good"}, {"CF-Connecting-IP": "203.0.113.9"})
        self.assertEqual(status, 503)
        status, _, _ = self.call("POST", "/faucet/claim", None, {"Content-Type": "text/plain"})
        self.assertIn(status, (413, 415))
        status, _, _ = self.call("GET", "/faucet/claims/not-an-id")
        self.assertEqual(status, 404)
        status, _, _ = self.call("GET", "/scan2?from=1")
        self.assertEqual(status, 404)

    def test_cors_only_for_the_faucet_pages(self):
        _, _, ok = self.call("GET", "/faucet/status", headers={"Origin": "https://abc123.zokafaucet.pages.dev"})
        self.assertEqual(ok["Access-Control-Allow-Origin"], "https://abc123.zokafaucet.pages.dev")
        _, _, other = self.call("GET", "/faucet/status", headers={"Origin": "https://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", other)
        req = urllib.request.Request(self.base + "/faucet/claim", method="OPTIONS", headers={"Origin": "https://faucet.zoka.network"})
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 204)
            self.assertIn("POST", r.headers["Access-Control-Allow-Methods"])
            self.assertEqual(r.headers["Access-Control-Allow-Headers"], "Content-Type")

    def test_too_many_tries_per_minute(self):
        codes = [self.call("POST", "/faucet/claim", {"address": ALICE, "token": "bad"}, {"CF-Connecting-IP": "198.51.100.5"})[0] for _ in range(8)]
        self.assertEqual(codes[:6], [403] * 6)
        self.assertEqual(codes[6:], [429, 429])


if __name__ == "__main__":
    unittest.main()
