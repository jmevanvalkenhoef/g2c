"""Mock of the third-party game API (spec §3.2: reserve / commit / release)
with fault injection. Faults apply to reserve and commit; release always
answers, so failure drills can unwind.

Fault control:
  - Global: FAULT_MODE env var (off | reject | timeout | server_error)
  - Per-request: X-Fault header with the same values (takes precedence)
"""
import os
import time
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)
LOCK = threading.Lock()

# idempotency_key -> {"status": "reserved"|"committed"|"released", "sale": {...}}
SALES = {}

FAULT_MODE = os.environ.get("FAULT_MODE", "off")


def fault():
    return request.headers.get("X-Fault", FAULT_MODE)


def reply(payload, code=200):
    # Flask timeout simulation: just sleep long enough for the client to give up
    f = fault()
    if f == "timeout":
        time.sleep(60)
        return jsonify({"status": "late"}), 200
    if f == "server_error":
        return jsonify({"status": "internal error"}), 500
    if f == "reject":
        return jsonify({"status": "rejected", "reason": "item_unavailable"}), 409
    return jsonify(payload), code


@app.post("/reserve_sale")
def reserve_sale():
    body = request.get_json(force=True)
    key = body.get("idempotency_key")
    if not key:
        return jsonify({"status": "error", "reason": "missing idempotency_key"}), 400
    with LOCK:
        existing = SALES.get(key)
        if existing and existing["status"] == "released":
            return reply({"status": "rejected", "reason": "sale_released"}, 409)
        if existing:
            return reply({"status": f"already_{existing['status']}"})
        SALES[key] = {"status": "reserved", "sale": body}
        app.logger.info("RESERVED %s item=%s buyer=%s seller=%s",
                        key, body.get("item_id"), body.get("buyer_id"), body.get("seller_id"))
    return reply({"status": "reserved"})


@app.post("/commit_sale")
def commit_sale():
    key = request.get_json(force=True).get("idempotency_key")
    with LOCK:
        rec = SALES.get(key)
        if rec and rec["status"] == "committed":
            return reply({"status": "already_committed"})
        if not rec or rec["status"] != "reserved":
            return jsonify({"status": "rejected", "reason": "not_reserved"}), 409
        rec["status"] = "committed"
        app.logger.info("COMMITTED %s", key)
    return reply({"status": "committed"})


@app.get("/sale_status")
def sale_status():
    key = request.args.get("key")
    with LOCK:
        rec = SALES.get(key)
    if not rec:
        return jsonify({"status": "not_found"}), 200
    return jsonify({"status": rec["status"]}), 200


@app.post("/release_sale")
def release_sale():
    body = request.get_json(force=True)
    key = body.get("idempotency_key")
    with LOCK:
        rec = SALES.get(key)
        if rec and rec["status"] == "committed":
            return jsonify({"status": "rejected", "reason": "sale_committed"}), 409
        if rec and rec["status"] == "released":
            return jsonify({"status": "already_released"})
        if not rec:   # tombstone: a late reserve with this key must not land
            SALES[key] = {"status": "released", "sale": None}
            return jsonify({"status": "not_found_ignored"})
        rec["status"] = "released"
        app.logger.info("RELEASED %s reason=%s", key, body.get("reason"))
    return jsonify({"status": "released"}), 200


@app.get("/debug/sales")
def debug_sales():
    with LOCK:
        return jsonify(SALES)


@app.post("/debug/reset")
def debug_reset():
    with LOCK:
        SALES.clear()
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
