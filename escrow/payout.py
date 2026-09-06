"""PayoutResolver — turns a seller's payout method into a payable BOLT11.

Profiles:
  lab  (type "stub"):  invoice minted at the lab's cln-seller node. Regtest
                       only; stands in for a real seller wallet.
  lnurl (type "lnurl"): Lightning address (LUD-16). Two HTTPS GETs, fresh
                       invoice minted at purchase time, seller never involved.
  nwc  (type "nwc"):   Nostr Wallet Connect (NIP-47): make_invoice via
                       Nostr relays, NIP-44 encrypted, BIP340 signed.

All resolvers return (bolt11, payment_hash) or raise PayoutError. The
payment_hash lets the caller verify/crash-recover the payout via listpays.
"""
import re
from typing import Optional
from urllib.parse import urlparse

import httpx

from cln import CLN, CLNError


class PayoutError(RuntimeError):
    pass


LNADDR_RE = re.compile(r"^[\w.\-+]+@[\w.\-]+(\.[\w\-]+)+$")


def resolve_lnurl(address: str, amount_msat: int,
                  client: Optional[httpx.Client] = None) -> tuple[str, str]:
    """LUD-16: user@domain -> fresh invoice for amount_msat."""
    if not LNADDR_RE.match(address):
        raise PayoutError(f"not a lightning address: {address!r}")
    name, domain = address.split("@")
    own = client is None
    c = client or httpx.Client(timeout=10)
    try:
        base = f"https://{domain}/.well-known/lnurlp/{name}"
        r = c.get(base)
        r.raise_for_status()
        pay = r.json()
        callback = pay.get("callback")
        lo, hi = pay.get("minSendable", 0), pay.get("maxSendable", 10**12)
        if not callback:
            raise PayoutError("lnurlp endpoint missing callback")
        if not (lo <= amount_msat <= hi):
            raise PayoutError(f"amount {amount_msat} outside [{lo},{hi}]")
        r = c.get(callback, params={"amount": amount_msat})
        r.raise_for_status()
        pr = r.json().get("pr")
        if not pr:
            raise PayoutError("callback returned no invoice")
        return pr, ""
    except httpx.HTTPError as e:
        raise PayoutError(f"lnurl fetch failed: {e}") from e
    finally:
        if own:
            c.close()


def verify_invoice(node: CLN, bolt11: str, expect_msat: int) -> str:
    """Decode via our own node; check amount; return payment_hash."""
    try:
        d = node.call("decodepay", [bolt11])
    except CLNError as e:
        raise PayoutError(f"decodepay failed: {e}") from e
    got = d.get("amount_msat")
    if got is not None and int(got) != expect_msat:
        raise PayoutError(f"invoice amount {got} != expected {expect_msat}")
    return d["payment_hash"]


class PayoutResolver:
    """Dispatch on seller_payout['type']. Returns (bolt11, payment_hash)."""

    def __init__(self, seller_node: Optional[CLN] = None,
                 escrow_node: Optional[CLN] = None):
        self.seller_node = seller_node   # lab profile only
        self.escrow_node = escrow_node

    def resolve(self, payout: dict, sale: dict) -> tuple[str, str]:
        ptype = (payout or {}).get("type", "stub")
        if ptype in ("stub", "lab"):
            return self._lab(sale)
        if ptype == "lnurl":
            bolt11, _ = resolve_lnurl(payout["target"], sale["seller_amount_msat"])
            pay_hash = verify_invoice(self.escrow_node, bolt11,
                                      sale["seller_amount_msat"])
            return bolt11, pay_hash
        if ptype == "nwc":
            import nwc
            try:
                bolt11, pay_hash = nwc.request_invoice(
                    payout["target"], sale["seller_amount_msat"],
                    "g2c sale payout")
            except nwc.NWCError as e:
                raise PayoutError(f"nwc: {e}") from e
            verify_invoice(self.escrow_node, bolt11, sale["seller_amount_msat"])
            return bolt11, pay_hash
        raise PayoutError(f"unknown payout type: {ptype!r}")

    def _lab(self, sale: dict) -> tuple[str, str]:
        label = f"g2c-{sale['sale_id']}"
        try:
            inv = self.seller_node.call(
                "invoice", [sale["seller_amount_msat"], label, "g2c sale payout"])
            return inv["bolt11"], inv["payment_hash"]
        except CLNError as e:
            if "already" not in str(e).lower():
                raise PayoutError(f"invoice: {e}") from e
            # deterministic label: recover the pre-crash invoice (idempotency)
            found = self.seller_node.call("listinvoices", {"label": label})
            invs = found.get("invoices", [])
            if not invs:
                raise PayoutError("invoice: label exists but not found")
            return invs[0]["bolt11"], invs[0]["payment_hash"]
