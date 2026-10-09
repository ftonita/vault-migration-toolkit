# Policy for the token that runs vault-migrate (apply / verify).
# Vault globs only allow "*" at the END of a path, so list each team mount explicitly:
# one block pair per mount from `vault-migrate plan` (kv-<team>).
#
#   vault policy write vault-migration examples/vault/migration-policy.hcl
#   vault token create -policy=vault-migration -ttl=8h -display-name=vault-migration

# apply/verify check that the target mount exists on every run.
path "sys/mounts" {
  capabilities = ["read"]
}

# Only needed with --create-mounts. Drop it if mounts are created by your platform team / Terraform.
path "sys/mounts/kv-*" {
  capabilities = ["create", "update"]
}

# --- kv-payments --------------------------------------------------------------
# create: new secrets; update: --overwrite (CAS on an existing version); read: idempotency + verify.
path "kv-payments/data/*" {
  capabilities = ["create", "read", "update"]
}
# custom_metadata (legacy_id, kind, last_rotated) is written with a second request.
path "kv-payments/metadata/*" {
  capabilities = ["create", "read", "update"]
}

# --- kv-scoring ---------------------------------------------------------------
path "kv-scoring/data/*" {
  capabilities = ["create", "read", "update"]
}
path "kv-scoring/metadata/*" {
  capabilities = ["create", "read", "update"]
}

# --- kv-platform --------------------------------------------------------------
path "kv-platform/data/*" {
  capabilities = ["create", "read", "update"]
}
path "kv-platform/metadata/*" {
  capabilities = ["create", "read", "update"]
}
