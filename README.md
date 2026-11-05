# Zoka Faucet

10 test ZKA a day for wallets that do not mine, on Zoka Quantic Testnet v2.
Served at `faucet.zoka.network`.

- `web/`: the page (Vite + TypeScript, no framework), on Cloudflare Pages.
  It sends a short address and a Cloudflare Turnstile token to the faucet's
  API, and follows the request until its payment leaves.
- `server/`: the faucet (`faucet.py`, Python standard library only). It runs
  next to seed2 and is reached at `https://api.zokascan.cc/faucet/` through
  the same Cloudflare tunnel as the explorer's API.

## Rules

- 10 ZKA per request, one request every 24 hours per wallet and per client IP
  (IPv6 by /64), and at most 400 ZKA in any 24 hours.
- The address must be a published short address (`zqtest2…`).
- Turnstile is checked on the server with the secret key.
- Payments leave one at a time, with the wallet engine ZSilent uses
  (`zoka quantic-pq-wallet send`), from the faucet's own wallet: a Zoka wallet
  has one payment in flight at a time, so the next starts once the last is
  mined.
- Client IPs are stored only as salted SHA-256 hashes; requests are deleted
  after 30 days.

## Server

`server/instalar-vps1.sh` installs it as `zoka-faucet.service`, under its own
user and with a systemd sandbox. The wallet password and the Turnstile secret
reach it as systemd credentials. It adds one rule to the tunnel, so
`api.zokascan.cc/faucet/…` goes to `127.0.0.1:9300` and everything else to
the node's public API, as before.

```sh
cd server && python -m unittest -v test_faucet
```

## Page

```sh
cd web
npm install
npm test
npm run build   # dist/ and dist/SHA256SUMS.txt
```
