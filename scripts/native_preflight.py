"""Read-only storage admission for expensive local Phase 6 validation.

The Owner's reference requirement is at least 10 GiB of persistent free space,
preferably 15 GiB. This is a prerequisite, never an OS acceptance result or a
reservation. No cleanup, formatting, capacity change, or remote Docker access.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

MIN_FREE_BYTES = 10 * 1024**3
RECOMMENDED_FREE_BYTES = 15 * 1024**3
VOLATILE_FILESYSTEMS = {"tmpfs", "ramfs", "devtmpfs"}


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)


def _mount_for(path: Path, mountinfo: str) -> tuple[str, str]:
    matches = []
    for line in mountinfo.splitlines():
        left, right = line.split(" - ", 1)
        mountpoint = Path(_unescape(left.split()[4]))
        if path == mountpoint or mountpoint in path.parents:
            matches.append((len(mountpoint.parts), str(mountpoint), right.split()[0]))
    if not matches:
        raise ValueError("mount information unavailable")
    _, mountpoint, filesystem = max(matches)
    return mountpoint, filesystem


def _probe(path: Path, mountinfo: str) -> dict:
    resolved = path.resolve()
    ancestor = resolved
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        raise ValueError("storage path has a non-directory ancestor")
    stat = os.statvfs(ancestor)
    mountpoint, filesystem = _mount_for(ancestor, mountinfo)
    return {
        "path": str(resolved), "existing_ancestor": str(ancestor),
        "device": ancestor.stat().st_dev, "mountpoint": mountpoint,
        "filesystem": filesystem, "free_bytes": stat.f_bavail * stat.f_frsize,
        "total_bytes": stat.f_blocks * stat.f_frsize,
        "free_inodes": stat.f_favail, "total_inodes": stat.f_files,
        "read_only": bool(stat.f_flag & os.ST_RDONLY),
    }


def _docker_root() -> Path:
    # Match the existing native controllers: only the authorized local daemon.
    env = os.environ.copy()
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        env.pop(key, None)
    result = subprocess.run(
        ["docker", "--host=unix:///var/run/docker.sock", "info", "--format", "{{json .DockerRootDir}}"],
        env=env, capture_output=True, text=True, check=True, timeout=10,
    )
    root = Path(json.loads(result.stdout))
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("local Docker data-root is not observable")
    return root


def capacity_report(*, output: Path, scratch: Path, workspace: Path) -> dict:
    report = {
        "status": "BLOCKED", "phase6_closed": False,
        "minimum_free_bytes_per_filesystem": MIN_FREE_BYTES,
        "recommended_free_bytes_per_filesystem": RECOMMENDED_FREE_BYTES,
        "scope": "read-only local capacity check; no reservation or acceptance claim",
        "paths": [], "reasons": [], "warnings": [],
    }
    paths = {"workspace": workspace, "output": output, "scratch": scratch}
    try:
        paths["docker_data_root"] = _docker_root()
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        report["reasons"].append({"role": "docker_data_root", "code": "unobservable", "error_type": type(error).__name__})
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text()
    except OSError as error:
        report["reasons"].append({"role": "mounts", "code": "unobservable", "error_type": type(error).__name__})
        return report
    for role, path in paths.items():
        try:
            row = {"role": role, **_probe(path, mountinfo)}
        except (OSError, ValueError) as error:
            report["reasons"].append({"role": role, "code": "unobservable", "error_type": type(error).__name__})
            continue
        report["paths"].append(row)
        if row["filesystem"] in VOLATILE_FILESYSTEMS:
            report["reasons"].append({"role": role, "code": "volatile_storage"})
        if row["read_only"]:
            report["reasons"].append({"role": role, "code": "read_only_storage"})
        if row["free_bytes"] < MIN_FREE_BYTES:
            report["reasons"].append({"role": role, "code": "insufficient_free_bytes"})
        elif row["free_bytes"] < RECOMMENDED_FREE_BYTES:
            report["warnings"].append({"role": role, "code": "below_recommended_headroom"})
        if row["total_inodes"] > 0 and row["free_inodes"] == 0:
            report["reasons"].append({"role": role, "code": "inode_exhausted"})
    # Shared mount capacity is not added once per path. Each required mount must
    # meet the same floor; separate tmpfs values never augment persistent bytes.
    report["filesystems_observed"] = len({row["device"] for row in report["paths"]})
    if not report["reasons"]:
        report["status"] = "PASS_PREFLIGHT_ONLY"
    return report


def require_native_storage(*, output: Path, scratch: Path, workspace: Path) -> dict:
    report = capacity_report(output=output, scratch=scratch, workspace=workspace)
    if report["status"] != "PASS_PREFLIGHT_ONLY":
        print(json.dumps(report, indent=2), file=sys.stderr)
        raise SystemExit(2)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    report = capacity_report(output=args.output, scratch=args.scratch, workspace=args.workspace)
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "PASS_PREFLIGHT_ONLY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
