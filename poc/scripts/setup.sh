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

echo "==> waiting for miner coinbase maturity (>=101 blocks)"
for i in $(seq 120); do
  HEIGHT=$(docker compose exec -T bitcoind bitcoin-cli -regtest -rpcuser=rpcuser \
      -rpcpassword=rpcpass -rpcwallet=miner getblockcount 2>/dev/null)
  [ "${HEIGHT:-0}" -ge 101 ] && { echo "    chain at $HEIGHT blocks"; break; }
  sleep 2
done

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
open_channel() {  # open_channel <from-node> <peer-id> <label>
  for i in $(seq 12); do
    OUT=$(cli "$1" fundchannel "$2" "${CHAN_SATS}sat" 2>&1) || true
    if echo "$OUT" | grep -q '"txid"'; then
      echo "    $3 tx: $(echo "$OUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["txid"])')"
      return 0
    fi
    echo "    $3 retry ($i): $(echo "$OUT" | head -c 120)"
    sleep 10
  done
  echo "    $3 FAILED"; return 1
}
# buyer -> escrow: gives escrow INBOUND capacity (can receive buyer payments)
open_channel cln-buyer "$ESCROW_ID" "buyer->escrow"
# escrow -> seller: gives escrow OUTBOUND capacity (can pay sellers)
open_channel cln-escrow "$SELLER_ID" "escrow->seller"

echo "==> waiting for channels to reach CHANNELD_NORMAL"
for i in $(seq 40); do
  B=$(cli cln-buyer  listpeerchannels | python3 -c 'import json,sys; print(any(c["state"]=="CHANNELD_NORMAL" for c in json.load(sys.stdin)["channels"]))')
  E=$(cli cln-escrow listpeerchannels | python3 -c 'import json,sys; print(any(c["state"]=="CHANNELD_NORMAL" for c in json.load(sys.stdin)["channels"]))')
  [ "$B" = "True" ] && [ "$E" = "True" ] && break
  sleep 5
done

# announcements need ~6 confirmations + gossip propagation before the buyer's
# router can use the fresh channel; mine past the depth and wait for gossip
echo "==> waiting for channel announcements / gossip"
docker compose exec -T bitcoind bitcoin-cli -regtest -rpcuser=rpcuser \
  -rpcpassword=rpcpass -rpcwallet=miner generatetoaddress 8 \
  $(docker compose exec -T bitcoind bitcoin-cli -regtest -rpcuser=rpcuser \
    -rpcpassword=rpcpass -rpcwallet=miner getnewaddress) >/dev/null
for i in $(seq 60); do
  N=$(docker compose exec -T cln-buyer lightning-cli --regtest listnodes 2>/dev/null | \
      python3 -c 'import json,sys; print(len(json.load(sys.stdin)["nodes"]))' 2>/dev/null)
  [ "${N:-0}" -ge 3 ] && { echo "    buyer knows $N nodes"; break; }
  sleep 3
done

echo
echo "=== topology ready ==="
cli cln-buyer  listfunds | python3 -m json.tool
cli cln-escrow listfunds | python3 -m json.tool
echo "escrow node id: $ESCROW_ID"
echo "next: ./scripts/hold-demo.sh settle|cancel [amount_sats]"
