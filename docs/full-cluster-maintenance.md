# Full cluster maintenance

The `stack_maintenance` Kestra flow runs from the physical infra cluster. It targets the three Proxmox hosts and their Ceph daemons and guests. It is manual and defaults to inspection only. It does not patch Talos, infra-cluster nodes, or Kubernetes workloads.

## Before a live run

1. Verify recent restorable host and guest backups outside this flow.
2. Audit CephFS and RBD clients outside the three Proxmox hosts. The flow checks known local mounts, RBD watchers, and CephFS sessions, but the operator must confirm external clients are stopped.
3. Run with `live=false`. Review `maintenance-state.json`, host inventory, package simulation, Ceph health, and any refusal. Resolve unknowns before retrying.
4. Retire the old `ordered_shutdown`, `ordered_restart`, `recabling_shutdown`, and `recabling_startup` flows before enabling live operations; Git sync does not delete them. The stack shutdown stages and post-power-on startup stages refuse to run while a maintenance lock exists on any host. Also confirm no other host maintenance is active. The maintenance controller installs persistent host locks for a live run.
5. For the live run, set `live=true`, `confirmation=FULL OUTAGE pve-0 pve-1 pve-2`, `backup_checked=true`, and `external_ceph_clients_checked=true`.

## Sequence

Kestra displays the run as separate tasks: preflight and lock, host/package preparation, guest shutdown and client verification, Ceph shutdown, package installation, each host reboot, and service restoration. Host journals record the next allowed stage and refuse out-of-order or replayed tasks after an interrupted stage. A failed task identifies the stage that stopped; inspect its logs and the host journals before taking recovery action. Live stages run only when `live=true`.

The controller snapshots host and guest state, downloads the simulated package set, then shuts down guests gracefully with an eight-minute timeout per guest. It rechecks live power state and skips guests that are already off. It never force-stops a guest. If the shutdown command times out after a guest has stopped, the flow accepts the verified stopped state; otherwise it fails and leaves the maintenance journal for inspection. After guest shutdown, it checks that Ceph clients are absent, stops Ceph daemon instances, installs pinned packages, and reboots hosts one at a time. It then restores Ceph monitor quorum and health, starts PBS, service VMs, other guests, and app-cluster VMs in that order, and restores original guest autostart and timer states. CephFS mounts are accepted only when their mount path is configured as PVE CephFS storage and their source lists this cluster's monitor IPs. It does not power off the physical hosts.

Every mutating step is journaled in `/var/lib/homelab-ops/active/state.json` on each host. A failure leaves the locks and journal in place; stop and inspect rather than rerunning maintenance. Package transactions and uncertainty about client access require operator reconciliation.

If the execution fails during `stop-guests`, first confirm it is terminal, all three journals match, and no old worker remains. The `stack_maintenance` continuation input `continue_interrupted_guest_shutdown=true` resumes only that journaled stage, rechecks each guest, and then proceeds through the remaining maintenance tasks. Before starting it, remove only the empty `/var/lib/homelab-ops/active/executor` directory on pve-0; the continuation claims it again. This cannot resume any other interrupted stage.

## Recovery after failure

1. Confirm the original Kestra execution is terminal and its worker is no longer running. Verify all three host journals match; if they differ, reconcile manually before proceeding.
2. Run `stack_maintenance_recovery` with `live=false` to inspect the saved state.
3. When safe to transfer ownership, remove only `/var/lib/homelab-ops/active/executor` on `pve-0` (an empty lock directory). Do not remove `active`, `owner`, or `state.json`.
4. Run `stack_maintenance_recovery` with `live=true` and the same three acknowledgements. Recovery restores services and guest states; it does not repeat package installs or reboots. If a package transaction was interrupted, repair and verify it manually first.

After either path, verify Proxmox quorum and Ceph `HEALTH_OK`, then check each guest's recorded power and autostart state. Check Kubernetes nodes, ready service endpoints, and application routes separately; a running VM is not proof that the app cluster recovered. Keep the journal and inspection artifact for the change record. Use host backups to roll back package versions; the flow does not downgrade packages.

## Flow names and migration

The source names are `stack_shutdown`, `stack_startup`, `stack_maintenance`, and `stack_maintenance_recovery`. The shutdown/startup flows act on an explicit guest list (app-cluster VMs, service VMs, PBS LXC); maintenance inventories every guest, and recovery is a separate flow to prevent selecting recovery during a normal maintenance run. The Git sync flow has `delete: false`, so retired flow IDs (`ordered_shutdown`, `ordered_restart`, `recabling_shutdown`, `recabling_startup`, `full_cluster_maintenance`) are not removed from a live Kestra instance automatically. After syncing and validating the new flows, explicitly retire the old IDs. Do not change Git sync to bulk deletion: it could remove manually managed flows in the namespace.
