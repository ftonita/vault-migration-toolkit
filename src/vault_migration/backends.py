"""Vault KV v2 backends.

* VaultHTTP: the real client (stdlib only, TLS verified, retries on 5xx).
  A missing mount is detected from the read itself (404 "no handler for route"), so the migration
  token needs no access to sys/mounts unless mounts are created.
* MemoryVault / FileFakeVault: in-process fakes for tests and offline demos. Never for real secrets.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class VaultError(RuntimeError):
    pass


class CasMismatch(VaultError):
    """check-and-set failed: someone else wrote the secret first."""


class MountMissing(VaultError):
    """No secrets engine is mounted at the requested mount."""


class MetadataError(VaultError):
    """The value was written, but its custom metadata was not."""


@dataclass(frozen=True)
class Stored:
    data: dict[str, str]
    version: int
    custom_metadata: dict[str, str] = field(default_factory=dict)


class Backend(Protocol):
    def read(self, mount: str, path: str) -> Stored | None: ...

    def write(
        self, mount: str, path: str, data: dict[str, str], cas: int, metadata: dict[str, str]
    ) -> int: ...

    def write_metadata(self, mount: str, path: str, metadata: dict[str, str]) -> None: ...

    def mount_exists(self, mount: str) -> bool: ...

    def create_mount(self, mount: str) -> None: ...


class MemoryVault:
    def __init__(self) -> None:
        self.mounts: set[str] = set()
        self.store: dict[tuple[str, str], Stored] = {}

    def read(self, mount: str, path: str) -> Stored | None:
        if mount not in self.mounts:
            raise MountMissing(f"no secret engine mounted at {mount}/")
        return self.store.get((mount, path))

    def write(self, mount, path, data, cas, metadata):  # noqa: ANN001
        if mount not in self.mounts:
            raise MountMissing(f"no secret engine mounted at {mount}/")
        current = self.store.get((mount, path))
        if cas != (current.version if current else 0):
            raise CasMismatch(f"{mount}/{path}: check-and-set mismatch")
        version = (current.version if current else 0) + 1
        self.store[(mount, path)] = Stored(dict(data), version, dict(metadata))
        return version

    def write_metadata(self, mount: str, path: str, metadata: dict[str, str]) -> None:
        current = self.store[(mount, path)]
        self.store[(mount, path)] = Stored(current.data, current.version, dict(metadata))

    def mount_exists(self, mount: str) -> bool:
        return mount in self.mounts

    def create_mount(self, mount: str) -> None:
        self.mounts.add(mount)


class FileFakeVault(MemoryVault):
    """MemoryVault persisted to a JSON file so CLI runs can share state in demos."""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.mounts = set(raw["mounts"])
            for key, val in raw["store"].items():
                mount, _, p = key.partition("::")
                self.store[(mount, p)] = Stored(val["data"], val["version"], val["metadata"])

    def _flush(self) -> None:
        raw = {
            "WARNING": "fake vault for demos; plaintext on purpose",
            "mounts": sorted(self.mounts),
            "store": {
                f"{m}::{p}": {"data": s.data, "version": s.version, "metadata": s.custom_metadata}
                for (m, p), s in self.store.items()
            },
        }
        self.path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    def write(self, mount, path, data, cas, metadata):  # noqa: ANN001
        version = super().write(mount, path, data, cas, metadata)
        self._flush()
        return version

    def write_metadata(self, mount: str, path: str, metadata: dict[str, str]) -> None:
        super().write_metadata(mount, path, metadata)
        self._flush()

    def create_mount(self, mount: str) -> None:
        super().create_mount(mount)
        self._flush()


class VaultHTTP:
    def __init__(
        self, addr: str, token: str, *, timeout: float = 10.0, retries: int = 3, namespace: str | None = None
    ) -> None:
        if not addr.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise VaultError("refusing to talk to Vault over plain http (except localhost)")
        self.addr, self.token, self.timeout, self.retries, self.namespace = (
            addr.rstrip("/"),
            token,
            timeout,
            retries,
            namespace,
        )

    @classmethod
    def from_env(cls) -> VaultHTTP:
        addr, token = os.environ.get("VAULT_ADDR"), os.environ.get("VAULT_TOKEN")
        if not addr or not token:
            raise VaultError("VAULT_ADDR and VAULT_TOKEN must be set")
        return cls(addr, token, namespace=os.environ.get("VAULT_NAMESPACE"))

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        headers = {"X-Vault-Token": self.token, "Content-Type": "application/json"}
        if self.namespace:
            headers["X-Vault-Namespace"] = self.namespace
        payload = json.dumps(body).encode() if body is not None else None
        for attempt in range(self.retries):
            req = urllib.request.Request(
                f"{self.addr}/v1/{path}", data=payload, method=method, headers=headers
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    return resp.status, (json.loads(raw) if raw else None)
            except urllib.error.HTTPError as err:
                raw = err.read()
                if err.code >= 500 and attempt < self.retries - 1:
                    time.sleep(0.2 * 2**attempt)
                    continue
                return err.code, (json.loads(raw) if raw else None)
            except urllib.error.URLError as err:
                if attempt < self.retries - 1:
                    time.sleep(0.2 * 2**attempt)
                    continue
                raise VaultError(f"cannot reach Vault: {err.reason}") from err
        raise VaultError("unreachable")  # pragma: no cover

    def read(self, mount: str, path: str) -> Stored | None:
        status, body = self._request("GET", f"{mount}/data/{path}")
        if status == 404:
            if "no handler for route" in json.dumps(body):
                raise MountMissing(f"no secret engine mounted at {mount}/")
            meta = ((body or {}).get("data") or {}).get("metadata")
            if not meta:
                return None
            # soft-deleted: no data, but the version still counts for check-and-set
            return Stored({}, meta["version"], meta.get("custom_metadata") or {})
        if status != 200:
            raise VaultError(f"read {mount}/{path}: HTTP {status}")
        d = body["data"]
        return Stored(d["data"] or {}, d["metadata"]["version"], d["metadata"].get("custom_metadata") or {})

    def write(self, mount, path, data, cas, metadata):  # noqa: ANN001
        status, body = self._request("POST", f"{mount}/data/{path}", {"options": {"cas": cas}, "data": data})
        if status == 400 and "check-and-set" in json.dumps(body):
            raise CasMismatch(f"{mount}/{path}: check-and-set mismatch")
        if status == 404 and "no handler for route" in json.dumps(body):
            raise MountMissing(f"no secret engine mounted at {mount}/")
        if status not in (200, 204):
            raise VaultError(f"write {mount}/{path}: HTTP {status}")
        version = body["data"]["version"]
        if metadata:
            try:
                self.write_metadata(mount, path, metadata)
            except VaultError as exc:
                raise MetadataError(f"value written as version {version}, but {exc}") from exc
        return version

    def write_metadata(self, mount: str, path: str, metadata: dict[str, str]) -> None:
        status, _ = self._request("POST", f"{mount}/metadata/{path}", {"custom_metadata": metadata})
        if status not in (200, 204):
            raise VaultError(f"metadata {mount}/{path}: HTTP {status}")

    def mount_exists(self, mount: str) -> bool:
        status, body = self._request("GET", "sys/mounts")
        if status != 200:
            raise VaultError(f"list mounts: HTTP {status}")
        return f"{mount}/" in body.get("data", body)

    def create_mount(self, mount: str) -> None:
        status, body = self._request(
            "POST", f"sys/mounts/{mount}", {"type": "kv", "options": {"version": "2"}}
        )
        if status == 400 and "already in use" in json.dumps(body):
            return  # created meanwhile by someone else
        if status not in (200, 204):
            raise VaultError(f"create mount {mount}: HTTP {status}")
