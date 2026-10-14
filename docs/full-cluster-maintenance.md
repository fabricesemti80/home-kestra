# Full cluster maintenance

The `full_cluster_maintenance` Kestra flow runs from the physical infra cluster. It targets the three Proxmox hosts and their Ceph daemons and guests. It is manual and defaults to inspection only. It does not patch Talos, infra-cluster nodes, or Kubernetes workloads.

## Before a live run

1. Verify recent restorable host and guest backups outside this flow.
2. Audit CephFS and RBD clients outside the three Proxmox hosts. The flow checks known local mounts, RBD watchers, and CephFS sessions, but the operator must confirm external clients are stopped.
3. Run with `live=false`. Review `maintenance-state.json`, host inventory, package simulation, Ceph health, and any refusal. Resolve unknowns before retrying.
   A host CephFS mount or mapped RBD device is a hard stop. Investigate its owner and arrange a verified unmount/unmap and restoration procedure before live maintenance; do not bypass this check.
4. Confirm that no `homelab.ops` shutdown/startup flow or other host maintenance is active. The maintenance controller installs persistent host locks for a live run.
5. For the live run, set `live=true`, `confirmation=FULL OUTAGE pve-0 pve-1 pve-2`, `backup_checked=true`, and `external_ceph_clients_checked=true`.

## Sequence

The controller snapshots host and guest state, downloads the simulated package set, stops all running guests gracefully without force stopping them, checks that Ceph clients are absent, stops Ceph daemon instances, installs pinned packages, and reboots hosts one at a time. It then restores Ceph monitor quorum and health, starts previously running service guests before app-cluster VMs, and restores original guest autostart and timer states. It does not power off the physical hosts.

Every mutating step is journaled in `/var/lib/homelab-ops/active/state.json` on each host. A failure leaves the locks and journal in place; stop and inspect rather than rerunning maintenance. Package transactions and uncertainty about client access require operator reconciliation.

## Recovery after failure

1. Confirm the original Kestra execution is terminal and its worker is no longer running. Verify all three host journals match; if they differ, reconcile manually before proceeding.
2. Run the flow with `recovery=true`, `live=false` to inspect the saved state.
3. When safe to transfer ownership, remove only `/var/lib/homelab-ops/active/executor` on `pve-0` (an empty lock directory). Do not remove `active`, `owner`, or `state.json`.
4. Run with `recovery=true`, `live=true` and the same three acknowledgements. Recovery restores services and guest states; it does not repeat package installs or reboots. Hosts not yet touched by package installation are checked against their original package versions. Hosts where installation began must match the planned versions; repair an interrupted or partial transaction manually before retrying recovery.

After either path, verify Proxmox quorum and Ceph `HEALTH_OK`, then check each guest's recorded power and autostart state. Check Kubernetes nodes, ready service endpoints, and application routes separately; a running VM is not proof that the app cluster recovered. Keep the journal and inspection artifact for the change record. Use host backups to roll back package versions; the flow does not downgrade packages.

## Flow names and migration

The source names are `recabling_shutdown`, `recabling_startup`, and `full_cluster_maintenance`. The recabling flows act on a selected guest list; the maintenance flow inventories every guest. The Git sync flow has `delete: false`, so renaming the two older flow IDs does not remove `ordered_shutdown` or `ordered_restart` from a live Kestra instance. After syncing and validating the renamed flows, explicitly retire the two old IDs. Do not change Git sync to bulk deletion: it could remove manually managed flows in the namespace.
