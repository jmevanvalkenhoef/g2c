"""Lab-only machinery for the regtest topology: demo pay bridge (the tap-QR
stand-in for a buyer wallet), CLN helper nodes, and peer re-healing (peers
drop across lab host restarts; production never needs this).

Imported by app.py ONLY when LAB_MODE=1. Production (VPS) never loads this.
"""
import asyncio
import os

from cln import CLN, CLNError          # raw CLN socket client (helper nodes)
from node import make_node

CLN_NETWORK = os.environ.get("CLN_NETWORK", "regtest")

# lab helper nodes (always CLN in the lab topology, regardless of the
# escrow's own NODE_BACKEND)
BUYER_NODE = make_node("cln", lightning_dir=os.environ.get("BUYER_NODE_DIR", "/sockets/buyer"),
                       network=CLN_NETWORK)
SELLER_NODE = make_node("cln", lightning_dir=os.environ.get("SELLER_NODE_DIR", "/sockets/seller"),
                        network=CLN_NETWORK)


async def demo_pay(escrow_node, sale: dict) -> None:
    """Fire-and-forget: the lab buyer node pays the hold invoice in the
    background; the reconcile loop detects the arriving HTLC on its own.
    Retries with peer re-healing (lab peers drop across restarts)."""
    buyer_host = os.environ.get("LAB_BUYER_HOST")

    async def _bg_pay():
        for attempt in range(3):
            try:
                _, status = await asyncio.to_thread(
                    BUYER_NODE.pay_invoice, sale["payment_request"], 8)
            except Exception:
                status = "error"
            ok = status in ("complete", "pending") or status.startswith("payment submitted")
            if ok:
                return
            print(f"[escrow] buyer pay retry {attempt+1}: {status}")
            if buyer_host and hasattr(escrow_node, "connect_peer"):
                try:
                    def _repeer():
                        bid = BUYER_NODE.get_info()["id"]
                        escrow_node.connect_peer(bid, buyer_host)
                    await asyncio.to_thread(_repeer)
                    print(f"[escrow] re-peered buyer via {buyer_host}")
                except Exception as e:
                    print(f"[escrow] buyer re-peer failed: {e}")
            await asyncio.sleep(5)

    asyncio.create_task(_bg_pay())


async def repeer_seller(escrow_node) -> None:
    """Re-connect the lab seller peer after payout failures (drop healing)."""
    seller_host = os.environ.get("LAB_SELLER_HOST")
    if seller_host and hasattr(escrow_node, "connect_peer"):
        try:
            def _repeer():
                sid = SELLER_NODE.get_info()["id"]
                escrow_node.connect_peer(sid, seller_host)
            await asyncio.to_thread(_repeer)
            print(f"[escrow] re-peered seller via {seller_host}")
        except Exception as e:
            print(f"[escrow] seller re-peer failed: {e}")
