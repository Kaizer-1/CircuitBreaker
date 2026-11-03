#!/usr/bin/env bash
# Wrapper around the official entrypoint: turn on password authentication and authorisation so each service can
# have its own role limited to its own keyspace (docs/03-architecture/02-data-ownership.md). The stock image
# starts with AllowAllAuthenticator and offers no environment variable to change it.
set -euo pipefail
conf="${CASSANDRA_CONF:-/etc/cassandra}/cassandra.yaml"
sed -i \
  -e 's/^authenticator:.*/authenticator: PasswordAuthenticator/' \
  -e 's/^authorizer:.*/authorizer: CassandraAuthorizer/' \
  "$conf"
exec docker-entrypoint.sh "$@"
