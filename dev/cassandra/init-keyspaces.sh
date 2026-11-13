#!/usr/bin/env bash
# One-shot init job: one keyspace per owning service, one login role per keyspace, and NO shared keyspace.
# Idempotent. A role's password comes from PASSWORD_<KEYSPACE> (e.g. PASSWORD_ORDER_SERVICE), set from a Kubernetes
# Secret on the cluster; without it, the local-development placeholder cb_dev_<keyspace> (Docker Compose).
# Used by Compose (cassandra-init) and the Helm chart (charts/circuitbreaker/charts/cassandra links to this file).
set -euo pipefail
HOST="${CASSANDRA_HOST:-cassandra}"
ADMIN_USER="${CASSANDRA_ADMIN_USER:-cassandra}"
ADMIN_PASSWORD="${CASSANDRA_ADMIN_PASSWORD:-cassandra}"

# keyspace == service name in snake_case, exactly the four Cassandra owners in the data ownership doc.
for ks in order_service matching_engine market_data sre_agent; do
  role="${ks}_role"
  var="PASSWORD_$(echo "$ks" | tr '[:lower:]' '[:upper:]')"
  password="${!var:-cb_dev_${ks}}"
  echo "keyspace $ks  role $role"
  cqlsh "$HOST" -u "$ADMIN_USER" -p "$ADMIN_PASSWORD" -e "
    CREATE KEYSPACE IF NOT EXISTS ${ks}
      WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1};
    CREATE ROLE IF NOT EXISTS ${role} WITH PASSWORD = '${password}' AND LOGIN = true;
    GRANT ALL PERMISSIONS ON KEYSPACE ${ks} TO ${role};"
done
echo "keyspaces ready:"
cqlsh "$HOST" -u "$ADMIN_USER" -p "$ADMIN_PASSWORD" -e "SELECT keyspace_name FROM system_schema.keyspaces;"
