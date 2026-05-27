#!/usr/bin/env bash
set -euo pipefail

# ── Colours / symbols ──────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
INFO="${CYAN}ℹ${NC}"; OK="${GREEN}✔${NC}"; WARN="${YELLOW}⚠${NC}"; ERR="${RED}✖${NC}"

# ═══════════════════════════════════════════════════════════════════
# SERVICE MAP — add new entries here
# Format:  service_name|secret_name|secret_namespace|local_port|relay_port
#   local_port  — port on your laptop (pgAdmin connects here)
#   relay_port  — socat listener inside the pod (must be unique per service)
# ═══════════════════════════════════════════════════════════════════
SERVICES=(
  "dns-data|dns-data-db-dsn|ddi|15432|5551"
  "dns-conf|dns-config-db-dsn|ddi|15433|5552"
  "dispatcher|dispatcher-db-dsn|ddi|15434|5553"
  "ipam|ipam-db-dsn|ddi|15435|5554"
  "scheduler|scheduler-dbclaim|atlas-jobs|15437|5556"
  "ricketts|ddi-dns-ricketts-db-dsn|ricketts|15436|5555"
  # "another-svc|another-svc-db-dsn|another-ns|15436|5555"
)
# ═══════════════════════════════════════════════════════════════════

# Per-user pod name to avoid conflicts when multiple users run simultaneously
_user="${USER:-default}"
_user=$(echo "$_user" | tr '[:upper:]' '[:lower:]' | tr ' ' '-')
POD_NAME="psql-client-${_user}"
POD_NS="${POD_NS:-default}"
POD_IMAGE="postgres:16-alpine"
SESSION_CREATED_POD=false
FORWARD_RELAY_PORT=""   # set by forward(), used by cleanup

# Cleanup only if this script run created the pod; kill only this service's socat relay.
cleanup() {
  # Stop all managed tunnels if multi-service mode
  if [[ ${#SVC_PF_PIDS[@]:-0} -gt 0 ]]; then
    for i in "${!SVC_PF_PIDS[@]}"; do
      [[ -n "${SVC_PF_PIDS[$i]:-}" ]] && kill "${SVC_PF_PIDS[$i]}" 2>/dev/null || true
    done
  fi
  if [[ -n "$FORWARD_RELAY_PORT" ]]; then
    # Kill only the socat listening on this service's relay port, not others
    kubectl exec "$POD_NAME" -n "$POD_NS" -- sh -c \
      "pkill -f 'TCP-LISTEN:${FORWARD_RELAY_PORT}' 2>/dev/null; true" 2>/dev/null || true
  fi
  if [[ "$SESSION_CREATED_POD" == "true" ]]; then
    SESSION_CREATED_POD=false   # prevent double-run if both EXIT and INT fire
    echo -e "\n${INFO} Cleaning up session pod ${CYAN}${POD_NAME}${NC}..."
    kubectl delete pod "$POD_NAME" -n "$POD_NS" --wait=false 2>/dev/null || true
    echo -e "${OK} Done."
  fi
}
trap cleanup EXIT INT TERM

SVC_PF_PIDS=()
SVC_STATUS=()
FORWARD_MODE=false
FORWARD_LOCAL_PORT=""   # resolved from SERVICES map

# ── 0. Pick a service ──────────────────────────────────────────────
usage() {
  echo -e "\nUsage: ${CYAN}$(basename "$0") <service> [<service2> ...] [--forward]${NC}\n"
  echo "Available services:"
  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r svc _ _ lport _ <<< "$entry"
    echo -e "  ${GREEN}${svc}${NC}  ${CYAN}(--forward → localhost:${lport})${NC}"
  done
  echo -e "\nOptions:"
  echo -e "  ${CYAN}--forward${NC}   Port-forward to localhost for pgAdmin (port defined in SERVICES map)"
  echo -e "\nExamples:"
  echo -e "  $(basename "$0") ipam                        # psql terminal"
  echo -e "  $(basename "$0") ipam --forward               # single pgAdmin tunnel"
  echo -e "  $(basename "$0") ipam dispatcher --forward    # multiple tunnels at once"
  echo ""
  exit 1
}

if [[ $# -lt 1 ]]; then
  usage
fi

# Separate service names from flags
SELECTED_SERVICES=()
SECRET_NAME=""
SECRET_NS=""
SVC_RELAY_PORT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --forward)
      FORWARD_MODE=true
      ;;
    -*)
      echo -e "${ERR} Unknown option: ${RED}${1}${NC}"
      usage
      ;;
    *)
      SELECTED_SERVICES+=("$1")
      ;;
  esac
  shift
done

if [[ ${#SELECTED_SERVICES[@]} -eq 0 ]]; then
  usage
fi

# Multiple services only supported with --forward
if [[ ${#SELECTED_SERVICES[@]} -gt 1 && "$FORWARD_MODE" == "false" ]]; then
  echo -e "${ERR} Multiple services require ${CYAN}--forward${NC} flag."
  echo -e "  Example: $(basename "$0") ipam dispatcher --forward"
  exit 1
fi

# For single-service non-forward mode OR single-service forward, resolve the service now
if [[ ${#SELECTED_SERVICES[@]} -eq 1 ]]; then
  SERVICE="${SELECTED_SERVICES[0]}"
  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r svc sec ns lport rport <<< "$entry"
    if [[ "$svc" == "$SERVICE" ]]; then
      SECRET_NAME="$sec"
      SECRET_NS="$ns"
      FORWARD_LOCAL_PORT="${lport}"
      SVC_RELAY_PORT="${rport}"
      break
    fi
  done
  if [[ -z "$SECRET_NAME" ]]; then
    echo -e "${ERR} Unknown service: ${RED}${SERVICE}${NC}"
    usage
  fi
fi

# ── 1. Confirm kubectl context ─────────────────────────────────────
current_context=$(kubectl config current-context 2>/dev/null || true)
if [[ -z "$current_context" ]]; then
  echo -e "${ERR} No kubectl context is set. Run ${CYAN}kubectl config use-context <ctx>${NC} first."
  exit 1
fi

echo -e "\n${INFO} Current kubectl context: ${GREEN}${current_context}${NC}"
echo -e "${INFO} Service:   ${GREEN}${SERVICE:-${SELECTED_SERVICES[*]}}${NC}"
read -rp "Is this the correct cluster? [y/N] " confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
  echo -e "${WARN} Aborted. Switch context with: kubectl config use-context <ctx>"
  exit 0
fi

# --forward with multiple services: subshells handle their own secret fetch; skip sections 2-3
# --forward with single service OR normal connect: fall through to secret fetch below
if [[ "$FORWARD_MODE" == "true" && ${#SELECTED_SERVICES[@]} -gt 1 ]]; then
  true  # jump to section 5
else

# ── 2. Fetch & decode the secret ───────────────────────────────────
echo -e "\n${INFO} Fetching secret ${CYAN}${SECRET_NAME}${NC} from namespace ${CYAN}${SECRET_NS}${NC}..."

secret_json=$(kubectl get secret "$SECRET_NAME" -n "$SECRET_NS" -o json 2>/dev/null) || {
  echo -e "${ERR} Failed to retrieve secret ${SECRET_NAME} in namespace ${SECRET_NS}."
  exit 1
}

decoded=$(echo "$secret_json" | jq -r '.data | map_values(@base64d)')

echo -e "${OK} Secret retrieved."

# Extract connection parameters (tries common key names)
extract() {
  local val
  for key in "$@"; do
    val=$(echo "$decoded" | jq -r ".\"${key}\" // empty" 2>/dev/null)
    if [[ -n "$val" ]]; then echo "$val"; return; fi
  done
}

db_host=$(extract host hostname)
db_user=$(extract username user)
db_name=$(extract database dbname db_name)
db_pass=$(extract password)
db_port=$(extract port)
db_port="${db_port:-5432}"

# Parse a URI-style DSN: postgres://user:pass@host:port/db
parse_uri_dsn() {
  local dsn="$1"
  db_user=$(echo "$dsn" | sed -n 's|.*://\([^:]*\):.*|\1|p')
  db_pass=$(echo "$dsn" | sed -n 's|.*://[^:]*:\([^@]*\)@.*|\1|p')
  db_host=$(echo "$dsn" | sed -n 's|.*@\([^:/]*\).*|\1|p')
  db_port=$(echo "$dsn" | sed -n 's|.*@[^:]*:\([0-9]*\)/.*|\1|p')
  db_port="${db_port:-5432}"
  db_name=$(echo "$dsn" | sed -n 's|.*/\([^?]*\).*|\1|p')
}

# Parse a libpq key=value DSN: host=... port=... user=... password=... dbname=...
parse_kv_dsn() {
  local dsn="$1"
  db_host=$(echo "$dsn" | grep -oP '(?<=host=)\S+' || true)
  db_port=$(echo "$dsn" | grep -oP '(?<=port=)\S+' || true)
  db_port="${db_port:-5432}"
  db_user=$(echo "$dsn" | grep -oP '(?<=user=)\S+' || true)
  db_pass=$(echo "$dsn" | grep -oP '(?<=password=)\S+' || true)
  db_name=$(echo "$dsn" | grep -oP '(?<=dbname=)\S+' || true)
}

# Detect DSN format and parse accordingly
parse_dsn() {
  local dsn="$1"
  if [[ "$dsn" == *"://"* ]]; then
    parse_uri_dsn "$dsn"
  else
    parse_kv_dsn "$dsn"
  fi
}

# Store raw DSNs for direct use with psql
raw_rw_dsn=""
raw_ro_dsn=""

if [[ -z "$db_host" ]]; then
  raw_rw_dsn=$(extract dsn uri DATABASE_URL "dsn.txt" "uri_dsn.txt")
  if [[ -n "$raw_rw_dsn" ]]; then
    parse_dsn "$raw_rw_dsn"
  fi
fi

# ── 2b. RO vs RW endpoint selection ───────────────────────────────
ro_host=$(extract ro_hostname ro_host)
ro_dsn=""

# If no dedicated ro_hostname field, try the ro DSN
if [[ -z "$ro_host" ]]; then
  ro_dsn=$(extract "ro_uri_dsn.txt" ro_dsn ro_uri)
  raw_ro_dsn="$ro_dsn"
  if [[ -n "${ro_dsn:-}" ]]; then
    if [[ "$ro_dsn" == *"://"* ]]; then
      ro_host=$(echo "$ro_dsn" | sed -n 's|.*@\([^:/]*\).*|\1|p')
    else
      ro_host=$(echo "$ro_dsn" | grep -oP '(?<=host=)\S+' || true)
    fi
  fi
fi

# Show choice when a full RO DSN exists (it may differ by port/db, not just host)
# or when a distinct RO hostname was found
if [[ -n "${ro_dsn:-}" || ( -n "${ro_host:-}" && "$ro_host" != "$db_host" ) ]]; then
  rw_host="$db_host"
  echo -e "\n${INFO} Both RO and RW endpoints found:"
  echo -e "  ${GREEN}1)${NC} Read-only  → ${CYAN}${ro_host:-$ro_dsn}${NC} (default)"
  echo -e "  ${GREEN}2)${NC} Read-write → ${CYAN}${rw_host}${NC}\n"
  read -rp "Choose endpoint [1]: " ep_choice
  if [[ "$ep_choice" == "2" ]]; then
    db_host="$rw_host"
    psql_conn="${raw_rw_dsn:-}"
    echo -e "${WARN} Using ${YELLOW}read-write${NC} endpoint."
  else
    if [[ -n "${raw_ro_dsn:-}" ]]; then
      psql_conn="$raw_ro_dsn"
      parse_dsn "$raw_ro_dsn"
    else
      db_host="$ro_host"
    fi
    echo -e "${OK} Using ${GREEN}read-only${NC} endpoint."
  fi
else
  echo -e "\n${INFO} Single endpoint → ${CYAN}${db_host}${NC}"
fi

if [[ -z "$db_host" || -z "$db_user" || -z "$db_name" ]]; then
  echo -e "${WARN} Could not auto-detect all connection params from the secret."
  echo -e "    Decoded keys: $(echo "$decoded" | jq -r 'keys | join(", ")')"
  echo ""
  read -rp "Host: " db_host
  read -rp "User: " db_user
  read -rp "Database: " db_name
  read -rp "Port [5432]: " db_port
  db_port="${db_port:-5432}"
  read -rsp "Password: " db_pass
  echo ""
fi

echo -e "\n${INFO} Connection details:"
echo -e "    Host:     ${GREEN}${db_host}${NC}"
echo -e "    Port:     ${GREEN}${db_port}${NC}"
echo -e "    User:     ${GREEN}${db_user}${NC}"
echo -e "    Database: ${GREEN}${db_name}${NC}"
echo -e "    Password: ${YELLOW}********${NC}\n"

read -rp "Connect now? [Y/n] " go
if [[ "$go" =~ ^[Nn]$ ]]; then
  echo -e "${WARN} Aborted."
  exit 0
fi

# ── 3. Connect via ephemeral pod ───────────────────────────────────

# Build the final psql connection string
# Prefer raw DSN from the secret (preserves password exactly); fall back to constructed string
if [[ -z "${psql_conn:-}" ]]; then
  psql_conn="host=${db_host} port=${db_port} user=${db_user} password=${db_pass} dbname=${db_name}"
fi

# Base64-encode to safely pass through shell / kubectl without mangling special chars
conn_b64=$(printf '%s' "$psql_conn" | base64)

fi   # end of non-forward secret/conn setup

connect() {
  # Check if a psql-client pod already exists
  pod_status=$(kubectl get pod "$POD_NAME" -n "$POD_NS" --no-headers 2>/dev/null | awk '{print $3}') || true

  if [[ "$pod_status" != "Running" ]]; then
    if [[ "$pod_status" == "Terminating" ]]; then
      echo -e "${WARN} Pod ${CYAN}${POD_NAME}${NC} is terminating; waiting for it to finish..."
      kubectl wait --for=delete pod/"$POD_NAME" -n "$POD_NS" --timeout=60s &>/dev/null || true
    elif [[ -n "$pod_status" ]]; then
      echo -e "${WARN} Recreating pod ${CYAN}${POD_NAME}${NC} (status: ${YELLOW}${pod_status}${NC})..."
      kubectl delete pod "$POD_NAME" -n "$POD_NS" --wait=false 2>/dev/null || true
      sleep 2
    fi

    echo -e "${INFO} Launching pod ${CYAN}${POD_NAME}${NC} (image: ${CYAN}${POD_IMAGE}${NC})..."
    kubectl run "$POD_NAME" -n "$POD_NS" \
      --image="$POD_IMAGE" \
      --restart=Never \
      --command -- sleep infinity &>/dev/null
    SESSION_CREATED_POD=true

    echo -e "${INFO} Waiting for pod to be ready..."
    kubectl wait --for=condition=Ready pod/"$POD_NAME" -n "$POD_NS" --timeout=60s &>/dev/null || {
      echo -e "${ERR} Pod failed to become ready."
      kubectl delete pod "$POD_NAME" -n "$POD_NS" --wait=false 2>/dev/null || true
      return 1
    }
  else
    echo -e "${OK} Found existing pod ${CYAN}${POD_NAME}${NC} in namespace ${CYAN}${POD_NS}${NC}."
  fi

  # Write DSN inside the pod
  kubectl exec "$POD_NAME" -n "$POD_NS" \
    -- sh -c "echo '$conn_b64' | base64 -d > /tmp/.dsn" 2>/dev/null || true

  echo -e "${OK} Opening psql...\n"

  # Run psql — exit script when psql exits for any reason
  set +e
  kubectl exec -it "$POD_NAME" -n "$POD_NS" -- sh -c 'PAGER=cat psql "$(cat /tmp/.dsn)"'
  set -e
}

# ── 4. Forward mode ───────────────────────────────────────────────
forward() {
  # Ensure pod is up (reuse connect() pod-launch logic without running psql)
  pod_status=$(kubectl get pod "$POD_NAME" -n "$POD_NS" --no-headers 2>/dev/null | awk '{print $3}') || true

  if [[ "$pod_status" != "Running" ]]; then
    if [[ "$pod_status" == "Terminating" ]]; then
      echo -e "${WARN} Pod ${CYAN}${POD_NAME}${NC} is terminating; waiting..."
      kubectl wait --for=delete pod/"$POD_NAME" -n "$POD_NS" --timeout=60s &>/dev/null || true
    elif [[ -n "$pod_status" ]]; then
      echo -e "${WARN} Recreating pod ${CYAN}${POD_NAME}${NC} (status: ${YELLOW}${pod_status}${NC})..."
      kubectl delete pod "$POD_NAME" -n "$POD_NS" --wait=false 2>/dev/null || true
      sleep 2
    fi

    echo -e "${INFO} Launching pod ${CYAN}${POD_NAME}${NC} (image: ${CYAN}${POD_IMAGE}${NC})..."
    kubectl run "$POD_NAME" -n "$POD_NS" \
      --image="$POD_IMAGE" \
      --restart=Never \
      --command -- sleep infinity &>/dev/null
    SESSION_CREATED_POD=true

    echo -e "${INFO} Waiting for pod to be ready..."
    kubectl wait --for=condition=Ready pod/"$POD_NAME" -n "$POD_NS" --timeout=60s &>/dev/null || {
      echo -e "${ERR} Pod failed to become ready."
      kubectl delete pod "$POD_NAME" -n "$POD_NS" --wait=false 2>/dev/null || true
      return 1
    }
  else
    echo -e "${OK} Found existing pod ${CYAN}${POD_NAME}${NC} in namespace ${CYAN}${POD_NS}${NC}."
  fi

  local relay_port="$SVC_RELAY_PORT"   # unique per service from SERVICES map
  FORWARD_RELAY_PORT="$relay_port"       # expose to cleanup()

  # Install socat in the pod (apk is silenced)
  echo -e "${INFO} Installing socat in pod..."
  kubectl exec "$POD_NAME" -n "$POD_NS" -- sh -c \
    'command -v socat &>/dev/null || apk add --quiet socat' 2>/dev/null || {
    echo -e "${ERR} Failed to install socat in pod."
    return 1
  }

  # Kill only this service's stale socat (leave other services' relays untouched)
  kubectl exec "$POD_NAME" -n "$POD_NS" -- sh -c \
    "pkill -f 'TCP-LISTEN:${relay_port}' 2>/dev/null; true" 2>/dev/null || true

  # Start socat relay with nohup so it survives after the exec shell exits
  echo -e "${INFO} Starting TCP relay → ${CYAN}${db_host}:${db_port}${NC}..."
  kubectl exec "$POD_NAME" -n "$POD_NS" -- sh -c \
    "nohup socat TCP-LISTEN:${relay_port},fork,reuseaddr TCP:${db_host}:${db_port} >/dev/null 2>&1 &" 2>/dev/null
  sleep 2

  # Verify socat is actually running before opening the port-forward
  kubectl exec "$POD_NAME" -n "$POD_NS" -- sh -c 'pgrep socat >/dev/null 2>&1' 2>/dev/null || {
    echo -e "${ERR} socat relay failed to start in pod."
    return 1
  }

  # URL-decode the password (%2B → +, %40 → @, etc.)
  local raw_pass
  raw_pass=$(printf '%s' "$db_pass" | python3 -c "import sys,urllib.parse; print(urllib.parse.unquote(sys.stdin.read()),end='')")

  local svc_name="${SERVICE}"   # e.g. "ipam" — used as the pg_service name

  # ── ~/.pg_service.conf — stores host/port/user/dbname ──────────
  local svcfile="$HOME/.pg_service.conf"
  touch "$svcfile"
  # Remove previous block for this service, then append refreshed block
  python3 - "$svcfile" "$svc_name" "$FORWARD_LOCAL_PORT" "$db_user" "$db_name" <<'PYEOF'
import sys, configparser, os
path, svc, port, user, dbname = sys.argv[1:]
cfg = configparser.RawConfigParser()
cfg.read(path)
cfg[svc] = {"host": "localhost", "port": port, "user": user, "dbname": dbname}
with open(path, "w") as f:
    cfg.write(f)
PYEOF

  # ── ~/.pgpass — stores password (wildcard username so rotation doesn't break pgAdmin) ──
  local pgpass="$HOME/.pgpass"
  # Escape : and \ in password as required by pgpass format
  local escaped_pass
  escaped_pass=$(printf '%s' "$raw_pass" | sed 's/\\/\\\\/g; s/:/\\:/g')
  touch "$pgpass"
  # Use * for username so pgpass matches regardless of which rotated user pgAdmin sends
  grep -v "^localhost:${FORWARD_LOCAL_PORT}:${db_name}:" "$pgpass" > "${pgpass}.tmp" 2>/dev/null || true
  echo "localhost:${FORWARD_LOCAL_PORT}:${db_name}:*:${escaped_pass}" >> "${pgpass}.tmp"
  mv "${pgpass}.tmp" "$pgpass"
  chmod 600 "$pgpass"   # must be after mv — mv preserves tmp file's umask perms

  echo -e "${OK} Credentials written to ${CYAN}~/.pgpass${NC}."
  echo -e ""
  echo -e "  ${CYAN}pgAdmin connection details:${NC}"
  echo -e "    Host:     ${GREEN}localhost${NC}"
  echo -e "    Port:     ${GREEN}${FORWARD_LOCAL_PORT}${NC}"
  echo -e "    Database: ${GREEN}${db_name}${NC}"
  echo -e "    Username: ${GREEN}${db_user}${NC}  ${YELLOW}← update in pgAdmin if this changes${NC}"
  echo -e "    Password: ${YELLOW}leave empty${NC} — auto-read from ${CYAN}~/.pgpass${NC}"
  echo -e "    Parameters → Password file: ${GREEN}/Users/${USER}/.pgpass${NC}"
  echo -e ""
  echo -e "  ${WARN} Press ${YELLOW}Ctrl+C${NC} to stop the port-forward."
  echo -e ""

  # Hold the port-forward open; print message on Ctrl+C
  set +e
  kubectl port-forward pod/"$POD_NAME" "${FORWARD_LOCAL_PORT}:${relay_port}" -n "$POD_NS" &
  PF_PID=$!
  trap 'echo -e "\n${OK} Port-forward stopping..."; kill "$PF_PID" 2>/dev/null; wait "$PF_PID" 2>/dev/null' INT
  wait "$PF_PID"
  trap cleanup EXIT INT TERM   # restore original trap
  set -e
}

# ── 4b. Start a single tunnel (socat + port-forward) ──────────────
# Sets up socat relay and starts kubectl port-forward in background.
# Stores the port-forward PID in SVC_PF_PIDS[index].
# Args: index into MS_* arrays
start_tunnel() {
  local i="$1"
  local svc="${MS_SVCS[$i]}" host="${MS_HOSTS[$i]}" port="${MS_PORTS[$i]}"
  local user="${MS_USERS[$i]}" dbname="${MS_NAMES[$i]}" pass="${MS_PASSES[$i]}"
  local lport="${MS_LPORTS[$i]}" rport="${MS_RPORTS[$i]}"

  # Kill any stale socat for this relay port
  kubectl exec "$POD_NAME" -n "$POD_NS" -- sh -c \
    "pkill -f 'TCP-LISTEN:${rport}' 2>/dev/null; true" 2>/dev/null || true

  # Start socat relay
  kubectl exec "$POD_NAME" -n "$POD_NS" -- sh -c \
    "nohup socat TCP-LISTEN:${rport},fork,reuseaddr TCP:${host}:${port} >/dev/null 2>&1 &" 2>/dev/null
  sleep 1

  # URL-decode the password
  local raw_pass
  raw_pass=$(printf '%s' "$pass" | python3 -c "import sys,urllib.parse; print(urllib.parse.unquote(sys.stdin.read()),end='')")

  # Write ~/.pgpass entry
  local pgpass="$HOME/.pgpass"
  local escaped_pass
  escaped_pass=$(printf '%s' "$raw_pass" | sed 's/\\/\\\\/g; s/:/\\:/g')
  touch "$pgpass"
  grep -v "^localhost:${lport}:" "$pgpass" > "${pgpass}.tmp" 2>/dev/null || true
  echo "localhost:${lport}:${dbname}:*:${escaped_pass}" >> "${pgpass}.tmp"
  mv "${pgpass}.tmp" "$pgpass"
  chmod 600 "$pgpass"

  # Start port-forward in background
  kubectl port-forward pod/"$POD_NAME" "${lport}:${rport}" -n "$POD_NS" >/dev/null 2>&1 &
  SVC_PF_PIDS[$i]=$!
  SVC_STATUS[$i]="connected"
}

# ── 4c. Stop a single tunnel ─────────────────────────────────────
stop_tunnel() {
  local i="$1"
  local rport="${MS_RPORTS[$i]}"

  # Kill port-forward
  if [[ -n "${SVC_PF_PIDS[$i]:-}" ]]; then
    kill "${SVC_PF_PIDS[$i]}" 2>/dev/null || true
    wait "${SVC_PF_PIDS[$i]}" 2>/dev/null || true
    SVC_PF_PIDS[$i]=""
  fi

  # Kill socat relay for this port
  kubectl exec "$POD_NAME" -n "$POD_NS" -- sh -c \
    "pkill -f 'TCP-LISTEN:${rport}' 2>/dev/null; true" 2>/dev/null || true

  SVC_STATUS[$i]="disconnected"
}

# ── 4d. Interactive menu ──────────────────────────────────────────
show_menu() {
  echo ""
  echo -e "  ${CYAN}━━━ Active Connections ━━━${NC}"
  for i in "${!MS_SVCS[@]}"; do
    local status_icon status_text
    if [[ "${SVC_STATUS[$i]}" == "connected" ]]; then
      # Check if PF process is still alive
      if kill -0 "${SVC_PF_PIDS[$i]}" 2>/dev/null; then
        status_icon="${GREEN}●${NC}"; status_text="${GREEN}connected${NC}"
      else
        status_icon="${RED}●${NC}"; status_text="${RED}died${NC}"
        SVC_STATUS[$i]="disconnected"
      fi
    else
      status_icon="${RED}○${NC}"; status_text="${YELLOW}disconnected${NC}"
    fi
    echo -e "  ${status_icon} $((i+1))) ${GREEN}${MS_SVCS[$i]}${NC}  localhost:${CYAN}${MS_LPORTS[$i]}${NC}  user=${MS_USERS[$i]}  db=${MS_NAMES[$i]}  [${status_text}]"
  done
  echo ""
  echo -e "  ${CYAN}Commands:${NC}"
  echo -e "    ${GREEN}d <num>${NC}  Disconnect a service   (e.g. ${CYAN}d 1${NC})"
  echo -e "    ${GREEN}c <num>${NC}  Connect/reconnect      (e.g. ${CYAN}c 1${NC})"
  echo -e "    ${GREEN}r <num>${NC}  Reconnect (disconnect + connect)"
  echo -e "    ${GREEN}s${NC}        Show status"
  echo -e "    ${GREEN}q${NC}        Quit all and exit"
  echo ""
}

# ── 5. Run ─────────────────────────────────────────────────────────
if [[ "$FORWARD_MODE" == "true" && ${#SELECTED_SERVICES[@]} -gt 1 ]]; then
  # ── Multi-service: fetch all secrets, show all details, confirm once ──────────────

  # Per-service resolved data stored in parallel arrays
  MS_SVCS=(); MS_HOSTS=(); MS_PORTS=(); MS_USERS=(); MS_NAMES=(); MS_PASSES=()
  MS_LPORTS=(); MS_RPORTS=(); MS_PSQL_CONNS=()

  for svc_arg in "${SELECTED_SERVICES[@]}"; do
    found=false
    for entry in "${SERVICES[@]}"; do
      IFS='|' read -r svc sec ns lport rport <<< "$entry"
      if [[ "$svc" == "$svc_arg" ]]; then
        found=true
        echo -e "\n${INFO} [${GREEN}${svc}${NC}] Fetching secret ${CYAN}${sec}${NC} (ns: ${CYAN}${ns}${NC})..."
        secret_json=$(kubectl get secret "$sec" -n "$ns" -o json 2>/dev/null) || {
          echo -e "${ERR} [${svc}] Failed to retrieve secret."; exit 1
        }
        dec=$(echo "$secret_json" | jq -r '.data | map_values(@base64d)')
        h=$(echo "$dec" | jq -r '.hostname // .host // empty')
        u=$(echo "$dec" | jq -r '.username // .user // empty')
        n=$(echo "$dec" | jq -r '.database // .dbname // .db_name // empty')
        p=$(echo "$dec" | jq -r '.password // empty')
        pt=$(echo "$dec" | jq -r '.port // empty'); pt="${pt:-5432}"
        pc=""
        if [[ -z "$h" ]]; then
          raw=$(echo "$dec" | jq -r '."dsn.txt" // ."uri_dsn.txt" // .dsn // empty')
          if [[ -n "$raw" ]]; then
            h=$(echo "$raw" | sed -n 's/.*host=\([^ ]*\).*/\1/p')
            u=$(echo "$raw" | sed -n 's/.*user=\([^ ]*\).*/\1/p')
            p=$(echo "$raw" | sed -n 's/.*password=\([^ ]*\).*/\1/p')
            n=$(echo "$raw" | sed -n 's/.*dbname=\([^ ]*\).*/\1/p')
            pt=$(echo "$raw" | sed -n 's/.*port=\([^ ]*\).*/\1/p'); pt="${pt:-5432}"
            pc="$raw"
          fi
        fi
        # RO/RW prompt per service
        ro_h=$(echo "$dec" | jq -r '.ro_hostname // .ro_host // empty')
        ro_dsn=$(echo "$dec" | jq -r '."ro_uri_dsn.txt" // .ro_dsn // .ro_uri // empty')
        if [[ -z "$ro_h" && -n "$ro_dsn" ]]; then
          ro_h=$(echo "$ro_dsn" | sed -n 's/.*host=\([^ ]*\).*/\1/p')
          [[ -z "$ro_h" ]] && ro_h=$(echo "$ro_dsn" | sed -n 's|.*@\([^:/]*\).*|\1|p')
        fi
        if [[ -n "$ro_h" && "$ro_h" != "$h" ]]; then
          echo -e "  ${INFO} Both RO and RW endpoints found:"
          echo -e "  ${GREEN}1)${NC} Read-only  → ${CYAN}${ro_h}${NC} (default)"
          echo -e "  ${GREEN}2)${NC} Read-write → ${CYAN}${h}${NC}"
          read -rp "  Choose endpoint for ${svc} [1]: " ep
          if [[ "$ep" == "2" ]]; then
            echo -e "  ${WARN} Using ${YELLOW}read-write${NC} endpoint."
          else
            h="$ro_h"
            [[ -n "$ro_dsn" ]] && pc="$ro_dsn"
            echo -e "  ${OK} Using ${GREEN}read-only${NC} endpoint."
          fi
        else
          echo -e "  ${INFO} Single endpoint → ${CYAN}${h}${NC}"
        fi
        MS_SVCS+=("$svc"); MS_HOSTS+=("$h"); MS_PORTS+=("$pt")
        MS_USERS+=("$u"); MS_NAMES+=("$n"); MS_PASSES+=("$p")
        MS_LPORTS+=("$lport"); MS_RPORTS+=("$rport"); MS_PSQL_CONNS+=("$pc")
        break
      fi
    done
    if [[ "$found" == "false" ]]; then
      echo -e "${ERR} Unknown service: ${RED}${svc_arg}${NC}"; usage
    fi
  done

  # Show combined summary
  echo -e "\n${INFO} Connection summary:"
  for i in "${!MS_SVCS[@]}"; do
    echo -e "  ${GREEN}${MS_SVCS[$i]}${NC} → localhost:${CYAN}${MS_LPORTS[$i]}${NC}  user=${GREEN}${MS_USERS[$i]}${NC}  db=${GREEN}${MS_NAMES[$i]}${NC}"
  done
  echo ""
  read -rp "Start all port-forwards? [Y/n] " go
  if [[ "$go" =~ ^[Nn]$ ]]; then echo -e "${WARN} Aborted."; exit 0; fi

  # Launch all tunnels in parallel
  SVC_PF_PIDS=()
  SVC_STATUS=()

  # Ensure pod is up and socat is installed (once, shared by all services)
  pod_status=$(kubectl get pod "$POD_NAME" -n "$POD_NS" --no-headers 2>/dev/null | awk '{print $3}') || true
  if [[ "$pod_status" != "Running" ]]; then
    if [[ "$pod_status" == "Terminating" ]]; then
      echo -e "${WARN} Pod ${CYAN}${POD_NAME}${NC} is terminating; waiting..."
      kubectl wait --for=delete pod/"$POD_NAME" -n "$POD_NS" --timeout=60s &>/dev/null || true
    elif [[ -n "$pod_status" ]]; then
      echo -e "${WARN} Recreating pod ${CYAN}${POD_NAME}${NC} (status: ${YELLOW}${pod_status}${NC})..."
      kubectl delete pod "$POD_NAME" -n "$POD_NS" --wait=false 2>/dev/null || true
      sleep 2
    fi
    echo -e "${INFO} Launching pod ${CYAN}${POD_NAME}${NC} (image: ${CYAN}${POD_IMAGE}${NC})..."
    kubectl run "$POD_NAME" -n "$POD_NS" --image="$POD_IMAGE" --restart=Never --command -- sleep infinity &>/dev/null
    SESSION_CREATED_POD=true
    echo -e "${INFO} Waiting for pod to be ready..."
    kubectl wait --for=condition=Ready pod/"$POD_NAME" -n "$POD_NS" --timeout=60s &>/dev/null || {
      echo -e "${ERR} Pod failed to become ready."; exit 1
    }
  else
    echo -e "${OK} Found existing pod ${CYAN}${POD_NAME}${NC} in namespace ${CYAN}${POD_NS}${NC}."
  fi
  echo -e "${INFO} Installing socat in pod..."
  kubectl exec "$POD_NAME" -n "$POD_NS" -- sh -c \
    'command -v socat &>/dev/null || apk add --quiet socat' 2>/dev/null || {
    echo -e "${ERR} Failed to install socat in pod."; exit 1
  }

  # Start all tunnels
  for i in "${!MS_SVCS[@]}"; do
    echo -e "${INFO} Starting tunnel for ${GREEN}${MS_SVCS[$i]}${NC} → localhost:${CYAN}${MS_LPORTS[$i]}${NC}..."
    start_tunnel "$i"
  done
  sleep 1

  echo -e "\n${OK} All ${#MS_SVCS[@]} tunnel(s) active."

  # Write pgAdmin connection details
  echo -e "\n  ${CYAN}pgAdmin connection details:${NC}"
  for i in "${!MS_SVCS[@]}"; do
    echo -e "\n  ${GREEN}${MS_SVCS[$i]}${NC}:"
    echo -e "    Host:     ${GREEN}localhost${NC}"
    echo -e "    Port:     ${GREEN}${MS_LPORTS[$i]}${NC}"
    echo -e "    Database: ${GREEN}${MS_NAMES[$i]}${NC}"
    echo -e "    Username: ${GREEN}${MS_USERS[$i]}${NC}  ${YELLOW}← update in pgAdmin if this changes${NC}"
    echo -e "    Password: ${YELLOW}leave empty${NC} — auto-read from ${CYAN}~/.pgpass${NC}"
    echo -e "    Parameters → Password file: ${GREEN}/Users/${USER}/.pgpass${NC}"
  done

  # Interactive menu loop
  show_menu
  while true; do
    read -rp "$(echo -e "${CYAN}▸${NC} ")" cmd arg
    case "$cmd" in
      d|D)
        idx=$((arg - 1))
        if [[ $idx -ge 0 && $idx -lt ${#MS_SVCS[@]} ]]; then
          if [[ "${SVC_STATUS[$idx]}" == "connected" ]]; then
            echo -e "${INFO} Disconnecting ${GREEN}${MS_SVCS[$idx]}${NC}..."
            stop_tunnel "$idx"
            echo -e "${OK} ${GREEN}${MS_SVCS[$idx]}${NC} disconnected."
          else
            echo -e "${WARN} ${MS_SVCS[$idx]} is already disconnected."
          fi
        else
          echo -e "${ERR} Invalid number. Use 1-${#MS_SVCS[@]}."
        fi
        ;;
      c|C)
        idx=$((arg - 1))
        if [[ $idx -ge 0 && $idx -lt ${#MS_SVCS[@]} ]]; then
          if [[ "${SVC_STATUS[$idx]}" == "disconnected" ]]; then
            echo -e "${INFO} Connecting ${GREEN}${MS_SVCS[$idx]}${NC}..."
            start_tunnel "$idx"
            echo -e "${OK} ${GREEN}${MS_SVCS[$idx]}${NC} connected."
          else
            echo -e "${WARN} ${MS_SVCS[$idx]} is already connected. Use ${CYAN}r${NC} to reconnect."
          fi
        else
          echo -e "${ERR} Invalid number. Use 1-${#MS_SVCS[@]}."
        fi
        ;;
      r|R)
        idx=$((arg - 1))
        if [[ $idx -ge 0 && $idx -lt ${#MS_SVCS[@]} ]]; then
          echo -e "${INFO} Reconnecting ${GREEN}${MS_SVCS[$idx]}${NC}..."
          stop_tunnel "$idx"
          sleep 1
          start_tunnel "$idx"
          echo -e "${OK} ${GREEN}${MS_SVCS[$idx]}${NC} reconnected."
        else
          echo -e "${ERR} Invalid number. Use 1-${#MS_SVCS[@]}."
        fi
        ;;
      s|S)
        show_menu
        ;;
      q|Q)
        echo -e "\n${INFO} Stopping all tunnels..."
        for i in "${!MS_SVCS[@]}"; do
          [[ "${SVC_STATUS[$i]}" == "connected" ]] && stop_tunnel "$i"
        done
        echo -e "${OK} All tunnels stopped."
        break
        ;;
      *)
        echo -e "${WARN} Unknown command. Type ${CYAN}s${NC} for help."
        ;;
    esac
  done
  exit 0
fi

# Single-service psql connect (non-forward) or single-service forward
if [[ "$FORWARD_MODE" == "true" ]]; then
  forward
elif [[ "$FORWARD_MODE" == "false" ]]; then
  while true; do
    set +e
    connect
    rc=$?
    set -e
    if [[ $rc -eq 0 ]]; then
      break
    fi
    echo ""
    read -rp "$(echo -e "${WARN} Connection failed. Retry? [Y/n] ")" retry
    if [[ "$retry" =~ ^[Nn]$ ]]; then
      echo -e "${ERR} Giving up."
      exit 1
    fi
    echo -e "${INFO} Retrying..."
  done
fi