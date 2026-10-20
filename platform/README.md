# OHP — OpenHands Orchestration Platform

A production-grade control plane that turns OpenHands from a single coding agent
into a coordinated, multi-agent software-delivery system. Submit a request in
plain language; the platform classifies it, routes it to the right specialist
sub-agent, drives the conversation to completion, verifies the result and records
everything durably.

> **Status:** working and verified end to end against a live OpenHands agent
> server (v1.50.1). See [Verification](#verification).

---

## Table of contents

- [Why this exists](#why-this-exists)
- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [CLI reference](#cli-reference)
- [The sub-agent roster](#the-sub-agent-roster)
- [Configuration](#configuration)
- [Guarantees](#guarantees)
- [Verification](#verification)
- [Testing](#testing)
- [Deployment](#deployment)
- [Repository layout](#repository-layout)
- [Документация на русском](#документация-на-русском)
- [Roadmap](#roadmap)

---

## Why this exists

An OpenHands conversation is powerful but singular: it has one context window,
one role and no durable memory of what it did. Real delivery needs more — many
specialists, a queue that survives a restart, idempotent operations that never
duplicate work, and a record of every decision.

OHP is that layer. It sits on top of the OpenHands **agent server** and adds:

- a **typed task model** with an explicit lifecycle,
- a **dispatcher** that classifies, decomposes and routes work,
- a **sub-agent roster** defined as plain Markdown files,
- **durable state** in SQLite with idempotency and leasing,
- a **CLI** and a small Python API.

## What it does

| Capability | Detail |
|---|---|
| Classification | Rule-based routing to 12 task types, with priority, complexity, risk and approval gating. |
| Decomposition | Break a request into ordered sub-tasks with dependency edges. |
| Delegation | Start a real OpenHands conversation per task, in the same workspace. |
| Idempotency | A repeated submit or a retried start never creates duplicate work. |
| Resilience | Timeouts become `outcome_unknown` and are reconciled, never blind-retried. |
| Durability | Tasks, events and leases live in SQLite and survive a restart. |
| Least privilege | Each role gets only the tools and skills it needs. |
| Audit | An append-only event log for every task, with secrets redacted. |

## Architecture

```
                          ┌──────────────────────────────────────────┐
   request (text)         │                 OHP                       │
        │                 │                                          │
        ▼                 │  ┌────────────┐   ┌──────────────────┐    │
 ┌─────────────┐  submit  │  │ classifier │──▶│   dispatcher     │    │
 │   CLI / API │─────────▶│  └────────────┘   │  (state machine) │    │
 └─────────────┘          │                   └────────┬─────────┘    │
                          │        ┌───────────────┬───┴──────────┐   │
                          │        ▼               ▼              ▼   │
                          │  ┌──────────┐   ┌────────────┐  ┌─────────┐
                          │  │  store   │   │  executor  │  │  locks  │
                          │  │ (SQLite) │   │ (prompt +  │  │ (lease) │
                          │  └──────────┘   │  parsing)  │  └─────────┘
                          │                 └─────┬──────┘             │
                          └───────────────────────┼────────────────────┘
                                                  │ create / poll / final
                                                  ▼
                          ┌──────────────────────────────────────────┐
                          │        OpenHands agent server             │
                          │  conversation per task (same workspace)   │
                          │  sub-agents from .agents/agents/*.md      │
                          └──────────────────────────────────────────┘
```

The task lifecycle:

```
created ─▶ queued ─▶ running ─▶ waiting_for_agent ─▶ running ─▶ completed
   │           │         │                │                        ▲
   │           │         ├─▶ retry_wait ──┘                        │
   │           │         ├─▶ blocked                               │
   │           │         ├─▶ outcome_unknown ─▶ (reconcile) ───────┘
   ▼           ▼         ▼
waiting_for_approval   failed / cancelled
```

Full details, including the state-transition table, are in
[docs/architecture/overview.md](docs/architecture/overview.md).

## Quick start

```bash
# 1. Install (no third-party runtime dependencies).
cd platform
python -m pip install -e ".[dev]"

# 2. Point the platform at your agent server and database.
cp .env.example .env
export SESSION_API_KEY="$(cat ~/.openhands/agent-canvas/api-key.txt 2>/dev/null || echo "$SESSION_API_KEY")"
export OHP_DB="$PWD/data/platform.db"

# 3. Check readiness.
python -m ohp doctor

# 4. Register a project.
python -m ohp projects --register demo --working-dir "$PWD/scratch"

# 5. Submit and run a task.
python -m ohp submit "Add a /health endpoint that returns 200" --project demo
python -m ohp run --drain
python -m ohp status
```

Or use the Python API:

```python
from ohp import Dispatcher, Settings, Store

settings = Settings()
store = Store(settings.db_path)
dispatcher = Dispatcher(store, settings)

task, created = dispatcher.submit("Fix the login bug", project="demo")
if created:
    dispatcher.run(task.task_id)
print(store.get_task(task.task_id).status)
```

## CLI reference

| Command | Purpose |
|---|---|
| `ohp init` | Create the database. |
| `ohp doctor` | Report environment readiness and agent-server health. |
| `ohp submit "<request>" [--project P] [--subtasks JSON]` | Classify and enqueue a request. |
| `ohp run [TASK_ID] [--max N]` | Run one task or drain the queue. |
| `ohp status` | Counts by state, plus queued and blocked tasks. |
| `ohp show TASK_ID` | Full task record and its event log. |
| `ohp approve TASK_ID` | Release a task parked for approval. |
| `ohp cancel TASK_ID` | Cancel a task and interrupt its conversation. |
| `ohp recover` | Reconcile tasks in `outcome_unknown`. |
| `ohp projects [--register NAME ...]` | List or register projects. |

## The sub-agent roster

Each specialist is a Markdown file in `.agents/agents/` — the OpenHands SDK
file-based agent format. The same file is both the SDK sub-agent definition and
the role instructions the dispatcher embeds in a task prompt, so there is one
source of truth.

| Agent | Role |
|---|---|
| `orchestrator` | Decomposes and delegates; never implements. |
| `product-manager` | Turns a vague request into a spec. |
| `architect` | Designs structure and records decisions. |
| `developer` | Implements and proves changes. |
| `qa` | Adversarially verifies acceptance criteria. |
| `documenter` | Writes README, guides and docstrings. |
| `devops` | Build, CI/CD, containers, deployment. |
| `security` | Audits for vulnerabilities and leaked secrets. |
| `researcher` | Answers questions with citations. |
| `integrator` | Commits, branches and pull requests. |

Regenerate the roster with `python tools/gen_agents.py`.

## Configuration

All settings come from the environment (prefix `OHP_`). See
[`.env.example`](.env.example) and [`config/platform.yaml`](config/platform.yaml).

| Variable | Default | Meaning |
|---|---|---|
| `SESSION_API_KEY` | — | **Required.** Agent-server session key. |
| `OHP_DB` | `~/.ohp/platform.db` | SQLite database path (durable). |
| `OHP_AGENT_SERVER_URL` | `http://127.0.0.1:60000` | Agent server base URL. |
| `OHP_MODEL` | server default | Model for `inherit` tasks. |
| `OHP_MAX_PARALLEL` | `3` | Concurrent conversations. |
| `OHP_TASK_TIMEOUT` | `1800` | Per-task timeout in seconds. |
| `OHP_LEASE_SECONDS` | `600` | Task lease duration. |
| `OPENHANDS_CLOUD_API_KEY` | — | Only for cross-sandbox delegation. |

## Guarantees

- **No duplicate work.** Idempotency keys are unique in the database, and a
  task's conversation id is derived from its task id, so a retried start cannot
  create a second conversation.
- **No blind retries of irreversible steps.** A timed-out start becomes
  `outcome_unknown` and is reconciled by inspecting the conversation.
- **No secrets in logs or the database.** A redacting JSON logger masks known
  secret values and credential-shaped strings at the sink.
- **No success without evidence.** A task is `completed` only when a
  conversation reached a terminal state and a result contract was parsed.

## Verification

The platform was verified against a live OpenHands agent server v1.50.1:

```
$ python -m ohp doctor
{"agent_server": {"available": true, "version": "1.50.1"}, ...}

$ python -m ohp submit "Create a file called hello.txt containing exactly: hello" --project scratch
$ python -m ohp run task_583e...
$ cat scratch/hello.txt
hello
```

The recorded result included the changed file, the verification commands and
their output — the task was marked `completed` only after a terminal
conversation and a parsed result contract.

## Testing

```bash
python -m pytest -q        # unit + integration tests, no network
python -m ruff check ohp tests
```

Tests use an in-memory fake agent server, so they are fast and hermetic. The
live smoke test is manual (see [Verification](#verification)).

## Deployment

- **Local / single host** — run `python -m ohp run --drain` from cron or a
  systemd timer. The SQLite database is the only durable state.
- **Container** — see [docs/operations/deployment.md](docs/operations/deployment.md).
- **Scale-out** — put the database on a shared volume or Postgres, run several
  dispatchers with distinct `owner_id`s, and let the lease table serialise them.
  See [ROADMAP.md](ROADMAP.md).

## Repository layout

```
platform/
├── ohp/                  # the package
│   ├── models.py         # Task, TaskResult, Event, Project
│   ├── state_machine.py  # the transition table
│   ├── store.py          # SQLite persistence
│   ├── classifier.py     # request -> routing decision
│   ├── dispatcher.py     # the orchestration core
│   ├── executor.py       # prompt building, result parsing, retries
│   ├── agent_server.py   # agent-server and cloud clients
│   ├── config.py         # settings
│   ├── logging_setup.py  # JSON logging with redaction
│   └── __main__.py       # CLI
├── .agents/agents/       # file-based sub-agents
├── tests/                # pytest suite
├── docs/                 # architecture, operations, integrations
├── tools/gen_agents.py   # regenerate the sub-agent roster
├── config/platform.yaml  # example configuration
└── pyproject.toml
```

## Документация на русском

**OHP — платформа оркестрации OpenHands.** Это управляющий слой, который
превращает одного кодирующего агента OpenHands в согласованную
мультиагентную систему доставки кода. Вы отправляете запрос обычным текстом,
платформа классифицирует его, направляет нужному специализированному
субагенту, доводит диалог до завершения, проверяет результат и надёжно
фиксирует всё в базе данных.

**Ключевые возможности:**

- **Классификация** запроса: 12 типов задач, приоритет, сложность, риск и
  обязательное подтверждение для опасных операций.
- **Декомпозиция** большой задачи на упорядоченные подзадачи с зависимостями.
- **Идемпотентность**: повторная отправка или повторный запуск никогда не
  создают дублирующую работу.
- **Отказоустойчивость**: таймаут переводит задачу в состояние
  `outcome_unknown` и она согласуется, а не перезапускается вслепую.
- **Надёжность**: задачи, события и блокировки хранятся в SQLite и переживают
  перезапуск.
- **Минимальные привилегии**: каждой роли выдаются только нужные инструменты.
- **Аудит**: журнал событий для каждой задачи с маскированием секретов.

**Быстрый старт:** `pip install -e ".[dev]"`, задайте `SESSION_API_KEY` и
`OHP_DB`, затем `python -m ohp doctor`, `python -m ohp submit "..."`,
`python -m ohp run --drain`.

Полное описание архитектуры — в
[docs/architecture/overview.md](docs/architecture/overview.md).

## Roadmap

See [ROADMAP.md](ROADMAP.md) for planned features, scaling strategy and the
infrastructure plan.

## License

Released under the MIT License. See [LICENSE](LICENSE).
