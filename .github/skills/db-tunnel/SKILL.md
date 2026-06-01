---
name: db-tunnel
description: 'Kubernetes PostgreSQL tunnel manager with textual TUI. USE FOR: adding features to db-tunnel.py, adding new database services, modifying TUI screens, fixing tunnel/socat/port-forward issues, building similar K8s tunnel scripts. TRIGGERS: db-tunnel, database tunnel, psql connect, port-forward, pgAdmin, socat relay, textual TUI.'
---

# db-tunnel.py — K8s PostgreSQL Tunnel Manager

## Overview

A Python CLI/TUI tool that connects to Kubernetes-hosted PostgreSQL databases via an ephemeral relay pod. Supports interactive psql sessions and port-forward tunnels for pgAdmin.

**Location**: `db/db-tunnel.py` (~1600 lines)

## Architecture

### Screens (3-screen TUI flow using textual)

```
ServiceSelectScreen → SetupScreen → ManageScreen
      (pick DBs)      (fetch creds,    (live dashboard,
                       choose RO/RW)    health checks)
```

1. **ServiceSelectScreen** — Multi-select databases with number keys (1-5), then `f` to forward (pgAdmin) or `c` for psql console (single service only)
2. **SetupScreen** — 3-step flow: confirm cluster → toggle RO/RW endpoints → connect. Fetches K8s secrets, sets up relay pod
3. **ManageScreen** — Live DataTable dashboard with keybindings: `d` disconnect, `c` connect, `r` reconnect, `h` health check, `p/u/b` clipboard copy, `Ctrl+Q` quit

### Core Data Models

```python
@dataclass
class ServiceDef:
    name: str          # e.g. "ipam"
    secret: str        # K8s secret name, e.g. "ipam-db-dsn"
    namespace: str     # K8s namespace, e.g. "ddi"
    local_port: int    # localhost port for pgAdmin, e.g. 15435
    relay_port: int    # socat relay port inside pod, e.g. 5554

@dataclass
class ConnInfo:
    host, port, user, password, dbname: str
    raw_dsn, ro_host, ro_dsn: str  # RO endpoint support

@dataclass
class Tunnel:
    svc: ServiceDef
    info: ConnInfo
    status: str  # "connected" | "disconnected"
    pf_proc: Optional[subprocess.Popen]  # port-forward process
    pf_log: Optional[str]  # stderr log path for error monitoring
```

### Key Functions

| Function | Purpose |
|----------|---------|
| `decode_secret()` | Fetch & base64-decode K8s secret |
| `resolve_connection()` | Build ConnInfo from secret data (multiple key patterns) |
| `ensure_pod()` | Create/wait for `psql-client` relay pod |
| `install_socat()` | Install socat in pod via `apk add` |
| `start_tunnel()` | Start socat relay + kubectl port-forward |
| `stop_tunnel()` | Terminate port-forward + kill socat |
| `check_tunnel_health()` | Check socat logs + active psql probe |
| `check_pf_errors()` | Monitor port-forward stderr for connection errors |
| `write_pgpass()` | Write/update `~/.pgpass` for password-free access |
| `psql_connect()` | Open interactive psql via relay pod |

### Tunnel Flow

```
User → localhost:15435 → kubectl port-forward → pod:5554 (socat) → RDS:5432
```

1. Ephemeral pod (`postgres:16-alpine`) with `socat` installed
2. socat listens on relay_port, forwards to actual DB host:port
3. kubectl port-forward maps local_port → relay_port
4. `~/.pgpass` written for password-free pgAdmin/psql access

### Error Monitoring

- socat stderr → `/tmp/socat-{port}.log` (checked in health checks)
- port-forward stderr → `/tmp/pf-{port}.log` (auto-checked every 5s)
- Patterns detected: connection reset, refused, broken pipe, EOF
- Auto-alerts user about credential rotation with actionable instructions

## Adding a New Service

Add entry to `SERVICES` list:

```python
SERVICES: list[ServiceDef] = [
    ServiceDef("ipam",       "ipam-db-dsn",       "ddi", 15435, 5554),
    ServiceDef("new-svc",    "new-svc-db-dsn",    "ns",  15436, 5555),  # ← add here
]
```

Ensure: unique `local_port` and `relay_port`, K8s secret exists in the namespace.

## Key Patterns & Lessons

- **RichLog not Log**: textual's `Log` doesn't support `markup=True`; use `RichLog` with `.write()` (not `.write_line()`)
- **asyncio in threads**: Python 3.9 `asyncio.Lock()` needs event loop; create Screen objects on main thread, not in `@work(thread=True)` workers
- **Priority bindings**: Number key bindings need `priority=True` when Button has focus
- **Password encoding**: Passwords are URL-encoded in K8s secrets; `unquote()` then base64-encode for safe shell transport in health checks
- **socat stdout**: Must redirect to `/dev/null` or it blocks kubectl exec (60s timeout)
- **Fast quit**: Terminate local pf_proc immediately; remote socat cleanup uses 5s timeout with try/except
- **Ctrl+Q over Q**: Prevents accidental quit when typing

## Dependencies

- Python 3.9+
- `textual` (auto-installed if missing)
- `kubectl` with valid context
- macOS `pbcopy` for clipboard shortcuts

## Entry Points

| Invocation | Behavior |
|------------|----------|
| `db-tunnel.py` | TUI picker (ServiceSelectScreen) |
| `db-tunnel.py ipam` | Direct psql connect |
| `db-tunnel.py ipam --forward` | TUI forward (SetupScreen → ManageScreen) |
| `db-tunnel.py ipam dispatcher --forward` | Multi-service TUI forward |
| `db-tunnel.py --no-tui` | Text-based fallback |
