# ADR 002: Container security hardening defaults

**Date:** Week 6  
**Status:** Accepted

## Context

Jobs submitted via the API run arbitrary Docker images with arbitrary commands.
This creates a real attack surface: a malicious job could attempt to escape its
container, read host files, or abuse the Docker socket.

We needed a set of hardening defaults that apply to every job container without
requiring callers to think about security.

## Decisions

### 1. Drop all Linux capabilities (`cap_drop: ALL`)

Linux capabilities are fine-grained root privileges (e.g. `CAP_NET_ADMIN`,
`CAP_SYS_PTRACE`). By default, Docker grants a permissive subset of capabilities
to every container. We drop all of them.

If a specific job legitimately needs a capability (e.g. a network scanner needs
`CAP_NET_RAW`), it can be added explicitly via the job spec in a future version.
Deny-by-default is the right starting posture.

### 2. No new privileges (`security_opt: no-new-privileges:true`)

Prevents the container process from gaining additional privileges via setuid/setgid
binaries or `execve()`. This closes a class of privilege escalation attacks where
a container binary elevates itself.

### 3. No network access by default (`network_mode: none`)

Job containers have no network interface by default. This prevents:
- Data exfiltration from a compromised job
- Lateral movement to other containers on the host network

Jobs that legitimately need network access should declare it explicitly in the
job spec. This will be implemented in a future version.

### 4. Non-root user in the runner image (`USER appuser`)

The task runner daemon itself runs as `uid=1001`. This means:
- If the FastAPI process is compromised, the attacker gets user-level access,
  not root
- Note: this does NOT protect against Docker socket abuse — anyone with
  socket access can still control Docker. See the deployment note below.

## What this does NOT protect against

- **Docker socket access**: the runner needs the socket to manage containers.
  A compromised runner process could start privileged containers. Mitigation:
  use a Docker socket proxy (e.g. `tecnativa/docker-socket-proxy`) in production
  to allow only the specific API calls the runner needs.
- **Container image vulnerabilities**: we pull and run arbitrary images. A malicious
  image could still exploit kernel vulnerabilities. This is an accepted risk for
  a developer tool; a production multi-tenant system would need gVisor or similar.

## Alternatives considered

**gVisor (runsc)**: Provides much stronger isolation by running containers in a
user-space kernel. Significant performance overhead and requires kernel support.
Documented as a future milestone for hardened deployments.

**Seccomp profiles**: Restricts which syscalls containers can make. A good next
step after cap_drop — tracked as a GitHub issue.
