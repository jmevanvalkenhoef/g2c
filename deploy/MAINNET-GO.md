# Mainnet go-checklist — everything up to and including first funding

Node: `g2c-escrow` · `<node-pubkey>`
Status (2026-10-23): funded, channel open, first real mainnet sale completed
end to end. Sections 1-5 below are the record of how it got there.
Previously: synced, systemd-managed (lnd + lnd-unlock + g2c-escrow + g2c-game, all
`restart=always`, enabled at boot). Escrow and game run the LND-ported code
that passed the full drill battery on regtest.

## 1. WALLET BACKUP (before funding — non-negotiable)

On the laptop (files never pass through chat):

```bash
scp root@g2c:/root/.lnd-wallet-seed  ~/.g2c-backup-wallet-seed
scp root@g2c:/root/.lnd-wallet-pass  ~/.g2c-backup-wallet-pass
chmod 600 ~/.g2c-backup-*
# verify the 24 words are readable, store offline (paper + encrypted copy)
```

## 2. FUND THE NODE

Send **0.006 BTC** (≈ 500k sat channel + push + on-chain fees + margin) to:

```
<funding-address>
```

(Regenerate with `ssh root@g2c '~/go/bin/lncli newaddress p2wkh'` if stale.)
Confirmations: 1 block (~10 min). Check: `lncli walletbalance`.

## 3. OPEN THE CHANNEL (single-hub topology)

Well-connected hub candidates observed in our live graph:
- ACINQ (Phoenix backend, ~1900 channels): 03864ef025fde8fb587d989186ce6a4a186895ee44a926bfc370e2c366597a3f8f
- 033d8656… (WalletOfSatoshi-associated hub): 033d8656219478701227199cbd6f670335c8d408a92ae88b962c49d4dc0e83e025

```bash
PEER=<pubkey from above>@<their host:port>   # get addr: lncli getnodeinfo <pubkey>
ssh root@g2c "~/go/bin/lncli connect $PEER"
# 500k sat channel, 250k pushed to their side = our inbound capacity;
# our remaining 250k = outbound for seller payouts. PUBLIC (announced).
ssh root@g2c '~/go/bin/lncli openchannel --node_key=<pubkey> \
    --local_amt=500000 --push_amt=250000 --conf_target=6'
# wait 6 confirmations, then verify:
ssh root@g2c "~/go/bin/lncli listchannels"
```

## 4. SELLER PAYOUT SETUP (Phone B)

Either:
- Wallet of Satoshi: put `name@walletofsatoshi.com` in Alice's payout
  profile via the PWA (tap the banner), or
- Zeus: create an NWC connection (make_invoice only, nos.lol relay) and
  paste the URI. Both are live paths; LNURL is the simpler first demo.

## 5. THE DRILL (phones)

1. Phone A + B: browse `http://<vps-ip>:8200`
2. Phone B logs in as **Alice** (seller), sets payout, lists an item
3. Phone A logs in as **Bob** (buyer), taps Buy
4. Phone A's wallet (any Lightning wallet) scans the QR and pays
5. Escrow: HTLC held → item transfers → seller paid → settle
6. Watch: `ssh root@g2c 'journalctl -u g2c-escrow -f'`

## Standing rules (mainnet)

- No `--force-recreate` / `down -v` on anything touching LND data once
  channels exist. `systemctl restart` is safe.
- Destructive testing stays on the regtest lab, never here.
- If the channel pair side drifts (many sales), rebalance or open a second
  channel — do not let outbound hit zero mid-sale (the payout would fail,
  the sale rolls back and refunds; ugly but safe).

## 6. Upgrading to the money-safety release

This release closes the paths where a sale could pay the seller AND refund
the buyer (in-flight payouts treated as failed, payouts after the buyer HTLC
was gone, settle errors read as success, payout lookups capped at 500).

1. **Drain first.** No sale may be mid-flight during the upgrade:
   `curl -s localhost:8100/v1/metrics` must show only COMPLETED / CANCELLED
   (or ERROR) in `states`. Pause listings in the game if needed.
2. `git pull` on the VPS.
3. Add to `/etc/g2c/escrow.env` (see `systemd/escrow.env.example`):
   `FEE_PPM=<your pricing>`, `HOLD_CLTV_EXPIRY=288`, `HOLD_SAFETY_BLOCKS=24`,
   `MIN_PAYOUT_CLTV=60`. The seller payout's routing fee is now capped at the
   sale's fee: with a flat 1-sat fee most payouts would fail and refund, so
   set `FEE_PPM` (or `MAX_ROUTING_FEE_MSAT`) deliberately.
4. The escrow now **refuses to start** with an empty or `dev-secret`
   `ESCROW_SHARED_SECRET`, or when `LND_TLS_CERT` / `LND_MACAROON` point at a
   missing file. That's intended.
5. `systemctl restart g2c-escrow g2c-game`, then `curl localhost:8100/v1/health`.
6. Game logins now take a password: the first login to Alice/Bob sets it.
7. One small sale end to end, then watch `journalctl -u g2c-escrow -f`.

`/v1/metrics` now lists `errors_needing_attention`. Anything there with a
`LOSS:` reason means the seller was paid but the buyer's HTLC got cancelled:
reconcile by hand. It should never appear; alert on it if you add monitoring.

## 7. Hardening (do soon, none of it changes behaviour)

- **Channel backup off the box**, after every channel open:
  `scp root@g2c:/root/.lnd/data/chain/bitcoin/mainnet/channel.backup ~/.g2c-backup-channel.backup`.
  The seed alone does not recover channel funds.
- **Least-privilege macaroon** for the escrow instead of `admin.macaroon`:
  ```bash
  lncli bakemacaroon info:read invoices:read invoices:write \
      offchain:read offchain:write --save_to /root/.lnd/escrow.macaroon
  ```
  then point `LND_MACAROON` at it. A leaked escrow key can then no longer
  move on-chain funds or close channels.
- **Non-root services**: run escrow and game as a dedicated `g2c` user
  (`User=g2c` in the units, data dirs owned by it, macaroon readable by it).
- The wallet password sits in `/root/.lnd-wallet-pass` for unattended unlock:
  keep it root-only, and accept that root on the VPS = the hot wallet.
