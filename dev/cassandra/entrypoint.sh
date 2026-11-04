#!/usr/bin/env bash
# Wrapper around the official entrypoint: turn on password authentication and authorisation so each service can
# have its own role limited to its own keyspace (docs/03-architecture/02-data-ownership.md). The stock image
# starts with AllowAllAuthenticator and offers no environment variable to change it.
set -euo pipefail
conf="${CASSANDRA_CONF:-/etc/cassandra}/cassandra.yaml"
# The matching engine stores each command's events in one logged batch (all or nothing). The default fail
# threshold (50 KiB, about 80 events) is too small for a MARKET order sweeping many levels; 2 MiB allows ~3,000.
sed -i \
  -e 's/^authenticator:.*/authenticator: PasswordAuthenticator/' \
  -e 's/^authorizer:.*/authorizer: CassandraAuthorizer/' \
  -e 's/^batch_size_warn_threshold:.*/batch_size_warn_threshold: 64KiB/' \
  -e 's/^batch_size_fail_threshold:.*/batch_size_fail_threshold: 2MiB/' \
  "$conf"
exec docker-entrypoint.sh "$@"
