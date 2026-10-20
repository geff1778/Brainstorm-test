# Roadmap

This document covers where the platform is going, how it scales, and what
infrastructure it should run on. It is deliberately concrete: each item has a
trigger that tells you when it is worth doing.

## Principles

1. **Durable before distributed.** Do not add a broker or a second writer until
   the single-host design is actually the bottleneck.
2. **Idempotency is not optional.** Every new integration must be safe to retry.
3. **Evidence over assertion.** A feature that reports success must be able to
   show the command and its output that proves it.
4. **Least privilege.** New tools are granted per role, never globally.

## Phase 1 — Hardening (now)

- [x] Typed task model, state machine, durable store, idempotency, leasing.
- [x] Rule-based classifier and a ten-role sub-agent roster.
- [x] Resilience: `outcome_unknown` reconciliation, backoff, fatal-error
      classification.
- [x] Redacting structured logging.
- [ ] **Verification gate for irreversible actions.** Before a task that mutates
      a shared environment, require a passing verification command recorded in
      the result contract.
- [ ] **Metrics export.** Emit counters and timings in Prometheus format from
      `ohp status` and a new `/metrics` endpoint.
- [ ] **Cost accounting.** Aggregate per-task token usage and cost from
      conversation stats and surface it in `ohp status`.

*Trigger:* the first time an operator cannot answer "what did this cost?" or
"why is the queue slow?".

## Phase 2 — Multi-project and integrations

- [ ] **Git provider integrations.** Native GitHub/GitLab/Bitbucket executors:
      branch, commit, open PR, read review comments. Today this is done by the
      `integrator` sub-agent through the `terminal`.
- [ ] **Issue-to-PR pipeline.** Watch a label or webhook, create a task per
      issue, run it, open a PR and link it back.
- [ ] **Webhook receiver.** A small FastAPI service that accepts GitHub/Slack
      events and calls `dispatcher.submit` with a stable idempotency key.
- [ ] **Human-in-the-loop UI.** A read-only web view of tasks, events and
      approvals (the approval gate already exists in the model).

*Trigger:* more than one project, or the first request to "watch issues and
open PRs automatically".

## Phase 3 — Scale-out

The single-writer SQLite design is correct until it is not. Move when any of
these hold: more than ~5 dispatchers, more than ~50 tasks/minute, or a need for
high availability.

- [ ] **Postgres backend.** The `Store` interface is already the seam. Add a
      `PostgresStore` implementing the same methods; use `SELECT ... FOR UPDATE
      SKIP LOCKED` for claiming.
- [ ] **Message queue.** Replace polling with a queue (Redis Streams, SQS, NATS).
      The dispatcher becomes a consumer; `run_pending` becomes `consume`.
- [ ] **Worker pool.** Separate the API (submit/approve) from workers (execute),
      so a slow task cannot block admission.
- [ ] **Distributed tracing.** Propagate a trace id from submit through every
      conversation and tool call; export to OpenTelemetry.

*Trigger:* a dispatcher restart drops work, or admission latency grows with the
queue length.

## Phase 4 — Intelligence

- [ ] **LLM-assisted classification.** Keep the rule-based classifier as the
      fast path; fall back to a model for ambiguous requests. The
      `Classification` contract does not change.
- [ ] **Adaptive routing.** Learn from completed tasks which role succeeds most
      often for a given request shape.
- [ ] **Self-healing.** When a task fails repeatedly, spawn a `researcher` task
      to diagnose the failure and propose a fix before retrying.
- [ ] **Memory.** A vector store of past tasks and their outcomes, retrieved
      into the prompt of similar new tasks.

*Trigger:* the rule-based classifier misroutes enough requests that an operator
notices, or repeat failures have the same root cause.

## Scaling the data layer

| Stage | Storage | Why |
|---|---|---|
| Single host | SQLite (WAL) | Zero operations; one writer is fine. |
| Several workers | SQLite on a shared volume, or Postgres | Leases serialise writers; Postgres removes the single-file constraint. |
| High volume | Postgres + partitioning by month | Keeps the hot table small; events are append-only. |
| Very high volume | Postgres + object storage for artifacts | Move large blobs (diffs, logs) out of the database. |

Events are append-only and dominate growth. Plan to archive events older than
90 days to cold storage; keep the task table small.

## Infrastructure plan

- **Compute.** One small VM per environment runs the agent server; the
  dispatcher runs beside it. Scale workers horizontally, not the database
  vertically, until Phase 3.
- **State.** A persistent volume for the database and a persistent volume for
  the agent server's conversation store. Neither is reproducible from code, so
  both must be backed up.
- **Secrets.** A managed secret store (the OpenHands secret store, or your
  cloud's). Never bake secrets into images or compose files.
- **Observability.** Structured logs to a collector; metrics from Phase 1; an
  alert on `outcome_unknown` count and on queue depth.
- **Cost control.** Set `max_budget_per_task` on conversations and alert when
  the daily aggregate crosses a threshold.

## Anti-goals

- We will **not** build a general workflow engine. The state machine is
  deliberately small; complexity belongs in the tasks, not the platform.
- We will **not** auto-merge or auto-deploy. The platform prepares a reviewable
  change; a human decides to merge.
- We will **not** store secrets in the task database. Only references to
  environment variables.
