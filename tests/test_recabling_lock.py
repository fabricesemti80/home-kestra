import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RecablingLockTests(unittest.TestCase):
    def test_both_flows_check_maintenance_lock_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_ssh = Path(directory) / "ssh"
            fake_ssh.write_text("#!/bin/sh\ncase \"$*\" in\n"
                                "  *'/var/lib/homelab-ops/active'*) exit 42 ;;\n"
                                "esac\nexit 0\n")
            fake_ssh.chmod(0o755)
            env = {**os.environ, "PATH": directory + os.pathsep + os.environ["PATH"],
                   "WORKING_DIR": directory, "HOMELAB_SSH_PRIVATE_KEY": "test",
                   "HOMELAB_SSH_KNOWN_HOSTS": "test"}
            for script, stage in (("ordered-shutdown.sh", "stop_k8s"),
                                  ("ordered-restart.sh", "start_services")):
                with self.subTest(script=script):
                    result = subprocess.run(["bash", str(ROOT / "scripts" / script),
                                             "true", "true", stage], env=env,
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 42)


if __name__ == "__main__":
    unittest.main()
