#!/usr/bin/env python3
"""Boot an offline disposable Ubuntu VM to test the core component package.

Uses QEMU software emulation inside an ordinary, unprivileged tools container.
No host disks, devices, credentials, SSH keys, Docker socket or external network.
Download the Ubuntu cloud image separately and supply its verified SHA256.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--image-sha256", required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tools", default="kane-system-validation:20261018")
    parser.add_argument("--timeout", type=int, default=480)
    args = parser.parse_args()
    image, package = args.image.resolve(strict=True), args.package.resolve(strict=True)
    digest = hashlib.file_digest(image.open("rb"), "sha256").hexdigest()
    if digest != args.image_sha256.lower():
        parser.error("base image checksum mismatch")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    test_script = ROOT / "scripts/validate-system-guest.py"
    writes = []
    for dest, data, mode in [
        ("/var/tmp/kane-system-core.deb", package.read_bytes(), "0644"),
        ("/usr/local/libexec/kane-system-guest-test.py", test_script.read_bytes(), "0700"),
        ("/etc/kane-validation-guest", b"disposable-vm\n", "0600"),
    ]:
        writes.append({"path": dest, "permissions": mode, "encoding": "b64",
                       "content": base64.b64encode(data).decode()})
    # JSON is a YAML subset; no template expansion of package or script contents.
    user_data = {"users": [], "ssh_pwauth": False, "disable_root": True,
                 "ssh_genkeytypes": ["ed25519"], "package_update": False, "package_upgrade": False,
                 # This offline component test needs no SSH setup, locale/mirror
                 # probes, cloud account attachment, phone-home or package fetch.
                 "cloud_init_modules": ["seed_random", "write_files", "growpart", "resizefs", "set_hostname"],
                 "cloud_config_modules": ["runcmd"],
                 "cloud_final_modules": ["scripts_user", "final_message"],
                 "write_files": writes,
                 "runcmd": [["python3", "/usr/local/libexec/kane-system-guest-test.py", "/var/tmp/kane-system-core.deb"],
                             ["systemctl", "poweroff"]]}
    (output / "user-data").write_text("#cloud-config\n" + json.dumps(user_data))
    (output / "meta-data").write_text(json.dumps({"instance-id": output.name, "local-hostname": "kane-validation"}))
    # A restricted emulated DHCP link prevents the stock cloud image waiting for
    # nonexistent networking. The outer container has --network=none; the guest
    # cannot reach the host network or Internet.
    (output / "network-config").write_text(
        "version: 2\nethernets:\n  validation:\n    match: {name: 'en*'}\n    dhcp4: true\n    dhcp6: false\n    optional: true\n")
    prefix = ["docker", "run", "--rm", "--network=none", "--user", f"{os.getuid()}:{os.getgid()}",
              "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128", "--memory=2300m", "--cpus=2",
              "--mount", f"type=bind,src={output},dst=/work", "--workdir=/work"]
    subprocess.run([*prefix, args.tools, "genisoimage", "-quiet", "-output", "seed.iso", "-volid", "cidata",
                    "-joliet", "-rock", "user-data", "meta-data", "network-config"], check=True)
    base_mount = ["--mount", f"type=bind,src={image},dst=/base/cloud.img,readonly"]
    subprocess.run([*prefix, *base_mount, args.tools, "qemu-img", "create", "-f", "qcow2", "-F", "qcow2",
                    "-b", "/base/cloud.img", "system.qcow2", "8G"], check=True)
    container_name = "kane-system-vm-" + uuid.uuid4().hex[:12]
    command = [*prefix, *base_mount, "--name", container_name, args.tools, "qemu-system-x86_64", "-machine", "q35", "-accel", "tcg,thread=multi",
               "-cpu", "max", "-smp", "2", "-m", "1536", "-drive", "file=system.qcow2,if=virtio,format=qcow2",
               "-object", "rng-random,id=rng0,filename=/dev/urandom", "-device", "virtio-rng-pci,rng=rng0",
               "-drive", "file=seed.iso,media=cdrom,readonly=on", "-display", "none", "-serial", "stdio", "-monitor", "none",
               "-nic", "user,model=virtio-net-pci,restrict=on", "-no-reboot"]
    started = time.monotonic()
    with (output / "console.log").open("w") as log:
        try:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout)
            exit_code = result.returncode
        except subprocess.TimeoutExpired:
            exit_code = 124
            subprocess.run(["docker", "rm", "--force", container_name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=20, check=False)
    console = (output / "console.log").read_text(errors="replace")
    lines = [line[line.index("KANE_SYSTEM_RESULT "):] for line in console.splitlines() if "KANE_SYSTEM_RESULT " in line]
    report = {"passed": exit_code == 0 and "KANE_SYSTEM_ACCEPTANCE_PASS" in console and bool(lines),
              "qemu_exit": exit_code, "wall_seconds": time.monotonic()-started,
              "base_image_sha256": digest, "package_sha256": hashlib.file_digest(package.open("rb"), "sha256").hexdigest(),
              "tools_image": subprocess.check_output(["docker", "image", "inspect", args.tools, "--format", "{{.Id}}"], text=True).strip(),
              "guest": json.loads(lines[-1].split(" ", 1)[1]) if lines else None}
    if report["passed"]:
        # Same persistent disk, same seed identity, another actual Linux boot.
        with (output / "second-boot.log").open("w") as log:
            try:
                second = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout)
                second_exit = second.returncode
            except subprocess.TimeoutExpired:
                second_exit = 124
                subprocess.run(["docker", "rm", "--force", container_name], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=20, check=False)
        second_console = (output / "second-boot.log").read_text(errors="replace")
        boot_lines = [line[line.index("KANE_SYSTEM_BOOT_RESULT "):] for line in second_console.splitlines() if "KANE_SYSTEM_BOOT_RESULT " in line]
        report["second_boot"] = json.loads(boot_lines[-1].split(" ", 1)[1]) if boot_lines else None
        report["second_boot_exit"] = second_exit
        report["passed"] = second_exit == 0 and "KANE_SYSTEM_BOOT_PASS" in second_console and bool(boot_lines)
        if report["passed"]:
            report["passed"] = report["second_boot"]["workspace_id"] == report["guest"]["workspace_id"]
    report["wall_seconds"] = time.monotonic()-started
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        print("VM validation failed; inspect " + str(output / "console.log"), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
