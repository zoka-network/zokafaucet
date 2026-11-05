#!/usr/bin/env bash
# Installs the faucet on VPS 1 (seed2), as root, from /root/zoka-faucet-install.
# Expects the wallet and the secrets made before:
#   /var/lib/zoka-faucet/wallets/faucet.json, /root/faucet.password,
#   /root/faucet-turnstile.secret
# Changes nothing of seed2. The Cloudflare tunnel gets one rule more, so
# api.zokascan.cc/faucet/... goes to the faucet; its old config is kept.
set -euo pipefail
SRC=/root/zoka-faucet-install
DATA=/var/lib/zoka-faucet
TUNNEL=/etc/cloudflared/config.yml

for f in "$DATA/wallets/faucet.json" /root/faucet.password /root/faucet-turnstile.secret "$SRC/faucet.py" "$SRC/zoka-faucet.service"; do
    [ -e "$f" ] || { echo "Falta $f"; exit 1; }
done

# 1. A user of its own, no login.
id zoka-faucet >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin zoka-faucet

# 2. The program.
install -d -m 0755 /opt/zoka-faucet
install -m 0644 "$SRC/faucet.py" /opt/zoka-faucet/faucet.py

# 3. The descriptor of seed2, which the wallet mirror follows (public data:
#    the node's endpoint and its ML-DSA key id), from its last announce line.
journalctl -u zoka-seed -o cat --no-pager | grep '^{"announce"' | tail -1 \
  | python3 -c 'import json,sys; d=json.load(sys.stdin)["descriptor"]; assert d["chain_id"]=="zoka-quantic-testnet-v2"; json.dump(d, open("/var/lib/zoka-faucet/seed2.descriptor.json","w"), indent=2)'
chown -R zoka-faucet:zoka-faucet "$DATA"
chmod 0700 "$DATA"

# 4. The service.
install -m 0644 "$SRC/zoka-faucet.service" /etc/systemd/system/zoka-faucet.service
systemctl daemon-reload
systemctl enable --now zoka-faucet
sleep 5
systemctl is-active zoka-faucet
curl -s --max-time 10 http://127.0.0.1:9300/faucet/status; echo

# 5. The tunnel: /faucet/ of api.zokascan.cc to the faucet, the rest as before.
if ! grep -q 'path: \^/faucet/' "$TUNNEL"; then
    cp -a "$TUNNEL" "$TUNNEL.antes-faucet-$(date +%Y%m%d-%H%M%S)"
    python3 - "$TUNNEL" <<'PY'
import sys
path = sys.argv[1]
text = open(path).read()
rule = "  - hostname: api.zokascan.cc\n"
block = ("  - hostname: api.zokascan.cc\n"
         "    path: ^/faucet/\n"
         "    service: http://127.0.0.1:9300\n"
         "    originRequest:\n"
         "      connectTimeout: 10s\n")
assert text.count(rule) == 1, "unexpected tunnel config"
open(path, "w").write(text.replace(rule, block + rule, 1))
PY
fi
cloudflared tunnel --config "$TUNNEL" ingress validate
cloudflared tunnel --config "$TUNNEL" ingress rule https://api.zokascan.cc/faucet/status
cloudflared tunnel --config "$TUNNEL" ingress rule https://api.zokascan.cc/status
systemctl restart zokascan-tunnel
sleep 8
echo "== desde afuera"
curl -s --max-time 15 https://api.zokascan.cc/faucet/status; echo
curl -s --max-time 15 -o /dev/null -w "api.zokascan.cc/status: %{http_code}\n" https://api.zokascan.cc/status
echo "Faucet instalado."
