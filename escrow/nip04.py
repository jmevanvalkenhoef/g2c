"""NIP-04 encryption (legacy scheme) — still used by Zeus's NWC service.

conversation key = raw ECDH x-coordinate (32B) used directly as AES-256 key
payload          = b64(AES-256-CBC(pkcs7(msg))) + "?iv=" + b64(16B iv)
"""
import base64
import secrets

from coincurve import PrivateKey, PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F


def _lift_x(xonly: bytes) -> bytes:
    x = int.from_bytes(xonly, "big")
    y_sq = (pow(x, 3, _P) + 7) % _P
    y = pow(y_sq, (_P + 1) // 4, _P)
    if y % 2:
        y = _P - y
    return b"\x02" + x.to_bytes(32, "big")


def conversation_key(secret: bytes, pub2: bytes) -> bytes:
    return PublicKey(_lift_x(pub2)).multiply(
        secret).format(compressed=True)[1:33]


def encrypt(msg: bytes, ck: bytes) -> str:
    iv = secrets.token_bytes(16)
    p = padding.PKCS7(128).padder()
    padded = p.update(msg) + p.finalize()
    ct = Cipher(algorithms.AES(ck), modes.CBC(iv)).encryptor().update(padded)
    return (base64.b64encode(ct).decode() + "?iv=" +
            base64.b64encode(iv).decode())


def decrypt(payload: str, ck: bytes) -> bytes:
    ct_b64, iv_b64 = payload.split("?iv=")
    ct = base64.b64decode(ct_b64)
    iv = base64.b64decode(iv_b64)
    dec = Cipher(algorithms.AES(ck), modes.CBC(iv)).decryptor()
    padded = dec.update(ct) + dec.finalize()
    unp = padding.PKCS7(128).unpadder()
    return unp.update(padded) + unp.finalize()


def is_nip04(payload: str) -> bool:
    return "?iv=" in payload
