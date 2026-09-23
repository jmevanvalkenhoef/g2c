#!/usr/bin/env bash
# Fault drill: the REAL escrow against the fault-injecting mock game.
# Proves spec §6 rows 3-4 against real HTLCs:
#   reject   -> execute_sale 409 -> cancel -> buyer auto-refunded
#   timeout  -> ambiguous -> keep retrying (hold the HTLC, settle nothing)
#   recovery -> game comes back -> sale completes
#
# The escrow container is recreated ONCE (to rewire GAME_URL to mock-game)
# and left running across fault-mode changes — recreating it would wipe its
# SQLite and orphan held HTLCs (learned the hard way). Only mock-game is
# recreated between modes.
set -euo pipefail
cd "$(dirname "$0")/.."
PASS=0; FAIL=0
check() { if [ "$2" = "$3" ]; then PASS=$((PASS+1)); echo "  ok: $1"; else FAIL=$((FAIL+1)); echo "  FAIL: $1 (expected '$2', got '$3')"; fi; }

ESC=http://localhost:8002
# LAB_FLAVOR=lnd adds the LND flavor override (escrow stays LND-backed)
FLAVOR="${LAB_FLAVOR:+-f docker-compose.$LAB_FLAVOR.yml}"
FAULTS="-f docker-compose.yml $FLAVOR -f docker-compose.faults.yml"

rewire_escrow() {  # point escrow at mock-game (once)
  docker compose $FAULTS up -d --force-recreate escrow > /dev/null 2>&1
  for i in $(seq 40); do curl -sf $ESC/v1/health 2>/dev/null | grep -q '"ok":true' && return 0; sleep 2; done
  echo "escrow failed to boot"; exit 1
}
set_fault() {  # recreate ONLY mock-game with the given FAULT_MODE
  FAULT_MODE=$1 docker compose $FAULTS up -d --force-recreate mock-game > /dev/null 2>&1
  sleep 2
}
restore() { docker compose $FLAVOR up -d --force-recreate escrow > /dev/null 2>&1; docker compose $FLAVOR up -d mock-game > /dev/null 2>&1; }
trap restore EXIT

new_sale() {
  curl -s $ESC/v1/sales -X POST -H 'Content-Type: application/json' \
    -d '{"item_id":1,"buyer_id":2,"seller_id":1,"amount_msat":20000000,
         "seller_payout":{"type":"stub","target":"x"}}' | python3 -c '
import json,sys; d=json.load(sys.stdin); print(d["sale_id"], d["payment_hash"])'
}
state_of() { curl -s $ESC/v1/sales/$1 | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])'; }
pay_status() {
  docker compose exec -T cln-buyer lightning-cli --regtest listpays 2>/dev/null | python3 -c "
import json,sys
pays=[p for p in json.load(sys.stdin)['pays'] if p['payment_hash'].startswith('$1'[:12])]
print(pays[-1]['status'] if pays else 'none')"
}
tap() {  # tap <sale_id> with flake retry: a transient pay failure shouldn't sink the drill
  curl -s $ESC/v1/sales/$1/pay -X POST > /dev/null
  for r in 1 2; do
    sleep 6
    [ "$(state_of $1)" != "AWAITING_PAYMENT" ] && return 0
    curl -s $ESC/v1/sales/$1/pay -X POST > /dev/null
  done
}

echo "== rewire escrow -> mock-game, FAULT_MODE=reject =="
rewire_escrow
set_fault reject
read SID PH <<< $(new_sale)
tap $SID
for i in $(seq 60); do S=$(state_of $SID); [ "$S" = "CANCELLED" -o "$S" = "COMPLETED" ] && break; sleep 1; done
check "reject -> CANCELLED" "CANCELLED" "$S"
for i in $(seq 30); do [ "$(pay_status $PH)" = "failed" ] && break; sleep 1; done
check "reject -> buyer refunded" "failed" "$(pay_status $PH)"

echo "== FAULT_MODE=timeout (ambiguous execute_sale) =="
set_fault timeout
read SID2 PH2 <<< $(new_sale)
tap $SID2
echo "  waiting 40s of ambiguity…"
sleep 40
check "timeout -> still retrying (not settled, not cancelled)" "EXECUTING" "$(state_of $SID2)"
check "timeout -> buyer still pending (HTLC held)" "pending" "$(pay_status $PH2)"

echo "== game recovers (FAULT_MODE=off) — held sale must complete =="
set_fault off
for i in $(seq 90); do S3=$(state_of $SID2); [ "$S3" = "COMPLETED" -o "$S3" = "CANCELLED" ] && break; sleep 1; done
check "recovery -> COMPLETED" "COMPLETED" "$S3"
sleep 3
check "recovery -> buyer paid" "complete" "$(pay_status $PH2)"

echo
echo "== fault drill: $PASS passed, $FAIL failed =="
[ "$FAIL" = "0" ]
