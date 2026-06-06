# Post-mortem: zombie container leak on worker crash

**Date:** Week 4  
**Severity:** High (data loss, resource leak)  
**Status:** Resolved — see commit `fix: add deferred cleanup and signal traps`

## What happened

During local development, killing the task runner with `ctrl-C` while a job was
running left the job's container alive on the host. The container kept running
indefinitely because Docker has no knowledge that we started it — it's just a
process. On the next daemon start, the job was stuck in `RUNNING` status in the DB
with no way to reconcile it.

After a testing session of 30 minutes, `docker ps` showed 11 orphaned containers.

## Root cause

The `_execute_attempt()` method in the scheduler created a container and started it,
but the cleanup (`remove_container`) was only in the success and failure branches:

```python
# BUGGY VERSION — cleanup not in finally block
container_id = await self._create_and_start(job)
result = await self._wait(container_id)
if result.succeeded:
    self._containers.remove_container(container_id)  # only here
```

When `ctrl-C` sent `SIGINT`, the coroutine was cancelled mid-execution. The cleanup
line was never reached. The container lived on.

## Fix

Two changes:

**1. Move cleanup into a `finally` block:**
```python
try:
    container_id = await self._create_and_start(job)
    result = await self._wait(container_id)
finally:
    if container_id:
        self._containers.remove_container(container_id, force=True)
```

`finally` runs even on `asyncio.CancelledError`, ensuring cleanup happens
regardless of how the coroutine exits.

**2. Register signal handlers for graceful shutdown:**
```python
loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(self.shutdown()))
loop.add_signal_handler(signal.SIGINT,  lambda: asyncio.create_task(self.shutdown()))
```

`shutdown()` cancels in-flight tasks after a grace period. The `finally` blocks
in each task do the container cleanup before the task exits.

## What we didn't fix (known limitation)

If the process is killed with `SIGKILL` (kill -9) or the host crashes, there is no
opportunity to run cleanup. A production deployment should have a reconciliation
task that runs on startup: scan the DB for jobs stuck in `RUNNING`, inspect whether
their `container_id` still exists in Docker, and mark them `FAILED` if the container
is gone.

This is tracked as a GitHub issue: "feat: startup reconciliation for orphaned jobs".

## Lesson

Cleanup in distributed systems must be in `finally` blocks or equivalent. It cannot
be contingent on the happy path completing. The same principle applies to locks,
file handles, and any external resource acquired in a try block.
