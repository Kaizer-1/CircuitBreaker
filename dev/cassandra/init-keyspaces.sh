#!/usr/bin/env bash
# One-shot init job: one keyspace per owning service, one login role per keyspace, and NO shared keyspace.
# Idempotent. Passwords are local-development placeholders (cb_dev_<keyspace>); real environments get secrets
# from Kubernetes Secrets (docs/03-architecture/07-deployment-and-environments.md).
set -euo pipefail
HOST="${CASSANDRA_HOST:-cassandra}"
ADMIN_USER="${CASSANDRA_ADMIN_USER:-cassandra}"
ADMIN_PASSWORD="${CASSANDRA_ADMIN_PASSWORD:-cassandra}"

# keyspace == service name in snake_case, exactly the four Cassandra owners in the data ownership doc.
for ks in order_service matching_engine market_data sre_agent; do
  role="${ks}_role"
  echo "keyspace $ks  role $role"
  cqlsh "$HOST" -u "$ADMIN_USER" -p "$ADMIN_PASSWORD" -e "
    CREATE KEYSPACE IF NOT EXISTS ${ks}
      WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};
    CREATE ROLE IF NOT EXISTS ${role} WITH PASSWORD = 'cb_dev_${ks}' AND LOGIN = true;
    GRANT ALL PERMISSIONS ON KEYSPACE ${ks} TO ${role};"
done
echo "keyspaces ready:"
cqlsh "$HOST" -u "$ADMIN_USER" -p "$ADMIN_PASSWORD" -e "SELECT keyspace_name FROM system_schema.keyspaces;"
