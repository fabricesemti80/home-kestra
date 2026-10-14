import json
from contextlib import nullcontext, redirect_stderr
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import maintenance  # noqa: E402
import maintenance_host  # noqa: E402


class InventoryTests(unittest.TestCase):
    def test_cephfs_storage_paths_ignore_other_storage_types(self):
        config = """dir: local
 path /var/lib/vz
cephfs: ceph-store
 path /mnt/pve/ceph-store
 content backup
rbd: ceph-rbd
 pool vms
"""
        self.assertEqual(maintenance_host.cephfs_paths(config), {"/mnt/pve/ceph-store"})

    def test_auxiliary_ceph_unit_is_ignored_and_loaded_daemon_is_discovered(self):
        def command(*args):
            if args[1] == "list-unit-files":
                return "ceph-crash.service enabled\ncron.service enabled"
            return "ceph-crash.service loaded active running\nceph-mds@pve-0.service loaded active running"

        def status(args, **_):
            return SimpleNamespace(stdout="enabled\n" if args[1] == "is-enabled" else "active\n")

        with patch.object(maintenance_host, "run", side_effect=command), patch.object(
            maintenance_host.subprocess, "run", side_effect=status
        ):
            found = maintenance_host.units()
        self.assertNotIn("ceph-crash.service", found)
        self.assertEqual(found["ceph-mds@pve-0.service"], {"enabled": "enabled", "active": "active"})


class ControllerReviewTests(unittest.TestCase):
    def test_guest_restore_order_is_pbs_services_other_guests_then_kubernetes(self):
        guests = [{"id": guest_id} for guest_id in (4090, 4016, 4052, 3050, 7000)]
        self.assertEqual([g["id"] for g in sorted(guests, key=maintenance.guest_restore_order)],
                         [4016, 3050, 4052, 7000, 4090])

    def test_failed_inspection_does_not_claim_recovery_is_needed(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["maintenance.py"]), patch.object(
            maintenance, "SSH", return_value=nullcontext(None)
        ), patch.object(maintenance.Controller, "execute", side_effect=maintenance.Refused(
            "inventory refused"
        )), patch.dict("os.environ", {"MAINTENANCE_LIVE": "false"}), redirect_stderr(output):
            self.assertEqual(maintenance.main(), 1)
        self.assertIn("No host changes were made", output.getvalue())

    def test_available_but_disabled_cephadm_does_not_call_orchestrator(self):
        calls = []
        responses = {
            ("ceph", "status"): {"health": {"status": "HEALTH_OK"}, "pgmap": {"num_pgs": 1}, "fsid": "test"},
            ("ceph", "mgr", "module", "ls"): {"enabled_modules": [], "disabled_modules": [{"name": "cephadm"}]},
            ("ceph", "mon", "dump"): {"mons": [
                {"name": h, "public_addrs": {"addrvec": [{"addr": ip + ":3300/0"}]}}
                for h, ip in maintenance.HOSTS.items()]},
            ("ceph", "osd", "metadata"): [{"id": str(i), "hostname": h}
                                            for i, h in enumerate(maintenance.HOSTS)],
            ("ceph", "mgr", "metadata"): [{"name": h, "hostname": h} for h in maintenance.HOSTS],
            ("ceph", "mds", "metadata"): [],
            ("ceph", "osd", "dump"): {"flags": ""},
        }

        class Runner:
            def call(self, host, action, **data):
                argv = data["argv"]
                calls.append(argv)
                return json.dumps(responses[tuple(argv[:2]) if argv[1] == "status" else tuple(argv[:-2])])

        snapshots = {h: {"mounts": {}, "units": {f"ceph-mon@{h}.service": {},
                                   f"ceph-mgr@{h}.service": {},
                                   f"ceph-osd@{i}.service": {}}}
                     for i, h in enumerate(maintenance.HOSTS)}
        maintenance.Controller(Runner()).ceph_inventory(snapshots)
        self.assertFalse(any(argv[:3] == ["ceph", "orch", "ps"] for argv in calls))

    def test_reboot_accepts_masked_unit_state(self):
        controller = maintenance.Controller(None, sleep=lambda _: None)
        controller.state = {"masks": {"pve-0": ["cron.service"]}, "rebooted": []}
        calls = []
        boot_ids = iter(["old", "new"])

        def command(host, *argv):
            calls.append(argv)
            if argv[0] == "cat":
                return next(boot_ids)
            if argv[0] == "hostname":
                return host
            if argv[0] == "systemctl":
                return "masked"
            raise AssertionError(argv)

        controller.cmd = command
        controller.mutate = lambda *args: None
        controller.wait = lambda label, predicate: self.assertTrue(predicate())
        controller.verify_quorum = lambda: True
        controller.journal = lambda *args: None
        controller.reboot("pve-0")
        self.assertIn(("systemctl", "show", "--property=UnitFileState", "--value", "cron.service"), calls)

    def test_restore_before_install_checks_original_versions(self):
        controller = maintenance.Controller(None)
        controller.state = {"packages_touched": [], "original": {
            "hosts": {h: {"versions": {"pve-manager": "8.4"}, "units": {}}
                      for h in maintenance.HOSTS}}}
        calls = []
        controller.verify_quorum = lambda: True
        controller.verify_packages = lambda host: self.fail("target versions checked before install")
        controller.cmd = lambda host, *argv: calls.append((host, argv)) or (
            "" if argv[:2] == ("dpkg", "--audit") else "8.4")
        controller.wait = lambda *args: (_ for _ in ()).throw(RuntimeError("past package gate"))
        with self.assertRaisesRegex(RuntimeError, "past package gate"):
            controller.restore()
        self.assertEqual(sum(argv[0] == "dpkg-query" for _, argv in calls), len(maintenance.HOSTS))


if __name__ == "__main__":
    unittest.main()
