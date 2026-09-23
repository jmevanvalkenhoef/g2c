# Mainnet go-checklist — everything up to and including first funding

Node: `g2c-escrow` · `<node-pubkey>`
Status: synced, systemd-managed (lnd + lnd-unlock + g2c-escrow + g2c-game, all
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
