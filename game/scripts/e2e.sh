#!/usr/bin/env bash
# g2c demo — end-to-end contract regression drill.
#
#   e2e.sh          against the escrow STUB (boots both services itself)
#   e2e.sh --real   against the REAL escrow (requires the poc/ lab up with
#                   channels + gossip; boots only the game, ESCROW_URL=:8002)
#
# Happy path, failure path, idempotency replays, double-pay guard.
# Exit code != 0 on any failure.
set -euo pipefail
cd "$(dirname "$0")/.."

REAL="${1:-}"
GAME=8000
ALICE=1; BOB=2
PASS=0; FAIL=0
STUB_PID=""

if [ "$REAL" = "--real" ]; then
  ESCROW=8002; TIMEOUT=120
else
  ESCROW=8001; TIMEOUT=30
fi

cleanup() { [ -n "$STUB_PID" ] && kill "$STUB_PID" 2>/dev/null || true
            pkill -f "uvicorn app:app --host 0.0.0.0 --port $GAME" 2>/dev/null || true; }
trap cleanup EXIT

check() {  # check <label> <expected> <actual>
  if [ "$2" = "$3" ]; then PASS=$((PASS+1)); echo "  ok: $1";
  else FAIL=$((FAIL+1)); echo "  FAIL: $1 (expected '$2', got '$3')"; fi
}
jget() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }
field() { curl -s localhost:$GAME/v1/items | python3 -c "import json,sys; print([x for x in json.load(sys.stdin) if x['id']==int(sys.argv[1])][0][sys.argv[2]])" $1 $2; }

wait_purchase() {  # wait_purchase <payment_hash> <state>  -> prints final state
  local ph=$1 want=$2 got=""
  for i in $(seq $TIMEOUT); do
    got=$(curl -s localhost:$GAME/v1/purchases/$ph -H "X-Player-Id: $BOB" | jget "['status']")
    [ "$got" = "$want" ] && break
    sleep 1
  done
  echo "$got"
}

echo "== booting ($([ "$REAL" = "--real" ] && echo REAL escrow || echo stub)) =="
pkill -f "[u]vicorn app:app" 2>/dev/null || true; sleep 1
if [ "$REAL" = "--real" ]; then
  for i in $(seq 30); do
    curl -sf localhost:$ESCROW/v1/health 2>/dev/null | grep -q '"ok":true' && break
    sleep 2
  done
  curl -sf localhost:$ESCROW/v1/health | grep -q '"ok":true' || { echo "real escrow not healthy on :$ESCROW — is the poc lab up?"; exit 1; }
else
  (cd escrow-stub && uv run uvicorn app:app --port $ESCROW > /tmp/g2c_stub.log 2>&1 &) ; STUB_PID=1
  sleep 3
fi
ESCROW_URL=http://localhost:$ESCROW uv run --project game-backend \
  uvicorn app:app --host 0.0.0.0 --port $GAME --app-dir game-backend > /tmp/g2c_game.log 2>&1 &
sleep 4
curl -sf localhost:$GAME/v1/items > /dev/null || { echo "game failed to boot"; tail /tmp/g2c_game.log; exit 1; }

g()  { curl -s "localhost:$GAME$1" -H "X-Player-Id: $2" "${@:3}"; }
gs() { curl -s "localhost:$GAME$1" -H "X-Escrow-Secret: dev-secret" "${@:3}"; }

# ---------------------------------------------------------------- happy path
echo "== happy path =="
g /v1/listings $ALICE -X POST -H 'Content-Type: application/json' \
  -d '{"item_id":1,"price_msat":50000000}' > /dev/null
S=$(g /v1/purchases $BOB -X POST -H 'Content-Type: application/json' -d '{"listing_id":1}')
PH=$(echo "$S" | jget "['payment_hash']"); SID=$(echo "$S" | jget "['sale_id']")
check "fee decided by escrow" "100" "$(echo "$S" | jget "['fee_msat']")"
check "seller net amount" "49999900" "$(echo "$S" | jget "['seller_amount_msat']")"
[ "$REAL" = "--real" ] && check "real BOLT11 invoice" "lnbcrt" "$(echo "$S" | jget "['payment_request'][:6]")"
sleep 1
check "no transfer before QR tap" "alice" "$(field 1 owner_name)"
g /v1/purchases/$PH/simulate-pay $BOB -X POST > /dev/null
check "purchase completed" "completed" "$(wait_purchase $PH completed)"
check "item transferred" "bob" "$(field 1 owner_name)"
check "execute_sale replay idempotent" "already_done" \
  "$(gs /execute_sale -X POST -H 'Content-Type: application/json' \
     -d "{\"item_id\":1,\"buyer_id\":$BOB,\"seller_id\":$ALICE,\"idempotency_key\":\"$PH\"}" | jget "['status']")"
check "sale_status done" "done" "$(gs "/sale_status?key=$PH" | jget "['status']")"

# ---------------------------------------------------------------- failure path
echo "== failure path =="
g /v1/listings $ALICE -X POST -H 'Content-Type: application/json' \
  -d '{"item_id":3,"price_msat":70000000}' > /dev/null
S2=$(g /v1/purchases $BOB -X POST -H 'Content-Type: application/json' -d '{"listing_id":2}')
PH2=$(echo "$S2" | jget "['payment_hash']"); SID2=$(echo "$S2" | jget "['sale_id']")
curl -s -X POST -H 'X-Escrow-Secret: dev-secret' localhost:$ESCROW/v1/sales/$SID2/fail > /dev/null
sleep 1
g /v1/purchases/$PH2/simulate-pay $BOB -X POST > /dev/null
check "purchase cancelled" "cancelled" "$(wait_purchase $PH2 cancelled)"
check "item returned to seller" "alice" "$(field 3 owner_name)"
check "listing reopened" "open" "$(field 3 listing_status)"
check "rollback replay idempotent" "already_rolled_back" \
  "$(gs /rollback_sale -X POST -H 'Content-Type: application/json' \
     -d "{\"idempotency_key\":\"$PH2\",\"reason\":\"replay\"}" | jget "['status']")"

# ---------------------------------------------------------------- double-pay guard
echo "== double-pay guard =="
check "second simulate-pay is a no-op" "completed" \
  "$(g /v1/purchases/$PH/simulate-pay $BOB -X POST | jget "['status']")"

# real mode: money actually moved correctly on-chain of Lightning
if [ "$REAL" = "--real" ]; then
  echo "== real-money checks =="
  BUYER_PAY=$(cd ../poc && docker compose exec -T cln-buyer lightning-cli --regtest listpays 2>/dev/null | \
    python3 -c "import json,sys; pays=[p for p in json.load(sys.stdin)['pays'] if p['payment_hash'].startswith('$PH'[:8])]; print(pays[-1]['status'] if pays else 'none')" 2>/dev/null || echo "?")
  check "buyer payment completed" "complete" "$BUYER_PAY"
fi

echo
echo "== result: $PASS passed, $FAIL failed =="
[ "$FAIL" = "0" ]
