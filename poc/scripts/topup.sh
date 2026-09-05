#!/usr/bin/env bash
# Top up lab channel capacity after heavy drill usage.
# Opens fresh channels on the EXISTING world (no data loss), mines past the
# announcement depth, and waits for gossip — the same ritual setup.sh does,
# minus the world creation.
#
# Note: the escrow's in/out channels drain one-way (buyer -> escrow -> seller),
# so capacity is consumed by total volume, not concurrency. Run this when
# sale payouts start failing; ~CHAN_SATS of fresh capacity each.
set -euo pipefail
cd "$(dirname "$0")/.."

CHAN_SATS="${1:-400000}"

BTC() { docker compose exec -T bitcoind bitcoin-cli -regtest -rpcuser=rpcuser -rpcpassword=rpcpass -rpcwallet=miner "$@"; }
cli() { local svc=$1; shift; docker compose exec -T "$svc" lightning-cli --regtest "$@"; }

echo "==> current escrow channel balances"
cli cln-escrow listfunds | python3 -c '
import json,sys
for c in json.load(sys.stdin)["channels"]:
    print("   ", c["peer_id"][:10], "our:", c["our_amount_msat"], "msat")'

ESCROW_ID=$(cli cln-escrow getinfo | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
SELLER_ID=$(cli cln-seller getinfo | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')

echo "==> opening top-up channels (${CHAN_SATS}sat each)"
cli cln-buyer  connect "$ESCROW_ID@cln-escrow" >/dev/null 2>&1 || true
cli cln-escrow connect "$SELLER_ID@cln-seller" >/dev/null 2>&1 || true
cli cln-buyer  fundchannel "$ESCROW_ID" "${CHAN_SATS}sat" | python3 -c 'import json,sys; print("    buyer->escrow tx:", json.load(sys.stdin).get("txid"))'
cli cln-escrow fundchannel "$SELLER_ID" "${CHAN_SATS}sat" | python3 -c 'import json,sys; print("    escrow->seller tx:", json.load(sys.stdin).get("txid"))'

echo "==> mining past announcement depth + waiting for gossip"
BTC generatetoaddress 8 "$(BTC getnewaddress)" >/dev/null
for i in $(seq 90); do
  cli cln-buyer listpeerchannels | python3 -c '
import json,sys
chs=json.load(sys.stdin)["channels"]
print(sum(1 for c in chs if c["state"]=="CHANNELD_NORMAL"))' > /tmp/nch 2>/dev/null || echo 0 > /tmp/nch
  N=$(cat /tmp/nch)
  [ "${N:-0}" -ge 2 ] && { echo "    buyer: $N usable channels"; break; }
  sleep 3
done

echo "==> done. escrow balances now:"
cli cln-escrow listfunds | python3 -c '
import json,sys
for c in json.load(sys.stdin)["channels"]:
    print("   ", c["peer_id"][:10], "our:", c["our_amount_msat"], "msat")'
