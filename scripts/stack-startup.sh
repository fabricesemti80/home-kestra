#!/usr/bin/env bash
# Ordered full-stack startup: wait for manually powered-on hosts -> Ceph
# services + flags -> PBS LXC -> service VMs -> app-cluster VMs.
# Stage-based; each Kestra task invokes one stage. Dry-run by default.
set -euo pipefail

DRY_RUN="${1:-true}"
WAIT_MINUTES="${2:-20}"
STAGE="${3:-wait_hosts}"

SSH_USER="${HOMELAB_SSH_USER:-root}"
NODES=("10.0.40.10:pve-0" "10.0.40.11:pve-1" "10.0.40.12:pve-2")
APP_VM_IDS=(4090 4091 4092)        # k8s-ctrl-01..03 (app-cluster)
SERVICE_VM_IDS=(3050 3051 4052)    # hermes, coolify, ai
INFRA_LXC_IDS=(4016)               # proxmox-pbs-0 (backup server)

log() { printf '[%s] %s\n' "$STAGE" "$*"; }
is_dry_run() { [[ "$DRY_RUN" == "true" || "$DRY_RUN" == "1" || "$DRY_RUN" == "yes" ]]; }

setup_ssh() {
  test -n "${HOMELAB_SSH_PRIVATE_KEY:-}" || { echo "HOMELAB_SSH_PRIVATE_KEY is missing" >&2; exit 1; }
  test -n "${HOMELAB_SSH_KNOWN_HOSTS:-}" || { echo "HOMELAB_SSH_KNOWN_HOSTS is missing" >&2; exit 1; }

  export SSH_DIR="${WORKING_DIR:-/tmp}/.ssh"
  mkdir -p "$SSH_DIR"
  chmod 700 "$SSH_DIR"
  printf '%s\n' "$HOMELAB_SSH_PRIVATE_KEY" > "$SSH_DIR/id_rsa"
  printf '%s\n' "$HOMELAB_SSH_KNOWN_HOSTS" > "$SSH_DIR/known_hosts"
  chmod 600 "$SSH_DIR/id_rsa" "$SSH_DIR/known_hosts"
}

ssh_cmd() {
  local host="$1"
  shift
  ssh -i "$SSH_DIR/id_rsa" \
    -o BatchMode=yes \
    -o ConnectTimeout=10 \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$SSH_DIR/known_hosts" \
    "$SSH_USER@$host" "$@"
}

run_remote() {
  local host="$1"
  shift
  if is_dry_run; then
    log "dry-run $host: $*"
  else
    ssh_cmd "$host" "$@"
  fi
}

first_available_host() {
  local entry host
  for entry in "${NODES[@]}"; do
    host="${entry%%:*}"
    if ssh_cmd "$host" true >/dev/null 2>&1; then
      printf '%s\n' "$host"
      return 0
    fi
  done
  return 1
}

# Resolve a guest ID to "<node>:<kind>" where kind is qemu|lxc.
guest_node() {
  local id="$1"
  local host
  host="$(first_available_host)"
  ssh_cmd "$host" "for node in pve-0 pve-1 pve-2; do
    test -f /etc/pve/nodes/\$node/qemu-server/$id.conf && { echo \"\$node:qemu\"; exit 0; }
    test -f /etc/pve/nodes/\$node/lxc/$id.conf && { echo \"\$node:lxc\"; exit 0; }
  done; exit 1"
}

node_ip() {
  case "$1" in
    pve-0) printf '10.0.40.10\n' ;;
    pve-1) printf '10.0.40.11\n' ;;
    pve-2) printf '10.0.40.12\n' ;;
    *) return 1 ;;
  esac
}

guest_tool() { [[ "$1" == qemu ]] && printf 'qm' || printf 'pct'; }

wait_hosts() {
  local deadline entry host name
  deadline=$((SECONDS + WAIT_MINUTES * 60))
  while ((SECONDS < deadline)); do
    local ok=0
    for entry in "${NODES[@]}"; do
      host="${entry%%:*}"
      name="${entry##*:}"
      if ssh_cmd "$host" true >/dev/null 2>&1; then
        log "$name reachable"
        ok=$((ok + 1))
      else
        log "$name not reachable yet"
      fi
    done
    ((ok == ${#NODES[@]})) && return 0
    sleep 15
  done
  echo "not all Proxmox hosts became reachable within ${WAIT_MINUTES} minutes" >&2
  exit 1
}

start_ceph_services() {
  local entry host name
  for entry in "${NODES[@]}"; do
    host="${entry%%:*}"
    name="${entry##*:}"
    log "start Ceph services on $name"
    run_remote "$host" "systemctl start ceph-mon.target ceph-mgr.target ceph-osd.target ceph-mds.target"
  done
}

clear_ceph_flags() {
  local host
  host="$(first_available_host)"
  run_remote "$host" "ceph osd unset pause 2>&1 || true; ceph osd unset norebalance 2>&1 || true; ceph osd unset nobackfill 2>&1 || true; ceph osd unset norecover 2>&1 || true; ceph osd unset noout 2>&1 || true"
}

wait_ceph_ok() {
  local host deadline health
  host="$(first_available_host)"
  deadline=$((SECONDS + WAIT_MINUTES * 60))
  while ((SECONDS < deadline)); do
    health="$(ssh_cmd "$host" "ceph health 2>/dev/null" || true)"
    log "ceph health: ${health:-UNKNOWN}"
    [[ "$health" == "HEALTH_OK" ]] && return 0
    sleep 15
  done
  echo "Ceph did not reach HEALTH_OK within ${WAIT_MINUTES} minutes" >&2
  exit 1
}

set_guest_onboot() {
  local value="$1"
  shift
  local id loc node kind host tool
  for id in "$@"; do
    loc="$(guest_node "$id")"
    node="${loc%%:*}"
    kind="${loc##*:}"
    host="$(node_ip "$node")"
    tool="$(guest_tool "$kind")"
    run_remote "$host" "$tool set $id --onboot $value"
  done
}

start_guests() {
  local id loc node kind host tool status
  for id in "$@"; do
    loc="$(guest_node "$id")"
    node="${loc%%:*}"
    kind="${loc##*:}"
    host="$(node_ip "$node")"
    tool="$(guest_tool "$kind")"
    status="$(ssh_cmd "$host" "$tool status $id | awk '{print \$2}'")"
    log "guest $id ($kind) on $node is $status"
    [[ "$status" == "running" ]] && continue
    run_remote "$host" "$tool start $id"
  done
}

setup_ssh

case "$STAGE" in
  wait_hosts)
    wait_hosts
    ;;
  wait_ceph)
    start_ceph_services
    clear_ceph_flags
    wait_ceph_ok
    ;;
  clear_ceph_maintenance)
    clear_ceph_flags
    wait_ceph_ok
    ;;
  start_infra)
    set_guest_onboot 1 "${INFRA_LXC_IDS[@]}"
    start_guests "${INFRA_LXC_IDS[@]}"
    ;;
  start_services)
    set_guest_onboot 1 "${SERVICE_VM_IDS[@]}"
    start_guests "${SERVICE_VM_IDS[@]}"
    ;;
  start_k8s)
    set_guest_onboot 1 "${APP_VM_IDS[@]}"
    start_guests "${APP_VM_IDS[@]}"
    ;;
  *)
    echo "unknown stage: $STAGE" >&2
    exit 1
    ;;
esac
