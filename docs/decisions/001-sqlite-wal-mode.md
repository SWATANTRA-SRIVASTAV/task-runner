# ADR 001: SQLite WAL mode for concurrent read/write access

**Date:** Week 3  
**Status:** Accepted

## Context

The task runner has two concurrent actors writing to the database:
- The **scheduler** updates job status constantly (PENDING → RUNNING → SUCCESS/FAILED)
- The **REST API** reads job status in response to GET requests and writes new jobs on POST

SQLite's default journal mode is `DELETE`, which uses exclusive write locks. This means
every write blocks all readers, and vice versa. In practice this caused the API to
return stale data or stall briefly whenever the scheduler was mid-update.

## Decision

Set `PRAGMA journal_mode=WAL` on first connect.

WAL (Write-Ahead Log) mode allows **concurrent readers while a write is in progress**.
Readers see the last committed snapshot; the writer appends to the WAL file.
The WAL is checkpointed back to the main DB file periodically.

## Consequences

**Good:**
- API reads no longer block on scheduler writes
- Write throughput is higher under concurrent load
- WAL mode is the recommended mode for most multi-reader SQLite workloads

**Trade-offs:**
- WAL uses slightly more disk space (two extra files: `.db-wal`, `.db-shm`)
- WAL files must be on the same filesystem as the main DB — this is automatically
  true in our Docker volume setup, but worth noting if the deployment changes
- Under heavy write load, the WAL file can grow large if checkpointing falls behind —
  not a concern at our job volumes, but documented for future operators

## Alternatives considered

**Move to PostgreSQL**: Adds operational complexity (separate container, connection pooling).
Not justified until we need to distribute the runner across multiple hosts. Filed as a
future milestone in the roadmap.

**Use an ORM (SQLAlchemy)**: Considered and rejected for the initial version. Raw `sqlite3`
makes the queries visible and debuggable. EXPLAIN QUERY PLAN is trivially accessible.
An ORM can be layered on later without changing the `JobRepository` interface.
