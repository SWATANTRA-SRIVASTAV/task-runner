#!/usr/bin/env bash
# Manual smoke test — submit a job, poll until done, check exit code.
# Run this against a locally running instance: ./scripts/smoke_test.sh

set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"

echo "=== Submit a job ==="
RESPONSE=$(curl -sf -X POST "$BASE/jobs" \
  -H "Content-Type: application/json" \
  -d '{
    "image": "alpine:latest",
    "command": ["sh", "-c", "echo hello from task runner && sleep 2"],
    "limits": {"memory_mb": 64, "cpu_quota": 0.5},
    "max_retries": 1,
    "timeout_seconds": 30
  }')

echo "$RESPONSE" | python3 -m json.tool
JOB_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Job ID: $JOB_ID"

echo ""
echo "=== Polling status ==="
for i in $(seq 1 20); do
  STATUS=$(curl -sf "$BASE/jobs/$JOB_ID" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  echo "  [$i] status = $STATUS"
  if [[ "$STATUS" == "success" || "$STATUS" == "failed" || "$STATUS" == "cancelled" ]]; then
    break
  fi
  sleep 1
done

echo ""
echo "=== Final job state ==="
curl -sf "$BASE/jobs/$JOB_ID" | python3 -m json.tool

echo ""
echo "=== Submit an OOM job (should fail with oom_killed) ==="
OOM_RESPONSE=$(curl -sf -X POST "$BASE/jobs" \
  -H "Content-Type: application/json" \
  -d '{
    "image": "python:3.12-slim",
    "command": ["python3", "-c", "x = bytearray(500 * 1024 * 1024)"],
    "limits": {"memory_mb": 32},
    "max_retries": 0
  }')
OOM_JOB_ID=$(echo "$OOM_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "OOM Job ID: $OOM_JOB_ID"
sleep 10
curl -sf "$BASE/jobs/$OOM_JOB_ID" | python3 -m json.tool
