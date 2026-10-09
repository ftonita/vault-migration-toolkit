from __future__ import annotations

import pytest

from vault_migration.backends import MemoryVault, VaultError
from vault_migration.migrate import apply, verify
from vault_migration.models import PlanItem, Target
from vault_migration.rules import plan


@pytest.fixture
def prepared(demo_secrets, rules):
    items, _ = plan(demo_secrets, rules)
    return items, {s.id: s for s in demo_secrets}


def run(prepared, vault, ledger, key, **kw):
    items, by_id = prepared
    return apply(items, by_id, vault, ledger, key, **{"execute": True, "create_mounts": True, **kw})


def test_dry_run_changes_nothing_and_writes_no_ledger(prepared, memory_vault, ledger, state_key):
    res = run(prepared, memory_vault, ledger, state_key, execute=False)
    assert len(res.would_write) == 36 and not res.written
    assert memory_vault.store == {} and memory_vault.mounts == set() and ledger.entries() == []


def test_execute_writes_values_and_metadata(prepared, memory_vault, ledger, state_key, demo_secrets):
    res = run(prepared, memory_vault, ledger, state_key)
    assert len(res.written) == 36 and res.ok
    stored = memory_vault.read("kv-payments", "dev/orders/db_password")
    src = next(s for s in demo_secrets if s.id == "S001")
    assert stored.data == {"value": src.value.reveal()}
    assert stored.custom_metadata["legacy_id"] == "S001" and stored.version == 1


def test_second_run_is_a_no_op(prepared, memory_vault, ledger, state_key):
    run(prepared, memory_vault, ledger, state_key)
    again = run(prepared, memory_vault, ledger, state_key)
    assert len(again.skipped) == 36 and not again.written
    assert all(s.version == 1 for s in memory_vault.store.values())


def test_existing_different_value_is_a_conflict_not_overwritten(prepared, memory_vault, ledger, state_key):
    run(prepared, memory_vault, ledger, state_key)
    memory_vault.store[("kv-payments", "dev/orders/db_password")] = memory_vault.store[
        ("kv-payments", "dev/orders/db_password")
    ].__class__({"value": "edited-in-vault"}, 1, {})
    res = run(prepared, memory_vault, ledger, state_key)
    assert res.conflicts == ["S001"] and not res.ok
    assert memory_vault.read("kv-payments", "dev/orders/db_password").data["value"] == "edited-in-vault"


def test_overwrite_uses_cas_and_bumps_version(prepared, memory_vault, ledger, state_key, demo_secrets):
    run(prepared, memory_vault, ledger, state_key)
    memory_vault.store[("kv-payments", "dev/orders/db_password")] = memory_vault.store[
        ("kv-payments", "dev/orders/db_password")
    ].__class__({"value": "edited"}, 1, {})
    res = run(prepared, memory_vault, ledger, state_key, overwrite=True)
    assert res.written == ["S001"]
    assert memory_vault.read("kv-payments", "dev/orders/db_password").version == 2


def test_missing_mount_fails_cleanly_without_create_mounts(prepared, memory_vault, ledger, state_key):
    res = run(prepared, memory_vault, ledger, state_key, create_mounts=False)
    assert len(res.failed) == 36 and not res.written
    assert "create-mounts" in ledger.entries()[0].detail


def test_partial_failure_is_resumable(prepared, ledger, state_key):
    class Flaky(MemoryVault):
        broken = True

        def write(self, mount, path, data, cas, metadata):  # noqa: ANN001
            if self.broken and path.endswith("prod/orders/db_password"):
                raise VaultError("HTTP 503")
            return super().write(mount, path, data, cas, metadata)

    v = Flaky()
    first = run(prepared, v, ledger, state_key)
    assert first.failed == ["S005"] and len(first.written) == 35
    v.broken = False
    second = run(prepared, v, ledger, state_key)
    assert second.written == ["S005"] and len(second.skipped) == 35 and second.ok


def test_ledger_and_results_contain_no_secret_values(prepared, memory_vault, ledger, state_key, demo_secrets):
    run(prepared, memory_vault, ledger, state_key)
    text = ledger.file.read_text(encoding="utf-8")
    assert all(s.value.reveal() not in text for s in demo_secrets)
    assert all(len(e.fingerprint8) == 8 for e in ledger.entries())


def test_verify_detects_match_mismatch_and_missing(prepared, memory_vault, ledger, state_key):
    items, by_id = prepared
    run(prepared, memory_vault, ledger, state_key)
    ok = verify(items, by_id, memory_vault, ledger, state_key)
    assert ok.ok and len(ok.verified) == 36
    k = ("kv-scoring", "prod/scorer/api_key")
    memory_vault.store[k] = memory_vault.store[k].__class__({"value": "x"}, 1, {})
    del memory_vault.store[("kv-scoring", "dev/scorer/api_key")]
    bad = verify(items, by_id, memory_vault, ledger, state_key)
    assert bad.mismatched == ["S018"] and bad.missing == ["S014"] and not bad.ok


def test_verify_against_empty_vault_reports_everything_missing(prepared, memory_vault, ledger, state_key):
    items, by_id = prepared
    assert len(verify(items, by_id, memory_vault, ledger, state_key).missing) == 36


def test_cas_mismatch_is_reported_as_failure(prepared, ledger, state_key):
    from vault_migration.backends import CasMismatch

    class Racy(MemoryVault):
        def write(self, *a, **k):  # noqa: ANN002, ANN003
            raise CasMismatch("lost the race")

    items, by_id = prepared
    res = apply(items[:1], by_id, Racy(), ledger, state_key, execute=True, create_mounts=True)
    assert res.failed == ["S001"]


def test_single_item_target_str():
    assert str(PlanItem("1", Target("kv-a", "dev/x/y")).target) == "kv-a/dev/x/y"


def test_dry_run_reports_missing_mounts_per_mount(prepared, memory_vault, ledger, state_key):
    res = run(prepared, memory_vault, ledger, state_key, execute=False, create_mounts=False)
    assert set(res.missing_mounts) == {"kv-payments", "kv-scoring", "kv-platform"}
    assert sum(map(len, res.missing_mounts.values())) == 36 == len(res.failed) and not res.ok
    assert ledger.entries() == [] and memory_vault.mounts == set()


def test_dry_run_with_create_mounts_lists_them_without_creating(prepared, memory_vault, ledger, state_key):
    res = run(prepared, memory_vault, ledger, state_key, execute=False)
    assert res.mounts_to_create == {"kv-payments", "kv-scoring", "kv-platform"} and res.ok
    assert memory_vault.mounts == set()


def test_missing_metadata_is_repaired_on_rerun(prepared, memory_vault, ledger, state_key):
    run(prepared, memory_vault, ledger, state_key)
    k = ("kv-payments", "dev/orders/db_password")
    old = memory_vault.store[k]
    memory_vault.store[k] = old.__class__(old.data, old.version, {"owner": "kept"})
    dry = run(prepared, memory_vault, ledger, state_key, execute=False)
    assert dry.metadata_repaired == ["S001"] and memory_vault.store[k].custom_metadata == {"owner": "kept"}
    res = run(prepared, memory_vault, ledger, state_key)
    assert res.metadata_repaired == ["S001"] and len(res.skipped) == 35 and res.ok
    md = memory_vault.store[k].custom_metadata
    assert md["legacy_id"] == "S001" and md["owner"] == "kept" and memory_vault.store[k].version == 1
    assert any(e.status == "metadata_written" and e.secret_id == "S001" for e in ledger.entries())
    assert not run(prepared, memory_vault, ledger, state_key).metadata_repaired


def test_failed_metadata_write_is_reported_and_repaired_later(prepared, ledger, state_key):
    from vault_migration.backends import MetadataError

    class NoMetadata(MemoryVault):
        broken = True

        def write(self, mount, path, data, cas, metadata):  # noqa: ANN001
            version = super().write(mount, path, data, cas, {})
            if self.broken:
                raise MetadataError(f"value written as version {version}, but metadata: HTTP 403")
            self.write_metadata(mount, path, metadata)
            return version

    v = NoMetadata()
    first = run(prepared, v, ledger, state_key)
    assert len(first.written) == 36 == len(first.failed) and not first.ok
    assert "re-run to repair" in first.errors["S001"]
    second = run(prepared, v, ledger, state_key)
    assert len(second.metadata_repaired) == 36 and second.ok and not second.written
    assert all(s.custom_metadata["migrated_by"] == "vault-migration-toolkit" for s in v.store.values())


def test_soft_deleted_target_is_a_conflict_and_overwrite_continues_its_versions(
    prepared, memory_vault, ledger, state_key
):
    run(prepared, memory_vault, ledger, state_key)
    k = ("kv-payments", "dev/orders/db_password")
    memory_vault.store[k] = memory_vault.store[k].__class__({}, 1, {})  # how VaultHTTP reads a deleted secret
    assert run(prepared, memory_vault, ledger, state_key).conflicts == ["S001"]
    assert run(prepared, memory_vault, ledger, state_key, overwrite=True).written == ["S001"]
    assert memory_vault.store[k].version == 2


def test_failed_mount_creation_is_tried_once_and_reported(prepared, ledger, state_key):
    class NoSys(MemoryVault):
        attempts = 0

        def create_mount(self, mount):  # noqa: ANN001
            self.attempts += 1
            raise VaultError(f"create mount {mount}: HTTP 403")

    v = NoSys()
    res = run(prepared, v, ledger, state_key)
    assert v.attempts == 3 and not res.mounts_to_create and not res.written
    assert res.mount_errors["kv-payments"] == "create mount kv-payments: HTTP 403"
    assert sum(map(len, res.missing_mounts.values())) == 36


def test_verify_reports_a_soft_deleted_secret_as_missing(prepared, memory_vault, ledger, state_key):
    items, by_id = prepared
    run(prepared, memory_vault, ledger, state_key)
    k = ("kv-payments", "dev/orders/db_password")
    memory_vault.store[k] = memory_vault.store[k].__class__({}, 1, {})
    assert verify(items, by_id, memory_vault, ledger, state_key).missing == ["S001"]
