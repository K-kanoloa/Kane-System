#!/usr/bin/env python3
"""Offline disposable VM for the existing Kanaloa container tests and ext4 home."""
import argparse
import base64
import gzip
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--image-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tools", default="kane-system-validation:20261018")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--reuse-payload", type=Path, help="Reuse a checksum-verified local dependency snapshot")
    parser.add_argument("--guest-script", type=Path, help="Explicit disposable-VM validation entry point")
    parser.add_argument("--boots", type=int, choices=[1, 2], default=1)
    parser.add_argument("--test-set", choices=["normal-stop", "phase1", "phase2-final", "phase3"], help="Additional disposable-VM acceptance gate")
    parser.add_argument("--reuse-vm", type=Path, help="Read-only dependency snapshot from a stopped single-boot validation VM")
    parser.add_argument("--reuse-vm-sha256")
    parser.add_argument("--extra-payload",type=Path,help="Owner-selected local offline model/test dependency archive")
    parser.add_argument("--vm-memory-mib",type=int,default=3072,choices=[3072,4096])
    args = parser.parse_args()
    image = args.image.resolve(strict=True)
    assert sha(image) == args.image_sha256, "base image checksum mismatch"
    if args.reuse_vm:
        assert args.reuse_payload and ((args.boots==1 and args.test_set in ('normal-stop','phase1','phase2-final')) or (args.boots==2 and args.test_set=='phase3'))
        args.reuse_vm=args.reuse_vm.resolve(strict=True)
        assert sha(args.reuse_vm)==args.reuse_vm_sha256, "dependency VM checksum mismatch"
        prior=json.loads((args.reuse_vm.parent/'manifest.json').read_text())
        assert prior['base_image']==args.image_sha256 and prior['payload']==json.loads((args.reuse_payload.parent/'manifest.json').read_text())['payload']
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    # Snapshot only the known test sources/dependencies, never environment secrets.
    paths = ["apps/api/app", "apps/api/tests", "adapters/dsh", "bench/pr2",
             "packages/kanaloa-tools", "bench/phase2", "node_modules", "target/debug/kanesys"]
    manifest = {}
    if args.reuse_payload:
        previous = args.reuse_payload.resolve(strict=True)
        manifest = json.loads((previous.parent/"manifest.json").read_text())
        assert sha(previous) == manifest["payload"], "cached payload checksum mismatch"
        reference = subprocess.check_output(
            ["docker", "image", "inspect", "ubuntu:24.04", "--format", "{{.Id}}"], text=True).strip()
        assert manifest["reference_image"] == reference, "cached reference image differs from local image"
        # A local hard link avoids copying immutable dependency data. Never edit it.
        try:
            os.link(previous, out/"payload.tar.gz")
            manifest["payload_reference"] = "immutable hardlink"
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
            if args.reuse_vm:
                # Cached VM already contains these dependencies; this host-only
                # reference is hashed but never read through the container mount.
                (out/"payload.tar.gz").symlink_to(previous)
                manifest["payload_reference"] = "immutable host symlink; cached VM"
            else:
                shutil.copy2(previous, out/"payload.tar.gz")
                manifest["payload_reference"] = "verified cross-filesystem copy"
    else:
        build_payload(out, paths, manifest)
    # Always snapshot current first-party code and test changes separately. The
    # immutable cached dependencies never silently substitute an older test.
    with tarfile.open(out/"source-overlay.tar.gz", "w:gz", compresslevel=1) as archive:
        for name in paths:
            if name == "node_modules":
                continue
            archive.add(ROOT/name, arcname=f"workspace/Kane-System/{name}",
                        filter=lambda info: None if "__pycache__" in info.name.split("/") else info)
    if args.extra_payload:
        shutil.copy2(args.extra_payload,out/"extra-payload.tar.gz")
        manifest["extra_payload"]=sha(out/"extra-payload.tar.gz")
    manifest["source_overlay"] = sha(out/"source-overlay.tar.gz")
    manifest["payload"] = sha(out/"payload.tar.gz")
    manifest["core"] = sha(ROOT/"target/debug/kanesys")
    manifest["base_image"] = args.image_sha256
    manifest["reference_image"] = subprocess.check_output(
        ["docker", "image", "inspect", "ubuntu:24.04", "--format", "{{.Id}}"], text=True).strip()
    tools = subprocess.check_output(["docker", "image", "inspect", args.tools, "--format", "{{.Id}}"], text=True).strip()
    manifest["tools_image"] = tools
    if args.reuse_vm:
        manifest['dependency_vm']={'sha256':args.reuse_vm_sha256,'base_image':args.image_sha256,'payload':manifest['payload'],'read_only':True}
    manifest["guest_script_sha256"] = sha(args.guest_script or ROOT/"scripts/validate-storage-runtime-guest.py")
    manifest["validation_resources"] = {"vm_cpus": 2, "vm_memory_mib": args.vm_memory_mib,
                                        "outer_memory_bytes": 6*1024**3,
                                        "agent_cpu_millis": 2000, "agent_memory_bytes": 536870912,
                                        "disk_cache": "none", "disk_discard": "unmap on disposable test disk"}
    if args.guest_script:
        manifest["validation_resources"].pop("agent_cpu_millis")
        manifest["validation_resources"].pop("agent_memory_bytes")
        manifest["validation_resources"]["agent_limits_source"] = "Per-Agent runtime.json and actual cgroup evidence"
    (out/"manifest.json").write_text(json.dumps(manifest, indent=2))
    run_vm(args, image, out, manifest, tools)
    result = json.loads((out/"result.json").read_text())
    return 0 if result["passed"] else 1


def build_payload(out, paths, manifest):
    with tarfile.open(out/"payload.tar.gz", "w:gz", compresslevel=1) as archive:
        for name in paths:
            source = ROOT/name
            archive.add(source, arcname=f"workspace/Kane-System/{name}",
                        filter=lambda info: None if "__pycache__" in info.name.split("/") else info)
        archive.add(ROOT/"apps/api/.venv/lib/python3.12/site-packages", arcname="opt/kane-test-python")
        for name in ("docker", "dockerd", "containerd", "containerd-shim-runc-v2",
                     "docker-init", "docker-proxy", "runc"):
            source = Path("/usr/local/bin")/name
            archive.add(source, arcname=f"usr/local/bin/{name}")
            manifest[name] = sha(source)
        source = Path(shutil.which("node")).resolve()
        archive.add(source, arcname="usr/local/bin/node")
        manifest["node"] = sha(source)
        subprocess.run(["docker", "save", "-o", str(out/"reference-image.tar"), "ubuntu:24.04"], check=True)
        archive.add(out/"reference-image.tar", arcname="var/tmp/kane-reference-image.tar")


def run_vm(args, image, out, manifest, tools):
    script = (args.guest_script or ROOT/"scripts/validate-storage-runtime-guest.py").read_bytes()
    cloud = {
        "users": [], "ssh_pwauth": False, "disable_root": True,
        "package_update": False, "package_upgrade": False,
        "cloud_init_modules": ["seed_random", "write_files", "growpart", "resizefs", "set_hostname"],
        "cloud_config_modules": ["runcmd"], "cloud_final_modules": ["scripts_user", "final_message"],
        "write_files": [
            {"path": "/etc/kane-storage-runtime-validation", "permissions": "0644", "content": "disposable-vm\n"},
            {"path": "/usr/local/libexec/kane-storage-runtime-test.py", "permissions": "0700",
             "encoding": "gz+b64", "content": base64.b64encode(gzip.compress(script, mtime=0)).decode()},
        ],
        "runcmd": [["python3", "/usr/local/libexec/kane-storage-runtime-test.py"]],
    }
    if args.test_set:
        cloud["write_files"].append({"path":"/etc/kane-pr4-test-set", "permissions":"0644", "content":args.test_set+"\n"})
    if args.reuse_vm:
        cloud['write_files'].append({'path':'/etc/kane-pr4-cached-dependencies','permissions':'0644','content':manifest['payload']+'\n'})
    if args.boots == 2:
        cloud["write_files"].append({"path":"/etc/systemd/system/kane-pr4-validation.service", "permissions":"0644",
            "content":"[Unit]\nDescription=Disposable PR-4 validation only\nAfter=local-fs.target\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 /usr/local/libexec/kane-storage-runtime-test.py\nTimeoutStartSec=1800\nTimeoutStopSec=15\nKillSignal=SIGKILL\n[Install]\nWantedBy=multi-user.target\n"})
        cloud["runcmd"] = [["systemctl","enable","kane-pr4-validation.service"],
                           ["systemctl","start","--no-block","kane-pr4-validation.service"]]
    (out/"user-data").write_text("#cloud-config\n"+json.dumps(cloud))
    (out/"meta-data").write_text(json.dumps({"instance-id": out.name, "local-hostname": "kane-storage-runtime-test"}))
    (out/"network-config").write_text("version: 2\nethernets:\n  offline:\n    match: {name: 'en*'}\n    dhcp4: false\n    optional: true\n")
    prefix = ["docker", "run", "--rm", "--network=none", "--user", f"{os.getuid()}:{os.getgid()}",
              "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128",
              "--memory=6g", "--cpus=2", "--mount", f"type=bind,src={out},dst=/work",
              "--workdir=/work"]
    subprocess.run([*prefix, tools, "genisoimage", "-quiet", "-output", "seed.iso",
                    "-volid", "cidata", "-joliet", "-rock", "user-data", "meta-data",
                    "network-config", *([] if args.reuse_vm else ["payload.tar.gz"]), "source-overlay.tar.gz", *(["extra-payload.tar.gz"] if args.extra_payload else [])], check=True, timeout=60)
    base = ["--mount", f"type=bind,src={image},dst=/base/cloud.img,readonly"]
    if args.reuse_vm:
        base += ['--mount',f'type=bind,src={args.reuse_vm},dst=/base/previous.qcow2,readonly']
    subprocess.run([*prefix, *base, tools, "qemu-img", "create", "-f", "qcow2", "-F", "qcow2",
                    "-b", "/base/previous.qcow2" if args.reuse_vm else "/base/cloud.img", "system.qcow2", "8G"], check=True, timeout=30)
    name = "kane-storage-runtime-"+uuid.uuid4().hex[:12]
    command = [*prefix, *base, "--name", name, tools, "qemu-system-x86_64",
               "-machine", "q35", "-accel", "tcg,thread=multi", "-cpu", "max", "-smp", "2", "-m", str(args.vm_memory_mib),
               "-drive", "file=system.qcow2,if=virtio,format=qcow2,cache=none,discard=unmap,detect-zeroes=unmap",
               "-drive", "file=seed.iso,media=cdrom,readonly=on",
               "-virtfs", "local,path=/work,mount_tag=evidence,security_model=none,id=evidence",
               "-display", "none", "-serial", "stdio", "-monitor", "none", "-nic", "none", "-no-reboot"]
    start = time.monotonic()
    boots=[]
    for boot in range(args.boots):
        with (out/("console.log" if args.boots == 1 else f"boot-{boot+1}-console.log")).open("x") as log:
            try:
                if args.test_set=='phase3' and boot==0:
                    # Only this explicitly marked disposable reference VM is cut.
                    # No guest shutdown/fixture cleanup is performed before the loss.
                    process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
                    deadline=time.monotonic()+args.timeout
                    cut=False
                    while process.poll() is None:
                        marker=out/'phase3-power-cut-ready.json'
                        if marker.is_file():
                            ready=json.loads(marker.read_text())
                            assert ready.get('ready') is True and ready.get('boot_id'), 'invalid disposable fault marker'
                            time.sleep(.15)
                            killed=subprocess.run(['docker','kill','--signal=KILL',name],capture_output=True,text=True,timeout=20)
                            assert killed.returncode==0,killed.stderr
                            (out/'power-cut.json').write_text(json.dumps({'injected':True,'method':'SIGKILL disposable QEMU container; no guest shutdown','container':name,'marker':ready,'host_time':time.time()},indent=2))
                            cut=True
                            break
                        if time.monotonic()>deadline:
                            subprocess.run(['docker','rm','--force',name],capture_output=True,timeout=20)
                            break
                        time.sleep(.1)
                    code=process.wait(timeout=30)
                    if cut: assert code in (137,-9),'unexpected QEMU power-cut exit'
                else:
                    code = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout).returncode
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=20)
                code = 124
        intentional_cut=args.test_set=='phase3' and boot==0 and (out/'power-cut.json').is_file()
        boots.append({"boot":boot+1,"exit_code":code,"intentional_power_cut":intentional_cut})
        if (code != 0 and not intentional_cut) or (out/"guest-error.txt").exists(): break
    result = {"exit_code": code, "seconds": time.monotonic()-start, "manifest": manifest,
              "boots":boots,
              "guest": json.loads((out/"guest-result.json").read_text()) if (out/"guest-result.json").exists() else None}
    result["passed"] = len(boots) == args.boots and code == 0 and bool(result["guest"] and result["guest"]["passed"])
    (out/"result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
