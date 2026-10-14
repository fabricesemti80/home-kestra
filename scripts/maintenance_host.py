"""Executed over strict SSH via stdin; never installed on a hypervisor."""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path("/var/lib/homelab-ops")
ACTIVE = ROOT / "active"
POLICY = Path("/usr/sbin/policy-rc.d")
POLICY_BYTES = b"#!/bin/sh\n# homelab-ops maintenance\nexit 101\n"


def run(*args):
    result = subprocess.run(args, check=True, text=True, capture_output=True, timeout=600)
    return result.stdout.strip()


def js(*args):
    return json.loads(run(*args))


def atomic(path, data):
    """Same-filesystem replace + fsync file and parent before acknowledging."""
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def locks_free():
    # Opening existing lockfiles read-only does not mutate a dry-run host.
    for name in ("/var/lib/dpkg/lock", "/var/lib/dpkg/lock-frontend",
                 "/var/lib/apt/lists/lock", "/var/cache/apt/archives/lock"):
        if Path(name).exists():
            with open(name, "rb") as stream:
                fcntl.lockf(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)


def units():
    result = {}
    lines = run("systemctl", "list-unit-files", "--no-legend", "--no-pager").splitlines()
    for line in lines:
        name, state, *_ = line.split()
        if (name.endswith(".timer") or name in ("cron.service", "pve-guests.service")
                or re.fullmatch(r"ceph-(?:mon|mgr|osd|mds)@[^.][^/]*\.service", name)):
            if "@" in name and name.startswith(name.split("@")[0] + "@."):
                # Uninstantiated template (e.g. zfs-scrub-@.timer): nothing to manage.
                continue
            active = subprocess.run(["systemctl", "is-active", name],
                                    text=True, capture_output=True, timeout=30).stdout.strip()
            result[name] = {"enabled": state, "active": active}
    # Instances may be enabled via target wants but absent from list-unit-files
    # (e.g. ceph-mds@<host>). Probe their state directly instead of failing.
    # Only templated daemon instances are managed; non-templated units such as
    # ceph-crash.service are out of scope.
    for line in run("systemctl", "list-units", "--all", "--plain", "--no-legend",
                    "--no-pager", "ceph-*.service").splitlines():
        name = line.split()[0]
        if not re.fullmatch(r"ceph-(?:mon|mgr|osd|mds)@[^.][^/]*\.service", name):
            continue
        if name in result:
            continue
        state = subprocess.run(["systemctl", "is-enabled", name],
                               text=True, capture_output=True, timeout=30).stdout.strip()
        if state not in {"enabled", "disabled", "static", "masked"}:
            # "indirect" etc.: effectively enabled via target wants.
            state = "enabled"
        active = subprocess.run(["systemctl", "is-active", name],
                                text=True, capture_output=True, timeout=30).stdout.strip()
        result[name] = {"enabled": state, "active": active}
    return result


def inventory():
    locks_free()
    if run("dpkg", "--audit"):
        raise ValueError("dpkg audit is not empty")
    name = run("hostname", "-s")
    os_release = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            os_release[key] = value.strip('"')
    guests = []
    for kind in ("qemu", "lxc"):
        for item in js("pvesh", "get", f"/nodes/{name}/{kind}", "--output-format", "json"):
            vmid = int(item["vmid"])
            config = js("pvesh", "get", f"/nodes/{name}/{kind}/{vmid}/config",
                        "--output-format", "json")
            status = js("pvesh", "get", f"/nodes/{name}/{kind}/{vmid}/status/current",
                        "--output-format", "json")
            # Do not persist config, descriptions, cloud-init values or credentials.
            guests.append({"id": vmid, "kind": kind, "node": name,
                           "status": status["status"], "qmpstatus": status.get("qmpstatus"),
                           "lock": config.get("lock"), "onboot": config.get("onboot"),
                           "template": bool(config.get("template", 0))})
    versions = {p: run("dpkg-query", "-W", "-f=${Version}", p)
                for p in ("pve-manager", "ceph-common", "base-files", "proxmox-ve")}
    return {"name": name, "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "os": os_release.get("ID"), "os_version": os_release.get("VERSION_ID"),
            "versions": versions, "guests": guests, "units": units(),
            "policy_exists": POLICY.exists() or POLICY.is_symlink(),
            "mounts": js("findmnt", "--json", "-o", "TARGET,SOURCE,FSTYPE",
                         "-t", "ceph,fuse.ceph,rbd")
            if subprocess.run(["findmnt", "-rn", "-t", "ceph,fuse.ceph,rbd"],
                              capture_output=True, timeout=30).returncode == 0 else {},
            "rbd_devices": js("rbd", "device", "list", "--format", "json"),
            "state_fstype": run("findmnt", "-n", "-o", "FSTYPE", "-T", "/var/lib"),
            "root": os.geteuid() == 0,
            "cluster": js("pvesh", "get", "/cluster/status", "--output-format", "json"),
            "ha": js("pvesh", "get", "/cluster/ha/resources", "--output-format", "json"),
            "active": ACTIVE.exists()}


def execute(request):
    action = request["action"]
    if action == "inventory":
        return inventory()
    if action == "command":
        return run(*request["argv"])
    if action == "lock":
        ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        ACTIVE.mkdir(mode=0o700)  # Atomic, no same-execution retry exception.
        atomic(ACTIVE / "owner", request["operation"].encode())
        sync_dir(ROOT)
        return True
    if action == "load":
        return json.loads((ACTIVE / "state.json").read_text())
    if action == "executor":
        if (ACTIVE / "owner").read_text() != request["operation"]:
            raise ValueError("lock owner mismatch")
        (ACTIVE / "executor").mkdir(mode=0o700)
        sync_dir(ACTIVE)
        return True
    if action == "save":
        if (ACTIVE / "owner").read_text() != request["operation"]:
            raise ValueError("lock owner mismatch")
        atomic(ACTIVE / "state.json", json.dumps(request["state"], sort_keys=True).encode())
        return True
    if action == "archive":
        if (ACTIVE / "owner").read_text() != request["operation"]:
            raise ValueError("lock owner mismatch")
        os.rename(ACTIVE, ROOT / ("completed-" + request["operation"]))
        sync_dir(ROOT)
        return True
    if action == "policy_install":
        if POLICY.exists() or POLICY.is_symlink():
            raise ValueError("existing policy-rc.d is unsupported")
        fd = os.open(POLICY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
        with os.fdopen(fd, "wb") as stream:
            stream.write(POLICY_BYTES)
            stream.flush()
            os.fsync(stream.fileno())
        sync_dir(POLICY.parent)
        return True
    if action == "policy_remove":
        if POLICY.is_symlink() or (POLICY.exists() and POLICY.read_bytes() != POLICY_BYTES):
            raise ValueError("policy-rc.d changed; operator intervention required")
        if POLICY.exists():
            POLICY.unlink()
            sync_dir(POLICY.parent)
        return True
    raise ValueError("unknown action")


if __name__ == "__main__":
    try:
        print(json.dumps(execute(json.load(sys.stdin))))
    except Exception as exc:
        # Remote command output can include repository URLs; do not echo it.
        print(type(exc).__name__ + ": host operation failed", file=sys.stderr)
        sys.exit(1)
