"""NIP-44 v2 encryption (nostr-protocol/nips 44.md).

Byte-exact implementation of the reference (paulmillr/nip44 javascript):
  conversation_key = HKDF-EXTRACT(sha256, ikm=ECDH_x32, salt='nip44-v2')
  message_keys     = HKDF-EXPAND(sha256, prk=ck, info=nonce32, 76)
                     -> chacha_key32 | chacha_nonce12 | hmac_key32
  pad              = u16be(len) || plaintext || zeros(calc_padded_len(len)-len)
  payload          = 0x02 || nonce(32) || chacha20(pad) || hmac(hmac_key, nonce||ct)
Validated against the official vectors in test_nip44.py.
"""
import base64
import hashlib
import hmac
import math

from coincurve import PrivateKey, PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

VERSION = b"\x02"
SALT = b"nip44-v2"

_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F


def _lift_x(xonly: bytes) -> bytes:
    """x-only pubkey -> 33-byte compressed, even y (NIP-44 convention)."""
    assert len(xonly) == 32
    x = int.from_bytes(xonly, "big")
    y_sq = (pow(x, 3, _P) + 7) % _P
    y = pow(y_sq, (_P + 1) // 4, _P)
    assert (y * y) % _P == y_sq, "x not on curve"
    if y % 2:
        y = _P - y
    return b"\x02" + x.to_bytes(32, "big")


def get_conversation_key(sec1: bytes, pub2: bytes) -> bytes:
    """HKDF-EXTRACT: hmac_sha256(key=salt, msg=ECDH x-coordinate)."""
    shared_x = PublicKey(_lift_x(pub2)).multiply(
        sec1).format(compressed=True)[1:33]
    return hmac.new(SALT, shared_x, hashlib.sha256).digest()


def get_message_keys(conversation_key: bytes, nonce: bytes) -> tuple[bytes, bytes, bytes]:
    """HKDF-EXPAND(prk=conversation_key, info=nonce, 76)."""
    okm = b""
    t = b""
    i = 1
    while len(okm) < 76:
        t = hmac.new(conversation_key, t + nonce + bytes([i]), hashlib.sha256).digest()
        okm += t
        i += 1
    return okm[:32], okm[32:44], okm[44:76]


def calc_padded_len(unpadded: int) -> int:
    if unpadded <= 32:
        return 32
    next_power = 1 << (math.floor(math.log2(unpadded - 1)) + 1)
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * (math.floor((unpadded - 1) / chunk) + 1)


def _chacha20(key: bytes, nonce12: bytes, data: bytes) -> bytes:
    # IETF ChaCha20: 16-byte IV = 32-bit counter (0) || 12-byte nonce
    enc = Cipher(algorithms.ChaCha20(key, b"\x00\x00\x00\x00" + nonce12),
                 mode=None).encryptor()
    return enc.update(data) + enc.finalize()


def _pad(plaintext: bytes) -> bytes:
    n = len(plaintext)
    assert 0 < n <= 0xFFFF
    return (n.to_bytes(2, "big") + plaintext +
            b"\x00" * (calc_padded_len(n) - n))


def encrypt(plaintext: bytes, conversation_key: bytes, nonce: bytes) -> bytes:
    chacha_key, chacha_nonce, hmac_key = get_message_keys(conversation_key, nonce)
    ct = _chacha20(chacha_key, chacha_nonce, _pad(plaintext))
    mac = hmac.new(hmac_key, nonce + ct, hashlib.sha256).digest()
    return VERSION + nonce + ct + mac


def decrypt(payload: bytes, conversation_key: bytes) -> bytes:
    assert payload[:1] == VERSION, "unsupported nip44 version"
    nonce, ct, mac = payload[1:33], payload[33:-32], payload[-32:]
    chacha_key, chacha_nonce, hmac_key = get_message_keys(conversation_key, nonce)
    expect = hmac.new(hmac_key, nonce + ct, hashlib.sha256).digest()
    assert hmac.compare_digest(mac, expect), "bad mac"
    padded = _chacha20(chacha_key, chacha_nonce, ct)
    n = int.from_bytes(padded[:2], "big")
    assert 0 < n <= 0xFFFF and len(padded) == 2 + calc_padded_len(n), "invalid padding"
    return padded[2:2 + n]


def b64(data: bytes) -> str:
    # NIP-44 payloads use STANDARD base64 (with padding), not base64url
    return base64.b64encode(data).decode()


def b64_decode(s: str) -> bytes:
    return base64.b64decode(s)
