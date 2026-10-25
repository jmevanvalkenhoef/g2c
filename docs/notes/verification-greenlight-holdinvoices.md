# Verification: Greenlight + hold invoices (Milestone 0)

Date: 2026-02-14 · Result: **Greenlight: NOT SUPPORTED today · Plugin ecosystem: SOLID**

## Finding 1 — CLN core has no native hold invoices

`ElementsProject/lightning` master tree contains no `holdinvoice` RPC
(no `doc/lightning-holdinvoice*`; only test fixtures `tests/plugins/hold_invoice.py`).
Hold invoices on CLN **require a plugin**. Confirmed.

## Finding 2 — the plugin exists and is production-grade: `BoltzExchange/hold`

- Successor of `daywalker90/holdinvoice` (archived, README redirects to Boltz's fork).
- Used by Boltz Exchange in production for their submarine-swap escrows.
- Actively maintained (last push 2026-04-30).
- API (exact match for our spec §2):
  - `holdinvoice <payment_hash> <amount>` — creates invoice from **hash only**
    (we keep the preimage secret until settle; node cannot settle unilaterally)
  - `settleholdinvoice <preimage>` — releases the HTLC
  - `cancelholdinvoice <payment_hash>` — cancels
  - `injectinvoice <invoice>` — adopts a third-party invoice by hash → enables
    Design 2 (same-preimage chaining) without HTLC interception plumbing
  - `listholdinvoices` — for reconciliation
  - gRPC interface with streaming updates (`protos/hold.proto`), mTLS client certs
- Safety nets: `hold-expiry-deadline` (auto-cancel N blocks before shortest HTLC
  expiry, default 3), `hold-mpp-timeout` (MPP shard handling, default 60 s).
- Storage: SQLite or PostgreSQL. Release binaries x86_64/armv7/aarch64.

## Finding 3 — Greenlight cannot load custom plugins

- Issue **Blockstream/greenlight#423** "design: Plugin support for Greenlight":
  open since **2024-05-03**, body is literally "TBD", label `state::brainstorming`,
  **zero comments**. Not on any visible roadmap.
- In-repo docs (24 .md files) contain **no mention** of user-loadable plugins or
  user-settable node config (`--plugin`, `important-plugin`, `setconfig`): the only
  plugin references are Greenlight's own `gl-plugin` (their internal mTLS/LSP plugin).
- Greenlight nodes are "a slightly modified Core Lightning node" whose command line
  and plugin set Blockstream controls; users get cln-grpc RPCs only — and since core
  CLN has no hold RPCs (Finding 1), there is nothing to call.
- Webhooks/notifications in Greenlight fire **after** settlement → useless for holds.

Confidence: high on plugin facts (READMEs, repo trees, release history); high on
Greenlight absence (issue tracker + full docs grep + architecture). Undocumented
escapes (e.g. Blockstream whitelisting a partner's plugin on request) can't be ruled
out from public sources — that's a question to ask them, not a thing to assume.

## Consequences — decision required (spec §12 Milestone 0b)

| Option | What | Trade-offs |
|---|---|---|
| **A. Self-run CLN on VPS + `BoltzExchange/hold`** | Docker or release binary; our keys, our channels | Standard ops (uptime, liquidity, backups); stays on the CLN path incl. Design 2 via `injectinvoice` |
| **B. Self-run LND on VPS (or LND hoster)** | Native `AddHoldInvoice`/`SettleInvoice`/`CancelInvoice`/`SubscribeSingleInvoice`; spec primitives map 1:1 | LND's hold RPC **requires supplying the preimage** at creation (weaker than hash-only, still fine for Design 1); LND route loses `injectinvoice`-style Design 2 |
| **C. Ask Blockstream** | Open an issue / partner channel: whitelist `hold` or prioritize #423 | Unknown timeline; blocks the project if it's the only path — pursue in parallel, don't wait on it |

Recommendation: **A**, with C pursued in parallel. The plugin is the differentiator:
hash-only creation means not even the node operator could settle early — the exact
property we wanted from Greenlight, achieved by owning the node outright.
