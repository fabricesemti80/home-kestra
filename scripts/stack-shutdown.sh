#!/usr/bin/env bash
# Ordered full-stack shutdown: app-cluster VMs -> service VMs -> PBS LXC ->
# Ceph maintenance flags -> Proxmox host poweroff.
# Stage-based; each Kestra task invokes one stage. Dry-run by default.
set -euo pipefail

DRY_RUN="${1:-true}"
REQUIRE_CEPH_HEALTHY="${2:-true}"
STAGE="${3:-preflight}"

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

check_hosts() {
  local entry host name
  for entry in "${NODES[@]}"; do
    host="${entry%%:*}"
    name="${entry##*:}"
    ssh_cmd "$host" true
    log "$name reachable at $host"
  done
}

check_no_maintenance_lock() {
  local entry host name
  for entry in "${NODES[@]}"; do
    host="${entry%%:*}"
    name="${entry##*:}"
    if ! ssh_cmd "$host" "test ! -e /var/lib/homelab-ops/active"; then
      echo "maintenance lock is active on $name; refusing stack shutdown" >&2
      return 1
    fi
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

guest_tool() { [[ "$1" == qemu ]] && printf 'qm' || printf 'pct'; }

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

stop_guests() {
  local id loc node kind host tool status
  for id in "$@"; do
    loc="$(guest_node "$id")"
    node="${loc%%:*}"
    kind="${loc##*:}"
    host="$(node_ip "$node")"
    tool="$(guest_tool "$kind")"
    status="$(ssh_cmd "$host" "$tool status $id | awk '{print \$2}'")"
    log "guest $id ($kind) on $node is $status"
    [[ "$status" == "running" ]] || continue
    if ! run_remote "$host" "$tool shutdown $id --timeout 180"; then
      echo "graceful shutdown failed for guest $id; refusing later shutdown stages" >&2
      return 1
    fi
    if ! is_dry_run; then
      status="$(ssh_cmd "$host" "$tool status $id | awk '{print \$2}'")"
      [[ "$status" == "stopped" ]] || {
        echo "guest $id did not stop; refusing later shutdown stages" >&2
        return 1
      }
    fi
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

check_no_maintenance_lock

case "$STAGE" in
  preflight)
    check_hosts
    for id in "${APP_VM_IDS[@]}" "${SERVICE_VM_IDS[@]}" "${INFRA_LXC_IDS[@]}"; do
      log "guest $id located at $(guest_node "$id")"
    done
    check_ceph_health
    ;;
  stop_k8s)
    set_guest_onboot 0 "${APP_VM_IDS[@]}"
    stop_guests "${APP_VM_IDS[@]}"
    ;;
  stop_services)
    set_guest_onboot 0 "${SERVICE_VM_IDS[@]}"
    stop_guests "${SERVICE_VM_IDS[@]}"
    ;;
  stop_infra)
    set_guest_onboot 0 "${INFRA_LXC_IDS[@]}"
    stop_guests "${INFRA_LXC_IDS[@]}"
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
