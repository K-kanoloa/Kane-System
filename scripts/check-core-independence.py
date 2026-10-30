#!/usr/bin/env python3
"""Build/test/package an allowlisted source export without any Agent product.

Requires cached Cargo dependencies, Rust/C tools, git, Python and dpkg-deb.
No network, source deletion, installation or publishing is performed here.
The export retains the existing license; this check cannot approve relicensing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FILES = {".gitignore", "Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "LICENSE",
         "THIRD_PARTY_NOTICES.md", "deploy/README.md",
         "scripts/build-system-package.py"}
PREFIXES = ("core/", "models/", "supervisor/", "deploy/debian/",
            "deploy/systemd/", "deploy/defaults/")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = output / "source"
    source.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()

    files = git("ls-files").splitlines()
    selected = [name for name in files if name in FILES or name.startswith(PREFIXES)]
    assert FILES <= set(selected), "required tracked source missing"
    hashes = {}
    for name in selected:
        original = ROOT / name
        assert original.is_file() and not original.is_symlink()
        destination = source / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, destination)
        hashes[name] = hashlib.sha256(destination.read_bytes()).hexdigest()
    provenance = {"source_commit": git("rev-parse", "HEAD"),
                  "source_dirty": bool(git("status", "--porcelain")),
                  "files_sha256": hashes, "license": "inherited; no relicensing approval",
                  "excluded": ["apps", "packages", "node_modules", "Python environments",
                               "Kanaloa runtime", "Kane Harness", "original Git history"]}
    (output / "source-manifest.json").write_text(json.dumps(provenance, indent=2) + "\n")

    def run(name: str, *args: str) -> None:
        with (output / name).open("w") as log:
            subprocess.run(args, cwd=source, stdout=log, stderr=subprocess.STDOUT, check=True)

    # A new local export repository lets the existing builder record its precise
    # inputs. Its commit is distinct from, and mapped to, the preserved source.
    run("export-init.log", "git", "init", "--initial-branch=validation")
    run("export-add.log", "git", "add", ".")
    run("export-commit.log", "git", "-c", "user.name=Kane local validation",
        "-c", "user.email=validation@localhost", "-c", "commit.gpgsign=false",
        "-c", "core.hooksPath=/dev/null", "commit", "-m", "Allowlisted standalone validation export")
    export_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    # Fresh target directory, no compiled output copied from the original tree.
    run("rust-tests.log", "cargo", "test", "--workspace", "--locked", "--offline")
    run("release-build.log", "cargo", "build", "--release", "--locked", "--offline", "-p", "kane-supervisor")
    metadata = json.loads(subprocess.check_output(
        ["cargo", "metadata", "--format-version=1", "--locked", "--offline"], cwd=source, text=True))
    inventory = [{"name": p["name"], "version": p["version"], "source": p["source"],
                  "license": p["license"], "license_file_declared": bool(p["license_file"]),
                  "workspace_member": p["id"] in metadata["workspace_members"]} for p in metadata["packages"]]
    local = [p for p in metadata["packages"] if p["source"] is None]
    assert {p["name"] for p in local} == {"kane-core", "kane-models", "kane-supervisor"}
    assert all(Path(p["manifest_path"]).is_relative_to(source) for p in local)
    (output / "dependency-inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
    run("package-build.log", "python3", "scripts/build-system-package.py", "--version", "0.3.0~independence1")
    package = next((source / "dist/system").glob("*.deb"))
    binary = source / "target/release/kanesys"
    result = {"build_tests_package_passed": True, "source_commit": provenance["source_commit"],
              "export_commit": export_commit, "source_files": len(selected),
              "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
              "package_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
              "dependency_count_all_targets": len(inventory),
              "runtime_installation": "not tested by this script",
              "license_security_audit": "inventory only; not release clearance"}
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
