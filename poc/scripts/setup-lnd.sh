#!/usr/bin/env bash
# LND-flavor lab setup: fund the LND escrow node, open both channels,
# wait for confirmations + gossip. Run after:
#   docker compose -f docker-compose.yml -f docker-compose.lnd.yml up -d --build
set -euo pipefail
cd "$(dirname "$0")/.."

LNDF="-f docker-compose.yml -f docker-compose.lnd.yml"
BTC() { docker compose exec -T bitcoind bitcoin-cli -regtest -rpcuser=rpcuser -rpcpassword=rpcpass -rpcwallet=miner "$@"; }
cli() { local svc=$1; shift; docker compose exec -T "$svc" lightning-cli --regtest "$@"; }
ladmin() { docker compose $LNDF run --rm --no-deps escrow uv run python lnd_admin.py "$@"; }

echo "==> LND wallet init/unlock"
LND_ID=$(ladmin init | tail -1 | awk '{print $2}')
echo "    lnd-escrow: $LND_ID"

echo "==> funding LND wallet"
ADDR=$(ladmin newaddr)
TX=$(BTC sendtoaddress "$ADDR" 1.0 "" "" false true)
BTC generatetoaddress 2 "$(BTC getnewaddress)" >/dev/null
sleep 3
echo "    funded: $TX"

SELLER_ID=$(cli cln-seller getinfo | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
echo "==> peering"
cli cln-buyer connect "$LND_ID@lnd-escrow" >/dev/null 2>&1 || true
ladmin connect "$SELLER_ID@cln-seller" >/dev/null
sleep 2

echo "==> opening channels"
# buyer(CLN) -> lnd-escrow: gives the escrow INBOUND (receives buyer payments)
cli cln-buyer fundchannel "$LND_ID" 500000sat >/dev/null
# lnd-escrow -> seller(CLN): the escrow's OUTBOUND (pays sellers)
ladmin openchannel "$SELLER_ID" 500000 >/dev/null
echo "    channels opening (miner confirms every 15s)"

echo "==> mining confirmations + waiting for channels"
BTC generatetoaddress 8 "$(BTC getnewaddress)" >/dev/null
for i in $(seq 60); do
  B=$(cli cln-buyer listpeerchannels | python3 -c 'import json,sys; print(any(c["state"]=="CHANNELD_NORMAL" for c in json.load(sys.stdin)["channels"]))' 2>/dev/null || echo False)
  L=$(ladmin channels 2>/dev/null | python3 -c 'import sys,json; print(any(json.loads(l)["active"] for l in sys.stdin))' 2>/dev/null || echo False)
  [ "$B" = "True" ] && [ "$L" = "True" ] && { echo "    both channels active"; break; }
  sleep 5
done

echo "==> gossip wait (announced channels)"
for i in $(seq 40); do
  N=$(cli cln-buyer listnodes 2>/dev/null | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["nodes"]))' 2>/dev/null || echo 0)
  [ "${N:-0}" -ge 3 ] && { echo "    buyer knows $N nodes"; break; }
  sleep 3
done

echo
echo "=== LND lab topology ready ==="
echo "escrow node: $LND_ID"
echo "next: game/scripts/e2e.sh --real ; python3 scripts/kill_drill.py ; scripts/fault-drill.sh"
