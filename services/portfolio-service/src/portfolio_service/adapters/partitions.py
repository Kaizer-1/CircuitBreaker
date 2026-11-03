"""Which matching.v1 partition holds a ticker: the same murmur2 hash every Kafka producer uses."""

from collections.abc import Collection

from aiokafka.partitioner import DefaultPartitioner

_partitioner = DefaultPartitioner()


def partition_for(key: str, partitions: Collection[int]) -> int:
    ordered = sorted(partitions)
    return int(_partitioner(key.encode(), ordered, ordered))
