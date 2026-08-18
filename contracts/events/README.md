# Product event contracts

Guardrail uses immutable, versioned JSON Schema contracts for producer telemetry.
Every event selects its schema with the pair `(event_name, event_version)`.

## Layout

```text
events/
├── common/product-event-envelope.v1.schema.json
├── examples/v1/<event_name>.json
└── v1/<event_name>.schema.json
```

All schemas use JSON Schema Draft 2020-12. The common envelope owns identity,
experiment, clock, source, and reserved Guardrail-lineage fields. Each event schema
then constrains its own `event_name` and `attributes` object.

## Compatibility policy

- Published schemas are immutable. A breaking or additive attribute change creates a
  new `event_version` rather than silently changing an active contract.
- Producers should validate before publishing. The public ingestion endpoint still
  accepts arbitrary JSON objects so the quarantine path can preserve and explain bad
  producer records.
- Fields prefixed with `_guardrail_` are platform-owned lineage extensions. Product
  producers must not create them.
- Event time, sent time, and received time are separate RFC 3339 timestamps.
- CI verifies the schemas themselves, all examples, the Python `EventName` catalog,
  and the Snowflake `CONFIG.EVENT_CONTRACTS` seed stay synchronized.

Run the contract suite with:

```bash
uv run pytest tests/test_event_contracts.py
```
