#!/usr/bin/env python3
"""Run only in the disposable offline PR-4C VM, never on a production host."""
import json
import os
from pathlib import Path
import subprocess
import time
import traceback

P = Path
ROOT = P("/workspace/Kane-System")
OUT = P("/exchange")


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)


def main():
    assert P("/etc/kane-storage-runtime-validation").read_text() == "disposable-vm\n"
    OUT.mkdir(exist_ok=True)
    run(["mount", "-t", "9p", "-o", "trans=virtio,version=9p2000.L", "evidence", str(OUT)])
    run(["mkdir", "-p", "/mnt/payload"])
    run(["mount", "-o", "ro", "/dev/sr0", "/mnt/payload"])
    run(["tar", "-xzf", "/mnt/payload/payload.tar.gz", "-C", "/"])
    run(["tar", "-xzf", "/mnt/payload/source-overlay.tar.gz", "-C", "/"])
    run(["umount", "/mnt/payload"])
    docker_log = (OUT/"dockerd.log").open("w")
    daemon = subprocess.Popen([
        "/usr/local/bin/dockerd", "--host=unix:///var/run/docker.sock",
        "--storage-driver=vfs", "--iptables=false", "--ip6tables=false",
        "--bridge=none", "--ip-forward=false", "--ip-masq=false",
    ], stdout=docker_log, stderr=subprocess.STDOUT)
    deadline = time.monotonic()+45
    while subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        if daemon.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError("isolated Docker daemon failed")
        time.sleep(.2)
    os.chown("/var/run/docker.sock", 0, 1000)
    os.chmod("/var/run/docker.sock", 0o660)
    run(["docker", "load", "-i", "/var/tmp/kane-reference-image.tar"], timeout=120)
    tests = [
        "test_bounded_home_persists_original_session_without_replay",
        "test_enospc_keeps_task_uncertain_and_does_not_replay",
    ]
    report = {"scope": "original Kanaloa + mock + bounded ext4 home; disposable VM",
              "os": P("/etc/os-release").read_text(), "kernel": run(["uname", "-r"]).stdout.strip(),
              "cases": []}
    home = P("/mnt/kane-home")
    home.mkdir(exist_ok=True)
    evidence = P("/var/tmp/kane-evidence")
    evidence.mkdir(exist_ok=True)
    os.chown(evidence, 1000, 1000)
    for index, test in enumerate(tests):
        image = f"/var/tmp/runtime-home-{index}.ext4"
        run(["truncate", "-s", "64M", image])
        run(["mkfs.ext4", "-q", "-F", "-m", "0", image])
        run(["mount", "-o", "loop,nosuid,nodev,noexec", image, str(home)])
        os.chown(home, 1000, 1000)
        os.chmod(home, 0o700)
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/ubuntu",
               "PYTHONPATH": f"{ROOT}/apps/api:/opt/kane-test-python",
               "KANE_PR1_TEST": "1", "KANE_PR2_TEST": "1", "KANE_PR4_TEST": "1",
               "KANE_STORAGE_TEST": "1", "KANE_STORAGE_HOME": str(home)}
        command = ["setpriv", "--reuid=1000", "--regid=1000", "--clear-groups",
                   "python3", "-m", "pytest", "-q", "-rs", "-s", "-p", "no:cacheprovider",
                   f"apps/api/tests/test_storage_runtime.py::{test}",
                   "--basetemp", str(evidence/str(index))]
        with (OUT/f"case-{index}.log").open("w") as log:
            try:
                code = subprocess.run(command, cwd=ROOT, env=env, stdout=log,
                                      stderr=subprocess.STDOUT, timeout=360).returncode
            except subprocess.TimeoutExpired:
                code = 124
        report["cases"].append({"test": test, "exit_code": code})
        # Preserve bounded test evidence, including native persistence on failures.
        # Setup failures can precede pytest's creation of --basetemp. Preserve
        # the failure result even then; missing evidence never becomes a pass.
        members = [str(p).lstrip("/") for p in (evidence/str(index), home) if p.exists()]
        archived = subprocess.run(
            ["tar", "-czf", str(OUT/f"case-{index}-evidence.tar.gz"),
             "--exclude=capacity-fill", "-C", "/", *members],
            text=True, capture_output=True, timeout=60,
        )
        report["cases"][-1]["evidence_exit_code"] = archived.returncode
        if archived.returncode:
            (OUT/f"case-{index}-archive-error.txt").write_text(archived.stderr)
        (OUT/"guest-progress.json").write_text(json.dumps(report, indent=2))
        if code != 0:
            break  # Do not unmount or overwrite a failed scenario's filesystem.
        run(["umount", str(home)])
    report["passed"] = len(report["cases"]) == len(tests) and all(
        c["exit_code"] == 0 and c["evidence_exit_code"] == 0 for c in report["cases"])
    (OUT/"guest-result.json").write_text(json.dumps(report, indent=2))
    daemon.terminate()
    try:
        daemon.wait(timeout=15)
    except subprocess.TimeoutExpired:
        daemon.kill()
        daemon.wait()
    docker_log.close()


def run_guest():
    # Check BEFORE entering the shutdown finally block. Running/importing this
    # helper on an ordinary host must never schedule a poweroff.
    marker = P("/etc/kane-storage-runtime-validation")
    if os.geteuid() != 0 or not marker.is_file() or marker.read_text() != "disposable-vm\n":
        raise SystemExit("Refusing: disposable root VM marker required")
    try:
        main()
    except Exception:
        error = traceback.format_exc()
        print(error, flush=True)
        if OUT.is_mount():
            (OUT/"guest-error.txt").write_text(error)
    finally:
        subprocess.run(["systemctl", "poweroff"], check=False)


if __name__ == "__main__":
    run_guest()
