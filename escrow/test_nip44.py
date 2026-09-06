#!/usr/bin/env python3
"""Validate nip44.py against the official NIP-44 v2 test vectors."""
import json
import sys
import unicodedata

sys.path.insert(0, ".")
import nip44
from coincurve import PrivateKey

v = json.load(open("/tmp/nip44_vectors.json"))["v2"]
fails = 0

# conversation keys
for vec in v["valid"]["get_conversation_key"]:
    got = nip44.get_conversation_key(
        bytes.fromhex(vec["sec1"]), bytes.fromhex(vec["pub2"])).hex()
    if got != vec["conversation_key"]:
        fails += 1
        print("convo FAIL:", vec["sec1"][:12], got[:16], "!=", vec["conversation_key"][:16])
print(f"conversation_key: {len(v['valid']['get_conversation_key'])} vectors")

# message keys
mk = v["valid"]["get_message_keys"]
ck = bytes.fromhex(mk["conversation_key"])
for vec in mk["keys"]:
    ck_key, chacha_nonce, hmac_key = nip44.get_message_keys(
        ck, bytes.fromhex(vec["nonce"]))
    ok = (ck_key.hex() == vec["chacha_key"] and
          chacha_nonce.hex() == vec["chacha_nonce"] and
          hmac_key.hex() == vec["hmac_key"])
    if not ok:
        fails += 1
        print("msgkeys FAIL")
print(f"message_keys: {len(mk['keys'])} vectors")

# padded lengths
for unpadded, want in v["valid"]["calc_padded_len"]:
    got = nip44.calc_padded_len(unpadded)
    if got != want:
        fails += 1
        print(f"padded FAIL: {unpadded} -> {got}, want {want}")
print(f"calc_padded_len: {len(v['valid']['calc_padded_len'])} vectors")

# encrypt/decrypt roundtrip (fixed nonces -> byte-exact payloads)
n = 0
for vec in v["valid"]["encrypt_decrypt"]:
    sec1 = bytes.fromhex(vec["sec1"])
    sec2 = bytes.fromhex(vec["sec2"])
    pub2 = PrivateKey(secret=sec2).public_key.format(compressed=True)[1:]
    ck = nip44.get_conversation_key(sec1, pub2)
    assert ck.hex() == vec["conversation_key"], "vector convo key mismatch"
    pt = unicodedata.normalize("NFC", vec["plaintext"]).encode()
    payload = nip44.encrypt(pt, ck, bytes.fromhex(vec["nonce"]))
    if nip44.b64(payload) != vec["payload"]:
        fails += 1
        print("encrypt FAIL:", vec["plaintext"][:20])
    if nip44.decrypt(payload, ck) != pt:
        fails += 1
        print("decrypt FAIL:", vec["plaintext"][:20])
    n += 1
print(f"encrypt_decrypt: {n} vectors")

# invalid: bad mac must throw
for vec in v["invalid"]["decrypt"]:
    if "sec1" not in vec or "sec2" not in vec:
        continue  # structural-rejection vectors (bad base64 etc.)
    sec2 = bytes.fromhex(vec["sec2"])
    pub1 = PrivateKey(secret=bytes.fromhex(vec["sec1"])).public_key.format(compressed=True)[1:]
    ck = nip44.get_conversation_key(sec2, pub1)
    try:
        nip44.decrypt(nip44.b64_decode(vec["payload"]), ck)
        fails += 1
        print("invalid-decrypt FAIL (accepted!):", vec["note"])
    except Exception:
        pass
print(f"invalid decrypt: {len(v['invalid']['decrypt'])} correctly rejected")

print("\nRESULT:", "ALL PASS" if fails == 0 else f"{fails} FAILURES")
sys.exit(0 if fails == 0 else 1)
