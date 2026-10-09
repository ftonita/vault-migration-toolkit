"""A tiny in-process Vault KV v2 look-alike so VaultHTTP is exercised over real HTTP."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class StubState:
    def __init__(self, token: str = "t-123") -> None:
        self.token = token
        self.mounts: set[str] = set()
        self.data: dict[str, dict] = {}  # "mount/path" -> {"versions": [..], "custom": {}}
        self.fail_next_with: list[int] = []  # status codes to return before behaving normally
        self.requests: list[tuple[str, str]] = []
        self.forbid_sys = False  # token without any sys/* capability
        self.soft_deleted: set[str] = set()  # "mount/path" deleted with `vault kv delete`
        self.fail_metadata_with: int | None = None


def make_server(state: StubState) -> tuple[ThreadingHTTPServer, str]:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: ANN002
            pass

        def _send(self, code: int, body=None):
            raw = json.dumps(body).encode() if body is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _handle(self, method: str):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            state.requests.append((method, self.path))
            if state.fail_next_with:
                return self._send(state.fail_next_with.pop(0), {"errors": ["boom"]})
            if self.headers.get("X-Vault-Token") != state.token:
                return self._send(403, {"errors": ["permission denied"]})
            path = self.path[len("/v1/") :]
            if state.forbid_sys and path.startswith("sys/"):
                return self._send(403, {"errors": ["permission denied"]})
            if path == "sys/mounts" and method == "GET":
                return self._send(200, {"data": {f"{m}/": {"type": "kv"} for m in state.mounts}})
            if path.startswith("sys/mounts/") and method == "POST":
                if path[len("sys/mounts/") :] in state.mounts:
                    return self._send(400, {"errors": [f"path is already in use at {path[11:]}/"]})
                state.mounts.add(path[len("sys/mounts/") :])
                return self._send(204)
            mount, kind, _, rest = (
                path.partition("/")[0],
                path.split("/")[1],
                None,
                "/".join(path.split("/")[2:]),
            )
            key = f"{mount}/{rest}"
            if mount not in state.mounts:  # what real Vault answers for an unknown mount
                return self._send(404, {"errors": [f'no handler for route "{path}". route entry not found.']})
            if kind == "data" and method == "GET":
                entry = state.data.get(key)
                if not entry:
                    return self._send(404, {"errors": []})
                v = len(entry["versions"])
                if key in state.soft_deleted:
                    return self._send(404, {"data": {"data": None, "metadata": {"version": v}}})
                return self._send(
                    200,
                    {
                        "data": {
                            "data": entry["versions"][-1],
                            "metadata": {"version": v, "custom_metadata": entry["custom"]},
                        }
                    },
                )
            if kind == "data" and method == "POST":
                entry = state.data.setdefault(key, {"versions": [], "custom": {}})
                cas = body.get("options", {}).get("cas")
                if cas is not None and cas != len(entry["versions"]):
                    if not entry["versions"]:
                        state.data.pop(key)
                    return self._send(
                        400, {"errors": ["check-and-set parameter did not match the current version"]}
                    )
                entry["versions"].append(body["data"])
                return self._send(200, {"data": {"version": len(entry["versions"])}})
            if kind == "metadata" and method == "POST" and state.fail_metadata_with:
                return self._send(state.fail_metadata_with, {"errors": ["permission denied"]})
            if kind == "metadata" and method == "POST":
                state.data[key]["custom"] = body["custom_metadata"]
                return self._send(204)
            return self._send(404, {"errors": ["unsupported"]})

        def do_GET(self):  # noqa: N802
            self._handle("GET")

        def do_POST(self):  # noqa: N802
            self._handle("POST")

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
