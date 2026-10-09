# vault-migration-toolkit

🇬🇧 **English** | [🇷🇺 Русский](README.ru.md)

[![ci](https://github.com/ftonita/vault-migration-toolkit/actions/workflows/ci.yml/badge.svg)](https://github.com/ftonita/vault-migration-toolkit/actions)
![Vault](https://img.shields.io/badge/HashiCorp_Vault-KV_v2-FFEC6E?logo=vault&logoColor=black)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Dependencies](https://img.shields.io/badge/runtime_deps-PyYAML_only-brightgreen)

A careful, resumable way to move secrets from a legacy store into **HashiCorp Vault KV v2**: find the problems first, map every secret to a team- and environment-scoped path, write with check-and-set, then prove the result matches. **It never prints, logs or reports a secret value.**

> **All data in this repository is synthetic.** The demo generator produces obviously fake values (`SYNTHETIC-...`). This is a reference design for the migration approach used on multi-team Vault rollouts, not code or data from any employer.

## Workflow

```mermaid
flowchart LR
    L[("legacy export<br/>json / csv")] --> I["inventory<br/>weak, shared, stale, unowned"]
    L --> P["plan<br/>team / env / app / name"]
    P --> A["apply<br/>dry-run by default<br/>CAS, create-only"]
    A --> V[("Vault KV v2")]
    V --> C["verify<br/>keyed fingerprints"]
    A -.-> G[("ledger.jsonl<br/>no values")]
```

| Step | What it guarantees |
|---|---|
| `inventory` | Needs no Vault access. Flags weak values, the **same value reused across environments** (high severity when prod is involved), duplicates, stale and unowned secrets. Compared via HMAC fingerprints, never by printing values. |
| `plan` | Deterministic mapping `kv-<team>/<env>/<app>/<name>` with team/env aliases and app-owner fallback. Anything ambiguous (no team, unknown env, colliding target) goes to a **manual queue** instead of being guessed. |
| `apply` | **Dry run unless `--execute`.** Create-only writes with check-and-set. An existing, different value is a *conflict*, not overwritten (`--overwrite` is explicit). Re-runs are idempotent because they compare with what is actually in Vault. |
| `verify` | Reads every migrated secret back and compares fingerprints: `verified / mismatched / missing`. Non-zero exit on any difference. |

## Try it in two minutes (no Vault needed)

```bash
pip install .
vault-migrate demo --out data                       # synthetic legacy export + rules
vault-migrate inventory --source data/legacy.json --state st --today 2026-10-08
A="--source data/legacy.json --rules data/rules.yml --state st --backend fake:st/fake-vault.json"
vault-migrate apply  $A --create-mounts             # dry run
vault-migrate apply  $A --execute --create-mounts
vault-migrate apply  $A --execute                   # idempotent
vault-migrate verify $A
```

Condensed output of that run on the bundled synthetic data (38 records; lines abridged):

```
inventory: 9 findings (high 2, medium 5, low 2)
  high    shared_across_envs  S033, S034  same value in orders/prod/smtp_password, orders/stage/smtp_password
  high    weak                S031        admin_password (orders/prod)
  ...
plan:   36 mapped, 2 in the manual queue (S037 no owning team, S038 environment 'uat')

DRY RUN (nothing written; add --execute): written=0 would_write=36 skipped=0 conflicts=0 failed=0 manual_queue=2
  would create mount: kv-payments/, kv-platform/, kv-scoring/
EXECUTED: written=36 would_write=0 skipped=0 conflicts=0 failed=0 manual_queue=2
EXECUTED: written=0 would_write=0 skipped=36 conflicts=0 failed=0 manual_queue=2
verified=36 mismatched=0 missing=0 manual_queue=2
# someone edits one secret in Vault by hand:
verified=35 mismatched=1 missing=0        mismatch: S008          (exit 1)
EXECUTED: ... skipped=35 conflicts=1      conflict: S008          (exit 1, nothing overwritten)
EXECUTED: written=1 ... (with --overwrite)
verified=36 mismatched=0 missing=0
```

## Migrate your own secrets

The fastest route from a legacy store to a verified Vault. Each step links to the detailed guide.

**1. Turn the source into an export.** The toolkit reads a flat JSON/CSV list of secrets ([format and samples](docs/SOURCES.md#1-the-export-format)). Use a ready adapter, a recipe, or rename CSV columns:

| Your source | How |
|---|---|
| `.env` files `<team>/<app>/<env>.env` | `python examples/adapters/env_files.py secrets/ --out legacy.json` |
| Nested JSON / YAML config | `python examples/adapters/nested_json.py config.json --out legacy.json` |
| Kubernetes Secrets | `kubectl get secrets -A -o json \| python examples/adapters/k8s_secrets.py - --out legacy.json` |
| AWS Secrets Manager, password-manager CSV | [recipes](docs/SOURCES.md#3-recipes-for-other-sources) |
| Spreadsheet / any CSV | rename the header to `id,name,value,app,env,team,kind,last_rotated,consumers` |
| Anything else | [write a 20-line adapter](docs/SOURCES.md#4-writing-your-own-adapter) |

A minimal record (`id`, `name`, `value` are required; `team`, `env`, `app` decide the target path):

```json
{"id": "LEG-0001", "name": "db_password", "value": "...", "app": "orders", "env": "production",
 "team": "Payments Team", "kind": "db_password", "last_rotated": "2026-08-14", "consumers": ["orders-api"]}
```

**2. Write the mapping rules.** Start from [`examples/rules.yml`](examples/rules.yml) and iterate with `plan` until the manual queue only holds records that need a human:

```yaml
mount_prefix: kv-                       # kv-<team>/<env>/<app>/<name>
allowed_envs: [dev, stage, prod]
team_aliases: { "Payments Team": payments }
env_aliases:  { production: prod, staging: stage }
app_owners:   { reports: platform }     # fallback when the record has no team
```

```bash
vault-migrate inventory --source legacy.json --state migration-state --out inventory.md   # no Vault needed
vault-migrate plan      --source legacy.json --rules rules.yml --out plan.md
```

**3. Prepare Vault.** Load [`examples/vault/migration-policy.hcl`](examples/vault/migration-policy.hcl) (one block per team mount), issue a short-lived token, and create the `kv-<team>` mounts or allow `--create-mounts` ([details](docs/VAULT.md#3-prepare-the-production-vault)). Rehearse first on a local dev Vault: `docker compose -f examples/vault/docker-compose.yml up -d`.

**4. Migrate and verify.**

```bash
export VAULT_ADDR=https://vault.example.com VAULT_TOKEN=...     # https enforced (http only for localhost)
A="--source legacy.json --rules rules.yml --state migration-state --backend http"
vault-migrate apply  $A                       # dry run; also reports mounts that do not exist yet
vault-migrate apply  $A --execute             # add --create-mounts if the token may create mounts
vault-migrate verify $A                       # exit 0 = every secret in Vault matches the export
```

**5. Switch consumers, then clean up.** Applications read `kv-<team>/data/<env>/<app>/<name>` → `.data.data.value` ([CLI, API, Vault Agent, Kubernetes examples](docs/VAULT.md#6-switch-applications-to-vault)). Revoke the migration token and shred the export ([rollback and cleanup](docs/VAULT.md#7-roll-back-and-clean-up)).

## Documentation

| Document | Contents |
|---|---|
| [docs/SOURCES.md](docs/SOURCES.md) | Export format field by field, adapters, recipes for other stores, writing an adapter |
| [docs/VAULT.md](docs/VAULT.md) | What is written to Vault, mounts, token policy, running and resuming, checking with the `vault` CLI, consumers, rollback, troubleshooting |
| [examples/](examples) | `legacy.json` / `legacy.csv` sample export, `rules.yml`, adapters with sample inputs, Vault policies, local Vault compose file |

## Safety design

- `Secret` wrapper: `repr`/`str`/f-strings are always `Secret(<redacted>)`; values are revealed only at the single write call.
- The ledger and reports contain ids, paths and 8-character keyed fingerprints. The HMAC key is random per migration, stored `0600` in the state directory.
- TLS is required for non-local Vault addresses; 5xx and network errors are retried with backoff.
- The legacy export itself is plaintext: keep it on an encrypted volume and shred it after `verify` passes.

## What is verified

Reproduce with `pip install -e ".[dev]" && pytest` (87 tests, 98% line coverage):

- Unit tests for parsing, redaction, fingerprints, rules, analysis, planning, migration (dry-run, idempotence, conflicts, CAS overwrite, partial failure and resume) and ledger contents.
- The HTTP client is tested over real HTTP against `tests/stub_vault.py`, a **stub that implements only the KV v2 subset used here** (data, metadata, mounts, CAS, soft delete, 5xx, 403). It is not Vault, so semantics such as mount permissions and real CAS error text are assumptions from the API documentation.
- End-to-end CLI flow on the synthetic dataset, including tampering and recovery (the output above).
- `examples/`: the JSON and CSV samples load to identical records, the sample plan matches the docs, and every adapter's output maps cleanly with `plan` (one is also taken through `apply` and `verify`).
- The CI job `real-vault` runs the whole flow (`apply --execute --create-mounts`, an idempotent re-run, `verify`) against a **real Vault 1.17 dev server**; it passed on the first run (2026-10-09). It also runs `examples/` with a **non-root token** limited by `examples/vault/migration-policy.hcl`, which grants no `sys/*` access except creating `kv-*` mounts.

**Not verified:** Vault Enterprise namespaces, production-style Vault (Raft, auth methods other than tokens), very large exports (the export is held in memory), and non-KV legacy sources. Custom metadata is written by a second request, so it is not atomic with the value; if it fails, a re-run repairs it.
