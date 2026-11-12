#!/usr/bin/env python3
"""Zoka faucet: test ZKA for wallets that do not mine (Zoka Quantic Testnet v3).

It runs next to seed2, behind the Cloudflare tunnel of api.zokascan.cc, which
sends it the paths under /faucet/. Only the Python standard library is used.

- A claim needs a published short address (zqtest2...) and a Cloudflare
  Turnstile token, checked here with the secret key.
- One claim every 24 hours per wallet and per client IP (IPv6 by /64), and at
  most --daily-cap-zka given in any 24 hours.
- Claims wait in a queue. One worker pays them, one at a time, with the wallet
  engine ZSilent uses (`zoka quantic-pq-wallet send`): a Zoka wallet has one
  payment in flight at a time, so the next one starts once the last is mined.
- Client IPs are stored only as salted SHA-256 hashes, and claims are deleted
  after --retention-days.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

ATOMS = 1_000_000
DAY = 24 * 3600
SHORT_ADDRESS = re.compile(r"^zqtest2[1-9A-HJ-NP-Za-km-z]{30,80}$")
CLAIM_ID = re.compile(r"^[0-9a-f]{32}$")
TURNSTILE_VERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
ACTIVE = ("queued", "sending", "sent")
MAX_BODY = 4096
MAX_TOKEN = 2048


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def format_zka(atoms: int) -> str:
    whole, frac = divmod(int(atoms), ATOMS)
    text = f"{whole:,}"
    frac_text = f"{frac:06d}".rstrip("0")
    return f"{text}.{frac_text}" if frac_text else text


# ── Configuration ───────────────────────────────────────────────────────────


@dataclass
class Config:
    data: Path
    zoka: str = "/usr/local/bin/zoka"
    api: str = "http://127.0.0.1:9190"
    peer: Optional[Path] = None
    wallet: str = "faucet"
    amount_atoms: int = 10 * ATOMS
    daily_cap_atoms: int = 400 * ATOMS
    memo: str = "Zoka faucet"
    origins: tuple[str, ...] = ("https://faucet.zoka.network", "https://zokafaucet.pages.dev")
    origin_pattern: str = r"^https://[a-z0-9-]+\.zokafaucet\.pages\.dev$"
    hostnames: tuple[str, ...] = ("faucet.zoka.network", "zokafaucet.pages.dev")
    retention_days: int = 30
    password: str = field(default="", repr=False)
    turnstile_secret: str = field(default="", repr=False)

    def origin_allowed(self, origin: str) -> bool:
        return origin in self.origins or bool(re.match(self.origin_pattern, origin or ""))

    def hostname_allowed(self, hostname: str) -> bool:
        return hostname in self.hostnames or hostname.endswith(".zokafaucet.pages.dev")


def read_secret(name: str, fallback_env: str) -> str:
    """A systemd credential, or an environment variable for local runs."""
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if directory:
        path = Path(directory) / name
        if path.exists():
            return path.read_text().strip()
    return os.environ.get(fallback_env, "").strip()


# ── Storage ─────────────────────────────────────────────────────────────────


def ip_key(ip: str) -> str:
    """The client: an IPv4 address, or the /64 of an IPv6 one."""
    try:
        address = ipaddress.ip_address(ip.strip())
    except ValueError:
        return "invalid"
    if address.version == 6:
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


class Store:
    """Claims in SQLite. One connection, guarded by a lock."""

    def __init__(self, path: Path, salt: bytes):
        self.salt = salt
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS claims (
                id TEXT PRIMARY KEY,
                created REAL NOT NULL,
                updated REAL NOT NULL,
                address TEXT NOT NULL,
                ip_hash TEXT NOT NULL,
                amount_atoms INTEGER NOT NULL,
                status TEXT NOT NULL,
                txid TEXT,
                error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0
            )"""
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS claims_address ON claims(address, created)")
        self.db.execute("CREATE INDEX IF NOT EXISTS claims_ip ON claims(ip_hash, created)")
        self.db.execute("CREATE INDEX IF NOT EXISTS claims_status ON claims(status, created)")
        # A claim that was being paid when the service stopped is never paid
        # again on its own: its payment may have left. It counts as sent.
        with self.lock:
            self.db.execute(
                "UPDATE claims SET status='sent', error='interrupted while sending' WHERE status='sending'"
            )

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def hash_ip(self, ip: str) -> str:
        return hashlib.sha256(self.salt + ip_key(ip).encode()).hexdigest()

    def admit(self, address: str, ip: str, amount: int, cap: int, now: float) -> tuple[Optional[str], str]:
        """A new queued claim, or why not: (claim id, "") or (None, reason)."""
        ip_hash = self.hash_ip(ip)
        since = now - DAY
        marks = ",".join("?" * len(ACTIVE))
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute(
                    f"SELECT MIN(created) FROM claims WHERE address=? AND created>? AND status IN ({marks})",
                    (address, since, *ACTIVE),
                ).fetchone()
                if row[0] is not None:
                    self.db.execute("ROLLBACK")
                    return None, f"wallet:{int(row[0] + DAY - now)}"
                row = self.db.execute(
                    f"SELECT MIN(created) FROM claims WHERE ip_hash=? AND created>? AND status IN ({marks})",
                    (ip_hash, since, *ACTIVE),
                ).fetchone()
                if row[0] is not None:
                    self.db.execute("ROLLBACK")
                    return None, f"ip:{int(row[0] + DAY - now)}"
                given = self.db.execute(
                    f"SELECT COALESCE(SUM(amount_atoms),0) FROM claims WHERE created>? AND status IN ({marks})",
                    (since, *ACTIVE),
                ).fetchone()[0]
                if given + amount > cap:
                    self.db.execute("ROLLBACK")
                    return None, "cap"
                claim = secrets.token_hex(16)
                self.db.execute(
                    "INSERT INTO claims(id,created,updated,address,ip_hash,amount_atoms,status) VALUES(?,?,?,?,?,?,'queued')",
                    (claim, now, now, address, ip_hash, amount),
                )
                self.db.execute("COMMIT")
                return claim, ""
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def get(self, claim: str) -> Optional[dict]:
        with self.lock:
            row = self.db.execute(
                "SELECT id,created,status,txid,error,amount_atoms FROM claims WHERE id=?", (claim,)
            ).fetchone()
            if not row:
                return None
            position = 0
            if row[2] == "queued":
                position = (
                    self.db.execute(
                        "SELECT COUNT(*) FROM claims WHERE status IN ('queued','sending') AND created<?",
                        (row[1],),
                    ).fetchone()[0]
                    + 1
                )
        return {
            "id": row[0],
            "status": row[2],
            "txid": row[3],
            "error": row[4],
            "amount_atoms": row[5],
            "position": position,
        }

    def next_queued(self) -> Optional[tuple[str, str, int, int]]:
        with self.lock:
            return self.db.execute(
                "SELECT id,address,amount_atoms,attempts FROM claims WHERE status='queued' ORDER BY created LIMIT 1"
            ).fetchone()

    def set(self, claim: str, status: str, *, txid: Optional[str] = None, error: Optional[str] = None, attempt: bool = False) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE claims SET status=?, txid=COALESCE(?,txid), error=?, updated=?, attempts=attempts+? WHERE id=?",
                (status, txid, error, time.time(), 1 if attempt else 0, claim),
            )

    def figures(self, now: float) -> dict:
        marks = ",".join("?" * len(ACTIVE))
        with self.lock:
            given = self.db.execute(
                f"SELECT COALESCE(SUM(amount_atoms),0) FROM claims WHERE created>? AND status IN ({marks})",
                (now - DAY, *ACTIVE),
            ).fetchone()[0]
            queue = self.db.execute("SELECT COUNT(*) FROM claims WHERE status IN ('queued','sending')").fetchone()[0]
            sent = self.db.execute("SELECT COUNT(*) FROM claims WHERE status='sent'").fetchone()[0]
        return {"given_24h_atoms": given, "queue": queue, "sent_total": sent}

    def purge(self, days: int, now: float) -> int:
        with self.lock:
            cursor = self.db.execute("DELETE FROM claims WHERE created<?", (now - days * DAY,))
            return cursor.rowcount


# ── The wallet engine and the node ──────────────────────────────────────────


class WalletError(Exception):
    pass


class Wallet:
    """`zoka quantic-pq-wallet`, the engine ZSilent uses, run for each step."""

    def __init__(self, config: Config, run: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self.config = config
        self.run = run

    def _call(self, action: str, extra: list[str], timeout: int) -> dict:
        c = self.config
        args = [c.zoka, "quantic-pq-wallet", action, "--network", "testnet", "--dir", str(c.data),
                "--name", c.wallet, "--password-env", "FAUCET_PASSWORD", "--api", c.api, "--json"]
        if c.peer:
            args += ["--peer", str(c.peer)]
        env = {"PATH": "/usr/bin:/bin", "HOME": str(c.data), "FAUCET_PASSWORD": c.password}
        try:
            done = self.run(args + extra, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired as exc:
            raise WalletError(f"{action} took longer than {timeout} s") from exc
        output = (done.stdout or "").strip()
        if done.returncode != 0:
            reason = (done.stderr or output or f"exit {done.returncode}").strip().splitlines()
            raise WalletError(reason[-1] if reason else f"{action} failed")
        reply = last_json_line(output)
        if reply is None:
            # The engine succeeded but its answer cannot be read. A send that
            # exits 0 was accepted by the node: it must not be tried again.
            if action == "send":
                return {}
            raise WalletError(f"{action}: unreadable reply")
        return reply

    def balance(self) -> dict:
        return self._call("balance", [], timeout=600)

    def send(self, to: str, amount_atoms: int) -> dict:
        amount = format_zka(amount_atoms).replace(",", "")
        return self._call("send", ["--to", to, "--amount", amount, "--memo", self.config.memo], timeout=900)


def last_json_line(output: str) -> Optional[dict]:
    """The engine's answer: its last line holding a JSON object. The node code
    it runs may print other JSON lines first, such as an accepted checkpoint."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


# A short address the node does not know is asked of its peers, and the
# node answers "not published" only after about 20 s. A shorter wait turned
# a wallet created a moment ago into "cannot reach the network" (12/11/2026).
ADDRESS_CHECK_SECONDS = 30


def short_is_published(api: str, short: str, opener: Callable = urllib.request.urlopen) -> bool:
    """The node knows the full address behind this short one."""
    try:
        with opener(f"{api}/address/{urllib.parse.quote(short)}", timeout=ADDRESS_CHECK_SECONDS) as reply:
            return reply.status == 200
    except urllib.error.HTTPError:
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WalletError(f"node API: {exc}") from exc


def verify_turnstile(config: Config, token: str, ip: str, opener: Callable = urllib.request.urlopen) -> tuple[bool, str]:
    body = urllib.parse.urlencode({"secret": config.turnstile_secret, "response": token, "remoteip": ip}).encode()
    request = urllib.request.Request(TURNSTILE_VERIFY, data=body, method="POST")
    try:
        with opener(request, timeout=10) as reply:
            result = json.loads(reply.read(65536) or b"{}")
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return False, f"turnstile unreachable: {exc}"
    if not result.get("success"):
        return False, "turnstile: " + ",".join(result.get("error-codes") or ["refused"])
    if result.get("action") != "claim":
        return False, "turnstile: wrong action"
    if not config.hostname_allowed(str(result.get("hostname", ""))):
        return False, "turnstile: wrong hostname"
    return True, ""


# ── The worker: pays the queue, one at a time ──────────────────────────────


@dataclass
class State:
    """What the worker last learned of the wallet, for /faucet/status."""

    spendable_atoms: Optional[int] = None
    reserved_atoms: int = 0
    max_sendable_atoms: Optional[int] = None
    pending: bool = False
    in_sync: bool = False
    empty: bool = False
    checked: float = 0.0
    error: Optional[str] = None


TRANSIENT = ("not in sync", "node API", "took longer", "unreachable", "pending", "busy", "lock")


class Worker:
    def __init__(self, config: Config, store: Store, wallet: Wallet, state: State, sleep: Callable[[float], None] = time.sleep):
        self.config, self.store, self.wallet, self.state, self.sleep = config, store, wallet, state, sleep
        self.stop = threading.Event()

    def refresh(self) -> dict:
        figures = self.wallet.balance()
        s = self.state
        s.spendable_atoms = int(figures.get("spendable_atoms") or 0)
        s.reserved_atoms = int(figures.get("reserved_atoms") or 0)
        s.max_sendable_atoms = int(figures.get("max_sendable_atoms") or 0)
        s.pending = bool(figures.get("pending_payment"))
        s.in_sync = not figures.get("node_behind")
        s.empty = not s.pending and s.max_sendable_atoms < self.config.amount_atoms
        s.checked = time.time()
        s.error = None
        return figures

    def step(self) -> float:
        """One pass. Returns how long to wait before the next one."""
        claim = self.store.next_queued()
        try:
            self.refresh()
        except WalletError as exc:
            self.state.error = str(exc)
            log(f"balance: {exc}")
            return 30
        if claim is None:
            return 60 if self.state.in_sync else 5
        if not self.state.in_sync or self.state.pending:
            # The last payment has not been mined yet, or the mirror is
            # still reading the chain: the claim waits its turn.
            return 15
        if self.state.empty:
            return 60
        claim_id, address, amount, attempts = claim
        self.store.set(claim_id, "sending", attempt=True)
        try:
            reply = self.wallet.send(address, amount)
        except WalletError as exc:
            reason = str(exc)
            log(f"send {claim_id[:8]}: {reason}")
            if any(word in reason for word in TRANSIENT) and attempts < 5:
                self.store.set(claim_id, "queued", error=None)
                return 30
            self.store.set(claim_id, "failed", error=public_reason(reason))
            return 5
        txid = str(reply.get("txid") or "")
        if not txid:
            # Sent, but its id was not readable: the wallet's pending payment has it.
            try:
                pending = self.refresh().get("pending_payment")
                if isinstance(pending, dict):
                    txid = str(pending.get("txid") or "")
            except WalletError:
                pass
        self.store.set(claim_id, "sent", txid=txid or None, error=None)
        log(f"sent {format_zka(amount)} ZKA, claim {claim_id[:8]}, tx {txid[:16]}")
        return 5

    def run(self) -> None:
        last_purge = 0.0
        while not self.stop.is_set():
            if time.time() - last_purge > 3600:
                removed = self.store.purge(self.config.retention_days, time.time())
                if removed:
                    log(f"purged {removed} old claims")
                last_purge = time.time()
            try:
                wait = self.step()
            except Exception as exc:  # the worker never dies
                log(f"worker: {exc!r}")
                wait = 30
            self.stop.wait(wait)


def public_reason(reason: str) -> str:
    if "resolve" in reason or "--to" in reason or "address" in reason:
        return "The faucet could not pay that address. Check that it is a Zoka Quantic Testnet short address."
    return "The faucet could not send this payment. Try again tomorrow."


# ── HTTP ────────────────────────────────────────────────────────────────────


class Limiter:
    """A few POSTs per minute per client, before Turnstile is even asked."""

    def __init__(self, per_minute: int = 6):
        self.per_minute = per_minute
        self.hits: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str, now: float) -> bool:
        with self.lock:
            recent = [t for t in self.hits.get(key, []) if now - t < 60]
            if len(recent) >= self.per_minute:
                self.hits[key] = recent
                return False
            recent.append(now)
            self.hits[key] = recent
            if len(self.hits) > 10_000:
                self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < 60}
            return True


@dataclass
class App:
    config: Config
    store: Store
    state: State
    limiter: Limiter = field(default_factory=Limiter)
    turnstile: Callable[[Config, str, str], tuple[bool, str]] = verify_turnstile
    published: Callable[[str, str], bool] = short_is_published


def wait_text(seconds: int) -> str:
    hours = max(1, round(seconds / 3600))
    return f"{hours} hour{'s' if hours != 1 else ''}"


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "zoka-faucet"
        sys_version = ""

        def log_message(self, fmt, *args):  # no access log: no IPs in the journal
            return

        def client_ip(self) -> str:
            return self.headers.get("CF-Connecting-IP") or self.client_address[0]

        def cors(self) -> dict:
            origin = self.headers.get("Origin", "")
            if app.config.origin_allowed(origin):
                return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
            return {"Vary": "Origin"}

        def reply(self, status: int, body: dict, extra: Optional[dict] = None) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            headers = {
                "Content-Type": "application/json",
                "Content-Length": str(len(data)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                **self.cors(),
                **(extra or {}),
            }
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            if not self.path.startswith("/faucet/"):
                return self.reply(404, {"error": "not found"})
            self.send_response(204)
            for key, value in {
                **self.cors(),
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Max-Age": "600",
            }.items():
                self.send_header(key, value)
            self.end_headers()

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/faucet/status":
                return self.reply(200, status_body(app))
            if path.startswith("/faucet/claims/"):
                claim = path.rsplit("/", 1)[-1]
                found = app.store.get(claim) if CLAIM_ID.match(claim) else None
                if not found:
                    return self.reply(404, {"error": "There is no such request."})
                return self.reply(200, claim_body(found))
            return self.reply(404, {"error": "not found"})

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            if path != "/faucet/claim":
                return self.reply(404, {"error": "not found"})
            ip = self.client_ip()
            now = time.time()
            if not app.limiter.allow(ip_key(ip), now):
                return self.reply(429, {"error": "Too many tries. Wait a minute."}, {"Retry-After": "60"})
            if "application/json" not in self.headers.get("Content-Type", ""):
                return self.reply(415, {"error": "Send JSON."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_BODY:
                return self.reply(413, {"error": "The request is too big."})
            try:
                body = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self.reply(400, {"error": "Send JSON."})
            address = str(body.get("address", "")).strip() if isinstance(body, dict) else ""
            token = str(body.get("token", "")) if isinstance(body, dict) else ""
            if not SHORT_ADDRESS.match(address):
                return self.reply(400, {"error": "Paste a Zoka Quantic Testnet short address: it starts with zqtest2.", "field": "address"})
            if not token or len(token) > MAX_TOKEN:
                return self.reply(400, {"error": "Complete the check first.", "field": "token"})
            # The address first: a mistyped one should not spend the check.
            try:
                if not app.published(app.config.api, address):
                    return self.reply(400, {"error": "That short address is not published yet. Open the wallet, wait until its short address appears and try again.", "field": "address"})
            except WalletError as exc:
                log(f"address check: {exc}")
                return self.reply(503, {"error": "The faucet cannot reach the network right now. Try again in a few minutes."})
            ok, why = app.turnstile(app.config, token, ip)
            if not ok:
                log(f"claim refused: {why}")
                return self.reply(403, {"error": "The check did not pass. Try it again.", "field": "token"})
            claim, reason = app.store.admit(address, ip, app.config.amount_atoms, app.config.daily_cap_atoms, now)
            if claim is None:
                kind, _, rest = reason.partition(":")
                if kind == "wallet":
                    return self.reply(429, {"error": f"This wallet already got its ZKA today. Come back in {wait_text(int(rest))}.", "field": "address"})
                if kind == "ip":
                    return self.reply(429, {"error": f"A request from your connection was already sent today. Come back in {wait_text(int(rest))}."})
                return self.reply(429, {"error": "The faucet gave all its ZKA for today. Come back tomorrow."})
            return self.reply(202, claim_body(app.store.get(claim)))

    return Handler


def status_body(app: App) -> dict:
    now = time.time()
    figures = app.store.figures(now)
    s = app.state
    left = max(0, app.config.daily_cap_atoms - figures["given_24h_atoms"])
    return {
        "network": "Zoka Quantic Testnet v3",
        "amount": format_zka(app.config.amount_atoms),
        "amount_atoms": app.config.amount_atoms,
        "daily_cap": format_zka(app.config.daily_cap_atoms),
        "left_24h": format_zka(left),
        "left_24h_atoms": left,
        "queue": figures["queue"],
        "sent_total": figures["sent_total"],
        # What the wallet holds, also what a payment waiting for its block keeps reserved.
        "balance": None if s.spendable_atoms is None else format_zka(s.spendable_atoms + s.reserved_atoms),
        "ready": bool(s.checked) and s.in_sync and not s.empty,
        "empty": s.empty,
    }


def claim_body(found: dict) -> dict:
    return {
        "id": found["id"],
        "status": found["status"],
        "position": found["position"],
        "amount": format_zka(found["amount_atoms"]),
        "txid": found["txid"] or None,
        "error": found["error"] or None,
    }


# ── Entry point ─────────────────────────────────────────────────────────────


def load_salt(data: Path) -> bytes:
    path = data / "faucet-ip.salt"
    if not path.exists():
        old = os.umask(0o077)
        try:
            path.write_bytes(secrets.token_bytes(32))
        finally:
            os.umask(old)
    return path.read_bytes()


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Zoka faucet (Zoka Quantic Testnet v3)")
    p.add_argument("--listen", default="127.0.0.1:9300")
    p.add_argument("--data", default="/var/lib/zoka-faucet")
    p.add_argument("--zoka", default="/usr/local/bin/zoka")
    p.add_argument("--api", default="http://127.0.0.1:9190")
    p.add_argument("--peer", default=None, help="descriptor of the node the wallet mirror follows")
    p.add_argument("--amount-zka", type=int, default=10)
    p.add_argument("--daily-cap-zka", type=int, default=400)
    args = p.parse_args(argv)

    host, _, port = args.listen.rpartition(":")
    if not ipaddress.ip_address(host).is_loopback:
        print("--listen must be a loopback address: the tunnel brings the requests", file=sys.stderr)
        return 2
    config = Config(
        data=Path(args.data),
        zoka=args.zoka,
        api=args.api,
        peer=Path(args.peer) if args.peer else None,
        amount_atoms=args.amount_zka * ATOMS,
        daily_cap_atoms=args.daily_cap_zka * ATOMS,
        password=read_secret("password", "FAUCET_PASSWORD"),
        turnstile_secret=read_secret("turnstile", "FAUCET_TURNSTILE_SECRET"),
    )
    if not config.password or not config.turnstile_secret:
        print("the wallet password and the Turnstile secret are needed (systemd credentials)", file=sys.stderr)
        return 2
    store = Store(config.data / "faucet.sqlite", load_salt(config.data))
    state = State()
    worker = Worker(config, store, Wallet(config), state)
    threading.Thread(target=worker.run, name="faucet-worker", daemon=True).start()
    server = ThreadingHTTPServer((host, int(port)), make_handler(App(config, store, state)))
    server.daemon_threads = True
    log(f"faucet on {args.listen}: {format_zka(config.amount_atoms)} ZKA per claim, {format_zka(config.daily_cap_atoms)} ZKA per 24 h")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
