#!/usr/bin/env python3
"""Destructive package lifecycle checks; ONLY in the marked disposable test VM."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
import traceback

SOCKET = "/run/kane-system/core.sock"
STATE = Path("/var/lib/kane-system/state.db")


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
    print("RUN", " ".join(args), "EXIT", result.returncode, result.stdout, flush=True)
    if check and result.returncode:
        raise RuntimeError(f"command failed: {args[0]}")
    return result


def rpc(op: str, **payload):
    body = json.dumps({"protocol": 1, "request": {"op": op, **payload}}).encode()
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(SOCKET)
        connection.sendall(struct.pack("!I", len(body)) + body)
        def exact(count):
            data = b""
            while len(data) < count:
                chunk = connection.recv(count-len(data))
                if not chunk:
                    raise RuntimeError("incomplete frame")
                data += chunk
            return data
        length = struct.unpack("!I", exact(4))[0]
        assert 0 < length <= 16_000_000
        return json.loads(exact(length))


def ready():
    deadline = time.monotonic() + 20
    while True:
        try:
            result = rpc("health")
            if result.get("ok"):
                return result
        except (OSError, RuntimeError):
            pass
        assert time.monotonic() < deadline, "core service failed to become ready"
        time.sleep(0.1)


def validate(package: str) -> dict:
    assert os.geteuid() == 0
    assert Path("/etc/kane-validation-guest").read_text() == "disposable-vm\n"
    assert Path("/proc/1/comm").read_text().strip() == "systemd"
    assert Path("/sys/fs/cgroup/cgroup.controllers").is_file()
    environment = {"os": Path("/etc/os-release").read_text(),
                   "kernel": run("uname", "-r").stdout.strip(),
                   "arch": run("uname", "-m").stdout.strip(),
                   "cgroup": Path("/sys/fs/cgroup/cgroup.controllers").read_text().strip(),
                   "cpu": os.cpu_count(), "model_backend": "none; system-service test"}
    run("dpkg", "--install", package)
    ready()
    run("systemctl", "is-active", "kane-system.target", "kane-core.service")
    run("systemctl", "is-enabled", "kane-system.target")
    run("systemd-analyze", "verify", "kane-core.service", "kane-system.target")
    uid = int(run("id", "-u", "kane-system").stdout.strip())
    assert uid != 0
    assert run("getent", "passwd", "kane-system").stdout.rstrip().endswith("/usr/sbin/nologin")
    assert STATE.stat().st_uid == uid and STATE.stat().st_mode & 0o777 == 0o600
    assert STATE.parent.stat().st_mode & 0o777 == 0o700
    assert Path(SOCKET).stat().st_mode & 0o777 == 0o600
    pid = run("systemctl", "show", "--property=MainPID", "--value", "kane-core.service").stdout.strip()
    process_status = dict(line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines() if ":" in line)
    assert int(process_status["Uid"].split()[1]) == uid
    assert int(process_status["CapEff"].strip(), 16) == 0
    assert process_status["NoNewPrivs"].strip() == "1"
    # Probe the real kernel permission boundary from a different UID.
    denied = "import socket; s=socket.socket(socket.AF_UNIX); s.connect('/run/kane-system/core.sock')"
    assert run("runuser", "-u", "nobody", "--", "python3", "-c", denied, check=False).returncode != 0
    denied = "open('/var/lib/kane-system/state.db','rb')"
    assert run("runuser", "-u", "nobody", "--", "python3", "-c", denied, check=False).returncode != 0
    workspace = rpc("create_workspace", kind="document")["result"]
    assert workspace["id"]
    before = time.monotonic()
    run("systemctl", "kill", "--signal=SIGKILL", "--kill-whom=main", "kane-core.service")
    ready()
    restart_seconds = time.monotonic() - before
    next_pid = run("systemctl", "show", "--property=MainPID", "--value", "kane-core.service").stdout.strip()
    assert next_pid != pid
    assert rpc("workspace", id=workspace["id"])["result"]["id"] == workspace["id"]
    backup = "/var/lib/kane-system/acceptance-backup.db"
    assert rpc("backup", destination=backup)["ok"]
    assert not rpc("backup", destination=backup)["ok"]
    assert not rpc("backup", destination="/etc/kane-should-not-write")["ok"]
    assert not Path("/etc/kane-should-not-write").exists()
    run("systemctl", "stop", "kane-system.target")
    assert run("systemctl", "is-active", "kane-core.service", check=False).returncode != 0
    run("runuser", "-u", "kane-system", "--", "kanesys", "--db", "/var/lib/kane-system/restored.db", "restore", backup)
    restored = run("runuser", "-u", "kane-system", "--", "kanesys", "--db", "/var/lib/kane-system/restored.db", "inspect", "workspace", workspace["id"])
    assert json.loads(restored.stdout)["id"] == workspace["id"]
    config = Path("/etc/kane-system/core.env")
    config.write_text(config.read_text().replace("/var/lib/kane-system/state.db", "/var/lib/kane-system/restored.db"))
    run("systemctl", "start", "kane-system.target")
    ready()
    after_recovery = rpc("create_workspace", kind="document")["result"]
    with config.open("a") as stream:
        stream.write("\n# Owner configuration preserved by upgrade\n")
    run("dpkg", "--force-confold", "--install", package)
    ready()
    assert "Owner configuration preserved" in config.read_text()
    upgrade_snapshots = list(STATE.parent.glob("backups/upgrade.*/state.db"))
    assert upgrade_snapshots
    # The upgrade backup must follow the selected recovered DB, not stale defaults.
    checked_backup = run("runuser", "-u", "kane-system", "--", "kanesys", "--db", str(upgrade_snapshots[-1]), "inspect", "workspace", after_recovery["id"])
    assert json.loads(checked_backup.stdout)["id"] == after_recovery["id"]
    assert rpc("workspace", id=workspace["id"])["result"]["id"] == workspace["id"]
    run("dpkg", "--remove", "kane-system-core")
    assert STATE.is_file() and config.is_file()
    assert not Path("/usr/bin/kanesys").exists()
    run("dpkg", "--install", package)
    ready()
    assert rpc("workspace", id=workspace["id"])["result"]["id"] == workspace["id"]
    assert rpc("workspace", id=after_recovery["id"])["result"]["id"] == after_recovery["id"]
    run("dpkg", "--purge", "kane-system-core")
    assert STATE.is_file()
    assert Path("/var/lib/kane-system/restored.db").is_file()
    assert int(run("id", "-u", "kane-system").stdout.strip()) == uid
    run("dpkg", "--install", package)
    ready()
    assert rpc("workspace", id=workspace["id"])["result"]["id"] == workspace["id"]
    run("journalctl", "--unit=kane-core.service", "--no-pager", "--lines=35")
    # Enabled now, executed only by the next real system boot. VM-only unit.
    Path("/etc/kane-validation-boot.json").write_text(json.dumps({"workspace_id": workspace["id"]}))
    Path("/etc/systemd/system/kane-validation-boot.service").write_text(
        "[Unit]\nDescription=Disposable VM second boot acceptance\nAfter=kane-system.target\n"
        "Requires=kane-system.target\nConditionPathExists=/etc/kane-validation-guest\n"
        "[Service]\nType=oneshot\nExecStart=/usr/bin/python3 /usr/local/libexec/kane-system-guest-test.py --boot-check\n"
        "ExecStartPost=/usr/bin/systemctl --no-block poweroff\nStandardOutput=tty\nStandardError=tty\nTTYPath=/dev/ttyS0\n"
        "[Install]\nWantedBy=multi-user.target\n")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "kane-validation-boot.service")
    return {"passed": True, "environment": environment, "restart_seconds_single_run": restart_seconds,
            "workspace_id": workspace["id"], "service_uid": uid,
            "build": json.loads(Path("/usr/share/doc/kane-system-core/build.json").read_text()),
            "scope": "actual Ubuntu/systemd core component lifecycle; no Agent/model/desktop/whole-OS acceptance"}


if __name__ == "__main__":
    try:
        if len(sys.argv) == 2 and sys.argv[1] == "--boot-check":
            assert Path("/etc/kane-validation-guest").read_text() == "disposable-vm\n"
            ready()
            run("systemctl", "is-active", "kane-system.target", "kane-core.service")
            expected = json.loads(Path("/etc/kane-validation-boot.json").read_text())["workspace_id"]
            assert rpc("workspace", id=expected)["result"]["id"] == expected
            print("KANE_SYSTEM_BOOT_RESULT " + json.dumps({"passed": True, "workspace_id": expected,
                                                         "pid1": Path("/proc/1/comm").read_text().strip()}), flush=True)
            print("KANE_SYSTEM_BOOT_PASS", flush=True)
            sys.exit(0)
        result = validate(sys.argv[1])
    except Exception:
        traceback.print_exc()
        run("systemctl", "status", "kane-core.service", "--no-pager", check=False)
        run("journalctl", "--unit=kane-core.service", "--no-pager", "--lines=80", check=False)
        print("KANE_SYSTEM_ACCEPTANCE_FAIL", flush=True)
        sys.exit(1)
    print("KANE_SYSTEM_RESULT " + json.dumps(result), flush=True)
    print("KANE_SYSTEM_ACCEPTANCE_PASS", flush=True)
