#!/usr/bin/env bash
# Wrapper around the official entrypoint: turn on password authentication and authorisation so each service can
# have its own role limited to its own keyspace (docs/03-architecture/02-data-ownership.md). The stock image
# starts with AllowAllAuthenticator and offers no environment variable to change it.
set -euo pipefail
conf="${CASSANDRA_CONF:-/etc/cassandra}/cassandra.yaml"
jvm="${CASSANDRA_CONF:-/etc/cassandra}/jvm17-server.options"
# The matching engine stores each command's events in one logged batch (all or nothing). The default fail
# threshold (50 KiB, about 80 events) is too small for a MARKET order sweeping many levels; 2 MiB allows ~3,000.
#
# The rest sizes Cassandra for its container (2 CPUs, 2 GiB), not for the machine: the stock scripts read the
# host's CPU count and memory, so on a 12-core laptop they chose 12 GC threads and 2.7 GB of direct memory.
# Each value and its measured reason: docs/03-architecture/07-deployment-and-environments.md, "Cassandra's
# memory budget". Memtables live off the heap so the garbage collector does not copy them; the commit log is
# capped (the default lets it reach 8 GiB); fewer request threads mean fewer stacks.
sed -i \
  -e 's/^authenticator:.*/authenticator: PasswordAuthenticator/' \
  -e 's/^authorizer:.*/authorizer: CassandraAuthorizer/' \
  -e 's/^batch_size_warn_threshold:.*/batch_size_warn_threshold: 64KiB/' \
  -e 's/^batch_size_fail_threshold:.*/batch_size_fail_threshold: 2MiB/' \
  -e 's/^memtable_allocation_type:.*/memtable_allocation_type: offheap_objects/' \
  -e 's/^#* *memtable_heap_space:.*/memtable_heap_space: 64MiB/' \
  -e 's/^#* *memtable_offheap_space:.*/memtable_offheap_space: 128MiB/' \
  -e 's/^#* *commitlog_total_space:.*/commitlog_total_space: 256MiB/' \
  -e 's/^#* *file_cache_size:.*/file_cache_size: 64MiB/' \
  -e 's/^concurrent_reads:.*/concurrent_reads: 8/' \
  -e 's/^concurrent_writes:.*/concurrent_writes: 16/' \
  -e 's/^concurrent_counter_writes:.*/concurrent_counter_writes: 8/' \
  -e 's/^#* *concurrent_compactors:.*/concurrent_compactors: 1/' \
  "$conf"
# JVM: GC threads for 2 CPUs (12 threads in a 2-CPU quota throttle the whole JVM mid-pause), shorter pause goal,
# regions small enough for a 512 MB heap, every native area capped, and a clean exit (then a restart) on
# OutOfMemoryError instead of a JVM limping on with a dead thread. The file is edited in place, so the lines are
# removed first: a container restarted by Compose keeps its file system.
sed -i -E \
  -e '/^-XX:(ParallelGCThreads|ConcGCThreads|ActiveProcessorCount|MaxDirectMemorySize|MaxMetaspaceSize)=/d' \
  -e '/^-XX:(ReservedCodeCacheSize)=|^-XX:\+ExitOnOutOfMemoryError/d' \
  -e 's/^-XX:G1HeapRegionSize=.*/-XX:G1HeapRegionSize=8m/' \
  -e 's/^-XX:MaxGCPauseMillis=.*/-XX:MaxGCPauseMillis=100/' \
  "$jvm"
cat >> "$jvm" <<'EOF'
-XX:ParallelGCThreads=2
-XX:ConcGCThreads=1
-XX:ActiveProcessorCount=2
-XX:MaxDirectMemorySize=192M
-XX:MaxMetaspaceSize=128M
-XX:ReservedCodeCacheSize=64M
-XX:+ExitOnOutOfMemoryError
EOF
# jemalloc (the image preloads it) sizes itself from the host too: 4 arenas per CPU it sees. Two arenas, no huge
# pages for its own memory (the kernel's "always" mode rounds half-used areas up to 2 MB) and freed memory given
# back within seconds.
export MALLOC_CONF="narenas:2,thp:never,background_thread:true,dirty_decay_ms:2000,muzzy_decay_ms:0"
exec docker-entrypoint.sh "$@"
