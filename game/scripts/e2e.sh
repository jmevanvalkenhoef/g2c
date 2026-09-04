#!/usr/bin/env bash
# g2c demo — end-to-end contract regression drill.
# Boots game-backend + escrow-stub, runs happy path, failure path,
# idempotency replays, and verifies game-DB end state after each.
# Exit code != 0 on any failure.
#
# Usage: game/scripts/e2e.sh          (servers must NOT already run on 8000/8001)
set -euo pipefail
cd "$(dirname "$0")/.."

GAME=8000; STUB=8001
ALICE=1; BOB=2   # deterministic seed ids (alice=1, bob=2)
PASS=0; FAIL=0

check() {  # check <label> <expected> <actual>
  if [ "$2" = "$3" ]; then PASS=$((PASS+1)); echo "  ok: $1";
  else FAIL=$((FAIL+1)); echo "  FAIL: $1 (expected '$2', got '$3')"; fi
}
jget() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }

echo "== booting =="
pkill -f "[u]vicorn app:app" 2>/dev/null || true; sleep 1
uv run --project game-backend uvicorn app:app --port $GAME  --app-dir game-backend > /tmp/g2c_game.log 2>&1 &
uv run --project escrow-stub  uvicorn app:app --port $STUB --app-dir escrow-stub  > /tmp/g2c_stub.log 2>&1 &
trap 'pkill -f "[u]vicorn app:app" 2>/dev/null || true' EXIT
sleep 4

g()  { curl -s "localhost:$GAME$1" -H "X-Player-Id: $2" "${@:3}"; }
gs() { curl -s "localhost:$GAME$1" -H "X-Escrow-Secret: dev-secret" "${@:3}"; }
owner() { curl -s localhost:$GAME/v1/items | python3 -c "import json,sys; print([x for x in json.load(sys.stdin) if x['id']==int(sys.argv[1])][0]['owner_name'])" $1; }
field() { curl -s localhost:$GAME/v1/items | python3 -c "import json,sys; print([x for x in json.load(sys.stdin) if x['id']==int(sys.argv[1])][0][sys.argv[2]])" $1 $2; }

# ---------------------------------------------------------------- happy path
echo "== happy path =="
g /v1/listings $ALICE -X POST -H 'Content-Type: application/json' \
  -d '{"item_id":1,"price_msat":500000}' > /dev/null
S=$(g /v1/purchases $BOB -X POST -H 'Content-Type: application/json' -d '{"listing_id":1}')
PH=$(echo "$S" | jget "['payment_hash']"); SID=$(echo "$S" | jget "['sale_id']")
check "fee decided by escrow" "100" "$(echo "$S" | jget "['fee_msat']")"
check "seller net amount" "499900" "$(echo "$S" | jget "['seller_amount_msat']")"
sleep 1
check "no transfer before QR tap" "alice" "$(owner 1)"
g /v1/purchases/$PH/simulate-pay $BOB -X POST > /dev/null
sleep 4
check "purchase completed" "completed" "$(g /v1/purchases/$PH $BOB | jget "['status']")"
check "item transferred" "bob" "$(owner 1)"
check "execute_sale replay idempotent" "already_done" \
  "$(gs /execute_sale -X POST -H 'Content-Type: application/json' \
     -d "{\"item_id\":1,\"buyer_id\":$BOB,\"seller_id\":$ALICE,\"idempotency_key\":\"$PH\"}" | jget "['status']")"
check "sale_status done" "done" "$(gs "/sale_status?key=$PH" | jget "['status']")"

# ---------------------------------------------------------------- failure path
echo "== failure path =="
g /v1/listings $ALICE -X POST -H 'Content-Type: application/json' \
  -d '{"item_id":3,"price_msat":700000}' > /dev/null
S2=$(g /v1/purchases $BOB -X POST -H 'Content-Type: application/json' -d '{"listing_id":2}')
PH2=$(echo "$S2" | jget "['payment_hash']"); SID2=$(echo "$S2" | jget "['sale_id']")
curl -s -X POST -H 'X-Escrow-Secret: dev-secret' localhost:$STUB/v1/sales/$SID2/fail > /dev/null
sleep 1
g /v1/purchases/$PH2/simulate-pay $BOB -X POST > /dev/null
sleep 4
check "purchase cancelled" "cancelled" "$(g /v1/purchases/$PH2 $BOB | jget "['status']")"
check "item returned to seller" "alice" "$(owner 3)"
check "listing reopened" "open" "$(field 3 listing_status)"
check "rollback replay idempotent" "already_rolled_back" \
  "$(gs /rollback_sale -X POST -H 'Content-Type: application/json' \
     -d "{\"idempotency_key\":\"$PH2\",\"reason\":\"replay\"}" | jget "['status']")"

# ---------------------------------------------------------------- double-pay guard
echo "== double-pay guard =="
check "second simulate-pay is a no-op" "completed" \
  "$(g /v1/purchases/$PH/simulate-pay $BOB -X POST | jget "['status']")"

echo
echo "== result: $PASS passed, $FAIL failed =="
[ "$FAIL" = "0" ]
