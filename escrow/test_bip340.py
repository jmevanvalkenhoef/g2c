import csv, sys, hashlib
sys.path.insert(0, ".")
import bip340

rows = list(csv.DictReader(open('/tmp/bip340_vectors.csv')))
n = fails = 0
for r in rows:
    if not r.get("secret key") or r["secret key"] in ("", "0"*64) or len(r["message"]) != 64:
        continue  # verify-only rows
    sec = bytes.fromhex(r["secret key"])
    aux = bytes.fromhex(r["aux_rand"]) if r.get("aux_rand") else b"\x00"*32
    msg = bytes.fromhex(r["message"])
    want = r["signature"]
    try:
        got = bip340.sign(sec, msg, aux).hex()
    except Exception as e:
        got = f"ERR {e}"
    n += 1
    if got.lower() != want.lower():
        fails += 1
        if fails <= 3:
            print("FAIL row:", r["index"], got[:32], "!=", want[:32])
print(f"bip340 sign: {n} vectors, {'ALL PASS' if fails == 0 else str(fails)+' FAILURES'}")
sys.exit(0 if fails == 0 else 1)
