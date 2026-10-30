#!/usr/bin/env python3
"""Check public Markdown links and Kane System Pages identity without network access."""

from __future__ import annotations

import re
import sys
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
EXTERNAL = ("#", "http://", "https://", "mailto:", "data:")


def markdown_failures() -> list[str]:
    failures: list[str] = []
    for source in ROOT.rglob("*.md"):
        if ".git" in source.parts:
            continue
        text = source.read_text(encoding="utf-8")
        for match in LINK.finditer(text):
            raw = match.group(1).strip().split()[0].strip("<>")
            if not raw or raw.startswith(EXTERNAL):
                continue
            relative = urllib.parse.unquote(raw.split("#", 1)[0])
            if relative and not (source.parent / relative).resolve().exists():
                failures.append(f"{source.relative_to(ROOT)} -> {raw}")
    return failures


def page_failures() -> list[str]:
    page = (ROOT / "docs/index.html").read_text(encoding="utf-8")
    checks = {
        "Kane System title": "<title>Kane System v0.3" in page,
        "correct repository": "github.com/K-kanoloa/Kane-System" in page,
        "correct clone command": "git clone https://github.com/K-kanoloa/Kane-System.git" in page,
        "development status": "Development preview" in page,
        "no production installer claim": "No production release or public installer" in page,
        "license boundary": "PolyForm Noncommercial 1.0.0" in page,
        "no inherited product title": "Kane-Kanaloa v3.0.1-beta" not in page,
        "no inherited download": "Kane-kanoloa/archive" not in page,
    }
    return [name for name, passed in checks.items() if not passed]


def main() -> int:
    failures = markdown_failures()
    failures.extend(f"docs/index.html: {item}" for item in page_failures())
    for name in ("deploy/README.md", "connectors/README.md", "connectors/capabilities.schema.json",
                 "scripts/native_preflight.py", "scripts/requirements-native-tests.txt"):
        if not (ROOT / name).is_file():
            failures.append(f"required public dependency missing: {name}")
    if failures:
        print("Public documentation check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("Public documentation links and Kane System page identity: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
