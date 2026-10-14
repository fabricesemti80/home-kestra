import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from maintenance import Controller, Refused  # noqa: E402


class CephClientGateTests(unittest.TestCase):
    def controller(self, watchers):
        class Runner:
            def call(self, host, action, **data):
                argv = data["argv"]
                if argv[:4] == ["ceph", "osd", "pool", "ls"]:
                    return '["rbd"]'
                if argv[:5] == ["ceph", "osd", "pool", "application", "get"]:
                    return '{"rbd": {}}'
                if argv[:3] == ["rbd", "namespace", "list"]:
                    return "[]"
                if argv[:2] == ["rbd", "ls"]:
                    return '["vm-disk"]'
                if argv[:2] == ["rbd", "status"]:
                    return '{"watchers": ' + watchers + '}'
                if argv[:2] == ["ceph", "tell"]:
                    raise AssertionError("CephFS sessions are handled by the ordered daemon shutdown")
                raise AssertionError(argv)

        controller = Controller(Runner())
        controller.state = {"original": {"hosts": {"pve-0": {"units": {
            "ceph-mds@pve-0.service": {"active": "active"}}}}}}
        return controller

    def test_refuses_active_rbd_client(self):
        with self.assertRaisesRegex(Refused, "RBD watchers remain"):
            self.controller('[{"address": "client"}]').clients_absent()

    def test_accepts_no_rbd_clients(self):
        self.controller("[]").clients_absent()


class CephMaintenanceFlagTests(unittest.TestCase):
    def test_stop_sets_all_flags_missing_from_saved_original_state(self):
        controller = Controller(None)
        controller.state = {"original": {"ceph": {"flags": ["noout"]},
                                           "hosts": {host: {"units": {}}
                                                     for host in ("pve-0", "pve-1", "pve-2")}},
                            "flags_intended": [], "masks": {"pve-0": [], "pve-1": [], "pve-2": []}}
        calls = []
        controller.mutate = lambda *args: calls.append(args)
        controller.stop_ceph()
        self.assertEqual([call[-1] for call in calls if call[0].startswith("ceph-flag:")],
                         ["norecover", "nobackfill", "norebalance", "pause"])


class GuestShutdownTests(unittest.TestCase):
    def controller(self, status_responses):
        guest = {"id": 4091, "kind": "qemu", "node": "pve-1", "status": "running"}
        controller = Controller(None)
        controller.state = {"original": {"hosts": {"pve-1": {"guests": [guest]}}}}
        statuses = iter(status_responses)
        controller.guest_status = lambda _: next(statuses)
        controller.all_stopped = lambda: None
        controller.clients_absent = lambda: None
        return controller

    def test_already_stopped_guest_is_skipped(self):
        controller = self.controller(["stopped"])
        calls = []
        controller.mutate = lambda *args: calls.append(args)
        controller.stop_guests()
        self.assertEqual(calls, [])

    def test_shutdown_uses_extended_timeout_and_accepts_verified_stop_after_command_error(self):
        controller = self.controller(["running", "stopped", "stopped"])
        calls = []

        def mutate(*args):
            calls.append(args)
            raise Refused("host command failed")

        controller.mutate = mutate
        controller.stop_guests()
        self.assertEqual(calls[0][2:], ("qm", "shutdown", "4091", "--timeout", "480"))

    def test_running_guest_after_shutdown_failure_stops_without_force(self):
        controller = self.controller(["running", "running"])
        controller.mutate = lambda *_: (_ for _ in ()).throw(Refused("command failed"))
        with self.assertRaisesRegex(Refused, "without force-stopping it"):
            controller.stop_guests()


class InterruptedGuestStageTests(unittest.TestCase):
    def test_resume_claims_executor_and_runs_only_interrupted_guest_stage(self):
        calls = []

        class Runner:
            def call(self, host, action, **data):
                calls.append((host, action, data))

        controller = Controller(Runner())
        controller.state = {"operation": "op", "next_stage": "stop-guests",
                            "stage_in_progress": "stop-guests", "sequence": 0,
                            "original": {"hosts": {"pve-1": {"guests": [
                                {"id": 4091, "kind": "qemu", "node": "pve-1", "status": "running"}]}}},
                            "pending": {"host": "pve-1", "action": "command",
                                        "argv": ["qm", "shutdown", "4091", "--timeout", "180"]}}
        controller.load_state = lambda: None
        controller.stop_guests = lambda: calls.append(("stage", "stop-guests", {}))
        controller.journal = lambda phase: calls.append(("journal", phase, {}))
        controller.run_stage("stop-guests", dry_run=False, confirmation="FULL OUTAGE pve-0 pve-1 pve-2",
                             backup=True, audit=True, resume_stop_guests=True)
        self.assertEqual(calls[0], ("pve-0", "executor", {"operation": "op"}))
        self.assertIn(("stage", "stop-guests", {}), calls)

    def test_resume_rejects_a_different_interrupted_stage(self):
        controller = Controller(None)
        controller.state = {"operation": "op", "next_stage": "stop-ceph",
                            "stage_in_progress": "stop-ceph", "sequence": 0, "pending": None}
        controller.load_state = lambda: None
        with self.assertRaisesRegex(Refused, "not stopped in the interrupted stop-guests stage"):
            controller.run_stage("stop-guests", dry_run=False,
                                 confirmation="FULL OUTAGE pve-0 pve-1 pve-2",
                                 backup=True, audit=True, resume_stop_guests=True)


if __name__ == "__main__":
    unittest.main()
