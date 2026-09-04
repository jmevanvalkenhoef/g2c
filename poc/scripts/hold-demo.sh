#!/usr/bin/env bash
# Manual drill: buyer pays a hold invoice at the escrow node,
# escrow settles (success) or cancels (failure path).
# Usage: ./scripts/hold-demo.sh settle|cancel [amount_sats]
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="${1:?usage: hold-demo.sh settle|cancel [amount_sats]}"
AMT="${2:-10000}"

escrow() { docker compose exec -T cln-escrow lightning-cli --regtest "$@"; }
buyer()  { docker compose exec -T cln-buyer  lightning-cli --regtest "$@"; }

# 1. we generate the preimage and hash — node never sees the preimage until settle
PREIMAGE=$(openssl rand -hex 32)
HASH=$(python3 -c "import hashlib;print(hashlib.sha256(bytes.fromhex('$PREIMAGE')).hexdigest())")
echo "preimage (secret): $PREIMAGE"
echo "payment_hash:      $HASH"

# 2. create the hold invoice from the HASH ONLY (amount = plain msat integer)
INVOICE=$(escrow holdinvoice "$HASH" $((AMT * 1000)))
BOLT11=$(echo "$INVOICE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["bolt11"])')
echo "hold invoice: $BOLT11"

echo "==> buyer pays; the payment must sit IN-FLIGHT (held, not settled)"
# retry_for=10: pay returns 'pending' after 10s — that pending IS the held state
timeout 25 docker compose exec -T cln-buyer lightning-cli --regtest -k pay bolt11="$BOLT11" retry_for=10 | python3 -c 'import json,sys; d=json.load(sys.stdin); print("    pay status:", d.get("status"), d.get("message",""))' || true

echo "==> escrow-side view (expect state=open/unpaid but HTLC accepted by hold plugin):"
escrow listholdinvoices "$HASH" | python3 -m json.tool

case "$MODE" in
  settle)
    echo "==> releasing preimage — completing buyer's payment"
    escrow settleholdinvoice "$PREIMAGE" | python3 -m json.tool
    ;;
  cancel)
    echo "==> cancelling — buyer must be auto-refunded"
    escrow cancelholdinvoice "$HASH" | python3 -m json.tool
    ;;
esac

sleep 2
echo "==> final hold-invoice state:"
escrow listholdinvoices "$HASH" | python3 -m json.tool
echo "==> buyer wallet after:"
buyer listfunds | python3 -m json.tool
