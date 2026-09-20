"""Minimal MCP stdio client for kgdc-mcp: JSON-RPC over the server's stdin/stdout, one lock for all callers."""
from __future__ import annotations

import itertools
import json
import subprocess
import threading
from pathlib import Path

BIN = Path(__file__).parent.parent / "kgdc-mcp/target/release/kgdc-mcp"


class Mcp:
    def __init__(self, ontology: str | Path, shapes: str | Path, ns: str = "http://example.org/data/", max_subjects: int = 40):
        if not BIN.exists():
            raise FileNotFoundError(f"{BIN} missing — run `cargo build --release` in kgdc-mcp/")
        self.p = subprocess.Popen([str(BIN), "--ontology", str(ontology), "--shapes", str(shapes), "--ns", ns, "--max-subjects", str(max_subjects)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.ids = itertools.count(1)
        self.lock = threading.Lock()
        self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "kgdc", "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.tools = self.rpc("tools/list")["result"]["tools"]

    def _send(self, msg: dict) -> None:
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def rpc(self, method: str, params: dict | None = None) -> dict:
        with self.lock:
            i = next(self.ids)
            self._send({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}})
            while True:
                line = self.p.stdout.readline()
                if not line:
                    raise RuntimeError("kgdc-mcp exited")
                d = json.loads(line)
                if d.get("id") == i:
                    return d

    def call(self, name: str, **args):
        """Tool result as Python data (structured content if any, else text); errors as {'error': ...}."""
        r = self.rpc("tools/call", {"name": name, "arguments": args})
        if "error" in r:
            return {"error": r["error"].get("message", str(r["error"]))}
        res = r["result"]
        if res.get("isError"):
            return {"error": res["content"][0]["text"]}
        return res.get("structuredContent", res["content"][0]["text"] if res.get("content") else None)

    def openai_tools(self, names: list[str] | None = None) -> list[dict]:
        return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("inputSchema", {"type": "object"})}}
                for t in self.tools if names is None or t["name"] in names]

    def close(self) -> None:
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.p.kill()
