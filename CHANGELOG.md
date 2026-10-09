# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Added
- Russian README (`README.ru.md`) with a language switcher in both READMEs.
- `examples/`: sample export in JSON and CSV, annotated `rules.yml`, adapters for `.env` trees, nested JSON/YAML and Kubernetes Secrets (with sample inputs), minimal Vault migration policy, consumer read policy, local Vault compose file.
- `docs/SOURCES.md` / `docs/SOURCES.ru.md`: export format, adapters, recipes (AWS Secrets Manager, KeePassXC CSV), writing an adapter.
- `docs/VAULT.md` / `docs/VAULT.ru.md`: layout in Vault, mounts, token policy, running and resuming, checks with the `vault` CLI, switching consumers, rollback, troubleshooting.
- Tests for `examples/`; CI runs `examples/` against real Vault with a non-root token limited by the example policy.

### Fixed
- The migration token no longer needs `read` on `sys/mounts`: a missing mount is detected from the secret read (Vault's 404 `no handler for route`). `sys/mounts/kv-*` is only needed with `--create-mounts`.
- `sys/mounts` is no longer requested twice per secret; known-missing mounts are not asked again.
- The dry run reports missing mounts (`missing mount: kv-x/ (N secrets; ...)`, exit 1), or the mounts it would create with `--create-mounts`. A failed mount creation is tried once and reported per mount.
- If custom metadata cannot be written after the value, the error says the value is in Vault, and a re-run writes the missing metadata (`metadata_repaired`, ledger status `metadata_written`) instead of skipping the secret.
- Soft-deleted targets: `apply` reports a conflict instead of failing on check-and-set; `verify` reports them as missing.
- `failed:` lines show the reason instead of "see ledger".

## [1.0.0] - 2026-10-08

### Added
- `inventory`: weak, shared-across-environments, duplicate, stale and unowned findings with severities.
- `plan`: deterministic legacy -> `kv-<team>/<env>/<app>/<name>` mapping; ambiguous records go to a manual queue.
- `apply`: dry-run by default, create-only writes with check-and-set, conflict detection, `--overwrite`, `--create-mounts`.
- `verify`: keyed-fingerprint comparison of source and Vault.
- Append-only ledger with no secret values; 0600 per-migration fingerprint key.
- Stdlib-only Vault HTTP client with retries; in-memory and file-backed fakes for offline demos.
- Synthetic data generator (`vault-migrate demo`).
