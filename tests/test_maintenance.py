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
                raise AssertionError(argv)

        controller = Controller(Runner())
        controller.state = {"original": {"hosts": {"pve-0": {"units": {}}}}}
        return controller

    def test_refuses_active_rbd_client(self):
        with self.assertRaisesRegex(Refused, "RBD watchers remain"):
            self.controller('[{"address": "client"}]').clients_absent()

    def test_accepts_no_rbd_clients(self):
        self.controller("[]").clients_absent()


if __name__ == "__main__":
    unittest.main()
