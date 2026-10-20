# Deployment

Three supported ways to run the platform. Start with the first.

## 1. Local / single host

The dispatcher runs beside the agent server and stores state in SQLite.

```bash
python -m pip install -e .
export SESSION_API_KEY="$(cat ~/.openhands/agent-canvas/api-key.txt)"
export OHP_DB=/var/lib/ohp/platform.db
export OHP_MODEL=openhands/deepseek-v4.1-flash

python -m ohp init
python -m ohp doctor
```

Run the queue from a systemd timer:

```ini
# /etc/systemd/system/ohp-drain.service
[Unit]
Description=OHP queue drain
After=network-online.target

[Service]
Type=oneshot
User=ohp
EnvironmentFile=/etc/ohp/ohp.env
WorkingDirectory=/opt/ohp
ExecStart=/usr/bin/python3 -m ohp run --drain --max 20
```

```ini
# /etc/systemd/system/ohp-drain.timer
[Unit]
Description=Run OHP drain every minute

[Timer]
OnBootSec=30
OnUnitActiveSec=60

[Install]
WantedBy=timers.target
```

## 2. Container

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir -e .
ENV OHP_DB=/data/platform.db
VOLUME ["/data"]
ENTRYPOINT ["python", "-m", "ohp"]
CMD ["run", "--drain"]
```

```bash
docker build -t ohp:latest .
docker run --rm -it \
  -e SESSION_API_KEY \
  -e OHP_AGENT_SERVER_URL=http://host.docker.internal:60000 \
  -v ohp-data:/data \
  ohp:latest status
```

> The agent server is normally bound to `127.0.0.1`. To reach it from a
> container, either run the container with `--network host`, or run the
> dispatcher on the host and let it talk to the server directly.

## 3. Scale-out (Phase 3)

When one dispatcher is no longer enough:

1. Move the store to Postgres (implement `PostgresStore` against the same
   interface; claim rows with `SELECT ... FOR UPDATE SKIP LOCKED`).
2. Run several dispatchers with distinct `owner_id`s. The lease table already
   prevents double execution.
3. Replace polling with a queue so admission does not scan the task table.

Keep `max_parallel` conservative. Each concurrent task is a full model
conversation; parallelism multiplies token cost, not just CPU.

## Backups

Two things are not reproducible from code:

- the SQLite database (`OHP_DB`) — back it up with `sqlite3 platform.db ".backup ..."`;
- the agent server's conversation store (`/workspace/conversations`) — back up
  the directory.

## Health checks

```bash
python -m ohp doctor   # exits 0 and prints JSON; agent_server.available must be true
python -m ohp status   # queue depth, counts by state
```

Alert on:

- `agent_server.available == false`,
- a rising `outcome_unknown` count,
- a queue that only grows.
