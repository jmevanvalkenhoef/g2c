#!/usr/bin/env python3
"""Lab admin for the lnd-escrow node: wallet lifecycle + channel ops via REST.

Used by poc/scripts/setup-lnd.sh. Subcommands:
  init                 create wallet (idempotent) or unlock it
  getinfo              print node id
  newaddr              on-chain address
  connect <id@host>    peer
  openchannel <id> <amount_sat> [push_sat]
  channels             list channels
Env: LND_REST_URL, LND_TLS_CERT, LND_MACAROON, LND_WALLET_PASS (lab default).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lnd import LNDNode, NodeError  # noqa: E402

WALLET_PASS = os.environ.get("LND_WALLET_PASS", "g2clabpass")


def admin_node() -> LNDNode:
    # macaroon may not exist pre-wallet; cert only
    return LNDNode(rest_url=os.environ.get("LND_REST_URL", "https://localhost:8080"),
                   cert_path=os.environ.get("LND_TLS_CERT"))


def running_node() -> LNDNode:
    return LNDNode(rest_url=os.environ.get("LND_REST_URL", "https://localhost:8080"),
                   cert_path=os.environ.get("LND_TLS_CERT"),
                   macaroon_path=os.environ.get("LND_MACAROON"))


def wait_unlocked(timeout=60):
    for _ in range(timeout):
        try:
            # rebuild per attempt: the macaroon may appear/refresh after
            # wallet creation and LNDNode reads it only at construction
            return running_node().get_info()
        except NodeError:
            time.sleep(1)
    raise SystemExit("lnd never became responsive/unlocked")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "getinfo"
    if cmd == "init":
        n = admin_node()
        try:
            n.get_info()
            print("wallet already exists")
        except NodeError as e:
            if "not created" in str(e):
                # REST initwallet REQUIRES a client-supplied seed
                n.init_wallet(WALLET_PASS, n.genseed())
                print("wallet created")
            elif "locked" in str(e) or "unlock" in str(e):
                print("unlocking:", n.unlock_wallet(WALLET_PASS))
        info = wait_unlocked()
        print("ready:", info["identity_pubkey"])
    elif cmd == "getinfo":
        print(wait_unlocked()["identity_pubkey"])
    elif cmd == "newaddr":
        print(running_node().new_address())
    elif cmd == "connect":
        node_id, host = sys.argv[2].split("@")
        running_node().connect_peer(node_id, host)
        print("connected")
    elif cmd == "openchannel":
        node_id, amt = sys.argv[2], int(sys.argv[3])
        push = int(sys.argv[4]) if len(sys.argv) > 4 else 0
        txid = running_node().open_channel(node_id, amt, push, private=False)
        print("funding txid:", txid)
    elif cmd == "channels":
        n = running_node()
        res = n._req("GET", "/v1/channels")
        for ch in res.get("channels", []):
            print(json.dumps({"remote": ch["remote_pubkey"][:16],
                              "active": ch.get("active"),
                              "local_msat": ch.get("local_balance"),
                              "capacity": ch.get("capacity")}))
    else:
        raise SystemExit(f"unknown command {cmd}")


if __name__ == "__main__":
    main()
