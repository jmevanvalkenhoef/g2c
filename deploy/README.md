# g2c signet deployment — Phase 3 runbook

One VPS, real phones (Zeus on signet), real seller payouts. All coins are
signet sats (free, from faucets). Read once, then follow the checklist.

## 0. What's different from the regtest lab

| | lab (poc/) | signet (deploy/) |
|---|---|---|
| network | private regtest | public signet |
| blocks | local miner 1/15s | real network (~10 min) |
| buyer wallet | cln-buyer node + tap-QR bridge | **players' Zeus wallets scan the QR** (DEMO_BRIDGE=0) |
| seller payout | lab's cln-seller node | **seller's real payout method** (Lightning address) |
| TLS | none (localhost) | Caddy + Let's Encrypt (needs DOMAIN) |
| fee | 100 msat | 1000 msat (configurable) |

Note on funding speed: real signet blocks are ~10 minutes. Channel opens and
their announcements take ~1 hour to become usable. Plan onboarding accordingly
(do it once, well before demo time).

## 1. VPS setup (once)

```bash
# any small VPS (1-2 GB RAM is fine), Docker installed, ports open:
#   80, 443 (web), 9735 (lightning p2p)
git clone https://github.com/jvanvalkenhoef/g2c && cd g2c/deploy
cat > .env <<EOF
DOMAIN=escrow.example.org        # or your domain; A-record -> VPS IP
ESCROW_SHARED_SECRET=$(openssl rand -hex 24)
EOF
docker compose up -d
docker compose logs -f caddy         # watch the TLS certificate issue
```

## 2. Fund the node (once)

1. **Signet coins** for the CLN wallet:
   ```bash
   ADDR=$(docker compose exec cln lightning-cli --signet newaddr bech32 | \
          python3 -c 'import json,sys;print(json.load(sys.stdin)["bech32"])')
   echo "fund $ADDR at e.g. https://signetfaucet.com (0.01-0.05 sBTC)"
   ```
2. Wait for a confirmation (~10 min), then **open an outbound channel** to a
   well-connected signet peer (so we can pay seller invoices):
   ```bash
   docker compose exec cln lightning-cli --signet \
     fundchannel <NODEID@HOST:PORT> 4000000sat
   ```
   Find peers: `lightning-cli --signet listnodes`, or ask in CLN/signet
   community channels. Announcement needs ~6 blocks (~1 h).
3. **Inbound capacity** (so phones can pay us): ask a peer to open a channel
   to our node id (`getinfo`), or use a signet channel-opening service.

## 3. Phones (per player, once)

1. Install **Zeus**, enable signet: Settings → node → network → signet.
2. Get signet coins from a faucet to a Zeus address.
3. Buyer: open a channel Zeus → our node (or to a well-connected peer if our
   node has enough inbound).
4. **Seller payout (lnurl profile)**: the seller needs a Lightning address on
   signet — this is the current gap; see "known gaps" below. For the first
   signet demo, sellers can be another CLN node you control (set
   `PAYOUT_MODE=lab` + point `SELLER_NODE_DIR` at it), or run the demo with
   the seller side on a wallet that provides signet LN addresses.

## 4. Demo flow (what changes vs the lab)

- Player taps **Buy** → QR appears → **scan it with Zeus** → pay.
  The tap does nothing anymore (DEMO_BRIDGE=0); the HTLC arriving at our node
  starts everything, exactly like the lab.
- Watch: `docker compose logs -f escrow` (state transitions) and
  `docker compose exec cln lightning-cli --signet listholdinvoices`.

## 5. Known gaps on signet (honest list)

- Signet Lightning-address providers are scarce: sellers need one that
  mints signet invoices (or run the lab's regtest flow instead).
- Funding/announce timing is real-blockchain slow (~1 h per channel setup).
- The PWA still says "tap the QR to pay" — on signet it should read "scan
  with your wallet" (cosmetic; the pill text comes from the escrow response).
- No backup story yet for `cln-data` (hsm_secret + channel DBs): snapshot the
  volume or lose the wallet. Do not put real mainnet money here without a
  proper backup + ops runbook (spec §10).
