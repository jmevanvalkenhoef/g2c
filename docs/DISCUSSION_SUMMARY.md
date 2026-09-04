# g2c — Discussion Summary & Decision Log

Date: 2026-02-14
Status: Architecture agreed, service spec in `ESCROW_SERVICE_SPEC.md`

## Context

A centralized game database (operated by a **third party**) holds ownership of in-game
items. Players (buyer and seller) have their **own Lightning wallets** — mostly custodial
(Wallet of Satoshi, Strike, …). No satoshis are held by the game and none should be held
as user balances by us. We provide a **standalone escrow service** that atomically
coordinates:

> buyer pays sats over Lightning ⇄ item ownership transfers in the game DB ⇄ seller
> receives sats over Lightning, minus our fee.

## Journey / key decisions

1. **"Atomic swap DB ⇄ Lightning" is not cryptographic — it's transactional.**
   A DB row is a trusted ledger entry, not a cryptographic claim. Lightning's invoice
   lifecycle is used as the two-phase-commit primitive instead.

2. **Deposits/purchases: hold invoices are the core mechanism.**
   HTLC arrives and locks → we do our work (item transfer + seller payout) →
   `settle(preimage)` or `cancel` (buyer auto-refunded by protocol, no refund flow
   exists). The failure mode "money moved but goods didn't" is designed out, not handled.

3. **Third-party DB changes the atomicity budget.**
   The item transfer is a remote API call, not a local SQL commit. Therefore:
   - every mutation on the game side must be **idempotent** with
     `idempotency_key = payment_hash`
   - the game must expose `sale_status` for reconciling ambiguous (timed-out) calls
   - **rule: settle the hold invoice only after the transfer is confirmed**, never after
     merely "sent"

4. **No balances anywhere.**
   Seller is paid inline at purchase time; fee is the spread (hold invoice for X,
   pay seller X − fee; the difference stays in our node). Earlier designs with seller
   balances / withdrawal state machines were dropped when it became clear the game DB is
   items-only and both parties have external wallets.

5. **Buyer's Lightning address: never needed.** The BOLT11 payment request is the only
   claim on the buyer. **Seller's Lightning address: needed** — passed in per sale,
   resolved to a fresh invoice at sale time (LNURL / LUD-16), re-fetchable on retry
   (one-shot invoices expire; addresses don't).

6. **Ordinary seller wallets can't receive hold invoices** (they auto-settle). So the
   two Lightning legs cannot both be held; we chose **Design 1 (sequential with
   compensation)** over Design 2 (same-preimage chaining via HTLC interception) for the
   first version. Design 2 remains a documented future upgrade.

7. **Node requirement and why Greenlight.**
   Hold invoices are a *receiver-side settlement policy* — you must control the node the
   HTLC arrives at. Handing a preimage to a third-party node destroys exactly the
   property you need (operator can settle unilaterally); handing only the hash keeps the
   cryptographic gate but still gives the operator custody of the float and isn't offered
   by any mainstream API. Custodial payment APIs (ZBD/OpenNode/Strike) settle instantly
   → refund-based escrow → weaker guarantees and you'd need the buyer's address back.
   Conclusion: the escrow must be a Lightning participant. **Greenlight (Blockstream)** =
   hosted CLN node, keys stay ours, no server babysitting. Sweet spot.

8. **Ordering principle: order operations by reversibility.**
   Most reversible first, irreversible last:
   `HTLC locked (cancelable) → item transfer (rollback API) → seller payment
   (irreversible) → settle (local, effectively can't fail)`.

## Trust model (explicit)

| Hop | Mechanism |
|---|---|
| player → their custodial wallet | institutional trust |
| buyer wallet → our node | real Lightning routing |
| our escrow (item transfer, payout timing) | institutional trust — we are the coordinator |
| game DB | institutional trust for item ownership |

No hop can be made cryptographic for the DB side; the design minimizes the window and
makes every failure unwind to "no money moved."

## Out of scope

Early tangents on USDT/chain liquidity, Omni history, and Lightning-address mechanics
informed vocabulary only; nothing in the service depends on them.

## Critical assumption — VERIFIED NEGATIVE for Greenlight (2026-02-14)

**Greenlight does not support hold invoices** (and cannot load custom plugins at all;
plugin support is open brainstorm issue Blockstream/greenlight#423, "TBD", since
May 2024). See `verification-greenlight-holdinvoices.md` for evidence.

The CLN plugin side is excellent: **`BoltzExchange/hold`** (Boltz's production plugin,
actively maintained) provides hash-only hold invoice creation — `holdinvoice <hash>
<amount>`, `settleholdinvoice <preimage>`, `cancelholdinvoice`, `injectinvoice` (→
Design 2!), gRPC + streaming, CLTV auto-cancel and MPP safety nets.

Fallback decision required: **A)** self-run CLN on VPS + `BoltzExchange/hold`
(recommended), **B)** self-run/hosted LND (native `AddHoldInvoice`), **C)** ask
Blockstream to whitelist the plugin (parallel, non-blocking).
