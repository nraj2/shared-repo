#!/usr/bin/env bash
set -euo pipefail

# ── Colours / symbols ──────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
INFO="${CYAN}ℹ${NC}"; OK="${GREEN}✔${NC}"; WARN="${YELLOW}⚠${NC}"; ERR="${RED}✖${NC}"

# ═══════════════════════════════════════════════════════════════════
# SERVICE MAP — add new entries here
# Format:  service_name|secret_name|secret_namespace
# ═══════════════════════════════════════════════════════════════════
SERVICES=(
  "ipam-niosbridge|ipam-niosbridge-managed-redis|ddi"
  "ddi-canonical-redis|ddi-canonical-redis|ddi"
  # "another-svc|another-svc-managed-redis|another-ns"
)
# ═══════════════════════════════════════════════════════════════════

POD_NAME="redis-client"
POD_NS="${POD_NS:-default}"
POD_IMAGE="redis:7-alpine"

# Cleanup pod on exit/interrupt
cleanup() {
  echo -e "\n${INFO} Cleaning up pod ${CYAN}${POD_NAME}${NC}..."
  kubectl delete pod "$POD_NAME" -n "$POD_NS" --wait=false 2>/dev/null || true
  echo -e "${OK} Done."
}
trap cleanup EXIT

# ── 0. Pick a service ──────────────────────────────────────────────
usage() {
  echo -e "\nUsage: ${CYAN}$(basename "$0") <service> [--tls]${NC}\n"
  echo "Available services:"
  for entry in "${SERVICES[@]}"; do
    svc="${entry%%|*}"
    echo -e "  ${GREEN}${svc}${NC}"
  done
  echo -e "\nOptions:"
  echo -e "  ${CYAN}--tls${NC}  Connect with TLS enabled"
  echo ""
  exit 1
}

if [[ $# -lt 1 ]]; then
  usage
fi

SERVICE=""
USE_TLS=false

for arg in "$@"; do
  case "$arg" in
    --tls) USE_TLS=true ;;
    -*) echo -e "${ERR} Unknown flag: ${RED}${arg}${NC}"; usage ;;
    *) SERVICE="$arg" ;;
  esac
done

if [[ -z "$SERVICE" ]]; then
  usage
fi

SECRET_NAME=""
SECRET_NS=""

for entry in "${SERVICES[@]}"; do
  IFS='|' read -r svc sec ns <<< "$entry"
  if [[ "$svc" == "$SERVICE" ]]; then
    SECRET_NAME="$sec"
    SECRET_NS="$ns"
    break
  fi
done

if [[ -z "$SECRET_NAME" ]]; then
  echo -e "${ERR} Unknown service: ${RED}${SERVICE}${NC}"
  usage
fi

# ── 1. Confirm kubectl context ─────────────────────────────────────
current_context=$(kubectl config current-context 2>/dev/null || true)
if [[ -z "$current_context" ]]; then
  echo -e "${ERR} No kubectl context is set. Run ${CYAN}kubectl config use-context <ctx>${NC} first."
  exit 1
fi

echo -e "\n${INFO} Current kubectl context: ${GREEN}${current_context}${NC}"
echo -e "${INFO} Service:   ${GREEN}${SERVICE}${NC}"
echo -e "${INFO} Secret:    ${CYAN}${SECRET_NAME}${NC}  (namespace: ${CYAN}${SECRET_NS}${NC})"
if $USE_TLS; then
  echo -e "${INFO} TLS:       ${GREEN}enabled${NC}"
fi
echo ""
read -rp "Is this the correct cluster? [y/N] " confirm
if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
  echo -e "${WARN} Aborted. Switch context with: kubectl config use-context <ctx>"
  exit 0
fi

# ── 2. Fetch & decode the secret ───────────────────────────────────
echo -e "\n${INFO} Fetching secret ${CYAN}${SECRET_NAME}${NC} from namespace ${CYAN}${SECRET_NS}${NC}..."

secret_json=$(kubectl get secret "$SECRET_NAME" -n "$SECRET_NS" -o json 2>/dev/null) || {
  echo -e "${ERR} Failed to retrieve secret ${SECRET_NAME} in namespace ${SECRET_NS}."
  exit 1
}

decoded=$(echo "$secret_json" | jq -r '.data | map_values(@base64d)')

echo -e "${OK} Secret retrieved."

# Extract connection parameters
extract() {
  local val
  for key in "$@"; do
    val=$(echo "$decoded" | jq -r ".\"${key}\" // empty" 2>/dev/null)
    if [[ -n "$val" ]]; then echo "$val"; return; fi
  done
}

rw_host=$(extract endpoint host hostname)
rw_port=$(extract port)
rw_port="${rw_port:-6379}"

ro_host=$(extract readerEndpoint ro_endpoint reader_endpoint)
ro_port=$(extract readerPort ro_port reader_port)
ro_port="${ro_port:-$rw_port}"

auth_token=$(extract authToken auth_token password auth)

# ── 2b. RO vs RW endpoint selection ───────────────────────────────
redis_host="$rw_host"
redis_port="$rw_port"

if [[ -n "${ro_host:-}" && "$ro_host" != "$rw_host" ]]; then
  echo -e "\n${INFO} Both RO and RW endpoints found:"
  echo -e "  ${GREEN}1)${NC} Read-only  → ${CYAN}${ro_host}:${ro_port}${NC} (default)"
  echo -e "  ${GREEN}2)${NC} Read-write → ${CYAN}${rw_host}:${rw_port}${NC}\n"
  read -rp "Choose endpoint [1]: " ep_choice
  if [[ "$ep_choice" == "2" ]]; then
    redis_host="$rw_host"
    redis_port="$rw_port"
    echo -e "${WARN} Using ${YELLOW}read-write${NC} endpoint."
  else
    redis_host="$ro_host"
    redis_port="$ro_port"
    echo -e "${OK} Using ${GREEN}read-only${NC} endpoint."
  fi
fi

if [[ -z "$redis_host" ]]; then
  echo -e "${WARN} Could not auto-detect endpoint from the secret."
  echo -e "    Decoded keys: $(echo "$decoded" | jq -r 'keys | join(", ")')"
  echo ""
  read -rp "Host: " redis_host
  read -rp "Port [6379]: " redis_port
  redis_port="${redis_port:-6379}"
fi

echo -e "\n${INFO} Connection details:"
echo -e "    Host:     ${GREEN}${redis_host}${NC}"
echo -e "    Port:     ${GREEN}${redis_port}${NC}"
if [[ -n "${auth_token:-}" ]]; then
  echo -e "    Auth:     ${YELLOW}********${NC}"
else
  echo -e "    Auth:     ${CYAN}none${NC}"
fi
if $USE_TLS; then
  echo -e "    TLS:      ${GREEN}enabled${NC}"
fi
echo ""

read -rp "Connect now? [Y/n] " go
if [[ "$go" =~ ^[Nn]$ ]]; then
  echo -e "${WARN} Aborted."
  exit 0
fi

# ── 3. Connect via ephemeral pod ───────────────────────────────────

# Build redis-cli arguments array
redis_args=(-h "$redis_host" -p "$redis_port")
if [[ -n "${auth_token:-}" ]]; then
  redis_args+=(-a "$auth_token")
fi
if $USE_TLS; then
  redis_args+=(--tls)
fi

connect() {
  # Check if a redis-client pod already exists
  pod_status=$(kubectl get pod "$POD_NAME" -n "$POD_NS" --no-headers 2>/dev/null | awk '{print $3}') || true

  if [[ "$pod_status" == "Running" ]]; then
    echo -e "${OK} Attaching to existing pod ${CYAN}${POD_NAME}${NC}. Type ${CYAN}rc${NC} to connect to Redis.\n"
    kubectl exec -it "$POD_NAME" -n "$POD_NS" -- sh
  else
    if [[ -n "$pod_status" ]]; then
      # Pod exists but not running (Completed, Error, etc.) — clean it up
      echo -e "${WARN} Cleaning up stale pod ${CYAN}${POD_NAME}${NC} (status: ${YELLOW}${pod_status}${NC})..."
      kubectl delete pod "$POD_NAME" -n "$POD_NS" --wait=false 2>/dev/null || true
      sleep 2
    fi
    # Launch pod with sleep, then exec redis-cli into it
    echo -e "${INFO} Launching pod ${CYAN}${POD_NAME}${NC} (image: ${CYAN}${POD_IMAGE}${NC})..."
    kubectl run "$POD_NAME" -n "$POD_NS" \
      --image="$POD_IMAGE" \
      --restart=Never \
      --command -- sleep infinity &>/dev/null

    # Wait for pod to be ready
    echo -e "${INFO} Waiting for pod to be ready..."
    kubectl wait --for=condition=Ready pod/"$POD_NAME" -n "$POD_NS" --timeout=60s &>/dev/null || {
      echo -e "${ERR} Pod failed to become ready."
      kubectl delete pod "$POD_NAME" -n "$POD_NS" --wait=false 2>/dev/null || true
      return 1
    }

    # Create a helper script 'rc' inside the pod that passes all extra args to redis-cli
    rc_script='#!/bin/sh\nredis-cli '"${redis_args[*]}"' "$@"'
    kubectl exec "$POD_NAME" -n "$POD_NS" \
      -- sh -c "printf '${rc_script}' > /usr/local/bin/rc && chmod +x /usr/local/bin/rc" 2>/dev/null || true

    echo -e "${OK} Dropping into pod shell.\n"
    echo -e "  ${CYAN}Usage:${NC}"
    echo -e "    ${GREEN}rc${NC}                                  → interactive redis-cli"
    echo -e "    ${GREEN}rc PING${NC}                              → single command"
    echo -e "    ${GREEN}rc --scan --pattern 'key*'${NC}           → scan keys"
    echo -e "    ${GREEN}rc --scan --pattern 'key*' | wc -l${NC}   → scan with pipes"
    echo -e ""
    kubectl exec -it "$POD_NAME" -n "$POD_NS" -- sh
  fi
}

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
