"""Minimal JSON-RPC client for Core Lightning's lightning-rpc unix socket.

No lightning-cli binary needed — the socket speaks JSON-RPC 2.0 directly.
Each CLN node's lightning-dir is volume-mounted into this container
(e.g. /sockets/escrow), so the socket lives at <dir>/<network>/lightning-rpc.
"""
import json
import socket
from pathlib import Path
from typing import Any, Optional


class CLNError(RuntimeError):
    def __init__(self, error: dict):
        super().__init__(str(error.get("message", error)))
        self.error = error


class CLN:
    def __init__(self, lightning_dir: str, network: str = "regtest"):
        self.socket_path = Path(lightning_dir) / network / "lightning-rpc"
        self._id = 0

    def call(self, method: str, params: Optional[list | dict] = None,
             timeout: float = 60.0) -> Any:
        self._id += 1
        req: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method,
                               "params": params if params is not None else []}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(self.socket_path))
            s.sendall(json.dumps(req).encode() + b"\n\n")
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    raise CLNError({"message": f"connection closed by {method}"})
                buf += chunk
                try:
                    resp = json.loads(buf)
                except json.JSONDecodeError:
                    continue
                break
        if resp.get("error"):
            raise CLNError(resp["error"])
        return resp.get("result")
