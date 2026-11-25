#!/usr/bin/env bash
# One-shot init job: one keyspace per owning service, one login role per keyspace, and NO shared keyspace.
# Idempotent. A role's password comes from PASSWORD_<KEYSPACE> (e.g. PASSWORD_ORDER_SERVICE), set from a Kubernetes
# Secret on the cluster; without it, the local-development placeholder cb_dev_<keyspace> (Docker Compose).
# Used by Compose (cassandra-init) and the Helm chart (charts/circuitbreaker/charts/cassandra links to this file).
#
# The superuser: Compose keeps the image's built-in password (cassandra), a local-development placeholder. On
# Kubernetes (ROTATE_DEFAULT_SUPERUSER=true) the superuser's password is the cassandra-admin Secret's, never the
# image's default: a cluster that still has the default (a new one, or one from before 2026-11-25) gets the Secret's
# password here, once, with ALTER ROLE. The Secret is read from a mounted file, which updates in place.
set -euo pipefail
HOST="${CASSANDRA_HOST:-cassandra}"
ADMIN_USER="${CASSANDRA_ADMIN_USER:-cassandra}"
IMAGE_DEFAULT=cassandra

admin_password() {
  if [ -n "${CASSANDRA_ADMIN_PASSWORD_FILE:-}" ]; then cat "$CASSANDRA_ADMIN_PASSWORD_FILE"
  else echo "${CASSANDRA_ADMIN_PASSWORD:-$IMAGE_DEFAULT}"; fi
}
can_login() {
  cqlsh "$HOST" -u "$ADMIN_USER" -p "$1" -e 'SELECT release_version FROM system.local' >/dev/null 2>&1
}

ADMIN_PASSWORD="$(admin_password)"
if [ "${ROTATE_DEFAULT_SUPERUSER:-false}" = true ]; then
  # Argo CD may start this job before it syncs a new Secret: a Secret still holding the image's default is about
  # to be replaced, so wait for it (up to 10 minutes).
  for _ in $(seq 120); do
    [ -n "$ADMIN_PASSWORD" ] && [ "$ADMIN_PASSWORD" != "$IMAGE_DEFAULT" ] && break
    echo "the cassandra-admin Secret still holds the image's default password (or none); waiting for its new value"
    sleep 5
    ADMIN_PASSWORD="$(admin_password)"
  done
  if [ -z "$ADMIN_PASSWORD" ] || [ "$ADMIN_PASSWORD" = "$IMAGE_DEFAULT" ]; then
    echo "the cassandra-admin Secret must hold a password other than the image's default" >&2
    exit 1
  fi
  # Wait for CQL with whichever password the superuser has now (up to 10 minutes).
  for _ in $(seq 120); do
    can_login "$ADMIN_PASSWORD" && break
    if can_login "$IMAGE_DEFAULT"; then
      cqlsh "$HOST" -u "$ADMIN_USER" -p "$IMAGE_DEFAULT" -e "ALTER ROLE $ADMIN_USER WITH PASSWORD = '$ADMIN_PASSWORD';"
      echo "superuser: the image's default password replaced by the cassandra-admin Secret's"
      break
    fi
    sleep 5
  done
  # Cassandra caches credentials for 2 s (credentials_validity): give a changed password a moment.
  for _ in $(seq 6); do can_login "$ADMIN_PASSWORD" && break; sleep 2; done
  if ! can_login "$ADMIN_PASSWORD"; then
    echo "neither the Secret's password nor the image's default logs in as $ADMIN_USER: changing the superuser's" \
      "password from one Secret value to another needs ALTER ROLE first (deployment doc, secrets)" >&2
    exit 1
  fi
else
  until can_login "$ADMIN_PASSWORD"; do sleep 5; done
fi

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
