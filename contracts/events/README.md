# Event contracts

JSON Schemas (draft 2020-12) for every integration event on Redpanda: one `<EventType>.schema.json` per event, plus
`envelope.schema.json` (the fields every event has) and `defs.schema.json` (shared types such as `ticker` and
`price_paise`).

> **Integration event:** an event published on the bus for other services (see the
> [event catalog](../../docs/03-architecture/03-event-catalog.md)). **Contract:** the agreed shape of a message; if a
> producer breaks it, consumers break.

## Where they come from

The [event catalog](../../docs/03-architecture/03-event-catalog.md) is the source of truth. These files are
**generated** from its JSON blocks; do not edit them by hand.

```bash
# after changing the catalog
uv run tools/check_event_contracts.py --write
# check (part of `make lint` and CI): files equal the catalog, every catalog example validates
uv run tools/check_event_contracts.py
```

Each event file combines the envelope with that event's `data` schema and pins `event_type`, `event_version` and
`producer`. It also records the topic and message key as `x-topic` and `x-key`.

## How they are used

| Who | Check |
|---|---|
| `tools/check_event_contracts.py` (lint, CI) | The files equal the catalog; every example in the catalog is valid. |
| Each producer's `tests/contract/test_events.py` | Every event the service builds validates against its file. A producer cannot add, drop or rename a field without the catalog changing first. |
| Each consumer's tests | The catalog's examples are parsed by the consumer's event models (tolerant reader: unknown fields are ignored). |

**Strict for producers, tolerant for consumers.** Every `data` schema has `"additionalProperties": false`, so a typo or an
unannounced field fails the producer's contract test. Consumers do not validate against the schema at runtime; they read
only the fields they need. That is what lets a producer add an optional field without breaking anyone (schema first).

## Viva Notes

**Q: Why keep JSON Schemas as files if the event catalog already has them?**
A: Tests and tools need machine-readable contracts. Generating the files from the catalog keeps one source of truth, and
the check in CI fails if anyone edits one without the other.

**Q: What stops a producer from breaking a consumer?**
A: Its contract test validates every event it publishes against these strict schemas, so a removed, renamed or extra
field fails the build before it ever reaches Redpanda.

**Q: Why are the schemas strict but the consumers tolerant?**
A: Strict schemas catch producer mistakes early; tolerant consumers mean a new optional field (added to the schema first)
never breaks an older consumer. Together they make non-breaking changes safe and breaking ones visible.
