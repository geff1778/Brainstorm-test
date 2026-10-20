# Architecture

This document explains how the platform is put together and why. Read it before
changing the state machine, the store or the dispatcher.

## Layers

The code follows a strict one-way dependency direction:

```
        CLI (__main__)  ─┐
                         ├─▶  Dispatcher  ─▶  Classifier
        Python API       ┘        │
                                  ├─▶  Executor   (prompt, parsing, retry)
                                  ├─▶  AgentServerClient / CloudClient
                                  └─▶  Store  ─▶  StateMachine  ─▶  Models
```

- **Models** know nothing about storage or HTTP. They are plain dataclasses.
- **StateMachine** knows the legal transitions; it has no side effects.
- **Store** is the only component that touches SQLite.
- **Classifier** and **Executor** are pure functions over models.
- **Dispatcher** is the only component that orchestrates; it is the seam where
  policy lives.
- **CLI/API** are thin adapters.

Because dependencies point inward, the store, the classifier and the executor
are all unit-testable without a network or a database.

## The task model

A `Task` is the unit of work. The fields that matter most:

| Field | Why it exists |
|---|---|
| `task_id` | Stable identity; also the seed for the conversation id. |
| `idempotency_key` | Unique in the database; the arbiter against duplicates. |
| `parent_task_id` | Links a decomposed sub-task to its parent. |
| `dependencies` | Ordered sub-tasks; a task is blocked until these complete. |
| `attempts` / `max_attempts` | Bounds retries. |
| `conversation_id` | The OpenHands conversation that executes the task. |
| `result` | The parsed result contract, or `None`. |

## The state machine

Transitions are declared in `state_machine.py` as an explicit table. Every
change goes through `Store.set_status`, which validates the edge and the actor.
Anything not in the table raises `IllegalTransition`.

```
created ──enqueue──▶ queued ──start──▶ running ──await_agent──▶ waiting_for_agent
   │                    │                  │                            │
   │ require_approval   │ cancel           │ complete/fail              │ agent_ready
   ▼                    ▼                  ▼                            ▼
waiting_for_approval  cancelled      completed / failed ◀──────────  running
   │ approve                            ▲
   ▼                                    │ reconcile_completed
 queued                          outcome_unknown ◀── timeout / crash
```

Key decisions:

- **`outcome_unknown` is a first-class state.** It means "we sent a request and
  do not know the result". The only exits are reconciliation, never a blind
  retry. This is what makes the platform safe around non-idempotent external
  calls.
- **Approval is a state, not a flag.** A high-risk task parks in
  `waiting_for_approval` and cannot run until an operator moves it to `queued`.
- **`blocked` is distinct from `failed`.** A task blocked on dependencies is not
  an error; it will be unblocked when its dependencies complete.

## Idempotency

Two independent mechanisms:

1. **Submission.** `Task.idempotency_key` is `UNIQUE`. `Store.create_task`
   returns the existing task when the key collides, so a retried webhook or a
   double-click cannot create two tasks. The key is either supplied by the
   caller or derived deterministically from the normalised request.
2. **Execution.** The conversation id is `uuid5(namespace, task_id)`
   (`conversation_id_for_task`). The agent server accepts a client-supplied
   conversation id, so a POST that timed out can be safely retried: the retry
   either creates the one conversation with that id or returns `409`.

## Concurrency

- The store runs in WAL mode with a 30-second busy timeout and `BEGIN IMMEDIATE`
  transactions, so concurrent writers serialise instead of corrupting.
- `Store.claim_task` takes an exclusive, expiring lease on a task. Two
  dispatchers with different `owner_id`s cannot run the same task; a crashed
  dispatcher's lease expires and can be stolen.
- `Dispatcher.run_pending` executes tasks in a thread pool capped at
  `max_parallel`. Each conversation is a separate HTTP client, so there is no
  shared mutable state between workers.

## Result contract

Every executor must return a `TaskResult`. The dispatcher parses it from the
agent's final message by looking for the last fenced JSON object, falling back to
the last bare `{...}`, and finally to the raw text as a summary. The contract is
also embedded verbatim in the prompt, so the agent is told exactly what to emit.

## Error handling

`executor.classify_error` sorts failures into three buckets:

| Bucket | Examples | Action |
|---|---|---|
| `retryable` | timeout, 502/503, rate limit, connection reset | backoff, then requeue up to `max_attempts` |
| `fatal` | 401/403, invalid key, not found | fail immediately |
| `unknown` | anything else | treat as fatal after retries |

A start that *times out* is different from a start that is *rejected*: the
timeout parks the task in `outcome_unknown`, a rejection fails it.

## Security

- Secrets are read from the environment and never persisted.
- `logging_setup.redact` masks known secret values and credential-shaped
  patterns at the logging sink.
- Sub-agents run with `never_confirm` only for read-mostly roles; the
  `integrator` (which pushes) uses `confirm_risky`.
- Tools are granted per role in `classifier.ROLE_TOOLS`.

## Extending the platform

| Goal | Where to change |
|---|---|
| New task type | `models.TaskType`, `classifier._TYPE_KEYWORDS`, `classifier._ROLE_FOR_TYPE` |
| New role | `classifier.ROLE_TOOLS`, `ROLE_SKILLS`, and a new `.agents/agents/<role>.md` |
| New state | `state_machine.TRANSITIONS` and a handler in the dispatcher |
| New store backend | implement the `Store` interface; keep the method names |
| New integration | a client like `agent_server.py`, called from the dispatcher |
