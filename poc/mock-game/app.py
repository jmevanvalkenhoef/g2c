"""Mock of the third-party game API (spec §3.2) with fault injection.

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

# idempotency_key -> {"status": "done"|"rolled_back", "sale": {...}}
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


@app.post("/execute_sale")
def execute_sale():
    body = request.get_json(force=True)
    key = body.get("idempotency_key")
    if not key:
        return jsonify({"status": "error", "reason": "missing idempotency_key"}), 400

    with LOCK:
        existing = SALES.get(key)
        if existing and existing["status"] == "done":
            return reply({"status": "already_done"})
        if existing and existing["status"] == "rolled_back":
            # rolled back after done → allow re-execution
            pass

        SALES[key] = {"status": "done", "sale": body}
        app.logger.info("EXECUTED %s item=%s buyer=%s seller=%s",
                        key, body.get("item_id"), body.get("buyer_id"), body.get("seller_id"))
    return reply({"status": "done"})


@app.get("/sale_status")
def sale_status():
    key = request.args.get("key")
    with LOCK:
        rec = SALES.get(key)
    if not rec:
        return jsonify({"status": "not_found"}), 200
    return jsonify({"status": rec["status"]}), 200


@app.post("/rollback_sale")
def rollback_sale():
    body = request.get_json(force=True)
    key = body.get("idempotency_key")
    with LOCK:
        rec = SALES.get(key)
        if rec and rec["status"] == "rolled_back":
            return jsonify({"status": "already_rolled_back"})
        if rec:
            rec["status"] = "rolled_back"
            app.logger.info("ROLLED BACK %s reason=%s", key, body.get("reason"))
    return jsonify({"status": "rolled_back"}), 200


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
