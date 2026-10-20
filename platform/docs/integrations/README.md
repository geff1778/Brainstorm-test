# Integrations

How to connect the platform to the outside world. Every integration must be
idempotent: retrying a delivery must not create a second task.

## The golden rule

Always pass a stable `idempotency_key` derived from the *source event*, not from
the wall-clock time. The platform will then collapse duplicate deliveries.

```python
from ohp import Dispatcher, Settings, Store

dispatcher = Dispatcher(Store(Settings().db_path), Settings())

# GitHub delivery id, or a hash of the issue id + label.
task, created = dispatcher.submit(
    issue_body,
    project="brainstorm",
    idempotency_key=f"github:issue:{issue_id}",
)
if created:
    dispatcher.run(task.task_id)
```

## GitHub

Two paths:

- **Agent-driven** (works today): the `integrator` sub-agent uses the `terminal`
  and the `GITHUB_TOKEN` to branch, commit and open a pull request.
- **Native** (roadmap): a dedicated executor using the GitHub API.

The `github` skill documents the API calls. Never put the token in a task
description; reference the environment variable instead.

## Webhooks

A minimal receiver that turns any webhook into a task:

```python
# webhook.py — run with: uvicorn webhook:app
from fastapi import FastAPI, Request

from ohp import Dispatcher, Settings, Store

app = FastAPI()
dispatcher = Dispatcher(Store(Settings().db_path), Settings())


@app.post("/hooks/{source}")
async def hook(source: str, request: Request) -> dict:
    payload = await request.json()
    # Derive a stable key from the source event, never from time.
    key = f"{source}:{payload.get('id') or payload.get('delivery')}"
    body = payload.get("issue", {}).get("body") or payload.get("text", "")
    task, created = dispatcher.submit(body, idempotency_key=key)
    return {"task_id": task.task_id, "created": created}
```

Run the drain on a timer or a worker; the webhook only admits work.

## CI/CD

Trigger a task from a failing workflow step:

```yaml
- name: Ask OHP to investigate
  if: failure()
  run: |
    python -m ohp submit "CI failed on ${{ github.sha }}: investigate and fix" \
      --project "${{ github.event.repository.name }}" \
      --idempotency-key "ci:${{ github.run_id }}"
```

## OpenHands Cloud (cross-sandbox delegation)

When work must run in its own sandbox, use the cloud client:

```python
from ohp.agent_server import CloudClient

cloud = CloudClient("https://app.all-hands.dev", api_key)
start = cloud.start_conversation(
    prompt="Implement the dashboard component.",
    repository="owner/repo",
    branch="main",
)
```

The local dispatcher remains the source of truth; the cloud conversation is
another executor.

## Slack

Post a summary when a task completes by subscribing to the event log:

```python
from ohp.models import TaskStatus

for task in store.list_tasks(status=TaskStatus.COMPLETED):
    notify_slack(f"✅ {task.title}: {task.result.summary if task.result else ''}")
```

Prefer reacting to events over polling where your transport supports it.
