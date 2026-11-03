#!/usr/bin/env bash
# One-shot init job: create every topic from docs/03-architecture/03-event-catalog.md (names, partitions, retention).
# Idempotent: topics that already exist are left alone.
set -euo pipefail
BROKERS="${REDPANDA_BROKERS:-redpanda:9092}"

DAY_MS=86400000

# create <topic> <partitions> <retention-days>
create() {
  local topic=$1 partitions=$2 days=$3
  if rpk -X brokers="$BROKERS" topic describe "$topic" >/dev/null 2>&1; then
    echo "exists   $topic"
  else
    rpk -X brokers="$BROKERS" topic create "$topic" -p "$partitions" -r 1 -c "retention.ms=$((days * DAY_MS))" >/dev/null
    echo "created  $topic  partitions=$partitions retention=${days}d"
  fi
}

# Topics and their settings are the event catalog's "Topics" table.
create orders.v1     5 7
create matching.v1   5 7
create candles.v1    5 1
create incidents.v1  1 30

# Dead-letter topics: `<topic>.dlq` (catalog: "Delivery guarantees"). The catalog gives no partition count or
# retention for them; Phase 1 uses 1 partition and 7 days (recorded in docs/08-developer-setup.md).
for t in orders.v1 matching.v1 candles.v1 incidents.v1; do create "$t.dlq" 1 7; done

echo "topics ready:"; rpk -X brokers="$BROKERS" topic list
