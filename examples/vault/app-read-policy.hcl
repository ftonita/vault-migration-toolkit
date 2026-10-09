# Example policy for a CONSUMER after the migration: the orders service in prod reads only its own secrets.
#
#   vault policy write payments-orders-prod examples/vault/app-read-policy.hcl
path "kv-payments/data/prod/orders/*" {
  capabilities = ["read"]
}
