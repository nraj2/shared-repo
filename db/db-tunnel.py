#!/usr/bin/env python3
"""
db-tunnel.py — Connect to Kubernetes-hosted PostgreSQL databases via an
ephemeral relay pod.  Supports interactive psql sessions and port-forward
tunnels for pgAdmin with per-service disconnect/reconnect.

Features:
    - TUI service picker (textual) — select databases, forward or psql
    - RO/RW endpoint toggle per service
    - Health checks, clipboard copy, auto-error detection
    - Text-based fallback when textual is not installed

Usage:
    db-tunnel.py                             # TUI picker (requires textual)
    db-tunnel.py ipam                        # psql terminal
    db-tunnel.py ipam --forward              # single pgAdmin tunnel
    db-tunnel.py ipam dispatcher --forward   # multiple tunnels with TUI
"""

from __future__ import annotations

import argparse
import atexit
import base64
import configparser
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

# Try importing textual for TUI mode
HAS_TEXTUAL = False
try:
    from textual.app import App, ComposeResult
    from textual.widgets import DataTable, Footer, Header, Static, RichLog
    from textual.binding import Binding
    from textual.containers import Vertical, Horizontal
    from textual.reactive import reactive
    from textual import work
    HAS_TEXTUAL = True
except ImportError:
    pass


def _ensure_textual() -> bool:
    """Prompt to install textual if missing. Returns True if available.
    Re-execs the script after install so TUI classes get defined."""
    global HAS_TEXTUAL
    if HAS_TEXTUAL:
        return True
    try:
        ans = input("textual is not installed (needed for TUI mode). Install now? [Y/n] ").strip()
    except (EOFError, KeyboardInterrupt):
        return False
    if ans.lower() == "n":
        return False
    print("Installing textual...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "textual"])
    except subprocess.CalledProcessError:
        print(f"Failed to install textual. Use --no-tui for text mode.")
        return False
    # Re-exec so the 'if HAS_TEXTUAL:' block defines all TUI classes
    print("Restarting with textual...")
    os.execv(sys.executable, [sys.executable] + sys.argv)
    return False  # unreachable

# ── Colours / symbols ──────────────────────────────────────────────
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
CYAN = "\033[0;36m"
NC = "\033[0m"

INFO = f"{CYAN}ℹ{NC}"
OK = f"{GREEN}✔{NC}"
WARN = f"{YELLOW}⚠{NC}"
ERR = f"{RED}✖{NC}"

# ═══════════════════════════════════════════════════════════════════
# SERVICE MAP — add new entries here
# ═══════════════════════════════════════════════════════════════════
@dataclass
class ServiceDef:
    name: str
    secret: str
    namespace: str
    local_port: int
    relay_port: int


SERVICES: list[ServiceDef] = [
    ServiceDef("ipam",       "ipam-db-dsn",         "ddi", 15435, 5554),
    ServiceDef("dispatcher", "dispatcher-db-dsn",   "ddi", 15434, 5553),
    ServiceDef("scheduler-dbclaim",  "scheduler-dbclaim", "atlas-jobs",  15437, 5556),
    ServiceDef("ricketts",  "ddi-dns-ricketts-db-dsn", "ricketts",  15436, 5555),
    ServiceDef("dns-conf",   "dns-config-db-dsn",  "ddi", 15433, 5552),
    ServiceDef("dns-data",   "dns-data-db-dsn",    "ddi", 15432, 5551),
]

SERVICE_MAP = {s.name: s for s in SERVICES}

POD_NAME = "psql-client"
POD_NS = os.environ.get("POD_NS", "default")
POD_IMAGE = "postgres:16-alpine"

# ── Runtime state ──────────────────────────────────────────────────
session_created_pod = False
pf_processes: dict[int, subprocess.Popen] = {}  # index → Popen


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════
def run(cmd: list[str], *, capture=True, check=True, timeout=60, **kw) -> subprocess.CompletedProcess:
    """Run a command, optionally capturing output."""
    return subprocess.run(
        cmd, capture_output=capture, text=True, check=check, timeout=timeout, **kw
    )


def kubectl(*args: str, capture=True, check=True, timeout=60) -> subprocess.CompletedProcess:
    return run(["kubectl", *args], capture=capture, check=check, timeout=timeout)


def kubectl_exec(cmd: str, *, pod=POD_NAME, ns=POD_NS) -> subprocess.CompletedProcess:
    return kubectl("exec", pod, "-n", ns, "--", "sh", "-c", cmd, check=False)


@dataclass
class ConnInfo:
    host: str = ""
    port: str = "5432"
    user: str = ""
    password: str = ""
    dbname: str = ""
    raw_dsn: str = ""      # original DSN string if available
    ro_host: str = ""       # read-only host (if different)
    ro_dsn: str = ""        # read-only DSN string


def decode_secret(secret_name: str, namespace: str) -> dict[str, str]:
    """Fetch and base64-decode all keys from a Kubernetes secret."""
    result = kubectl("get", "secret", secret_name, "-n", namespace, "-o", "json")
    data = json.loads(result.stdout).get("data", {})
    return {k: base64.b64decode(v).decode() for k, v in data.items()}


def extract(decoded: dict[str, str], *keys: str) -> str:
    """Return the first non-empty value matching any of the given keys."""
    for k in keys:
        v = decoded.get(k, "")
        if v:
            return v
    return ""


def parse_uri_dsn(dsn: str) -> ConnInfo:
    """Parse postgres://user:pass@host:port/db?params."""
    info = ConnInfo(raw_dsn=dsn)
    m = re.match(r"postgres(?:ql)?://([^:]*):([^@]*)@([^:/]+)(?::(\d+))?/([^?]*)", dsn)
    if m:
        info.user, info.password, info.host = m.group(1), m.group(2), m.group(3)
        info.port = m.group(4) or "5432"
        info.dbname = m.group(5)
    return info


def parse_kv_dsn(dsn: str) -> ConnInfo:
    """Parse host=... port=... user=... password=... dbname=... ."""
    info = ConnInfo(raw_dsn=dsn)
    for key, attr in [("host", "host"), ("port", "port"), ("user", "user"),
                      ("password", "password"), ("dbname", "dbname")]:
        m = re.search(rf"{key}=(\S+)", dsn)
        if m:
            setattr(info, attr, m.group(1))
    if not info.port:
        info.port = "5432"
    return info


def parse_dsn(dsn: str) -> ConnInfo:
    if "://" in dsn:
        return parse_uri_dsn(dsn)
    return parse_kv_dsn(dsn)


def extract_host_from_dsn(dsn: str) -> str:
    """Extract just the hostname from a DSN string."""
    if "://" in dsn:
        m = re.search(r"@([^:/]+)", dsn)
        return m.group(1) if m else ""
    m = re.search(r"host=(\S+)", dsn)
    return m.group(1) if m else ""


def resolve_connection(decoded: dict[str, str]) -> ConnInfo:
    """Build ConnInfo from decoded secret data, trying multiple key patterns."""
    info = ConnInfo()
    info.host = extract(decoded, "hostname", "host")
    info.user = extract(decoded, "username", "user")
    info.dbname = extract(decoded, "database", "dbname", "db_name")
    info.password = extract(decoded, "password")
    info.port = extract(decoded, "port") or "5432"

    # If no host found, try parsing a DSN
    if not info.host:
        raw = extract(decoded, "dsn.txt", "uri_dsn.txt", "dsn", "DATABASE_URL")
        if raw:
            parsed = parse_dsn(raw)
            info.host = parsed.host
            info.user = parsed.user or info.user
            info.password = parsed.password or info.password
            info.dbname = parsed.dbname or info.dbname
            info.port = parsed.port or info.port
            info.raw_dsn = raw

    # Resolve RO endpoint
    info.ro_host = extract(decoded, "ro_hostname", "ro_host")
    ro_dsn = extract(decoded, "ro_uri_dsn.txt", "ro_dsn", "ro_uri")
    if ro_dsn:
        info.ro_dsn = ro_dsn
        if not info.ro_host:
            info.ro_host = extract_host_from_dsn(ro_dsn)

    return info


def prompt_ro_rw(info: ConnInfo, svc_name: str = "", indent: str = "") -> ConnInfo:
    """Prompt user to choose RO or RW endpoint if both exist."""
    if info.ro_host and info.ro_host != info.host:
        print(f"{indent}{INFO} Both RO and RW endpoints found:")
        print(f"{indent}  {GREEN}1){NC} Read-only  → {CYAN}{info.ro_host}{NC} (default)")
        print(f"{indent}  {GREEN}2){NC} Read-write → {CYAN}{info.host}{NC}")
        label = f" for {svc_name}" if svc_name else ""
        choice = input(f"{indent}  Choose endpoint{label} [1]: ").strip()
        if choice == "2":
            print(f"{indent}  {WARN} Using {YELLOW}read-write{NC} endpoint.")
        else:
            info.host = info.ro_host
            if info.ro_dsn:
                info.raw_dsn = info.ro_dsn
                parsed = parse_dsn(info.ro_dsn)
                info.user = parsed.user or info.user
                info.password = parsed.password or info.password
                info.dbname = parsed.dbname or info.dbname
                info.port = parsed.port or info.port
            print(f"{indent}  {OK} Using {GREEN}read-only{NC} endpoint.")
    else:
        print(f"{indent}{INFO} Single endpoint → {CYAN}{info.host}{NC}")
    return info


def prompt_missing(info: ConnInfo) -> ConnInfo:
    """Prompt user for any missing connection parameters."""
    if not info.host or not info.user or not info.dbname:
        print(f"{WARN} Could not auto-detect all connection params from the secret.")
        if not info.host:
            info.host = input("Host: ").strip()
        if not info.user:
            info.user = input("User: ").strip()
        if not info.dbname:
            info.dbname = input("Database: ").strip()
        if not info.port:
            p = input("Port [5432]: ").strip()
            info.port = p or "5432"
        if not info.password:
            import getpass
            info.password = getpass.getpass("Password: ")
    return info


# ── pgpass management ──────────────────────────────────────────────
def write_pgpass(local_port: int, dbname: str, password: str) -> None:
    """Write or update ~/.pgpass entry for the given port. Cleans stale entries by port."""
    pgpass = Path.home() / ".pgpass"
    raw_pass = unquote(password)
    # Escape \ and : as required by pgpass format
    escaped = raw_pass.replace("\\", "\\\\").replace(":", "\\:")
    entry = f"localhost:{local_port}:{dbname}:*:{escaped}"

    lines: list[str] = []
    if pgpass.exists():
        lines = [
            l for l in pgpass.read_text().splitlines()
            if not l.startswith(f"localhost:{local_port}:")
        ]
    lines.append(entry)
    pgpass.write_text("\n".join(lines) + "\n")
    pgpass.chmod(0o600)


def write_pg_service(svc_name: str, local_port: int, user: str, dbname: str) -> None:
    """Write or update ~/.pg_service.conf for the given service."""
    svcfile = Path.home() / ".pg_service.conf"
    cfg = configparser.RawConfigParser()
    if svcfile.exists():
        cfg.read(str(svcfile))
    cfg[svc_name] = {
        "host": "localhost",
        "port": str(local_port),
        "user": user,
        "dbname": dbname,
    }
    with open(svcfile, "w") as f:
        cfg.write(f)


# ── Pod management ─────────────────────────────────────────────────
def ensure_pod() -> None:
    """Ensure the relay pod is running; create if needed."""
    global session_created_pod
    try:
        r = kubectl("get", "pod", POD_NAME, "-n", POD_NS, "--no-headers", check=False)
        status = r.stdout.split()[2] if r.stdout.strip() else ""
    except (IndexError, subprocess.TimeoutExpired):
        status = ""

    if status == "Running":
        print(f"{OK} Found existing pod {CYAN}{POD_NAME}{NC} in namespace {CYAN}{POD_NS}{NC}.")
        return

    if status == "Terminating":
        print(f"{WARN} Pod {CYAN}{POD_NAME}{NC} is terminating; waiting...")
        kubectl("wait", "--for=delete", f"pod/{POD_NAME}", "-n", POD_NS,
                "--timeout=60s", check=False)
    elif status:
        print(f"{WARN} Recreating pod {CYAN}{POD_NAME}{NC} (status: {YELLOW}{status}{NC})...")
        kubectl("delete", "pod", POD_NAME, "-n", POD_NS, "--wait=false", check=False)
        time.sleep(2)

    print(f"{INFO} Launching pod {CYAN}{POD_NAME}{NC} (image: {CYAN}{POD_IMAGE}{NC})...")
    kubectl("run", POD_NAME, "-n", POD_NS,
            f"--image={POD_IMAGE}", "--restart=Never",
            "--command", "--", "sleep", "infinity")
    session_created_pod = True

    print(f"{INFO} Waiting for pod to be ready...")
    kubectl("wait", "--for=condition=Ready", f"pod/{POD_NAME}", "-n", POD_NS,
            "--timeout=60s")


def install_socat() -> None:
    """Install socat in the relay pod if not already present."""
    print(f"{INFO} Installing socat in pod...")
    kubectl_exec("command -v socat >/dev/null 2>&1 || apk add --quiet socat")


# ── Cleanup ────────────────────────────────────────────────────────
def cleanup() -> None:
    global session_created_pod
    # Kill all port-forward subprocesses
    for idx, proc in list(pf_processes.items()):
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    pf_processes.clear()

    if session_created_pod:
        session_created_pod = False
        print(f"\n{INFO} Cleaning up session pod {CYAN}{POD_NAME}{NC}...")
        kubectl("delete", "pod", POD_NAME, "-n", POD_NS, "--wait=false", check=False)
        print(f"{OK} Done.")


atexit.register(cleanup)


def _install_signal_handlers() -> None:
    """Install signal handlers for non-TUI mode only.
    Textual manages its own signals; ours would kill the TUI."""
    def handle_signal(signum, frame):
        cleanup()
        sys.exit(0)
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)


# ═══════════════════════════════════════════════════════════════════
# Tunnel management (for --forward mode)
# ═══════════════════════════════════════════════════════════════════
@dataclass
class Tunnel:
    svc: ServiceDef
    info: ConnInfo
    status: str = "disconnected"   # connected | disconnected
    pf_proc: Optional[subprocess.Popen] = field(default=None, repr=False)
    pf_log: Optional[str] = field(default=None, repr=False)  # path to pf stderr log


def start_tunnel(t: Tunnel, idx: int) -> None:
    """Start socat relay + kubectl port-forward for a tunnel."""
    rport = t.svc.relay_port
    lport = t.svc.local_port

    # Ensure pod is alive (may have been deleted/restarted)
    ensure_pod()
    install_socat()

    # Kill stale socat
    kubectl_exec(f"pkill -f 'TCP-LISTEN:{rport}' 2>/dev/null; true")

    # Start socat relay (log errors to /tmp/socat-<port>.log)
    socat_log = f"/tmp/socat-{rport}.log"
    kubectl_exec(f"rm -f {socat_log}")
    kubectl_exec(
        f"nohup socat TCP-LISTEN:{rport},fork,reuseaddr "
        f"TCP:{t.info.host}:{t.info.port} >/dev/null 2>{socat_log} &"
    )
    time.sleep(1)

    # Check socat started successfully
    r = kubectl_exec(f"pgrep -f 'TCP-LISTEN:{rport}' >/dev/null 2>&1")
    if r.returncode != 0:
        # socat failed to start — show the error
        err_r = kubectl_exec(f"cat {socat_log} 2>/dev/null")
        err_msg = err_r.stdout.strip() if err_r.stdout else "unknown error"
        print(f"{ERR} socat failed to start for {GREEN}{t.svc.name}{NC}: {RED}{err_msg}{NC}")
        print(f"{WARN} Check host/port: {CYAN}{t.info.host}:{t.info.port}{NC}")
        t.status = "disconnected"
        return

    # Write credentials
    write_pgpass(lport, t.info.dbname, t.info.password)

    # Start port-forward in background, capture stderr for error monitoring
    pf_log_path = f"/tmp/pf-{lport}.log"
    # Truncate old log to avoid stale error alerts
    open(pf_log_path, "w").close()
    pf_log_fh = open(pf_log_path, "a")
    proc = subprocess.Popen(
        ["kubectl", "port-forward", f"pod/{POD_NAME}",
         f"{lport}:{rport}", "-n", POD_NS],
        stdout=subprocess.DEVNULL, stderr=pf_log_fh,
    )
    t.pf_proc = proc
    t.pf_log = pf_log_path
    pf_processes[idx] = proc
    t.status = "connected"


def check_tunnel_health(t: Tunnel, idx: int) -> bool:
    """Check socat log for errors AND test DB connectivity through the relay.
    Returns True if healthy, False if errors detected."""
    rport = t.svc.relay_port
    socat_log = f"/tmp/socat-{rport}.log"

    # 1. Check socat log for relay-level errors
    r = kubectl_exec(f"cat {socat_log} 2>/dev/null")
    log_content = r.stdout.strip() if r.stdout else ""

    if log_content:
        error_patterns = [
            "Connection refused",
            "Connection timed out",
            "No route to host",
            "Name or service not known",
            "Network is unreachable",
        ]
        errors = [line for line in log_content.splitlines()
                  if any(pat.lower() in line.lower() for pat in error_patterns)]
        if errors:
            print(f"\n{ERR} socat relay errors for {GREEN}{t.svc.name}{NC}:")
            for line in errors[-5:]:
                print(f"    {RED}{line}{NC}")
            print(f"{WARN} Pausing tunnel. Check host/port: "
                  f"{CYAN}{t.info.host}:{t.info.port}{NC}")
            print(f"{INFO} Use {CYAN}r {idx+1}{NC} to reconnect after fixing.")
            stop_tunnel(t, idx)
            return False

    # 2. Active probe: test PostgreSQL connectivity through the socat relay
    raw_pass = unquote(t.info.password)
    # Base64-encode password to pass safely through shell
    pass_b64 = base64.b64encode(raw_pass.encode()).decode()
    probe = kubectl_exec(
        f"PGPASSWORD=$(echo '{pass_b64}' | base64 -d) "
        f"psql -h 127.0.0.1 -p {rport} -U {shlex.quote(t.info.user)} "
        f"-d {shlex.quote(t.info.dbname)} -c 'SELECT 1' 2>&1 | head -5"
    )
    probe_out = probe.stdout.strip() if probe.stdout else ""

    if probe.returncode != 0 or "FATAL" in probe_out or "error" in probe_out.lower():
        print(f"\n{ERR} DB connection test failed for {GREEN}{t.svc.name}{NC}:")
        for line in probe_out.splitlines()[:5]:
            print(f"    {RED}{line}{NC}")
        print(f"{WARN} Credentials: user={CYAN}{t.info.user}{NC}, db={CYAN}{t.info.dbname}{NC}")
        print(f"{INFO} If you changed the username in pgAdmin, revert it to: {GREEN}{t.info.user}{NC}")
        return False

    return True


def check_pf_errors(t: Tunnel) -> list[str]:
    """Check port-forward log for connection errors. Returns list of error messages."""
    if not t.pf_log or not os.path.exists(t.pf_log):
        return []
    try:
        with open(t.pf_log) as f:
            content = f.read()
    except Exception:
        return []
    if not content.strip():
        return []

    error_patterns = [
        "connection reset by peer",
        "connection refused",
        "broken pipe",
        "error copying",
        "Unhandled Error",
        "lost connection to pod",
        "EOF",
    ]
    errors = []
    for line in content.strip().splitlines():
        if any(pat.lower() in line.lower() for pat in error_patterns):
            errors.append(line.strip())
    return errors


def stop_tunnel(t: Tunnel, idx: int) -> None:
    """Stop the port-forward and socat relay for a tunnel."""
    if t.pf_proc:
        try:
            t.pf_proc.terminate()
            t.pf_proc.wait(timeout=5)
        except Exception:
            t.pf_proc.kill()
        t.pf_proc = None
        pf_processes.pop(idx, None)

    kubectl_exec(f"pkill -f 'TCP-LISTEN:{t.svc.relay_port}' 2>/dev/null; true")
    t.status = "disconnected"


def show_menu(tunnels: list[Tunnel]) -> None:
    """Display the interactive tunnel status menu."""
    print()
    print(f"  {CYAN}━━━ Active Connections ━━━{NC}")
    for i, t in enumerate(tunnels):
        if t.status == "connected" and t.pf_proc:
            if t.pf_proc.poll() is None:
                icon = f"{GREEN}●{NC}"
                text = f"{GREEN}connected{NC}"
            else:
                icon = f"{RED}●{NC}"
                text = f"{RED}died{NC}"
                t.status = "disconnected"
        else:
            icon = f"{RED}○{NC}"
            text = f"{YELLOW}disconnected{NC}"
        print(
            f"  {icon} {i+1}) {GREEN}{t.svc.name}{NC}"
            f"  localhost:{CYAN}{t.svc.local_port}{NC}"
            f"  user={t.info.user}"
            f"  db={t.info.dbname}"
            f"  [{text}]"
        )
    print()
    print(f"  {CYAN}Commands:{NC}")
    print(f"    {GREEN}d <num>{NC}  Disconnect a service   (e.g. {CYAN}d 1{NC})")
    print(f"    {GREEN}c <num>{NC}  Connect/reconnect      (e.g. {CYAN}c 1{NC})")
    print(f"    {GREEN}r <num>{NC}  Reconnect (disconnect + connect)")
    print(f"    {GREEN}h{NC}        Health check all tunnels")
    print(f"    {GREEN}s{NC}        Show status")
    print(f"    {GREEN}q{NC}        Quit all and exit")
    print()


def print_pgadmin_details(tunnels: list[Tunnel]) -> None:
    """Print pgAdmin connection details for all tunnels."""
    user = os.environ.get("USER", "user")
    print(f"\n  {CYAN}pgAdmin connection details:{NC}")
    for t in tunnels:
        print(f"\n  {GREEN}{t.svc.name}{NC}:")
        print(f"    Host:     {GREEN}localhost{NC}")
        print(f"    Port:     {GREEN}{t.svc.local_port}{NC}")
        print(f"    Database: {GREEN}{t.info.dbname}{NC}")
        print(f"    Username: {GREEN}{t.info.user}{NC}  {YELLOW}← update in pgAdmin if this changes{NC}")
        print(f"    Password: {YELLOW}leave empty{NC} — auto-read from {CYAN}~/.pgpass{NC}")
        print(f"    Parameters → Password file: {GREEN}/Users/{user}/.pgpass{NC}")


# ═══════════════════════════════════════════════════════════════════
# TUI Mode (requires: pip install textual)
# ═══════════════════════════════════════════════════════════════════

if HAS_TEXTUAL:
    from textual.screen import Screen
    from textual.widget import Widget
    from textual.widgets import Button, Rule

    # ── Service Selection Screen ──
    class ServiceSelectScreen(Screen):
        """Pick which databases to connect to."""

        CSS = """
        #sel-header {
            height: auto;
            padding: 1 3;
            background: $boost;
            margin-bottom: 1;
        }
        #sel-list {
            height: auto;
            padding: 0 3;
        }
        """

        BINDINGS = [
            Binding("1", "toggle_1", show=False, priority=True),
            Binding("2", "toggle_2", show=False, priority=True),
            Binding("3", "toggle_3", show=False, priority=True),
            Binding("4", "toggle_4", show=False, priority=True),
            Binding("5", "toggle_5", show=False, priority=True),
            Binding("f", "forward", "Forward (f)", show=True, priority=True),
            Binding("c", "console", "psql (c)", show=True, priority=True),
            Binding("ctrl+q", "quit_app", "Quit (^Q)", show=True),
        ]

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._selected: set[str] = set()

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Static(
                "[b cyan]┌─────────────────────────────────────────┐[/]\n"
                "[b cyan]│[/]    [b white]DB Tunnel Manager[/]                   [b cyan]│[/]\n"
                "[b cyan]│[/]    [dim]Select databases to connect[/]        [b cyan]│[/]\n"
                "[b cyan]└─────────────────────────────────────────┘[/]",
                id="sel-header", markup=True,
            )
            yield Static("", id="sel-list", markup=True)
            yield Footer()

        def on_mount(self) -> None:
            self._refresh_list()

        def _refresh_list(self) -> None:
            lines = []
            lines.append("  [dim]Press number key to toggle selection:[/]")
            lines.append("")
            for idx, svc in enumerate(SERVICES):
                num = idx + 1
                if svc.name in self._selected:
                    lines.append(
                        f"    [b white on green] {num} [/]  [green]☑[/] [b]{svc.name:<14}[/]"
                        f"  [dim](localhost:{svc.local_port})[/]"
                    )
                else:
                    lines.append(
                        f"    [b white on #333333] {num} [/]  [dim]☐[/] {svc.name:<14}"
                        f"  [dim](localhost:{svc.local_port})[/]"
                    )
            lines.append("")
            count = len(self._selected)
            if count == 1:
                lines.append(f"  [green]{count} selected[/] — [b green]f[/] forward (pgAdmin)  │  [b cyan]c[/] console (psql)")
            elif count > 1:
                lines.append(f"  [green]{count} selected[/] — press [b green]f[/] to forward (pgAdmin)")
                lines.append("  [dim]psql console only available with 1 service selected[/]")
            else:
                lines.append("  [dim]Select at least one database, then press [b]f[/] to forward or [b]c[/] for psql[/]")
            lines.append("")
            lines.append("  [dim]Ctrl+Q to quit[/]")
            self.query_one("#sel-list", Static).update("\n".join(lines))

        def _toggle(self, idx: int) -> None:
            if idx >= len(SERVICES):
                return
            name = SERVICES[idx].name
            if name in self._selected:
                self._selected.discard(name)
            else:
                self._selected.add(name)
            self._refresh_list()

        def action_toggle_1(self) -> None: self._toggle(0)
        def action_toggle_2(self) -> None: self._toggle(1)
        def action_toggle_3(self) -> None: self._toggle(2)
        def action_toggle_4(self) -> None: self._toggle(3)
        def action_toggle_5(self) -> None: self._toggle(4)

        def action_forward(self) -> None:
            if not self._selected:
                return
            ordered = [s.name for s in SERVICES if s.name in self._selected]
            self.app.services = ordered
            self.app.push_screen(SetupScreen(ordered))

        def action_console(self) -> None:
            if len(self._selected) != 1:
                return
            svc_name = next(iter(self._selected))
            self.app._psql_service = svc_name
            self.app.exit()

        def action_quit_app(self) -> None:
            self.app.exit()

    # ── Setup Screen ──
    class SetupScreen(Screen):
        """Step-by-step setup: confirm cluster, pick endpoints, connect."""

        CSS = """
        #header-box {
            height: auto;
            padding: 1 3;
            background: $boost;
            margin-bottom: 1;
        }
        #step-area {
            height: auto;
            padding: 0 3;
        }
        #actions {
            height: auto;
            padding: 1 3;
            dock: bottom;
        }
        #actions Button {
            margin: 0 2 0 0;
        }
        #progress-log {
            height: 1fr;
            margin: 1 3;
            border: round $accent;
            padding: 0 1;
        }
        """

        BINDINGS = [
            Binding("1", "toggle_1", show=False, priority=True),
            Binding("2", "toggle_2", show=False, priority=True),
            Binding("3", "toggle_3", show=False, priority=True),
            Binding("4", "toggle_4", show=False, priority=True),
            Binding("5", "toggle_5", show=False, priority=True),
            Binding("enter", "do_connect", "Connect", show=True, priority=True),
            Binding("ctrl+q", "quit_app", "Quit (^Q)", show=True),
        ]

        def __init__(self, services: list[str], **kwargs):
            super().__init__(**kwargs)
            self.services = services
            self._service_infos: list[tuple] = []
            self._endpoint_choices: dict[str, str] = {}
            self._ready = False

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Static(
                "[b cyan]┌─────────────────────────────────────────┐[/]\n"
                "[b cyan]│[/]    [b white]DB Tunnel Manager[/]                   [b cyan]│[/]\n"
                "[b cyan]│[/]    [dim]PostgreSQL tunnel management tool[/]   [b cyan]│[/]\n"
                "[b cyan]└─────────────────────────────────────────┘[/]",
                id="header-box", markup=True,
            )
            yield Static("[dim]Connecting to cluster...[/dim]", id="step-area", markup=True)
            yield Rule()
            with Horizontal(id="actions"):
                yield Button("▶ Connect", id="btn-connect", variant="success", disabled=True)
                yield Button("✕ Quit", id="btn-quit", variant="error")
            yield RichLog(id="progress-log", highlight=True, markup=True)
            yield Footer()

        def on_mount(self) -> None:
            self._load_cluster_info()

        @work(thread=True)
        def _load_cluster_info(self) -> None:
            log = self.query_one("#progress-log", RichLog)

            try:
                ctx = kubectl("config", "current-context").stdout.strip()
            except Exception:
                log.write("[red bold]✖ ERROR:[/] No kubectl context set!")
                return

            log.write(f"[green]✔[/] Connected to cluster: [b]{ctx}[/]")

            infos = []
            for svc_name in self.services:
                svc = SERVICE_MAP.get(svc_name)
                if not svc:
                    log.write(f"[red]✖[/] Unknown service: {svc_name}")
                    return

                log.write(f"[cyan]⟳[/] Fetching credentials for [b]{svc.name}[/]...")
                try:
                    decoded = decode_secret(svc.secret, svc.namespace)
                except Exception:
                    log.write(f"[red]✖[/] Failed to get secret for {svc.name}")
                    return

                info = resolve_connection(decoded)
                infos.append((svc, info))
                log.write(f"  [green]✔[/] Ready")

            self._service_infos = infos
            log.write("")
            log.write("[green bold]All secrets loaded.[/] Configure endpoints below ↑")

            self.app.call_from_thread(self._render_ui, ctx)

        def _render_ui(self, ctx: str) -> None:
            """Build the selection UI on the main thread."""
            # Initialize default choices
            for svc, info in self._service_infos:
                if info.ro_host and info.ro_host != info.host:
                    self._endpoint_choices[svc.name] = "ro"
                else:
                    self._endpoint_choices[svc.name] = "single"

            self._refresh_step_area(ctx)
            self.query_one("#btn-connect", Button).disabled = False
            self._ready = True

        def _refresh_step_area(self, ctx: str = "") -> None:
            """Rebuild the step area display."""
            if not ctx:
                try:
                    ctx = kubectl("config", "current-context").stdout.strip()
                except Exception:
                    ctx = "unknown"

            lines = []
            # Section 1: Cluster
            lines.append("[b]STEP 1[/] │ Cluster")
            lines.append(f"         [green]✔[/] {ctx}")
            lines.append("")
            # Section 2: Endpoints
            lines.append("[b]STEP 2[/] │ Choose Endpoint")
            lines.append("         [dim]Press the number key (e.g. [b]1[/]) to switch between RO ↔ RW[/]")
            lines.append("")

            for idx, (svc, info) in enumerate(self._service_infos):
                num = idx + 1
                if info.ro_host and info.ro_host != info.host:
                    choice = self._endpoint_choices.get(svc.name, "ro")
                    if choice == "ro":
                        lines.append(f"    [b white on green] {num} [/]  [b]{svc.name}[/]  [dim]← press {num} to switch[/]")
                        lines.append(f"         [green]◉ READ-ONLY[/]   {info.ro_host}")
                        lines.append(f"         [dim]◯ READ-WRITE  {info.host}[/]")
                    else:
                        lines.append(f"    [b white on yellow] {num} [/]  [b]{svc.name}[/]  [dim]← press {num} to switch[/]")
                        lines.append(f"         [dim]◯ READ-ONLY   {info.ro_host}[/]")
                        lines.append(f"         [yellow]◉ READ-WRITE[/]  {info.host}")
                else:
                    lines.append(f"    [b white on blue] {num} [/]  [b]{svc.name}[/]")
                    lines.append(f"         [blue]◉ SINGLE[/]      {info.host}")
                lines.append("")

            # Section 3: Action
            lines.append("[b]STEP 3[/] │ Press [b green]Enter[/] or click [b green]▶ Connect[/] to start tunnels")

            step_area = self.query_one("#step-area", Static)
            step_area.update("\n".join(lines))

        def _toggle_endpoint(self, idx: int) -> None:
            if not self._ready or idx >= len(self._service_infos):
                return
            svc, info = self._service_infos[idx]
            if not info.ro_host or info.ro_host == info.host:
                return
            current = self._endpoint_choices.get(svc.name, "ro")
            self._endpoint_choices[svc.name] = "rw" if current == "ro" else "ro"
            self._refresh_step_area()

        def action_toggle_1(self) -> None: self._toggle_endpoint(0)
        def action_toggle_2(self) -> None: self._toggle_endpoint(1)
        def action_toggle_3(self) -> None: self._toggle_endpoint(2)
        def action_toggle_4(self) -> None: self._toggle_endpoint(3)
        def action_toggle_5(self) -> None: self._toggle_endpoint(4)

        def action_do_connect(self) -> None:
            btn = self.query_one("#btn-connect", Button)
            if not btn.disabled and self._ready:
                btn.disabled = True
                self._do_connect()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "btn-quit":
                self.app.exit()
            elif event.button.id == "btn-connect":
                if self._ready:
                    event.button.disabled = True
                    self._do_connect()

        @work(thread=True)
        def _do_connect(self) -> None:
            log = self.query_one("#progress-log", RichLog)
            tunnels = []

            log.write("")
            log.write("[b]━━━ Connecting ━━━[/]")

            for svc, info in self._service_infos:
                choice = self._endpoint_choices.get(svc.name, "ro")
                if choice == "ro" and info.ro_host:
                    info.host = info.ro_host
                    if info.ro_dsn:
                        info.raw_dsn = info.ro_dsn
                        parsed = parse_dsn(info.ro_dsn)
                        info.user = parsed.user or info.user
                        info.password = parsed.password or info.password
                        info.dbname = parsed.dbname or info.dbname
                        info.port = parsed.port or info.port
                    log.write(f"  [green]{svc.name}[/] → RO endpoint")
                elif choice == "rw":
                    log.write(f"  [yellow]{svc.name}[/] → RW endpoint")
                else:
                    log.write(f"  [blue]{svc.name}[/] → single endpoint")
                tunnels.append(Tunnel(svc=svc, info=info))

            log.write("")
            log.write("[cyan]⟳[/] Preparing relay pod...")
            ensure_pod()
            install_socat()
            log.write("[green]✔[/] Pod ready")

            for i, t in enumerate(tunnels):
                log.write(f"[cyan]⟳[/] {t.svc.name} → localhost:{t.svc.local_port}...")
                start_tunnel(t, i)
                if t.status == "connected":
                    log.write(f"  [green]✔[/] Tunnel active")
                else:
                    log.write(f"  [red]✖[/] Failed to start!")

            log.write("")
            log.write("[green bold]✔ All tunnels ready![/]")
            time.sleep(1)

            self.app._pending_tunnels = tunnels
            self.app.call_from_thread(self.app._show_manage_screen)

        def action_quit_app(self) -> None:
            self.app.exit()

    # ── Management Screen ──
    class ManageScreen(Screen):
        """Live tunnel dashboard with controls."""

        CSS = """
        #mgmt-header {
            height: auto;
            padding: 0 3;
            margin: 1 0;
        }
        #mgmt-table {
            height: auto;
            max-height: 45%;
            margin: 0 3;
        }
        #hint-bar {
            height: auto;
            padding: 0 3;
            margin: 1 0 0 0;
        }
        #mgmt-log {
            height: 1fr;
            margin: 0 3 1 3;
            border: round $accent;
            padding: 0 1;
        }
        """

        BINDINGS = [
            Binding("d", "disconnect", "Disconnect", show=True),
            Binding("c", "connect", "Connect", show=True),
            Binding("r", "reconnect", "Reconnect", show=True),
            Binding("h", "health", "Health Check", show=True),
            Binding("p", "copy_port", "Copy Port", show=True),
            Binding("u", "copy_user", "Copy User", show=True),
            Binding("b", "copy_db", "Copy DB", show=True),
            Binding("ctrl+q", "quit_all", "Quit All (^Q)", show=True),
            Binding("1", "sel_1", show=False),
            Binding("2", "sel_2", show=False),
            Binding("3", "sel_3", show=False),
            Binding("4", "sel_4", show=False),
            Binding("5", "sel_5", show=False),
        ]

        def __init__(self, tunnels: list, **kwargs):
            super().__init__(**kwargs)
            self.tunnels = tunnels

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Static(self._header_text(), id="mgmt-header", markup=True)
            yield DataTable(id="mgmt-table")
            yield Static(
                "[dim]Keys:[/] [b]↑↓[/] select  │  "
                "[b green]c[/] connect  [b red]d[/] disconnect  "
                "[b yellow]r[/] reconnect  [b cyan]h[/] health  [b]^Q[/] quit",
                id="hint-bar", markup=True,
            )
            yield RichLog(id="mgmt-log", highlight=True, markup=True)
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one("#mgmt-table", DataTable)
            table.add_columns("", "Service", "Port", "User", "Database", "Status")
            table.cursor_type = "row"
            self._refresh_table()
            self.set_interval(5, self._auto_check)
            self._log("[green]✔[/] Tunnel manager active. Use keys above to manage.")

        def _refresh_table(self) -> None:
            table = self.query_one("#mgmt-table", DataTable)
            table.clear()
            for i, t in enumerate(self.tunnels):
                icon, status = self._status_info(t)
                table.add_row(
                    icon, t.svc.name, f"localhost:{t.svc.local_port}",
                    t.info.user, t.info.dbname, status,
                )

        def _status_info(self, t) -> tuple[str, str]:
            if t.status == "connected" and t.pf_proc:
                if t.pf_proc.poll() is None:
                    return ("🟢", "Connected")
                t.status = "disconnected"
                return ("🔴", "Died")
            return ("⚫", "Disconnected")

        def _header_text(self) -> str:
            user = os.environ.get("USER", "user")
            lines = [
                "[b]pgAdmin Connection Details[/]  [dim](select text to copy)[/]",
                "",
            ]
            for t in self.tunnels:
                lines.append(f"  [b cyan]── {t.svc.name} ──[/]")
                lines.append(f"    Host:       [green]localhost[/]")
                lines.append(f"    Port:       [green]{t.svc.local_port}[/]")
                lines.append(f"    Database:   [green]{t.info.dbname}[/]")
                lines.append(f"    Username:   [green]{t.info.user}[/]  [yellow]← update in pgAdmin if this changes[/]")
                lines.append(f"    Password:   [yellow]leave empty[/] — auto-read from ~/.pgpass")
                lines.append(f"    Passfile:   [dim]/Users/{user}/.pgpass[/]")
                lines.append("")
            return "\n".join(lines)

        def _log(self, msg: str) -> None:
            self.query_one("#mgmt-log", RichLog).write(msg)

        def _get_idx(self) -> int:
            table = self.query_one("#mgmt-table", DataTable)
            return table.cursor_row if table.cursor_row is not None else 0

        def action_sel_1(self) -> None: self._goto(0)
        def action_sel_2(self) -> None: self._goto(1)
        def action_sel_3(self) -> None: self._goto(2)
        def action_sel_4(self) -> None: self._goto(3)
        def action_sel_5(self) -> None: self._goto(4)

        def _goto(self, idx: int) -> None:
            if idx < len(self.tunnels):
                self.query_one("#mgmt-table", DataTable).move_cursor(row=idx)

        @work(thread=True)
        def action_disconnect(self) -> None:
            idx = self._get_idx()
            if idx >= len(self.tunnels):
                return
            t = self.tunnels[idx]
            if t.status == "connected":
                self._log(f"[yellow]⟳[/] Disconnecting [b]{t.svc.name}[/]...")
                stop_tunnel(t, idx)
                self._log(f"[green]✔[/] {t.svc.name} disconnected")
            else:
                self._log(f"[dim]⚠ {t.svc.name} already disconnected[/]")
            self.app.call_from_thread(self._refresh_table)

        @work(thread=True)
        def action_connect(self) -> None:
            idx = self._get_idx()
            if idx >= len(self.tunnels):
                return
            t = self.tunnels[idx]
            if t.status == "disconnected":
                self._log(f"[cyan]⟳[/] Connecting [b]{t.svc.name}[/]...")
                start_tunnel(t, idx)
                if t.status == "connected":
                    self._log(f"[green]✔[/] {t.svc.name} connected")
                    time.sleep(2)
                    self._health_one(t, idx)
                else:
                    self._log(f"[red]✖[/] {t.svc.name} failed to connect")
            else:
                self._log(f"[dim]⚠ {t.svc.name} already connected. Press 'r' to reconnect.[/]")
            self.app.call_from_thread(self._refresh_table)

        @work(thread=True)
        def action_reconnect(self) -> None:
            idx = self._get_idx()
            if idx >= len(self.tunnels):
                return
            t = self.tunnels[idx]
            self._log(f"[yellow]⟳[/] Reconnecting [b]{t.svc.name}[/]...")
            stop_tunnel(t, idx)
            time.sleep(1)
            start_tunnel(t, idx)
            if t.status == "connected":
                self._log(f"[green]✔[/] {t.svc.name} reconnected")
                time.sleep(2)
                self._health_one(t, idx)
            else:
                self._log(f"[red]✖[/] {t.svc.name} failed to reconnect")
            self.app.call_from_thread(self._refresh_table)

        @work(thread=True)
        def action_health(self) -> None:
            self._log("[cyan]⟳[/] Running health check on all tunnels...")
            all_ok = True
            for i, t in enumerate(self.tunnels):
                if t.status == "connected":
                    if not self._health_one(t, i):
                        all_ok = False
            if all_ok:
                self._log("[green]✔[/] All tunnels healthy!")
            self.app.call_from_thread(self._refresh_table)

        def _health_one(self, t, idx: int) -> bool:
            ok = check_tunnel_health(t, idx)
            if not ok:
                self._log(f"[red]✖[/] {t.svc.name}: connection test FAILED (user={t.info.user})")
            return ok

        def _auto_check(self) -> None:
            changed = False
            for t in self.tunnels:
                if t.status == "connected":
                    # Check if port-forward process died
                    if t.pf_proc and t.pf_proc.poll() is not None:
                        t.status = "disconnected"
                        self._log(f"[red bold]⚠ {t.svc.name} port-forward died![/]")
                        self._log(
                            f"[yellow]  → Credentials may have rotated. "
                            f"Update username/password in pgAdmin and press [b]r[/] to reconnect.[/]"
                        )
                        changed = True

                    # Check port-forward stderr log for errors
                    pf_errors = check_pf_errors(t)
                    if pf_errors:
                        # Classify: critical errors vs transient ones
                        critical_pats = ["connection reset by peer", "connection refused",
                                         "lost connection to pod", "EOF"]
                        is_critical = any(
                            any(p in e.lower() for p in critical_pats)
                            for e in pf_errors
                        )
                        self._log(f"[yellow]⚠ {t.svc.name} port-forward errors:[/]")
                        for err in pf_errors[-3:]:
                            self._log(f"  [dim]{err}[/]")
                        if is_critical:
                            self._log(
                                f"[yellow bold]  → Check pgAdmin config for {t.svc.name}![/]"
                                f"\n[yellow]    If credentials rotated: update Username to "
                                f"[b]{t.info.user}[/] and reconnect.[/]"
                                f"\n[yellow]    Press [b]u[/] to copy username, "
                                f"[b]r[/] to reconnect tunnel.[/]"
                            )
                        else:
                            self._log("[dim]  Transient errors — tunnel still active. Monitor for recurrence.[/]")
                        # Clear log after reporting to avoid repeat alerts
                        try:
                            open(t.pf_log, "w").close()
                        except Exception:
                            pass
                        changed = True

            if changed:
                self._refresh_table()

        def _copy_to_clipboard(self, value: str, label: str) -> None:
            """Copy a value to macOS clipboard via pbcopy."""
            try:
                subprocess.run(["pbcopy"], input=value.encode(), check=True)
                idx = self._get_idx()
                svc = self.tunnels[idx].svc.name if idx < len(self.tunnels) else ""
                self._log(f"[green]✔[/] Copied {label}: [b]{value}[/]  [dim]({svc})[/]")
            except Exception:
                self._log(f"[yellow]⚠[/] Copy failed. Value: {value}")

        def action_copy_port(self) -> None:
            idx = self._get_idx()
            if idx < len(self.tunnels):
                self._copy_to_clipboard(str(self.tunnels[idx].svc.local_port), "Port")

        def action_copy_user(self) -> None:
            idx = self._get_idx()
            if idx < len(self.tunnels):
                self._copy_to_clipboard(self.tunnels[idx].info.user, "Username")

        def action_copy_db(self) -> None:
            idx = self._get_idx()
            if idx < len(self.tunnels):
                self._copy_to_clipboard(self.tunnels[idx].info.dbname, "Database")

        @work(thread=True)
        def action_quit_all(self) -> None:
            self._log("[yellow]Stopping all tunnels...[/]")
            for i, t in enumerate(self.tunnels):
                if t.status == "connected":
                    self._log(f"  [dim]Stopping {t.svc.name}...[/]")
                    # Kill port-forward immediately (local, fast)
                    if t.pf_proc:
                        try:
                            t.pf_proc.terminate()
                        except Exception:
                            pass
                        t.pf_proc = None
                    # Kill socat in pod (remote, slow — do with short timeout)
                    try:
                        subprocess.run(
                            ["kubectl", "exec", POD_NAME, "-n", POD_NS, "--",
                             "sh", "-c", f"pkill -f 'TCP-LISTEN:{t.svc.relay_port}' 2>/dev/null; true"],
                            capture_output=True, timeout=5,
                        )
                    except Exception:
                        pass
                    t.status = "disconnected"
            self.app.call_from_thread(self.app.exit)

    # ── Main App ──
    class TunnelManagerApp(App):
        """Full TUI app: setup → manage tunnels."""

        TITLE = "DB Tunnel Manager"

        CSS = """
        Screen { layout: vertical; }
        """

        def __init__(self, services: list[str] | None = None, **kwargs):
            super().__init__(**kwargs)
            self.services = services or []
            self._pending_tunnels: list = []
            self._psql_service: str | None = None

        def _show_manage_screen(self) -> None:
            self.push_screen(ManageScreen(self._pending_tunnels))

        def on_mount(self) -> None:
            if self.services:
                self.push_screen(SetupScreen(self.services))
            else:
                self.push_screen(ServiceSelectScreen())


def interactive_menu(tunnels: list[Tunnel]) -> None:
    """Run the interactive disconnect/reconnect menu (text fallback)."""
    # Fallback: text-based menu
    print_pgadmin_details(tunnels)
    show_menu(tunnels)

    while True:
        try:
            raw = input(f"{CYAN}▸{NC} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not raw:
            continue

        parts = raw.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("d", "c", "r"):
            try:
                idx = int(arg) - 1
            except (ValueError, IndexError):
                print(f"{ERR} Provide a number. E.g. {CYAN}{cmd} 1{NC}")
                continue
            if idx < 0 or idx >= len(tunnels):
                print(f"{ERR} Invalid number. Use 1-{len(tunnels)}.")
                continue

            t = tunnels[idx]
            if cmd == "d":
                if t.status == "connected":
                    print(f"{INFO} Disconnecting {GREEN}{t.svc.name}{NC}...")
                    stop_tunnel(t, idx)
                    print(f"{OK} {GREEN}{t.svc.name}{NC} disconnected.")
                else:
                    print(f"{WARN} {t.svc.name} is already disconnected.")
            elif cmd == "c":
                if t.status == "disconnected":
                    print(f"{INFO} Connecting {GREEN}{t.svc.name}{NC}...")
                    start_tunnel(t, idx)
                    if t.status == "connected":
                        print(f"{OK} {GREEN}{t.svc.name}{NC} connected.")
                        time.sleep(2)
                        check_tunnel_health(t, idx)
                else:
                    print(f"{WARN} {t.svc.name} is already connected. Use {CYAN}r{NC} to reconnect.")
            elif cmd == "r":
                print(f"{INFO} Reconnecting {GREEN}{t.svc.name}{NC}...")
                stop_tunnel(t, idx)
                time.sleep(1)
                start_tunnel(t, idx)
                if t.status == "connected":
                    print(f"{OK} {GREEN}{t.svc.name}{NC} reconnected.")
                    time.sleep(2)
                    check_tunnel_health(t, idx)

        elif cmd == "h":
            print(f"{INFO} Running health check...")
            all_ok = True
            for i, t in enumerate(tunnels):
                if t.status == "connected":
                    if not check_tunnel_health(t, i):
                        all_ok = False
            if all_ok:
                print(f"{OK} All tunnels healthy.")
        elif cmd == "s":
            show_menu(tunnels)
        elif cmd == "q":
            print(f"\n{INFO} Stopping all tunnels...")
            for i, t in enumerate(tunnels):
                if t.status == "connected":
                    stop_tunnel(t, i)
            print(f"{OK} All tunnels stopped.")
            break
        else:
            print(f"{WARN} Unknown command. Type {CYAN}s{NC} for help.")


# ═══════════════════════════════════════════════════════════════════
# Single-service psql connect
# ═══════════════════════════════════════════════════════════════════
def psql_connect(info: ConnInfo) -> int:
    """Open an interactive psql session via the relay pod."""
    ensure_pod()

    # Build DSN and base64-encode to pass safely
    dsn = info.raw_dsn or (
        f"host={info.host} port={info.port} user={info.user} "
        f"password={info.password} dbname={info.dbname}"
    )
    conn_b64 = base64.b64encode(dsn.encode()).decode()

    kubectl_exec(f"echo '{conn_b64}' | base64 -d > /tmp/.dsn")

    print(f"{OK} Opening psql...\n")
    result = subprocess.run(
        ["kubectl", "exec", "-it", POD_NAME, "-n", POD_NS, "--",
         "sh", "-c", 'PAGER=cat psql "$(cat /tmp/.dsn)"'],
        check=False,
    )
    return result.returncode


# ═══════════════════════════════════════════════════════════════════
# Single-service forward
# ═══════════════════════════════════════════════════════════════════
def single_forward(svc: ServiceDef, info: ConnInfo) -> None:
    """Run a single port-forward tunnel (blocking)."""
    ensure_pod()
    install_socat()

    relay_port = svc.relay_port
    local_port = svc.local_port

    # Kill stale socat
    kubectl_exec(f"pkill -f 'TCP-LISTEN:{relay_port}' 2>/dev/null; true")

    # Start socat relay
    print(f"{INFO} Starting TCP relay → {CYAN}{info.host}:{info.port}{NC}...")
    kubectl_exec(
        f"nohup socat TCP-LISTEN:{relay_port},fork,reuseaddr "
        f"TCP:{info.host}:{info.port} >/dev/null 2>&1 &"
    )
    time.sleep(2)

    # Verify socat
    r = kubectl_exec("pgrep socat >/dev/null 2>&1")
    if r.returncode != 0:
        print(f"{ERR} socat relay failed to start in pod.")
        sys.exit(1)

    # Write credentials
    write_pgpass(local_port, info.dbname, info.password)
    write_pg_service(svc.name, local_port, info.user, info.dbname)

    user = os.environ.get("USER", "user")
    print(f"{OK} Credentials written to {CYAN}~/.pgpass{NC}.")
    print()
    print(f"  {CYAN}pgAdmin connection details:{NC}")
    print(f"    Host:     {GREEN}localhost{NC}")
    print(f"    Port:     {GREEN}{local_port}{NC}")
    print(f"    Database: {GREEN}{info.dbname}{NC}")
    print(f"    Username: {GREEN}{info.user}{NC}  {YELLOW}← update in pgAdmin if this changes{NC}")
    print(f"    Password: {YELLOW}leave empty{NC} — auto-read from {CYAN}~/.pgpass{NC}")
    print(f"    Parameters → Password file: {GREEN}/Users/{user}/.pgpass{NC}")
    print()
    print(f"  {WARN} Press {YELLOW}Ctrl+C{NC} to stop the port-forward.")
    print()

    # Blocking port-forward
    try:
        subprocess.run(
            ["kubectl", "port-forward", f"pod/{POD_NAME}",
             f"{local_port}:{relay_port}", "-n", POD_NS],
            check=False,
        )
    except KeyboardInterrupt:
        print(f"\n{OK} Port-forward stopping...")


# ═══════════════════════════════════════════════════════════════════
# Multi-service forward
# ═══════════════════════════════════════════════════════════════════
def multi_forward(selected: list[str]) -> None:
    """Forward multiple services with interactive menu."""
    tunnels: list[Tunnel] = []

    for svc_name in selected:
        svc = SERVICE_MAP.get(svc_name)
        if not svc:
            print(f"{ERR} Unknown service: {RED}{svc_name}{NC}")
            sys.exit(1)

        print(f"\n{INFO} [{GREEN}{svc.name}{NC}] Fetching secret {CYAN}{svc.secret}{NC} (ns: {CYAN}{svc.namespace}{NC})...")
        try:
            decoded = decode_secret(svc.secret, svc.namespace)
        except subprocess.CalledProcessError:
            print(f"{ERR} [{svc.name}] Failed to retrieve secret.")
            sys.exit(1)

        info = resolve_connection(decoded)
        info = prompt_ro_rw(info, svc_name=svc.name, indent="  ")
        tunnels.append(Tunnel(svc=svc, info=info))

    # Show summary
    print(f"\n{INFO} Connection summary:")
    for t in tunnels:
        print(
            f"  {GREEN}{t.svc.name}{NC} → localhost:{CYAN}{t.svc.local_port}{NC}"
            f"  user={GREEN}{t.info.user}{NC}  db={GREEN}{t.info.dbname}{NC}"
        )
    print()
    go = input("Start all port-forwards? [Y/n] ").strip()
    if go.lower() == "n":
        print(f"{WARN} Aborted.")
        return

    # Ensure pod + socat
    ensure_pod()
    install_socat()

    # Start all tunnels
    for i, t in enumerate(tunnels):
        print(f"{INFO} Starting tunnel for {GREEN}{t.svc.name}{NC} → localhost:{CYAN}{t.svc.local_port}{NC}...")
        start_tunnel(t, i)
    time.sleep(1)

    print(f"\n{OK} All {len(tunnels)} tunnel(s) active.")

    # Interactive menu
    interactive_menu(tunnels)


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════
def _run_psql_from_tui(svc_name: str) -> None:
    """Run psql console flow after TUI exits (single service)."""
    svc = SERVICE_MAP[svc_name]
    print(f"\n{INFO} Fetching secret {CYAN}{svc.secret}{NC} from namespace {CYAN}{svc.namespace}{NC}...")
    try:
        decoded = decode_secret(svc.secret, svc.namespace)
    except subprocess.CalledProcessError:
        print(f"{ERR} Failed to retrieve secret {svc.secret}.")
        sys.exit(1)
    print(f"{OK} Secret retrieved.")

    info = resolve_connection(decoded)
    info = prompt_ro_rw(info, svc_name=svc.name)
    info = prompt_missing(info)

    print(f"\n{INFO} Connection details:")
    print(f"    Host:     {GREEN}{info.host}{NC}")
    print(f"    Port:     {GREEN}{info.port}{NC}")
    print(f"    User:     {GREEN}{info.user}{NC}")
    print(f"    Database: {GREEN}{info.dbname}{NC}")
    print(f"    Password: {YELLOW}********{NC}\n")

    while True:
        rc = psql_connect(info)
        if rc == 0:
            break
        retry = input(f"\n{WARN} Connection failed. Retry? [Y/n] ").strip()
        if retry.lower() == "n":
            print(f"{ERR} Giving up.")
            sys.exit(1)
        print(f"{INFO} Retrying...")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Connect to Kubernetes-hosted PostgreSQL databases.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Available services:\n"
            + "\n".join(
                f"  {s.name:<14} (--forward → localhost:{s.local_port})"
                for s in SERVICES
            )
            + "\n\nExamples:\n"
            "  %(prog)s ipam                        # psql terminal\n"
            "  %(prog)s ipam --forward               # single pgAdmin tunnel\n"
            "  %(prog)s ipam dispatcher --forward    # multiple tunnels\n"
        ),
    )
    parser.add_argument("services", nargs="*", metavar="service",
                        help="Service name(s) to connect to (optional with TUI)")
    parser.add_argument("--forward", action="store_true",
                        help="Port-forward mode for pgAdmin (instead of psql)")
    parser.add_argument("--no-tui", action="store_true",
                        help="Disable TUI, use text-based menu instead")
    args = parser.parse_args()

    # ── No services: launch TUI picker or error ───────────────────
    if not args.services:
        if not args.no_tui and _ensure_textual():
            app = TunnelManagerApp()
            app.run()
            # Check if user chose psql console from TUI
            if app._psql_service:
                _run_psql_from_tui(app._psql_service)
            return
        else:
            print(f"{ERR} No services specified.")
            print(f"  Available: {', '.join(SERVICE_MAP.keys())}")
            parser.print_help()
            sys.exit(1)

    # Validate services
    for s in args.services:
        if s not in SERVICE_MAP:
            print(f"{ERR} Unknown service: {RED}{s}{NC}")
            parser.print_help()
            sys.exit(1)

    # Multiple services require --forward
    if len(args.services) > 1 and not args.forward:
        print(f"{ERR} Multiple services require {CYAN}--forward{NC} flag.")
        print(f"  Example: {sys.argv[0]} ipam dispatcher --forward")
        sys.exit(1)

    # ── TUI mode: launch full UI for --forward when textual is available ──
    if args.forward and not args.no_tui and _ensure_textual():
        # Validate services exist
        for s in args.services:
            if s not in SERVICE_MAP:
                print(f"{ERR} Unknown service: {RED}{s}{NC}")
                sys.exit(1)
        app = TunnelManagerApp(args.services)
        app.run()
        return

    # ── Confirm kubectl context ────────────────────────────────────
    _install_signal_handlers()  # Safe for non-TUI mode only
    try:
        ctx = kubectl("config", "current-context").stdout.strip()
    except subprocess.CalledProcessError:
        print(f"{ERR} No kubectl context is set. Run {CYAN}kubectl config use-context <ctx>{NC} first.")
        sys.exit(1)

    svc_label = " ".join(args.services)
    print(f"\n{INFO} Current kubectl context: {GREEN}{ctx}{NC}")
    print(f"{INFO} Service:   {GREEN}{svc_label}{NC}")
    confirm = input("Is this the correct cluster? [y/N] ").strip()
    if confirm.lower() != "y":
        print(f"{WARN} Aborted. Switch context with: kubectl config use-context <ctx>")
        sys.exit(0)

    # ── Multi-service forward ──────────────────────────────────────
    if args.forward and len(args.services) > 1:
        multi_forward(args.services)
        return

    # ── Single service ─────────────────────────────────────────────
    svc = SERVICE_MAP[args.services[0]]
    print(f"\n{INFO} Fetching secret {CYAN}{svc.secret}{NC} from namespace {CYAN}{svc.namespace}{NC}...")
    try:
        decoded = decode_secret(svc.secret, svc.namespace)
    except subprocess.CalledProcessError:
        print(f"{ERR} Failed to retrieve secret {svc.secret} in namespace {svc.namespace}.")
        sys.exit(1)
    print(f"{OK} Secret retrieved.")

    info = resolve_connection(decoded)
    info = prompt_ro_rw(info)
    info = prompt_missing(info)

    print(f"\n{INFO} Connection details:")
    print(f"    Host:     {GREEN}{info.host}{NC}")
    print(f"    Port:     {GREEN}{info.port}{NC}")
    print(f"    User:     {GREEN}{info.user}{NC}")
    print(f"    Database: {GREEN}{info.dbname}{NC}")
    print(f"    Password: {YELLOW}********{NC}\n")

    if args.forward:
        # Single service forward
        go = input("Start port-forward? [Y/n] ").strip()
        if go.lower() == "n":
            print(f"{WARN} Aborted.")
            return
        single_forward(svc, info)
    else:
        # psql connect
        go = input("Connect now? [Y/n] ").strip()
        if go.lower() == "n":
            print(f"{WARN} Aborted.")
            return
        while True:
            rc = psql_connect(info)
            if rc == 0:
                break
            retry = input(f"\n{WARN} Connection failed. Retry? [Y/n] ").strip()
            if retry.lower() == "n":
                print(f"{ERR} Giving up.")
                sys.exit(1)
            print(f"{INFO} Retrying...")


if __name__ == "__main__":
    main()
