#!/usr/bin/env bash
set -euo pipefail

DRY_RUN="${1:-true}"
REQUIRE_CEPH_HEALTHY="${2:-true}"
STAGE="${3:-preflight}"

SSH_USER="${HOMELAB_SSH_USER:-root}"
NODES=("10.0.40.10:pve-0" "10.0.40.11:pve-1" "10.0.40.12:pve-2")
APP_VM_IDS=(4090 4091 4092)
SERVICE_VM_IDS=(3050 3051)

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

vm_node() {
  local vmid="$1"
  local host node
  host="$(first_available_host)"
  ssh_cmd "$host" "for node in pve-0 pve-1 pve-2; do test -f /etc/pve/nodes/\$node/qemu-server/$vmid.conf && echo \$node && exit 0; done; exit 1"
}

node_ip() {
  case "$1" in
    pve-0) printf '10.0.40.10\n' ;;
    pve-1) printf '10.0.40.11\n' ;;
    pve-2) printf '10.0.40.12\n' ;;
    *) return 1 ;;
  esac
}

check_hosts() {
  local entry host name
  for entry in "${NODES[@]}"; do
    host="${entry%%:*}"
    name="${entry##*:}"
    ssh_cmd "$host" true
    log "$name reachable at $host"
  done
}

check_ceph_health() {
  local host health
  host="$(first_available_host)"
  health="$(ssh_cmd "$host" "ceph health 2>/dev/null" || true)"
  log "ceph health: ${health:-UNKNOWN}"
  if [[ "$REQUIRE_CEPH_HEALTHY" == "true" && "$health" != "HEALTH_OK" ]]; then
    echo "Ceph must be HEALTH_OK before shutdown; got ${health:-UNKNOWN}" >&2
    exit 1
  fi
}

set_vm_onboot() {
  local value="$1"
  shift
  local vmid node host
  for vmid in "$@"; do
    node="$(vm_node "$vmid")"
    host="$(node_ip "$node")"
    run_remote "$host" "qm set $vmid --onboot $value"
  done
}

stop_vms() {
  local vmid node host status
  for vmid in "$@"; do
    node="$(vm_node "$vmid")"
    host="$(node_ip "$node")"
    status="$(ssh_cmd "$host" "qm status $vmid | awk '{print \$2}'")"
    log "vm $vmid on $node is $status"
    [[ "$status" == "running" ]] || continue
    run_remote "$host" "qm shutdown $vmid --timeout 180 && test \"\$(qm status $vmid)\" = 'status: stopped'"
  done
}

set_ceph_flags() {
  local host
  host="$(first_available_host)"
  run_remote "$host" "ceph osd set noout 2>&1 && ceph osd set norecover 2>&1 && ceph osd set nobackfill 2>&1 && ceph osd set norebalance 2>&1 && ceph osd set pause 2>&1"
}

poweroff_hosts() {
  local entry host name
  for entry in "${NODES[@]}"; do
    host="${entry%%:*}"
    name="${entry##*:}"
    log "poweroff $name"
    run_remote "$host" "shutdown -h now" || true
  done
}

setup_ssh

case "$STAGE" in
  preflight)
    check_hosts
    for vmid in "${APP_VM_IDS[@]}"; do log "vm $vmid located on $(vm_node "$vmid")"; done
    check_ceph_health
    ;;
  stop_k8s)
    set_vm_onboot 0 "${APP_VM_IDS[@]}"
    stop_vms "${APP_VM_IDS[@]}"
    ;;
  stop_services)
    if ((${#SERVICE_VM_IDS[@]} == 0)); then
      log "no service VMs configured"
    else
      set_vm_onboot 0 "${SERVICE_VM_IDS[@]}"
      stop_vms "${SERVICE_VM_IDS[@]}"
    fi
    ;;
  set_ceph_maintenance)
    set_ceph_flags
    ;;
  poweroff_hosts)
    poweroff_hosts
    ;;
  *)
    echo "unknown stage: $STAGE" >&2
    exit 1
    ;;
esac
