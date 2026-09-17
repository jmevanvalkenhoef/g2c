"""NodeClient — backend-agnostic interface over the escrow's Lightning node.

Implementations:
  CLNNode (this file)  — wraps cln.py's JSON-RPC client (validated path)
  LNDNode (lnd.py)     — LND REST client (mainnet/VPS profile, neutrino)

Unified state vocabularies:
  hold invoice state: "open" | "accepted" | "settled" | "cancelled" | None(unknown)
  payment state:      "pending" | "complete" | "failed" | None(unknown)
"""
import os
from typing import Optional, Protocol

import cln


class NodeError(RuntimeError):
    pass


class NodeClient(Protocol):
    def get_info(self) -> dict: ...
    def create_hold_invoice(self, payment_hash_hex: str, amount_msat: int) -> str: ...
    def settle_hold_invoice(self, preimage_hex: str) -> None: ...
    def cancel_hold_invoice(self, payment_hash_hex: str) -> None: ...
    def hold_invoice_state(self, payment_hash_hex: str) -> Optional[str]: ...
    def pay_invoice(self, bolt11: str, timeout_s: int) -> tuple[bool, str]: ...
    def payment_state(self, payment_hash_hex: str) -> Optional[str]: ...
    def decode_invoice(self, bolt11: str) -> dict: ...
    # lab profile (deterministic seller invoices)
    def create_invoice(self, amount_msat: int, label: str, desc: str) -> tuple[str, str]: ...
    def find_invoice_by_label(self, label: str) -> Optional[dict]: ...


# ---------------------------------------------------------------- CLN

_CLN_STATE = {"unpaid": "open", "accepted": "accepted", "paid": "settled",
              "settled": "settled", "cancelled": "cancelled",
              "expired": "cancelled"}


class CLNNode:
    def __init__(self, node: cln.CLN):
        self.node = node

    def get_info(self) -> dict:
        return self.node.call("getinfo")

    def create_hold_invoice(self, payment_hash_hex: str, amount_msat: int) -> str:
        # hash-only creation: the node never sees the preimage
        inv = self.node.call("holdinvoice", [payment_hash_hex, amount_msat])
        return inv["bolt11"]

    def settle_hold_invoice(self, preimage_hex: str) -> None:
        self.node.call("settleholdinvoice", [preimage_hex])

    def cancel_hold_invoice(self, payment_hash_hex: str) -> None:
        self.node.call("cancelholdinvoice", [payment_hash_hex])

    def hold_invoice_state(self, payment_hash_hex: str) -> Optional[str]:
        try:
            res = self.node.call("listholdinvoices", [payment_hash_hex])
        except cln.CLNError:
            return None
        invs = res.get("holdinvoices", [])
        if not invs:
            return None
        return _CLN_STATE.get(invs[0].get("state"))

    def pay_invoice(self, bolt11: str, timeout_s: int) -> tuple[bool, str]:
        try:
            res = self.node.call("pay", {"bolt11": bolt11, "retry_for": timeout_s})
            status = res.get("status", "pending")
        except cln.CLNError as e:
            return False, str(e)
        return status == "complete", status

    def payment_state(self, payment_hash_hex: str) -> Optional[str]:
        try:
            res = self.node.call("listpays", {"payment_hash": payment_hash_hex})
            pays = res.get("pays", [])
        except cln.CLNError:
            return None
        return pays[-1]["status"] if pays else None

    def decode_invoice(self, bolt11: str) -> dict:
        d = self.node.call("decodepay", [bolt11])
        out = {"payment_hash": d.get("payment_hash")}
        amt = d.get("amount_msat")
        out["amount_msat"] = int(amt) if amt is not None else None
        return out

    def create_invoice(self, amount_msat: int, label: str, desc: str) -> tuple[str, str]:
        inv = self.node.call("invoice", [amount_msat, label, desc])
        return inv["bolt11"], inv["payment_hash"]

    def find_invoice_by_label(self, label: str) -> Optional[dict]:
        res = self.node.call("listinvoices", {"label": label})
        invs = res.get("invoices", [])
        if not invs:
            return None
        inv = invs[0]
        st = inv.get("status")              # paid | unpaid | expired
        state = "settled" if st == "paid" else ("cancelled" if st == "expired" else "open")
        return {"bolt11": inv["bolt11"], "payment_hash": inv["payment_hash"],
                "state": state}


# ---------------------------------------------------------------- factory

def make_node(kind: Optional[str] = None, **kwargs) -> NodeClient:
    """kind: 'cln' | 'lnd' (env NODE_BACKEND, default cln). kwargs are
    backend-specific (lightning_dir for CLN; rest_url/cert/macaroon for LND)."""
    kind = (kind or os.environ.get("NODE_BACKEND", "cln")).lower()
    if kind == "cln":
        return CLNNode(cln.CLN(kwargs.get("lightning_dir", os.environ.get(
            "ESCROW_NODE_DIR", "/sockets/escrow")),
            kwargs.get("network", os.environ.get("CLN_NETWORK", "regtest"))))
    if kind == "lnd":
        from lnd import LNDNode
        allowed = {"rest_url", "cert_path", "macaroon_path"}
        return LNDNode(**{k: v for k, v in kwargs.items() if k in allowed})
    raise NodeError(f"unknown NODE_BACKEND: {kind!r}")
