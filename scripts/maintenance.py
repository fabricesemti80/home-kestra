#!/usr/bin/env python3
"""Full outage controller. No commands run at import time; stdlib only."""
import argparse
from contextlib import AbstractContextManager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

HOSTS = {"pve-0": "10.0.40.10", "pve-1": "10.0.40.11", "pve-2": "10.0.40.12"}
FIRST = "pve-0"
APP = {4090, 4091, 4092}
SERVICES = {3050, 3051}
CONFIRM = "FULL OUTAGE pve-0 pve-1 pve-2"
SUPPORTED = {(12, 8, 18), (12, 8, 19), (13, 9, 19)}
UPGRADE = ["apt-get", "-s", "--no-remove", "--with-new-pkgs", "upgrade"]


class Refused(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise Refused(message)


def major(version):
    match = re.match(r"(?:\d+:)?(\d+)", version)
    require(match, "unparseable version")
    return int(match[1])


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class SSH(AbstractContextManager):
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="maintenance-ssh-")
        self.directory = Path(self.temp.name)
        for env, name in (("HOMELAB_SSH_PRIVATE_KEY", "key"),
                          ("HOMELAB_SSH_KNOWN_HOSTS", "known_hosts")):
            value = os.environ.pop(env, "")
            require(value.strip(), env + " is required")
            path = self.directory / name
            path.touch(mode=0o600)
            path.write_text(value + "\n")
        self.helper = Path(__file__).with_name("maintenance_host.py").read_text()

    def __exit__(self, *args):
        self.temp.cleanup()

    def call(self, host, action, **data):
        require(host in HOSTS, "host is not allowlisted")
        args = ["ssh", "-T", "-i", str(self.directory / "key"),
                "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10",
                "-o", "ServerAliveCountMax=3", "-o", "StrictHostKeyChecking=yes",
                "-o", "UserKnownHostsFile=" + str(self.directory / "known_hosts"),
                "root@" + HOSTS[host], "python3 -c " + shlex.quote(self.helper)]
        try:
            result = subprocess.run(args, input=json.dumps({"action": action, **data}),
                                    text=True, capture_output=True, timeout=660)
        except subprocess.TimeoutExpired:
            raise Refused(f"{host}: {action} timed out; state may be partial") from None
        require(result.returncode == 0, f"{host}: {action} failed; state may be partial")
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise Refused(f"{host}: invalid helper response") from None


class Controller:
    def __init__(self, runner, output="maintenance-state.json", timeout=1200,
                 clock=time.monotonic, sleep=time.sleep):
        self.runner, self.output, self.timeout = runner, Path(output), timeout
        self.clock, self.sleep = clock, sleep
        self.state = None

    def cmd(self, host, *argv):
        return self.runner.call(host, "command", argv=list(argv))

    def js(self, host, *argv):
        return json.loads(self.cmd(host, *argv))

    def wait(self, label, predicate):
        end = self.clock() + self.timeout
        while self.clock() < end:
            try:
                if predicate():
                    return
            except (Refused, ValueError, KeyError):
                pass
            self.sleep(10)
        raise Refused(label + " timed out; no force/automatic retry")

    def artifact(self):
        self.output.touch(mode=0o600, exist_ok=True)
        self.output.chmod(0o600)
        self.output.write_text(json.dumps(self.state, indent=2, sort_keys=True) + "\n")

    def journal(self, phase, pending=None):
        self.state["sequence"] += 1
        self.state["phase"], self.state["pending"] = phase, pending
        self.artifact()
        for host in HOSTS:
            self.runner.call(host, "save", operation=self.state["operation"], state=self.state)

    def change(self, label, host, action="command", **data):
        self.journal(label, {"host": host, "action": action, **data})
        value = self.runner.call(host, action, **data)
        self.journal(label + ":done")
        return value

    def mutate(self, label, host, *argv):
        return self.change(label, host, argv=list(argv))

    @staticmethod
    def quorum(inventory):
        nodes = {x["name"]: x for x in inventory if x.get("type") == "node"}
        clusters = [x for x in inventory if x.get("type") == "cluster"]
        require(set(nodes) == set(HOSTS), "PVE membership must be exactly the three target nodes")
        require(len(clusters) == 1 and clusters[0].get("quorate") == 1, "PVE lacks quorum")
        for name, item in nodes.items():
            require(item.get("ip") == HOSTS[name] and item.get("online") == 1,
                    "PVE membership identity/offline mismatch")

    def validate_inventory(self, snapshots, recovery=False):
        require(set(snapshots) == set(HOSTS), "incomplete host snapshot")
        versions = set()
        ids = set()
        for name, host in snapshots.items():
            require(host["name"] == name, "SSH hostname mismatch")
            require(host["os"] == "debian", "only Debian PVE is supported")
            version = (major(host["os_version"]), major(host["versions"]["pve-manager"]),
                       major(host["versions"]["ceph-common"]))
            require(version in SUPPORTED, "unreviewed Debian/PVE/Ceph major tuple")
            versions.add(version)
            self.quorum(host["cluster"])
            require(not host["ha"], "HA resources unsupported; no HA/watchdog manipulation")
            require(not host["mounts"] and not host["rbd_devices"],
                    f"{name}: host Ceph mounts or mapped RBD clients unsupported")
            require(not host["policy_exists"], "existing policy-rc.d unsupported")
            require(not host["active"] or recovery, "incomplete/overlapping operation blocks new run")
            require(host["state_fstype"] in {"ext4", "xfs", "zfs", "btrfs"},
                    "journal filesystem must be host-local")
            require(host["root"], "root SSH required")
            units = host["units"]
            require("cron.service" in units and "pve-guests.service" in units,
                    "required scheduler/autostart units missing")
            for unit, original in units.items():
                require(re.fullmatch(r"[A-Za-z0-9_.@:\\-]+", unit), "unsafe unit name")
                require(original["enabled"] in {"enabled", "disabled", "static", "masked"},
                        "unsupported runtime/linked/generated unit")
                require(original["active"] in {"active", "inactive"}, "unit not in stable state")
                if unit.startswith("ceph-"):
                    require(original["active"] == "active" and original["enabled"] != "masked",
                            "all Ceph instances must initially be active and unmasked")
            for guest in host["guests"]:
                require(guest["id"] not in ids, "duplicate guest ID")
                ids.add(guest["id"])
                require(guest["node"] == name and guest["kind"] in {"qemu", "lxc"},
                        "invalid guest identity")
                require(guest["status"] in {"running", "stopped"} and not guest["lock"],
                        "locked/migrating/suspended guests unsupported")
                require(guest["qmpstatus"] in {None, "running", "stopped"},
                        "paused/suspended QEMU unsupported")
                require(guest["onboot"] in {None, 0, 1}, "invalid onboot setting")
        require(len(versions) == 1, "mixed OS/PVE/Ceph majors unsupported")
        require(APP | SERVICES <= ids, "expected service/app VMs missing")
        require(all(g["kind"] == "qemu" for h in snapshots.values()
                    for g in h["guests"] if g["id"] in APP | SERVICES),
                "expected service/app IDs must be QEMU")

    def ceph_inventory(self, snapshots):
        health = self.js(FIRST, "ceph", "status", "--format", "json")
        require(health["health"]["status"] == "HEALTH_OK", "Ceph must initially be HEALTH_OK")
        require(health.get("pgmap", {}).get("num_pgs", 0) > 0, "Ceph has no verified PGs")
        modules = self.js(FIRST, "ceph", "mgr", "module", "ls", "--format", "json")
        if "cephadm" in modules["enabled_modules"]:
            require(not self.js(FIRST, "ceph", "orch", "ps", "--format", "json"),
                    "cephadm daemons unsupported")
        mons = self.js(FIRST, "ceph", "mon", "dump", "--format", "json")["mons"]
        require({m["name"] for m in mons} == set(HOSTS), "monitor membership mismatch")
        for mon in mons:
            addresses = mon.get("public_addrs", {}).get("addrvec", [])
            require(addresses and all(a["addr"].split(":")[0] == HOSTS[mon["name"]]
                                      for a in addresses), "monitor address mismatch")
        osds = self.js(FIRST, "ceph", "osd", "metadata", "--format", "json")
        mgrs = self.js(FIRST, "ceph", "mgr", "metadata", "--format", "json")
        mdss = self.js(FIRST, "ceph", "mds", "metadata", "--format", "json")
        expected = set()
        for kind, items, key in (("osd", osds, "id"), ("mgr", mgrs, "name"),
                                 ("mds", mdss, "name"), ("mon", mons, "name")):
            require(items or kind == "mds", "missing Ceph daemon class")
            for item in items:
                node = item["name"] if kind == "mon" else item["hostname"].split(".")[0]
                require(node in HOSTS, "external Ceph daemon unsupported")
                unit = f"ceph-{kind}@{item[key]}.service"
                require(unit in snapshots[node]["units"], "Ceph daemon/unit inventory mismatch")
                expected.add((node, unit))
        actual = {(n, u) for n, h in snapshots.items() for u in h["units"] if u.startswith("ceph-")}
        require(actual == expected, "unaccounted Ceph daemon units")
        flags = self.js(FIRST, "ceph", "osd", "dump", "--format", "json")["flags"].split(",")
        return {"fsid": health["fsid"], "flags": flags,
                "added_flags": [f for f in ("noout",) if f not in flags]}

    def packages(self, host, versions):
        # No apt update in dry-run. Pins are refreshed and revalidated before download.
        text = self.cmd(host, *UPGRADE)
        require(not re.search(r"^(Remv|Purg) ", text, re.M), "package removals forbidden")
        packages = {}
        for line in text.splitlines():
            if not line.startswith("Inst "):
                continue
            match = re.match(r"Inst ([a-z0-9+.:~-]+)(?: \[[^\]]+\])? \(([^ ]+) ", line)
            require(match, "unrecognized simulated package change")
            package, version = match.groups()
            require(re.fullmatch(r"[A-Za-z0-9.+:~_-]+", version), "unsafe package version")
            packages[package] = version
            if package in versions:
                require(major(version) == major(versions[package]), "major upgrade forbidden")
            if package.startswith(("ceph", "librados", "librbd")):
                require(major(version) == major(versions["ceph-common"]), "Ceph major drift")
        require("proxmox-ve" not in text or "Remv proxmox-ve" not in text,
                "proxmox-ve must remain installed")
        return packages

    def preflight(self):
        snapshots = {h: self.runner.call(h, "inventory") for h in HOSTS}
        self.validate_inventory(snapshots)
        ceph = self.ceph_inventory(snapshots)
        packages = {h: self.packages(h, snapshots[h]["versions"]) for h in HOSTS}
        original = {"hosts": snapshots, "ceph": ceph}
        self.state = {"schema": 2, "operation": str(uuid.uuid4()), "sequence": 0,
                      "phase": "planned", "pending": None, "original": original,
                      "original_digest": digest(original), "packages": packages,
                      "packages_touched": [],
                      "masks": {h: [] for h in HOSTS}, "policies": [],
                      "rebooted": [], "flags_intended": []}
        self.artifact()
        return snapshots

    def guests(self):
        return [g for h in self.state["original"]["hosts"].values() for g in h["guests"]]

    def guest_command(self, guest, action, *args):
        tool = "qm" if guest["kind"] == "qemu" else "pct"
        return [tool, action, str(guest["id"]), *args]

    def guest_status(self, guest):
        return self.cmd(guest["node"], *self.guest_command(guest, "status")).split()[-1]

    def mask(self, host, unit, stop=True):
        original = self.state["original"]["hosts"][host]["units"][unit]
        if original["enabled"] != "masked":
            self.state["masks"][host].append(unit)
            self.mutate("mask:" + unit, host, "systemctl", "mask", unit)
        if stop and original["active"] == "active":
            self.mutate("stop:" + unit, host, "systemctl", "stop", unit)

    def all_stopped(self):
        for host in HOSTS:
            for kind in ("qemu", "lxc"):
                guests = self.js(host, "pvesh", "get", f"/nodes/{host}/{kind}",
                                 "--output-format", "json")
                require(all(g["status"] == "stopped" for g in guests), "guests remain running")
                require({int(g["vmid"]) for g in guests} ==
                        {g["id"] for g in self.guests() if g["node"] == host and g["kind"] == kind},
                        "guest inventory drift")

    def clients_absent(self):
        # Acknowledgement covers external clients not visible through these probes.
        pools = self.js(FIRST, "ceph", "osd", "pool", "ls", "--format", "json")
        for pool in pools:
            apps = self.js(FIRST, "ceph", "osd", "pool", "application", "get", pool,
                           "--format", "json")
            require(set(apps) <= {"rbd", "cephfs", "mgr"}, "unsupported Ceph pool application")
            if "rbd" not in apps:
                continue
            require(not self.js(FIRST, "rbd", "namespace", "list", "--pool", pool,
                                "--format", "json"), "RBD namespaces unsupported")
            for image in self.js(FIRST, "rbd", "ls", "--pool", pool, "--format", "json"):
                status = self.js(FIRST, "rbd", "status", "--pool", pool, "--image", image,
                                 "--format", "json")
                require(not status["watchers"], "RBD watchers remain")
        for host, saved in self.state["original"]["hosts"].items():
            for unit in saved["units"]:
                if unit.startswith("ceph-mds@"):
                    daemon = unit[len("ceph-mds@"):-len(".service")]
                    clients = self.js(host, "ceph", "tell", "mds." + daemon,
                                      "client", "ls", "--format", "json")
                    require(clients == [], "CephFS sessions remain or response unsupported")

    def verify_quorum(self):
        for host in HOSTS:
            self.quorum(self.js(host, "pvesh", "get", "/cluster/status", "--output-format", "json"))
        return True

    def reboot(self, host):
        old = self.cmd(host, "cat", "/proc/sys/kernel/random/boot_id")
        self.mutate("reboot:" + host, host, "systemd-run", "--unit=homelab-maintenance-reboot",
                    "--on-active=5s", "/usr/bin/systemctl", "reboot")
        self.wait("changed boot ID " + host,
                  lambda: self.cmd(host, "cat", "/proc/sys/kernel/random/boot_id") != old)
        require(self.cmd(host, "hostname", "-s") == host, "reboot identity mismatch")
        self.wait("PVE quorum after " + host, self.verify_quorum)
        for unit in self.state["masks"][host]:
            require(self.cmd(host, "systemctl", "show", "--property=UnitFileState",
                             "--value", unit) == "masked",
                    "persistent mask lost after reboot")
        self.state["rebooted"].append(host)
        self.journal("reboot-verified:" + host)

    def execute(self, dry_run=True, confirmation="", backup=False, audit=False):
        self.preflight()
        if dry_run:
            print("Dry-run inspected all hosts; no host writes. Review maintenance-state.json.")
            return
        require(confirmation == CONFIRM and backup and audit,
                "live operation requires exact confirmation, backup and Ceph-client audit")
        # Fixed host first closes races with legacy flows. Locks intentionally survive failures.
        for host in HOSTS:
            self.runner.call(host, "lock", operation=self.state["operation"])
        self.runner.call(FIRST, "executor", operation=self.state["operation"])
        self.journal("locked")
        # Re-inventory after lock, before host settings change; detect the preflight/lock race.
        current = {h: self.runner.call(h, "inventory") for h in HOSTS}
        for host in HOSTS:
            current[host]["active"] = False
        require(current == self.state["original"]["hosts"], "inventory changed while acquiring lock")
        for host in HOSTS:
            saved = self.state["original"]["hosts"][host]
            # Stop/mask all system timers and cron, including the known Sunday Ceph job.
            for unit in saved["units"]:
                if unit.endswith(".timer") or unit == "cron.service":
                    self.mask(host, unit)
            self.mask(host, "pve-guests.service", stop=False)
            self.state["policies"].append(host)
            self.change("policy-install", host, "policy_install")
            self.mutate("refresh-packages", host, "apt-get", "update", "--error-on=any")
            packages = self.packages(host, saved["versions"])
            self.state["packages"][host] = packages
            pins = [f"{p}={v}" for p, v in sorted(packages.items())]
            if pins:
                self.mutate("download-packages", host, "apt-get", "--assume-yes", "--no-remove",
                            "--download-only", "install", *pins)
        # Download/dependency failures abort here, before guest shutdown.
        for guest in self.guests():
            self.mutate("disable-onboot", guest["node"],
                        *self.guest_command(guest, "set", "--onboot", "0"))
        for guest in sorted(self.guests(), key=lambda g: (g["id"] not in APP, g["id"])):
            if guest["status"] == "running":
                self.mutate("graceful-shutdown", guest["node"],
                            *self.guest_command(guest, "shutdown", "--timeout", "180"))
                require(self.guest_status(guest) == "stopped", "graceful shutdown incomplete")
        self.all_stopped()
        self.clients_absent()
        for flag in self.state["original"]["ceph"]["added_flags"]:
            self.state["flags_intended"].append(flag)
            self.mutate("ceph-flag:" + flag, FIRST, "ceph", "osd", "set", flag)
        for kind in ("mds", "osd", "mgr", "mon"):
            for host, saved in self.state["original"]["hosts"].items():
                for unit in saved["units"]:
                    if unit.startswith("ceph-" + kind + "@"):
                        self.mask(host, unit)
        for host in HOSTS:
            require(self.packages(host, self.state["original"]["hosts"][host]["versions"]) ==
                    self.state["packages"][host], "simulated package manifest drift")
            pins = [f"{p}={v}" for p, v in sorted(self.state["packages"][host].items())]
            if pins:
                self.state["packages_touched"].append(host)
                self.journal("install-packages-intent:" + host)
                self.mutate("install-packages", host, "env", "DEBIAN_FRONTEND=noninteractive",
                            "NEEDRESTART_MODE=l", "apt-get", "--assume-yes", "--no-remove",
                            "--no-download", "-o", "Dpkg::Options::=--force-confold", "install", *pins)
            self.verify_packages(host)
        for host in HOSTS:
            self.reboot(host)
        self.restore()

    def verify_packages(self, host):
        require(not self.cmd(host, "dpkg", "--audit"), "dpkg needs manual repair")
        for package, version in self.state["packages"][host].items():
            require(self.cmd(host, "dpkg-query", "-W", "-f=${Version}", package) == version,
                    "package transaction incomplete; operator repair required")
        for package, old in self.state["original"]["hosts"][host]["versions"].items():
            require(major(self.cmd(host, "dpkg-query", "-W", "-f=${Version}", package)) ==
                    major(old), "installed major changed")

    def restore(self):
        self.verify_quorum()
        for host in HOSTS:
            if host in self.state["packages_touched"]:
                self.verify_packages(host)
            else:
                require(not self.cmd(host, "dpkg", "--audit"), "dpkg needs manual repair")
                for package, version in self.state["original"]["hosts"][host]["versions"].items():
                    require(self.cmd(host, "dpkg-query", "-W", "-f=${Version}", package) == version,
                            "original package state changed before installation")
        # Only recorded units are touched, only our masks removed. Original enabled bits untouched.
        for kind in ("mon", "mgr", "osd", "mds"):
            for host, saved in self.state["original"]["hosts"].items():
                for unit, original in saved["units"].items():
                    if not unit.startswith("ceph-" + kind + "@"):
                        continue
                    if unit in self.state["masks"][host]:
                        self.mutate("unmask:" + unit, host, "systemctl", "unmask", unit)
                    if original["active"] == "active":
                        self.mutate("start:" + unit, host, "systemctl", "start", unit)
            if kind == "mon":
                def mon_quorum():
                    result = self.js(FIRST, "ceph", "quorum_status", "--format", "json")
                    return set(result["quorum_names"]) == set(HOSTS)
                self.wait("Ceph monitor quorum", mon_quorum)
        require(self.js(FIRST, "ceph", "status", "--format", "json")["fsid"] ==
                self.state["original"]["ceph"]["fsid"], "Ceph FSID mismatch")
        for flag in self.state["flags_intended"]:
            self.mutate("clear-own-flag:" + flag, FIRST, "ceph", "osd", "unset", flag)
        self.wait("Ceph HEALTH_OK", lambda: self.js(
            FIRST, "ceph", "status", "--format", "json")["health"]["status"] == "HEALTH_OK")
        for guest in sorted(self.guests(), key=lambda g: (
                0 if g["id"] in SERVICES else 2 if g["id"] in APP else 1, g["id"])):
            status = self.guest_status(guest)
            if guest["status"] == "running" and status == "stopped":
                self.mutate("restore-running-guest", guest["node"], *self.guest_command(guest, "start"))
            require(self.guest_status(guest) == guest["status"], "guest status mismatch")
        for guest in self.guests():
            args = ["--delete", "onboot"] if guest["onboot"] is None else [
                "--onboot", str(guest["onboot"])]
            self.mutate("restore-onboot", guest["node"], *self.guest_command(guest, "set", *args))
        for host in self.state["policies"]:
            self.change("restore-policy", host, "policy_remove")
        for host, saved in self.state["original"]["hosts"].items():
            for unit, original in saved["units"].items():
                if unit.startswith("ceph-"):
                    continue
                if unit in self.state["masks"][host]:
                    self.mutate("restore-mask:" + unit, host, "systemctl", "unmask", unit)
                # pve-guests is a oneshot: never start it here (could start stopped guests).
                if unit != "pve-guests.service" and original["active"] == "active":
                    self.mutate("restore-scheduler:" + unit, host, "systemctl", "start", unit)
        self.journal("complete")
        # Coordinator archived LAST; another operation cannot enter during cleanup.
        for host in reversed(HOSTS):
            self.runner.call(host, "archive", operation=self.state["operation"])
        print("Maintenance complete; guest power states verified. Kubernetes readiness NOT verified.")

    def recover(self, dry_run, confirmation, backup, audit):
        states = [self.runner.call(h, "load") for h in HOSTS]
        require(all(s == states[0] for s in states), "divergent journals; manual reconciliation required")
        self.state = states[0]
        require(self.state.get("schema") == 2 and
                self.state.get("original_digest") == digest(self.state.get("original")),
                "invalid recovery snapshot")
        require(re.fullmatch(r"[a-f0-9-]{36}", self.state.get("operation", "")),
                "invalid operation ID")
        self.validate_inventory(self.state["original"]["hosts"])
        for host in HOSTS:
            require(self.cmd(host, "hostname", "-s") == host, "recovery hostname mismatch")
        require(not self.state.get("pending") or self.state["phase"] != "install-packages",
                "uncertain package transaction; manual repair/reconciliation required")
        if dry_run:
            self.artifact()
            print("Recovery inspection only; no host writes.")
            return
        require(confirmation == CONFIRM and backup and audit, "recovery acknowledgements required")
        # Persistent executor requires human clearance after original worker is proven dead.
        self.runner.call(FIRST, "executor", operation=self.state["operation"])
        self.restore()  # Never apt update/install/download, reboot, or repeat shutdown.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("maintain", "recover"), default="maintain")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    dry_run = not (args.live or os.environ.get("MAINTENANCE_LIVE", "").lower() == "true")
    controller = None
    try:
        with SSH() as runner:
            controller = Controller(runner)
            options = {"dry_run": dry_run,
                       "confirmation": os.environ.get("MAINTENANCE_CONFIRMATION", ""),
                       "backup": os.environ.get("MAINTENANCE_BACKUP_ACK", "").lower() == "true",
                       "audit": os.environ.get("MAINTENANCE_CLIENT_AUDIT_ACK", "").lower() == "true"}
            if args.mode == "recover" or os.environ.get("MAINTENANCE_RECOVERY", "").lower() == "true":
                controller.recover(**options)
            else:
                controller.execute(**options)
    except Exception as exc:
        # Never dump subprocess args/env, remote output or credentials.
        phase = controller.state["phase"] if controller and controller.state else "preflight"
        detail = "No host changes were made." if dry_run else (
            "Locks/state retained; use operator recovery runbook.")
        print(f"FAILED at {phase}: {exc if isinstance(exc, Refused) else type(exc).__name__}. "
              + detail, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
