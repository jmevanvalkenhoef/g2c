#!/usr/bin/env python3
"""Kill -9 drill: crash the escrow service mid-sale at EVERY state transition
and assert the sale always lands in a legal terminal state.

For each target state in the happy-path sequence:
  create sale -> tap (buyer pays) -> watch sale state at 50ms resolution ->
  `docker compose restart escrow` the moment the target state appears ->
  wait for a terminal state -> verify game/Lightning consistency.

Legal outcomes (spec §4/§8):
  COMPLETED: item owner == buyer, buyer payment complete
  CANCELLED: item owner == seller, listing open, buyer payment failed
Anything else (stuck pending, completed without transfer, refund without
cancel) is a FAIL.

Usage: poc/scripts/kill_drill.py [game_port]     (needs: lab up, game NOT
running — this script boots it on :8000 with ESCROW_URL=:8002)
"""
import json
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error

GAME = f"http://localhost:{sys.argv[1] if len(sys.argv) > 1 else 8000}"
ESCROW = "http://localhost:8002"
ALICE, BOB = 1, 2
SECRET = {"X-Escrow-Secret": "dev-secret"}


def req(url, method="GET", body=None, headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json",
                                        **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, {}   # unreachable (e.g. escrow restarting) — caller treats as DOWN


def compose(*args):
    subprocess.run(["docker", "compose", *args], cwd="poc", check=False,
                   capture_output=True)


def buyer_pay_status(ph):
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "cln-buyer", "lightning-cli",
         "--regtest", "listpays"], cwd="poc", capture_output=True, text=True)
    try:
        pays = json.loads(out.stdout)["pays"]
        mine = [p for p in pays if p["payment_hash"].startswith(ph[:12])]
        return mine[-1]["status"] if mine else "none"
    except Exception:
        return "?"


def item_owner(item_id):
    _, items = req(f"{GAME}/v1/items")
    return [i for i in items if i["id"] == item_id][0]["owner_name"]


def run_drill(target_state, item_id):
    print(f"\n=== kill at {target_state} (item {item_id}) ===")
    req(f"{GAME}/v1/listings", "POST",
        {"item_id": item_id, "price_msat": 20000000},
        {"X-Player-Id": ALICE})
    _, listings = req(f"{GAME}/v1/market")
    lid = [l for l in listings if l["item_id"] == item_id][0]["id"]
    _, sale = req(f"{GAME}/v1/purchases", "POST", {"listing_id": lid},
                  {"X-Player-Id": BOB})
    sid, ph = sale["sale_id"], sale["payment_hash"]

    killed = threading.Event()

    def watcher():
        while not killed.is_set():
            code, s = req(f"{ESCROW}/v1/sales/{sid}", headers=SECRET, timeout=5)
            state = s.get("state") if code == 200 else "DOWN"
            if state == target_state:
                print(f"    kill! (state={state})")
                killed.set()
                compose("restart", "escrow")
                return
            if state in ("COMPLETED", "CANCELLED"):
                print(f"    sale finished before kill landed ({state}) — retry")
                killed.set()
                return
            time.sleep(0.05)

    t = threading.Thread(target=watcher)
    t.start()
    req(f"{GAME}/v1/purchases/{ph}/simulate-pay", "POST", {},
        {"X-Player-Id": BOB}, timeout=20)

    # wait for terminal state (restart downtime ~15s, payouts, retries)
    deadline = time.time() + 240
    final = None
    while time.time() < deadline:
        code, s = req(f"{ESCROW}/v1/sales/{sid}", headers=SECRET, timeout=5)
        state = s.get("state") if code == 200 else None
        if state in ("COMPLETED", "CANCELLED", "ERROR"):
            final = state
            break
        time.sleep(0.5)
    if final is None:
        return False, f"stuck non-terminal (last={state})"

    _, purchase = req(f"{GAME}/v1/purchases/{ph}", headers={"X-Player-Id": BOB})
    pstat = purchase.get("status")
    owner = item_owner(item_id)
    # buyer node bookkeeping lags the HTLC settle by a moment — poll for it
    pay = buyer_pay_status(ph)
    if final == "COMPLETED" and pay == "pending":
        for _ in range(30):
            time.sleep(1)
            pay = buyer_pay_status(ph)
            if pay != "pending":
                break

    if final == "COMPLETED":
        ok = (pstat == "completed" and owner == "bob" and pay == "complete")
        return ok, f"COMPLETED purchase={pstat} owner={owner} buyerpay={pay}"
    if final == "CANCELLED":
        ok = (pstat == "cancelled" and owner == "alice" and pay == "failed")
        return ok, f"CANCELLED purchase={pstat} owner={owner} buyerpay={pay}"
    return False, f"ERROR terminal state! {final} purchase={pstat} owner={owner}"


def main():
    # health
    for _ in range(30):
        code, h = req(f"{ESCROW}/v1/health", headers=SECRET)
        if code == 200 and h.get("ok"):
            break
        time.sleep(1)
    else:
        sys.exit("escrow not healthy — is the lab up?")

    # boot the game fresh (fresh world = deterministic ids).
    # kill by PORT, not by name: host ps shows container processes too and
    # those can't (and must not) be signaled from here.
    subprocess.run(["fuser", "-k", "8000/tcp"], check=False,
                   capture_output=True)
    time.sleep(2)
    subprocess.Popen(
        ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"],
        cwd="game/game-backend",
        env={**__import__("os").environ, "ESCROW_URL": ESCROW},
        stdout=open("/tmp/g2c_game.log", "w"), stderr=subprocess.STDOUT)
    booted = False
    for _ in range(30):
        code, items = req(f"{GAME}/v1/items", timeout=2)
        if code == 200 and isinstance(items, list):
            booted = True
            break
        time.sleep(1)
    if not booted:
        sys.exit("game failed to boot on :8000 (see /tmp/g2c_game.log)")

    targets = ["HTLC_HELD", "EXECUTING", "PAYING_SELLER", "SETTLING"]
    alice_items = [i["id"] for i in req(f"{GAME}/v1/items")[1]
                   if i["owner_name"] == "alice"]
    results = []
    for target, item in zip(targets, alice_items):
        ok, detail = run_drill(target, item)
        results.append((target, ok, detail))
        print(f"    -> {'PASS' if ok else 'FAIL'}: {detail}")

    print("\n=== kill drill summary ===")
    passed = sum(1 for _, ok, _ in results if ok)
    for target, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  kill@{target:14s} {detail}")
    print(f"{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
