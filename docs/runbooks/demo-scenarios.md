# Reproducible demo scenarios

## Fast offline acceptance

Run all scenarios without Snowflake:

```bash
uv run guardrail-phase6-demo --scenario all
```

The command runs the real statistical analyzer, product-guardrail evaluator, rollout
state machine, decision construction, and in-memory state application. Decision and
transition IDs are content-addressed and repeat identically.

| Scenario | Expected evidence path | Expected decision | Expected state |
| --- | --- | --- | --- |
| `healthy-rollout` | Integrity ready, powered conversion lift, all product guardrails pass | `RAMP` | `RUNNING_25` |
| `telemetry-corruption` | Variant flicker and event loss block analysis before metrics drive rollout | `PAUSE` | `PAUSED` |
| `harmful-guardrail` | Conversion wins, but cancellation, payment failure, and delivery time fail | `PAUSE` | `PAUSED` |

Run one story with:

```bash
uv run guardrail-phase6-demo --scenario healthy-rollout
uv run guardrail-phase6-demo --scenario telemetry-corruption
uv run guardrail-phase6-demo --scenario harmful-guardrail
```

## Live Snowflake safety story

The persisted Phase 3 acceptance dataset deliberately contains variant flicker and an
under-sized treatment arm. Reproduce the safety path with:

```bash
uv run guardrail-phase4-integrity-check ranking_phase3_20260806
uv run guardrail-phase4-analysis ranking_phase3_20260806
uv run guardrail-phase5-decision ranking_phase3_20260806 --approved-by product-owner
```

Expected output:

1. Integrity reports `BLOCKED` for flicker, assignment/exposure consistency, and event
   loss.
2. Analysis preserves health lineage but returns no metric decision evidence.
3. Rollout recommends `PAUSE`, persists one decision and one transition, and exits `2`.
4. An identical retry returns the same snapshot, decision, and transition IDs.

## UI walkthrough

1. Start the API and `guardrail-ui`.
2. In **Experiment**, create or load an experiment and inspect allocation state.
3. In **Health**, run the seven gates and explain any blocking row.
4. In **Results**, show status, power, confidence intervals, and snapshot IDs.
5. In **Decisions**, apply the approved evaluation and refresh immutable history.

Do not present treatment conversion alone as a conclusion. Start with integrity, then
power, then practical lift, then product guardrails.
