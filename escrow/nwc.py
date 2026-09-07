"""Nostr Wallet Connect client — the 'make_invoice' half of NWC (NIP-47).

The escrow holds a seller's wallet-connect URI (secret = OUR nostr key,
wallet = the seller's nostr service key, e.g. Zeus). To pay a seller we ask
their wallet for a fresh invoice over a Nostr relay:

  kind 23194 (request)  content = NIP-44(json {"method":"make_invoice",...})
  kind 23195 (response) content = NIP-44(json {"result":{"invoice":...}})

Sync websocket (websocket-client): called from the sync payout path, with a
hard timeout — a seller's offline wallet must not stall the escrow forever.
"""
import hashlib
import json
import os
import secrets
import time
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

import websocket  # websocket-client

import bip340
import nip04
import nip44


class NWCError(RuntimeError):
    pass


def parse_uri(uri: str) -> dict:
    """nostr+walletconnect://<wallet_pubkey>?relay=<url>&secret=<hex>"""
    if not uri.startswith("nostr+walletconnect://"):
        raise NWCError(f"not a walletconnect URI: {uri[:40]!r}")
    parsed = urlparse(uri.replace("nostr+walletconnect://", "http://"))
    q = parse_qs(parsed.query)
    wallet = parsed.netloc.lower()
    try:
        relays = [unquote(r) for r in q["relay"]]
        secret = q["secret"][0]
    except KeyError as e:
        raise NWCError(f"URI missing {e}") from e
    if len(secret) != 64:
        raise NWCError("bad secret (want 64 hex chars)")
    return {"wallet": wallet, "relays": relays,
            "secret": bytes.fromhex(secret)}


def _event_id(pubkey: str, created_at: int, kind: int,
              tags: list, content: str) -> str:
    serial = json.dumps([0, pubkey, created_at, kind, tags, content],
                        separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serial.encode()).hexdigest()


def _make_request_event(secret: bytes, wallet_pub: str, payload: dict) -> dict:
    # Zeus's NWC service speaks NIP-04; the NIP-47 spec says NIP-44 — send
    # NIP-04 (works everywhere today) and accept either on responses.
    ck = nip04.conversation_key(secret, bytes.fromhex(wallet_pub))
    content = nip04.encrypt(
        json.dumps(payload, separators=(",", ":")).encode(), ck)
    pub = bip340.pubkey(secret).hex()
    created_at = int(time.time())
    tags = [["p", wallet_pub]]
    eid = _event_id(pub, created_at, 23194, tags, content)
    sig = bip340.sign(secret, bytes.fromhex(eid)).hex()
    return {"id": eid, "pubkey": pub, "created_at": created_at,
            "kind": 23194, "tags": tags, "content": content, "sig": sig}


def _parse_response(event: dict, secret: bytes, wallet_pub: str) -> dict:
    if event.get("pubkey", "").lower() != wallet_pub:
        raise NWCError("response from unexpected pubkey")
    if not bip340_signs_event(event):
        raise NWCError("response signature invalid")
    if nip04.is_nip04(event["content"]):
        ck = nip04.conversation_key(secret, bytes.fromhex(wallet_pub))
        content = nip04.decrypt(event["content"], ck)
    else:
        ck = nip44.get_conversation_key(secret, bytes.fromhex(wallet_pub))
        content = nip44.decrypt(nip44.b64_decode(event["content"]), ck)
    return json.loads(content)


def bip340_signs_event(event: dict) -> bool:
    """verify the response really came from the wallet's key"""
    import hashlib as H
    try:
        eid = _event_id(event["pubkey"], event["created_at"], event["kind"],
                        event["tags"], event["content"])
        if eid != event["id"]:
            return False
        sig = bytes.fromhex(event["sig"])
        R_x, s = sig[:32], int.from_bytes(sig[32:], "big")
        N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
        t = H.sha256(b"BIP0340/challenge").digest()
        e = int.from_bytes(H.sha256(t + t + R_x + bytes.fromhex(event["pubkey"])
                                    + bytes.fromhex(eid)).digest(), "big") % N
        return _verify_points(R_x, s, e, event["pubkey"])
    except Exception:
        return False


def _verify_points(R_x, s, e, pub_hex) -> bool:
    """R == s*G - e*P  (with P = even-y lift of the x-only pubkey)"""
    N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    from coincurve import PrivateKey, PublicKey
    sG = PrivateKey(secret=s.to_bytes(32, "big")).public_key
    P = PublicKey(bytes.fromhex("02" + pub_hex))
    neg_eP = P.multiply(((N - e) % N).to_bytes(32, "big"))
    combined = PublicKey.combine_keys([sG, neg_eP])
    return combined.format(compressed=True)[1:] == R_x


def request_invoice(uri: str, amount_msat: int, description: str,
                    timeout: float = 25.0) -> tuple[str, str]:
    """Ask the seller's wallet for an invoice. Returns (bolt11, payment_hash).
    Raises NWCError on wallet error / timeout / unreachable relay."""
    cfg = parse_uri(uri)
    secret, wallet = cfg["secret"], cfg["wallet"]
    req = _make_request_event(secret, wallet, {
        "method": "make_invoice",
        "params": {"amount_msat": amount_msat, "description": description},
    })
    last_err: Optional[str] = None
    for relay in cfg["relays"]:
        try:
            return _request_via_relay(relay, req, secret, wallet, timeout)
        except NWCError as e:
            last_err = f"{relay}: {e}"
    raise NWCError(f"all relays failed ({last_err})")


def _request_via_relay(relay_url, req, secret, wallet, timeout) -> tuple[str, str]:
    sub = f"g2c_{secrets.token_hex(4)}"
    try:
        ws = websocket.create_connection(relay_url, timeout=timeout,
                                                  subprotocols=["nostr"])  # relays require the nostr subprotocol
    except Exception as e:
        raise NWCError(f"relay connect failed: {e}") from e
    ws.settimeout(timeout)
    deadline = time.time() + timeout
    try:
        ws.send(json.dumps(["EVENT", req]))
        req_filter = {"kinds": [23195], "authors": [wallet],
                      "#e": [req["id"]], "since": int(time.time()) - 60}
        ws.send(json.dumps(["REQ", sub, req_filter]))
        while time.time() < deadline:
            try:
                raw = ws.recv()
            except Exception:
                break
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, list) and msg[0] == "EVENT" and len(msg) > 2:
                ev = msg[2]
                if not any(t[0] == "e" and t[1] == req["id"]
                           for t in ev.get("tags", [])):
                    continue
                resp = _parse_response(ev, secret, wallet)
                if "error" in resp:
                    raise NWCError(f"wallet error: {resp['error']}")
                result = resp.get("result", {})
                invoice = result.get("invoice", "")
                if not invoice.startswith("ln"):
                    raise NWCError(f"wallet returned no invoice: {result}")
                return invoice, result.get("payment_hash", "")
            if isinstance(msg, list) and msg[0] == "NOTICE":
                raise NWCError(f"relay notice: {msg[1] if len(msg)>1 else msg}")
        raise NWCError("timed out waiting for wallet response")
    finally:
        try:
            ws.close()
        except Exception:
            pass
