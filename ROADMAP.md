# 🗺️ BrainStorm — Roadmap

This document describes where the product is going, how it scales, and what
infrastructure each stage needs. It complements the architecture overview in
[README.md](README.md).

---

## 1. Product direction

BrainStorm today is a strong single-server realtime quiz. The next stages move
it from "fun self-hosted game" to "reliable platform with persistence,
observability and horizontal scale".

### Phase 1 — Hardening (now → next)

| Item | Why | Notes |
| --- | --- | --- |
| Persist rooms | A deploy currently drops every live game | SQLite/Postgres snapshot + restore on boot |
| Structured JSON logs | Logs are human-formatted only | `LOG_FORMAT=json` switch; ship to a collector |
| Metrics | No visibility into load or AI latency | `/metrics` (Prometheus): rooms, players, answers/sec, GigaChat latency/error rate |
| Sentry-style error capture | Exceptions are only logged | One DSN env var, capture in the error handlers |
| Health depth | `/health` is shallow | Split liveness (`/livez`) from readiness (DB + AI reachability) |

### Phase 2 — Accounts & progression

| Item | Why |
| --- | --- |
| Email/verification + password reset | Accounts currently have no recovery path |
| OAuth (Google/Yandex) | Lower friction for classroom use |
| Server-side cosmetics inventory | Shop purchases should persist as owned items, not local state |
| Seasons & ranked ladder | Give the leaderboard a recurring goal |
| Daily quests | Retention loop built on existing XP/coins |

### Phase 3 — Content & social

| Item | Why |
| --- | --- |
| Public question packs per topic | Reuse curated content instead of regenerating |
| Author profiles & collections | UGC authors get credit and followers |
| Replay/history viewer | Turn `room_history` into a shareable recap |
| Teams/classroom mode | Teacher dashboard: assign a quiz, see per-student results |
| Localisation (EN/ES) | The UI is Russian-first today |

### Phase 4 — Platform

| Item | Why |
| --- | --- |
| Plugin hooks for game modes | New modes without touching the core loop |
| Public REST API + webhooks | Embed quizzes in LMS/other apps |
| Mobile apps (thin wrappers) | Push notifications for "your friend started a game" |
| AI providers abstraction | GigaChat today; allow OpenAI/Anthropic/local models behind one interface |

---

## 2. Scaling the backend

The current design keeps **all room state in one process's memory** and runs a
**single eventlet worker**. That is correct and fast for a small footprint, and
it is the first thing to change under load.

### Stage A — Single instance (today)

```
Clients ─▶ Gunicorn (1 eventlet worker) ─▶ in-memory rooms + SQLite
```

- Good to roughly a few hundred concurrent players on one small instance.
- Bottlenecks: one CPU core, GigaChat latency, SQLite write contention.

**Quick wins before adding infrastructure**

1. Move every blocking call off the event loop (`requests`/GigaChat) into
   `eventlet.tpool` or a thread pool so slow AI calls do not stall the loop.
2. Cache AI results harder (the TTL cache exists — extend it to hints).
3. Batch leaderboard writes; they are already fire-and-forget.

### Stage B — Multiple workers, shared state

```
Clients ─▶ Load balancer (sticky) ─▶ N eventlet workers
                    │                        │
                    └──────── Redis ─────────┘
                    rooms, pub/sub, rate limits, sessions
```

Required changes:

| Concern | Change |
| --- | --- |
| Room state | Serialise `Room` to Redis; a pub/sub channel fans events to all workers |
| Socket.IO fan-out | `message_queue=` Redis adapter (Flask-SocketIO supports this) |
| Sticky sessions | Required so a client's websocket stays on one worker |
| Rate limiting | Move `RateLimiter` counters to Redis (atomic `INCR` + `EXPIRE`) |
| Cache | Share the TTL cache via Redis |
| Scheduler | Leader election or a Redis lock so background tasks run once |

### Stage C — Horizontal, stateless

```
CDN (static) ─▶ Ingress ─▶ Autoscaling group of app pods
                              │        │
                        Postgres    Redis (cluster)
                        (durable)   (ephemeral state + pub/sub)
                              │
                       Object storage (avatars, exports)
```

- **Postgres** replaces SQLite for durable data (accounts, UGC, history,
  campaign). SQLite stays viable for single-instance deploys.
- **Redis** holds ephemeral state only (rooms, presence, rate limits).
- **Autoscaling** on CPU + websocket count; sticky sessions or a Redis-backed
  presence layer.
- **Read replicas** for leaderboard/reporting queries.

### Data & capacity notes

- Rooms are small (a few KB); thousands fit in memory. The real cost is the
  number of open websockets and AI calls per second.
- Cap concurrency against GigaChat with a semaphore + queue so bursts degrade
  gracefully into the fallback bank instead of timing out.
- Partition `room_history` by month once it grows; it is append-only.
- Indexes already exist for the hot leaderboard/UGC paths (`app/db.py`).

---

## 3. Reliability & operations

| Area | Target |
| --- | --- |
| Deploys | Blue/green or rolling with a drain step so live games finish |
| Backups | Nightly `leaderboard.db`/`user_data.db` (or Postgres PITR) snapshots |
| Migrations | Introduce Alembic once Postgres lands; keep the current idempotent SQLite bootstrap for dev |
| Load testing | Locust/k6 scenario: create room, 8 players, 20 questions, chat |
| SLOs | API p95 < 200 ms; AI p95 < 6 s; websocket connect < 1 s |
| Chaos | Kill a worker mid-game; assert reconnect + state recovery |

---

## 4. Security roadmap

Already done: hashed passwords with per-password salts and legacy-hash upgrade,
constant-time secret comparison, session hardening, rate limits on auth/AI,
production fail-fast config validation, escaped XSS sinks, untracked `.env`.

Next:

- CSRF tokens for state-changing REST calls (cookies are `SameSite=Lax` today).
- Per-room moderator roles instead of the global admin/cheat split.
- Audit log for admin actions (kick/ban/reset) with actor + timestamp.
- Content moderation hook for UGC and chat (profanity filter exists; add review).
- Secret manager (Vault/SSM) instead of `.env` for multi-instance deploys.

---

## 5. Testing roadmap

Current: 179 unit/integration tests covering security, validation, domain,
services, REST and the full Socket.IO flow (including cheat/admin guards).

Next:

- Load/soak tests for concurrent rooms.
- Contract tests asserting every client event has a handler and vice versa
  (the invariant already exists in `tests/test_realtime.py`; extend it to
  payload shapes).
- Frontend tests (Playwright) for the join → play → results flow.
- Fuzz the AI JSON parser with malformed GigaChat responses.
- Coverage gate in CI (fail under 60%).

---

## 6. Milestones

| Milestone | Contents |
| --- | --- |
| **v2.1** | Persist rooms, JSON logs, `/metrics`, deeper health checks |
| **v2.2** | Redis-backed state + Socket.IO message queue, sticky multi-worker deploy |
| **v2.3** | Email/OAuth accounts, persistent cosmetics, seasons |
| **v3.0** | Postgres, autoscaling, public API, plugin game modes |
