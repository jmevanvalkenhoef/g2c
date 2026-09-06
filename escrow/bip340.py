"""BIP-340 Schnorr signing (for Nostr event signing) on coincurve.

Only sign() — relays and wallets verify. Validated against the official
BIP-340 test vectors in test_bip340.py.
"""
import hashlib

from coincurve import PrivateKey

_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def _tagged(tag: str, msg: bytes) -> bytes:
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + msg).digest()


def pubkey(secret: bytes) -> bytes:
    """x-only public key for a secret."""
    return PrivateKey(secret=secret).public_key.format(compressed=True)[1:]


def sign(secret: bytes, msg: bytes, aux: bytes = b"\x00" * 32) -> bytes:
    assert len(msg) == 32
    d = int.from_bytes(secret, "big")
    pub = pubkey(secret)
    if PrivateKey(secret=secret).public_key.format(compressed=True)[0] == 0x03:
        d = _N - d          # BIP340: work with d' whose pubkey has even Y
    t = bytes(a ^ b for a, b in zip(d.to_bytes(32, "big"),
                                    _tagged("BIP0340/aux", aux)))
    k0 = int.from_bytes(_tagged("BIP0340/nonce", t + pub + msg), "big") % _N
    R = PrivateKey(secret=k0.to_bytes(32, "big")).public_key.format(compressed=True)
    k = k0 if R[0] == 0x02 else _N - k0
    e = int.from_bytes(_tagged("BIP0340/challenge", R[1:] + pub + msg), "big") % _N
    s = (k + e * d) % _N
    return R[1:] + s.to_bytes(32, "big")
