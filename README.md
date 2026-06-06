# Distributed Task Runner

A daemon that schedules and runs isolated workloads inside Docker containers,
exposed via a REST API. Built to understand how Docker actually works — Linux
namespaces, cgroups v2, OCI image internals — not just how to use it.

## What it does

- Submit jobs (image + command + env + resource limits) via HTTP POST
- Each job runs in a dedicated Docker container with per-job CPU and memory quotas
- Live log streaming via Server-Sent Events
- Exponential backoff retry with full jitter (no thundering herd)
- Graceful shutdown — SIGTERM drains in-flight jobs before exit
- SQLite persistence with WAL mode for concurrent read/write

## Architecture

```
┌─────────────────────────────────────────────┐
│                  FastAPI                     │
│  POST /jobs  GET /jobs/:id  GET /jobs/:id/logs│
└──────────────────┬──────────────────────────┘
                   │
           JobRepository (SQLite WAL)
                   │
           JobScheduler (asyncio loop)
                   │
           ContainerManager
                   │
           Docker Engine API (unix socket)
                   │
        [job containers — siblings, not nested]
```

## Quick start

```bash
# Prerequisites: Docker running, Python 3.12+
git clone <your-repo>
cd task-runner
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run the daemon
uvicorn app.main:app --reload

# In another terminal — submit a job
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{"image": "alpine:latest", "command": ["echo", "hello"]}'

# Check status
curl http://localhost:8000/jobs/<job-id>
```

## Run with Docker

```bash
docker compose up --build
./scripts/smoke_test.sh
```

## Run tests

```bash
pytest tests/unit/ -v          # no Docker required
pytest tests/integration/ -v   # requires write access to /tmp
```

## Design decisions

See [`docs/decisions/`](docs/decisions/) for Architecture Decision Records:

- [ADR 001: SQLite WAL mode](docs/decisions/001-sqlite-wal-mode.md)
- [ADR 002: Container security hardening](docs/decisions/002-container-security.md)

## Known issues and limitations

See [GitHub Issues](#) for open bugs and planned work. Notable ones:

- **No startup reconciliation**: jobs stuck in `RUNNING` after a hard crash are
  not automatically resolved. The daemon must be restarted manually and the
  orphaned jobs marked failed. See post-mortem: `docs/post-mortem-zombie-containers.md`
- **Single-host only**: the scheduler and DB are co-located. Multi-host
  distribution would require replacing SQLite with PostgreSQL and the scheduler
  with a distributed lock (e.g. Redis SETNX).
- **No auth**: the API has no authentication. Do not expose port 8000 publicly.

## What I learned building this

- How Docker namespaces (PID, mount, net, user) actually isolate processes
- How cgroups v2 `memory.max` and `cpu.max` map to Docker's `--memory` and `--cpus` flags
- Why OOM kills are silent by default and how to detect them via exit code + inspect
- Why full jitter matters for retry storms (thundering herd)
- Why `finally` blocks are non-negotiable for resource cleanup in async code
