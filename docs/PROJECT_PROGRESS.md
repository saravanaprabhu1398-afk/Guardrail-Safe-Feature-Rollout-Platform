# Guardrail Project Progress Tracker

**Last updated:** 2026-08-18
**Overall completion:** 35 of 50 planned delivery items (70%)
**Current focus:** Phase 4 reproducible analysis snapshots
**Delivery status:** On track; no active implementation blocker

This tracker records delivered outcomes, verification evidence, remaining scope,
and risks. The detailed product and architecture specification remains in the
[project README](../README.md).

## Delivery dashboard

| Phase | Status | Completed | Progress | Exit status | Next action |
| --- | --- | ---: | ---: | --- | --- |
| 0 — Product and system design | **Complete** | 7/7 | 100% | Architecture, executable contracts, and consequential decisions are versioned. | Keep contracts and ADRs synchronized as the platform evolves. |
| 1 — Simulator and assignment | **Complete** | 7/7 | 100% | Local API, simulator, and acknowledged Redpanda transport pass. | Preserve the transport acceptance as a regression gate. |
| 2 — Snowflake ingestion and raw layer | **Complete** | 8/8 | 100% | The complete ingestion, quarantine, replay, and reconciliation loop is live. | Preserve the acceptance evidence as a regression test. |
| 3 — Experiment models and metrics | **Complete** | 7/7 | 100% | Live dbt build, freshness, and analytical smoke gates pass. | Preserve the Phase 3 acceptance as a regression gate. |
| 4 — Integrity and statistics | **In progress** | 6/7 | 86% | Integrity, statistical tests, confidence intervals, and power gates are live. | Persist reproducible analysis snapshots. |
| 5 — Metrics and rollout automation | Not started | 0/7 | 0% | — | Depends on integrity and analysis results. |
| 6 — UI and portfolio packaging | Not started | 0/7 | 0% | — | Depends on the end-to-end decision loop. |
| **Total** | **In progress** | **35/50** | **70%** | Phase 4 now blocks both corrupt and underpowered results. | Complete reproducible analysis persistence. |

## Latest shipped milestone

### Phase 4 confidence intervals and power gates

Delivered on 2026-08-18:

- [x] Added two-sided 95% Wilson-score-based difference intervals for conversion.
- [x] Added two-sided 95% Student-t confidence intervals for Welch delivery-time
  mean differences using the calculated unequal-variance degrees of freedom.
- [x] Defined default minimum detectable effects of five percentage points for
  conversion and five minutes for delivery time at 80% target power.
- [x] Calculated normal-approximation control and treatment sample requirements using
  the configured allocation ratio rather than assuming a balanced experiment.
- [x] Reported estimated power and whether both per-variant requirements are met.
- [x] Added explicit metric and experiment `UNDERPOWERED` states. Descriptive
  estimates, intervals, and p-values remain inspectable while decisions stay blocked.
- [x] Preserved robust conversion intervals and sample planning when sparse binary
  cells make the pooled z-test itself ineligible.
- [x] Added balanced, 95/5 allocation, sparse-data, complete, and underpowered tests.

Live diagnostic evidence for `ranking_phase3_20260806`:

| Metric | 95% effect interval | Estimated power | Required control / treatment | Result |
| --- | ---: | ---: | ---: | --- |
| Conversion rate | -13.37 to +54.52 percentage points | 6.05% | 14,744 / 776 | Insufficient binary cells |
| Delivery minutes | -15.18 to +13.13 minutes | 15.15% | 848 / 45 | Underpowered |

These diagnostic calculations deliberately bypassed the health gate only to validate
the live Snowflake summaries. The supported CLI and API continue to return `BLOCKED`
with no metrics for this known-corrupt experiment. A healthy, sufficiently powered
live experiment remains necessary to demonstrate the complete path.

### Phase 4 integrity-gated statistical analysis

Delivered on 2026-08-18:

- [x] Added typed, version-scoped statistical evidence and metric/result contracts.
- [x] Implemented a two-sided pooled two-proportion z-test for conversion metrics,
  including a minimum of five successes and failures in each variant.
- [x] Implemented a two-sided Welch unequal-variance t-test for delivery minutes from
  Snowflake count, mean, and sample-variance summaries.
- [x] Reported treatment-minus-control absolute and relative lift, standard error,
  test statistic, degrees of freedom where applicable, and p-value.
- [x] Short-circuited statistical evidence retrieval whenever a required integrity
  check fails or is inconclusive; blocked reports contain no metric statistics.
- [x] Implemented `GET /experiments/{experiment_id}/results` and the executable
  `guardrail-phase4-analysis` command with a nonzero blocked-result exit code.
- [x] Added complete, partial, insufficient-data, provider-failure, and
  integrity-short-circuit regression cases.

Live analysis evidence for `ranking_phase3_20260806`:

| Check | Observed | Result |
| --- | ---: | --- |
| Integrity gate | Flicker, exposure-consistency, and event-loss checks block | Statistics correctly withheld |
| Analysis CLI | `BLOCKED`; empty metric list | Exit status 2 |
| Conversion evidence query | 61/185 control; 5/9 treatment | Query validated; not interpreted |
| Delivery evidence query | 56 control; 5 treatment observations | Query validated; not interpreted |

The healthy analysis path is covered with deterministic fixtures. A new healthy live
experiment is still required to demonstrate a complete Snowflake-backed result.

### Phase 4 telemetry-health gates

Delivered on 2026-08-18:

- [x] Extended version-scoped evidence across raw deliveries, canonical events,
  assignments, exposures, and user-level experiment facts.
- [x] Added blocking event-loss checks with a 5% per-variant ceiling, 3 percentage
  point cross-variant skew ceiling, and a 20-assignment minimum per variant.
- [x] Added raw duplicate-delivery detection with a 5% operational threshold. A
  breach is visible but non-blocking because stable `event_id` deduplication protects
  the analytical facts.
- [x] Added blocking late-data checks with a 5% ceiling in each variant.
- [x] Added blocking freshness-skew detection using the absolute difference between
  control and treatment p95 event-to-receive latency, capped at 120 seconds.
- [x] Extended the CLI and experiment-health API from three to seven structured checks.
- [x] Restored network-policy-compliant Snowflake access and ran the complete live
  evidence query against the Phase 3 acceptance experiment.

Live telemetry-health evidence for `ranking_phase3_20260806`:

| Check | Observed | Result |
| --- | ---: | --- |
| Sample-ratio mismatch | p = 0.8176 | Pass |
| Variant flicker | 3 / 194 users | Correctly blocked |
| Invalid exposure consistency | 3 / 197 exposures | Correctly blocked |
| Event loss | 191 control / 9 treatment assignments | Correctly blocked as insufficient |
| Raw duplicate delivery | 28 / 953 deliveries (2.94%) | Pass |
| Late data | 3.43% control / 1.96% treatment | Pass |
| Variant freshness skew | 0 seconds at p95 | Pass |

The overall report is correctly blocked by known flicker and variant-mismatch faults.
A new healthy live experiment is still required to demonstrate the all-pass path.

### Phase 4 allocation and consistency integrity gates

Delivered on 2026-08-18:

- [x] Added typed, version-scoped integrity evidence and structured check results.
- [x] Implemented Pearson sample-ratio mismatch detection against the configured
  control/treatment allocation, using eligible exposed users as the analysis sample.
- [x] Required at least five expected users per allocation cell and blocked analysis
  with `INSUFFICIENT_DATA` instead of applying the chi-square approximation outside
  its configured validity gate.
- [x] Added blocking checks for exposure variant flicker, assignment variant flicker,
  orphan exposures, assignment/variant mismatches, and excessive assignment/exposure
  gaps.
- [x] Added a Snowflake evidence repository scoped by experiment and allocation
  version, including time-bounded attribution of orphan exposures.
- [x] Implemented `GET /experiments/{experiment_id}/health` and an executable
  `guardrail-phase4-integrity-check` command with a nonzero blocked-result exit code.
- [x] Added healthy, insufficient-data, and deliberately corrupted regression cases.

Local integrity acceptance evidence:

| Check | Observed | Result |
| --- | ---: | --- |
| Healthy configured 95/5 exposure split | 950 / 50 | Pass |
| Deliberate sample-ratio mismatch | 850 / 150 | Correctly blocked |
| Expected treatment cell below five users | 1 | Correctly blocked as insufficient |
| Variant-flicker fault | 2 users | Correctly blocked |
| Orphan, variant-mismatch, and assignment-flicker faults | 1 each | Correctly blocked |
| Treatment event-loss rate above 5% | 20% | Correctly blocked |

The previous Phase 3 dataset intentionally contains three flicker users and only nine
eligible treatment users. The live telemetry-health milestone above records the now
successful Snowflake execution and expected blocked result.

### Phase 3 experiment measurement layer

Delivered on 2026-08-06:

- [x] Added dbt Core and `dbt-snowflake` 1.12 with native PAT authentication and a
  credential-safe repository wrapper.
- [x] Added a typed staging view over canonical deduplicated telemetry.
- [x] Built event-grain assignment, exposure, outcome, and attributed-outcome facts.
- [x] Matched each exposure to its latest preceding assignment while retaining
  orphan and variant-mismatched exposure evidence.
- [x] Implemented most-recent-touch, 24-hour event-time outcome attribution.
- [x] Added user identity and session-grain models.
- [x] Added one-row-per-user experiment facts and variant-level KPI aggregations.
- [x] Materialized six `CORE` and two `METRICS` models as five-minute Snowflake
  Dynamic Tables.
- [x] Added source freshness plus uniqueness, null, relationship, accepted-value,
  attribution-window, eligibility, bounds, and non-negative metric tests.
- [x] Added an executable Phase 3 smoke gate and offline dbt parsing to CI.

Live analytics acceptance evidence:

| Check | Observed | Result |
| --- | ---: | --- |
| Persisted simulator events | 953 raw / 925 unique | Pass |
| Staged canonical events including earlier smoke data | 931 | Pass |
| Assignments | 200 | Pass |
| Total / eligible exposures | 203 / 194 | Pass |
| Product / attributed outcomes | 135 / 135 | Pass |
| User-experiment metric rows | 194 | Pass |
| Experiment-variant result rows | 2 | Pass |
| Preserved variant-flicker users | 3 | Pass |
| Snowflake Dynamic Tables | 8 | Pass |
| dbt build nodes | 83 passed, 0 warnings/errors | Pass |
| Source freshness | Passed | Pass |
| Analytical invariant violations | 0 | Pass |

Acceptance experiment: `ranking_phase3_20260806`
Transport batch: `7a82bbcb-d572-4ee4-b8fc-a8a8f4772540`

The observed treatment conversion rate is intentionally not a rollout conclusion:
the 5% treatment allocation produced only nine eligible treatment users. Phase 4's
minimum-sample and statistical gates must block premature interpretation.

### Phase 0 contract and decision closeout

Delivered on 2026-08-06:

- [x] Added a reusable JSON Schema Draft 2020-12 product-event envelope.
- [x] Added strict schemas and valid examples for all nine version-1 events.
- [x] Aligned simulator attributes with the published event catalog.
- [x] Added compatibility tests that prevent drift between JSON Schema, examples,
  Pydantic models, simulator output, and Snowflake contract seeds.
- [x] Recorded accepted ADRs for stable assignment identity, effectively-once
  analytics, append-only raw retention, and source-offset commit semantics.
- [x] Added GitHub Actions jobs for contract validation, formatting, lint, tests,
  Compose validation, and production-container build.
- [x] Pinned third-party workflow actions to immutable release commits and restricted
  workflow permissions to read-only repository content.

Contract evidence:

| Check | Expected | Observed | Result |
| --- | ---: | ---: | --- |
| Version-1 event schemas | 9 | 9 | Pass |
| Version-1 valid examples | 9 | 9 | Pass |
| Contract representations synchronized | 4 | 4 | Pass |
| Contract-focused tests | 32 | 32 passed | Pass |
| Accepted architecture decisions | 4 | 4 | Pass |

### Phase 1 Redpanda transport closeout

Delivered on 2026-08-06:

- [x] Added a single-node local Redpanda service and Redpanda Console to Docker
  Compose.
- [x] Provisioned the versioned `guardrail.product-events.v1` topic with three
  partitions.
- [x] Added broker-acknowledged publishing with idempotence enabled and exact
  topic/partition/offset receipts.
- [x] Routed `POST /events` and persisted simulator telemetry through a selectable
  Redpanda or direct-Snowpipe dispatcher.
- [x] Added a manual-commit sink worker that preserves transport envelope, partition,
  and offset metadata in the raw Snowflake payload.
- [x] Prevented consumer offset advancement when Snowflake publication fails or
  reconciliation is unbalanced.

Live transport acceptance evidence:

| Check | Expected | Observed | Result |
| --- | ---: | ---: | --- |
| Topic partitions | 3 | 3 | Pass |
| Broker-acknowledged records | 3 | 3 | Pass |
| Worker-consumed records | 3 | 3 | Pass |
| Snowflake raw records | 3 | 3 | Pass |
| Contract-valid records | 2 | 2 | Pass |
| Quarantined records | 1 | 1 | Pass |
| Duplicate records | 1 | 1 | Pass |
| Source-lineage rows | 3 | 3 | Pass |
| Reconciliation balanced | `true` | `true` | Pass |

Transport batch: `e44a9cfa-ebc1-4f7c-bafe-7a5c0b860ed4`
Snowflake batch: `29d59148-0dae-4c3f-8617-00e546992d6c`

### Phase 2 streaming-ingestion vertical slice

Delivered on 2026-08-06:

- [x] Provisioned the `GUARDRAIL` database, layered schemas, development roles,
  two X-Small warehouses, and a 20-credit resource monitor.
- [x] Connected the application using a PAT restricted to `GUARDRAIL_ENGINEER`.
- [x] Registered nine versioned product-event contracts.
- [x] Created the high-performance Snowpipe Streaming pipe and deterministic
  local development channel.
- [x] Preserved immutable raw JSON payloads, canonical hashes, batch IDs, and
  stream offsets.
- [x] Added contract validation and structured quarantine reasons.
- [x] Added clean-layer `event_id` deduplication.
- [x] Added batch audit and producer-to-clean reconciliation evidence.
- [x] Added `POST /events` and `persist_events: true` simulation support.
- [x] Added corrected-event validation, deterministic replay identity, queue state
  transitions, rejection lineage, and idempotent completed retries.
- [x] Added rejection lookup and replay API endpoints.

Live acceptance evidence:

| Check | Expected | Observed | Result |
| --- | ---: | ---: | --- |
| Producer records | 3 | 3 | Pass |
| Append-only raw records | 3 | 3 | Pass |
| Contract-valid records | 2 | 2 | Pass |
| Quarantined records | 1 | 1 | Pass |
| Duplicate records | 1 | 1 | Pass |
| Reconciliation balanced | `true` | `true` | Pass |
| Rejection reason | `MISSING_EVENT_ID` | `MISSING_EVENT_ID` | Pass |

Acceptance batch: `3161e62b-074f-45ca-bc21-441c8f27e7cd`

Replay acceptance evidence:

| Check | Observed | Result |
| --- | --- | --- |
| Replay state | `COMPLETED` | Pass |
| Attempts after two identical requests | 1 | Pass |
| Raw replay rows | 1 | Pass |
| Rejection lineage | Original rejection ID preserved | Pass |
| Reconciliation | Balanced | Pass |
| Identical completed retry | Original replay and batch returned | Pass |

Replay ID: `0ca162e2-2237-5c7a-8907-4ae21ab0b27b`
Replay batch: `7dd00d5e-abc3-4019-88ec-208851ea7884`

## Verification status

| Verification | Current result | Command or evidence |
| --- | --- | --- |
| Complete Python test suite | 97 passed | `uv run pytest` |
| Phase 4 integrity regression suite | Passed; healthy data accepted and corrupt data blocked | `uv run pytest tests/test_integrity.py` |
| Phase 4 statistical regression suite | Passed; complete, partial, sparse, underpowered, unequal-allocation, and integrity-blocked paths covered | `uv run pytest tests/test_analysis.py` |
| Live Phase 4 integrity report | Seven checks evaluated; known faulty dataset correctly blocked | `uv run guardrail-phase4-integrity-check ranking_phase3_20260806` |
| Live Phase 4 analysis report | Integrity faults withheld statistics and returned exit status 2 | `uv run guardrail-phase4-analysis ranking_phase3_20260806` |
| Contract compatibility tests | 32 passed | `uv run pytest tests/test_event_contracts.py` |
| Python lint | Passed | `uv run ruff check .` |
| Python formatting | Passed | `uv run ruff format --check .` |
| Container build and API health | Passed | `docker compose up -d --build guardrail-api` and `GET /health` |
| Snowflake PAT connection | Passed | `uv run guardrail-snowflake-check` |
| Phase 2 DDL deployment | 19 statements applied | `uv run guardrail-snowflake-setup-ingestion` |
| Live streaming acceptance | Passed and balanced | `uv run guardrail-phase2-smoke` |
| Live replay acceptance | Passed, balanced, and idempotent | `uv run guardrail-phase2-replay-smoke` |
| Redpanda broker and topic | Healthy; three partitions | `docker compose ps` and `rpk topic describe` |
| Live Redpanda-to-Snowflake acceptance | Passed, balanced, and source offsets preserved | `uv run guardrail-redpanda-smoke` |
| dbt connection and compile | dbt 1.12; PAT connection and 9-model DAG passed | `uv run guardrail-dbt debug` and `uv run guardrail-dbt compile` |
| Live dbt build | 83/83 nodes passed; 8 Dynamic Tables and 1 view | `uv run guardrail-dbt build --full-refresh` |
| dbt source freshness | Passed | `uv run guardrail-dbt source freshness` |
| Phase 3 analytics acceptance | Passed; non-empty facts and 0 invariant violations | `uv run guardrail-phase3-smoke` |
| CI workflow definition | Added; contracts, offline dbt parse, quality, Compose, and container build | `.github/workflows/ci.yml` |

## Active work board

### Now — Phase 4 statistical completion

- [x] Implement sample-ratio mismatch detection.
- [x] Detect variant flicker and assignment/exposure inconsistencies.
- [x] Detect freshness skew, event loss, duplicates, and late data.
- [x] Implement proportion and continuous metric analysis.
- [x] Block statistical decisions when required integrity checks fail.
- [x] Add confidence intervals and allocation-aware sample-size requirements.
- [ ] Persist reproducible health and analysis snapshots.

## Risks and decisions requiring attention

| Risk or debt | Impact | Mitigation | Status |
| --- | --- | --- | --- |
| Local Redpanda runs as one development broker. | It does not demonstrate production availability or replication. | Keep single-node Compose for reproducibility; document a replicated/managed deployment topology before production. | Accepted for local development |
| Batch classification runs synchronously in the sink worker after stream commit. | A slow warehouse batch occupies a consumer and can increase source lag. | Keep processing idempotent and offset-safe; later move classification to an observable Snowflake task graph. | Accepted in ADR 0004 |
| Local development uses one deterministic streaming channel. | Multiple producer instances could contend for offsets. | Derive channel names from environment, producer instance, and source partition. | Required before scaling |
| Replay execution is synchronous for the local MVP. | A long-running provider failure holds the request open. | Move replay execution behind an asynchronous job while retaining the same deterministic replay ID and status model. | Deferred hardening |
| Full JSON Schema validation currently runs during contract development and CI, while ingestion uses Pydantic envelope checks or Snowflake required-field checks. | An event-specific type violation can reach quarantine logic without the complete schema explanation. | Integrate the versioned schema catalog into ingestion validation while preserving arbitrary raw payloads. | Deferred ingestion hardening |
| Five join/window-heavy Dynamic Tables currently select Snowflake `FULL` refresh under `AUTO`; three simpler facts use `INCREMENTAL`. | Full refresh cost will grow with event history. | Profile refresh history and refactor unsupported operators or split models before scaling data volume. | Accepted for the current synthetic scale |
| The Phase 3 acceptance treatment arm has only nine eligible users. | The apparent conversion lift is statistically unreliable. | Integrity, sparse-cell, interval, and 80%-power gates now block interpretation until enough valid treatment evidence exists. | Intentionally blocked from interpretation |

## Phase 3 exit gate

Phase 3 is complete only when all of the following are demonstrated together:

- [x] Assignment, exposure, outcome, identity, and session grains are explicit.
- [x] Ineligible exposure records remain observable but cannot enter metrics.
- [x] Outcomes are attributed with event time inside the 24-hour window.
- [x] User and variant facts answer conversion and guardrail questions.
- [x] Snowflake maintains the analytical DAG as Dynamic Tables.
- [x] dbt build, source freshness, and live analytical acceptance all pass.

## Phase 2 exit gate

Phase 2 is complete only when all of the following are demonstrated together:

- [x] Valid and invalid records are separated.
- [x] Duplicate raw delivery does not duplicate the clean analytical event.
- [x] Producer, raw, validated, rejected, and duplicate counts reconcile.
- [x] Stream offsets resume safely across producer restarts.
- [x] A corrected quarantined record can be replayed with full lineage.
- [x] The replay path is idempotent when the same correction is retried.

## Update protocol

Update this file whenever a milestone changes state:

1. Change the date, completed-item count, and percentages.
2. Move finished work into the latest shipped milestone.
3. Record reproducible verification commands and observed results.
4. Update the active work board and next milestone.
5. Close, downgrade, or add risks based on current evidence.
6. Keep the matching phase checklist in `README.md` synchronized.
