# Five-minute Guardrail demo script

This is the recording-ready script. Keep the terminal at readable zoom, hide `.env`
and `.secrets`, and preload Guardrail Control at <http://localhost:8501>.

## 0:00–0:35 — Problem and architecture

“Guardrail is a Snowflake-powered experimentation and progressive-delivery platform.
It answers a harder question than whether a metric moved: whether the telemetry and
evidence are trustworthy enough to change customer exposure.”

Show the system architecture, then point to the decision order: telemetry, integrity,
statistics, product guardrails, rollout, immutable audit.

## 0:35–1:20 — Metrics as code

Show `metrics/order_conversion_rate.v1.yml` and run:

```bash
uv run guardrail-metric-compile metrics/*.yml
```

Call out schema validation, registered event/entity references, parameterized SQL,
versioned definition and SQL hashes, and explicit activation.

## 1:20–2:10 — Healthy rollout

Run:

```bash
uv run guardrail-phase6-demo --scenario healthy-rollout
```

Show `RAMP`, `RUNNING_5 → RUNNING_25`, practical conversion lift, passing product
guardrails, approver metadata, and content-addressed IDs.

## 2:10–3:15 — False winner from corrupt telemetry

Open the **Health** workspace for `ranking_phase3_20260806`, or run:

```bash
uv run guardrail-phase6-demo --scenario telemetry-corruption
```

Explain that flicker and event loss invalidate the apparent winner. Show that metrics
cannot override integrity and the state becomes `PAUSED`.

## 3:15–4:10 — Local win, global harm

Run:

```bash
uv run guardrail-phase6-demo --scenario harmful-guardrail
```

Show that conversion succeeds while cancellation, payment failure, and delivery time
all exceed policy. The product-level decision is still `PAUSE`.

## 4:10–4:40 — Audit and operations

Show the **Decisions** workspace and the `CONTROL` landing table in the rollout
runbook. Explain snapshot lineage, idempotent retries, and atomic decision/transition
persistence.

## 4:40–5:00 — Cost and close

Show the 24-hour report:

```bash
uv run guardrail-phase6-cost-report --lookback-hours 24 --history-limit 500
```

Close with: “The result is an end-to-end data product where a release recommendation
is reproducible, explainable, and unable to outrun its evidence.”

## Recording checklist

- [ ] API, UI, and optional Snowflake worker are healthy.
- [ ] Offline demo returns three passing scenarios.
- [ ] No terminal, browser, or editor view exposes secrets.
- [ ] Font size and audio are legible.
- [ ] The final video is approximately five minutes.
- [ ] The video link is added to the root README.
