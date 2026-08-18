# Rollout operations

## Decision order

Guardrail evaluates evidence in this order:

1. Version-scoped integrity and telemetry health.
2. Statistical eligibility, confidence, and power.
3. Primary-metric practical improvement.
4. Cancellation, payment-failure, and delivery-time guardrails loaded from active
   metric artifacts.
5. Allowed state transition and human approval metadata.
6. Atomic decision and transition persistence before in-memory allocation changes.

Never bypass a failed earlier gate because a later metric appears favorable.

## Manual validation

```bash
uv run guardrail-metric-compile metrics/*.yml
uv run guardrail-phase4-integrity-check <experiment_id>
uv run guardrail-phase4-analysis <experiment_id>
uv run guardrail-phase5-decision <experiment_id> --approved-by <owner>
```

For API-driven operation:

```bash
curl http://localhost:8000/experiments/<experiment_id>/health
curl http://localhost:8000/experiments/<experiment_id>/results
curl http://localhost:8000/experiments/<experiment_id>/decisions
curl -X POST http://localhost:8000/experiments/<experiment_id>/transitions \
  -H 'Content-Type: application/json' \
  -d '{"requested_by":"guardrail-operator","approved_by":"product-owner"}'
```

## Where data lands

| Data | Snowflake location | Grain and retention |
| --- | --- | --- |
| Producer payload and delivery metadata | `RAW.PRODUCT_EVENTS` | Append-only delivery |
| Invalid contract data | `RAW.REJECTED_EVENTS` and `RAW.REPLAY_QUEUE` | Rejection/replay lineage |
| Valid canonical telemetry | `CLEAN.PRODUCT_EVENTS_DEDUPLICATED` | One row per stable event ID |
| Assignment, exposure, and attributed outcome facts | `CORE.FCT_*` | Declared event/entity grains |
| User and variant metrics | `METRICS.FCT_USER_EXPERIMENT_METRICS`, `METRICS.AGG_EXPERIMENT_RESULTS` | User/allocation and variant/allocation |
| Metric YAML plus compiled SQL | `CONTROL.METRIC_DEFINITIONS` | Metric name and immutable version |
| Integrity evidence and reports | `CONTROL.EXPERIMENT_HEALTH_SNAPSHOTS` | Content-addressed health snapshot |
| Statistical evidence and reports | `CONTROL.EXPERIMENT_ANALYSIS_SNAPSHOTS` | Content-addressed analysis snapshot |
| Rollout recommendation | `CONTROL.ROLLOUT_DECISIONS` | Content-addressed decision |
| Approved state change | `CONTROL.STATE_TRANSITIONS` | Content-addressed transition linked to decision |

Experiment configuration and current allocation state remain in the thread-safe
in-memory development store. Restarting the API clears that operational state; the
Snowflake evidence, snapshots, metric artifacts, and audit history remain persisted.

## Responding to `CONTINUE`, `PAUSE`, and `TERMINATE`

- `CONTINUE`: keep allocation unchanged. Confirm whether the cause is power, incomplete
  guardrail evidence, or practical significance before scheduling another evaluation.
- `PAUSE`: stop new treatment assignment, preserve all evidence IDs, and investigate
  the named integrity or product checks. Do not replay or edit raw rows in place.
- `TERMINATE`: treat as repeated or terminal harm. Preserve the transition and require
  a new experiment/allocation version for any future attempt.

After corrective replay, rerun reconciliation, allow Dynamic Tables to refresh, create
a new analysis snapshot, and compare IDs before seeking approval.

## Cost and query-performance review

```bash
uv run guardrail-phase6-cost-report --lookback-hours 24 --history-limit 500
```

Review failed query count, p95 elapsed time, overload queue time, bytes scanned, and
the highest estimated active-compute slices. The estimate is not a billing total: it
excludes idle time, concurrency, warehouse minimums, and non-query services. Use
Snowflake metering views with an appropriately privileged finance/operations role for
authoritative credits.
