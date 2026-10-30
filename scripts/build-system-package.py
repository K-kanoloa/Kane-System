#!/usr/bin/env python3
"""Build an inspectable development system-service .deb; never installs it.

Package tooling is Python; authoritative runtime and control service are Rust.
The package is one component of the planned system, not an OS release.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "target/release/kanesys")
    parser.add_argument("--output", type=Path, default=ROOT / "dist/system")
    parser.add_argument("--version", default="0.3.0~dev1")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9][0-9a-zA-Z.+~]*", args.version):
        parser.error("version must be a simple Debian version without revision")
    binary = args.binary.resolve(strict=True)
    elf = binary.read_bytes()[:20]
    if elf[:6] != b"\x7fELF\x02\x01":
        parser.error("expected a 64-bit little-endian Linux ELF binary")
    arch = {62: "amd64", 183: "arm64"}.get(struct.unpack("<H", elf[18:20])[0])
    if arch is None:
        parser.error("unrecognized ELF architecture")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / f"kane-system-core_{args.version}_{arch}.deb"
    if artifact.exists():
        parser.error("refusing to overwrite an existing package")
    epoch = int(os.getenv("SOURCE_DATE_EPOCH") or subprocess.check_output(
        ["git", "show", "-s", "--format=%ct", "HEAD"], cwd=ROOT, text=True).strip())

    def copy(source: Path, dest: Path, mode: int = 0o644) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        dest.chmod(mode)

    with tempfile.TemporaryDirectory(prefix="kane-package-") as directory:
        stage = Path(directory)
        control = stage / "DEBIAN"
        control.mkdir()
        (control / "control").write_text(
            f"Package: kane-system-core\nVersion: {args.version}\nArchitecture: {arch}\n"
            "Maintainer: Kane System local development <root@localhost>\n"
            "Section: admin\nPriority: optional\n"
            "Depends: libc6 (>= 2.39), libgcc-s1, systemd (>= 255), util-linux, passwd\n"
            "Description: Kane System experimental native core system-service profile\n"
            " Rust registry, private Owner IPC and systemd service.\n"
            " This is development infrastructure, not a completed OS release.\n")
        for script in ("preinst", "postinst", "prerm", "postrm"):
            copy(ROOT / "deploy/debian" / script, control / script, 0o755)
        (control / "conffiles").write_text("/etc/kane-system/core.env\n")
        copy(binary, stage / "usr/bin/kanesys", 0o755)
        for source in (ROOT / "deploy/systemd").iterdir():
            copy(source, stage / "usr/lib/systemd/system" / source.name)
        copy(ROOT / "deploy/defaults/core.env", stage / "etc/kane-system/core.env")
        sysusers = stage / "usr/lib/sysusers.d/kane-system.conf"
        sysusers.parent.mkdir(parents=True)
        sysusers.write_text('u kane-system - "Kane System core" /var/lib/kane-system /usr/sbin/nologin\n')
        doc = stage / "usr/share/doc/kane-system-core"
        copy(ROOT / "deploy/README.md", doc / "README.md")
        copy(ROOT / "LICENSE", doc / "copyright")
        build = {
            "package": "kane-system-core", "version": args.version, "arch": arch,
            "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "source_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)),
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "cargo_lock_sha256": hashlib.sha256((ROOT / "Cargo.lock").read_bytes()).hexdigest(),
            "source_date_epoch": epoch, "release": False, "compatibility": "Unknown",
        }
        (doc / "build.json").write_text(json.dumps(build, indent=2) + "\n")
        # Explicit modes avoid inheriting the developer environment's private umask.
        for path in [stage, *stage.rglob("*")]:
            if path.is_dir():
                path.chmod(0o755)
            elif not os.access(path, os.X_OK):
                path.chmod(0o644)
            os.utime(path, (epoch, epoch))
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(stage), str(artifact)],
                       check=True, env={**os.environ, "SOURCE_DATE_EPOCH": str(epoch)})
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    artifact.with_suffix(".deb.sha256").write_text(f"{digest}  {artifact.name}\n")
    print(json.dumps({"artifact": str(artifact), "sha256": digest, "release": False}))


if __name__ == "__main__":
    main()
