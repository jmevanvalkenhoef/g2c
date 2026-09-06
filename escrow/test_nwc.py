#!/usr/bin/env python3
"""NWC client roundtrip test: mock relay that plays the seller's wallet.

Client (nwc.request_invoice) -> relay -> wallet logic (decrypt make_invoice,
create invoice, sign response) -> back through the relay to the client.
Exercises URI parsing, event building, NIP-44 both ways, BIP340 sign+verify,
and the relay protocol — everything except a real Zeus on the other end.
"""
import asyncio
import hashlib
import json
import secrets
import sys
import threading
import time

sys.path.insert(0, ".")
import websockets
import bip340
import nip44
import nwc

client_secret = secrets.token_bytes(32)
wallet_secret = secrets.token_bytes(32)
client_pub = bip340.pubkey(client_secret).hex()
wallet_pub = bip340.pubkey(wallet_secret).hex()
ck = nip44.get_conversation_key(client_secret, bytes.fromhex(wallet_pub))

received_requests = []


def wallet_handle(ev: dict) -> dict:
    """the 'Zeus' side: decrypt the request, return a signed response event"""
    payload = json.loads(nip44.decrypt(nip44.b64_decode(ev["content"]), ck))
    received_requests.append(payload)
    assert payload["method"] == "make_invoice", payload
    amount = payload["params"]["amount_msat"]
    fake_hash = hashlib.sha256(str(amount).encode()).hexdigest()
    result = {"result_type": "make_invoice",
              "result": {"type": "lightning_invoice",
                         "invoice": f"lntb{amount // 1000}m1FAKEINVOICE",
                         "payment_hash": fake_hash}}
    nonce = secrets.token_bytes(32)
    content = nip44.b64(nip44.encrypt(json.dumps(result, separators=(",", ":")).encode(),
                                      ck, nonce))
    tags = [["p", client_pub], ["e", ev["id"]]]
    created = int(time.time())
    eid = nwc._event_id(wallet_pub, created, 23195, tags, content)
    sig = bip340.sign(wallet_secret, bytes.fromhex(eid)).hex()
    return {"id": eid, "pubkey": wallet_pub, "created_at": created,
            "kind": 23195, "tags": tags, "content": content, "sig": sig}


pending = {}


async def relay_handler(ws):
    async for raw in ws:
        msg = json.loads(raw)
        if msg[0] == "EVENT":
            ev = msg[1]
            if ev["kind"] == 23194:
                pending[ev["pubkey"]] = wallet_handle(ev)
        elif msg[0] == "REQ":
            sub = msg[1]
            resp = pending.get(client_pub)
            if resp:
                await ws.send(json.dumps(["EVENT", sub, resp]))
                await ws.send(json.dumps(["EOSE", sub]))


def run_relay(port, ready):
    async def main():
        async with websockets.serve(relay_handler, "127.0.0.1", port):
            ready.set()
            await asyncio.Future()
    asyncio.run(main())


PORT = 8990
ready = threading.Event()
threading.Thread(target=run_relay, args=(PORT, ready), daemon=True).start()
ready.wait(5)

uri = (f"nostr+walletconnect://{wallet_pub}"
       f"?relay=ws%3A%2F%2F127.0.0.1%3A{PORT}&secret={client_secret.hex()}")
bolt11, pay_hash = nwc.request_invoice(uri, 49999900, "g2c sale payout")

assert bolt11.startswith("lntb"), bolt11
assert pay_hash == hashlib.sha256(b"49999900").hexdigest()
assert received_requests[0]["params"]["amount_msat"] == 49999900
print("NWC roundtrip PASS")
print("  invoice:", bolt11[:30], "…")
print("  wallet saw: make_invoice", received_requests[0]["params"]["amount_msat"], "msat")

# error paths
try:
    nwc.parse_uri("nostr+walletconnect://abcd")  # no relay/secret
    raise SystemExit("should have raised")
except nwc.NWCError:
    print("parse_uri validation PASS")
try:
    nwc.request_invoice(
        f"nostr+walletconnect://{wallet_pub}?relay=ws%3A%2F%2F127.0.0.1%3A1&secret={client_secret.hex()}",
        1000, "x", timeout=5)
    raise SystemExit("should have raised (dead relay)")
except nwc.NWCError as e:
    print("dead-relay timeout PASS:", str(e)[:60])
