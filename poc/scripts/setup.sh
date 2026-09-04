#!/usr/bin/env bash
# One-time setup: fund the three CLN nodes and open the two channels.
#   buyer  --(pays hold invoices)-->  escrow  --(pays seller)-->  seller
set -euo pipefail
cd "$(dirname "$0")/.."

BTC="docker compose exec -T bitcoind bitcoin-cli -regtest -rpcuser=rpcuser -rpcpassword=rpcpass -rpcwallet=miner"
cli() { local svc=$1; shift; docker compose exec -T "$svc" lightning-cli --regtest "$@"; }

FUND_SATS=100000000   # 1 BTC per node from the miner wallet
CHAN_SATS=500000      # 500k sat channels

echo "==> waiting for nodes to be ready"
for n in cln-escrow cln-buyer cln-seller; do
  for i in $(seq 60); do
    if cli "$n" getinfo >/dev/null 2>&1; then break; fi
    sleep 2
  done
done
cli cln-escrow getinfo >/dev/null

echo "==> funding node wallets"
for n in cln-escrow cln-buyer cln-seller; do
  ADDR=$(cli "$n" newaddr bech32 | python3 -c 'import json,sys; print(json.load(sys.stdin)["bech32"])')
  TX=$($BTC sendtoaddress "$ADDR" $(( FUND_SATS / 100000000 )) "" "" false true)
  echo "    $n <- $TX"
done
echo "==> mining a confirmation"
$BTC generatetoaddress 1 $($BTC getnewaddress) >/dev/null
sleep 5

echo "==> connecting nodes"
ESCROW_ID=$(cli cln-escrow getinfo | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["id"])')
SELLER_ID=$(cli cln-seller getinfo | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["id"])')
cli cln-buyer  connect "$ESCROW_ID@cln-escrow" >/dev/null
cli cln-escrow connect "$SELLER_ID@cln-seller" >/dev/null

echo "==> opening channels (miner confirms every 15s)"
# buyer -> escrow: gives escrow INBOUND capacity (can receive buyer payments)
cli cln-buyer  fundchannel "$ESCROW_ID" "${CHAN_SATS}sat" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("    buyer->escrow tx:", d.get("txid", d))'
# escrow -> seller: gives escrow OUTBOUND capacity (can pay sellers)
cli cln-escrow fundchannel "$SELLER_ID" "${CHAN_SATS}sat" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("    escrow->seller tx:", d.get("txid", d))' 

echo "==> waiting for channels to reach CHANNELD_NORMAL"
for i in $(seq 40); do
  B=$(cli cln-buyer  listpeerchannels | python3 -c 'import json,sys; print(any(c["state"]=="CHANNELD_NORMAL" for c in json.load(sys.stdin)["channels"]))')
  E=$(cli cln-escrow listpeerchannels | python3 -c 'import json,sys; print(any(c["state"]=="CHANNELD_NORMAL" for c in json.load(sys.stdin)["channels"]))')
  [ "$B" = "True" ] && [ "$E" = "True" ] && break
  sleep 5
done

echo
echo "=== topology ready ==="
cli cln-buyer  listfunds | python3 -m json.tool
cli cln-escrow listfunds | python3 -m json.tool
echo "escrow node id: $ESCROW_ID"
echo "next: ./scripts/hold-demo.sh settle|cancel [amount_sats]"
