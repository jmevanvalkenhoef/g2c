#!/usr/bin/env bash
# Manual drill: buyer pays a hold invoice at the escrow node,
# escrow settles (success) or cancels (failure path).
# Usage: ./scripts/hold-demo.sh settle|cancel [amount_sats]
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="${1:?usage: hold-demo.sh settle|cancel [amount_sats]}"
AMT="${2:-10000}"

escrow() { docker compose exec -T cln-escrow lightning-cli "$@"; }
buyer()  { docker compose exec -T cln-buyer  lightning-cli "$@"; }

# 1. we generate the preimage and hash — node never sees the preimage until settle
PREIMAGE=$(openssl rand -hex 32)
HASH=$(python3 -c "import hashlib;print(hashlib.sha256(bytes.fromhex('$PREIMAGE')).hexdigest())")
echo "preimage (secret): $PREIMAGE"
echo "payment_hash:      $HASH"

# 2. create the hold invoice from the HASH ONLY
INVOICE=$(escrow holdinvoice "$HASH" "${AMT}sat" "g2c PoC drill")
BOLT11=$(echo "$INVOICE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["bolt11"])')
echo "hold invoice: $BOLT11"

echo "==> buyer pays (should sit in-flight; ctrl-C here to see the held state)"
buyer pay "$BOLT11" || true

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
