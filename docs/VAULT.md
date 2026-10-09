# Working with Vault

🇬🇧 **English** | [🇷🇺 Русский](VAULT.ru.md)

This guide covers what the toolkit writes, how to prepare a Vault (local rehearsal and production), the exact policy the migration token needs, how to run and check the migration, how applications read the secrets afterwards, and how to roll back. The `vault-migrate` and `vault` commands below were run against a Vault 1.17 dev server with the files in [`examples/`](../examples); the Vault Agent and Kubernetes snippets in section 6 were not.

## 1. What ends up in Vault

| Concept | Value |
|---|---|
| Secrets engine | **KV version 2**, one mount per team: `kv-<team>/` (prefix from `mount_prefix`) |
| Path | `kv-<team>/<env>/<app>/<name>`, e.g. `kv-payments/prod/orders/db_password` |
| Data | exactly one key: `{"value": "<secret>"}` |
| Custom metadata | `legacy_id`, `kind`, `last_rotated` (if known), `migrated_by=vault-migration-toolkit` |
| Versioning | first write is version 1 with `cas=0` (create-only); `--overwrite` writes the next version with CAS on the current one |

Remember that the KV v2 HTTP API inserts `data/` (values) or `metadata/` (metadata) after the mount: the secret above is `GET /v1/kv-payments/data/prod/orders/db_password`. The `vault kv` CLI with `-mount=` hides this.

## 2. Rehearse on a local Vault

```bash
docker compose -f examples/vault/docker-compose.yml up -d     # or: vault server -dev -dev-root-token-id=dev-root-token
export VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN=dev-root-token

A="--source examples/legacy.json --rules examples/rules.yml --state rehearsal-state --backend http"
vault-migrate apply  $A                                        # dry run: would_write=8, manual_queue=2
vault-migrate apply  $A --execute --create-mounts              # written=8
vault-migrate apply  $A --execute                              # skipped=8 (idempotent)
vault-migrate verify $A                                        # verified=8 mismatched=0 missing=0
vault kv get -mount=kv-payments prod/orders/db_password
```

The dev server keeps data in memory and starts unsealed with a root token: use it only with synthetic data. Run the rehearsal against a copy of your real `rules.yml` and a **synthetic** export of the same shape.

## 3. Prepare the production Vault

### 3.1 Mounts

Choose one of two options:

- Let the tool create them: `--create-mounts` enables `kv-<team>/` as KV v2 for every team in the plan.
- Create them yourself (Terraform, platform team), which is usually preferable because you can set options such as `max_versions` or `cas_required`:

  ```bash
  vault-migrate plan --source legacy.json --rules rules.yml | grep -o '`kv-[^/]*' | tr -d '`' | sort -u  # mounts in the plan
  vault secrets enable -path=kv-payments -version=2 kv
  ```

Without `--create-mounts`, a missing mount makes each of its secrets `failed` with `mount kv-x/ does not exist`. The dry run does not check this, so compare the mounts first.

### 3.2 Policy for the migration token

[`examples/vault/migration-policy.hcl`](../examples/vault/migration-policy.hcl) is the minimal policy; CI runs `examples/` with a non-root token limited by it:

| Path | Capabilities | Why |
|---|---|---|
| `sys/mounts` | `read` | **Always needed**: `apply` and `verify` check that each target mount exists. Without it every run stops with `list mounts: HTTP 403`. |
| `sys/mounts/kv-*` | `create`, `update` | Only with `--create-mounts` |
| `kv-<team>/data/*` | `create`, `read`, `update` | `create`: new secrets; `read`: idempotency and `verify`; `update`: `--overwrite` |
| `kv-<team>/metadata/*` | `create`, `read`, `update` | `custom_metadata` is written by a second request |

Vault only allows `*` at the end of a policy path, so `kv-*/data/*` does not work: add one `data` + `metadata` block per team mount (the example has three). For `verify` alone, `read` on `data/*` and `sys/mounts` is enough.

```bash
vault policy write vault-migration examples/vault/migration-policy.hcl
export VAULT_TOKEN=$(vault token create -policy=vault-migration -ttl=8h \
                     -display-name=vault-migration -field=token)
```

Use a short TTL that covers the migration window, and revoke the token afterwards (section 7).

### 3.3 Connection settings

| Variable | Meaning |
|---|---|
| `VAULT_ADDR` | **Required.** Must be `https://...`; plain `http` is accepted only for `127.0.0.1` / `localhost`. |
| `VAULT_TOKEN` | **Required.** The migration token. Other auth methods (AppRole, OIDC, ...): log in with the `vault` CLI and export the resulting token. |
| `VAULT_NAMESPACE` | Optional, Vault Enterprise namespace (sent as `X-Vault-Namespace`). Not covered by the test suite. |
| `SSL_CERT_FILE` | CA bundle for a corporate CA. The client uses Python's default trust store; **`VAULT_CACERT` is not read.** |

Requests time out after 10 s; 5xx responses and network errors are retried 3 times with backoff.

## 4. Run the migration

```bash
export VAULT_ADDR=https://vault.example.com VAULT_TOKEN=...      # migration token from 3.2

vault-migrate inventory --source legacy.json --state migration-state --out inventory.md   # no Vault access
vault-migrate plan      --source legacy.json --rules rules.yml --out plan.md            # no Vault access

A="--source legacy.json --rules rules.yml --state migration-state --backend http"
vault-migrate apply  $A              # dry run against the real Vault: would_write / skipped / conflicts
vault-migrate apply  $A --execute    # the write
vault-migrate verify $A              # read back and compare fingerprints
```

Review `inventory.md` and `plan.md` with the secret owners before `--execute`.

Exit codes: `0` success; `1` conflicts, failures, mismatches or missing secrets (and `inventory --fail-on` with a high finding); `2` invalid input or a Vault/configuration error (message on stderr).

### The state directory

`migration-state/` contains `fingerprint.key` (random HMAC key, `0600`) and `ledger.jsonl` (one line per action: id, target, status, 8-char fingerprint, time, never a value). Use the **same** state directory for every run of a migration, because fingerprints from a different key cannot be compared.

```bash
jq -r 'select(.status=="failed" or .status=="conflict") | [.secret_id, .target, .detail] | @tsv' migration-state/ledger.jsonl
```

### Failures, resume, conflicts

- **Resume:** just run `apply --execute` again. Already migrated secrets are compared with what is in Vault and `skipped`; only missing ones are written.
- **Conflict:** the target exists with a different value (someone created it by hand, or two legacy records point to it). Nothing is overwritten. Find out which value is correct; if it is the legacy one, run `apply --execute --overwrite` (CAS on the current version, the old one stays in history).
- **Manual queue:** records `plan` could not map. Fix the data or `rules.yml` (aliases, `app_owners`, `allowed_envs`) and run `plan` again; never edit target paths by hand.

## 5. Check the result in Vault

```bash
vault secrets list | grep kv-                                         # mounts
vault kv list -mount=kv-payments prod/orders                          # secrets of one app
vault kv get -mount=kv-payments prod/orders/db_password               # value + metadata (prints the value!)
vault kv get -field=value -mount=kv-payments prod/orders/db_password  # just the value
vault kv metadata get -mount=kv-payments prod/orders/db_password      # versions, custom_metadata.legacy_id
```

`verify` is the authoritative check. The commands above are for spot checks, and `kv get` prints the value to your terminal.

## 6. Switch applications to Vault

Give each consumer a read-only policy for its own path, like [`examples/vault/app-read-policy.hcl`](../examples/vault/app-read-policy.hcl):

```hcl
path "kv-payments/data/prod/orders/*" {
  capabilities = ["read"]
}
```

Reading the migrated value (always the `value` key):

```bash
# CLI
vault kv get -field=value -mount=kv-payments prod/orders/db_password
# HTTP API: note data/ in the path and .data.data in the response
curl -s -H "X-Vault-Token: $VAULT_TOKEN" "$VAULT_ADDR/v1/kv-payments/data/prod/orders/db_password" | jq -r .data.data.value
```

Vault Agent template:

```
{{ with secret "kv-payments/data/prod/orders/db_password" }}{{ .Data.data.value }}{{ end }}
```

Kubernetes with the Vault Agent Injector (pod annotations):

```yaml
vault.hashicorp.com/agent-inject: "true"
vault.hashicorp.com/role: "orders-prod"
vault.hashicorp.com/agent-inject-secret-db_password: "kv-payments/data/prod/orders/db_password"
vault.hashicorp.com/agent-inject-template-db_password: |
  {{ with secret "kv-payments/data/prod/orders/db_password" }}{{ .Data.data.value }}{{ end }}
```

The `consumers` field of the export lists who needs to be switched. Keep the legacy store read-only until every consumer reads from Vault.

## 7. Roll back and clean up

```bash
# a single secret back to a previous version (creates a new version with the old data)
vault kv rollback -mount=kv-payments -version=1 prod/orders/db_password

# undo a whole migration: delete everything the ledger says was written (DESTRUCTIVE, all versions)
jq -r 'select(.status=="written") | .target' migration-state/ledger.jsonl | sort -u |
  while read -r t; do vault kv metadata delete "$t"; done

# after a successful verify and cut-over
vault token revoke "$VAULT_TOKEN"     # the migration token
shred -u legacy.json                  # plaintext export
rm migration-state/fingerprint.key    # ledger fingerprints become unlinkable; keep ledger.jsonl as the audit trail
```

The migration copies values **as they are**. Secrets reported by `inventory` as `weak` or `shared_across_envs` should be rotated in Vault once consumers have switched.

## 8. Troubleshooting

Configuration errors stop the run with `error: ...` on stderr and exit code 2. Inside `apply`, errors for a single secret are counted as `failed: <id> (see ledger)` and the run continues; the message itself is in the ledger's `detail` (see the `jq` command in section 4).

| Message | Cause | Fix |
|---|---|---|
| `VAULT_ADDR and VAULT_TOKEN must be set` | environment not exported | `export VAULT_ADDR=... VAULT_TOKEN=...` |
| `refusing to talk to Vault over plain http (except localhost)` | `VAULT_ADDR=http://remote` | use `https://` |
| `cannot reach Vault: ... CERTIFICATE_VERIFY_FAILED` | corporate CA not trusted | `export SSL_CERT_FILE=/path/to/ca-bundle.pem` |
| `list mounts: HTTP 403` | no `read` on `sys/mounts` | add it to the policy (3.2) |
| `failed: ...` + ledger `mount kv-x/ does not exist` | mount missing | create it (3.1) or add `--create-mounts` |
| `create mount kv-x: HTTP 403` | no `create`/`update` on `sys/mounts/kv-*` | extend the policy or create mounts beforehand |
| `write kv-x/...: HTTP 403` | no `create` on `kv-x/data/*` | add the team mount to the policy |
| `metadata kv-x/...: HTTP 403` | no `update` on `kv-x/metadata/*` | fix the policy. The value **was written**, so a re-run reports it as `skipped` and does not retry the metadata; set it by hand with `vault kv metadata put -mount=kv-x -custom-metadata=legacy_id=... -custom-metadata=kind=... -custom-metadata=migrated_by=vault-migration-toolkit <path>` (this replaces all custom metadata, so pass every key) |
| `conflict: <id>` | target exists with another value | see section 4, *Conflicts* |
| `mismatch: <id>` in verify | value in Vault changed after the migration | find out who changed it; `apply --execute --overwrite` restores the legacy value |
