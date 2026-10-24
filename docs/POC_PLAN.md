# PoC Plan — Escrow Service + Vertical-Slice Game

Status: DRAFT v0.2 (revised for playable-game PoC) · Date: 2026-02-14
Supersedes v0.1 phase list. Deps: `ESCROW_SERVICE_SPEC.md`, `verification-greenlight-holdinvoices.md`.

---

## PoC goal

A playable micro-marketplace: two (or n) players on phones, each with their own
Lightning wallet, trade an in-game item for sats through the escrow — with the spec's
guarantee intact:

> buyer's payment completes ⇔ item ownership transferred AND seller paid;
> every failure path unwinds to "no sats moved".

Not goals: APK polish/app-store, embedded wallets, production security, HA,
regulatory sign-off, scale.

## Key scope decisions

1. **PWA, not APK.** Mobile web app on a VPS URL; "add to home screen" on Android.
   APK later via Capacitor/TWA wrap if the concept proves out.
2. **Wallets = Zeus (signet mode)** on players' phones. Matches the production model
   (external, self-custodial wallets; game never touches sats).
3. **Seller payout via Lightning address** (LUD-16): the seller links an
   address to their game profile once; at purchase the escrow requests a fresh
   invoice for the exact net amount and pays it — zero seller interaction at
   sale time. (Nostr Wallet Connect was built and later dropped: one payout
   method keeps the product and the code simple.)
4. **Environment ladder**: regtest (dev, scripted payer) → signet VPS + phones
   (multi-device demo) → optional mainnet with throwaway sums (custodial-wallet UX).

## Components (target layout)

```
game/                    # NEW in v0.2
  game-backend/          # FastAPI + SQLite: users, inventory, listings, purchases
                         #   - supersedes mock-game; KEEPS execute_sale/sale_status/
                         #     rollback_sale contract (idempotent on payment_hash)
                         #   (since replaced by reserve/commit/release, spec §3.2)
                         #   - calls escrow POST /v1/sales on buy; polls sale status
  game-web/              # PWA: inventory, list item (invoice attach), market view,
                         #   buy → show BOLT11 QR; single-page, no build toolchain
  escrow/                # THE remaining core build (spec §4/§8/§9):
                         #   state machine + SQLite + CLN client (hold plugin RPC)
poc/                     # existing regtest lab (infra for phases 1-2)
docs/                    # unchanged
```

## Phase 1 — Escrow service core (2–4 days) — against regtest lab

- Python (PoC language decision: Python for speed; money-critical rewrite later if
  warranted).
- Implements spec §4 state machine, §9 schema, §6 failure handling; drives
  `holdinvoice/settleholdinvoice/cancelholdinvoice/listholdinvoices` on cln-escrow.
- Tested against the existing regtest compose + fault-injecting game API.
- Exit: every failure-matrix row passes scripted; kill -9 at every transition recovers.

## Phase 2 — Playable game on regtest (2–3 days)

- game-backend (replaces mock-game in compose), game-web PWA.
- Clients = desktop browsers; payer = scripted cln-buyer (phones can't reach regtest).
- Exit: full happy path via GUI (list → buy → QR → pay → item moves); at least one
  failure drill (expired seller invoice, execute_sale reject) via GUI.

## Phase 3 — Signet VPS + real phones (2–3 days + coordination)

- Move stack (escrow + game backend + web) to small VPS; CLN on signet; HTTPS.
- Each player: Zeus (signet), signet faucet coins, open channel Zeus→escrow node.
- The demo: list item on phone A, buy on phone B, settle in Zeus, item moves.
- Exit: demo recorded; hold-window p99 measured on real paths; UX notes written.

## Phase 4 — Verdict (½ day)

- `poc-results.md`: go/no-go, latencies, wallet UX, ops-feel, open issues
  (incl. seller-invoice UX replacement via LN address on mainnet, regulatory gate).

## Cost estimate

- VPS: 1 small instance (~1 month)
- Signet coins: free (faucets)
- Mainnet (optional Phase 3.5): ~1M sats locked, recoverable
- Calendar: ~2 weeks part-time; the escrow core (Phase 1) is the only hard part.

## Complete when

- [ ] Phase 1 exit: failure matrix green incl. crash drills
- [ ] Phase 2 exit: GUI happy path + failure drill on regtest
- [ ] Phase 3 exit: two phones, two Zeus wallets, one trade end-to-end on signet
- [ ] No state ever reached where sats moved without ownership transfer
- [ ] `poc-results.md` with go/no-go
