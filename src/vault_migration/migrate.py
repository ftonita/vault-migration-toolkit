"""Apply a plan to Vault (create-only, idempotent, resumable) and verify the result."""

from __future__ import annotations

from dataclasses import dataclass, field

from .backends import Backend, MetadataError, MountMissing, Stored, VaultError
from .fingerprint import fingerprint
from .ledger import Entry, Ledger
from .models import LegacySecret, PlanItem


@dataclass
class Result:
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    would_write: list[str] = field(default_factory=list)
    metadata_repaired: list[str] = field(default_factory=list)  # identical value, custom metadata (re)written
    errors: dict[str, str] = field(default_factory=dict)  # secret id -> reason (never a value)
    missing_mounts: dict[str, list[str]] = field(default_factory=dict)  # mount -> ids it blocks
    mount_errors: dict[str, str] = field(default_factory=dict)  # mount -> why it could not be created
    mounts_to_create: set[str] = field(default_factory=set)  # created (or would be, with a dry run)

    @property
    def ok(self) -> bool:
        return not self.conflicts and not self.failed


def _metadata(s: LegacySecret) -> dict[str, str]:
    md = {"legacy_id": s.id, "kind": s.kind, "migrated_by": "vault-migration-toolkit"}
    if s.last_rotated:
        md["last_rotated"] = s.last_rotated.isoformat()
    return md


def _read(backend: Backend, mount: str, path: str, absent: set[str]) -> tuple[Stored | None, bool]:
    """Read a target; (stored, mount_exists). Mounts known to be absent are not asked again."""
    if mount in absent:
        return None, False
    try:
        return backend.read(mount, path), True
    except MountMissing:
        absent.add(mount)
        return None, False


def apply(
    items: list[PlanItem],
    secrets: dict[str, LegacySecret],
    backend: Backend,
    ledger: Ledger,
    key: bytes,
    *,
    execute: bool,
    overwrite: bool = False,
    create_mounts: bool = False,
) -> Result:
    res = Result()
    absent: set[str] = set()
    for item in items:
        s, t = secrets[item.secret_id], item.target
        fp = fingerprint(key, s.value)
        md = _metadata(s)
        try:
            current, mount_ok = _read(backend, t.mount, t.path, absent)
            if current is not None and fingerprint(key, current.data.get("value", "")) == fp:
                if any(current.custom_metadata.get(k) != v for k, v in md.items()):
                    res.metadata_repaired.append(s.id)
                    if execute:
                        backend.write_metadata(t.mount, t.path, {**current.custom_metadata, **md})
                        ledger.append(Entry(s.id, str(t), "metadata_written", fp[:8]))
                    continue
                res.skipped.append(s.id)
                if execute:
                    ledger.append(Entry(s.id, str(t), "skipped_identical", fp[:8]))
                continue
            if current is not None and not overwrite:
                res.conflicts.append(s.id)
                if execute:
                    ledger.append(
                        Entry(s.id, str(t), "conflict", fp[:8], "target exists with a different value")
                    )
                continue
            if not mount_ok:
                if create_mounts and execute and t.mount not in res.mount_errors:
                    try:
                        backend.create_mount(t.mount)
                        absent.discard(t.mount)
                    except VaultError as exc:
                        res.mount_errors[t.mount] = str(exc)
                if not create_mounts or t.mount in res.mount_errors:
                    res.missing_mounts.setdefault(t.mount, []).append(s.id)
                    reason = res.mount_errors.get(t.mount, "use --create-mounts")
                    raise VaultError(f"mount {t.mount}/ does not exist ({reason})")
                res.mounts_to_create.add(t.mount)
            if not execute:
                res.would_write.append(s.id)
                continue
            backend.write(t.mount, t.path, {"value": s.value.reveal()}, current.version if current else 0, md)
            res.written.append(s.id)
            ledger.append(Entry(s.id, str(t), "written", fp[:8]))
        except MetadataError as exc:
            # the value is in Vault; a re-run sees it as identical and repairs the metadata
            res.written.append(s.id)
            res.failed.append(s.id)
            res.errors[s.id] = f"{exc}; re-run to repair the metadata"
            ledger.append(Entry(s.id, str(t), "written", fp[:8]))
            ledger.append(Entry(s.id, str(t), "failed", fp[:8], res.errors[s.id]))
        except VaultError as exc:
            res.failed.append(s.id)
            res.errors[s.id] = str(exc)
            if execute:
                ledger.append(Entry(s.id, str(t), "failed", fp[:8], str(exc)))
    return res


@dataclass
class Verification:
    verified: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatched and not self.missing


def verify(
    items: list[PlanItem], secrets: dict[str, LegacySecret], backend: Backend, ledger: Ledger, key: bytes
) -> Verification:
    out = Verification()
    absent: set[str] = set()
    for item in items:
        s, t = secrets[item.secret_id], item.target
        fp = fingerprint(key, s.value)
        stored, _ = _read(backend, t.mount, t.path, absent)
        if stored is None or "value" not in stored.data:  # absent, or soft-deleted
            out.missing.append(s.id)
            ledger.append(Entry(s.id, str(t), "missing", fp[:8]))
        elif fingerprint(key, stored.data.get("value", "")) != fp:
            out.mismatched.append(s.id)
            ledger.append(Entry(s.id, str(t), "mismatch", fp[:8]))
        else:
            out.verified.append(s.id)
            ledger.append(Entry(s.id, str(t), "verified", fp[:8]))
    return out
